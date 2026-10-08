---
paths:
  - "src/video_analysis.py"
  - "src/auto_mode/stage5_qwen_scene_worker.py"
  - "src/beatsync_fork/qwen_progress.py"
  - "tests/test_qwen_stream.py"
  - "tests/test_qwen_worker_protocol.py"
  - "tests/test_qwen_semantic_recovery.py"
  - "tests/test_media_neutral_semantics.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Stage 5: the out-of-process Qwen worker

## Stage 5 runs out-of-process

`video_analysis.py` never loads a model in-process. It writes a JSON request into
`input/video_analysis_cache/`, spawns `stage5_qwen_scene_worker.py` with `sys.executable`, and reads a JSON
response back. The worker prefers a persistent **`llama-server`** (model loads once, parallel slots) and
falls back to **`llama-mtmd-cli`** per frame. Multiple source videos are batched into one worker process by
default (`BEATSYNC_QWEN_BATCH_VIDEOS`) for the same reason.

The request/response JSON files are **intentionally left behind** for debugging — the `finally:` blocks
that would delete them are deliberately empty. Do not "clean that up".

**The parent streams the worker's stdout** (Phase 2B). `_run_qwen_worker_batch()` and
`_run_qwen_worker()` go through `beatsync_fork.qwen_progress.run_qwen_worker()`, which uses `Popen` and
drains stdout and stderr on **two separate threads** while the main thread owns `wait(timeout=…)`.
Draining both concurrently is mandatory, not tidiness: a failing llama.cpp run emits megabytes of Vulkan
diagnostics, and reading stdout to EOF first deadlocks the moment the stderr pipe buffer fills. stderr is
kept as a bounded tail (2400 chars batch / 1800 single — the same limits the old code printed), so a
loudly-failing worker cannot grow the parent's RAM.

Three boundaries here are load-bearing:

- **Only the two long-running Python-worker launches were converted.** The short
  `llama-mtmd-cli --version` probe in `_llama_version_token()` is still a plain `subprocess.run` — it
  feeds the cache signature and has nothing to stream.
- **The worker's own `subprocess.Popen` is a different thing.** `LlamaServerClient._start` has managed
  the `llama-server` child since long before Phase 2B, handing it dedicated log-file handles
  (`*_llama_server_ctx*_stdout.log`), not the worker's pipes. Never state a repo-wide "no `Popen`"
  invariant — it is false and it describes the wrong boundary. A test asserts the worker still contains
  exactly one `Popen` and that it is still `_start`'s.
- **A reader thread owns closing its own pipe.** Closing a pipe from the waiting thread blocks on the
  buffer's internal lock while its reader sits in `readline()`; with a grandchild holding the write end
  that turned a 2s timeout into a 120s return (measured). `stream_worker_process` therefore joins both
  readers against **one shared deadline** and closes only pipes whose reader has already finished.

Timeout and failure semantics are unchanged from `subprocess.run`: bounded wait, then `terminate()` →
short grace → `kill()`; a non-zero exit prints the bounded stderr tail and returns `{}`; deterministic
visual tags stay active either way. Measured process-tree boundary on a timeout: the direct Python worker
is killed, and an already-started `llama-server` is **orphaned** — identically to the pre-Phase-2B path,
because both kill only the direct child and neither runs the worker's `finally: client.close()`. That gap
is pre-existing; do not "fix" it with `taskkill /T` or a process-group redesign as a side effect of
unrelated work.

**The response JSON remains the only semantic authority.** Nothing is reconstructed from stdout and no
tag is ever parsed out of a progress line, so the same request returns the same semantics whether a
progress callback exists or not. A test compares the streaming result against the old capture path for
identical response JSON.

Qwen is advisory, not authoritative: `_merge_semantic()` keeps deterministic motion/quality metrics
dominant (e.g. action = 0.72·deterministic + 0.28·semantic·motion_gate) so a pretty static frame can't be
hallucinated into an action shot. Keep that weighting shape when adding semantic fields.

## A persistently rejected candidate gets one targeted recovery (R1)

A candidate whose semantics `_normalize_semantic` rejects used to make its **whole source permanently
uncacheable**, because `_qwen_job_completed` requires `returned_ids == requested_ids`, so one missing
tag means no checkpoint and the source is re-analysed on every run forever. Two sources in the real
845-file library were in exactly that state (measured: 10 requested, 10 decoded, 9 tagged), costing a
~51 s Stage 5 tax on every warm run.

Measured root cause — **truncation**, not a field-level rejection. The primary request budgets
`_max_new_tokens()` (default 128) and leaves `description` an unbounded string. llama-server returns
`finish_reason="length"` with non-empty but truncated text; `_parse_json_object`'s `\{.*\}` finds no
closing brace, `json.loads` fails, and `_normalize_semantic` rejects at its not-a-dict guard. All 8
numeric keys, both enums and substantial description content were already present in the raw text;
what was missing was **JSON termination** — generation stopped mid-description, so neither the
description string's closing quote nor the object's closing brace was emitted (the captured output
has an odd quote count). The description value is therefore *not* syntactically complete, which is
why no amount of lenient brace-matching would rescue it. Because decoding is greedy
(`temperature 0`, `top_k 1`) every retry re-issues the identical request and gets byte-identical
output, which is why it never resolves.

**Raising the token budget alone is not a fix, and that is measured rather than assumed.** One of the
two cases is a degenerate repetition loop (`lips moving, lips open, lips closed, …`) that simply
consumes a larger budget too: still truncated at 160, 192 **and 256** tokens, growing 350 → 470 → 614
→ 880 characters. The grammar bound is the half that stops the loop; the extra budget is only needed so
the bounded JSON can close (131 and 129 tokens observed). The bound alone also fails — it leaves the
other case one token short. Smallest variant recovering both 3/3 deterministically:
**160 tokens + `description.maxLength = 96`**. Larger bounds (112, 128) also pass but retain *more* of
the repetition, which is the argument for 96.

Load-bearing details:

- **The primary path is untouched.** `_max_new_tokens()` still governs the ordinary request, still
  keyed into the D2 signature through `BEATSYNC_QWEN_MAX_NEW_TOKENS`; `SEMANTIC_SCHEMA` still carries
  `description = {"type": "string"}` with no bound; the prompt and greedy sampling are unchanged.
  `_recovery_semantic_schema()` deep-copies rather than mutating the global. Proven cross-branch:
  primary output is **byte-identical** to merged main on all four measured candidates (342/347/350/306
  chars) against one shared llama-server instance.
- **Recovery is deliberately LAST in the control flow**, and every *applicable* pre-existing primary
  retry/fallback path stays ahead of it. The tiers are conditional, not a fixed sequence every
  candidate walks: the initial attempt always runs; the server retry applies to failed candidates
  while a server is still available; the reduced-slot restart fires only on its existing condition
  (`valid_ratio < 0.70`, server active, `batch_size > 1`) and `return`s recursively, so only the
  innermost wave reaches recovery; the serial/CLI fallback applies per the existing backend state.
  Recovery does **not** force any of those tiers to run — it simply sits after whichever ones did.
  Only then does a still-unresolved candidate get **exactly one** recovery generation. No recursion,
  no second attempt.
- **Eligibility is recomputed from `semantics`, not from `failed`.** The serial fallback tier resolves
  candidates without rewriting `failed`, so trusting `failed` would re-ask for semantics that already
  arrived.
- **Semantic rejection only, never transport failure.** `_is_semantic_rejection(semantic, text)`
  requires a falsy semantic *and* non-empty text. An HTTP error, dead server, CLI timeout, non-zero
  exit or empty generation produces no truncated output to rescue, and re-asking would paper over a
  broken backend — those paths report `False` as the 4th tuple element. The classification is internal
  to the inference wave and **never reaches a cached payload**.
- **`max_tokens`/`semantic_schema` overrides default to `None` on all three `generate()` primitives**
  (`LlamaServerClient`, `LlamaMtmdClient`, `QwenLlamaClient`), so every existing caller is unchanged.
  The CLI client's ctx-fallback self-retry forwards them too — without that, a recovery hitting a ctx
  error would silently retry as an ordinary 128-token unbounded request and truncate again while
  appearing to have run.
- **The constants are hard-coded, not environment variables.** A `BEATSYNC_QWEN_RECOVERY_*` knob would
  be result-affecting Qwen configuration absent from `_qwen_config_token()` — exactly the defect D2
  fixed for `MAX_WINDOWS`. They are contract-governed instead, like the prompt and the schema.
- **No cache re-key and no contract bump for this first introduction**, and the argument is structural:
  a source current main caches had every requested candidate tagged on the primary path, so recovery
  never runs and the persisted semantics are identical; a source that missed a candidate fails
  `_qwen_job_completed`, so current main wrote **no complete record at all** — recovery can only turn
  an absence into a record, never contradict a stored one. The 843 existing D2 records stay reusable.
  A *future* change to these constants does not inherit that argument, because fallback-generated
  records will exist by then: default policy is to bump `CACHE_CONTRACT_VERSION` unless
  persisted-output compatibility is explicitly proven.
- **The completion contract is unchanged.** `_qwen_job_completed`, `_stored_ai_cache_is_consistent`,
  `ai_enabled`/`ai_deferred` semantics and checkpoint eligibility are all untouched. Recovery merely
  supplies one more candidate semantic; the existing machinery still decides whether the source may be
  checkpointed. A failed recovery leaves the candidate absent and the job incomplete, exactly as before.
- **Recovery does not cure repetition.** The recovered degenerate description is still partially
  repetitive — it is merely valid JSON, schema-valid, normalization-valid and bounded to ≤ 96 chars.
  The win is that one runaway description no longer makes an entire source permanently uncacheable.
  Prose quality is out of scope.

Measured cost: ~0.61–0.72 s per recovery call (mean ~0.65 s), so 2 calls ≈ 1.3 s for the known library.
Prediction only until a production run confirms it: once both sources recover and checkpoint, a
subsequent identical warm run should show 845/845 cache hits and launch no Qwen worker at all.

## Persisted Stage-5 semantics are media-neutral (P2)

**This is an architectural boundary, not an optimisation:**

```
STAGE 5                      = INTRINSIC MEDIA TRUTH      (persistent)
STAGE 6 / THE AI DIRECTOR    = CREATIVE INTERPRETATION    (ephemeral, per render)
```

Stage 5 records what is visually present — motion, character focus, visual quality, beauty, action
intensity, tension/softness, semantic content. It must **not** answer "what should I do with this shot
for this particular song?" That interpretation belongs downstream, and it may vary per render, per
section and per seed without invalidating or rewriting a single cache record.

A source therefore needs one semantic analysis per compatible **source identity** + **Qwen backend
identity** + **result-affecting media-semantic Qwen configuration** — not one per `smart_preset`.

**The prompt is the whole mechanism.** `stage5_qwen_scene_worker._build_prompt()` takes no arguments
and carries the validated instruction *"Assess the moment only from what is visually present; do not
adapt the tags to music, song energy, edit style, or desired pacing."* The old
`"The music edit style is {style_hint}."` conditioning, `_qwen_prompt_style_hint` and the
`smart_preset` component of `_qwen_config_token` are all gone. Do **not** replace the removed hint
with a fake constant style: after P2 there is no edit style in persisted Stage-5 semantics at all.

Load-bearing details:

- **Nothing about the music crosses the worker boundary.** Neither request JSON (single or batch)
  carries `audio_profile`; `_build_prompt` was its only consumer. Tests execute the real
  request-building bodies and assert the exact key sets.
- **`audio_profile` survives on `analyze_video_sources` as a retained integration signature with zero
  effect.** `auto_mode.analyze_beats_auto` still passes it, so that call site needed no change, but it
  reaches no cache key, no Qwen request, no prompt and no persisted record — a test asserts the
  parameter is absent from the function body, and that two arbitrarily different profiles produce the
  identical cache path. Every *private* seam that existed only to forward it
  (`_analyze_single_video`, `_annotate_candidates_with_qwen`, `_complete_deferred_qwen`,
  `_complete_deferred_qwen_batch`, `_run_qwen_worker`, `_run_qwen_worker_batch`,
  `_video_signature`, `_cache_path`, `classify_library_sources`) lost the parameter outright. Do not
  find it a new use.
- **The three media-semantic settings still re-key.** `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH` and
  `_MAX_NEW_TOKENS` change how many candidates get semantics, what the model sees and whether the JSON
  truncates, so they stay in `_qwen_config_token()`. Runtime-only knobs stay out.
- **`stage5_cache_v2 → stage5_cache_v3`, and the cold rebuild is intentional.** A v3 record *means*
  something different from a v2 one, so the one generation constant was bumped. There is deliberately
  **no migration**: v2 filenames are never produced or looked up again, nothing reuses their Qwen
  semantics, and the old files are left on disk untouched by the retention policy — no compatibility
  loader, no rewriter, no automatic deletion. `ANALYSIS_VERSION` is unchanged, because deterministic
  candidate scoring, window building and the candidate schema are.
- **The schema was deliberately *not* redesigned.** The real-material A/B validated the *existing*
  schema under a media-neutral prompt, so `recommended_use`, `emotion`, all eight numeric fields and
  the description contract are untouched; changing them here would have invalidated that evidence and
  widened the rebuild boundary. Under P2, read `recommended_use` as a media-neutral suggestion from the
  model, **not** an instruction tied to the current song — a future planner may weight it, ignore it or
  reinterpret it without touching the cache. Schema evolution is separate, later work.
- **The evidence.** Validated on the user's real material (`J:\New folder`): 23 analysed sources, 17
  source groups, 10,913 deterministic candidates, 115 identical A/B semantic moments; 115/115
  decoded/tagged with 0 failures on both sides; post-`_merge_semantic` mean score differences
  ≤ ~0.023; planner seeds 0/101/202 with 0 fallbacks and near-identical editorial scores; blind human
  review of 40 frames giving B a 10/16 decisive win rate with fewer editorial-leak (4 → 1) and
  false-action (2 → 1) flags. The reading is *not* "B is dramatically better" — it is that
  media-neutral semantics are not materially worse on real content, remain equally usable by Stage 6,
  and stop music/edit intent leaking into persisted media semantics. The earlier hockey human-scoring
  experiment is **superseded** and must not be cited as implementation evidence.
- **Normal Create Video is unchanged and still music-aware.** Stages 1–4 still run, Stage 6 still
  receives `beat_info`, sections, energy, targets, the creative seed and the candidate tags/scores, and
  its scoring and planning logic were not touched. The only difference is that Stage-5 semantics no
  longer change because Stage 1–4 resolved a different edit style.
- **Creative modes above Stage 5 must not undo this, and three have now shipped without doing so.**
  A Master Seed (Variant Lab), natural-language interpretation (AI Director) and **section-specific
  weighting (Freestyle V1)** are all implemented — entirely *above* Stage 5.

  **Director V2 is the closest call so far, and it did not cross the line.** It became narrowly
  content-aware, but in the one direction this boundary permits: the preparation *scan* counts
  usable moments from records it had already loaded, and a deterministic local step reads that
  concentration figure **after** the model has answered. No semantic intent, axis name, direction
  word or creative value reaches a Qwen request, the prompt, a persisted record or cache identity;
  the Stage-5 model, request format, prompt, schema and both generation constants are untouched; and
  the Director's own 4B text model is a *separate* asset that appears nowhere in Stage-5 identity.
  The scan gained no write path. `tests/test_media_neutral_semantics.py` pins every one of those
  halves. None of it is
  implemented **here**, and the guard did not weaken: `master_seed`, `director`, `proposal` and
  `freestyle` are still banned on the **Stage-5 side** (`video_analysis.py`, the Qwen worker,
  `library_prep.py`), which is where that ban was always load-bearing. A hybrid interpretation mode
  and an optional second style-aware pass over a *small shortlisted* candidate set remain future work,
  with no speculative abstraction added for either — `shortlist`, `second_pass` and
  `interpretation_mode` are banned everywhere including the GUI. The permanent constraints are:
  creative state never enters Stage-5 cache identity; no per-render interpretation overwrites
  persistent semantics; and a future second pass stays run-scoped rather than becoming the library's
  durable truth.

## The portable-Python `._pth` provenance hazard

`bin\python-3.13.14-embed-amd64\python._pth` is patched to include the runtime checkout's `src`, and
**that entry wins over `PYTHONPATH`**. A probe that simply does `import video_analysis` under the
portable interpreter can therefore load a *different checkout's* module and, because
`VIDEO_ANALYSIS_CACHE_DIR` is derived from `logger.ROOT_DIR`, point straight at the **real runtime
cache**. This has actually happened during a study. Any script that imports pipeline modules for
inspection must first strip that path entry, then assert `video_analysis.__file__` is the intended
file and that the redirected cache directory is not the runtime one — and refuse to run otherwise.
