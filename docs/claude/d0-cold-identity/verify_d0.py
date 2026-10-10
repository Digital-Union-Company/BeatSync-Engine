"""Recompute every D0 number and digest from the curated evidence in this directory.

Stdlib only, no repository imports, no network, no runtime dependency -- run it on any recent
CPython from anywhere:

    python docs/claude/d0-cold-identity/verify_d0.py

Exits 0 only if every check passes; prints the first failing comparison otherwise. This script is
evidence, not a test: `pytest.ini` sets `testpaths = tests`, so it is never collected.

It re-derives, from measurements.json / source_manifest.json / cache_keys.ordered.txt:

  * the frozen 1815-source manifest digest          96bc14cd...
  * the ordered cache-key digest                    bed9efdd...
  * identity invariance across 1/2/4/8/16 workers   one distinct key-dump hash
  * all ten reported medians
  * every cold step and pairwise difference, the warm ratios
  * both cold-first / fully-warm-subsequent crossover models (w1-vs-w16 and w8-vs-w16)
  * the sample-overlap facts behind the w4-vs-w8 and w8-vs-w16 qualifications
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

EXPECT_MANIFEST_SHA256 = "96bc14cd9014a5b9fd3fa8445494ab0fd88c26ef6909927f21c1ca2f0ab47c6c"
EXPECT_ORDERED_KEYS_SHA256 = "bed9efdd1776c9ee9d90977935363ab9a58652064733067c8adfbfe037818b54"
EXPECT_KEY_DUMP_SHA256 = "53b4c9b6905640f661b0783c2ba20dca4322667d9092d75f402c3316c8fab78e"

EXPECT_COLD = {1: 99.5217, 2: 110.3139, 4: 114.3353, 8: 114.0817, 16: 115.7938}
EXPECT_WARM = {1: 6.6150, 2: 3.7322, 4: 2.0465, 8: 1.6973, 16: 0.8070}

failures: list[str] = []


def check(label: str, got, want, tol: float | None = None) -> None:
    if tol is None:
        ok = got == want
    else:
        ok = abs(float(got) - float(want)) <= tol
    print("  [%s] %-52s got=%s want=%s" % ("OK" if ok else "FAIL", label, got, want))
    if not ok:
        failures.append(label)


def load(name):
    with open(os.path.join(HERE, name), "rb") as fh:
        return json.loads(fh.read().decode("utf-8-sig"))


# ---------------------------------------------------------------- digest formulas
# Reproduced verbatim from the D0 harness (harness/d0_common.py) so this script stands alone.
def manifest_digest(entries) -> str:
    d = hashlib.sha256()
    for item in entries:
        d.update(("%s\x00%s\x00%s\x1e"
                  % (item["path"], item["size"], item["mtime_ns"])).encode("utf-8"))
    return d.hexdigest()


def ordered_digest(values) -> str:
    d = hashlib.sha256()
    for index, value in enumerate(values):
        token = "\x00NONE\x00" if value is None else str(value)
        d.update(("%d\x00%s\x1e" % (index, token)).encode("utf-8"))
    return d.hexdigest()


man = load("source_manifest.json")
mm = load("measurements.json")
slots = mm["slots"]
valid = [s for s in slots if s["valid"]]

print("1) FROZEN SOURCE MANIFEST")
check("entry count", len(man["entries"]), 1815)
check("recomputed manifest digest", manifest_digest(man["entries"]), EXPECT_MANIFEST_SHA256)
check("digest matches recorded field", man["manifest_sha256"], EXPECT_MANIFEST_SHA256)
print()

print("2) ORDERED CACHE-KEY DIGEST (embeds the scratch cache dir -- see README)")
with open(os.path.join(HERE, "cache_keys.ordered.txt"), "rb") as fh:
    dump_bytes = fh.read()
check("key dump sha256", hashlib.sha256(dump_bytes).hexdigest(), EXPECT_KEY_DUMP_SHA256)
rows = [ln.split("\t") for ln in dump_bytes.decode("utf-8").splitlines() if ln.strip()]
check("key dump rows", len(rows), 1815)
check("positions are 0..1814 in order",
      [int(r[0]) for r in rows] == list(range(1815)), True)
cache_dir = mm["digest_reproduction"]["cache_dir_embedded_in_ordered_keys_digest"]
full = [cache_dir + "\\" + r[1] for r in rows]
check("recomputed ordered-keys digest", ordered_digest(full), EXPECT_ORDERED_KEYS_SHA256)
print()

print("3) IDENTITY INVARIANCE ACROSS WORKER COUNTS")
hashes = []
with open(os.path.join(HERE, "cache_key_dump_hashes.txt"), "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line and not line.startswith("#"):
            hashes.append(line.split()[0])
check("key dumps listed", len(hashes), 18)
check("distinct key-dump hashes", len(set(hashes)), 1)
check("that one hash", hashes[0], EXPECT_KEY_DUMP_SHA256)
digests = {s["ordered_keys_sha256"] for s in slots}
digests |= {s["warm_repeat_ordered_keys_sha256"] for s in slots
            if s["warm_repeat_ordered_keys_sha256"]}
check("distinct ordered_keys digests over all slots", len(digests), 1)
check("identity computations", mm["method"]["identity_computations_total"], 35)
for field in ("none_count", "exception_count", "duplicate_position_count",
              "manifest_drift_count", "cache_files_present_after"):
    check("every slot has %s == 0" % field, sum(s[field] for s in slots), 0)
check("every slot returned 1815 keys",
      sorted({s["returned_key_count"] for s in slots}), [1815])
check("every slot saw 1815 sources", sorted({s["source_count"] for s in slots}), [1815])
check("every slot used one manifest", sorted({s["manifest_sha256"] for s in slots}),
      [EXPECT_MANIFEST_SHA256])
print()

print("4) SLOT ACCOUNTING AND EXCLUSIONS")
timed = [s for s in slots if not s["is_prewarm"]]
check("timed slots", len(timed), 17)
check("valid slots", len(valid), 15)
excluded = sorted(s["slot"] for s in timed if not s["valid"])
check("excluded slots", excluded, ["d0_a1_w1", "d0_a2_w2"])
for s in timed:
    if not s["valid"]:
        over = s["reset"]["standby_after_mb"] > 200.0
        check("%s breached the 200 MB ceiling" % s["slot"], over, True)
    else:
        check("%s met standby_after <= 200 MB" % s["slot"],
              s["reset"]["standby_after_mb"] <= 200.0, True)
check("every reset used -Et", sorted({s["reset"]["switch"] for s in timed}), ["-Et"])
check("every RAMMap exit code 0", sorted({s["reset"]["rammap_exit_code"] for s in timed}), [0])
check("3 valid runs per worker count",
      sorted(len([s for s in valid if s["effective_workers"] == w]) for w in (1, 2, 4, 8, 16)),
      [3, 3, 3, 3, 3])
print()

print("5) ALL TEN MEDIANS, RECOMPUTED FROM THE ACCEPTED RAW RUNS")
cold_runs, warm_runs = {}, {}
for w in (1, 2, 4, 8, 16):
    cold_runs[w] = sorted(s["cold_seconds"] for s in valid if s["effective_workers"] == w)
    warm_runs[w] = sorted(s["warm_repeat_seconds"] for s in valid if s["effective_workers"] == w)
    check("w%-2d cold median" % w, round(st.median(cold_runs[w]), 4), EXPECT_COLD[w], 5e-5)
    check("w%-2d warm median" % w, round(st.median(warm_runs[w]), 4), EXPECT_WARM[w], 5e-5)
print()

print("6) COLD STEPS AND PAIRWISE DIFFERENCES (seconds)")
order = [1, 2, 4, 8, 16]
steps = {"1->2": 10.7922, "2->4": 4.0214, "4->8": -0.2536, "8->16": 1.7121}
for a, b in zip(order, order[1:]):
    check("step w%d->w%d" % (a, b), round(EXPECT_COLD[b] - EXPECT_COLD[a], 4),
          steps["%d->%d" % (a, b)], 5e-5)
check("w2->w16 remaining cold cost", round(EXPECT_COLD[16] - EXPECT_COLD[2], 4), 5.4799, 5e-5)
check("w1->w16 total cold cost", round(EXPECT_COLD[16] - EXPECT_COLD[1], 4), 16.2721, 5e-5)
check("front-loaded + remainder == total",
      round(10.7922 + 5.4799, 4), round(EXPECT_COLD[16] - EXPECT_COLD[1], 4), 5e-5)
check("w4/w8/w16 cluster span",
      round(max(EXPECT_COLD[k] for k in (4, 8, 16)) - min(EXPECT_COLD[k] for k in (4, 8, 16)), 4),
      1.7121, 5e-5)
print()

print("7) WARM SPEED-UP RATIOS vs WORKER 1")
for w, want in ((1, 1.00), (2, 1.77), (4, 3.23), (8, 3.90), (16, 8.20)):
    check("w%-2d warm ratio (2dp)" % w, round(EXPECT_WARM[1] / EXPECT_WARM[w], 2), want, 5e-3)
check("w16 warm ratio (4dp)", round(EXPECT_WARM[1] / EXPECT_WARM[16], 4), 8.1970, 5e-5)
print()

print("8) CONDITIONAL CUMULATIVE MODEL -- cold first, every later scan fully warm")
print("   MODEL ONLY. INTER_BATCH_CACHE_SURVIVAL = UNMEASURED.")


def cumulative(w, n):
    return EXPECT_COLD[w] + (n - 1) * EXPECT_WARM[w]


for n, want1, want16 in ((1, 99.5217, 115.7938), (2, 106.1367, 116.6008),
                         (3, 112.7517, 117.4078), (4, 119.3667, 118.2148),
                         (5, 125.9817, 119.0218)):
    check("scan %d cumulative w1" % n, round(cumulative(1, n), 4), want1, 5e-5)
    check("scan %d cumulative w16" % n, round(cumulative(16, n), 4), want16, 5e-5)
check("scan 4 advantage to w16", round(cumulative(1, 4) - cumulative(16, 4), 4), 1.1519, 5e-5)
x1 = 1 + (EXPECT_COLD[16] - EXPECT_COLD[1]) / (EXPECT_WARM[1] - EXPECT_WARM[16])
check("continuous crossover w1 vs w16", round(x1, 4), 3.8017, 5e-5)
check("first integer scan where w16 leads w1", int(x1) + 1, 4)
x8 = 1 + (EXPECT_COLD[16] - EXPECT_COLD[8]) / (EXPECT_WARM[8] - EXPECT_WARM[16])
check("continuous crossover w8 vs w16", round(x8, 4), 2.9231, 5e-5)
check("first integer scan where w16 leads w8", int(x8) + 1, 3)
print()

print("9) SAMPLE OVERLAP -- what n=3 does and does not establish")


def separated(a, b):
    return max(a) < min(b)


check("cold w1 strictly below w2", separated(cold_runs[1], cold_runs[2]), True)
check("cold w2 strictly below w4", separated(cold_runs[2], cold_runs[4]), True)
check("cold w8 strictly below w16", separated(cold_runs[8], cold_runs[16]), True)
check("cold w1 strictly below w16", separated(cold_runs[1], cold_runs[16]), True)
check("cold w8 NOT strictly below w4 (overlap)", separated(cold_runs[8], cold_runs[4]), False)
check("cold w4-w8 median margin", round(st.median(cold_runs[4]) - st.median(cold_runs[8]), 4),
      0.2536, 5e-5)
check("warm w8 strictly below w4", separated(warm_runs[8], warm_runs[4]), True)
check("warm w16 strictly below w1", separated(warm_runs[16], warm_runs[1]), True)
check("warm w16 NOT strictly below w8 (overlap)", separated(warm_runs[16], warm_runs[8]), False)
check("w1 warm run spread (max-min)",
      round(max(warm_runs[1]) - min(warm_runs[1]), 4), 5.2664, 5e-5)
print()

print("10) EFFECT OF RE-INCLUDING THE TWO EXCLUDED SLOTS")
ex = {s["slot"]: s["cold_seconds"] for s in slots if not s["valid"] and not s["is_prewarm"]}
check("d0_a1_w1 cold", round(ex["d0_a1_w1"], 4), 100.6211, 5e-5)
check("d0_a2_w2 cold", round(ex["d0_a2_w2"], 4), 110.7998, 5e-5)
w1_all = sorted(cold_runs[1] + [ex["d0_a1_w1"]])
check("w1 cold median with 4 runs", round(st.median(w1_all), 4), 99.6103, 5e-5)
check("exclusion made w1 look faster, i.e. favoured the REJECTED option",
      st.median(w1_all) > st.median(cold_runs[1]), True)
d = EXPECT_COLD[16] - st.median(w1_all)
check("crossover with both slots re-included still scan 4",
      int(1 + d / (EXPECT_WARM[1] - EXPECT_WARM[16])) + 1, 4)
print()

print("=" * 72)
if failures:
    print("VERIFY FAILED -- %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("D0 VERIFY: ALL CHECKS PASSED")
sys.exit(0)
