# D0 — COLD_PARALLEL_IDENTITY_REGRESSION corrective design + measurement

> ## ARCHIVED COPY — CORRECTED 2026-10-10 (D0-E1)
>
> This is the archived copy of the D0 write-up, placed under version control by the D0-E1 evidence
> archival task. It is **verbatim** the original except for one corrected attribution, marked inline
> below with `[CORRECTED D0-E1]`. No measurement, median, label or conclusion changed.
>
> **The slot-attribution slip.** The pre-registration section credited the cold timings
> *100.6211 s* and *110.7998 s* to the **replacement** slots `d0_r1_w1` / `d0_r2_w2`. Those two
> figures are in fact the **excluded** slots `d0_a1_w1` / `d0_a2_w2`. The replacements measured
> **99.1686 s** (w1) and **110.0748 s** (w2) — both inside the accepted spread at their setting, so
> the paragraph's *argument* (that the exclusion is a technicality rather than a timing judgement)
> was always correct; only the antecedent was wrong. Verified against the raw slot records; see
> `measurements.json` and run `verify_d0.py`.
>
> The immutable pre-correction copy is retained in the external archive named in `README.md`
> (sha256 `166473386bcd8cb65fdbe871abb9b534a23b65acec9d68bb4dd7b01b3c093ff0`).

> ## CORRECTED 2026-10-10 (D0-R1) — two claims in the original version were wrong
>
> Independent review found two unsupported claims in this document. Both are corrected below. The
> **measurements** (the run records under `runs/`, the key dumps, the medians, the Pareto labels and
> the parity result) are unaffected, and the recommendation is unchanged — it never rested on either
> claim.
>
> **1. The "w1 wins by ≤0.9 s for N ≤ 300" claim was FALSE and is withdrawn.** It came from scaling
> the measured per-source cost down to a hypothetical 300-source library — an extrapolation from the
> only size actually measured (1815 sources). On the measured library, worker 1's advantage is large
> for the first three scans: **16.2721 s / 10.4641 s / 4.6561 s** at 1 / 2 / 3 scans. Worker 16 first
> leads at the **4th** scan, by **1.1519 s**. Continuous crossover **3.8017 scans**. The
> `N >= 301` threshold is retained only as a *conditional model* statement, never as an observed
> production threshold.
>
> **2. The "≤0.41 % of the workflow" claim is WITHDRAWN and not replaced.** It extrapolated full
> preparation time from the historical 41-source / 490.8 s Qwen figure, which
> `DEFAULT_ANALYZE_BATCH_SIZE`'s own docstring explicitly labels an **order of magnitude** because
> candidate counts, clip lengths and per-source cost vary by more than 10x. No percentage of total
> preparation time is supportable from current data, and **no replacement percentage is offered**.
> Identity evidence is stated in absolute seconds only.
>
> `INTER_BATCH_CACHE_SURVIVAL = UNMEASURED` — the real number of later scans behaving fully warm,
> partially warm or cold is unknown, which affects the exact cumulative crossover in a real
> preparation workflow.

`RECOMMENDED_STRATEGY = B — keep the 16-worker default, accept the measured cold trade-off`
`STORAGE_DETECTION_REQUIRED = NO` · `NVME_SSD_STATUS = UNMEASURED` · `IDENTITY_PARITY = PASS`

Read-only milestone. No repository file changed, no branch, no commit, no PR, no implementation.

Authority: main `eabf342e22bbf6fe876d2006a0fd86bf8cbbd75f`, tree `548d302ca0f6e60034e62f4486380b901d4a9ac3`,
0 open PRs. `src/` and `tests/` are byte-identical to the H2 basis `dd26c4fe` (that merge was
docs-only), and the D0 export's `video_analysis.py` matches the git blob `67f54b9d` and H2's bytes
`c88e19d6`, so these timings are directly comparable to H2's rather than merely similar.

Library: `J:\New folder\Cuts`, **1815 sources**, 45.744 GiB, 5.163 GiB of bounded fingerprint windows,
manifest `96bc14cd9014a5b9fd3fa8445494ab0fd88c26ef6909927f21c1ca2f0ab47c6c` —
**`H2_LIBRARY_EXACT_MATCH = YES`**. Storage `J:` = `WDC WD30EFRX-68EUZN0`, SATA, MediaType `HDD`.

## The finding that decides the design: the cold cost is FRONT-LOADED at 1→2, not flat above it

**[CORRECTED D0-R2 — the original heading and claim were too strong and partly false.]** They read
"cold is a STEP at 1→2, then flat", "the entire cold penalty is paid going from 1 to 2 workers" and
"beyond that it plateaus within ~1.5 s". Not licensed by the table: the 1→2 transition is the largest
single increase (+10.7922 s) but **not** the whole w1→w16 cost, and w2..w16 span 5.4799 s rather than
~1.5 s.

H2 measured cold only at 1 and 16 workers, which left open the hope of an intermediate "sweet spot".
There is none — but the reason is the frontier shape, not flatness. Measured cold step sizes:

| transition | Δ cold |
|---|---|
| w1 → w2 | **+10.7922 s** (largest single increase) |
| w2 → w4 | +4.0214 s |
| w4 → w8 | −0.2536 s |
| w8 → w16 | +1.7121 s |
| **w2 → w16** | **+5.4799 s** (cost remaining above the first step) |
| **w1 → w16** | **+16.2721 s** (total) |

So the regression is **front-loaded** at 1→2, additional cold cost remains at higher worker counts,
w2 through w16 span **5.4799 s**, and only the higher-worker w4/w8/w16 measurements form a tighter
cluster spanning **1.7121 s**.

Medians of 3 valid controlled-cold runs each, balanced order frozen before any timing:

| workers | cold median | cold vs w1 | cold penalty | warm median | warm speed-up | Pareto |
|---|---|---|---|---|---|---|
| **1** | **99.5217 s** | 1.0000x | — | 6.6150 s | 1.00x | **PARETO** |
| 2 | 110.3139 s | 0.9022x | +10.792 s | 3.7322 s | 1.77x | **PARETO** |
| 4 | 114.3353 s | 0.8704x | +14.814 s | 2.0465 s | 3.23x | **DOMINATED** by 8 |
| 8 | 114.0817 s | 0.8724x | +14.560 s | 1.6973 s | 3.90x | **PARETO** |
| **16** | **115.7938 s** | 0.8595x | +16.272 s | **0.8070 s** | **8.20x** | **PARETO** |

`STATIC_POLICY_CANDIDATE = NONE.` No measured setting is both materially better cold than 16 and
materially close to it warm. Worker 1 is the only setting that avoids the regression, and it costs
8.2x warm throughput. Worker 4 is the one dominated point — worker 8 is faster on *both* axes, so
nobody should ever choose 4.

Per-setting runs (cold / warm, seconds):

```
w=1   cold [99.1686, 99.5217, 99.6989]      warm [6.4925,  6.6150, 11.7589]
w=2   cold [110.0748, 110.3139, 110.6744]   warm [3.7189,  3.7322,  6.3808]
w=4   cold [114.1619, 114.3353, 114.4101]   warm [2.0241,  2.0465,  3.2459]
w=8   cold [113.6586, 114.0817, 114.3318]   warm [1.1127,  1.6973,  1.7438]
w=16  cold [115.4680, 115.7938, 115.8657]   warm [0.7839,  0.8070,  1.1890]
```

Cold is tight (spread ≤1.1 s within every setting). **Warm is noisy** — worker 1 produced
6.4925 / 6.6150 / 11.7589 s. The median 6.6150 s agrees closely with H2's independent 6.585 s, so the
median is used; the mean (8.2888 s) is skewed by the single 11.76 s outlier and is reported but not
relied on. Every conclusion below was checked against both.

## Why this is NOT a storage-detection problem

Both the cold penalty and the warm benefit scale linearly with source count, so the crossover is
**scale-invariant**: concurrency 16 becomes cumulatively cheaper than 1 from the **3.80th** full
library scan onward (2.96th on the all-17-slot view).

And the workflow forces repeated full scans. Verified from source, not assumed:

| fact | evidence |
|---|---|
| Scan Library classifies the **full** source list | `gui.py:3146` passes `ready_paths` |
| Analyze's runtime-identity probe does **not** re-hash the library | `gui.py:3212` passes `[]`; `_cache_identity_workers(0)` returns `0`, so no pool starts |
| `analyze_video_sources` re-derives identity only for the selected batch | `gui.py:3226`, `subset = scan.subset_for_analysis_batch(batch_size)` |
| the recorded scan is **dropped** after each batch | `library_prep.record_analysis_complete` → `replace(state, scan=None, …)` |
| default batch size | `DEFAULT_ANALYZE_BATCH_SIZE = 100` |

So preparing N sources needs `ceil(N/100)` full-library scans. Cumulative identity seconds:

| N | scans | w=1 | w=2 | w=4 | w=8 | w=16 | best |
|---|---|---|---|---|---|---|---|
| 100 | 1 | **5.5** | 6.1 | 6.3 | 6.3 | 6.4 | w=1 (+0.9 s) |
| 300 | 3 | **18.6** | 19.5 | 19.6 | 19.4 | 19.4 | w=1 (+0.8 s) |
| 400 | 4 | 26.3 | 26.8 | 26.6 | 26.3 | **26.1** | w=16 |
| 1000 | 10 | 87.6 | 79.3 | 73.1 | 71.3 | **67.8** | w=16 (−19.8 s) |
| **1815** | **19** | 218.6 | 177.5 | 151.2 | 144.6 | **130.3** | **w=16 (−88.3 s)** |
| 5000 | 50 | 1167.1 | 807.7 | 591.2 | 543.4 | **427.9** | w=16 (−739.2 s) |

**[CORRECTED]** The table above scales the measured per-source cost to other library sizes, which is
an extrapolation from the single measured size (1815). The original claim that "w1 wins only for
N ≤ 300 and there by at most 0.9 s" is **withdrawn as false**.

On the **measured** 1815-source library, repeating the same full scan, worker 1's advantage is
substantial for the first three scans and worker 16 only leads from the fourth:

| full scans | w=1 | w=16 | advantage |
|---|---|---|---|
| 1 | 99.5217 s | 115.7938 s | **w1 by 16.2721 s** |
| 2 | 106.1367 s | 116.6008 s | **w1 by 10.4641 s** |
| 3 | 112.7517 s | 117.4078 s | **w1 by 4.6561 s** |
| 4 | 119.3667 s | 118.2148 s | w16 by 1.1519 s |
| 5 | 125.9817 s | 119.0218 s | w16 by 6.9599 s |

Continuous crossover: **3.8017 scans**. Under the cold-first / fully-warm-subsequent sensitivity
model, the crossover therefore occurs at the **fourth** full scan, corresponding to **more than 300
outstanding sources** at the default batch size of 100. That is a **conditional model statement, not
an observed production threshold** — `INTER_BATCH_CACHE_SURVIVAL = UNMEASURED`.

**This is the argument against Strategy D.** A storage-aware policy that detected the HDD and dropped
to worker 1 would make the HDD user's full 1815-source preparation go from **130.3 s to 218.6 s —
88.3 s worse**. Storage type is not the variable that decides the answer; cold-vs-warm *frequency* is,
and no storage query can observe that. Detection would solve the wrong problem.

## Scale of the identity-policy difference — in absolute seconds only

**[CORRECTED — the original "≤0.41 % of the workflow" claim is withdrawn and NOT replaced.]** It
extrapolated full preparation time from the historical 41-source / 490.8 s Qwen measurement.
`DEFAULT_ANALYZE_BATCH_SIZE`'s own docstring states that figure is an **order of magnitude** only,
because candidate counts, clip lengths and per-source cost vary by more than 10x. No percentage of
total preparation wall time is supportable from current data, and none is substituted.

What can be stated, in absolute measured terms:

| comparison | measured difference |
|---|---|
| single cold scan, w16 vs w1 | **+16.2721 s** (w16 slower) |
| each later warm scan, w16 vs w1 | **−5.8080 s** (w16 faster) |
| 19 repeats of the same scan, fully-warm model | w1 218.6 s vs w16 130.3 s → **88.3 s** |

So the identity-policy difference is measured in **seconds to minutes**, while Qwen preparation is
independently known to operate on a much larger timescale for substantial cold libraries. That is a
qualitative statement about relative magnitude and is deliberately **not** turned into a guaranteed
percentage.

### Sensitivity: the one unmeasured assumption, and why it does not change the decision

The table above assumes scans 2..n are warm. Between batches a Qwen analysis of 100 sources runs and
decodes video, which could evict the fingerprint windows. Let `f` be the fraction of repeat scans that
are fully cold again. `f` is **UNMEASURED**.

| f | w=1 | w=2 | w=4 | w=8 | w=16 | best | best-vs-worst spread |
|---|---|---|---|---|---|---|---|
| 0.00 | 218.6 | 177.5 | 151.2 | 144.6 | **130.3** | w=16 | 88.3 s |
| 0.10 | 385.8 | 369.3 | 353.3 | 346.9 | **337.3** | w=16 | 48.5 s |
| 0.25 | **636.7** | 657.1 | 656.5 | 650.4 | 647.8 | w=1 | 20.4 s |
| 1.00 | **1890.9** | 2096.0 | 2172.4 | 2167.6 | 2200.1 | w=1 | 309.2 s |

Break-even is **f ≈ 0.222** — worker 1 wins only once more than ~22 % (≈4 of 18) repeat scans lose
their cache entirely. Crucially, **as `f` rises every setting slows together**, so the spread between
best and worst *collapses* from 88.3 s to 20.4 s at f = 0.25. The `f` uncertainty changes which setting is
nominally best but makes the choice matter less, not more. It is therefore not a blocker for accepting
the current default, and it is the one cheap measurement that would be required before any future
change.

Headroom argument (bounding, not proof): the fingerprint windows are 5.16 GiB; after each cold run
standby held ~7.4–8.8 GB and the free list ~49.2–49.9 GB, so an intervening batch would have to
displace ~5 GiB of specific pages while ~49 GB of free list is consumed first. Full eviction of all 18
repeats is implausible; partial eviction is unmeasured.

## Cold method and validity

`RESET_METHOD = RAMMap64.exe -accepteula -Et` (Empty Standby List), RAMMap 1.63 SHA256
`e970913798…` verified at run time. **Never `-Es`** (Empty System Working Set — the H2 attempt-1
defect). No bulk-read eviction. `COLD_METHOD_PROVEN = YES` (H2 two-cycle validation).

17 slots ran; **15 valid**, 3 per worker count. Every purge evicted **98.46–99.98 %** of the standby
list (7.1–48.7 GB), RAMMap exit 0 throughout.

### Two slots invalid, and a flaw in my own pre-registration

`d0_a1_w1` (standby 49131.1 → 429.2 MB, 99.13 % evicted) and `d0_a2_w2` (33457.1 → 514.8 MB, 98.46 %)
**failed the pre-registered `standby_after ≤ 200 MB` criterion** and are retained as INVALID with that
reason.

Both purges plainly worked. The 200 MB ceiling was *absolute*, calibrated in H2 where standby-before
was ~7.5 GB; these two ran while standby held 49.1 GB and 33.5 GB with only 164.7 MB free, so a larger
absolute residual re-accumulated during the 2 s settle. **The criterion should have been relative
(percent evicted). That is an error in my pre-registration, recorded rather than silently corrected** —
the threshold stands as written, the slots stay invalid, and two replacement slots (`d0_r1_w1`,
`d0_r2_w2`, order fixed in advance) were added under `plan_amendment_1.json` to restore the required 3
valid runs per setting.

**[CORRECTED D0-E1 — the original sentence misattributed these two figures.]** It read: "Their cold
timings (100.6211 s at w1, 110.7998 s at w2) sit squarely with the valid runs at the same settings,
so excluding them is a technicality, not a timing judgement." The word *their* pointed at the
replacement slots, but **100.6211 s and 110.7998 s are the cold timings of the EXCLUDED slots**
`d0_a1_w1` and `d0_a2_w2`. The replacement slots measured **99.1686 s** (`d0_r1_w1`) and
**110.0748 s** (`d0_r2_w2`).

The point the sentence was making survives intact, and in fact holds for both pairs:

| slot | role | cold | accepted spread at that setting |
|---|---|---:|---|
| `d0_a1_w1` | excluded | 100.6211 s | w1 accepted 99.1686 – 99.6989 s |
| `d0_r1_w1` | replacement | 99.1686 s | inside |
| `d0_a2_w2` | excluded | 110.7998 s | w2 accepted 110.0748 – 110.6744 s |
| `d0_r2_w2` | replacement | 110.0748 s | inside |

So the two excluded runs sit just **above** their setting's accepted spread, and the replacements sit
inside it. Excluding them is a technicality of the pre-registered threshold, not a timing judgement —
and, as noted below, it moves worker 1's cold median *down* (99.6103 → 99.5217 s), which flatters the
**rejected** alternative rather than the retained decision.

Every statistic was computed **both** ways. The two views agree on every Pareto label, on
`STATIC_POLICY_CANDIDATE = NONE`, and on the crossover to within one scan. Including the invalid slots
would make worker 1 look slightly *worse*, so their exclusion is not favourable to the recommendation.

## Identity parity — hard gate PASS

```
ordered_keys_sha256 = bed9efdd1776c9ee9d90977935363ab9a58652064733067c8adfbfe037818b54
```

One digest across **all 35 identity computations** — 18 identity-computing runs (the 15 sweep slots,
the 2 replacement slots and the prewarm) plus 17 warm controls (the prewarm ran without one).
`none = 0`, `exceptions = 0`,
`manifest drift = 0`, `duplicate positions = 0`, `cache_files_present_after = 0` in every slot. One
distinct `video_analysis.py` sha256, one manifest, one backend token, one config token throughout.
`CACHE_CONTRACT_VERSION = stage5_cache_v3` and
`ANALYSIS_VERSION = auto_av_analysis_v8_llama_vulkan_batched` unchanged.

**The digest differs from H2's `f93300d4…` for a benign reason that must not be read as a parity
failure:** `ordered_keys_sha256` hashes the *full* cache path, which embeds
`VIDEO_ANALYSIS_CACHE_DIR`, and D0's scratch directory differs from H2's. The identities themselves are
provably identical — all 18 D0 key dumps are byte-identical to H2's
`53b4c9b6905640f661b0783c2ba20dca4322667d9092d75f402c3316c8fab78e`. The worker count reached no key at
any setting from 1 to 16, which is the invariant that matters.

## Storage inventory (§25)

| disk | model | media | bus | drive | free |
|---|---|---|---|---|---|
| 2 | WDC WD30EFRX-68EUZN0 | **HDD** | SATA | **J:** (library) | 451.0 GB |
| 0 | WDC WD30EFRX-68EUZN0 | HDD | SATA | D: | 1765.1 GB |
| 3 | WD_BLACK SN850X 1000GB | SSD | **NVMe** | C: (system) | 126.9 GB |
| 4 | WDC WDS256G1X0C-00ENX0 | SSD | **NVMe** | **E:** | **153.1 GB** |
| 1 | Corsair Force LE SSD | SSD | SATA | I: (repo) | 112.3 GB |

**An NVMe measurement is feasible**: `E:` is a non-system NVMe SSD with 153.1 GB free, enough for the
45.744 GiB frozen corpus without deleting anything. It was **not** performed. `NVME_SSD_STATUS =
UNMEASURED`.

It is not decision-critical: an SSD result could only *strengthen* the case for keeping 16 (if cold
also scales on NVMe, 16 is more clearly right), and it cannot overturn the HDD conclusion, which
already favours 16 on cumulative grounds. No synthetic small-file substitute was used.

Production has **zero** storage-detection seam: grep for `MediaType|SpindleSpeed|seek.?penalty|
GetDriveType|IOCTL|PhysicalDisk|is_ssd|is_hdd|storage_type|DeviceIoControl` across `src/**/*.py`
returns **0 hits**, and no `ctypes`, `win32` or PowerShell shell-out exists there. Adding one is a
platform change, not a tuning change.

## Decision matrix

| | A — lower static default | B — keep 16, accept | C — runtime self-adaptive | D — storage/seek-aware |
|---|---|---|---|---|
| `CORRECTNESS_RISK` | LOW | **LOW** | MEDIUM | MEDIUM |
| `IMPLEMENTATION_COMPLEXITY` | XS | **none** | M | L |
| `NEW_PLATFORM_DEPENDENCY` | NO | **NO** | NO | **YES** |
| `COLD_HDD_EVIDENCE` | **STRONG** | **STRONG** | PARTIAL | STRONG |
| `WARM_EVIDENCE` | **STRONG** | **STRONG** | PARTIAL | STRONG |
| `NVME_SSD_EVIDENCE` | NONE | NONE | NONE | NONE |
| `UNKNOWN_DEVICE_BEHAVIOR` | n/a | n/a | signal may be ambiguous | must be defined conservatively; **unknown must not mean SSD** |
| `MULTI_VOLUME_BEHAVIOR` | n/a | n/a | n/a | unsolved — sources may span HDD+SSD, UNC, Storage Spaces, RAID |
| `TESTABILITY` | easy | **nothing to test** | hard (needs OS cache state) | hard (needs device fixtures) |
| `RECOMMENDATION` | **NO** | **YES** | **NO** | **NO** |

**A — NO.** The sweep removes its premise. The cold cost is front-loaded at 1→2 (+10.7922 s) with a
further +5.4799 s above w2, so the only setting that avoids the regression is worker 1, at 8.2x warm
cost — and no intermediate setting buys back meaningful warm throughput cheaply. Worker 1 *is*
materially better for the first
three full scans (by 16.2721 / 10.4641 / 4.6561 s), but it is worse from the fourth onward, and no
setting is better on both axes. Worker 4 is strictly dominated by 8. There is no sweet spot to move to.

**B — YES.** Worker 16 is on the Pareto frontier and is the strongest measured warm setting by a wide
margin (8.20x). Under the cold-first / fully-warm-subsequent model it is cumulatively cheapest from
the fourth full scan onward. The burden of evidence is on *changing* a default, and nothing here meets
it: no static alternative dominates, storage detection observes the wrong abstraction, and no adaptive
signal is proven. The cold regression is real, documented and **not fixed** — it is accepted as an
explicit measured trade-off.

**C — NO.** `ADAPTIVE_RUNTIME_SIGNAL = NOT PROVEN`. §21 gates the probe on static results being
unsatisfactory; they are satisfactory, so no probe was run. Independently, the payoff is adverse: the
most an adaptive policy can win is the ≤16.27 s one-off cold penalty, while a misclassification costs
5.81 s on *every* subsequent scan — and the hard problem stands, that process-local state is not OS
file-cache authority and the OS may evict pages independently.

**D — NO.** Detection would make the HDD user 88.3 s worse at full preparation, and it answers the
wrong question — the deciding variable is cold-vs-warm frequency, which no storage query observes.
If it were ever revisited, the semantic signal is `StorageDeviceSeekPenaltyProperty`
(`DeviceSeekPenalty`) rather than the SSD/HDD product label, and §23's questions (multi-volume, UNC,
Storage Spaces, unknown media, elevation, per-invocation cost, safe degradation) would all need
answers first.

## Contract safety

No worker count, storage type, cache state or seek-penalty signal reaches `_video_signature`,
`_cache_path`, `_qwen_config_token`, any cache payload, `CACHE_CONTRACT_VERSION` or `ANALYSIS_VERSION`.
Worker count remains execution policy only, as proven by the single ordered digest across 1–16
workers. `BEATSYNC_CACHE_IDENTITY_WORKERS` is unchanged and still available for benchmarking.

Existing guards: `tests/test_stage5_cache_identity.py` + `tests/test_scale_diagnostics.py` →
**180 passed**.
