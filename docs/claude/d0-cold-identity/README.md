# D0 — cold cache-identity worker sweep: curated evidence

Durable, in-repository evidence for the decision recorded in
`.claude/rules/stage5-cache-identity.md` ("D0: the decision — `ACCEPTED_TRADE_OFF`, and 16 stays")
and `CHANGELOG-FORK.md`. It exists so that decision can be independently re-verified without the
original machine, and to satisfy `PCBUS-HK-v1`: temporary storage is a workspace, never an archive,
so evidence a committed decision rests on may not live only under `C:\tmp`.

**This directory changes no behaviour.** It is evidence and documentation. The 16-worker identity
default, `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION` and every runtime file are untouched by it.

```
COLD_PARALLEL_IDENTITY_REGRESSION_STATUS = ACCEPTED_TRADE_OFF
PRODUCTION_CHANGE_REQUIRED               = NO
STATIC_POLICY_CANDIDATE                  = NONE
RECOMMENDED_STRATEGY                     = B — KEEP 16
INTER_BATCH_CACHE_SURVIVAL               = UNMEASURED
NVME_SSD_STATUS                          = UNMEASURED
```

This is **acceptance, not a fix**: the cold first-touch penalty on a mechanical HDD remains
historically true and is not resolved by code.

## Verify it yourself

```bat
python docs\claude\d0-cold-identity\verify_d0.py
```

Stdlib only — no portable runtime, no CUDA, no FFmpeg, no repository imports, no network. It exits 0
only if every check passes, and re-derives from the files here:

| | |
|---|---|
| frozen manifest digest | `96bc14cd9014a5b9fd3fa8445494ab0fd88c26ef6909927f21c1ca2f0ab47c6c` |
| ordered cache-key digest | `bed9efdd1776c9ee9d90977935363ab9a58652064733067c8adfbfe037818b54` |
| key-dump content hash (invariance) | `53b4c9b6905640f661b0783c2ba20dca4322667d9092d75f402c3316c8fab78e` |
| all ten medians, all cold steps, all warm ratios | from the 15 accepted raw runs |
| both cumulative crossover models | w1-vs-w16 and w8-vs-w16 |
| the n=3 overlap facts | which comparisons are separated and which are not |

Both digest formulas are reproduced verbatim in `verify_d0.py` from `harness/d0_common.py`, so the
script stands alone.

## What was measured

`video_analysis._compute_cache_paths_parallel` — the real production helper, not a reimplementation —
over a frozen **1815-source** library on `J:` (`WDC WD30EFRX-68EUZN0`, SATA, MediaType **HDD**).
Cold state was forced with `RAMMap64.exe -accepteula -Et` (Empty Standby List) before every timed
run; `-Es` is explicitly forbidden in the plan, because it was the H2 attempt-1 defect that produced
six "cold" slots that were all warm. Each slot carries its own immediate no-reset **warm control**.

Run order was frozen *before any timing* (`benchmark_plan.json`,
`frozen_before_any_timing: true`) as a balanced rotation:

```
1, 2, 4, 8, 16 | 4, 8, 16, 1, 2 | 16, 1, 2, 4, 8
```

Each worker count appears once per block and in three different within-block positions, so
position-correlated drift cannot favour any single setting.

### The accepted medians

| workers | cold median | warm median | warm speed-up vs w1 |
|---|---:|---:|---:|
| 1 | 99.5217 s | 6.6150 s | 1.00x |
| 2 | 110.3139 s | 3.7322 s | 1.77x |
| 4 | 114.3353 s | 2.0465 s | 3.23x |
| 8 | 114.0817 s | 1.6973 s | 3.90x |
| 16 | 115.7938 s | 0.8070 s | 8.20x |

### Every accepted observation (seconds)

Medians above are the middle value of each row. `measurements.json` and `measurements.csv` carry
these per slot, with the reset telemetry.

| workers | cold runs | warm runs |
|---|---|---|
| 1 | 99.1686 · **99.5217** · 99.6989 | 6.4925 · **6.6150** · 11.7589 |
| 2 | 110.0748 · **110.3139** · 110.6744 | 3.7189 · **3.7322** · 6.3808 |
| 4 | 114.1619 · **114.3353** · 114.4101 | 2.0241 · **2.0465** · 3.2459 |
| 8 | 113.6586 · **114.0817** · 114.3318 | 1.1127 · **1.6973** · 1.7438 |
| 16 | 115.4680 · **115.7938** · 115.8657 | 0.7839 · **0.8070** · 1.1890 |

**Read the dispersion, not just the medians.** Cold is tight (≤1.1 s within every setting). **Warm is
noisy**: worker 1 spans 6.4925–11.7589 s, a 5.2664 s spread. The 8.20x warm ratio is therefore a
*median* ratio, not a guaranteed speed-up — the same three runs admit anywhere from ~5.5x
(w1 fastest ÷ w16 slowest) to ~15.0x (w1 slowest ÷ w16 fastest). The median is used because it
agrees closely with H2's independent 6.585 s at worker 1; the mean (8.2888 s) is skewed by the single
11.76 s outlier and was reported but not relied on. Every conclusion was checked against both.

### What n = 3 does and does not establish

With three runs per cell, only **complete separation** of the two samples is meaningful; it is the
strongest statement this design can support, and the sweep's load-bearing comparisons reach it:

| comparison | status |
|---|---|
| cold: w1 below w2, w2 below w4, w8 below w16, **w1 below w16** | separated — established |
| warm: w2 below w1, w4 below w2, w8 below w4, **w16 below w1** | separated — established |
| **cold: w8 vs w4** | **overlapping** — 0.2536 s median margin, not established |
| **warm: w16 vs w8** | **overlapping** — not established |

So the decision rests on separated evidence, while two *fine* orderings do not. See M1 and M4 in
`.claude/rules/stage5-cache-identity.md` for how the prose is qualified accordingly.

### The two excluded slots

17 slots ran; **15 are accepted**, 3 per worker count.

| slot | standby after reset | pre-registered ceiling | outcome |
|---|---:|---|---|
| `d0_a1_w1` | 429.2 MB | ≤ 200 MB | **EXCLUDED** |
| `d0_a2_w2` | 514.8 MB | ≤ 200 MB | **EXCLUDED** |

Both purges plainly worked (99.13 % and 98.46 % of the standby list evicted); they ran while standby
held 49.1 GB and 33.5 GB, so a larger *absolute* residual re-accumulated during the 2 s settle. The
ceiling should have been relative rather than absolute — that is an acknowledged flaw in the
pre-registration, and it was **recorded rather than silently corrected**: the threshold stands as
written, the slots stay excluded, and two replacement slots (`d0_r1_w1`, `d0_r2_w2`, order fixed in
advance under `plan_amendment_1.json`) restored 3 accepted runs per setting.

**The exclusion does not flatter the retained decision.** Re-including both moves worker 1's cold
median from 99.5217 s *up* to 99.6103 s — i.e. excluding them makes the **rejected** alternative look
better, and the crossover still lands on scan 4. `verify_d0.py` asserts exactly this.

### Identity invariance — the contract that actually matters

Worker count is execution policy and must reach no cache key. Across all **35** identity
computations (17 timed slots × cold + warm control, plus one prewarm without a control) at 1, 2, 4,
8 and 16 workers:

- one distinct ordered-keys digest, `bed9efdd…`;
- all **18** ordered key dumps byte-identical, content hash `53b4c9b6…` — and identical to H2's;
- `none_count`, `exception_count`, `duplicate_position_count`, `manifest_drift_count` and
  `cache_files_present_after` are **0** in every slot;
- every slot returned 1815 keys over the one frozen manifest.

`ordered_keys_sha256` hashes **full cache paths**, so it embeds the scratch cache directory. That is
the entire reason D0's digest differs from H2's `f93300d4…` while the identities themselves are
byte-identical — compare `cache_keys.ordered.txt`, not the digest, across campaigns. The directory
needed to reproduce the digest is recorded in `measurements.json`
(`digest_reproduction.cache_dir_embedded_in_ordered_keys_digest`).

## Contents

| file | what it is |
|---|---|
| `verify_d0.py` | recomputes every number and digest here; exit 0 = all pass |
| `measurements.json` | all 17 slots: timings, counts, digests, reset telemetry, medians |
| `measurements.csv` | the same table, flat, for spreadsheet or `git diff` audit |
| `source_manifest.json` | the frozen 1815-entry manifest (path, size, mtime_ns) |
| `cache_keys.ordered.txt` | one canonical ordered key dump, 1815 rows |
| `cache_key_dump_hashes.txt` | sha256 of all 18 dumps — the invariance proof |
| `benchmark_plan.json` | the pre-registration, verbatim |
| `plan_amendment_1.json` | the replacement-slot amendment, verbatim |
| `sweep.transcript.txt`, `replacement.transcript.txt` | run transcripts, verbatim |
| `D0_RESULT.md` | the original write-up, with one corrected attribution marked inline |
| `harness/` | the exact measurement harness, including both digest formulas |
| `INVENTORY.sha256` | sha256 of every file in this directory |
| `.gitattributes` | pins these files as binary so `core.autocrlf` cannot rewrite a line ending and silently invalidate every hash above |

## What was deliberately excluded, and where the full archive is

Curated, not wholesale — the original bundle is 111 files and some of it carries no information:

| excluded | why |
|---|---|
| `rammap_io/` (34 files) | **every one is 0 bytes**; RAMMap writes no stdout/stderr. The reset evidence that matters (exit code, standby before/after, % evicted, settle) is folded into `measurements.json`. |
| 17 of the 18 key dumps (~1.4 MB) | byte-identical to the one retained; their sha256s are kept in `cache_key_dump_hashes.txt`, which is what proves invariance. |
| per-slot `*.reset.json` / `*.postrun.json` (34 files) | folded losslessly into each slot's record in `measurements.json`. |
| per-slot console logs (19 files) | redundant with the slot records and the two transcripts. |

The **immutable full archive** — all 111 original files, the pre-correction `D0_RESULT.md`, and a
per-file SHA-256 manifest — is retained outside the repository at:

```
G:\My Drive\CLAUDE HANDOFF\BeatSync-Engine\D0-COLD-IDENTITY\
  manifest.json      sha256 71c85a1671cc9ac2a7b26aaffba18c5b3995b5ede37f6e2f3394407f3e0d8ab7
  manifest.sha256    documents the scheme: <hash>  manifest.json
  evidence\          111 files, 1,774,985 bytes
```

That copy is cloud-synchronised and hash-verified against the `C:\tmp` original. **Do not delete
either the archive or the temp original as housekeeping**, and do not infer staleness from age.

## Privacy and secrets

Checked before committing, and nothing here needs redaction:

- **No credentials, tokens or private configuration.** The `backend_token` / `config_token` values in
  the slot records are BLAKE2b-derived cache-identity digests (`ai_…`, `cfg_…`), not secrets.
- **The manifest paths are mechanical.** All 1815 entries are `.mp4` files named `0.mp4`, `0_1.mp4`,
  `0_27.mp4` … across four folders under one library root. No titles, descriptions, names or dates —
  which is why the manifest is committed whole rather than sanitized: `manifest_sha256` is computed
  over the paths, so stripping them would destroy the one thing it proves.
- **Machine-specific absolute paths were reduced to what verification needs.** Each slot's
  `cache_dir`, `video_analysis_file` and `dropped_syspath_entries` are not repeated 17 times; the one
  cache directory the ordered-keys digest depends on is recorded once, and
  `video_analysis_sha256` (`c88e19d6…`) is retained as the load-bearing provenance fact.

## Reopen conditions

Revisit the accepted trade-off only on bounded evidence: real user evidence that first-touch identity
latency is materially harming the workflow; an inter-batch cache-survival measurement showing
repeated full scans are mostly cold *and* the identity cost is product-significant; a material change
to the source-library workflow; a robust storage-agnostic cold/warm signal becoming available;
SSD/NVMe measurements revealing a materially different policy opportunity; or source counts growing
until the absolute regression becomes material.

The one cheap measurement that would have to come first is `f`, the fraction of repeat scans that are
cold again — **unmeasured**, break-even ≈ 0.222 under the model in `D0_RESULT.md`.
