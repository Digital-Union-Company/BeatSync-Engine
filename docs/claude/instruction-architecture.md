# Claude instruction architecture

Why the instruction set is split the way it is, and where every section of the pre-refactor root
`CLAUDE.md` went. This document is reference material: it is not a rule and is not loaded
automatically.

## The problem

The root `CLAUDE.md` had grown to **173,483 characters / 2,298 lines** across 59 headings. A root
`CLAUDE.md` is loaded into **every** session, so the entire architectural history of every subsystem
was being injected before the first question was asked — whether the session was about Smart Mix, a
typo in a batch file, or nothing to do with the pipeline at all.

## The layout

```
root CLAUDE.md                     always loaded   constitution: identity, commands, architecture
                                                   map, global invariants, hard safety floor
.claude/rules/operating-policies.md  always loaded  DU-REPO-WORKFLOW-v1, PCBUS-HK-v1,
                                                   PCBUS-WATCHDOG-v1 - they govern every task
.claude/rules/<subsystem>.md       path-scoped     loaded only when the matching files are touched
docs/claude/*.md                   on demand       history, superseded text, this document
```

Three decisions are worth recording:

1. **Path scoping, not root `@imports`.** A short root that `@import`s every extracted document
   would fix the file-length warning while still injecting the same context. The saving here comes
   from `paths:` frontmatter: a session that never opens `src/beatsync_fork/smart_mix.py` never loads
   the Smart Mix contract.

2. **Exactly one unscoped rule, and it is the policy file.** The repository workflow, housekeeping
   and monitoring policies are not subsystem knowledge — they apply to every task, including tasks
   that touch no Python at all. Scoping them would be wrong. Everything else is scoped. The root file
   restates only the handful of non-negotiables (never force-push, commit/push/verify closeout, the
   external scratch root, the two preserved runtime directories, the 20-minute read-only recheck) as
   a deliberate safety redundancy; the operative detail is not duplicated.

3. **`src/gui.py` loads one small rule, not fifteen.** `gui.py` integrates every subsystem, so
   attaching each subsystem rule to it would rebuild the explosion. Instead `gui-integration.md`
   carries the cross-cutting GUI invariants (the `QuietConsole` print trap, the worker-thread rule,
   the live-input render gate, positional `inputs` alignment, source/preparation isolation, the
   single-writer report panels) plus a routing table. Each subsystem rule is scoped to its own
   implementation **and its GUI seam test** - `tests/test_gui_guard_seam.py`,
   `tests/test_gui_progress_seam.py`, `tests/test_creative_controls_seam.py`,
   `tests/test_audio_layers_seam.py` - so working on a seam loads that seam's contract without
   loading all the others. The code layout made this possible: the seams are already pinned by
   dedicated test files, one per subsystem.

## Classification

| Class | Meaning | Where it lives |
|---|---|---|
| `ALWAYS_ACTIVE` | needed in almost every session | root `CLAUDE.md`, `operating-policies.md` |
| `PATH_SCOPED_RULE` | a live rule for one subsystem | `.claude/rules/*.md` with `paths:` |
| `REFERENCE_DOCUMENTATION` | real but historical; evidence, not instruction | `docs/claude/*.md` |
| `REMOVABLE_DUPLICATION` | restated something stated authoritatively elsewhere | removed, recorded in `removed-and-superseded.md` |

Subsystem rules were moved **verbatim**. Measurements and "do not retune this without new
measurement" evidence stayed *with* their rule rather than moving to `docs/claude/`, because under
path scoping that evidence is no longer injected into unrelated sessions - it loads exactly when
someone is editing the thing it constrains, which is when it is needed.

## Section mapping

Every H1/H2/H3/H4 block of the pre-refactor root file (59 headings, measured on
`origin/main` = `b7667975fb8ef692a174e46fd23132be1fab3ef8`). Rule paths are relative to
`.claude/rules/`.

| Old line | Lvl | Old section | New location | Classification | Disposition |
|---|---|---|---|---|---|
| 1 | H1 | CLAUDE.md | root `CLAUDE.md` | `ALWAYS_ACTIVE` | CONSOLIDATED - rewritten as the constitution |
| 5 | H2 | What this is | root `CLAUDE.md` | `ALWAYS_ACTIVE` | PRESERVED (AGPL/fork line folded in from Platform notes) |
| 15 | H2 | Commands | root `CLAUDE.md` | `ALWAYS_ACTIVE` | CONSOLIDATED - representative CLI set; all seven creative flags named |
| 43 | H3 | Tests | `test-harness.md` + root pointer | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 86 | H2 | Architecture | root `CLAUDE.md` (architecture map) | `ALWAYS_ACTIVE` | CONSOLIDATED |
| 88 | H3 | Pipeline | root stage table + `pipeline-core.md` | `ALWAYS_ACTIVE + PATH_SCOPED_RULE` | PRESERVED |
| 108 | H3 | Import order is load-bearing | `pipeline-core.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 127 | H3 | The frame-lock invariant | `pipeline-core.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 138 | H3 | A failed clip carries the reason FFmpeg gave (Phase 3B) | `pipeline-core.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 183 | H3 | Stage 5 runs out-of-process | `stage5-worker.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 234 | H3 | A persistently rejected candidate gets one targeted recovery (R1) | `stage5-worker.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 317 | H3 | Analysis cache | para 1 -> `docs/claude/removed-and-superseded.md`; para 2 -> `stage5-cache-identity.md` | `REMOVABLE_DUPLICATION / PATH_SCOPED_RULE` | SUPERSEDED (summary) + PRESERVED (`ANALYSIS_VERSION` bump rule) |
| 332 | H4 | Cache identity and the contract generation (D2) | `stage5-cache-identity.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 420 | H4 | Durability invariants (D1) | `stage5-cache-durability.md`; historical bullet -> `docs/claude/stage5-cache-history.md` | `PATH_SCOPED_RULE + REFERENCE_DOCUMENTATION` | PRESERVED + CONSOLIDATED (historical bullet relocated) |
| 559 | H4 | The portable-Python `._pth` provenance hazard | `stage5-worker.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 569 | H3 | Stage 5 returns two different truths (R1) | `stage5-reporting.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 654 | H3 | Counts and optional telemetry have different trust contracts (T1) | `stage5-reporting.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 761 | H3 | Console vs. UI output — structured progress | `progress-events.md` + root invariant + `gui-integration.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 854 | H3 | The creative variation seed (Phase A) | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 893 | H3 | The Creative Profile — three more controls (B0 + Creative Controls Core) | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 942 | H4 | Cut Density is a Stage 4 control | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 977 | H4 | Micro Cuts is the other Stage 4 control, and owns one layer | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1008 | H4 | Energy Response and Motion Bias are Stage 6 controls | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1047 | H4 | Source Diversity is the dynamic Stage 6 control | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1079 | H4 | Semantic Emphasis reinterprets persisted media truth (PR2) | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1128 | H4 | Stage 5 isolation is the real B0 invariant | `creative-controls.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1147 | H4 | Wiring and reporting | `creative-controls.md` + `gui-integration.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1190 | H4 | Future L2 invalidation — documented, not implemented | `creative-controls.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1207 | H4 | Creative Presets are named slider recipes and nothing else (PR3) | `creative-presets.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1306 | H3 | Audio Layers V1 adds voice without touching the edit (D) | `audio-mixdown.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1403 | H3 | Smart Mix V1 adds SFX to the same master (E) | `audio-mixdown.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim - includes the PR #26 ndarray + scalar rules |
| 1536 | H3 | Variant Lab V1 generates one reproducible recipe (C2) | `variant-lab.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1656 | H4 | What presets deliberately are *not* | `creative-presets.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1670 | H4 | Future Freestyle / Director boundary | `creative-presets.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1681 | H3 | Scale diagnostics (L0) | `scale-diagnostics.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1724 | H3 | Rendering modes | `pipeline-core.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1747 | H2 | Environment variables | `pipeline-core.md` + root pointer | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1766 | H2 | Platform notes | `platform-and-packaging.md` + root (Windows-only, AGPL) | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1780 | H2 | Fork-specific code (`src/beatsync_fork/`) | `fork-package.md` + root hard rule | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1813 | H3 | Media Library Preparation (P V1 + P2) | `library-preparation.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1879 | H4 | Analyze submits one bounded batch | `library-preparation.md` | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 1941 | H3 | Persisted Stage-5 semantics are media-neutral (P2) | `stage5-worker.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 2015 | H3 | Video source modes and the confirmation gate | `input-gate.md` + root invariant | `PATH_SCOPED_RULE` | PRESERVED verbatim |
| 2095 | H1 | Repository Workflow Policy: DU-REPO-WORKFLOW-v1 | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2100 | H2 | Mandatory task closeout — commit and push | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2134 | H2 | Exceptions — when NOT to commit or push | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2149 | H2 | Branch / remote safety | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2162 | H2 | Commit hygiene | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2171 | H2 | Post-push verification | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2190 | H1 | Housekeeping Policy: PCBUS-HK-v1 | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2197 | H2 | Temporary work goes outside the repository | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2212 | H2 | Repository hygiene | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2221 | H2 | Every task cleans up after itself | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2230 | H2 | Never clean what you did not create | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2238 | H2 | Classify before removing — conservative by default | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2255 | H2 | Project-specific retention (overrides the generic rules above) | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2264 | H2 | Git discipline | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2276 | H2 | Source-control awareness | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |
| 2285 | H1 | Monitoring Policy: PCBUS-WATCHDOG-v1 | `operating-policies.md` (unscoped) + root hard safety floor | `ALWAYS_ACTIVE` | PRESERVED verbatim |

## Durable markers

| Marker | Preserved in |
|---|---|
| `DU-REPO-WORKFLOW-v1` | `.claude/rules/operating-policies.md` (+ named in root) |
| `PCBUS-HK-v1` | `.claude/rules/operating-policies.md` (+ named in root) |
| `PCBUS-WATCHDOG-v1` | `.claude/rules/operating-policies.md` (+ named in root) |
| `stage5_cache_v3` | root `CLAUDE.md`, `stage5-cache-identity.md`, `stage5-worker.md`, `creative-controls.md`, `variant-lab.md`, `audio-mixdown.md`, `library-preparation.md`, `scale-diagnostics.md` |
| `auto_av_analysis_v8_llama_vulkan_batched` | root `CLAUDE.md`, `stage5-cache-identity.md`, `creative-controls.md`, `variant-lab.md`, `audio-mixdown.md`, `library-preparation.md`, `scale-diagnostics.md` |

`PCBUS-HK-v1` remains a synchronisation marker, not a version number - it is not bumped for wording
changes. The repository-local policy copy is still durable: it is tracked, it is unscoped so it loads
every session, and it travels with the repository across machines.
