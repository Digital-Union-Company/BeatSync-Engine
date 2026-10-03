---
paths:
  - "src/beatsync_fork/input_report.py"
  - "src/video_processor.py"
  - "src/video_analysis.py"
  - "tests/test_scale_diagnostics.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Scale diagnostics (L0)

The roadmap targets 5,000+ sources and ~100,000 candidate moments with **no source-count cap**, so the
rule is *measure first*. L0 adds timings and counts around the four places that grow with the library
and **optimises nothing**. There are no thresholds, no warnings, no caps and no metrics store — a
number that would be a gate is out of scope by design.

- **Folder scan** — `InputReport.render_text()` shows the `scan_seconds` the scan already recorded
  (`Scan time:   4.1s`). `format_seconds` is total: non-numeric, `NaN`, `inf` and negatives all render
  `0.0s`, because a report must never raise inside a UI callback.
- **Render-time source verification** — `_process_video_guarded_unlocked` owns the **one**
  authoritative `resolve_for_render` call and times it, handing the figure to `process_video`,
  which reports it as a completed **Stage 0** (`Stage.INPUT` already existed for pre-Stage-1 source work; L0 is its first producer).
  The gate is timed, never changed: same call, same live declaration, same allow/deny, and no
  verification result is ever reused between renders.

  **C3-R0 moved where that call lives, not what it does.** Rendering became mutually exclusive and
  gained a second entry point, so the gate (and its timer) sit in one shared core that both
  mutex-owning wrappers reach:

  ```
  _process_video_guarded_unlocked   = the ONE timed live source gate + render core
  process_video_guarded             = single-render mutex wrapper      -> core
  render_selected_variants_guarded  = C3-R0 batch mutex wrapper        -> core, per candidate
  ```

  Because the batch calls the core **once per candidate**, each candidate is independently
  re-verified and carries its **own** `verification_seconds` — there is deliberately no
  batch-level gate and no cached decision shared between the two renders. Neither wrapper may
  rebuild a second gate or a second timer; a seam test asserts exactly one `resolve_for_render`
  exists in `gui.py`. The diagnostic output format is unchanged: C3-R0 added no metric, no
  threshold and no ETA. `StageConsoleLogger.end_stage` gained an
  optional elapsed override used only for a stage whose END is the first event the logger sees —
  otherwise Stage 0 would print "ended in 0 seconds" for work that already happened.
- **Stage 5 cache scan** — `cache_identity_seconds` (the `_cache_path`/`_video_signature` side,
  including the D2 bounded content fingerprint) and `cache_lookup_seconds` (the `_load_cache` side)
  are accumulated around the *existing* calls with `perf_counter`, plus `cache_lookups`. They are
  deliberately separate: the two costs scale differently, and combining them would hide which one
  grows. Never add them to deterministic or Qwen analysis time.
- **Stage 5 counts** — `candidate_count` is `len(all_candidates)` directly, never re-derived from
  telemetry, so it cannot disagree with the list Stage 6 receives. `source_count`, `cache_hits` and
  the R1 `*_this_run` fields are unchanged, and the current-run / historical-aggregate separation
  still holds: the summary line order is sources → current-run Qwen → cache check → `Analysis time` →
  visual library, with the optional *cached-library* line yielding to the five-line budget. **The
  budget is not raised.**
- **Stage 6 planner** — `create_music_video` times only `build_planned_clip_sequence` and records
  `planner_seconds`, `planner_candidate_count` and `planner_segment_count` in `render_info`.
  `planner_candidate_count` is the candidate **moments** presented to the planner, never the source
  file count — that distinction is the whole point of the number, since the planner cost is
  segments × candidates. Existing render diagnostics (`render_cuts`, `timeline_frames`, `encoder`,
  `audio_duration`, `final_assembly_seconds`) are untouched and not duplicated.

**All of it is ephemeral invocation metadata.** Nothing here enters a cache payload, a cache key or
the completion contract: `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are unchanged, and
`tests/test_scale_diagnostics.py` asserts the new names are absent from `_video_signature`,
`_cache_path`, `_qwen_config_token`, `_cache_entry_is_complete`, `_checkpoint_cache` and the
record-producing functions. One name collides on purpose: a per-source record has carried its own
`candidate_count` since long before L0 — different scope, same word, and the test pins the old one
unchanged rather than forbidding the name.
