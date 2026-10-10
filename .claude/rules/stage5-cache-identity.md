---
paths:
  - "src/video_analysis.py"
  - "tests/test_stage5_cache_identity.py"
  - "tests/test_fingerprint.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Stage 5 analysis cache: identity and the contract generation

`input/video_analysis_cache/*.json` is the Stage-5 record store. The exact identity contract is
below; it is the authority, and there is deliberately no second summary of it anywhere.

`ANALYSIS_VERSION` keeps its own narrower job and does **not** own cache-generation semantics: **bump
it in `video_analysis.py` whenever candidate scoring, window building, or the candidate schema
changes**, otherwise stale candidates silently survive. Cache identity and the contract generation
belong to `CACHE_CONTRACT_VERSION`.

## Cache identity and the contract generation (D2)

**`CACHE_CONTRACT_VERSION = "stage5_cache_v3"` is the single constant owning cache identity *and* the
persisted contract.** It is the first component of every signature *and* the value stored as
`cache_contract` in every record, so a key and its payload can never disagree about their generation.
There is deliberately no second version constant. **Bump it whenever a change alters what a cached
result means** — the identity algorithm, the Qwen prompt, the semantic normalisation/output contract,
or a result-affecting Qwen setting not already in the signature. Do *not* hash the worker source into
the key: a progress or performance-only worker edit must not invalidate semantic cache.
`ANALYSIS_VERSION` keeps its own narrower job (candidate scoring, window building, candidate schema)
and neither D2 nor P2 touches it.

**It has been bumped once, `stage5_cache_v2` → `stage5_cache_v3`, by P2** — the media-neutral prompt
changed what a persisted semantic record *means*. See the media-neutral section of
`.claude/rules/stage5-worker.md` for why there is no migration.

**Source identity** = `CACHE_CONTRACT_VERSION | ANALYSIS_VERSION | abspath | st_size | st_mtime_ns |
bounded content fingerprint | backend token (or `no_ai`) | Qwen config token`. Pre-D2 it was
`abspath + size + int(st_mtime)`, which reproducibly gave the *same* signature to a file rewritten
inside the same second. `st_mtime_ns` fixes that truncation but **not** an exact-mtime restore — the
fingerprint is what catches that, which is why mtime_ns alone was not enough. The absolute path stays
in identity on purpose: identity is *location + content*, so a moved file re-keys, preserving the
pre-D2 contract. Content-only identity would deduplicate copies; that is a semantic change D2 does not
make.

**Bounded fingerprint**: BLAKE2b, 16-byte digest, over `str(size)` then content. Files ≤ 3 MiB are read
whole; larger files contribute exactly three non-overlapping 1 MiB windows — `[0, 1 MiB)`,
`[mid, mid + 1 MiB)` with `mid = max(1 MiB, min(size // 2 - 512 KiB, size - 2 MiB))`, and
`[size - 1 MiB, size)`. The clamp guarantees no byte is hashed twice, so bytes read = `min(size, 3 MiB)`.
Measured on the real 702-source library: **5.1 ms/source → ~3.6 s per 700, ~2.1 GB read**. BLAKE2b
rather than SHA-256 because it measured faster and this is **accidental** stale-cache prevention — no
cryptographic or adversarial claim is made.

**Backend identity** covers all four components by *absolute path* + size + `st_mtime_ns` + content:
full BLAKE2b for `llama-server.exe` and `llama-mtmd-cli.exe` (9 KB and 83 KB — exact identity for
~13 ms), the bounded fingerprint for the two GGUFs (1.83 GB and 0.82 GB). Pre-D2 the token used the
*basename*, so same-name/same-size/same-second files in different directories collided and an override
pointing at another copy did not re-key. The `llama --version` string stays as extra evidence but is no
longer load-bearing alone, so a failed version probe remains non-fatal.

**The backend token is computed once per `analyze_video_sources` invocation — on success *and* on
failure — and threaded into every source signature** (`backend_token=` / `config_token=` on
`_video_signature`/`_cache_path`; there is no `audio_profile=` there any more, see P2). It used to be
reached *from* `_video_signature`,
i.e. once per source; with content fingerprints that is 702 reads of a ~2.65 GB backend, measured at
**61.7 minutes**. Threaded it is ~9–20 ms once. It is invocation-scoped, not module-cached, so a later
call in the same process still observes a swapped model or llama build.

## Per-source identity is computed concurrently (L1B)

**Identity *semantics* are untouched by L1B.** The source signature is the same eight components, the
bounded fingerprint reads the same `min(size, 3 MiB)`, `_bounded_fingerprint`, `_video_signature`,
`_cache_path`, `_qwen_config_token` and `_qwen_backend_signature_token` have **zero** executable
changes, and `CACHE_CONTRACT_VERSION`/`ANALYSIS_VERSION` are unchanged. No cache was invalidated and
there is no migration. What changed is **orchestration**: the per-source phase is bounded-parallel.

**The shared helper is `_compute_cache_paths_parallel(sources, enable_ai, qwen_model_path, *,
backend_token, config_token)`**, used by both production consumers of the per-source identity scan —
`analyze_video_sources` and `classify_library_sources`. It calls the real `_cache_path` exactly once
per submitted position on a `ThreadPoolExecutor`, with the invocation's **already-resolved** tokens.
Identity is I/O bound (one `stat` plus at most 3 MiB read), which is why threads help at all.

**Backend and Qwen-config identity are still computed once per invocation, by the caller.** No worker
may call `_qwen_backend_signature_token` or `_qwen_config_token` for itself — per source that is the
61.7-minute shape below, re-created inside a thread pool. The two callers also keep their own
invocation state (availability, the tokens, `ai_cache_disabled`, their own reporting); only the
per-source computation is shared, deliberately **not** a new orchestration abstraction.

**Position is the authority, never the path.** Results are written into a pre-sized list by
submission index, so submitting `[A, B, A]` returns `[key A, key B, key A]`: duplicate paths stay
distinct positions, nothing is deduplicated, and completion order cannot leak into the returned
order. Everything downstream — `idx` / `video_file` / `cache_file` ownership, `cache_paths`,
`results_by_index`, `jobs`, progress numbering, `classifications`, Stage 6 — consumes those results
positionally, in original source order.

**Failure semantics are the serial ones.** An unprovable identity is still a per-source `None` and
never fails the phase; an *unexpected* exception is surfaced through `future.result()` rather than
laundered into `None`, because a weaker success state is exactly what must not be invented here. There
are no retries: a `None` is final for the run.

**The worker count is bounded hard at 16 and is execution policy only.**
`_CACHE_IDENTITY_WORKER_CAP = 16`; `_cache_identity_workers(source_count)` returns 0 for ≤ 0 sources,
1 for one source, and otherwise `min(source_count, 16)`.
`BEATSYNC_CACHE_IDENTITY_WORKERS` overrides it for benchmarking, clamped to
`1 … min(source_count, 16)`, with a malformed value falling back to the measured default. **16 is a
cap, not a tuning default: nothing above 16 workers has been measured**, so raising it is a new
measurement rather than a configuration change. The knob reaches no signature, no key, no record and
no contract constant — changing it must produce byte-identical identities, and a test asserts the
name appears in none of the identity primitives.

**Measured warm, on the real 1672-source Windows library** (`J:\New folder\Cuts`, ~4.75 GiB of
bounded fingerprint windows), against the exact production identity operation:

| workers | warm identity phase | speed-up |
|---|---|---|
| 1 | 6.545 s | — |
| 2 | 3.552 s | 1.84x |
| 4 | 1.796 s | 3.65x |
| 8 | 1.088 s | 6.02x |
| **16** | **0.746 s** | **8.77x** |

Exact identity parity over the same library: **20,064 comparisons, 0 mismatches, 0 `None` results,
0 missing results, 0 duplicate results.**

**This L1B table is historical, and D0 supersedes it for the intermediate worker counts (D0-E1).** It
was measured on a **1672**-source library; D0 re-measured warm on the later **1815**-source library
and got materially different figures in the middle of the range — most visibly **worker 8: 1.088 s /
6.02x here versus 1.6973 s / 3.90x in D0**, with worker 16 close (0.746 s / 8.77x versus 0.8070 s /
8.20x). Different library, different campaign; **neither table is a correction of the other**, and
both stand as measured. **For current worker-count comparisons quote the D0 table**, which is the one
with a matching cold column and committed raw runs. The same caveat applies to the warm figures in
`_cache_identity_workers`' docstring in `src/video_analysis.py`, which still records the 1672-source
numbers — that is upstream-adjacent source text and was deliberately **not** edited here; realigning
it is a separate proposed task, not part of this documentation change.

### Measured cold (H2): `COLD_PARALLEL_IDENTITY_REGRESSION`

```
H2_RESULT_CATEGORY          = D — COLD_PARALLEL_REGRESSION
NEW_MAINTENANCE_FINDING     = COLD_PARALLEL_IDENTITY_REGRESSION
```

On the real 1815-source Windows library under a controlled standby-list purge before every timed run,
median of 3 valid runs each:

| workers | cold identity phase | warm control | cold vs 1 worker |
|---|---|---|---|
| 1 | **98.190 s** | 6.585 s | — |
| **16** | **115.806 s** | 0.767 s | **0.8479x — a 17.9 % slow-down** |

The warm column reconfirms the 8.77x above (8.58x at 1815 sources). The cold column is the opposite
sign, and the groups do not overlap: every worker-16 cold run (115.185–116.135 s) was slower than
every worker-1 cold run (97.933–103.280 s).

**Both of these are true of the *same* current default, and must be stated together.** The 16-worker
path is a proven warm-cache optimisation, **but** H2 establishes a cold first-touch regression on this
measured mechanical-HDD workload — and production uses that one default in both states. So never
describe the 16-worker default as a cold-start or first-touch improvement, and do not read this
finding as implying the cold regression has been corrected. **It has not.** Choosing a correction
strategy is a separate, future authorization.

**Scope of the finding.** It is measured on this 1815-source mechanical-HDD library
(`J:` — `WDC WD30EFRX`), applies to controlled first-touch/cold cache identity, is **not** proven
universal across storage devices, and **does not affect identity correctness**. It is not a general
regression of the parallel implementation — warm behaviour remains strongly beneficial.

**Mechanism is interpretation, not measurement.** The result is consistent with seek/readahead
contention on a mechanical HDD: 16 concurrent readers over scattered source files can increase
physical seeking and disrupt sequential readahead, whereas the warm result is consistent with those
bounded fingerprint reads being served predominantly from the OS file cache rather than requiring the
same cold disk access, so the `stat` + BLAKE2b work parallelises cleanly. **H2 measured the timing
effect, not the storage mechanism** — it did not instrument seek counts, storage queue depth,
readahead decisions or head movement, and it did not establish that any particular warm read avoided
the disk. Accordingly the *sign* must not be assumed to carry to NVMe/SSD, where cold behaviour is
**unmeasured**; establishing it is new measurement work, not an inference from this result.

Identity parity held exactly across the whole H2 run — one ordered digest over all 1815 positions in
every worker-1 and worker-16 run, 0 `None`, 0 exceptions, 0 drift, 0 duplicate positions — which is
the contract that matters here: the worker count reaches no key. See
`.claude/rules/stage5-reporting.md` for the full boundary of what the cold figures do and do not
support.

### D0: the decision — `ACCEPTED_TRADE_OFF`, and 16 stays

```
COLD_PARALLEL_IDENTITY_REGRESSION_STATUS = ACCEPTED_TRADE_OFF
PRODUCTION_CHANGE_REQUIRED               = NO
STATIC_POLICY_CANDIDATE                  = NONE
STORAGE_DETECTION_REQUIRED               = NO
ADAPTIVE_RUNTIME_SIGNAL                  = NOT PROVEN
INTER_BATCH_CACHE_SURVIVAL               = UNMEASURED
NVME_SSD_STATUS                          = UNMEASURED
```

H2 measured cold only at 1 and 16 workers, which left open the hope of an intermediate setting with
near-worker-1 cold cost and most of the warm benefit. **D0 swept the already-supported settings and
there is no such point.** Same frozen 1815-source library and manifest, same real
`_compute_cache_paths_parallel`, same `-Et` cold method, balanced order frozen before any timing,
3 valid controlled-cold runs per setting each with its own immediate no-reset warm control:

| workers | cold median | warm median | warm speed-up | frontier |
|---|---|---|---|---|
| **1** | **99.5217 s** | 6.6150 s | 1.00x | **PARETO** (best cold) |
| 2 | 110.3139 s | 3.7322 s | 1.77x | **PARETO** |
| 4 | 114.3353 s | 2.0465 s | 3.23x | **DOMINATED by 8** |
| 8 | 114.0817 s | 1.6973 s | 3.90x | **PARETO** |
| **16** | 115.7938 s | **0.8070 s** | **8.20x** | **PARETO** (best warm) |

**Those are medians of three runs each, and the dispersion is part of the evidence (D0-E1).** The
accepted observations, and the two slots the pre-registration excluded, are:

| workers | cold runs (s) | warm runs (s) |
|---|---|---|
| 1 | 99.1686 · **99.5217** · 99.6989 | 6.4925 · **6.6150** · 11.7589 |
| 2 | 110.0748 · **110.3139** · 110.6744 | 3.7189 · **3.7322** · 6.3808 |
| 4 | 114.1619 · **114.3353** · 114.4101 | 2.0241 · **2.0465** · 3.2459 |
| 8 | 113.6586 · **114.0817** · 114.3318 | 1.1127 · **1.6973** · 1.7438 |
| 16 | 115.4680 · **115.7938** · 115.8657 | 0.7839 · **0.8070** · 1.1890 |

Cold is tight (≤1.1 s within every setting). **Warm is noisy** — worker 1 spans 5.2664 s — so
**8.20x is a median ratio, not a guaranteed production speed-up**: the same three runs admit ~5.5x to
~15.0x depending on which are paired. The median is used because it agrees closely with H2's
independent 6.585 s at worker 1; the mean (8.2888 s) is skewed by the single 11.76 s run and was
reported but not relied on. 17 slots ran and **15 were accepted**: `d0_a1_w1` (standby 429.2 MB) and
`d0_a2_w2` (514.8 MB) failed the pre-registered `standby_after ≤ 200 MB` ceiling and were replaced by
`d0_r1_w1` / `d0_r2_w2`, ordered in advance. Re-including them moves w1's cold median *up* to
99.6103 s, so the exclusion flatters the **rejected** alternative, not this decision, and the
crossover still lands on scan 4.

**The full evidence is committed**: `docs/claude/d0-cold-identity/` carries every slot, the frozen
manifest, the key dumps' hashes, the pre-registration and the harness, plus `verify_d0.py`, which
recomputes all ten medians and both digests from stdlib alone.

**H2 and D0 are two independent campaigns, and they corroborate each other (D0-E1).** Their 1-vs-16
cold figures differ slightly — H2 reported 98.190 / 115.806 s (0.8479x, 17.9 % slower), D0 reports
99.5217 / 115.7938 s (0.8595x, 16.4 % slower) — because they are separate sets of timed runs, not
because either was revised. **Neither number is corrected by the other, and both stand as measured.**
They agree where it matters: each D0 median falls inside H2's reported run range for the same setting
(H2 worker 1 spanned 97.933–103.280 s, worker 16 spanned 115.185–116.135 s), the sign and rough
magnitude of the regression reproduce, and identity parity is exact in both. When quoting a 1-vs-16
cold figure, say which campaign it came from.

**The cold regression is front-loaded, but it does not stop at the second worker.** The 1→2
transition is the largest single increase (**+10.7922 s**), and additional cold cost remains above it:
w2→w16 adds a further **+5.4799 s**, for **+16.2721 s** in total from w1 to w16. The step sizes are
`1→2 +10.7922`, `2→4 +4.0214`, `4→8 −0.2536`, `8→16 +1.7121` s — so the higher-worker measurements
w4/w8/w16 form a tighter cluster spanning **1.7121 s**, while w2 through w16 span **5.4799 s**.

So worker 1 is the only setting that avoids the regression, and it costs 8.20x warm throughput. No
static setting is universally superior across both cache states, which is why
`STATIC_POLICY_CANDIDATE = NONE` and a lower static default is not justified. **That conclusion rests
on the frontier shape and the measured cold-versus-warm trade-off, not on the cold curve being flat
above worker 2 — it is not.** Partial concurrency is the poor bargain here: w2 already gives up
+10.7922 s of the +16.2721 s cold cost while delivering only 1.77x of the available 8.20x warm
speed-up.

**Worker 4 is the one dominated point, and the strength of that claim differs per axis (D0-E1).**
On the medians w8 is better on both, so w4 is Pareto-dominated and there is no reason to choose it.
But say what each axis actually supports, because n = 3:

- **Warm: established.** w8's three runs (1.1127 / 1.6973 / 1.7438 s) lie entirely below w4's
  (2.0241 / 2.0465 / 3.2459 s) — complete separation, a ~17 % better median.
- **Cold: not established.** The margin is **0.2536 s** on ~114 s (0.22 %), and the samples
  *overlap* — w8 `113.6586 … 114.3318` against w4 `114.1619 … 114.4101`. This is the only adjacent
  cold pair in the sweep that is not cleanly separated. Do **not** state w8 is faster cold as a
  measured fact; at n = 3 the honest reading is "indistinguishable".

**The practical advice survives either way, which is why this was not worth re-measuring:** if cold
is a tie and warm is better, w8 still (weakly) dominates w4, so w4 remains the one setting with no
argument for it. What is withdrawn is the categorical "faster on both axes", not the conclusion. The
same qualification applies to **w8 vs w16 on the warm axis**, which also overlaps — see the
cumulative comparison below.

Repeating the same full scan, under the illustrative **cold-first / fully-warm-subsequent** model:

| full scans | w=1 | w=16 | advantage |
|---|---|---|---|
| 1 | 99.5217 s | 115.7938 s | **w1 by 16.2721 s** |
| 2 | 106.1367 s | 116.6008 s | w1 by 10.4641 s |
| 3 | 112.7517 s | 117.4078 s | w1 by 4.6561 s |
| 4 | 119.3667 s | 118.2148 s | **w16 by 1.1519 s** |
| 5 | 125.9817 s | 119.0218 s | w16 by 6.9599 s |

Continuous crossover **3.8017 scans**, i.e. the fourth full scan — corresponding to more than 300
outstanding sources at the default batch size of 100. **That is a conditional model statement, not an
observed production threshold.** `INTER_BATCH_CACHE_SURVIVAL = UNMEASURED`: a long Qwen batch runs
between scans and whether it evicts the bounded fingerprint windows was never measured, so the real
number of later scans behaving fully warm, partially warm or cold is unknown, and the real cumulative
crossover with it. State the identity-policy difference in **absolute seconds** — no percentage of
Stage-5 or of total preparation wall time is claimed from current data.

**Worker 8 is 16's nearest rival, and it loses under the same model (D0-E1).** It is the only setting
that is better cold than 16 *and* within one order of magnitude warm, so it is the one worth stating
explicitly rather than leaving as an unexamined `PARETO` label:

| | w8 | w16 | |
|---|---:|---:|---|
| cold median | 114.0817 s | 115.7938 s | w8 better by **1.7121 s** — samples cleanly separated |
| warm median | 1.6973 s | 0.8070 s | w16 better by **0.8903 s** (2.10x) — samples **overlap** at n = 3 |

Under the *same* cold-first / fully-warm-subsequent model, w16 overtakes w8 at a continuous
**2.9231 scans**, i.e. from the **third** full scan — one scan earlier than it overtakes worker 1.
**This is the same conditional model and carries the same caveat: it is not an observed production
threshold**, and the warm leg of the comparison is the half that n = 3 does not establish. It is
recorded because it shows the retained default is not resting on an unexamined neighbour, not as
independent proof.

**Why not storage detection.** Media type does not observe the variable that decides the trade-off:
OS file-cache state. A mechanical HDD can have a perfectly warm identity scan, so a rule like
"HDD → worker 1" would throw away the measured warm benefit on that same HDD. There is also no seam
for it — no storage-type detection, no Win32 storage IOCTL path, no `ctypes` storage code, no
PowerShell storage query and no path→volume→physical-device mapping contract exist in this
repository, and `src/**/*.py` returns zero hits for any of them. Adding one is a substantial platform
dependency that D0 does not support. **Why not an adaptive policy.** D0 measured no robust
production-safe cold/warm signal, and process-local invocation history is not OS file-cache authority
— the OS may evict pages independently.

**So the 16-worker default is retained deliberately**, as an explicit product/performance trade-off:
strongest measured warm performance, a known and documented HDD cold penalty, no universally superior
static alternative, and **no correctness consequence** — D0 reconfirmed exact identity invariance with
a single ordered digest across all 35 identity computations at 1, 2, 4, 8 and 16 workers, with
`none`/`exceptions`/`drift`/`duplicate positions` all 0 and no cache payload written.
`CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched.

**This is acceptance, not a fix.** The cold regression remains historically true and is not
`FIXED` or resolved by code. Revisit the decision only on bounded evidence: real user evidence that
first-touch identity latency is materially harming the workflow; an inter-batch cache-survival
measurement showing repeated full scans are mostly cold *and* the identity cost is
product-significant; a material change to the source-library workflow; a robust storage-agnostic
cold/warm signal becoming available; SSD/NVMe measurements revealing a materially different policy
opportunity; or source counts growing until the absolute regression becomes material.

**If the invocation-level backend identity fails, AI caching is off for that entire run.** The
orchestrator holds an explicit `ai_cache_disabled` state and then does not call `_cache_path` at all —
and, since L1B, does not start a thread pool either: it constructs the ordered `None` results
directly, so one invocation-level failure stays one failure rather than becoming 1 + N retries. The
reason is unchanged: down in `_video_signature` a `None` `backend_token` means *"not supplied, compute it now"*, so
handing the failed `None` onward made every source retry the fingerprinting (measured **1 + N** calls)
and let a transient later success re-enable caching *mid-run*. Never overload `None` as both "not
supplied" and "supplied but failed" at that boundary, and **do not** claim a per-source retry can
restore caching: it must not. Analysis, Qwen and rendering continue normally; only the cache is off.

**Qwen identity keys three things** — `_qwen_config_token()` takes no arguments — on *effective*
values mirroring the runtime's own parsing and clamps, so behaviourally identical configurations key
identically (unset == explicit default; a malformed value == the default the worker actually uses):

- `BEATSYNC_QWEN_MAX_WINDOWS` — default 120, then `max(0, …)`. D1 proved this changes how many
  candidates get semantics while being absent from identity.
- `BEATSYNC_QWEN_FRAME_WIDTH` — 512, clamp 224–768. Changes the image the VLM sees.
- `BEATSYNC_QWEN_MAX_NEW_TOKENS` — 128, clamp 32–256. Can truncate the semantic JSON.

There was a fourth, `audio_profile["smart_preset"]`, because the worker interpolated it into the
prompt. **P2 retired it**, along with `_qwen_prompt_style_hint` and the whole notion of an edit style
in persisted semantics; that is the media-neutral section of `.claude/rules/stage5-worker.md`, and
the D2 R2 tests asserting the
opposite were deliberately replaced. Runtime-only knobs stay **excluded**: slots, device, timeouts,
batching, ctx, prefetch. A `no_ai` run gets a canonical no-AI config token, so Qwen settings never
perturb a deterministic key.

**Unprovable identity means no cache, never a weak key.** If a stat or fingerprint fails —
source *or* backend — `_video_signature` and `_cache_path` return `None`: that source gets no lookup and
no write for the run, and analysis/rendering continue normally. There is no `ai_missing`-style
placeholder any more, because a *stable* token for an unprovable input is exactly what lets a stale
entry be reused. `_checkpoint_cache(None, …)` is already a no-op, which is the seam this uses.

**The contract marker is stamped where records are born**, in `_analyze_single_video`, so AI,
deterministic/`no_ai` and candidate-less records all carry it before the completion rule sees them.
`_save_cache` stays a pure transport primitive and never injects semantic truth.

**D2 costs exactly one cold rebuild, by design** (~0.86–1.27 h for the current 702-source library) and
there is **no migration and no cleanup**: new inputs produce new filenames, so pre-D2 records are simply
never looked up. There is deliberately no D1→D2 compatibility loader. Old and new generations coexist at
roughly 55 MB each (~110 MB peak); `input/video_analysis_cache/` is preserved by policy, so do not add
automatic deletion. Warm runs thereafter pay only the ~3.6 s identity scan plus ~20 ms of backend work.
