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
0 missing results, 0 duplicate results.** A **controlled cold parallel speed-up is NOT MEASURED** —
do not claim one. See `.claude/rules/stage5-reporting.md` for what the cold figures do and do not
support.

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
