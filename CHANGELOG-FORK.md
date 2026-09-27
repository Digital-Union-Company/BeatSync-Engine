# Fork modification log

This file records the modifications this fork makes to the upstream project, as required by
**AGPL-3.0 §5(a)** ("cause the modified work to carry prominent notices stating that you modified it,
and giving a relevant date").

| | |
|---|---|
| **Fork** | [Digital-Union-Company/BeatSync-Engine](https://github.com/Digital-Union-Company/BeatSync-Engine) |
| **Upstream** | [Merserk/BeatSync-Engine](https://github.com/Merserk/BeatSync-Engine) |
| **Upstream baseline** | `06679c1f28c6d0b56c901495e52497cccebc744a` |
| **Fork modifications began** | 2026-09-26 |
| **Licence** | GNU Affero General Public License v3.0 (unchanged from upstream) |

Fork-specific code lives in `src/beatsync_fork/`. Upstream modules are touched only at small call sites
marked `# [FORK]`, so upstream synchronisation stays straightforward. Upstream Auto Mode creative
behaviour is preserved as the default.

---

## Unreleased

### Fixed — 2026-09-27 (Qwen targeted semantic recovery: one persistent rejection no longer retires a source)

**The defect.** A candidate whose semantics `_normalize_semantic` rejected made its *entire source*
permanently uncacheable. `_qwen_job_completed` requires `returned_ids == requested_ids`, so one missing
tag means the job is incomplete, nothing checkpoints, and the source is re-analysed on every run
forever. Two sources in the real 845-file production library were in exactly that state — measured 10
requested, 10 decoded, **9** tagged — costing a ~51 s Stage 5 tax on every warm run.

**Measured root cause: truncation, not a field-level rejection.** The primary request budgets
`_max_new_tokens()` (default 128) and leaves `description` an unbounded schema string. llama-server
returns `finish_reason="length"` with `completion_tokens` exactly at the budget and non-empty but
truncated text; `_parse_json_object`'s `\{.*\}` finds no closing brace, `json.loads` fails, and
`_normalize_semantic` rejects at its not-a-dict guard. The raw text already contained all 8 numeric
keys, a valid `emotion` and `recommended_use`, and substantial description content; what was missing
was **JSON termination** — generation stopped mid-description, leaving both the description string's
closing quote and the object's closing brace unemitted (the captured output has an odd quote count).
The description value is therefore not syntactically complete, so this is not a case lenient
brace-matching could have rescued. Greedy decoding (`temperature 0`, `top_k 1`) makes it byte-for-byte
reproducible, so each retry re-issues the identical request: the candidate can never resolve. Verified
by reproducing the request out of process and capturing the raw output that production discards.

**A bigger token budget alone is not the fix — measured, not assumed.** One of the two cases is a
degenerate repetition loop (`lips moving, lips open, lips closed, …`) that consumes whatever budget it
is given: still truncated at 160, 192 **and 256** tokens, the description growing 350 → 470 → 614 → 880
characters and never closing. The grammar bound is the half that terminates the loop; the extra budget
is only needed so the bounded JSON can close (131 and 129 tokens observed). The bound alone also fails,
leaving the other case one token short of closing. Full matrix, 3/3 repetitions each:

| variant | case A (long description) | case B (repetition loop) |
|---|---|---|
| 128, current schema | invalid | invalid |
| 160 / 192 / 256, current schema | valid | **invalid at every budget** |
| 128 + `maxLength 96` | invalid | valid |
| **160 + `maxLength 96`** | **valid** | **valid** |
| 192 + `maxLength 96` | valid | valid |

Smallest variant recovering both: **160 tokens + `description.maxLength = 96`**. Bounds of 112 and 128
also pass but retain more of the repetition, which is why 96 was chosen.

**The change.** Recovery is last in the control flow, with every *applicable* pre-existing primary
retry/fallback path ahead of it. Those tiers are conditional rather than a fixed sequence every
candidate walks: the initial attempt always runs, the server retry applies while a server is still
available, the reduced-slot restart fires only on its existing `valid_ratio < 0.70` condition (and
returns recursively, so only the innermost wave reaches recovery), and the serial/CLI fallback applies
per the existing backend state. Recovery forces none of them to run. A candidate still unresolved after
whichever tiers applied gets **exactly one** targeted recovery generation with those two measured
parameters. Same image, same prompt, same model, same greedy sampling. If it succeeds the semantic is
added normally; if it fails, behaviour is unchanged: candidate absent, job incomplete, no checkpoint.

**Deliberately narrow:**

- **The primary path is unchanged.** `_max_new_tokens()` still governs the ordinary request and remains
  D2-keyed via `BEATSYNC_QWEN_MAX_NEW_TOKENS`; `SEMANTIC_SCHEMA` keeps `description = {"type":
  "string"}` with no bound; prompt and sampling untouched; `_recovery_semantic_schema()` deep-copies
  instead of mutating the global. Proven cross-branch against one shared llama-server instance: primary
  output is **byte-identical** to merged main on all four measured candidates (342 / 347 / 350 / 306
  chars), including both previously-successful controls.
- **Semantic rejection only.** `_is_semantic_rejection()` requires a falsy semantic *and* non-empty
  text. HTTP errors, a dead server, CLI timeouts, non-zero exits and empty generations are excluded —
  there is no truncated output to rescue, and re-asking would paper over a broken backend. Existing
  transport retry/fallback behaviour is preserved. The classification stays internal to the inference
  wave and never reaches a cached payload.
- **Recovery eligibility is recomputed from the collected semantics**, not from the stale `failed` list,
  because the serial fallback tier resolves candidates without rewriting it.
- **Hard-coded constants, not environment variables.** A tunable recovery knob would be result-affecting
  Qwen configuration absent from `_qwen_config_token()` — the exact defect D2 fixed for `MAX_WINDOWS`.
- **The override arguments default to `None`** on all three `generate()` primitives, so every existing
  caller behaves identically. The CLI client's ctx-fallback self-retry forwards them as well; without
  that, a recovery hitting a context error would silently retry as an ordinary 128-token unbounded
  request and truncate again while appearing to have run.
- **The completion contract is frozen.** `_qwen_job_completed`, `_stored_ai_cache_is_consistent`,
  `ai_enabled`/`ai_deferred` semantics and checkpoint eligibility are untouched. `src/video_analysis.py`
  is not modified at all.

**No cache re-key, no contract bump** for this first introduction, and the argument is structural rather
than empirical: a source current main caches had every requested candidate tagged on the primary path,
so recovery never runs and the persisted semantics are identical; a source that missed a candidate
failed `_qwen_job_completed` and therefore has **no complete record on disk at all** — recovery can only
turn an absence into a record, never contradict a stored one. `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v2`, `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`, and the 843
existing D2 production records remain reusable. A *future* change to the recovery constants does **not**
inherit this argument, because fallback-generated records will exist by then; default policy for such a
change is to bump the cache contract unless persisted-output compatibility is explicitly proven.

**Honest limit.** Recovery does not cure repetition. The recovered degenerate description is still
partially repetitive — it is merely valid JSON, schema-valid, normalization-valid and bounded to ≤ 96
characters. The point is that one runaway description no longer makes an entire source permanently
uncacheable; model prose quality is out of scope.

Measured recovery cost ~0.61–0.72 s per call (mean ~0.65 s), so ~1.3 s for the two known sources.
Prediction only, pending a production run: once both recover and checkpoint, a subsequent identical warm
run should show 845/845 cache hits and launch no Qwen worker.

Changed: `src/auto_mode/stage5_qwen_scene_worker.py`, `tests/test_qwen_semantic_recovery.py` (21 tests),
`CLAUDE.md`, `CHANGELOG-FORK.md`. Suite 614 passed / 2 skipped (593 + 21 new; same two pre-existing
`WinError 1314` symlink skips).

### Changed — 2026-09-27 (Stage 5 cache identity D2: one deliberate generation transition)

D1 made the cache *durable* and its completion state *honest*. What it deliberately left alone was
whether a cache key identifies the right inputs at all. Measured on current main before changing
anything:

- **Source identity collided.** `abspath + size + int(st_mtime)` gave the *same* signature to a file
  whose content was replaced inside the same second, and to one whose exact original mtime was
  restored — so a stale analysis was reused for different media, the worst error class in this cache.
- **Backend identity collided** for all four components. The token used the file *basename* plus size
  plus `int(mtime)`, so a same-name/same-size/same-second swap was invisible and an override pointing
  at another copy of a model did not re-key.
- **`BEATSYNC_QWEN_MAX_WINDOWS` was absent from identity.** Runs with 120, 60, 0 and unset all shared
  one cache file, so a 60-window result was silently reused by a run asking for 120. The same applied
  to `BEATSYNC_QWEN_FRAME_WIDTH` and `BEATSYNC_QWEN_MAX_NEW_TOKENS`, both of which change the persisted
  semantics.
- **There was no persisted completion contract**, so nothing distinguished a record written under one
  set of rules from another.

D2 fixes all of it in **one** generation transition rather than several.

`CACHE_CONTRACT_VERSION = "stage5_cache_v2"` is a single constant used both as the first signature
component and as the stored `cache_contract` field, so a key and its payload cannot disagree about
their generation. `ANALYSIS_VERSION` is untouched — its job is candidate scoring, window building and
the candidate schema, none of which changed.

Source identity is now `CACHE_CONTRACT_VERSION | ANALYSIS_VERSION | abspath | st_size | st_mtime_ns |
bounded content fingerprint | backend token | Qwen config token`. `st_mtime_ns` alone would not have
been enough: it closes the integer-second truncation but an exact-mtime restore still collides, so the
fingerprint is the part that actually catches it. The fingerprint is BLAKE2b/16 over the size plus
either the whole file (≤ 3 MiB) or three non-overlapping 1 MiB windows (head, a clamped middle, tail),
measured at **5.1 ms/source → ~3.6 s and ~2.1 GB for 700 sources**. No cryptographic claim is made —
bounded fingerprints are for practical accidental stale-cache prevention.

Backend identity now covers each component by absolute path, size, `st_mtime_ns` and content: a full
hash for the two tiny executables (9 KB, 83 KB) and the bounded fingerprint for the two GGUFs
(1.83 GB, 0.82 GB). The `llama --version` string remains as extra evidence rather than the only strong
signal, so a failed probe stays non-fatal.

**One measurement shaped the whole design.** `_qwen_backend_signature_token` was reached from
`_video_signature`, i.e. **once per source — 702 times**, and was not memoised. Adding content
fingerprints there without changing that would have cost **61.7 minutes** per run. The orchestrator now
computes the backend and config tokens once per `analyze_video_sources` invocation and threads them into
every signature: ~9–20 ms total. It is invocation-scoped rather than module-cached, so a second call in
the same process still sees a swapped model, and a test asserts exactly four component fingerprints for
N sources.

Qwen identity uses *effective* values mirroring the runtime's own clamps, so an unset variable and its
explicit default agree, and a malformed value agrees with the default the worker really falls back to.
It keys `MAX_WINDOWS`, `FRAME_WIDTH`, `MAX_NEW_TOKENS` and — added in R2 below —
`audio_profile["smart_preset"]`. Runtime-only knobs (slots, device, timeouts, batching) are deliberately
excluded, and the whole `audio_profile` is deliberately not hashed.

**R2 — two gaps the first pass left.** Both were reproduced through the real `analyze_video_sources`
before being fixed:

- **Prompt context was missing from identity.** The worker's `_build_prompt` reads
  `audio_profile.get("smart_preset", "rhythmic_gmv_amv")` and interpolates it straight into the Qwen
  prompt, and `analyze_video_sources` forwards the audio profile into the worker request — so two runs
  differing only in preset produce different semantics. Measured on the first D2 commit: presets
  `rhythmic_hype_gmv_amv` and `cinematic_soft_amv` produced the *same* cache file. The effective style
  hint is now part of the Qwen identity token (missing key still equals the explicit default, and a
  `no_ai` key is unaffected), with a seam test that reads the worker's own call so a worker-side change
  to the key or default fails the suite. The worker is untouched.
- **A failed backend identity recomputed per source, and could recover mid-run.** The orchestrator
  passed the failed `None` token onward, but `_video_signature` reads `None` as "not supplied, compute
  it now" — so the same value meant two different things at that boundary. Measured: 6 sources produced
  **7** backend-token calls (1 + N), and a token that failed once then succeeded re-enabled caching
  inside that run, writing 6 records contrary to the documented run-level fail-closed contract. An
  explicit `ai_cache_disabled` state now short-circuits before `_cache_path` is called at all: 1 call,
  0 writes, no mid-run recovery, and analysis still returns all 6 sources. The success path already
  behaved correctly and still does — 1 call per invocation, 2 across two invocations.

`CACHE_CONTRACT_VERSION` is deliberately **not** bumped for R2: the first D2 commit is unshipped and no
D2 cache generation exists yet, so this is remediation inside the same unshipped generation rather than
a new one.

**Unprovable identity now means no cache rather than a weak key.** A stat or fingerprint failure, source
or backend, makes the signature and cache path `None`: no lookup, no write, and the render continues.
The old `ai_missing` placeholder is gone, because a *stable* token for an unprovable input is precisely
what allows a stale entry to be reused.

The contract marker is stamped in `_analyze_single_video`, where records are built, so AI,
deterministic/`no_ai` and candidate-less records all carry it; `_save_cache` remains a pure transport
primitive.

**Cost and legacy.** Exactly one cold rebuild is accepted by design — ~0.86–1.27 h for the current
702-source library — after which warm runs pay only the ~3.6 s identity scan plus ~20 ms of backend
work. There is **no migration, no cleanup and no D1→D2 compatibility loader**: new inputs mean new
filenames, so the 2196 pre-D2 records are simply never looked up. They remain on disk as harmless
historical artifacts (~55 MB, alongside a similar new generation), and `input/video_analysis_cache/` is
preserved by policy. The two D1 records whose completeness could never be proven become unreachable and
rebuild naturally, so that open question closes without a judgement call. A cold D2 run was verified to
return a payload and stored records identical to base main, cache-only metadata aside.

### Fixed — 2026-09-27 (Stage 5 cache durability D1: checkpointing + AI completion truth)

**The measured reason.** Until now `analyze_video_sources` wrote the cache in a single terminal loop
at the very end of Stage 5, so an interruption before that loop discarded *everything* new. Measured
against the pre-D1 code with three uncached sources: interrupting during the deterministic pass left
**2 completed analyses and 0 durable cache entries**; interrupting after every deterministic analysis
*and* every Qwen tag had finished still left **3 completed sources and 0 durable entries**. A hard
`os._exit` mid-run left 0. On the real 702-source library (median 4.4 s, mean 6.5 s per source) a full
cold rebuild is 0.85–1.26 hours, all of which a single Ctrl-C could previously throw away.

- **Per-source checkpointing.** Every point at which a source could have become complete now *checks
  checkpoint eligibility* through the one completion rule, and writes only if the rule accepts:
  after each serial `_analyze_single_video`, after each parallel deterministic result, after
  `_complete_deferred_qwen`, and after **each per-job merge** in `_complete_deferred_qwen_batch`. The
  terminal loop remains only as a backstop and goes through the same guard, so it can no longer
  promote an incomplete record into an accepted cache entry.

  What that actually persists, per shape:

  - a **non-AI** parallel or serial result is complete on arrival and is written immediately;
  - an **AI-deferred** parallel result carries `ai_deferred=True`, so the rule *refuses* it at that
    point — it becomes durable only once its Qwen result genuinely completes;
  - a **candidate-less** result that finished its deterministic scoring pass is the explicit
    no-Qwen-work exception and is written (see the R2 note below for why the scoring evidence is
    required);
  - in the **shared Qwen batch**, each per-job merge is checkpointed independently *once the worker's
    final response has returned*, so a failing or missing sibling — and a parent interruption during
    the post-response merge loop — no longer discards jobs already written.

  The boundary this does **not** cross: while the shared worker is still in flight, its per-job
  results exist only inside that process and nothing about them is durable yet. Streamed worker
  progress is presentation only and carries no semantic result authority. See *Honest limits* below.
- **One completion rule.** New `_cache_entry_is_complete()` answers "is this payload reusable for this
  request?" in one place. It rejects non-dict payloads, wrong `analysis_version`, missing/non-string
  `video_file`, non-list `candidates`, and anything with `ai_deferred` truthy; under `require_ai` it
  also demands genuine AI completion. `_checkpoint_cache()` consults it before any write, which is
  what makes early saving safe.
- **`ai_enabled` stopped lying.** Previously `_complete_deferred_qwen` swallowed a Qwen exception and
  then set `ai_enabled=True` unconditionally, so a failed run was cached as AI-complete and Qwen
  never retried for that source. Completion now comes from an explicit signal the Qwen facade
  reports on every return path — *not* from "no exception was raised". (What that signal is allowed to
  mean was itself tightened twice afterwards; see R4 and R5 below.) Deterministic candidates and
  visual tags are untouched on failure.
- **Batch failure is judged per job.** A globally non-empty `semantics_by_job` was being treated as
  proof that every requested job completed; a job simply absent from the response became
  `ai_enabled=True` with 0 tags and was reused as AI-complete forever. Completion is now a per-job
  membership test, one missing job no longer fails its siblings, and the existing total-batch-failure
  behaviour is preserved.
- **Candidate-less sources are no longer re-analysed forever** — but only when the deterministic
  scoring pass genuinely completed, proven by the existing scoring evidence in `timings`. There is
  then no Qwen work to do, so the source is reusable, expressed through the completion rule with
  `ai_enabled` left honestly `False` rather than faked to satisfy the loader. An empty candidate list
  on its own is **not** proof of success: an OpenCV-open failure produces the same shape, and R2
  below records that the first pass wrongly accepted it.
- **Hardened writer.** `_save_cache` now publishes through a unique same-directory temp file
  (`tempfile.mkstemp`), `flush` + `os.fsync`, then `os.replace`, with best-effort cleanup in
  `finally`. The old shared `path + ".tmp"` was demonstrably unsafe across processes: under
  deterministic barriers one writer's `os.replace` published the *other* writer's payload while
  reporting success, and the loser failed with `FileNotFoundError` into a warning the GUI discards.
  After the fix, 160 concurrent writes from two processes produced 0 exceptions, no shared temp
  names, no mixed payloads and no orphan temps. `PROCESS_CRASH_ATOMICITY` is preserved (verified at
  all four boundaries); **power-loss durability is still not guaranteed** — the temp is fsynced, the
  containing directory is not.
- **Minimal load validation.** The loader now refuses version-correct but malformed or *foreign*
  payloads, and can be told which source it expected. Unexpected extra fields remain allowed for
  forward compatibility.

**R2 — three completion gaps the first pass left open.** Each was reproduced against the first D1
commit before being fixed:

- **The serial inline path ignored the completion signal entirely.** `_analyze_single_video` still
  returned `ai_enabled = enable_ai and not defer_ai`, so a `workers == 1` run reported AI-complete
  whatever Qwen did. Measured on the pre-R2 code: worker returning `{}` → `ai_enabled=True`; worker
  raising → `ai_enabled=True`; `BEATSYNC_QWEN_MAX_WINDOWS=0` → `ai_enabled=True`. It also let the
  private `_qwen_completed` flag reach `timings` — and therefore the cached payload — through
  `timings.update(qwen_info)`. The inline path now pops the flag before the update and derives
  `ai_enabled` from it, exactly like the deferred paths.
- **Zero semantic tags were conflated with a worker-process failure.** The first pass keyed on
  emptiness of `semantics`, so a worker that finished and a worker that never produced a response
  both reported not-completed. R2 replaced that with membership of the job's entry in the response
  envelope — which R4 below shows was still wrong, in the opposite direction.
- **An OpenCV-open failure was accepted as a candidate-less success.** `"Warning: OpenCV could not
  open …; candidate analysis skipped."` returns `candidates == []`, which the D1 completion rule
  treated as "nothing for Qwen to do, therefore complete" — so one transient decode failure would
  have cached an empty result and retired a readable source permanently. Measured on the pre-R2
  code: the open-failure record was accepted under both `require_ai` modes **and** checkpointed. The
  rule now requires `timings["candidate_scoring_seconds"]`, which is written only inside the
  `cap.isOpened()` branch; a genuine no-usable-moments result keeps it and stays reusable, the
  failure does not and is retried.

**R4 — per-job Qwen completion required actual per-frame results.** R2/R3 defined completion as
membership of the job's entry in the worker response (`timings_by_job["single"]`, or `job_id` in
`semantics_by_job`) and documented "valid completed response + zero tags = SUCCESS". Read against the
worker, that is too weak: `_run_semantics_for_video` always returns a timings dict and `main` always
records it under the job id, so the envelope only proves **the job loop returned**. Inside the loop,
`_normalize_semantic` yields `{}` for any candidate whose semantic content is invalid,
`_run_inference_wave` classifies `{}` as failed and retries it (server retry → reduced-slot restart →
serial fallback), and a candidate still failing is simply **absent** from the returned semantics.

Measured against the R3 commit, all of these were wrongly reported as complete and cached as
AI-complete: `frame_count=3 tag_count=0 semantics={}`; `frame_count=0 tag_count=0`;
`frame_count=4 tag_count=3` (partial); and the batch equivalents, which were also checkpointed.

New shared rule `_qwen_job_completed()` — used by **both** the single and batch paths so the
arithmetic exists once — requires the expected per-job envelope, a dict `timing`, `frame_count > 0`,
and `tag_count == frame_count`, plus basic count coherence (`tag_count` may not exceed the semantic
records actually returned). Tags that did arrive are still merged; only the verdict changes, so an
incomplete job is retried next run instead of inheriting a silent gap. A successful sibling in a
partially-failed batch remains independently complete and checkpointed, and total-batch-failure
behaviour is unchanged.

Two accompanying corrections: **tag count is not forbidden from completion truth** — it is meaningful
only together with the envelope and the real `frame_count`, and the earlier blanket claim to the
contrary is removed. And the claim that *every* `_run_qwen_worker` failure returns `{}` is narrowed:
that holds for process-level failures (launch error, non-zero exit, timeout, unreadable response),
but candidate-level inference failures are swallowed and retried **inside** the worker, so a
successful worker process can still return an incomplete per-job semantic result. Worker counts are
now read through `_coerce_count`, closing a latent `ValueError` crash on a malformed payload (present
in the batch path before R4). The candidate-less no-Qwen-work case is untouched and remains separate:
no job is submitted, so the per-frame rule does not apply to it.

**R5 — completion must cover the *requested* candidate set, not just the decoded one.** R4 required
`frame_count > 0` and `tag_count == frame_count`, which proves every *decoded* frame was tagged. But
`_prefetch_candidate_frames` returns only the frames it could actually read
(`ready = [p for p in plans if p["image"] is not None]`), so `frame_count` can be smaller than the
candidate set the parent submitted — the worker even logs `{len(ready)}/{len(candidates)}`. Measured
against the R4 commit: a job requesting 3 candidates that decoded and tagged only 2 was reported
complete and cached as AI-complete, leaving a requested candidate with no semantics at all.

Completion now requires the requested set to be covered end to end — `frame_count` must equal the
number of candidates `_select_ai_candidates` submitted, `tag_count` must equal `frame_count`, and the
**returned semantic ids must equal the requested ids exactly**. R4 passed only
`len(semantic_by_id)`, so a response whose counts looked perfect but whose ids were foreign counted as
completion; measured on R4, three foreign ids satisfied it. An extra id now also fails, because the
worker keys semantics by our own candidate ids and anything else means the response does not match the
request.

Two robustness corrections alongside it. A worker-reported `frame_count = 0` was being rewritten into
the requested candidate count in the stored timings by `timing.get(key) or default`; `_reported_count`
now decides on **presence**, so a genuine zero survives and the fallback applies only when the field is
absent. And `bool` subclasses `int`, so R4's `isinstance(..., int)` check accepted
`frame_count: true` and then compared it equal to 1; `_is_count` rejects bools.

The candidate-less no-Qwen-work case is untouched and deliberately does **not** go through these
requested-count rules: no job is submitted, so there is nothing to cover.

**R6 — legacy AI records must be self-consistent too.** R5 hardened how *new* records are created, but
`require_ai` reuse of an *existing* candidateful record still ultimately trusted
`bool(data["ai_enabled"])` — a flag written by the very code this branch has repeatedly proven could
set it wrongly. A read-only audit of the real 2196-entry runtime cache measured the consequence:

| | |
|---|---|
| source-cache entries | 2196 (plus 28 qwen debug/repro artifacts, excluded) |
| provably R5-complete | 2190 (`candidate_count == frame_count == tag_count == ai_analyzed`) |
| **definitely inconsistent** | **4** — `ai_enabled=True` with `qwen_frame_count=10`, `qwen_tag_count=9`, 9 `ai_analyzed` |
| unverifiable but internally coherent | 2 (398 candidates/114 tagged, 525/119) |

The 4 are exactly the false-complete shape D1 exists to prevent, and the pre-R6 loader accepted all
2196. New `_stored_ai_cache_is_consistent()` now runs whenever `require_ai` reuse depends on
`ai_enabled`, rejecting only contradictions provable from **already-persisted** fields: real integer
counts (not bools), `frame_count > 0`, `tag_count == frame_count`,
`frame_count <= len(candidates)`, usable candidate ids, and `ai_analyzed` ids that are unique, a subset
of the candidate ids, and number exactly `tag_count`.

It deliberately does **not** call `_qwen_job_completed`: that needs `requested_ids`, which legacy
payloads never stored (nor the `BEATSYNC_QWEN_MAX_WINDOWS` value in force), so replaying the live rule
against an old record would mean inventing evidence. For the same reason it does not require
`frame_count == len(candidates)` — a smaller value is the normal result of `_select_ai_candidates`
limiting the submitted set, so the two unverifiable records remained reusable under the D1 loader and
were left to the D2 completion-contract decision. *The D2 transition above has since settled that by
orphaning them.*

Measured against the real cache at the D1 merge boundary, read-only: **2192 accepted, 4 rejected**, the
rejected set exactly the four audited files. **No runtime cache file was created, edited, renamed or deleted** — a rejection is
an ordinary cache miss, and the source is recomputed and republished through the already-hardened
writer. No migration command is provided and none is needed. `require_ai=False` deterministic reuse is
unaffected (those candidates are real work), and the candidate-less path still answers to
`_deterministic_analysis_completed`.

`BEATSYNC_QWEN_MAX_WINDOWS=0` now reports not-completed rather than AI-complete, deliberately:
`QWEN_MAX_WINDOWS` is not part of cache identity, so a knowingly Qwen-less record must not be stored
under the AI model key. `BEATSYNC_DISABLE_QWEN=1` remains the supported deterministic-only path — it
is turned into `enable_ai=False` in `auto_mode/__init__.py` (verified), producing the separate
`no_ai` cache identity.

R2 changed no cache key, signature or `ANALYSIS_VERSION`, and preserved every first-pass fix
(per-source checkpointing, unique fsynced temps, atomic replace, batch per-job completion, loader
validation, terminal backstop). A successful uninterrupted run is payload-equivalent in **both**
execution shapes — parallel/deferred and serial/inline — with the only difference being the removal
of the leaked `_qwen_completed` key from the serial shape's stored timings.

**Cache compatibility — at the D1 merge boundary.** There was no cache-key, signature or
`ANALYSIS_VERSION` change: `_video_signature` still used `int(stat.st_mtime)`, so existing entries
remained addressable. Verified read-only against the real cache at that point: all entries addressable,
and 2192 of 2196 reusable (see R6 — 4 intentionally rejected as self-contradictory). A successful
uninterrupted run returned a payload identical to the pre-D1 result (candidates, per-video records and
summary compared field by field). *The later D2 generation transition documented above supersedes this
contract and naturally orphans every pre-D2 entry, so these figures describe the D1-era cache, not
current lookup behaviour.*

**Honest limits — as of D1.** D1 introduced no two-phase deterministic-partial cache contract, so if
the process died while a shared Qwen worker was still running *before its response returned*, that
batch's deterministic work had to be recomputed — still true today, since D2 changed identity, not that
contract. **D1 did not fix source-identity collisions**: `int(st_mtime)` gave the same signature to a
file rewritten in place with the same size inside the same second, and an exact-mtime restore collided
regardless of precision. Source and backend identity hardening was deferred to D2, because moving to
`st_mtime_ns` re-keys the entire cache and forces a cold rebuild; **the D2 section above now closes that
work** and accepts exactly one such rebuild.

### Changed — 2026-09-27 (Phase 3C: NVENC clips decode in software)

The NVENC extraction path no longer asks FFmpeg for CUDA input decoding. **`h264_nvenc` still does the
encoding** — only the input-side `-hwaccel cuda` request is gone, so the architecture is now software
decode → the existing CPU filter chain → NVENC encode. The CPU-encode branch keeps its `-hwaccel auto`
and is untouched.

A read-only decode-path study measured three real paths on RTX 3080 / driver 617.14 over 5 real sources
× 4 deterministic windows (2.0 s, 1280×720, 30 fps, 60 frames per clip), in balanced interleaved order:

- **`-hwaccel cuda` was not actually decoding on hardware for most sources.** It failed to initialise on
  48 of 60 clips (33 requested nvdec decode surfaces against a limit of 32) and FFmpeg fell back to
  software decode anyway — the warning was the only thing it reliably produced.
- **Where real NVDEC *did* engage, it was slower.** On the one sampled source whose surface count fits,
  hardware decode was positively confirmed from FFmpeg's own verbose decoder log and ran ~20 % slower
  than software decode (0.530 s vs 0.436 s per clip), because this CPU filter chain
  (`trim,setpts,scale,fps`) has to pull the frames back to system memory.
- **Genuine CUDA decode is achievable but still not worth it.** `-hwaccel cuda -threads 8` produced
  confirmed hardware decode on all 5 sources with zero failures, exact frame counts and byte-identical
  output — and was slower than plain software decode on 20 of 20 clips. (`-threads 16` and
  `-extra_hw_frames 0` did not resolve the surface count at all.) It is not adopted.
- **Output is unchanged, byte for byte.** All 20 A/B pairs produced identical output *files* — not
  merely equal frame counts or equivalent content — and each variant was deterministic across
  repetitions. The post-change smoke reproduces the study's Variant-B files exactly: 5/5 SHA-256 match.
- **Modest, consistent speedup.** Sequential median 0.434 s vs 0.449 s (faster on 18/20 clips, and in
  every balanced repetition); at the real 4-worker NVENC cap, 12.17 s vs 13.46 s wall for 40 clips
  (3.286 vs 2.972 clips/s), with 0 decode-init failures instead of 32 and ~500 MiB lower peak VRAM.

The FFmpeg argv delta is exactly the removal of the two tokens `-hwaccel cuda`, proven by diffing the
argv the production function really constructs, before and after, with every other token in the same
order. No filter graph, encoder, quality-argument, frame-lock, worker-policy, resolution or FPS change;
`get_nvenc_quality_args`, `get_cpu_h264_quality_args`, `seconds_to_frame_count`,
`frame_count_to_seconds`, `build_frame_aligned_cut_timeline` and `_effective_clip_workers` are all
AST-identical to the base commit, and `video_processor.py`, the Phase 3B diagnostic module,
`ANALYSIS_VERSION` and the analysis-cache identity are unmodified.

The Phase 3B regression fixture that carries the old `cuvidCreateDecoder` / decode-surface warning text
is **deliberately retained**. Phase 3C removes the production trigger, not the requirement that the
diagnostic selector keep telling a recovered warning apart from the fatal encoder cause.

### Fixed — 2026-09-27 (Phase 3B: Stage 6 FFmpeg failure diagnostics)

A Stage 6 render of 701 sources lost every clip and reported `283 clip(s) failed; refusing to
concatenate an incomplete timeline`. FFmpeg had already said exactly why —
`Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0` — but
`extract_clip_segment_ffmpeg()` printed that to stdout and returned a bare `False`, `create_clip_parallel`
turned the `False` into the fixed string `"FFmpeg extraction failed"`, and the GUI redirects stdout into
`QuietConsole`. Identifying the cause took a dedicated forensic phase. This change makes the reason
travel with the failure. Diagnostics only: no encoder, command, timing or render behaviour changed.

- **New fork module `src/beatsync_fork/ffmpeg_diagnostics.py`** (stdlib-only). `summarize_ffmpeg_failure`
  ranks stderr lines and returns one bounded line; `describe_output_problem` and `describe_exception`
  cover the cases where there is no stderr worth quoting. Vendor-neutral by construction: the marker
  tuples contain no NVIDIA/NVENC/CUDA-specific tokens and no incident-specific values such as `13.1` or
  `610.00`, and a test asserts exactly that token set. Generic diagnostic terms stay intentionally
  allowed — `_SPECIFIC_MARKERS` does include `"driver"`, which matches any vendor — so the next failure
  family benefits too.
- **`extract_clip_segment_ffmpeg()` keeps its `-> bool` signature** and becomes a thin delegate to the
  new `extract_clip_segment_ffmpeg_detailed() -> Tuple[bool, str]`. The call graph shows exactly one
  in-repo caller, but the boolean function is a module-level API in an upstream file, so it was left
  compatible rather than converted. The FFmpeg command construction is **byte-identical** to the base
  commit (all 17 `cmd`/`filters`/frame-arithmetic statements compare equal by AST).
- **`create_clip_parallel` reports `f"FFmpeg extraction failed: {reason}"`**, keeping the historical
  wording as a prefix so existing expectations still match. Everything downstream already worked:
  `clip_failures` → Stage 6 warning → `first_failures[:3]` on the refusal → `ProgressView`. No GUI, no
  `progress.py` and no `ProgressView` change was required; `gui.py` is untouched.
- **A successful clip stays successful.** The summariser is only reached inside the
  `returncode != 0` branch, asserted by a seam test. This mattered concretely *at the time of Phase 3B*,
  when the NVENC path still requested `-hwaccel cuda`: successful NVENC clips on driver 617.14 were
  emitting `cuvidCreateDecoder … CUDA_ERROR_INVALID_VALUE` / `more than 32 (33) decode surfaces` while
  FFmpeg fell back to software decode, so the validated 150-clip render would otherwise have acquired a
  large crop of spurious "reasons". Phase 3B observed the behaviour without quantifying it; Phase 3C
  later measured it at **48 of 60** old-path clips failing CUDA decode initialisation, with one sampled
  source family engaging real NVDEC and emitting no such warning — so it was most successful clips, not
  all of them. (Phase 3C also removed the request, so current renders no longer emit it — the rule
  itself is unchanged and still load-bearing.)
- **Bounded**: 240 characters, one line, control characters stripped, heap addresses collapsed so the
  same failure yields a reproducible string. A 1 MB stderr produced a 61-character reason in test. The
  full text still reaches the console through the pre-existing print.
- **The complete-timeline refusal is untouched.** Measured against real `create_music_video` with two
  clips forced to fail out of nine: `RuntimeError: 2 clip(s) failed; refusing to concatenate an
  incomplete timeline.`, no output file written, `failed_clips=2 total_clips=9`, two warning events and
  two `first_failures` entries each carrying the decisive driver/API text within 215 characters.

One defect the fixture caught before commit: the first selector took the *earliest* diagnostic line,
which in the real capture is the recovered nvdec warning four lines above the fatal encoder error. The
selector now anchors on FFmpeg's consequence lines and picks the specific lines nearest that boundary.

Verified on the current environment (RTX 3080, driver 617.14): real NVENC extraction returns `True` with
2,964,751 bytes and exactly 60 frames, its detailed reason empty; real CPU H.264 extraction returns
`True` with 11,944,372 bytes and exactly 60 frames. `ANALYSIS_VERSION`, cache identity, the frame-lock
timeline builder, NVENC/CPU quality arguments, `_effective_clip_workers`, `-hwaccel cuda` and
`logger.check_nvenc()` are all unchanged; 21 protected files are blob-identical to the base commit.

### Added — 2026-09-26 (Phase 2B: live Qwen worker progress)

Closes the last observability hole in the pipeline: the parent → Python-worker subprocess boundary.
Observability only — no creative, inference, scoring, cache-identity or render behaviour changed.

- **New fork module `src/beatsync_fork/qwen_progress.py`** (stdlib-only, like the rest of the package).
  Three pieces: a namespaced one-line JSON wire protocol (`encode`/`decode`/`is_protocol_line`), a
  `QwenProgressTranslator` that folds payloads into Stage 5 `phase="qwen"` events, and
  `stream_worker_process()` / `run_qwen_worker()`, the `Popen`-based runner.
- **The worker emits machine-readable progress** (`src/auto_mode/stage5_qwen_scene_worker.py`).
  `BEATSYNC_QWEN_PROGRESS\t{…}` lines for `worker_state` (`loading_model`, `backend_ready`,
  `prefetch`), `job_start`, `job_progress` and `job_end`, **additive** to the existing human-readable
  lines, which are byte-for-byte unchanged. Emission goes through a guarded `_emit_progress()` that
  swallows every failure — including a missing fork package — because a status line must never cost a
  run that has already spent GPU minutes. The only other change to this file is an optional
  `job_context` parameter on `_run_semantics_for_video()` carrying
  `job_index`/`job_total`/`job_id`/`source_name` so emitted lines can be attributed. Prompt, schema,
  vocabularies, device/slot/context selection, frame prefetch, inference waves and the
  `LlamaServerClient`/`LlamaMtmdClient`/`QwenLlamaClient` classes are all AST-identical to the base
  commit.
- **The parent streams instead of capturing** (`src/video_analysis.py`).
  `_run_qwen_worker_batch()` and `_run_qwen_worker()` now call `fork_qwen.run_qwen_worker()` with the
  same argv, environment, UTF-8-with-replacement decoding, timeout and `{}`-on-failure contract they
  had with `subprocess.run(capture_output=True)`. `event_callback` is threaded through
  `analyze_video_sources` → `_analyze_single_video` / `_complete_deferred_qwen` /
  `_complete_deferred_qwen_batch` → `_annotate_candidates_with_qwen` → both worker launches, so live
  progress works in **both** execution modes rather than silently only in batched runs.
- **stdout and stderr are drained concurrently** on two threads while the main thread owns
  `wait(timeout=…)`. Required, not decorative: a failing llama.cpp run emits megabytes of Vulkan
  diagnostics, and draining stderr only after `wait()` deadlocks once the pipe buffer fills. stderr is
  retained as a bounded tail (2400 chars batch / 1800 single — the limits the old code already
  printed), so RAM stays flat however loudly the worker fails.
- **Reported truthfully.** Per-job progress is an *uncounted* `STATE` event: the only provable
  denominator is the current job's `len(frame_items)`, so the panel shows
  `Qwen job 17 / 300 · 64 / 120 candidates (53.3%) · 2.1 candidates/s · batch 8` and never invents a
  cross-job total. The rate is the worker's own measured `idx / elapsed`, labelled **`candidates/s`**.
  Staying uncounted also keeps `· 758 sources completed` visible and avoids fighting `ProgressView`'s
  per-`(stage, phase)` monotonicity, which would otherwise reject job 18's restart at `1 / 130` as a
  stale straggler.
- **The response JSON is still the only semantic authority.** Nothing is reconstructed from stdout; a
  test asserts the streaming path returns exactly what the old capture path returned for identical
  response JSON, and that results are identical with and without a progress callback. Request/response
  files remain retained per the project's PCBUS-HK-v1 override (the `finally:` blocks are still bare
  `pass`).
- **Scope of `Popen`.** `stage5_qwen_scene_worker.py` already used `subprocess.Popen` for its internal
  `llama-server` lifecycle long before Phase 2B, handing it dedicated log-file handles rather than the
  worker's pipes; that code is untouched and a test pins it to exactly one `Popen` inside
  `LlamaServerClient._start`. The short `llama-mtmd-cli --version` probe remains a plain
  `subprocess.run`. A repo-wide "no `Popen`" invariant would be both false and about the wrong
  boundary.
- **Measured, not assumed.** Red evidence first: under `capture_output=True` a worker emitting its
  first progress line at ~0s delivered **0 callbacks before exit**, first observed at 0.857s — the exit
  timestamp. After the change, the real worker with the real Qwen3VL-2B GGUF on Vulkan reported
  `loading model` at +0.17s and `backend ready` at +8.98s of a 10.38s run. Timeout process-tree
  boundary was measured both ways: the direct Python worker is killed and an already-started
  `llama-server` is **orphaned**, identically to the pre-Phase-2B path, because both kill only the
  direct child. That gap is pre-existing and was deliberately not "fixed" with `taskkill /T`.

Two defects found by the new tests and fixed before commit: a job's first `job_progress` was being
swallowed by the throttle window (the update that replaces "starting" with a real count), and closing a
pipe from the waiting thread blocked on the reader's buffer lock, turning a 2s timeout into a 120s
return whenever a grandchild held the write end — reader threads now own their own close and are joined
against one shared deadline.

`ANALYSIS_VERSION` is unchanged (`auto_av_analysis_v8_llama_vulkan_batched`): a progress-only change must
not invalidate a 758-video analysis cache. `tests/test_no_runtime_dependency.py` now discovers fork
modules from disk instead of a hardcoded list, so a future module cannot escape the stdlib-only guard.

### Fixed — 2026-09-26 (Phase 2A review remediation, round 3)

- **`ProgressEvent.data` is now immutable *recursively*** (`src/beatsync_fork/progress.py`). R2's
  `MappingProxyType` was shallow, and the pipeline genuinely emits nested mutable values —
  `section_types=[...]` from Stage 3, `first_failures=[...]` from the Stage 6 refusal — so
  `event.data["section_types"].append("evil")` still succeeded, and `as_dict()`'s shallow copy handed
  the *same* nested objects to every consumer. Small stdlib-only `_freeze`/`_thaw` pair: mappings
  become `MappingProxyType` over recursively frozen copies, lists and tuples become tuples, scalars
  (including `str`/`bytes`) are untouched. `as_dict()` recursively thaws back to plain
  `dict`/`list`, so the payload shares no mutable container with the event and stays JSON
  serialisable. Only the container types the pipeline actually emits are handled; no serialization
  framework was added.
- **Stage 5 no longer bills cache hits as analysis throughput** (`progress.py`,
  `src/video_analysis.py`). `StageCounter.rate` was `current / elapsed`, and the source counter
  advances for cache hits too, so 420 instant cache hits plus one slow real analysis reported
  **839.9 sources/s** in the reproduction. The rate basis is now tracked separately from the
  completion count: `advance(..., counts_toward_rate=False)` records a cache hit without billing it,
  and `begin_rate_window()` re-bases the clock once the cache scan finishes. The count still reads
  `421 / 758` with cache hits visible; the rate now reads `0.1 analyzed sources/s`, labelled via a
  `rate_unit` so a bare `sources/s` cannot be misread next to a count that includes cache hits.
  **Stage 6 clip throughput is unchanged** — it opens no rate window, so its basis is the whole
  counter exactly as before. No ETA was added.
- **A stale straggling progress event no longer wipes a good measurement** (`progress_view.py`).
  Monotonicity protected `current` but `rate`/`elapsed`/`message` were overwritten unconditionally, so
  an out-of-order Stage 6 event carrying no rate silently degraded
  `612 / 1216 · 4.8 clips/s · elapsed 2m 07s` to `612 / 1216`. An event ignored for the count is now
  ignored for those fields too, and a known measurement is never replaced by `None`. Found by the
  portable UI smoke, not by the unit tests.
- **28 new tests** (`tests/test_progress_truth.py`), 16 of which were confirmed failing against
  `95150de` first, including AST/source assertions that `video_analysis.py` really opts cache hits out
  of the rate, really re-bases the window, and really labels the rate unit.

### Fixed — 2026-09-26 (Phase 2A review remediation)

- **Progress counters are now subphase-aware** (`src/beatsync_fork/progress_view.py`,
  `src/video_analysis.py`). Monotonicity was enforced per *stage*, but Stage 6's ProRes path contains
  two counted subphases with different units and denominators — conversion counts sources, extraction
  counts clips — so the conversion count leaked into extraction. Reproduced before the fix:
  758 sources followed by 100 segments rendered **`758 / 100 (758.0%)`**; with 1216 segments,
  extraction appeared to begin **62.3% complete**. The monotonicity key is now `(stage, phase)`, taken
  from the `data["phase"]` marker the pipeline already emitted. Nothing carries across a phase
  boundary — not the count, total, unit, rate or elapsed time — while monotonicity *within* each phase
  is unchanged, so a straggling `4 / 10` after `7 / 10` still shows `7 / 10`.
- **No fake percentage for uncounted phases.** `Final assembly started` previously still displayed
  `1216 / 1216 (100.0%)` as though the assembly itself were complete; the Qwen phase likewise wore the
  deterministic pass's `758 / 758 (100.0%)`. An uncounted active phase now shows its own state plus a
  history line (`· 1216 clips completed`) instead of inheriting a counter. The Qwen state events carry
  `phase="qwen"` so the view can tell the two apart. **Qwen live N/T progress remains NOT implemented
  (Phase 2B); no `Popen` was introduced.**
- **`ProgressEvent.data` is genuinely read-only.** `frozen=True` only prevented field rebinding, so
  `event.data["x"] = ...` succeeded and an event handed to several consumers could be edited under the
  others. `data` is now a `MappingProxyType` over a private copy; `as_dict()` is built field by field
  because `dataclasses.asdict` deep-copies and cannot handle a mappingproxy. Stdlib only, no
  serialization framework.
- **23 new tests** (`tests/test_progress_phases.py` plus immutability regressions), including AST
  assertions that `video_processor.py` really emits `prores_convert` / `prores_extract` / `assembly`
  and `video_analysis.py` really emits `qwen`, so the suite cannot drift into testing an invented
  event shape.

### Added — 2026-09-26 (Phase 2A — structured progress / observability)

- **Structured progress core** (`src/beatsync_fork/progress.py`, `progress_view.py`): an immutable
  `ProgressEvent(stage, kind, message, current, total, elapsed_seconds, rate, data)` with a `Stage`
  enum, `EventKind` (start/progress/metric/state/warning/error/end), JSON-friendly `as_dict()`, and a
  `StageCounter` that is monotonic, bounded and throttled (~2 updates/sec, always emitting the final
  one). `emit()` swallows any callback exception — and a `None` event — so observability can never
  fail a render; `KeyboardInterrupt` still propagates. Stdlib-only, so the whole core is testable on a
  bare interpreter. `ProgressView` accumulates events into the status panel.
- **The GUI no longer parses prose for stage identity.** `gui.py` previously recovered the current
  stage with `re.search(r"Stage (\d+) is processing", message)`; that regex is gone. Stage identity is
  now the integer `event.stage`. The existing architecture is preserved — worker thread → `queue.Queue`
  → generator → widgets — and the `event_callback` only ever enqueues, so no Gradio component is
  touched from a worker thread (asserted by a test).
- **Stages 1-4** emit start/end boundaries with useful metrics (beats + tempo, features profiled,
  section count, cuts/beats/ratio/interval/preset). No ETAs: these stages take seconds, so a projection
  would be noise.
- **Stage 5 deterministic analysis** reports real source progress — `total` is the actual source count,
  **cache hits count as completed work** (a fully cached run shows completion rather than sitting at
  zero), and a video that fails in parallel and is retried serially advances the counter exactly once.
  Worker count and cache-hit count are reported as metrics.
- **Stage 6 rendering** reports `current / total` clips against the frame-locked segment count, with
  measured rate and elapsed. `current` advances only for clips actually created, individual failures
  surface as concise warnings (first three in detail, then a running count), and the
  incomplete-timeline refusal — unchanged in behaviour — is now visible in the UI instead of only on a
  discarded stdout.
- **ProRes** reports both of its serial loops: sources converted and segments extracted.
- **Final assembly** emits start/finish states and a concise error on failure. No fake percentage for a
  single FFmpeg call.
- **Backward compatible.** `progress_callback` and `console_callback` are untouched and
  `event_callback` is optional, so CLI/headless callers keep working. Legacy string statuses are still
  accepted on the same queue, but only shown before any structured event arrives, so they cannot
  overwrite richer output.
- **Qwen live progress is NOT implemented.** `video_analysis.py` still launches the worker with
  `subprocess.run(capture_output=True)`, so the worker's own `Qwen llama.cpp tagged N/T` lines remain
  invisible to the parent until it exits. Only honest high-level states are emitted
  (started / tags N/T / finished / failed). Live streaming is **Phase 2B**; no `Popen` was introduced.
- **100 new tests** (`test_progress_core.py`, `test_progress_sequence.py`, `test_gui_progress_seam.py`)
  plus the fork no-runtime-dependency guard extended to both new modules.

### Fixed — 2026-09-26 (Phase 1B review remediation)

- **The render gate now validates the LIVE source controls, not only the stored session state**
  (`src/beatsync_fork/input_confirmation.py`, `src/beatsync_fork/input_session.py`, `src/gui.py`).
  Gradio delivers widget changes as separate queued events, so at the moment Create is clicked the
  `gr.State` can lag behind the widgets. Reproduced before fixing: a browser confirmation of 2 files
  was **allowed** while the live `gr.File` value already held 3, because `current_snapshot_for_render`
  re-stat'ed `confirmed.paths` instead of the live list; likewise a confirmed folder was allowed while
  the folder textbox already pointed elsewhere. `process_video_guarded` now receives `source_mode`,
  `source_folder`, `source_recursive` and `video_input` as render-request inputs and builds a
  `LiveSourceDeclaration`; `check_declaration()` compares declared intent (mode / folder / recursive)
  before any filesystem work, so a folder the user has navigated away from is never even scanned. The
  event-driven invalidation is unchanged and still provides immediate UX feedback. Handler parameter
  names mirror the widget names because Gradio supplies them positionally, and a test asserts the two
  lists line up name-for-name.
- **Folder identity now covers the whole supported scope, not just the ready subset** (same modules).
  Reproduced before fixing: after confirming 1 ready `.mp4`, an external writer created a 0-byte
  `new.mp4`; the scanner rejected it as `empty_file`, the ready list was unchanged, and the gate
  **allowed** the render even though a new supported source entry had appeared. This is not
  hypothetical — the real `Cuts` library was observed gaining about one MP4 per minute. `SourceSnapshot`
  gained `excluded`, an ordered tuple of `ExcludedEntry(path, reason)` for supported-extension files the
  scan could not use, and it participates in the digest (`SNAPSHOT_VERSION` → v2). Only the stable
  reason code is recorded, never OS error text, which would make the digest unstable. Unsupported files
  (`.mp3`, `.txt`, `.jpg`) are deliberately excluded from identity, so adding one does not invalidate a
  confirmation. The render list remains the **ready** files only; no media is hashed.
- **35 new tests** (`tests/test_input_gate_live.py`, `tests/test_gui_guard_seam.py`): live browser list
  +1 / −1 / same-count replacement / reorder / empty / missing file, live folder / recursive / mode
  changes, "a folder outside the declaration is never scanned", new empty and new unreadable supported
  files, rejected↔ready transitions, unsupported additions ignored, and a dependency-injected seam test
  proving `process_video` is never reached after a denial.

### Added — 2026-09-26 (Phase 1B — GUI source modes + confirmation gate)

- **Local folder video-source mode** (`src/gui.py`, `src/ui_content.py`), the new default and the
  recommended mode for large libraries. It calls the existing `beatsync_fork.input_manager` — no
  duplicate enumeration logic in the UI — and shows exact
  `Discovered / Supported / Rejected / Ready / Duplicates / Total size`. Source files are referenced
  **in place**: no Gradio upload, no copies. Browser-files mode remains available and unchanged.
- **Authoritative confirmation gate.** `Create Music Video` now starts disabled and is enabled only by
  an explicit `Confirm N files` action.
- **`src/beatsync_fork/input_confirmation.py`**: `SourceSnapshot` — an ordered identity over
  (normalised path, size, mtime_ns) per file plus mode, scan root and recursive flag, digested with
  SHA-256. Confirmation is over the **set**, not the count, so two different lists of equal length are
  never interchangeable. File contents are not hashed: the head+tail fingerprint in `input_manager` is
  for duplicate candidacy and must not become a per-render cost. Also provides `describe_change()` for
  actionable abort messages and `evaluate_gate()`.
- **`src/beatsync_fork/input_session.py`**: the source state machine (`set_mode`, `set_folder_path`,
  `set_recursive`, `scan_folder_action`, `set_browser_files`, `confirm_action`) and
  `resolve_for_render()`. Pure, Gradio-free and stdlib-only, so the gate is fully testable without a
  web server.
- **Render-time re-verification.** `process_video_guarded()` in `gui.py` re-derives source identity
  from the filesystem on every click and refuses before Stage 1 if it no longer matches the
  confirmation. UI disablement alone is treated as a courtesy, not a guarantee.
- **Invalidation rules**: mode switch, folder path change, recursive toggle, re-scan, and any browser
  file-list change all clear the confirmation. FPS, encoder, output filename and audio deliberately do
  not — they are not source identity, and a test asserts the confirmation survives them.
- **Honest browser semantics.** Browser mode reports only `Backend ready: N`. The browser-side
  selected/pending count is never transmitted to Python, so it is never displayed; a test asserts no
  such number is printed.
- **64 new tests** (`tests/test_input_confirmation.py`, `tests/test_input_gate.py`) covering snapshot
  identity (added / removed / replaced-same-count / renamed / size / mtime / order / root / recursive /
  mode), empty-set refusal, and the full gate matrix. The fork no-runtime-dependency guard now covers
  the two new modules as well.

### Added — 2026-09-26

- **Repository workflow policy** (`CLAUDE.md`): `DU-REPO-WORKFLOW-v1` task-closeout rules.
- **Fork identity surface** (`src/beatsync_fork/__init__.py`): `FORK_NAME`, `FORK_VERSION`,
  `UPSTREAM_BASELINE_COMMIT`, `fork_identity()`.
- **Local input manager core** (`src/beatsync_fork/input_manager.py`,
  `src/beatsync_fork/input_report.py`): deterministic local-folder scanning with explicit
  discovered / supported / rejected / duplicate / ready accounting and cheap duplicate-candidate
  fingerprinting. **Not yet wired into the GUI** — see `CLAUDE.md`.
- **Test harness** (`pytest.ini`, `requirements-dev.txt`, `tests/`): first automated tests in the
  project, including a 1000-file regression test proving the input manager does not truncate large
  source lists.
- **`.gitignore`**: runtime/build artefacts (`bin/`, `input/`, `output/`, `__pycache__/`).

### Changed — 2026-09-26

- **`README.md`**: added a fork notice and a licence section. Upstream content is otherwise unmodified.

### Fixed — 2026-09-26

- **Input scan no longer silently skips an unscannable directory** (`src/beatsync_fork/input_manager.py`).
  `os.walk` ignores `scandir` failures unless an `onerror` callback is supplied, so an unreadable,
  vanished or disconnected nested directory was skipped in silence while `scan_folder()` still
  returned an `InputSet` that could report INPUT READY — the same silent-truncation failure the module
  exists to prevent, sourced from the filesystem instead of the browser. Traversal is now
  all-or-nothing: a complete walk returns an `InputSet`, and any directory that cannot be listed
  raises `InputScanError` naming the failing path and preserving the original `OSError` as `__cause__`.
  `followlinks=False` and all ordering, classification and accounting semantics are unchanged.
  Individual files that cannot be stat'ed remain `unreadable` rejections rather than scan failures,
  because a named rejection is not a silent loss.
- **Non-recursive scans no longer drop an entry whose metadata probe fails**
  (`src/beatsync_fork/input_manager.py`). The non-recursive iterator selected entries with
  `os.path.isfile()`, which *suppresses* stat/access errors and returns `False`. A locked, offline or
  vanished top-level source file therefore disappeared before the classifier saw it — absent from
  `ready`, absent from `rejected`, and missing from `discovered_count`, so even the
  `discovered == ready + rejected + path_collisions` invariant could not detect the loss. Reproduced:
  a three-file folder with one unreadable `.mp4` reported `discovered=2, ready=2, rejected=0` and
  INPUT READY. The iterator now decides only "directory or not" from the `scandir` entry and passes
  everything else to the classifier, which accounts for it as `unreadable`. This also removes a
  redundant `stat` per entry. Ordinary directories are still excluded; recursive behaviour,
  ordering, duplicate and collision semantics are unchanged. Side effect: a broken symlink at the top
  level is now reported as an `unreadable` rejection instead of vanishing.

### Removed — 2026-09-26

- Untracked the stale compiled artefact `src/auto_mode/__pycache__/stage5_qwen_scene_worker.cpython-313.pyc`
  from Git. The file was left on disk (it is regenerable, and removing files on disk is not this
  change's business).

### Unchanged

No change to generated video output. Auto Mode stages 1-6, `AutoWaveConfig` defaults, the analysis
cache format, seed/randomness behaviour, `gui.py` and the render path are untouched.
