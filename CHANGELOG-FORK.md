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

### Maintenance — 2026-10-10 (Cold cache-identity regression decision)

**No code change.** Decides the open `COLD_PARALLEL_IDENTITY_REGRESSION` finding recorded by H2
below. The current 16-worker default is **retained**, and the measured cold penalty on a mechanical
HDD is accepted as an explicit product trade-off.

```
COLD_PARALLEL_IDENTITY_REGRESSION_STATUS = ACCEPTED_TRADE_OFF
PRODUCTION_CHANGE_REQUIRED               = NO
```

H2 had measured cold at only 1 and 16 workers, leaving open the hope of an intermediate setting with
near-worker-1 cold cost and most of the warm benefit. D0 swept every already-supported setting on the
same frozen 1815-source library (manifest `96bc14cd…`), through the same real
`_compute_cache_paths_parallel`, with the same `-Et` standby-list purge before each timed run, a
balanced order frozen before any timing, and 3 valid controlled-cold runs per setting each with its
own immediate no-reset warm control:

| workers | cold median | warm median | warm speed-up | frontier |
|---|---|---|---|---|
| 1 | **99.5217 s** | 6.6150 s | 1.00x | PARETO (best cold) |
| 2 | 110.3139 s | 3.7322 s | 1.77x | PARETO |
| 4 | 114.3353 s | 2.0465 s | 3.23x | **DOMINATED by 8** |
| 8 | 114.0817 s | 1.6973 s | 3.90x | PARETO |
| 16 | 115.7938 s | **0.8070 s** | **8.20x** | PARETO (best warm) |

Cold turned out to be a **step at 1→2 workers and then flat** — the whole penalty is paid by the
second worker (+10.792 s) and the rest plateaus within ~1.5 s. So worker 1 is the only setting that
avoids the regression, at 8.20x warm cost, and there is **no universally superior static
replacement**: workers 1, 2, 8 and 16 all sit on the cold/warm frontier and **only worker 4 is
dominated**, by worker 8, which is faster on both axes. Nobody should choose 4.

Repeating the same full scan under the illustrative cold-first / fully-warm-subsequent model, worker 1
leads by 16.2721 s / 10.4641 s / 4.6561 s at 1 / 2 / 3 scans and worker 16 first leads at the fourth
by 1.1519 s (continuous crossover 3.8017 scans, i.e. more than 300 outstanding sources at the default
batch size of 100). **That is a conditional model statement, not an observed production threshold.**
`INTER_BATCH_CACHE_SURVIVAL = UNMEASURED` — a long Qwen batch runs between scans and whether it evicts
the bounded fingerprint windows was never measured, so the real mix of fully warm, partially warm and
cold later scans is unknown, and the real cumulative crossover with it. Identity evidence is stated in
absolute seconds; **no share of Stage 5 or of total preparation wall time is claimed**, because the
historical 41-source / 490.8 s Qwen figure is an order of magnitude only.

Two corrective strategies were examined and rejected on the measurements.
`STORAGE_DETECTION_REQUIRED = NO`: media type does not observe OS file-cache state, which is the
variable that decides the trade-off — a mechanical HDD can have a perfectly warm identity scan, so
"HDD → worker 1" would discard the measured warm benefit on that same HDD. There is also no seam for
it (no storage-type detection, no Win32 storage IOCTL, no `ctypes` storage code, no PowerShell storage
query, no path→volume→device mapping), so adding one is a platform dependency D0 does not support.
`ADAPTIVE_RUNTIME_SIGNAL = NOT PROVEN`: no robust production-safe cold/warm signal was measured, and
process-local invocation history is not OS file-cache authority. `NVME_SSD_STATUS = UNMEASURED`, and
the decision does not depend on SSD behaviour.

**The identity contract is unchanged.** D0 reconfirmed exact invariance: one ordered digest across all
35 identity computations at 1, 2, 4, 8 and 16 workers, with 0 `None` results, 0 exceptions, 0 manifest
drift, 0 duplicate positions and 0 cache payload writes. `CACHE_CONTRACT_VERSION` and
`ANALYSIS_VERSION` are untouched, no cache was invalidated, and
`BEATSYNC_CACHE_IDENTITY_WORKERS` remains available for benchmarking. Worker count is still execution
policy only.

This is **acceptance, not a fix** — the cold regression remains historically true and is not resolved
by code. It should be revisited only on bounded evidence: real user evidence that first-touch identity
latency is materially harming the workflow; an inter-batch cache-survival measurement showing repeated
full scans are mostly cold *and* the identity cost is product-significant; a material change to the
source-library workflow; a robust storage-agnostic cold/warm signal becoming available; SSD/NVMe
measurements revealing a different policy opportunity; or source counts growing until the absolute
regression becomes material.

### Maintenance — 2026-10-10 (H2 — controlled cold cache-identity benchmark, 1 vs 16 workers)

No code change. Closes the `NOT MEASURED` gap that *Performance — 2026-10-04 (Stage-5 Bounded Cache
Identity Parallelism R1)* shipped with, and that `.claude/rules/stage5-reporting.md` recorded as "not
a measurement of the ~103.8–110.5 s first-touch case under parallelism … do not claim a cold parallel
improvement … until one is measured on Windows against the exact candidate". It is now measured, and
**it is a slow-down, not an improvement.**

Measured on the real Windows library (now **1815 sources**, 45.7 GiB, on `J:` — a `WDC WD30EFRX`
mechanical HDD) through the real `_compute_cache_paths_parallel`, with a controlled standby-list purge
before every timed run and an immediate no-reset warm control inside every slot. Median of 3 valid
runs each, in the balanced order `1,16,16,1,1,16` frozen before any timing was observed:

| | worker 1 | worker 16 | ratio |
|---|---|---|---|
| controlled **cold** | **98.190 s** | **115.806 s** | **0.8479x — 17.9 % slower** |
| immediate **warm** control | 6.585 s | 0.767 s | 8.5838x |

The warm figure reconfirms L1B's 8.77x on a larger library; the cold figure inverts it. The groups do
not overlap — every worker-16 cold run (115.185–116.135 s) was slower than every worker-1 cold run
(97.933–103.280 s), with worker-16 spread of only 0.950 s — so this is not noise.

**Classification: `H2_RESULT_CATEGORY = D — COLD_PARALLEL_REGRESSION`, recording a new maintenance
finding `COLD_PARALLEL_IDENTITY_REGRESSION`.** The 16-worker path is a proven warm-cache optimisation,
**but** H2 establishes a cold first-touch regression on this measured mechanical-HDD workload — and
production currently applies that same default in both cache states. This entry **records** the
regression; it does not correct it, and the correction strategy is a separate future decision.

The finding is scoped deliberately: it is measured on this 1815-source mechanical-HDD library, applies
to controlled first-touch/cold cache identity, is **not** proven universal across storage devices, and
**does not affect identity correctness**. It is **not** a general regression of the parallel
implementation — warm behaviour remains strongly beneficial.

Separating what was measured from what explains it: the *measured fact* is that worker 16 is 17.9 %
slower cold on this workload. The *supported interpretation* is that the result is consistent with
seek/readahead contention on a mechanical HDD — 16 concurrent readers over scattered source files can
increase physical seeking and disrupt sequential readahead, whereas the warm result is consistent with
those bounded fingerprint reads being served predominantly from the OS file cache rather than
requiring the same cold disk access, so the `stat` plus bounded-fingerprint work parallelises cleanly.
**H2 measured the timing effect, not the storage mechanism**: it did not instrument seek counts,
storage queue depth, readahead decisions or head movement, and it did not establish that any
particular warm read avoided the disk, so the mechanism is not asserted as fact. The *open question*
is cold behaviour on SSD/NVMe, which remains **unmeasured**; the sign is therefore not generalised
beyond this HDD.

**Identity parity is exact**, which is the contract the worker knob is bound by: a single ordered
`sha256` over all 1815 positions across every worker-1 and worker-16 run, 0 `None` results,
0 exceptions, 0 manifest drift, 0 duplicate positions, identical frozen source manifest and identical
backend/config tokens throughout. `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and no
cache was invalidated.

Getting a trustworthy cold state was itself the hard part, and the first attempt failed. A driver
purging with RAMMap `-Es` produced six "cold" slots that were all warm (each within ~1–2 % of its own
warm control) because `-Es` is **Empty System Working Set**, which *demotes* cached pages onto the
standby list instead of discarding them — standby *rose* 895 MB across the purge. The correct
operation is `-Et`, **Empty Standby List**, confirmed from the RAMMap 1.63 binary's own usage string
(`-E[wsmt0]`) and menu resources rather than from recollection. `-Et` drove standby from 7 559.7 MB to
0.5 MB and was accepted only after a two-cycle warm→cold→warm→cold validation, plus a per-slot
telemetry condition and a cold/warm ratio floor fixed before the run. The invalid attempt is retained
in task evidence with its reason rather than discarded; its warm scaling and identity parity remain
valid and are the 8.58x reconfirmation above.

This measurement ran entirely against a frozen out-of-repository source export: 0 Qwen jobs,
0 `analyze_video_sources` calls, 0 cache payload writes, 0 renders, and no writes to production
`input/` or `output/`. It is an identity-phase benchmark only — no full Stage-5 speed-up or slow-down
may be inferred from it.

**The historical ~69 s full-call result remains unreproduced, but H2 now strongly explains its
cold/warm shape** through controlled identity-I/O measurement
(`HISTORICAL_69S_STATUS = STRONGLY_EXPLAINED_BUT_NOT_REPRODUCED`). It is not reproduced because that
figure came from ~845 sources through a full `analyze_video_sources()` call, which H2 never made. It
is strongly explained because controlled first-touch identity repeatedly measures ~98–100 s serial
against ~6.6 s warm on the larger real library — the same order of magnitude and the same direction as
~69 s versus ~3.2–3.4 s, which supports identity I/O and OS file-cache state as the explanation for
that shape. Neither `RESOLVED` nor `REPRODUCED`; nothing here retcons the original number.

### Fixed — 2026-10-10 (installer tool idempotence)

Two defects made `scripts/install.ps1` re-download bootstrap tooling it already had. No application
runtime code is involved, and no model, Python or FFmpeg pin moved.

**A valid llama.cpp `b9842` is no longer replaced because its version arrives on stderr.**
`llama-cli.exe --version` writes to stderr and leaves stdout empty (measured: exit 0, stdout empty,
`version: 9842 (6f4f53f2b)` on stderr). The old probe was `& $CliExe --version 2>$null`, which
discarded exactly the text it needed — and under the Windows PowerShell 5.1 that `install.bat`
launches, with `$ErrorActionPreference = "Stop"`, redirecting a native command's stderr *throws* a
`RemoteException`. So the probe did not merely come back empty, it landed in the `catch` branch, and
a perfectly good pinned build was re-downloaded and replaced on **every** run. Version probing now
goes through an explicit `Invoke-NativeProbe` helper that redirects both streams to temp files
outside the repository, preserves the native exit code, and reports a failed launch as exit `-1` so
callers treat it exactly like a wrong version. The naive `2>&1` fix was measured too and throws as
well, with the version line itself as the exception message. The matched build number is now derived
from `$LlamaBuild` rather than restated in the probe; the one remaining `version:\s+9842` literal is
a tripwire that is compared against the derived pattern and throws if they disagree, so moving the
pin fails loudly instead of silently matching nothing.

Both pinned-version comparisons were also tightened from a trailing `\b`, which matches between `0`
and `-` and so accepted `uv 0.13.0-rc1` as satisfying a `0.13.0` pin. That was found by measuring
the predicate against near-miss strings, not by reading it.

**UV is pinned to `0.13.0`, archive-verified, and retained after a successful install.** It came
from a floating `/releases/latest/download/` URL, so each run could install a different, unaudited
build of the tool that resolves and installs every other dependency — and then cleanup deleted the
whole `bin\uv` directory on success, forcing another download next time. UV now comes from the
pinned release, its archive is checked for exact size (15,722,003 bytes) and SHA256 before it is
extracted, and `bin\uv\uv.exe` is kept. A retained executable is reused only if it reports exit 0
*and* the exact pinned version; anything missing, wrong or unprovable is replaced. The installer
re-proves UV **after** cleanup, so retention itself is what gets verified before it reports success.
Download caches stay transient — `bin\downloads\` is still removed wholesale, so idempotence never
comes to rest on a cached archive.

Measured on Windows against an isolated copy of the verified portable runtime: run 1 reused
llama.cpp with **zero** llama archive downloads and fetched UV exactly once; run 2 reused both, with
zero llama and zero UV archive downloads, and the llama executables and `uv.exe` were byte-identical
before and after. This is **tool-asset reuse, not offline installation** — the installer still
resolves and installs packages from the network on every run.

Adds `tests/test_installer_contract.py`, the repository's first dedicated installer test: 37 checks
read the real `install.ps1` as source and assert the pins, the stderr-safe probe, the UV integrity
constants, the reuse/replace decisions and the retention rule. It runs on a bare CPython with no
PowerShell, Windows, network, portable runtime or model assets. Against the unmodified installer 19
of them fail.

### Maintenance — 2026-10-10 (Freestyle V1 Windows runtime acceptance)

No code change. Closes the `FREESTYLE_WINDOWS_ACCEPTANCE` debt that v0.1.0 shipped with — the one
recorded under *Added — 2026-10-04 (Freestyle V1)* below, which states that the acceptance run "was
**not** re-run against the reconstructed module, and could not be". That entry is left exactly as
written; it was true when written, and this entry supersedes it rather than rewriting history.

Freestyle V1 was re-verified on Windows against the **published v0.1.0 source**
(`fe84ef3a2c1c72cc1758bf9afaffd440637f6fd5`, tree `7b9b2c46`), exported clean and imported with an
isolation and module-provenance guard, on the RC1-verified portable runtime. The **original fixture
was recovered** from retained task evidence — the same 72 s track and the same three sources, hash
verified — so the historical cut counts are directly comparable rather than re-baselined.

All of it reproduced exactly: OFF **47**, every section `Base` **47**, `drop=High Energy` +
`intro=Cinematic` **43**, and a repeat run **43** with the identical timeline and plan hash. The
per-section counts matched too (OFF `intro 9 / drop 17 / verse 7 / finale 14`; ruled
`8 / 14 / 7 / 14`). OFF and all-`Base` produced an array-equal timeline, changing only the `drop`
rule left every other section untouched, each of the four real FFmpeg renders came out at 72.000 s
and exactly 2160 frames, and every post-warm-up condition hit the Stage-5 cache 3/3 with zero Qwen
jobs — a Freestyle change never invalidates media truth. 48/48 acceptance checks passed.

The reconstructed `freestyle.py` therefore behaves as the original implementation did, and
`.claude/rules/freestyle.md` no longer carries its "not re-verified for the current `freestyle.py`"
caveat. The historical 0.1.0 entries are unchanged.

## 0.1.0 — 2026-10-09

First tagged release of the Digital Union fork. Everything below was previously recorded under
`Unreleased` and is carried over unchanged; no entry has been rewritten, reordered or summarised.

### Fixed — 2026-10-09 (v0.1.0 RC0 release blockers)

Two independent corrections found by the v0.1.0 release candidate review.

**1. AI Director V2: intent evidence basis, and a truthful read-out.**

Every present semantic axis now carries a required `basis` of `requested` or `associated`, and
deterministic product policy decides which may reach an execution control. The system prompt and
model schema become the exact RC0-P3 evidence bytes
(prompt `4e43d0cb…40e928e8`, schema `d841b142…95cb37d4`); the model, installer, llama.cpp build and
media adapter are unchanged, and it is still one model call.

RC0-P0…P2 measured five materially different prompt wordings trying to stop the 4B model inferring
`source_variety` from mood alone, and none worked: a style-only instruction such as *"Let it come
apart at the seams."* kept producing an unrequested source-breadth preference that changed the
render. Asking the model to suppress the thought failed; asking it the separate question *"was this
requested?"* did not.

`basis` is not a confidence value and not a seventh dimension. It answers whether the user expressed
the preference, while `strength` still answers how much they want it, and the two are independent —
a faintly-put real request is `requested` with a small strength. A strength threshold was measured
and **rejected**: explicit breadth requests span 20…100 and overlap style-inferred emissions, so the
only threshold that rejects every style-only emission also discards 12 of 32 genuine requests.

```
cut_pacing · scene_reading · motion_preference            both bases actionable
impact_accents · section_reactivity · source_variety      requested only; an associated
    reading resolves exactly as an omitted axis — the control stays at 50, at any strength
```

`resolve_semantic_intent()` owns the policy so the GUI, the media adapter, Apply and any future
non-GUI caller share it. The raw intent is never filtered in place — it is provenance. Because
`basis` resolves *before* the media step, a suppressed association arrives as BASE 50, the
adapter's `BASE > 50` gate cannot fire, and FINAL stays 50 with no adjustment.

`basis` also created a divergence the read-out could not previously have: the model's `explanation`
describes its **raw** reading while the recipe shows what was **applied**, and on a requested-only
axis those can legitimately disagree. The explanation is left completely alone — never parsed,
rewritten, truncated differently or withheld — and one deterministic line derived from the
structured intent states the difference:

```
Director: ...suggests using more of the available clips rather than reusing a small set...
Not applied (associated, not requested): Source variety - left neutral.
```

The wording is attributed on purpose: it reports a model classification, not a claim about what the
user objectively asked for.

**Accepted limitation.** RC0-P3 measured **2 of 24** very faintly phrased explicit source-breadth
requests as `associated` — direction correct, basis conservative — so those leave Source Diversity
neutral instead of nudging it, and the new line makes that visible rather than silent. A false
negative is a no-op, whereas a style-only false positive changes execution without a request. The
RC0-P4 wording that fixed those two cases was rejected because it also made style-only text read as
`requested`, produced the programme's only wrong direction, and dropped targeted basis accuracy to
98.8 %. There is no claim of 100 % natural-language breadth recall.

`src/gui.py` needed no change. Contract: `.claude/rules/director.md`.

**2. Headless CLI: duplicate Windows source enumeration.**

`video_processor.get_video_files()` ran four separate `Path.glob` passes (`*.mp4`, `*.MP4`, `*.mkv`,
`*.MKV`). Windows `Path.glob` matches case-insensitively, so each real file was returned by two
patterns — measured on this machine as **4 real files producing 8 entries**, which doubled Stage-5
analysis work in the headless path. The GUI folder mode was unaffected.

The passes are kept (they are what supports both extensions in either case on a case-sensitive
platform) and the first occurrence of each real file is retained, identified by
`os.path.normcase(os.path.abspath(...))` rather than by filename — so two directories holding the
same basename stay distinct, and on a case-sensitive platform genuinely distinct differently-cased
filenames also stay distinct. MP4/MKV support, mixed-case files, first-seen ordering and the
existing `ValueError` on an empty directory are all preserved. 4 → 8 before, 4 → 4 after.

### Maintenance — 2026-10-07 (post-C3 contract/documentation consistency)

**Prose only. No runtime behaviour, constant, signature, wiring, assertion, UI copy or cache version
changed.** C3-R1B-b and L2 V1 made a number of *current-state* statements false, and those statements
live in the path-scoped `.claude/rules/*.md` files that `CLAUDE.md` directs every future session to
read before modifying matching source. That made the drift compounding rather than cosmetic.

Corrected to current truth — rendering is **2–4** candidates, sequential, canonical ascending index,
one lifecycle spanning the whole selection, and only `CANDIDATE_LOCAL` continues:

- `.claude/rules/variant-lab.md` — the C2-section line claiming "batch rendering is still deferred
  and a split guard asserts none of that machinery exists" (batch rendering ships; the guard now
  keeps `variant_lab.py` / `creative_recipe.py` free of batch concepts, which is the half that was
  always load-bearing), and the C3-R1A lifecycle line "the whole two-candidate batch".
- `.claude/rules/fork-package.md` — the `render_batch.py` module row, which described "rendering
  exactly two compared candidates" and a selection contract of "exactly two". Now states MIN 2 /
  MAX 4, that over-long selections are refused rather than truncated, that `RENDER_SELECTION_SIZE`
  no longer exists, and that the batch-level cause domain and terminal-candidate restriction are
  R1B-b additions — while keeping the module's purity contract intact.
- `.claude/rules/gui-integration.md` — the wrapper bullet, the Freestyle-tuple cardinality, and the
  routing-table row "C3-R0 render-two-candidates seam".
- `.claude/rules/input-gate.md` — `render_selected_variants_guarded` described as the "two-candidate
  batch wrapper". The gate contract itself is unchanged: every selected candidate still reaches the
  same authoritative live-source gate.
- `.claude/rules/progress-events.md` — the prefix was documented as `Rendering candidate {n} / 2`;
  it is `Rendering candidate {position} / {request.count}`. No batch-owned `ProgressEvent` was
  introduced and no stage/phase/counter semantics changed.
- `.claude/rules/render-worker.md` — the lifecycle example "the WHOLE two-candidate C3-R0 batch ->
  spanning BOTH candidates".
- `.claude/rules/creative-presets.md` — "Still not implemented … L2 stage caching" was globally
  false. The passage now separates **implemented elsewhere but banned in this surface** (L2 Stage-3
  caching and Freestyle V1 both ship; a preset still acquires neither a cache concept nor a section
  mechanism) from **genuinely unimplemented** (source groups, per-section Micro Cuts, a per-section
  Variation Seed, a per-section-instance editor, a shortlisted second Qwen pass, a content-aware
  Director) — recorded so the bans have a stated reason, explicitly **not** as a roadmap.
- `src/gui.py` — six comment/docstring sites. **Comments and docstrings only**: the executable AST is
  proven equivalent to the base after stripping docstring expressions.
- `tests/test_creative_presets.py` — one comment. `_STILL_SPECULATIVE`,
  `_FREESTYLE_ACCEPTED_IN_GUI`, `_FREESTYLE_STILL_SPECULATIVE`, `_PRESETS_ONLY_SPECULATIVE` and
  `_DIRECTOR_ACCEPTED_IN_GUI` are untouched; the file's **full AST is byte-identical**.

**Correct "two" counts were deliberately retained** — they describe architecture, not candidate
cardinality: `gui.py` has exactly **two** `RenderLifecycle` construction sites, there are exactly
**two** mutex-owning render wrappers, and `variant_batch_state` has exactly **two** readers. No blind
`two → 2–4` replacement was performed; every edit was reviewed in context.

**Historical entries below are intentionally preserved.** Phase 2A saying live Qwen progress was not
yet implemented, and C3 V1 saying batch rendering was deferred, were both true when written. History
is not rewritten to look current; this entry records the cleanup instead.

### Added — 2026-10-08 (Content-aware AI Director V2 — semantic intent IR + narrow media adaptation)

**The model no longer speaks BeatSync's control vocabulary.** Director V1 asked the local model for
the six internal creative controls directly. That shape was measured three ways — dense six values,
sparse absolute values, and direction+strength on the internal names — and all three failed the same
ordinary sentence, *"Keep scene choice relatively even across sections."*, which a 2B, a 4B and an 8B
model all read as neutral. The problem was not model size: the language task included "which way
does the `energy_response` slider move?", which the user never said.

V2 replaces that with a **user-semantic intent IR**. The model classifies meaning on six ordinary
editing dimensions — `cut_pacing` (sparser/denser), `impact_accents` (fewer/more), `scene_reading`
(visual/semantic), `section_reactivity` (steadier/responsive), `motion_preference` (calmer/dynamic),
`source_variety` (reuse/diverse) — each with a `direction` and a `strength` of 1..100, and
deterministic code alone maps those onto the controls. None of the six execution control names may
appear in the model-facing prompt or schema; an import-time assertion and a permanent test both
enforce it, and both surfaces are hash-pinned to the exact bytes the evidence was produced against.

There is deliberately **no `0` strength and no `neutral` direction**: both are indistinguishable from
omitting the dimension, and the sparse experiment measured exactly that failure — a model given a
neutral option emitted no-op entries, and a model asked for absolute values collapsed every downward
request onto 50. Omission is the only way to say "leave this alone", and it maps to exactly 50.

**Selected intent model: `Qwen3-4B-Instruct-2507 Q8_0`, text-only.** On the frozen matrices it
reached 100 % strict-schema validity, 14/14 concepts, 10/10 media-control concepts, 0 wrong-direction
answers, 6/6 on the energy cluster that defeated every earlier contract, and **12/12 on an unseen
holdout**, deterministically, at a ~3.5 s median one-shot cost. It is a **third** model asset, not a
replacement: Stage 5 keeps `Qwen3VL-2B-Instruct-Q8_0.gguf` plus its `mmproj` for media semantics, and
there is **no fallback** from the Director to that 2B model — it was measured against this contract
and failed it (8/14 overall, 0/4 downward requests), so a missing Director model reports honestly and
points at the installer instead.

**One narrow media-aware adaptation, and only one.** A prototype proposed four; a value study put all
four through the real Stage-6 planner on real candidate pools across eight fixed Variation Seeds, and
three earned DROP despite being pure, bounded, deterministic and cheap:

- *Semantic Emphasis* is **execution-inert** without Qwen coverage — at 0 % coverage attenuation
  changed no render outcome at all (measured character effect exactly 0.00000);
- *Energy Response* **changed the sign** of its benefit with the direction of the request;
- *Motion Bias* discarded 71–78 % of the requested character for a sign-inconsistent ±0.1 % score
  change, i.e. noise.

Only **Source Diversity** survived, and only upward. On a prepared library with ~4 effective sources
a strong diversity request cannot buy a single extra source, so the extra reuse pressure is pure
score cost; relaxing it recovered **+1.2353 %** mean legacy score across 8/8 seeds with the
unique-source count identical every time. **It is a trade-off, not a free win** —
`adjacent_source_repeats` rose from 19.6 to 28.8 mean — and the proposal says both halves in one
breath. Reuse-direction requests are **never** attenuated: on the same library that was consistently
harmful (4/4 cases, mean −0.4352 %), while diverse-direction requests were consistently positive
(7/7, mean +0.8653 %). The support function measures leverage and is direction-agnostic; the value is
directional, so the direction gate lives in the adapter.

**The model never receives media.** It gets the instruction, the system prompt and the schema — no
frames, filenames, Stage-5 records, counts or summary — so there is no media prompt-injection
surface. The one media fact is read by deterministic local code *after* the model has answered. A
test inspects the real argv and asserts no summary field, no measured number and no `mmproj`/image
argument appears.

**Media adaptation requires a current, fully prepared scan, or it does not happen.** The gate is: a
recorded scan exists; the **live** preparation declaration still describes it (the same cheap check
Analyze uses, because a queued widget change can leave the stored scan behind the screen); the
library is fully prepared; and the summary is provable. Anything else produces the ordinary
semantic-IR proposal with a truthful note naming the reason — *not* a failure, and never described as
a fallback. A partial scan's aggregate describes a subset, and the value evidence was produced on
complete pools.

**Preparation gained one bounded aggregate, at no extra cost.** The scan already loads every reusable
record to decide PREPARED, so it now also counts that source's usable moments there — the one
authorized record-read point — and `build_scan_result` turns the result into the frozen four-scalar
`PreparedMediaSummary` (`candidate_moments`, `effective_sources`, `top_source_share`,
`median_moments_per_source`), with `effective_sources` as inverse-HHI over candidate shares. The
counting sits inside the already-decided PREPARED branch and is exception-guarded, so no shape of
stored candidate data can change a classification verdict; `_cache_entry_is_complete` remains the one
completion rule. No second cache read, no retained record, no write path, and a field count
independent of library size — the record rides in `gr.State`, which deep-copies.

**`DirectorProposal` now carries BASE and FINAL.** BASE is what the intent resolved to and is
provenance only; FINAL is what Apply writes and what the preset label derives from. Both share the
one minted Variation Seed, so the media step cannot look like it re-rolled the clip selection, and
the read-out visibly separates the Director's own reading from the deterministic adjustment —
the model is never credited with the media change.

Unchanged: Generate still writes **zero** execution widgets and Apply is still the one Director
writer of the Variation Seed, the six sliders and the preset label; Create Music Video remains the
only thing that renders; Apply is still not stale-gated, including across a later media change;
`CreativeRecipe.from_mapping` is reused unmodified as the trust boundary for BASE and FINAL alike;
the seed is still minted last, and an invalid answer consumes none. Stage 5 is untouched —
`CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`, and the worker, prompt, schema and request format are
unmodified. llama.cpp stays at build `b9842`; the installer now also requires `llama-completion.exe`
by name, which shipped in the same archive but was never verified, so an otherwise "ready" install
could have had no Director runtime at all.

### Added — 2026-10-07 (C3-R1B-b — render 2–4 candidates, continue past a local failure)

**C3-R1B is complete.** C3-R1B-a made every failure cause truthfully typed and deliberately spent
none of it; R1B-b spends it on exactly two changes and nothing else.

```
selection      exactly 2            ->  a bounded range, MIN 2 .. MAX 4
on failure     every class stops    ->  CANDIDATE_LOCAL continues; everything else still stops
```

- **`RENDER_SELECTION_MIN = 2` / `RENDER_SELECTION_MAX = 4`**, and the exact-size
  `RENDER_SELECTION_SIZE` constant is **gone** with no alias (every consumer was internal to
  `render_batch.py`, verified across `src/` and `tests/`; an ambiguous alias beside a range is how a
  future caller silently reintroduces the two-candidate rule). **Four is a product contract, not a
  tunable:** past candidate 1 the cost is linear with no economy of scale — the only real shared
  saving, the ~15.7 s of Stage 1-3, is already fully banked at candidate 2 by the L2 process cache —
  and C3-R1B/P0 measured ≈67 s for the first candidate and ≈51 s for each subsequent one on the
  NVENC path at ~150 clips, materially more on the serial ProRes path. That puts four in the same
  order as the single render a user already accepts. Still unrelated to
  `CANDIDATE_COUNT_MAX = 12`, which is comparison legibility and costs a millisecond.
- **An over-long selection is refused, never truncated.** Five ticks answers `()`. Silently
  rendering the first four would deliver something the user did not ask for, after they committed to
  the wait. Canonical ascending index order is unchanged — `[3,1,0,2]` renders as `(0,1,2,3)`.
- **The continuation matrix, and the whole of it:**

  ```
  SUCCESS           -> render the next selected candidate
  CANDIDATE_LOCAL   -> record the failure, then CONTINUE
  SHARED_FATAL      -> STOP; remaining candidates NOT ATTEMPTED
  UNKNOWN_FATAL     -> STOP; remaining candidates NOT ATTEMPTED
  CANCELLED         -> STOP; remaining candidates NOT ATTEMPTED
  ```

  Three properties make it safe rather than merely implemented. The decision is read off
  `candidate_outcome.outcome_kind` — **never** the raw `candidate_kind` local — so the *model*
  decides what an unclassified `None` means (`SUCCESS` with a durable path, `UNKNOWN_FATAL`
  without); branching on the raw value would let an unclassified failure continue as "not
  CANDIDATE_LOCAL, therefore carry on". The **loop-head cancellation check still comes first in
  every iteration**, so a `continue` cannot outrun a Stop: a Cancel arriving in the gap after a local
  failure leaves the remaining candidates NOT ATTEMPTED. And nothing is retried or reclassified.
- **"A candidate failed" and "the batch stopped" became independent facts**, which reshaped the
  outcome model. `stopped_on_failure` is **removed** — a boolean meaning "something failed, therefore
  we stopped" cannot be true once a local failure is continued past. `_failed_candidate()` became
  `_terminal_candidate()`, which names only the **last attempted** candidate, only if it genuinely
  failed, and only when work was actually left unrun; a failure on the *final* selected candidate
  stopped nothing and now reads as a count. New single-source `failed` and `cancelled_count`
  properties exclude successes and (for `failed`) cancellations, so a user's Stop is never counted as
  their render breaking.
- **A completed batch has `outcome_kind is None`, even carrying local failures** — deliberately not
  batch-`CANDIDATE_LOCAL`, because that cause belongs to the candidate and the batch carried out its
  policy to the end. Only `CANCELLED`, `SHARED_FATAL` and `UNKNOWN_FATAL` are batch-terminal, and the
  fatal branch additionally requires `len(outcomes) < request.count`.
- **Truthful N-candidate summaries.** `4 / 4 succeeded` · `3 / 4 succeeded; 1 failed` ·
  `2 / 4 succeeded; 2 failed` · `1 / 4 succeeded; 1 failed; stopped on candidate 3` ·
  `1 / 4 succeeded; cancelled during candidate 2` ·
  `2 / 4 succeeded; batch cancelled before candidate 3`. A local failure the batch continued past is
  a **count**, never "stopped on candidate N". One block per **attempted** candidate, and no record
  is fabricated for one that was not attempted; a completed batch never claims any candidate went
  unattempted.
- **Earlier durable outputs are never rolled back** — whatever happens later, local or fatal or
  cancelled. No cleanup path deletes a promoted output, and a later local failure never blanks an
  earlier successful preview.
- **UI: range copy plus one widened textbox.** `INFO_RENDER_CANDIDATES`,
  `PLACEHOLDER_RENDER_BATCH_SUMMARY` and `INFO_RENDER_SELECTED` now say "2 to 4", state that renders
  run in list order, and explain the continuation policy in the user's own terms ("specific to that
  candidate's own settings" vs "affects every candidate equally") without exposing a single internal
  class name. The summary textbox went `lines=10, max_lines=20` → `12 / 28`, because a four-candidate
  summary measures 20–21 lines. **No gallery, no new component, no layout redesign**, and the
  selector is still empty by default — quadrupling the possible commitment makes that more
  load-bearing, not less.
- **The top-level lifecycle terminal state is read from selection exhaustion, not from the last
  candidate** (corrected post-review). The derivation is `CANCELLED` when a cancellation won,
  `FINISHED` when `len(outcomes) == request.count`, `FAILED` otherwise. A first draft kept R1A's
  `session_state[RENDER_OUTCOME_KEY]` read — whatever the final candidate happened to write — which
  was sufficient while every candidate failure stopped the batch and became order-dependent the
  moment `CANDIDATE_LOCAL` continued: `CANDIDATE_LOCAL, SUCCESS` gave `FINISHED` while
  `SUCCESS, CANDIDATE_LOCAL` gave `FAILED`, for two batches with identical counts, identical
  headline and identical batch cause. `RenderLifecycle` belongs to the render **event**, so its
  state answers "what happened to the event", not "did every candidate succeed" — a `SHARED_FATAL`
  on the *final* selected candidate therefore leaves the lifecycle `FINISHED` while the candidate
  keeps its own fatal cause and the summary still reports the failure. The mirrored pair is pinned
  as a regression against the **actual** shared lifecycle the wrapper installed, and the defensive
  `is_terminal()` backstop and abandonment handling are unchanged.
- **A continued-past local failure is never named as the stop cause** (corrected post-review).
  `_terminal_candidate()` still carried one pre-R1B-b assumption: it read *any* non-success,
  non-cancelled last candidate as the one that ended the run — i.e. it read *failure* as *terminal*,
  which since R1B-b are different things. The visible consequence was one false phrase:

  ```
  CANDIDATE_LOCAL, then Cancel before candidate 2
    "0 / 4 succeeded; stopped on candidate 1; batch cancelled"       <- candidate 1 stopped nothing
    "0 / 4 succeeded; 1 failed; batch cancelled before candidate 2"  <- true
  ```

  Terminality is now a **class** test against the new `_CANDIDATE_TERMINAL_CAUSES` —
  `_BATCH_TERMINAL_CAUSES` minus `CANCELLED`, so exactly `SHARED_FATAL` and `UNKNOWN_FATAL`, the two
  the continuation policy actually stops on. `CANDIDATE_LOCAL` is counted as a failure and never
  blamed; `CANCELLED` keeps its own wording; `SUCCESS` stopped nothing. The **dual-truth case is
  unchanged** — a genuine fatal plus a cancellation still reports both, because that fatal really
  was terminal — and the fix is a definition change rather than a formatter special case, so no
  candidate index is treated specially anywhere.
- **The batch-level cause domain is now enforced, not just documented.**
  `RenderBatchOutcome.__post_init__` rejects `SUCCESS` and `CANDIDATE_LOCAL` with `ValueError`:
  `SUCCESS` is a candidate outcome (a finished batch reports counts and has no terminal cause —
  that is what `None` means), and `CANDIDATE_LOCAL` belongs to the candidate the batch *continued
  past*, so it cannot have terminated the run. Allowed explicit values are `None`, `CANCELLED`,
  `SHARED_FATAL`, `UNKNOWN_FATAL`. The sound cancelled-candidate derivation is preserved and the
  model still derives no fatal cause from candidate records.
- **Cancellation and the taxonomy are untouched.** `render_worker.py` and `audio_mixdown.py` are
  byte-identical: one lifecycle already spanned the whole batch, however many candidates, which is
  the clearest evidence the C3-R1A design generalised. `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION`
  and `L2_CACHE_VERSION` are unchanged — rendering four candidates is the same candidate render
  repeated sequentially.
- **Verification.** Controlled 4-candidate acceptance over the real handler body, all eight §35
  scenarios PASS with measured execution call order (no media, no portable runtime, nothing in
  `output/` or the production cache touched). Eight mutations demonstrated to fail the permanent
  guards and restored byte-exactly: MAX→5, exact-two cardinality restored, continuation removed,
  continue-after-SHARED_FATAL, continue-after-UNKNOWN_FATAL, continue-after-CANCELLED, loop-head
  cancellation check removed, and the summary blaming the first failure for stopping the batch.

### Changed — 2026-10-07 (C3-R1B-a — truthful failure classification)

**The render path now names why it failed, and that is *all* it does.** C3-R1A shipped the
five-member `RenderOutcomeKind` vocabulary with producers that were not truthful. R1B-a fixes the
foundation and changes **no** continuation policy:

```
before R1B-a    failure -> stop
after  R1B-a    failure -> record the truthful cause -> still stop
```

That zero-policy-change property is load-bearing. `RENDER_SELECTION_SIZE` is still **2**, there is no
3+ candidate rendering, and there is no continue-after-`CANDIDATE_LOCAL`. Those are **C3-R1B-b**,
separately authorized.

What R1A actually did, and why each part was wrong:

```
every AudioMixError   -> CANDIDATE_LOCAL   wrong for BOTH causes that reach that handler
eleven producers      -> key left None     -> silently derived UNKNOWN_FATAL downstream
the batch loop        -> CANCELLED or None -> every other proven class discarded
SHARED_FATAL          -> no producer at all
```

- **`src/audio_mixdown.py` gained four local cause types under the retained `AudioMixError` base** —
  `AudioProbeError`, `AudioMixInputError`, `AudioMixPlanError`, `AudioMixExecutionError`. Every
  pre-existing `except audio_mixdown.AudioMixError` still catches exactly what it caught before, and
  every message is unchanged character for character: `tests/test_audio_mixdown.py`'s 125 cases pass
  with **no assertion changed**. Each of the three narrow wraps forwards its cause's text with
  `str(exc)` rather than rewriting it, so the *type* changes and the reason the user reads does not
  — a test asserts that for all three, and asserts byte-equality between the generated-master
  wrapper and its `AudioProbeError` cause. (A first draft prefixed that one with "Could not verify
  the mixed master: ", which really was a user-facing change in a classification-only milestone.)
- **The cause types deliberately carry no `RenderOutcomeKind`.** The module cannot know whether it
  is running a single render, candidate 1 or candidate 4, so it cannot answer "would every remaining
  candidate fail the same way?". The measured reason this matters: `probe_duration` has four
  production call sites, and the *same four* internal failures resolve to **three different**
  classes depending on the caller. A test asserts the module's executable code never references
  `RenderOutcomeKind`, `RENDER_OUTCOME_KEY` or `session_state`.
- **`SHARED_FATAL` has five real producers**, each proven by showing nothing the candidate resolves
  is an input to the outcome: the live source-gate refusal, the six primary audio/video selection
  failures, the voice preflight, `AudioMixPlanError`, and an `errno.EXDEV` durable promotion.
- **Two R1A classifications were corrected, in opposite directions.** A voice `PlacementFailure` is
  **SHARED**, not candidate-local: `plan_voice_placements` reads only `avoid_drops`,
  `start_delay_seconds` and `min_gap_seconds`, all batch-frozen, while the one candidate-varied
  field reaches the duck floor *after* placement succeeded — and it is unreachable with an empty
  voice selection. Conversely the **SFX preflight is CANDIDATE_LOCAL** even though its root and roles
  are frozen, because *reachability* is candidate state: `smart_mix_active` requires
  `sfx_amount > 0`. Measured through the real resolvers — root master 92, spread 100, full range,
  four candidates — amounts resolve **87 / 87 / 0 / 79**, so with a broken SFX library candidate 3
  renders fine. `SmartMixStructureError` is CANDIDATE_LOCAL for the identical reason; shared
  `beat_info` data does not make a shared *failure* when a candidate can avoid the operation.
- **Every wrapping catch is narrow — `except AudioProbeError`, never `AudioMixError` or `Exception`.**
  A wide catch would swallow a `RenderCancelled` from inside `probe_duration` and report a user's
  Stop as a broken voice clip or a failed mix. `RenderCancelled` is still not an `AudioMixError`,
  `build_mixed_master` keeps both cleanup clauses, and the original probe cause is chained with
  `from exc` so nothing downstream has to read a message.
- **`_promote_output_no_replace()` returns `(message, outcome_kind)`.** Its three branches were
  already structurally separate, so this added no logic: `FileExistsError` → `CANDIDATE_LOCAL` (the
  destination name carries the candidate index and master), `errno.EXDEV` → `SHARED_FATAL`
  (`session_dir` and `output/` are process-global), any other `OSError` → `UNKNOWN_FATAL`. One
  atomic no-replace `os.rename`, no deletion, no copy fallback, no `exists()` pre-check — all
  unchanged — and **the SUCCESS commit point did not move.**
- **The batch loop preserves the full class** instead of only `CANCELLED`, taken solely from
  `RENDER_OUTCOME_KEY` and passed to `RenderCandidateOutcome.outcome_kind` **unchanged**, with no
  filter in between. `durable` remains the sole success authority, and an explicit
  `success`/`outcome_kind` contradiction **raises** — `RenderCandidateOutcome.__post_init__` owns
  that validation and the batch must not sanitize it. A first draft wrapped the hand-off in an
  agreement test and substituted `None` on disagreement; that defeated the invariant the model
  exists to enforce and then published a derived class the producer never named, which would have
  hidden a broken producer contract from C3-R1B-b. Conservative derivation applies only when the key
  is `None`. **The stop condition is unchanged and class-blind.**
- **The defensive original-music fallback probe is `UNKNOWN_FATAL`, unconditionally.** It is
  unreachable in current production — `analyze_beats_auto` guards `y.size == 0` and then sets
  `audio_duration = len(y) / sr`, strictly positive — so this is a frozen forward-safe decision
  rather than a measurement. It was hoisted out of the `build_mixed_master(...)` argument list only
  because an exception inside an argument expression cannot be caught separately from the call it
  feeds; the short-circuit is reproduced exactly and the call still takes no `lifecycle`.
- **No class is ever derived from prose.** Every assignment to `RENDER_OUTCOME_KEY` names an enum
  member explicitly, and a test asserts `gui.py` contains no status-text inspection.
- **`tests/test_render_failure_classification.py` is new** (70 cases) and pins the whole
  producer → class matrix, including the root-92 reachability fixture, the real `_process_video_impl`
  executed end to end for the collision / shared-input / success rows, and the real batch loop
  executed to prove each class survives into `RenderCandidateOutcome` **and** that every class still
  stops the batch. Six mutations were demonstrated to fail the permanent guards and restored
  byte-exactly (SHA-256 verified).
- **Not modified:** `render_worker.py` (five members were always enough; producers were what was
  missing), `smart_mix.py`, `audio_mix.py`, `video_processor.py`, `ffmpeg_processing.py`,
  `auto_mode/*`, `video_analysis.py`, `ui_content.py`, `stage_cache.py`, `variant_batch.py`.
  `render_batch.py` received a docstring correction only — no executable change.
  `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION` and `L2_CACHE_VERSION` are untouched, and
  cancellation behaviour is byte-for-byte unchanged.

### Added — 2026-10-07 (C3-R1A — render lifecycle and cancellation)

**A render can be stopped.** One **Cancel Active Render** button stops whichever render is running —
an ordinary Create Music Video click or the whole two-candidate C3-R0 batch — at the next safe point.

```
BOUNDARY_ONLY_CANCEL
  FFmpeg-class subprocess   terminated, graced, killed if needed, REAPED -> then RenderCancelled
  in-flight Stage 5 / Qwen  never hard-killed; effective at the next boundary after it returns
```

- **`src/beatsync_fork/render_worker.py` is new and stdlib-only** (CLAUDE.md's hard rule):
  `RenderCancelled`, `RenderOutcomeKind`, `RenderLifecycleState` with a monotonic transition table,
  the frozen `RenderOutcome`, and `RenderLifecycle` — one cancellation `threading.Event`, one
  lock-protected state machine, one opaque invocation id. It owns no Gradio object, path, `Popen`,
  FFmpeg command, Qwen process, stage data, media identity or cache identity, and keeps no registry
  of lifecycles.
- **`RenderCancelled` is an ordinary `Exception`, deliberately — not `BaseException`.** So every
  broad `except Exception` on the render path names it explicitly *before* the generic handler:
  nine such sites across `ffmpeg_processing.py`, `audio_mixdown.py`, `video_processor.py` and
  `gui.py`, pinned by count and by handler order. Two of them re-raise with **no event at all**,
  because a cancellation must not be narrated as `Final assembly failed`.
- **`lifecycle=None` is byte-for-byte today's behaviour.** The parameter is appended **last with a
  default** at all fourteen seams, and both media runners open with
  `if lifecycle is None: return subprocess.run(..., timeout=timeout)` as their first statement — so
  the headless CLI, `video_processor.main` and every existing caller are unaffected, and no
  positional argument moved. Measured, not asserted: with no lifecycle, no `Popen` is created at all.
- **Quiescence is proven before cancellation propagates.** Each cancellable runner polls its own
  child and terminates → graces → kills → **reaps** it, raising only once the reap returned.
  `create_music_video`'s clip loop calls `executor.shutdown(wait=True, cancel_futures=True)`
  explicitly, stores the exception, `break`s, and re-raises only **after** the `with` block has
  exited. `cancel_futures=True` is independently load-bearing: without it every already-submitted
  future would still run, i.e. the whole extraction continuing after Stop.
- **Exactly ONE lifecycle per top-level render event**, constructed by a mutex-owning wrapper — the
  batch's single lifecycle spans **both** candidates, and the candidate boundary is checked first
  thing each iteration. Terminal-marking happens only in those two wrappers, never in
  `process_video.worker()`, which the batch calls once per candidate: `_transition` silently no-ops
  once terminal, so marking there would freeze the shared lifecycle on candidate 1's outcome.
- **Only a plain string crosses into Gradio.** `render_invocation_state` is a `gr.State('')` holding
  an opaque id; the live object stays in a server-side capacity-one slot holding
  `(invocation_id, lifecycle)` or `None`. `_clear_active_render` clears only if the slot still names
  that id, so a slow abandoned finalizer cannot unregister a newer render.
- **Cancel owns its own concurrency lane** (`CANCEL_CONCURRENCY_ID`), never `RENDER_CONCURRENCY_ID`
  and never Gradio's `cancels=`. The first would queue Cancel behind the render it must signal; the
  second kills the event while leaving the daemon worker alive — the hazard C3-R0 bounded itself to
  two renders to avoid. The handler never takes `_RENDER_LOCK` and touches nothing but the slot.
- **Abandonment is still not cancellation.** `process_video`'s finalizer joins its worker with no
  timeout and no kill, exactly as before; no `finally` anywhere requests a cancellation.
- **The durable `os.rename` promotion is the ONE success commit point**, and the ProRes preview is
  post-commit convenience work. Three explicit branches: `lifecycle=None` is today's blocking call;
  an already-pending cancellation never starts a preview child at all; and a cancellation arriving
  **while the preview child is running** terminates, graces, kills if needed and reaps it through
  `ffmpeg_processing.run_cancellable_media_command` — the one new public entry point to the existing
  reviewed runner, rather than a second copy of its process logic. Either way the partial preview is
  never selected, `preview_path` falls back to the durable `.mov`, and the outcome stays `SUCCESS`:
  the user owns that file. This is the only place a `RenderCancelled` may be swallowed, and the
  exemption is located structurally rather than by a token. A genuine `TimeoutExpired` is not caught
  on either path — a stuck encode is a different fact from a user pressing Stop.
- **A cancelled candidate is reported as cancelled, never as a failure — and so is a cancelled
  *batch*.** `RenderCandidateOutcome.outcome_kind` carries the per-candidate typed cause, validated
  against `success` in `__post_init__`, and the batch reads it from `session_state` rather than
  inferring it from `durable` or from status prose. `RenderBatchOutcome.outcome_kind` carries the
  **batch-level** cause, which exists because one shape is expressible nowhere else: a cancellation
  landing in the gap *between* candidates leaves candidate 1 genuinely SUCCESS and candidate 2 never
  attempted, so no candidate record is cancelled and none is fabricated. That now reads
  `1 / 2 succeeded; batch cancelled before candidate 2` plus `Batch CANCELLED.` / `1 candidate not
  attempted.` / `Earlier successful output was kept.` — rather than blaming a candidate that had just
  succeeded. `SHARED_FATAL` exists in the vocabulary with no producer: telling shared from
  candidate-local apart is what continue-after-failure needs, and that is **C3-R1B**.
- **`RENDER_SELECTION_SIZE` is still 2, and no cache, schema or version constant moved** —
  `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION` and `L2_CACHE_VERSION` are untouched.
  `video_analysis.py`, `stage5_qwen_scene_worker.py` and `qwen_progress.py` are untouched **by
  contract**, which is what makes "an in-flight Qwen call is never hard-killed" structural.
- `tests/test_render_worker.py` and `tests/test_render_cancellation.py` are new, and
  `.claude/rules/render-worker.md` is the durable contract. **Eight** deliberate mutations were each
  confirmed to fail them: dropping `cancel_futures=True`, moving the re-raise inside the `with`,
  making the slot clear unconditional, letting the preview downgrade `SUCCESS`, moving the
  post-Stage-5 boundary inside that stage's broad `except`, removing the batch-level cancelled truth
  from the model, dropping it at the `gui.py` call site, and reverting the preview to a blocking
  `subprocess.run`.
- **Controlled Windows runtime acceptance** (task scratch only; no production media, no Stage-5 cache
  mutation, no user output): 4 simultaneously-live real children all reaped and gone from `tasklist`
  at a 0.027 s max cancel-to-quiescent latency; a stale invocation id cannot reach a newer render; the
  C3 boundary case shows a candidate-2 execution call count of 0 with a truthful summary; a
  Stage-5 stub blocked, was cancelled mid-call, ran to **natural completion**, and the boundary
  immediately after it fired with no fallback-sampling laundering and no Stage 6; and a real preview
  child was confirmed alive via `tasklist`, cancelled, reaped and gone in 0.096 s while the durable
  `.mov` survived and the outcome stayed `SUCCESS`.

### Performance — 2026-10-04 (L2 Stage Caching V1 — process-local post-Stage-3 reuse)

**A repeated render of the same track reuses the audio front end and Stages 1–3 instead of
recomputing them.** One entry, in this process only, holding the five facts the pipeline still needs
after Stage 3.

```
HIT   skip librosa.load, normalize, HPSS, detect_master_beat_grid,
      analyze_wave_features, analyze_sections  ->  continue into the existing Stage-4 code
MISS  run the pre-L2 body verbatim             ->  publish, only after Stage 3 succeeded
```

- **The boundary is measured, not chosen for tidiness.** Medians on the real Windows track
  (`Nero - Satisfy.mp3`): audio load 0.502 s, normalize 0.001 s, **HPSS 12.453 s**, Stage 1 0.487 s,
  Stage 2 0.993 s, Stage 3 1.299 s — **~15.735 s** of reusable work, serialising to ~344 KB.
  **Stage 4 measured 0.0076 s and is deliberately NOT cached**: that does not pay for the extra key,
  the extra correctness surface or the resolved-Freestyle key it would need. **Stage 6 measured
  2.930 s and is also uncached**, because every target scenario changes something it reads.
- **Cached: `audio_duration`, `beat_times`, `tempo`, `features`, `sections`.** The expensive raw
  front-end arrays (`y`, `y_harmonic`, `y_percussive`, `beat_frames`, `onset_env`) are deliberately
  *not* retained — nothing after Stage 3 reads them, so keeping them merely because they were
  expensive would hold megabytes of audio per entry for no reuse at all.
- **One entry, process-local, zero persistence.** No LRU, no size knob, no dictionary of historical
  keys, no cache directory, no cache JSON, no pickle artifact. `stage_cache.py` contains exactly one
  `open()` and it is `"rb"`, for the fingerprint; `os.makedirs`, `json.dump`, `pickle`, `np.save` and
  `tempfile` are banned by test. A process restart starts cold, and a hit is never described as a
  disk or persistent cache hit.
- **The key is `(L2_CACHE_VERSION, track identity, effective audio window, Stage 1–3 analysis
  config, use_gpu_requested)`** — frozen and structured, never a concatenated string. Track identity
  is absolute path + `st_size` + `st_mtime_ns` + a bounded BLAKE2b content fingerprint, the accepted
  D2 geometry re-expressed rather than imported (`stage_cache.py` must not import
  `video_analysis.py`). It is the same class of **accidental** stale-result prevention as Stage 5 —
  not adversarial, and no claim is made about bytes outside a large file's sampled windows.
  Unprovable identity means the cache is simply unavailable, never a weak key.
- **`L2_CACHE_VERSION = "l2_stage3_v1"` is a new, independent constant.**
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`. **No cache migration, no invalidation, no payload
  change** anywhere.
- **No creative state may enter the Stage-3 key**, and that is the whole value: the Variation Seed,
  all six controls, every preset and every Freestyle declaration reach one identical key, so a
  creative change reuses the ~15.7 s and recomputes only from Stage 4 onward. The video source list,
  Stage-5 state, Qwen config, encoder, FPS, voice/SFX settings and output path are absent too — a
  source-library change cannot invalidate cached audio analysis, and an audio change cannot
  invalidate Stage-5 media records.
- **The Stage 1–3 config identity is derived from the real source and guarded against drift.** The
  seven participating fields (`sr`, `hop_length`, `n_fft`, `wave_smooth_beats`, `phrase_beats`,
  `bar_beats`, `section_min_seconds`) are exactly what the front end and Stages 1–3 read today; a
  permanent test re-derives that set from the live source on every run and fails if a field is read
  that the key does not cover. The reverse is asserted too: no Stage-4-only field participates.
- **`use_gpu` separation is conservative, not a parity claim.** Stage 2 has a CPU/CuPy branch and this
  repository has no byte-exact parity contract, so the two *requested* modes key separately. No claim
  is made that their outputs differ.
- **Defensive deep copying on both sides is load-bearing.** `put` stores a `copy.deepcopy` and `get`
  returns one, so the stored graph is never reachable from any caller and a downstream mutation of a
  cache-hit bundle cannot corrupt a later hit. This does not rely on downstream code being careful.
- **Fail open on an ordinary failure; propagate memory exhaustion.** Key creation, lookup, copying
  and an ordinary store failure each degrade to the existing uncached path — no cache, a miss and
  recompute, or reuse lost with the current render continuing. **`MemoryError` is explicitly
  re-raised ahead of that handler**, because turning a failed allocation into a cache miss would
  immediately start the ~15.7 s uncached audio-analysis path at the moment the process has least
  memory for it. `KeyboardInterrupt` and `SystemExit` are not caught at all, being `BaseException`
  subclasses; there is no `BaseException` catch and no bare `except`.

  *(Corrected in R2. R1's code and prose both claimed "`Exception` only, so a `MemoryError` still
  propagates", which is false in Python: `MemoryError` subclasses `Exception`, so the single
  `except Exception` swallowed it. The code now matches the stated intent and the prose matches the
  code; a permanent test asserts the subclass relationship mechanically, another pins the handler
  order, and three behavioural tests prove `MemoryError` propagates at each seam with no Stage 1–3
  recomputation.)*

  Nothing is published before Stage 3 succeeds, so a failed stage cannot poison the entry, and an
  entry stored before a later `MemoryError` stays reusable. Existing `librosa.load`, Stage 1/2/3 and
  empty-audio failures keep their current behaviour exactly.
- **The miss path is the pre-L2 body, proven rather than asserted.** A differential test extracts
  `analyze_beats_auto` from both the authorized base and this revision, runs them against identical
  deterministic stubs and requires array-exact equality of `beat_times`, `tempo`, `features`,
  `sections`, `selected_beats`, `selection_info` and `audio_visual_profile`. Separately, the 39
  statements of the miss branch are byte-identical to base apart from indentation.
- **Truthful progress, with no file touched.** `progress.py`, `progress_view.py` and `gui.py` are
  unmodified. A hit emits a START/END pair for each skipped stage ("Reusing cached beat grid" /
  "… energy and rhythm features" / "… musical sections"), every event carrying `cached=True` and
  **no `elapsed_seconds`** — there is no fresh Stage 1–3 timing and inventing one would be a
  fabricated measurement. The metadata is the cached facts themselves, and the legacy
  `progress_callback` still walks stages 1–3 so an old consumer does not jump to Stage 4.
- **C3 gains reuse with no C3 code change.** `variant_lab.py`, `variant_batch.py`,
  `render_batch.py` and the GUI's C3 orchestration are untouched. Candidate 1 populates the entry and
  candidate 2 hits it because the cache is process-local rather than call-local; there is deliberately
  no explicit "share with candidate 2" mechanism.
- **Windows performance acceptance has NOT been run.** The ~15.7 s figure is the pre-implementation
  measurement that justified the boundary, not a post-implementation claim for this candidate.

Changed: `src/beatsync_fork/stage_cache.py` (new), `src/auto_mode/__init__.py`,
`tests/test_stage_cache.py` (new), `tests/test_l2_stage3_cache_identity.py` (new),
`.claude/rules/l2-stage-cache.md` (new), `.claude/rules/creative-controls.md`,
`.claude/rules/freestyle.md`, `CHANGELOG-FORK.md`.

### Performance — 2026-10-04 (Stage-5 Bounded Cache Identity Parallelism R1)

**The per-source Stage-5 identity scan is now bounded-parallel. Cache identity itself is unchanged.**

Both production consumers of the per-source strong identity scan — `analyze_video_sources()` and
`classify_library_sources()` — computed `_cache_path()` one source at a time, then loaded the record.
The identity half is I/O bound (one `stat` plus the D2 bounded fingerprint, at most 3 MiB read per
source), so it parallelises; the record half was left exactly as it was.

```
before   for each source:  _cache_path()  then  _load_cache()
after    bounded parallel _cache_path phase
         -> results restored to original source order
         -> existing serial _load_cache / classification phase
```

- **Measured warm on the real 1672-source Windows library** (`J:\New folder\Cuts`, ~4.75 GiB of
  bounded fingerprint bytes), against the exact production identity operation: **6.545 s** at 1
  worker, 3.552 s at 2 (1.84x), 1.796 s at 4 (3.65x), 1.088 s at 8 (6.02x) and **0.746 s** at 16
  (**8.77x**). 16 workers is the selected default.
- **Exact identity parity**: **20,064 comparisons, 0 mismatches, 0 `None` results, 0 missing results,
  0 duplicate results.** No key changed, so `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and
  `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`. **No cache invalidation, no
  migration.** `_bounded_fingerprint`, `_video_signature`, `_cache_path`, `_qwen_config_token`,
  `_qwen_backend_signature_token`, `_load_cache`, `_cache_entry_is_complete`, `_checkpoint_cache` and
  `_save_cache` have **zero executable changes**.
- **A controlled cold parallel speed-up is NOT MEASURED, and is not claimed.** First-touch serial
  identity on that library measured **~103.8–110.5 s** (immediate warm serial: ~6.5–10.5 s), which is
  evidence of *potential* user value and nothing more. The historical ~69 s full-call discrepancy is
  **not** declared resolved: identity I/O now strongly explains its shape, but the current real
  library had 417 cache misses, so a full warm `analyze_video_sources()` was correctly not run and the
  exact historical run was not reproduced. Windows acceptance against this candidate is still
  outstanding.
- **Position is the authority, never the path.** `_compute_cache_paths_parallel` writes results into a
  pre-sized list by submission index, so `[A, B, A]` returns `[key A, key B, key A]`: duplicate paths
  stay distinct positions, nothing is deduplicated, and completion order cannot leak into
  `cache_paths`, `results_by_index`, `jobs`, progress numbering, `classifications` or Stage 6.
  Permanent tests force completion order to be the exact **reverse** of submission order rather than
  trusting the scheduler.
- **Backend and Qwen-config identity are still computed once per invocation, by the caller.** No
  worker may call `_qwen_backend_signature_token` or `_qwen_config_token` for itself — per source that
  is the shape measured at 61.7 minutes for 702 sources. The two callers also keep their own
  invocation state and their own `ai_cache_disabled` decision; only the per-source computation is
  shared, deliberately not extracted into a new orchestration abstraction.
- **Failure semantics are the serial ones.** An unprovable identity is still a per-source `None` and
  never fails the phase; an unexpected exception is surfaced through `future.result()` rather than
  laundered into `None`. No retries. When backend identity is unprovable the caller builds the ordered
  `None` results directly — no `_cache_path` call and no thread pool — so one invocation-level failure
  stays one failure rather than becoming 1 + N.
- **The worker cap is hard at 16 because nothing above 16 has been measured.**
  `_cache_identity_workers()` returns 0 / 1 / `min(n, 16)`; `BEATSYNC_CACHE_IDENTITY_WORKERS` is a
  benchmarking override clamped to `1 … min(n, 16)`, with a malformed value falling back to the
  measured default. It is execution policy only: it reaches no signature, no key, no record and no
  contract constant, and a test proves the name appears in none of the identity primitives while
  every worker setting reproduces byte-identical keys.
- **`cache_identity_seconds` changed meaning, deliberately.** It is now the **wall-clock latency of
  the whole bounded phase** rather than a serial accumulation; summing per-worker task durations would
  make the 8.77x speed-up report as a slow-down. `cache_lookup_seconds` keeps its original
  serial-accumulation meaning, because record lookup is still serial. Structural tests pin both
  definitions in both callers.
- **Nothing else was parallelised.** Record loading, hit/miss accounting, job construction,
  classification verdicts and progress remain serial and in source order; the analysis pass keeps its
  existing `_video_analysis_workers` pool and Qwen keeps its existing sequencing. The classifier
  remains read-only with respect to cache records, and its four-verdict vocabulary is unchanged and
  asserted identical at 1, 2, 4, 8 and 16 workers.

Changed: `src/video_analysis.py`, `tests/test_stage5_cache_identity.py`,
`tests/test_library_preparation.py`, `tests/test_scale_diagnostics.py`,
`.claude/rules/stage5-cache-identity.md`, `.claude/rules/library-preparation.md`,
`.claude/rules/scale-diagnostics.md`, `.claude/rules/stage5-reporting.md`.

### Fixed — 2026-10-04 (Legacy Micro Cuts Safety R1)

**The rare half-beat accent layer now keeps its own spacing floor between its own accents.**
`add_rare_micro_cuts` built its occupied set once, before the candidate loop, and never returned an
accepted extra to it. Each candidate was therefore judged against the main grid **only**, so two
accepted extras could each clear the floor against the grid while violating it against each other.

```
before   occupied = the main grid, fixed for the whole loop
after    occupied = the main grid + every extra accepted so far
```

- **Measured, freshly, on merged `main` before the edit.** Funded 200 BPM fixture, 50-cut grid: at
  Micro Cuts 75 a pair **0.3000 s** apart and at Micro Cuts 100 the same, against a **0.3400 s**
  floor, with the budget fully funded (2 and 4 accents). After the fix the closest pair is 2.4000 s
  at both settings.
- **`final_wave_cleanup` never closed it and was never going to.** It enforces
  `peak_energy_min_interval` — 0.30 s, and *divided* by the density factor, so 0.15 s at Cut Density
  100 — which is Cut Density policy. `micro_min_gap` is deliberately density-independent, so the two
  cannot coincide at any setting. The fix therefore belongs in the accent layer, which already owns
  the floor, and no density field was touched.
- **The floor and the budget are unchanged.** Still `max(cfg.micro_min_gap, median_beat * 0.45)`; no
  new floor, nothing density-derived. `enable_rare_micro_cuts`, `max_micro_cut_ratio`,
  `MICRO_CUT_RATIO_CAP`, `micro_percentile` and the four density fields are untouched. This is a
  safety correction, not a retune.
- **A rejected candidate does not consume budget, and that is load-bearing.** The scan continues, so
  a funded render still delivers `max_extra` *safe* accents. The rejected alternative — leaving the
  inserter alone and post-filtering its output with the existing `micro_extra_safety` — satisfies
  every spacing assertion and silently under-delivers the funded budget: measured **1 of 2** accents
  at Micro Cuts 75 and **2 of 4** at 100. A permanent test asserts `len(extras) == max_extra` so that
  shape of regression is loud, and a mutation reproducing it fails that test.
- **Nothing changed on realistic material.** A permanent 25-combination compatibility matrix
  (Cut Density × Micro Cuts, both 0/25/50/75/100) on the real 13-section 123 BPM fixture requires
  `np.array_equal` against a frozen pre-fix reference, and all 25 agree. The collision needs a fast
  tempo — roughly **176–273 BPM**, since it requires `period < 0.34 s` and the layer returns early
  below `median_beat 0.22` — which that fixture never reaches. The compatibility rule is explicit: an
  output may differ **only** where the old behaviour accepted a floor-violating extra.
- **The oracle does not regenerate expected values from the new code.**
  `_legacy_add_rare_micro_cuts_reference`, labelled `LEGACY_REFERENCE_FOR_COMPATIBILITY_ONLY`, is a
  frozen transcription of the pre-R1 body living in the test module; a calibration test asserts it
  still reproduces the 0.3000 s defect, and a guard asserts production never imports it.
- **Freestyle V1 is behaviourally untouched**, pinned byte-identical across 30 real heterogeneous
  combinations (plus 6 that resolve uniformly and are skipped by name). `micro_extra_safety` is now a
  measured no-op on tested inputs, and is **deliberately retained** in the heterogeneous composition
  as a preserved guard — the structural tripwire that keeps the two Stage-4 compositions distinct
  still holds. It was not removed, and the no-op measurement is recorded rather than acted on.
- **One historical test was converted, not deleted.**
  `test_the_legacy_micro_pass_really_can_place_two_extras_too_close` asserted the defect *existed* —
  its own docstring named this milestone as the one that should retire it. It is now
  `test_the_legacy_micro_pass_keeps_its_own_floor_between_extras`, asserting the positive invariant
  with the historical 0.3000 s / 0.3400 s measurement preserved in its docstring and a guard that the
  pre-fix value is no longer reachable.

Freestyle V1's own entry below is **not** rewritten: the defect was real when that milestone shipped
and was recorded there honestly. This later milestone corrected it; `.claude/rules/freestyle.md` now
carries both the history and the current invariant.

Scope: `src/auto_mode/stage4_select.py`, `tests/test_micro_cuts.py`,
`.claude/rules/pipeline-core.md`, `.claude/rules/freestyle.md`, `CHANGELOG-FORK.md`. No Stage 5, no
Stage 6, no GUI; `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`. Windows runtime acceptance has **not** been run against
this commit.

### Added — 2026-10-04 (Freestyle V1)

**One song, edited two ways.** Every creative control the app has ever had is global for the whole
track: one Cut Density, one Motion Bias, one of everything, first beat to last. Freestyle V1 is the
first feature that lets a `drop` be cut differently from the `verse` before it.

```
THE SIX SLIDERS   = the global base                      (unchanged execution truth)
A FREESTYLE RULE  = a per-SECTION delta on five of them  (a render request, never source identity)
STILL EXACTLY ONE EACH = the Variation Seed, and Micro Cuts
```

- **Five fields per section, derived not restated.** `FREESTYLE_CONTROL_FIELDS` is
  `presets.CREATIVE_CONTROL_FIELDS` minus `micro_cuts`: Cut Density, Semantic Emphasis, Energy
  Response, Motion Bias, Source Diversity. **Micro Cuts stays global** because it governs a rare
  accent layer whose whole contract is that it cannot become flicker, and **the Variation Seed stays
  global** so the one number a user writes down keeps describing the render. Neither has a field on
  `SectionOverride`, so a rule cannot touch them — structural, not careful.
- **Sparse, and keyed by section TYPE.** Unset fields inherit the live global value; a section with
  no rule, an unknown section type, an empty rule or a stale declaration all get the global base
  object back **by identity**. Every instance of a repeated type shares one rule.
- **Freestyle OFF is byte-identical to current `main`, and so are three more cases.** The
  short-circuit tests the resolved *values*, not the checkbox: off, on with no rule, on with a rule
  that sets no density, and on with every rule landing on the base density all take Stage 4's exact
  legacy composition. Rules are *retained* while the checkbox is off, so switching Freestyle on
  without setting anything also changes nothing.
- **Stage 4 gained a second composition, and `final_wave_cleanup` is deliberately absent from it.**
  Its density band is computed from the global beat count and its cap ranks cuts across the whole
  track, so running it after per-section selection let one section's rule delete cuts from unrelated
  sections — measured: changing only the `drop` rule mutated five non-target sections, three not even
  adjacent. The heterogeneous path is `section_density_cleanup` per section →
  `cross_section_safety` (boundary-straddling pairs only, `min(gapA, gapB)`, keep earlier) →
  **unchanged** `add_rare_micro_cuts` → `micro_extra_safety`. Measured on the 566-beat / 13-section
  fixture: moving only `drop` from 50 to 100 leaves all eleven non-drop sections byte-identical.
- **A known legacy defect is recorded and deliberately NOT fixed.** `add_rare_micro_cuts` computes
  `selected_sorted` before its loop and never adds an accepted extra back, so two extras can land
  closer together than the accent layer's own floor; the post-micro cleanup enforces only
  `peak_energy_min_interval` and does not close it either. Fixing it would change the output of every
  existing uniform render, so it is out of scope. Measured at 200 BPM on a funded 50-cut grid:
  4 extras, closest pair **0.3000 s** against a 0.3400 s floor. The Freestyle path does not inherit
  it — `micro_extra_safety` returns 2 extras, closest pair 2.4000 s — and a test asserts the legacy
  defect *exists*, so the fix cannot be mistaken for a no-op.
- **L1A survived the refactor it was designed for.** `ScoringControls` became a per-segment value, so
  the Stage-6 static table grew a key dimension — `(ScoringControls, target)`, built **lazily**, so
  the column count is bounded by `min(distinct controls × distinct targets, segments)` and can never
  reach the pre-L1A per-segment shape. `ScoringControls` is a frozen dataclass of three
  `float | None`, hence hashable and equality-deduping, so two sections resolving to the same values
  share one column and no new identity type was invented. **Source Diversity stays out of the table**
  and is threaded per segment, because it reads the running `usage` counter. `usage`, `recent_ids`
  and `recent_videos` are still created once for the whole plan, and the seed is still global.
- **Nothing re-analyses.** No Freestyle value reaches `_qwen_config_token`, `_video_signature`,
  `_cache_path`, a Qwen request, the prompt or a persisted record. `video_analysis.py`,
  `stage5_qwen_scene_worker.py`, `video_processor.py`, `ffmpeg_processing.py`, `creative.py`,
  `presets.py`, `creative_recipe.py`, `variant_lab.py`, `variant_batch.py`, `render_batch.py`,
  `director.py`, `audio_mix.py` and `smart_mix.py` were **not modified**; `CACHE_CONTRACT_VERSION`
  stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`.
  Changing a rule re-plans; it never re-analyses.
- **GUI: one accordion, eleven widgets, one read-out.** A checkbox, ten section dropdowns
  (`Base` plus the four named presets — `Custom` excluded, since it is a state with no values to
  project) and a read-only summary. All eleven `.change()` registrations write **only** the summary.
  The summary marks every inherited field `Base` and never prints an inherited number, because the
  global controls have five legitimate writers and a number there could not be kept honest.
- **No CLI flag**, no new environment variable, no stage cache, no per-section Micro Cuts, no
  per-section seed, no section-instance editor and no visual timeline. Tests ban each by name.

**Runtime acceptance status — read this before citing the numbers below.** The three
Freestyle files (`src/beatsync_fork/freestyle.py`, `tests/test_freestyle.py`,
`.claude/rules/freestyle.md`) were **absent** from the working tree and from every reachable git
object when this feature was finished, while the Stage-4, Stage-6, GUI, `ui_content`, rule and test
changes that *call* them were already present. The module was therefore **reconstructed** against
that call surface: the eleven-widget GUI seam, `auto_mode._resolve_freestyle` /
`_freestyle_section_settings`, `stage6_av_planner.freestyle_declaration` and the assertions in
`test_cut_density.py`, `test_micro_cuts.py`, `test_stage6_score_precompute.py`, `test_variant_lab.py`,
`test_creative_presets.py` and `test_creative_controls_seam.py`. The whole suite passes
(3736 passed, 260 skipped; the 3 remaining failures are pre-existing Windows-only path-case
assertions that also fail on the parent commit). The paragraph below is the **original** run's
recorded evidence and is retained as such — it was **not** re-run against the reconstructed module,
and could not be: this is a Windows-only app and the reconstruction was done in a Linux session with
no portable runtime, no FFmpeg and no Qwen models. Treat the measured cut counts as unverified for
this code until the acceptance run is repeated on the Windows machine.

Runtime acceptance (original run, not re-verified): real Stages 1–6 through real FFmpeg on a
140 BPM / 72 s synthetic track with Stage-3 sections `intro`/`drop`/`verse`/`finale` and three sources, in an isolated scratch build.
Freestyle OFF gave 47 cuts; ON with every section `Base` gave a **byte-identical** 47; ON with
`drop=High Energy` + `intro=Cinematic` gave 43 and reproduced exactly on a repeat run. Both renders
came out at 72.000 s with video and audio streams. Holding a per-section baseline and moving only
the `drop` rule, the number of other sections that changed was **zero**.

One measured behaviour worth stating because it looks wrong and is not: ruling `drop` at density 65
or 100 *reduced* that section from 17 cuts to 14. The **global** Cut Density slider does exactly the
same on the same material (50 → 17; 65/80/100 → 14), so the per-section control faithfully
reproduces the global one, non-monotonicity included. That discreteness is pre-existing and is
recorded, not fixed.

New files: `src/beatsync_fork/freestyle.py` (stdlib-only; imports `creative` and `presets` and
nothing else) and `tests/test_freestyle.py`, which owns the record's own contract — the derived field
registry, the two unreachable controls, the `SECTION_TYPES` pin against the real
`stage3_sections.classify_section`, the preset projection, the identity-preserving composition and
the two read-outs. `ui_content` supplies the `Freestyle:` label for the success panel, exactly as it
does `Creative variation:`, so `describe()` stays a bare list of resolved numbers and reads correctly
inside the Stage-4 console line too. `tests/test_library_preparation.py`'s frozen render-request list
gained the eleven appended widgets, derived from `freestyle.SECTION_TYPES` rather than retyped; the
property it protects — no preparation state reaches the render request — is unchanged. Full contract:
`.claude/rules/freestyle.md`.

### Added — 2026-10-03 (AI Director V1)

**Describe the edit you want in a sentence; get a reviewable proposal for the seven creative values
that already decided every render.** AI Director V1 is a **second producer of `CreativeRecipe`, not
a new render pipeline** — which is exactly the role C2 wrote that class for when it refused to
carry a master seed, a spread or any other generator provenance. `creative_recipe.py` needed no
change at all.

```
natural-language intent -> local text-only Qwen -> validated proposal
-> the user presses Apply Proposal -> the existing Variation Seed + six Creative Controls
```

- **Visual only, and resource identity is untouchable.** The model emits exactly the six 0..100
  controls in `presets.CREATIVE_CONTROL_FIELDS` order. It generates nothing else: not the three
  audio levels, voice clips, voice timing, `avoid_drops`, the SFX folder or roles, the source
  folder or files, FPS, the encoder, the output filename, the source confirmation or the Media
  Library Preparation state.
- **The model never chooses the Variation Seed.** The schema has no `seed` property and
  `additionalProperties` is `false`, so the grammar cannot emit one — and the strict parser rejects
  one that arrives anyway rather than ignoring it, because a producer that thinks it owns the seed
  is a producer worth failing. The GUI mints it with the existing `variation.random_seed()`
  **after** six valid controls exist; a test counts the draws and requires **zero** for an invalid
  response, so a malformed answer cannot yield a plausible-looking half proposal.
- **The existing trust boundary is reused, unmodified.** `CreativeRecipe.from_mapping` decides, and
  its all-or-nothing contract is unweakened: missing field, extra field, wrong type, `bool`,
  `float`, out of range or seed 0 rejects the whole recipe. No coercion, no clamping, no partial
  application, no fallback to Balanced.
- **Strict execution, tolerant explanation.** A missing, non-string or overlong `explanation` still
  yields a valid proposal (blank or bounded); an invalid *control* rejects the proposal entirely.
  The explanation is display text and reaches no recipe, profile, bus, stage, planner, cache or
  Qwen request. Measured: llama.cpp does **not** compile the schema's `maxLength` into its grammar
  on this build (60/160/280 all produced identical ~460-character output), so the bound is a
  request in the schema and a guarantee only in `normalize_explanation`.
- **The whole of stdout is parsed strictly.** No regex fishes a `{...}` out of prose — that is how
  a truncated object or a chatty preamble gets half-accepted, and Stage 5's own documented
  truncation defect lived exactly there. Strip, remove the one fixed `[end of text]` marker
  llama.cpp appends to its own output, `json.loads` the remainder, require the exact key set.
  `--json-schema` is defence in depth; the parser is the authority.
- **Two measured deviations from the obvious invocation, both load-bearing.** On the installed
  build (`b9842-6f4f53f2b`) `llama-cli` is an interactive chat front end: it *rejects* `-no-cnv`
  ("please use llama-completion instead"), ignores `--no-display-prompt`, and prints its banner and
  timings **into stdout**. So the Director uses **`llama-completion.exe`** from the same `bin`
  layout, which emits the JSON object alone on stdout. And it uses **`-cnv -st`** rather than
  `-no-cnv`: `-cnv` applies the model's chat template and `-st` runs one turn and exits. Raw
  completion skips the template, and on an Instruct model that is not a small difference —
  measured over five intents, `-no-cnv` collapsed every control to 0 or 1 and rambled past the
  token budget, while `-cnv -st` produced coherent, well-separated recipes. The retained P0 probes
  could not tell the two apart, because both of them in fact measured template-applied output.
- **Model assets reused; the Stage-5 worker is not.** The same installed
  `Qwen3VL-2B-Instruct-Q8_0.gguf`, **text-only** — no `mmproj`, no image argument.
  `stage5_qwen_scene_worker.py` is not invoked, `qwen_progress` is not used, and neither is
  imported for its paths; `gui.py` derives both from the one general `ROOT_DIR` constant. One
  bounded `subprocess.run` per proposal with a **60 s** timeout and `CREATE_NO_WINDOW`: no
  `llama-server`, no port, no persistent process, no session, no chat history.
- **Media-blind, slider-blind, cacheless.** The invocation receives the instruction, the system
  prompt and the schema — no frames, filenames, Stage-5 records, `beat_info`, sections, tempo or
  source state. It does not read the current seed, sliders, preset or Variant Lab state either, so
  an instruction is an *absolute* editing intention rather than a transformation of the screen.
  There is no Stage-5 cache use, no proposal cache, no prompt history: every press is independent.
- **Propose, then apply.** Generate Proposal writes **zero** execution widgets — only the proposal
  state and the two read-outs — which is what makes generating-is-not-applying structural, exactly
  as it is for `generate_variants_btn`. Apply writes `variation_seed`, the six sliders,
  `creative_preset` and the Director status, and nothing else. Neither renders, and neither can
  reach a render entry point or the source gate (walked structurally from both buttons).
  **Unlike Variant Lab's Apply there is deliberately no stale gate**: a proposal is an absolute set
  of seven values, so it survives an apply and may be re-applied after manual experiments.
- **The Director emits no preset label.** Which named recipe six numbers match is a GUI read-out,
  so Apply recomputes `presets.matching_preset(...)` itself (programmatic slider writes do not fire
  `.input()`). `_variant_apply_outputs` is deliberately **not** reused — it also writes the lab's
  Master Seed, the three audio levels and the lab report, none of which the Director generates —
  but `matching_preset` is, so there is still exactly one preset-label path.
- **Writer matrices extended by exact list, never relaxed.** The six sliders, the Variation Seed and
  `creative_preset` each gained precisely `apply_director_btn.click` and nothing else;
  `generate_director_btn.click` is absent from all three. `director_proposal_state` has exactly one
  reader.
- **Isolation.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `video_analysis.py`,
  `stage5_qwen_scene_worker.py`, `src/auto_mode/*`, `video_processor.py`, `ffmpeg_processing.py`,
  `creative.py`, `presets.py`, `creative_recipe.py`, `variant_lab.py`, `variant_batch.py`,
  `render_batch.py`, `audio_mix.py` and `smart_mix.py` are **untouched**. Director widgets are
  absent from every source and preparation list, and the Director borrows no existing read-out
  panel. Variant Lab interoperability is through the live sliders only — Apply moves them, and the
  lab reads them as its base exactly as it always has. **No CLI flag**: the CLI already takes the
  seven resolved numbers, which are the reproducible contract.
- **Real-runtime acceptance** on the installed build, text-only, no mmproj: **2.54–2.57 s** per
  proposal end to end (cold model load every time, because the process exits), `rc=0`, strict
  payload valid, recipe `20/10/70/60/30/50` for a cinematic instruction, GUI-minted Variation Seed,
  `matching_preset -> Custom`, no orphan process, and the applied sliders accepted unchanged as a
  Variant Lab and `variant_batch` base. No render was performed.

### Fixed — 2026-10-03 (Universal GUI atomic no-overwrite — H1)

**No BeatSync GUI render can silently replace an existing durable output any more.** Ordinary
Create Music Video could: the promotion into `output/` was `shutil.move`, which on the supported
Windows environment replaces the destination without a word — measured, with the user's old bytes
simply gone. The name is distinct only per second per Variation Seed, so an earlier run, a manual
copy or a same-second retry was enough to lose a finished video.

- **One universal policy, no opt-in.** C3-R0 had protected only the batch, behind a keyword-only
  `refuse_existing_output` that defaulted to `False` — which is precisely how the single render
  kept the destructive path. That flag is **removed** from the whole GUI render chain and is not
  replaced by another boolean: with an atomic primitive, overwriting is *impossible* rather than
  *disabled*, and a flag defaulting to the unsafe value is a trap for the next caller.
- **The promotion is a single no-replace OS operation.** `gui._promote_output_no_replace()` calls
  `os.rename` once — not `shutil.move`, not `os.replace`, not `exists()`-then-move. The old
  double-`exists()` guard was a TOCTOU pair: another process could create the path in the window
  between the check and the move, and the move destroyed it anyway. Measured on Windows:
  destination free promotes; destination occupied raises `FileExistsError` (`WinError 183`) with
  the existing bytes unchanged and the render still on disk. **This rests on Windows-specific
  semantics** — POSIX `rename(2)` replaces silently — which is sound only because this app is
  Windows-only by construction, and is recorded as such in `.claude/rules/pipeline-core.md`.
- **An early check still runs before Stage 1**, purely so a doomed render costs no analysis. It is
  a courtesy; the rename is the authority, and there is deliberately no second `exists()` before
  it.
- **Every failure is fail-closed.** Nothing is ever deleted to make room. A collision or a
  promotion error preserves the existing file, **retains the new render** in `session_dir` and
  names both paths, leaves `LAST_OUTPUT_PATH_KEY` empty, and generates no ProRes preview — a
  promotion failure must never read as a finished render. A cross-volume destination
  (`errno.EXDEV`, measured as `WinError 17`) refuses rather than copying: a copy is not an atomic
  promotion, and an interrupted one would leave a partial video at the final path.
- **Nothing about naming changed.** Same timestamp, same `_seedNNNNNN` suffix, same `.mp4`/`.mov`
  choice, same ProRes `_preview.mp4`. No auto-rename, no `_2`, no counter, no UUID — the Phase A
  contract is explicit that today's names must not change, and a collision counter would invent a
  naming subsystem with a race of its own.
- **The CLI is untouched.** `video_processor.py` has no `shutil.move` at all: it hands the user's
  explicit `-o` path to FFmpeg, which writes it with `-y`. Overwriting a path the user named is
  the standard command-line contract, so H1 is GUI-only.
- **C3-R0 keeps its candidate stems**, and the reason is restated rather than retired. They were
  introduced because a same-name collision meant one candidate destroying the other's video; now
  it would make the second candidate legitimately *refuse*, and a batch asked for two videos would
  deliver one. Identity is what lets both succeed.

### Added — 2026-10-03 (Variant Lab Render Two Compared Candidates — C3-R0)

C3 V1 let the user generate N candidate settings, compare them and apply **one**. The payoff of a
comparison is watching the videos, though — and getting two meant two manual round-trips, with the
batch consumed by the first Apply and the base moved out from under the rest. C3-R0 closes that
loop: tick exactly two candidates, press Render Selected Variants, get two real videos.

- **Exactly two, sequential, and no cancellation — one decision rather than three.** There is no
  safe stop channel in this architecture today: a cancelled Gradio event can return its slot while
  the daemon render worker is still alive, and the next render would clear the live one's
  process-global processing directory. Rather than ship a Stop button that cannot actually stop
  FFmpeg, C3-R0 ships none and bounds the commitment to two renders. Three or more,
  continue-after-failure and real cancellation are **C3-R1**, behind an explicit worker lifecycle.
  The selection bound is deliberately unrelated to the comparison bound of 12: that one is
  legibility and costs a millisecond, this one is uninterruptible render minutes.
- **Renders are now mutually exclusive, and that was a latent hazard rather than a new one.**
  `create_music_video` clears `get_processing_dir()` at the start of every render, and
  `PROCESSING_DIR` is one module-level constant — not per session. Until now `process_btn.click`
  was the only render event, so nothing could overlap. Adding a second one created the hazard, so
  both wrappers take a process-global non-reentrant `threading.Lock` non-blockingly and refuse
  cleanly when it is held, and both events share one Gradio `concurrency_id` with
  `concurrency_limit=1`. The lock is the authority because it is provable without Gradio; the
  concurrency group is cooperative serialisation on top.
- **One gate core, two mutex-owning wrappers.** The live source gate, the config normalisation and
  the render delegation moved out of `process_video_guarded` into
  `_process_video_guarded_unlocked`, which both the single-render wrapper and the batch wrapper
  call — the batch once per candidate, so **every candidate is independently re-verified** and
  carries its own `verification_seconds`. The batch deliberately reaches the core and not the
  wrapper: re-entering a non-reentrant lock it already holds would make a batch refuse itself on
  its own first candidate. `process_video_guarded`'s signature and its `process_btn.click`
  registration are byte-identical, because they are half of a positional Gradio contract.
- **Candidate output identity cannot rest on the Variation Seed.** C3 deduplicates candidate
  *masters* deliberately; `CreativeRecipe.seed` is an independent draw and is deduplicated
  nowhere. Measured: root 5484 yields masters 945730 and 862920 that **both** resolve Variation
  Seed 536635. Since the render path names its file `_seed<VariationSeed>` and `shutil.move`
  overwrites silently (also measured), each candidate gets a stem carrying the request tag, the
  candidate index and the candidate master — `music_video_batch<tag>_c01_m609591` — and the
  existing suffix follows unchanged.
- **Batch-only no-overwrite.** `refuse_existing_output` is keyword-only so the positional widget
  list can never supply it, defaults to `False` so ordinary Create Music Video keeps its shipped
  behaviour exactly, and the batch passes `True`: the destination is checked before Stage 1 *and*
  again immediately before the move, preserving whatever is already on disk. The pre-existing
  single-render overwrite is a separate latent defect, reported rather than changed inside this
  feature. **Superseded by H1 below, which also corrects this entry's original wording**: it said
  "hard no-overwrite", which overstated two `os.path.exists` checks around a `shutil.move` — a
  TOCTOU pair, sound against this batch's own second candidate but only best-effort against
  another process.
- **Durable output is the success authority**, not the preview and not the status prose. A ProRes
  render moves the real `.mov` into `output/` and then returns a session-temp `_preview.mp4`, so a
  preview step failing afterwards must not retroactively fail a finished render. A new
  `LAST_OUTPUT_PATH_KEY` on `session_state` records it, with the same lifecycle as the two report
  keys: cleared before every attempt, set only after the move succeeds. Nothing parses a path out
  of status text.
- **Fail fast, preserve prior success.** A failed candidate stops the batch and deletes nothing.
  The render boundary exposes no typed failure classification, so a batch cannot tell a
  shared-input failure — which would simply repeat — from a candidate-local one.
- **The candidate values come from the stored recipes, never from the screen.** Two candidates are
  never simultaneously visible, so live widgets cannot be a batch's execution authority. The
  non-candidate intent (audio, voice, SFX, source, output, encoder, FPS) is frozen from the
  submitted event arguments, so edits made while the batch runs cannot reach it.
- **Rendering does not use Apply's stale-declaration gate, and does not consume the batch.** Apply
  has that gate because it writes a historical candidate into the *current* screen; rendering
  reads already-resolved artifacts and writes no widget. The comparison survives a render, so the
  pair can be rendered again or one of them applied. `variant_batch_state` now has exactly two
  readers, pinned as an exact list.
- **Report ownership is unchanged.** `audio_layers_report` and `smart_mix_report` keep
  `process_btn.click` as their only writer; the batch reads them from `session_state` and a
  dedicated summary owns multi-render diagnostics, so no panel can describe a candidate the user
  is not looking at. Progress is the existing stream with one ordinal line prefixed — no new
  `ProgressEvent`, stage, phase, counter or ETA, and nothing parsed back out of the text.
- **New pure module** `beatsync_fork/render_batch.py` (stdlib-only): the selection contract, the
  candidate output identity and the summary formatter. It decides; `gui.py` performs every side
  effect. `variant_batch.py` is untouched and keeps its own batch-render ban at full strength,
  which is what holds the generation/render split honest.
- **Nothing in the pipeline changed.** `video_processor.py`, `ffmpeg_processing.py`,
  `video_analysis.py`, `audio_mix.py`, `smart_mix.py` and `src/auto_mode/*` are untouched;
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`. No stage cache was invented: Stages 1–3 and a warm
  Stage-5 cache scan are simply repeated per candidate, because Stage 4 and Stage 6 must re-run
  anyway once Cut Density and Micro Cuts vary.
- **Six seam suites were re-pointed, not weakened.** `test_audio_layers_seam.py`,
  `test_creative_controls_seam.py`, `test_creative_seed.py` and `test_scale_diagnostics.py`
  asserted gate/config/timing properties against `process_video_guarded`'s body; those properties
  moved into the shared core and now hold for *both* render paths. Every wrapper-level assertion —
  the positional signature, the `fn=` registration, the report writer matrices — stayed exactly
  where it was, and new structural guards assert that the wrapper owns the mutex and duplicates no
  gate logic.

### Added — 2026-10-03 (Variant Lab Multi-Variant Generation + Comparison — C3 V1)

Variant Lab could generate one recipe per click and wrote it straight back, which is a good way to
wander and a poor way to **choose**: comparing two directions meant generating one, reading it,
generating another and having already lost the first. C3 resolves N candidates at once from one
frozen starting point, shows them side by side, and applies exactly one.

```
GENERATE N  ->  COMPARE N  ->  APPLY ONE  ->  (the user presses Create Music Video)
```

- **C3 V1 renders nothing, and that is the scope decision rather than an omission.** A render is
  ~150 FFmpeg clip extractions through the one hardware encoder a *single* render already saturates
  (`_effective_clip_workers()` exists for exactly that contention); a candidate costs tens of
  microseconds. Batch rendering is deferred to a separate milestone (**C3-R**) along with its own
  failure policy, output naming, cancellation and per-variant report ownership — none of which
  exists today, and none of which was speculatively added.
- **A new pure module, `beatsync_fork/variant_batch.py`**, stdlib-only like the rest of the package.
  It **orchestrates the frozen resolvers and never re-implements them**: every candidate is
  `variant_lab.resolve(...)` + `variant_lab.resolve_audio(...)` unchanged, so no C2 or E2 golden
  vector can move. A test forbids `uniform(`, `_half_up`, `anchor_for` and `hashlib` inside it. The
  dependency is one-way — `variant_batch → variant_lab`, never the reverse — and `variant_lab.py`
  gained exactly one constant plus its `__all__` entry and docstring prose.
- **One candidate master per index, under the new `batch` RNG domain**:
  `variant_lab|1|<root>|batch|<i>` → SHA-1 → a six-digit master in 1..999999, the same range every
  other seed the user sees lives in. Index-keyed rather than drawn from one sequential stream, and
  that is the load-bearing choice: **asking for 8 candidates instead of 5 leaves the first 5
  identical.** The three existing domains are untouched, so adding C3 shifts no value any master
  seed already resolves to.
- **Candidate masters are unique as a contract, not a probability.** Twelve six-digit draws collide
  about once in 14,000 batches and two identical rows read as a bug, so a collision steps
  deterministically forward (wrapping at the top) against the masters already fixed at *lower*
  indices only — prefix stability survives, the scan is bounded, and it is never a redraw. Same
  reasoning as C2 R1-B's `_fresh_variant_master_seed`; the forced-collision test is monkeypatched
  rather than hoped for.
- **One click freezes the screen once.** All N candidates resolve from the *same* original visual
  and audio base — candidate 2 is never resolved from candidate 1. That is structural rather than
  careful: **generating writes no execution widget at all**, so no result can feed the next draw.
  Looping today's single Generate would chain, because *that* handler writes back.
- **`VariantBatchDeclaration` is a declaration record, not the cached base snapshot C2 rejected.**
  It is never read as a substitute for the live widgets at resolve time, only compared against
  them — the same family as `SourceSnapshot` and `PrepScanResult`. Canonical by construction, so
  staleness is plain structural equality rather than a digest with a serialisation contract to keep
  in step.
- **Apply is live-gated and fail-closed.** It re-reads the whole screen through the shared
  normalisation helper at click time and refuses unless it equals the declaration its batch was
  built from — the same reason `process_btn` validates the live source controls and
  `prep_analyze_btn` takes the live preparation controls. A refusal writes `gr.skip()` to every
  execution widget; a *stale* refusal also consumes the batch so it cannot be retried, while a
  missing selection does not, because that list is still valid. There are deliberately **no**
  `.change()` invalidation handlers: adding one per slider would put a second binding on widgets
  whose single `.input()` is itself a load-bearing contract.
- **Apply is terminal for one batch** — it moves the live base, so the remaining candidates now
  describe a starting point that no longer exists — and it writes the **candidate's own** master
  into the Master Seed box, because that field means provenance for the recipe now on the sliders.
  The batch root stays visible in the comparison text. Neither seed alone reproduces anything:
  R1-A's rule applies to both, and two tests pin it.
- **One projection helper, one preset path.** Apply calls the existing `_variant_apply_outputs`
  with temporarily rehydrated resolution objects, so it writes the identical thirteen-widget tuple
  an ordinary Generate writes and recomputes `matching_preset` explicitly. A test asserts Apply's
  output equals a single Generate from that candidate master.
- **The batch is deepcopy-safe, which is a real Gradio `State` requirement rather than a style
  note.** `VariantLabConfig` / `AudioVariantConfig` hold `MappingProxyType` and the resolutions hold
  those configs, so none of them may enter session state. `VariantBatch` stores plain ints, strings,
  tuples and the two frozen recipe dataclasses; `rehydrate()` builds temporary resolutions inside
  one handler and never returns them. Tests assert `copy.deepcopy(batch) == batch` and walk every
  reachable object for a forbidden type.
- **Count 2 / 12 / default 5.** The cap is comparison legibility and a typo guard, not a resource
  bound — twelve candidates cost about a millisecond — and it must not be reused as a future
  batch-render limit.
- **GUI: six components** inside the *existing* Variant Lab accordion — count, Generate Variants,
  a read-only comparison table, a `gr.Radio` selector whose `(label, value)` value *is* the
  candidate index, Apply Selected Variant, and a read-only status. No `gr.Dataframe` dependency.
  `VariantBatch.table_text()` is the one formatter; `gui.py` formats none of it. The selector
  registers no handler.
- **Shared normalisation.** The config/base construction `_on_generate_variant` used to inline was
  lifted into `_build_variant_resolution_context`, now used by all three Variant Lab handlers — the
  declaration Apply compares against is only trustworthy if it is built by the same code that built
  the batch. Single Generate is value-identical and `_on_new_variant → _on_generate_variant` is
  unchanged.
- **Writer matrices extended by exact list, never relaxed.** The six creative sliders now have
  exactly `creative_preset.input`, `generate_variant_btn.click`, `new_variant_btn.click` and
  `apply_variant_btn.click`; the three audio levels exactly the latter three. Absent from both:
  `generate_variants_btn.click`. The excluded audio controls keep **zero** writers and both report
  panels keep `process_btn.click` alone.
- **Two pre-C3 negative guards were split, not deleted** — in `test_variant_lab.py` and in
  `test_creative_presets.py`, which carried a second independent copy. `variant_lab.py`,
  `creative_recipe.py` and `presets.py` still know nothing about C3. The load-bearing replacement
  is **structural** rather than a token list: the call graph is walked from both C3 buttons and may
  not reach `process_video_guarded`, `process_video`, `analyze_beats_auto` or `create_music_video`,
  which a rename cannot evade. Freestyle, an AI Director, a rendered variant gallery, a stage cache
  and shortlist machinery all remain forbidden.
- **Isolation unchanged.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION`
  stays `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`,
  `stage5_qwen_scene_worker.py`, `video_processor.py`, `ffmpeg_processing.py`, `library_prep.py`,
  `audio_mix.py`, `smart_mix.py` and `src/auto_mode/*` are untouched; the source confirmation gate
  and Media Library Preparation are unaffected. `variant_batch_state` reaches no planner, renderer,
  profile or cache — `apply_variant_btn.click` is its only reader. **No CLI flag.**

### Added — 2026-10-03 (Variant Lab Audio Integration — E2 V1)

Variant Lab can now vary audio, which the two entries below deliberately left to E2. It varies
**exactly three** values, all of them already plain `0..100` integers:

```
music_under_voice_percent    sfx_amount    sfx_level_percent
```

- **A parallel resolver, so C2 is preserved structurally.** `variant_lab.resolve_audio()` is a
  sibling of `resolve()`; `_resolve_v1` and `resolve` were **not modified**, so no visual golden
  vector can move. `_resolve_control` gained a `domain` parameter defaulting to `DOMAIN_CONTROLS`,
  which keeps the C2 call site and every C2 key byte-identical while letting E2 reuse the one spread
  formula rather than copying it. The reserved `"audio"` RNG domain is now **used**:
  `rng_for(master, "audio", <field name>)`, one named stream per control, so adding a fourth audio
  control later cannot shift these three.
- **One master seed and one Spread drive both halves.** No second audio seed, no second audio Spread.
- **The default ticked selection is EMPTY**, deliberately unlike the visual side's all-six: opening an
  existing Variant Lab and pressing Generate must leave the audio levels exactly where they were.
  Audio variation is opt-in, and **Spread 0 varies nothing** here because there is no audio analogue
  of the clip Variation Seed.
- **Each base is normalised by the module that owns the control.** `music_under_voice_percent` falls
  back to 35 (Audio Layers) while both Smart Mix controls fall back to 50, so a single shared 0..100
  normaliser would quietly raise the music floor on a malformed value. `variant_lab` delegates to
  `audio_mix.normalize_music_under_voice` and `smart_mix.normalize_control` instead of restating them.
- **Deliberately excluded, as product decisions rather than omissions.** Voice clips and the SFX
  folder are resource identity; `avoid_drops` is a *protective* rule with measured evidence behind it
  (a clip starting 0.44 s before a drop puts 96 % of its speech inside it); enabled SFX roles are
  structural intent whose toggling perturbs the frozen cross-role occupancy; and `voice_start_delay` /
  `voice_min_gap` are deferred fractional-seconds placement controls whose draws can legitimately make
  a render refuse. None of them needs a seconds-range model or boolean-randomization semantics, and
  none was added.
- **No audio-engine executable behaviour changed.** `smart_mix.py` is **completely unmodified**;
  `audio_mix.py` is unmodified **below its module docstring** — the `AudioMixConfig` definition, the
  placement rules and the duck model are byte-identical, and a later review-only R2 rewrote only that
  docstring, which had still described E2 as future work. `tests/test_audio_mix.py` /
  `tests/test_smart_mix.py` are unchanged — independent regression controls for voice placement, the
  duck model, the frozen Nero ladder and the occupancy policy.
  `process_video_guarded` still builds `AudioMixConfig`/`SmartMixConfig` from the live widgets at render
  click time, so the three visible sliders remain execution truth and no `AudioRecipe`,
  `AudioVariantConfig`, master seed or lab range reaches the planner, the renderer, `CreativeProfile`,
  `beat_info["creative"]`, `AudioMixPlan` or `SmartMixPlan`.
- **`creative_recipe.py` received the same current-truth correction in R2**, also module-docstring only:
  the seven-field `CreativeRecipe` implementation, its validation and the `CreativeRecipe` →
  `CreativeProfile` bridge are unchanged, and the prose now scopes "the exact execution configuration"
  to the *visual* creative contract rather than to the whole render. Proven docstring-only by comparing
  the complete module below the docstring: AST-equal, byte-identical, no import change — for both files.
- **GUI: seven new configuration components** inside the *existing* Variant Lab accordion — one
  `CheckboxGroup` with explicit `(label, value)` choices (the value *is* the frozen stream name) and
  three min/max `gr.Number` pairs. Variant Lab may write exactly `music_under_voice`, `sfx_amount` and
  `sfx_level`, from its own two buttons only; `gui.py` contains no `rng_for` and no `DOMAIN_AUDIO` and
  delegates every draw to the pure resolver. Generate and New Variant still render nothing.
- **The writer guards were split, never weakened.** The excluded audio controls keep **zero** writers
  and the report panels keep `process_btn.click` alone; two new tests pin the permitted three to their
  exact two-writer list. The split guards resolve list indirection (`expanded_names_in`) because
  `variant_lab_outputs` reaches its widgets through a named sub-list — without that, every
  "is this widget ever written?" assertion would have passed vacuously.
- **Isolation unchanged.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`, `stage5_qwen_scene_worker.py`,
  `video_processor.py`, `ffmpeg_processing.py`, `library_prep.py` and `src/auto_mode/*` are untouched;
  the source confirmation gate and Media Library Preparation are unaffected. **No CLI flag.**
- Verification: **3430 passed, 2 skipped** (from 3359/2), +71 tests. Audio golden vectors were computed
  by an independent reimplementation that imports nothing from the repository. Load-bearing proof:
  reverting the three production files makes the E2 suite fail to collect; reverting only `gui.py`
  fails 31 tests; drawing from `controls` instead of `audio` fails all 12 golden recipes; defaulting
  the selection to all-three fails exactly the two backward-compatibility tests.

### Fixed — 2026-10-02 (Smart Mix V1 — numpy-safe structure projection)

**Smart Mix could not run at all.** The runtime acceptance render aborted after Stage 5, before
Stage 6, with `ValueError: The truth value of an array with more than one element is ambiguous.`

- **Root cause.** `smart_mix.project_structure` defaulted four `beat_info` fields with the
  `value or ()` idiom. At real runtime `times`, `rhythm_data["impact_strength"]`,
  `["is_bar_anchor"]` and `["is_phrase_anchor"]` are all numpy `ndarray` — 566 elements on the
  calibration track — and `bool(ndarray)` raises for any length above one. The exception escaped
  the `SmartMixStructureError` handler and aborted the whole render, so no video was produced
  either.
- **Why every test passed.** Every fixture in the suite, and the shipped `nero_structure.json`,
  feed lists and tuples, whose truth value is perfectly well defined. The projection boundary is
  the one place real arrays arrive, and nothing crossed it with them.
- **The fix is the boundary only.** `_missing_as_empty` makes "missing means empty" explicit —
  `None` becomes `()`, **anything else is returned untouched** — and the values are then consumed
  by iteration and `len` alone. No numpy import: `beatsync_fork` stays stdlib-only, which is what
  keeps the planner testable on a bare interpreter. It is not numpy-aware either, so any container
  whose `__bool__` is ambiguous or forbidden now passes through. Empty and `None` still produce the
  same clear `no usable beat times` / `misaligned` errors as before.
- **The regression needs no numpy.** A stdlib `AmbiguousArray` iterates normally but raises on any
  boolean coercion, exactly like `ndarray`. It covers each of the four fields **independently** —
  the original failure hit `times` first and would have masked the other three — plus all four at
  once, and the full 566-beat calibration fixture projected through ambiguous arrays with the
  frozen ladder re-verified from it. A structural guard forbids those four keys appearing inside
  any `BoolOp`, `bool(...)` or `not` within `project_structure`, narrowly rather than banning `or`
  across the module. Verified load-bearing: reverting the production change fails 12 of them,
  including all four per-field cases.
- **Confirmed against the real runtime**: `project_structure` now accepts the live ndarray
  `beat_info` (566 beats, 13 sections), and the accepted ladder reproduces exactly from that live
  structure — 25 → 15, **50 → 19** (3 riser / 5 impact / 6 transition / 3 vocal_shot /
  2 atmosphere), 75 → 24, 100 → 25, zero non-atmosphere overlaps, risers ending on
  53.267 / 88.143 / 204.266, and the 87.655 collision skip intact.
- **Malformed scalar input also had to be caught (R1b).** Removing `value or ()` had a second,
  quieter consequence: that idiom was absorbing a malformed *falsey scalar* (`0`, `0.0`, `False`)
  into the empty path, so without it a scalar reached `_finite_floats` — which iterates its
  argument immediately — and escaped `project_structure` as a raw
  `TypeError: 'int' object is not iterable` instead of `SmartMixStructureError`. Reproduced
  executably for both `times` and `impact_strength` before any further edit. `_finite_floats` now
  answers `()` for a non-iterable argument, the same answer it already gave for a non-numeric
  element, `NaN` or `inf`, so `times = 0` reports `no usable beat times` exactly as it did before
  the ndarray fix. The guard is `try: for …`, **not** `if values`, so the container is still never
  truth-tested — verified with the same `AmbiguousArray`, `bool_calls == 0`. It also covers the
  *pre-existing* truthy-scalar case (`times = 7`), which `or ()` never normalised either; that one
  is a long-standing defect of the same class rather than a regression. `_finite_floats` has
  exactly two callers, both of them these two fields inside `project_structure`, so no unrelated
  planner behaviour is reachable. The anchor fields were already covered by their own
  `try/except TypeError` and were deliberately **not** widened.
- Nothing else changed: the planner, percentile, Amount mapping, aliases, priority, occupancy,
  asset cursor, R1 library diagnostics, the single report formatter, the FFmpeg graph, D's voice
  behaviour and the Stage-5 constants are all untouched. `python -m pytest`: **3359 passed,
  2 skipped** (up from 3322).

### Fixed — 2026-10-02 (Smart Mix V1 — R1 correction)

One narrow reporting defect before merge. The accepted architecture is unchanged: the Amount
mapping, percentile population and formula, role vocabulary, alias matching, role priority,
occupancy, the candidate-attempt asset cursor, the calibration ladder, the preflight probing policy,
the SFX gain, the FFmpeg graph, the one-mix-engine design, `AudioMixPlan`'s single field and D's
voice behaviour are all value-identical.

- **Library scan diagnostics were collected and then silently dropped.** `prepare_sfx_inputs` has
  always counted unknown role folders, root-level files, unsupported files and files in disabled
  roles — but its return value was only ever read for `library_root`, so none of it reached the
  report, the status panel, the console or any other user-visible surface. That broke the frozen
  contract that these are *reported* and ignored: a library containing `Impats/` beside a valid
  `Risers/` still preflights successfully, so the user was told only that there "happened to be no
  impacts". The whole point of exact folder-role classification is that a typo is **visible**.
- **The fix is reporting, not validation tightening.** Unknown folders, root-level files,
  unsupported extensions and disabled-role assets all remain non-fatal and ignored, and the fatal
  rules — missing/non-directory root, zero usable enabled assets, a missing or unprobeable enabled
  asset, an invalid duration — are untouched.
- **One pure value, one formatter.** `prepare_sfx_inputs` now returns the immutable
  `SfxLibraryDiagnostics` instead of a loose dict, `plan_sfx` carries it on `SmartMixPlan` as a
  trailing defaulted field, and `SmartMixPlan.report_lines()` is the only place it is rendered.
  `gui.py` threads the value and formats nothing — a test asserts none of the four phrases appears
  in `gui.py` or `audio_mixdown.py`, so there is no second report formatter. `SfxPlacement` is
  unchanged, and the diagnostics reach no placement, no `AudioMixPlan`, no `CreativeProfile`, no
  `CreativeRecipe` and no cache.
- **Compact and deterministic.** A line appears only when its count or list is non-empty, so a clean
  library gains no `Ignored …: 0` noise. Unknown folder *names* are listed (the names are the useful
  diagnostic) sorted case-folded with the original name as tie-break, never in `os.walk` order;
  everything else is a count rather than a list of paths. Disabled-role files read
  `Files in disabled roles skipped: N` — deliberately neutral, since disabling a role is a choice.
- **The zero-placement report is unregressed**: library header, `No SFX placed`, role-empty and
  skip reasons, plus any diagnostics.
- **The regression test runs the real chain** — `prepare_sfx_inputs` → `plan_sfx` →
  `report_lines()` — on a real folder tree containing `Impats/typo.wav`, `Risers/valid.wav`,
  `loose.wav` and `Risers/notes.txt`. Asserting on the scanner's own return value would have passed
  throughout the bug, which is precisely why it was not caught.
- No Gradio event was added: no scan button, no change or upload handler, no preflight callback.
  `smart_mix_report` keeps exactly one writer, `process_btn.click`. `python -m pytest`: **3322
  passed, 2 skipped** (up from 3292).

### Added — 2026-10-01 (Smart Mix / SFX Pool V1 — E)

Deterministic sound-design accents — impacts, risers, atmospheres, transitions and vocal shots —
placed into the **same** final audio master Audio Layers already produces. Three controls: an SFX
library folder, which roles are enabled, SFX Amount and SFX Level.

- **No second mix engine.** E is a second producer into D's existing graph. `AudioMixPlan` gained
  exactly one trailing defaulted field (`sfx_placements`), the executor gained one stream per
  placement, and there is still one `amix`, one `alimiter` and one exact-duration master.
  `video_processor.py` and `ffmpeg_processing.py` are untouched.
- **The original music stays the only audio BeatSync analyses.** SFX are planned *from* the
  finished `beat_info`, after the analysis and never fed back into it, so the cuts, the shot choices
  and the whole video edit are unchanged by adding sound design.
- **Folder = role, by an exact case-folded table** (`Impacts/`, `Risers/`,
  `Atmosphere/`|`Ambience/`, `Transitions/`, `VocalShots/` and their plural / `vocal shot`
  variants). No `contains`, no `startswith`, no punctuation rewriting and no classifier: unknown
  folders and root-level files are reported and ignored rather than guessed, so a typo is visible
  instead of silently becoming a role. `.wav .mp3 .flac` only — `.m4a` stays out for D's reason.
- **Seedless and deterministic.** Pools are ordered with the project's existing
  `input_manager.order_key` and consumed round-robin; the same library, track and settings always
  reproduce exactly. The reserved `"audio"` RNG domain remains unused — that is E2.
- **The asset cursor advances on every candidate attempt, not every success**, so one asset too long
  to fit cannot be retried at every later anchor and permanently block the rest of its pool.
- **Cross-role collisions are resolved by priority, never by moving an anchor.** Roles plan
  riser → impact → transition → vocal_shot → atmosphere; every accepted non-atmosphere interval is
  pairwise disjoint under half-open `[start, end)`, so a riser ending exactly where a transition
  begins is legal. Atmospheres deliberately underlay everything and never enter occupancy.
- **The impact percentile is taken over the whole aligned beat array**, with the bar-anchor mask
  applied afterwards — what the accepted calibration measured. The stdlib implementation reproduces
  `numpy.percentile`'s default method with a measured maximum difference of **0.0** on the real
  566-beat array, keeping `beatsync_fork` numpy-free.
- **SFX Amount is total over 0..100**: 0 is a hard off-branch; above it, continuous quantities
  interpolate between measured knots and clamp below the lowest rather than extrapolating, and
  integer caps quantise half-up. Risers and atmospheres follow the song's structure and ignore it.
- **SFX Level is a linear gain** (50 % = 0.50, never −50 dB) applied as one `volume=` per stream. It
  is execution state rather than plan data, so it rides as a separate argument and the frozen
  `SfxPlacement` shape carries no gain. Level 0 is a valid mute and does not disable planning.
- **SFX are neither ducked nor ducking.** The voice envelope multiplies the music only. With no
  voice, no unity envelope is synthesised at all — the music routes straight through, which also
  removes a full-length generated stream D was multiplying by one.
- **The whole enabled library is probed before Stage 1**, so a corrupt asset in an enabled role
  fails before any analysis is spent. Disabled roles, unknown folders and root-level files are never
  probed. Zero usable assets is fatal; one empty enabled role beside a valid one is not, and no
  musical outcome — no drops, no anchors, a collision, an asset that does not fit — is ever fatal.
  Those report zero/skipped placements with reasons instead.
- **Measured on the calibration track** with the frozen occupancy policy: amount 25 → 15 SFX,
  **50 → 19** (3 risers, 5 impacts, 6 transitions, 3 vocal shots, 2 atmospheres), 75 → 24,
  100 → 25, with **zero non-atmosphere overlaps** throughout. The default impact count is 5 rather
  than the 6 an earlier approximate ladder showed: one high-impact bar anchor falls inside the riser
  into the following drop and is skipped. That is the accepted consequence of riser priority and is
  pinned by a test. `tests/fixtures/nero_structure.json` carries the derived structure — beat times,
  anchor masks, the impact curve and the section table, **no audio** — so the suite reproduces the
  calibration without depending on the production media file.
- **Isolation.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `video_analysis.py`,
  `stage5_qwen_scene_worker.py`, `library_prep.py`, `creative.py`, `creative_recipe.py`,
  `variant_lab.py`, `presets.py` and `variation.py` are untouched. The SFX root is a runtime path
  and belongs to neither `CreativeProfile` nor `CreativeRecipe`. **No CLI flag** — GUI only.
- **Five GUI components** in one collapsed `🔊 Smart Mix / SFX` accordion, using a single
  `CheckboxGroup` whose choices carry the exact internal role name rather than a label-derived
  heuristic. No Scan button: the library is validated by the Create Music Video preflight. The
  report mirrors Audio Layers' R1-B lifecycle and has exactly one writer.
- **Three existing guards amended honestly, never evaded.** The pinned render click-input list
  legitimately gained four config widgets (its real property — no preparation state in the render
  request — is unchanged); the voiceless-graph test now pins the *stronger* property that no
  envelope machinery is built at all; and the success-panel guard gained `and prepared_voices`,
  since an SFX-only render also produces an `audio_plan`. `python -m pytest`: **3292 passed,
  2 skipped** (up from 3063; the two skips are the pre-existing Windows symlink-privilege ones).

### Added — 2026-10-01 (Audio Layers V1 — D)

Spoken voice over the music, with deterministic ducking, **without changing the edit the music
produced**. Five controls: voice clips, start delay, minimum gap, avoid drops, music under voice.

- **The original music stays the only audio BeatSync analyses.** `analyze_beats_auto` always
  receives `local_audio_path`; the mixed master is produced *from* the finished analysis and is
  never fed back into it. Tempo, the beat grid, sections, energy, cut selection, Qwen and the visual
  targets are all decided before any voice exists, so adding a clip cannot move a single cut. This
  is the most important test in D and it is asserted directly against the real call.
- **No voice clips is the exact legacy path** — a structural branch, not an equivalent WAV. No
  ordering, no ffprobe, no planner, no FFmpeg, no temporary file; `create_music_video` receives the
  same `local_audio_path` object it always did.
- **Deterministic filename order, never the browser's.** The HTML File API returns whatever the OS
  dialog supplies, which is not the user's click order, so voice clips are sorted with the project's
  existing `input_manager.order_key` total order. The help text says so and recommends `01_`, `02_`.
- **Placement is deterministic and seedless** — no RNG, no Variation Seed, no Master Creative Seed.
  Start delay 2.0 s, minimum gap 1.0 s, and a **bounded 4 s lookahead** for a nicer section anchor.
  The bound is measured: preferring the best section start *anywhere* threw the first clip 27 s
  forward and the second 83 s forward on the real track, stranding the rest of it.
- **Avoid drops means the whole spoken interval**, not just its start. Measured on real material, a
  start-only rule let a 12 s clip begin 0.44 s before a drop and put **11.56 s — 96 % of its
  speech — inside that drop**. Avoided types are exactly `drop` and `finale`; `hook` and `chorus`
  are energetic but common and excluding them would fail chorus-heavy tracks.
- **A clip is never dropped, truncated, overlapped or pushed past the end.** If no legal placement
  exists the render fails with the offending clip, its duration and the cursor — before any video
  clip is extracted.
- **Music under voice is a linear gain**, documented as such: 35 % means gain 0.35, never −35 dB.
  Attack/release are fixed at 250/400 ms, so the music is already at the floor when the first
  syllable lands.
- **Overlapping duck windows take the minimum gain**, so a short gap leaves the music continuously
  ducked instead of bouncing back up — and no hidden `min_gap >= attack + release` constraint is
  imposed. The FFmpeg expression is `max()` of duck amounts, never a chain of `if()` where whichever
  event matched first would win.
- **The envelope is a per-sample generated stream, not `volume`.** The obvious
  `volume=eval=frame` measured as a staircase — about five steps across the 250 ms attack, the
  largest a 0.22 linear jump (~2.2 dB), an audible zipper on sustained music — and `volume` has no
  per-sample mode. `aevalsrc` + `amultiply` evaluates per sample instead: measured deviation from
  the pure reference fell from **0.2245 to 0.0101**, and a realistic 280 s track still mixes in
  2.3 s.
- **A safety ceiling, not loudness normalisation.** Full-scale music plus a full-scale voice clips
  **7.9 % of samples** without a limiter. `alimiter=limit=0.97:attack=1:release=50:level=0:latency=1`
  removes it entirely. `latency=1` is load-bearing and was verified on this portable build before
  the filter was frozen: without it the limiter delays the whole master by 47 samples (0.979 ms);
  with it, impulses land on exactly the planned sample and a non-clipping fixture comes out
  **byte-identical** to the unlimited mix. `amix` uses `normalize=0` because `normalize=1` moved the
  same mix from −21.28 to −27.09 dBFS, changing the music level with the voice count.
- **One 48 kHz / stereo / `pcm_s24le` master, exactly the music's duration** — the same format final
  assembly already encodes, so the mux has no new work. Measured delta **0.0000 ms** for one voice,
  three voices, a voice ending at the music's end, and a voice deliberately overrunning it.
  Verified at ≤ 1 ms, which matters because `create_music_video` derives the frame timeline from
  this file.
- **The master is temporary and lives in `session_dir`** — never `get_processing_dir()`, which
  `create_music_video` clears at startup and would delete it moments after it was written. Fresh
  uuid path per render, removed in a `finally` on success and on failure. Voice sources are never
  copied or deleted; there is no persistent audio cache.
- **Two new modules.** Stdlib-only `beatsync_fork/audio_mix.py` owns the config, normalisation,
  placement and duck model; `src/audio_mixdown.py` owns ffprobe, the filtergraph and execution. The
  dependency is one-way. Seconds controls deliberately do **not** reuse `creative.normalize_control`
  — `2.5` is a valid delay — but keep the same explicit type boundary, and NaN/inf fall back rather
  than clamping so an infinite delay cannot become an enormous `adelay`.
- **Nothing else changed.** `video_processor.py` and `ffmpeg_processing.py` are untouched: the
  renderer already accepts an arbitrary audio path and all four branches consume it.
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and voice never reaches Stage 5, `CreativeProfile`,
  `CreativeRecipe`, `VariantLabConfig` or the preset recipes. Variant Lab audio integration is **E2**
  and is not pre-empted here. **No CLI flag** — ordered multi-file voice policy is not worth that
  surface in V1.
- Audio controls are live render-request inputs, absent from `source_outputs`, `prep_outputs`, every
  source and preparation handler and `live_declaration`, so changing one cannot clear a confirmation.
  One concise success-panel line when voice was used, and nothing at all when it was not.

### Fixed — 2026-10-01 (Audio Layers V1 — R1 correction)

Two narrow corrections before merge. The accepted architecture — the music-only analysis, the pure
planner, the duck model, the measured FFmpeg filtergraph, the `session_dir` master and the isolation
guarantees — is unchanged, and no measured expectation was retuned.

- **R1-A — a selected voice file could be silently dropped.** The preflight was handed
  `_as_existing_source_paths(voice_files) or voice_files`, and that helper *filters* to paths that
  still exist. Selecting `01`, `02`, `03` with `02` missing therefore rendered a plausible two-clip
  video instead of failing: exactly the silent-fallback outcome D exists to prevent, and worse than
  the music-only fallback because it still looked deliberate. `prepare_voice_inputs` now receives
  the raw selection and validates **all** of it — a usable path value, a supported extension, an
  existing readable file, a successful probe and a positive duration — raising `AudioMixError` on
  the first bad entry and **never** returning the valid subset. `_selected_voice_paths` normalises a
  scalar, `PathLike` or arbitrary iterable into a list without discarding anything, and the
  deterministic filename ordering is applied *after* validation with its length re-checked, so
  nothing can vanish in the sort either. Still raised before `analyze_beats_auto`, so a bad voice
  file costs no analysis. The helper's existing video-source use is unchanged — filtering is correct
  there, because the confirmation gate has already vouched for those paths.
- **R1-B — the placement report widget was dead.** `audio_layers_report` was declared with no
  writer and was not among the render event's outputs, so the advertised read-out could never
  display anything. A successful plan now populates it from the planner's own
  `audio_plan.report_lines()` — never recomputed in the GUI — through
  `session_state[AUDIO_LAYERS_REPORT_KEY]`, which `process_video_guarded` projects onto a fourth
  output. `process_video` keeps its three-value contract; only the outer handler became
  four-valued. The key is cleared at the start of every attempt and before the gate, so **no voice,
  a refused render, a preflight failure and a mixdown failure all leave it blank** rather than
  showing the previous render's placements. It remains pure diagnostics: nothing downstream reads
  it, and it stays absent from the render inputs, `source_outputs`, `prep_outputs`, every source and
  preparation handler and `live_declaration`.
- **One guard split, not weakened.** The invariant "no Audio Layers widget is written by any
  handler" became false for exactly one legitimate widget. The five *configuration* controls keep
  the full prohibition, and the report gained a named single-writer assertion (`process_btn.click`)
  plus an explicit check that no source, preparation, preset or Variant Lab handler touches it.
- The report lifecycle is proven by **executing the real extracted orchestration** from
  `_process_video_impl` against stubs, not by calling `report_lines()` in isolation — a hand-written
  mirror would keep passing after the production code stopped matching it. `python -m pytest`:
  **3063 passed, 2 skipped** (up from 3033; the two skips are the pre-existing Windows
  symlink-privilege ones). No cache contract, analysis version, planner, renderer or pure-module
  change: `src/beatsync_fork/audio_mix.py` was **not** modified.

### Fixed — 2026-10-01 (Variant Lab V1 — R1 correction)

Two narrow corrections to the Variant Lab PR before merge. The accepted core — `CreativeRecipe`,
named RNG sub-streams, golden vectors, the spread formula, range normalisation, the algorithm
version and the isolation guarantees — is unchanged, and **every golden vector is untouched**.

- **R1-A — the Master Seed help text was false.** It said "type a master seed you used before and
  Generate to get that exact recipe back", which is not true on its own: the base is the live
  sliders and Generate *writes the recipe back into them*, so an immediate second Generate resolves
  from the first recipe rather than the original preset. The resolver was always correct; only the
  wording over-promised. `INFO_MASTER_SEED` and `INFO_VARIANT_LAB` now state the real contract —
  a master repeats a draw only with the same starting values, ranges, ticked controls and spread,
  and the exact render settings are the Variation Seed plus the six sliders. **No cached base
  snapshot was added to hide the behaviour**: hidden state that disagrees with the visible sliders
  would be worse than the honest explanation. Pinned with exact values — Cinematic + master 582913
  + spread 50 → `55/15/60/63/22/62`, immediately again → `71/9/55/77/16/71`, restore Cinematic →
  `55/15/60/63/22/62`, with the clip seed `822019` throughout — plus a structural test that stops
  the help text reclaiming master-only reproducibility.
- **R1-B — `🎲 New Variant` now guarantees a different master.** `random_seed()` draws from
  1..999999 and can legitimately return the value already in the box, so "always mints a new
  master" was a probability rather than the product contract. `_fresh_variant_master_seed(previous)`
  draws once and, on a collision with a usable previous master, steps deterministically to an
  adjacent seed — one draw, never a retry loop waiting on `SystemRandom` to disagree. Randomness
  stays in the GUI; the pure resolver is unchanged. The old test relied on the draw simply not
  colliding; it is replaced by a monkeypatched forced-collision test, because no test here may
  carry a one-in-a-million failure mode.

### Added — 2026-10-01 (Variant Lab V1 — C2)

The seed is no longer the only exploration axis. Declare which of the six creative controls may
vary, within what bounds, and how far from your current settings — then generate **one**
reproducible recipe. It writes the Variation Seed and the six sliders, and nothing else.

- **One recipe at a time, and no render.** Multi-variant generation, batch rendering and variant
  comparison are C3 and are deliberately absent (a test asserts no such machinery exists). The user
  still presses Create Music Video.
- **Execution truth is unchanged.** A resolved recipe is written into the *existing* Variation Seed
  and six sliders; the pipeline keeps receiving only those seven values. No Variant Lab state
  reaches `CreativeProfile`, `beat_info["creative"]`, `process_video_guarded`, `render_info`, the
  filename or any stage — so there is no second planner interpretation path, and the resolved seven
  integers remain the durable artifact even if the generator is later retuned.
- **The base is always the live sliders.** No duplicate base controls and no cached snapshot: the
  six current values are read at click time, so Balanced explores around Balanced and a hand-tuned
  Custom explores around that. The preset *name* is never read. Ranges are explicit constraints and
  deliberately do **not** follow the base around; when the base sits outside one, the anchor clamps
  into it rather than raising.
- **Named RNG sub-streams — the load-bearing part.** Every control draws from its own stream keyed
  `"variant_lab|1|<master>|controls|<field>"` (SHA-1, first 12 hex digits, `hashlib` never `hash()`).
  A single sequential `Random` would have been simpler and is exactly what this must not be: adding
  one control later would shift every subsequent draw and silently invalidate every master seed a
  user had written down. Enabling another control, re-ranging another control, reordering the
  declarations, appending a future control and adding a whole future `audio` domain all leave a
  control's value — and the clip seed — untouched. Each property has its own test.
- **Golden vectors are pinned as literals** for the derived integers, clip seeds, per-control draws
  and whole recipes, and are recomputed independently rather than by calling the helper twice. A
  change to the namespace, separator, digest, hex slice or version position breaks a test instead of
  quietly producing different-but-plausible recipes.
- **Master Creative Seed is generator provenance**, distinct from the Variation Seed it resolves.
  `🎲 New Variant` always mints a fresh one; `✨ Generate Variant` reuses what is in the box, or
  mints one if it is unset. Either way the master is **returned as an output**, so it is on screen
  before it is used — `random_seed()` is the only non-deterministic call and it lives in the GUI,
  never in the pure resolver, which refuses to resolve without a positive master.
- **A master seed alone does not identify a recipe.** `MASTER SEED = generator provenance`;
  `CREATIVE RECIPE = the durable execution artifact`. A master repeats a draw only with the same
  base, ranges, randomize selection, spread and algorithm version. Since the base is the live
  sliders and Generate writes the recipe back into them, two Generates with one master
  intentionally differ — the second resolves from the first recipe. The seven resolved values
  remain the exact render settings.
- **Spread 0 is not a legacy render.** It freezes the six controls at their anchors but still
  resolves a *positive* clip seed, so clip selection still varies. The help text and a test both say
  so. A recipe can never carry seed 0 — that is the planner's legacy branch, and validation refuses
  it.
- **Spread is distance, not height.** `anchor + spread * u * (headroom in that direction)`, with
  explicit half-up quantisation (never `round()`, whose banker's rounding would collapse 2.5 and 3.5
  onto even values). The bias contract is stated precisely because the obvious phrasing is false:
  the anchor is the directional **median** and up/down is approximately a fair coin, but mean
  displacement is *not* zero when the anchor sits off-centre — base 30 in 0..100 has 70 points of
  headroom above and 30 below, so it drifts up, and a base on an endpoint can only move inward.
  Measured at spread 100 over 200 master seeds: 99% of recipes move controls in **mixed** directions
  and 2 in 200 move all six the same way.
- **Defaults: Spread 50, all six controls randomized, every range 0..100** — including Source
  Diversity, which is *not* quietly narrowed. Range-narrowing and spread are measurably redundant
  (`base ± 25 at spread 50` is numerically identical to `full range at spread 25`), so Spread is the
  single wildness dial and ranges express a genuine constraint. A test pins each default.
- **Two new stdlib-only modules.** `creative_recipe.py` is the seven-integer execution contract with
  **all-or-nothing** validation — one bad field rejects the whole recipe, because a half-applied
  mixture of a generator's output and silent 50s looks deliberate and is not. That is deliberately
  the opposite of `CreativeProfile.from_mapping`, which stays lenient because it reads a
  possibly-stale internal bus; both contracts are asserted side by side. `variant_lab.py` owns the
  named streams, the spread maths and every normalisation. Neither carries generator metadata into
  the recipe, so a future AI Director can emit one without inventing a master seed it never had.
- **No new slider handlers.** The PR3 preset event graph is untouched — one `.input()` per slider —
  and the lab's own thirteen config widgets register nothing at all; they are read at click time.
  Because programmatic writes do not fire `.input()`, the generate handler computes
  `matching_preset(resolved six)` explicitly, so the label never keeps claiming a stale preset. At
  spread 0 it correctly reads the base preset's own name.
- **`gr.RangeSlider` does not exist in Gradio 6.19.0** (verified against the installed package), so
  each control gets an explicit min/max `gr.Number` pair; a custom JS control was out of scope. One
  `gr.CheckboxGroup` carries the randomize selection, using `(label, value)` choices so the display
  stays human while the returned value is the exact field name — never derived from the label by a
  lowercase/replace heuristic.
- **Isolation.** Variant Lab widgets are absent from `source_outputs`, `prep_outputs`, every source
  and preparation handler, `live_declaration` and `process_btn.click`; the generate handlers write
  only the master seed, Variation Seed, six sliders, preset label and report, and start no render.
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `src/auto_mode/*`, `video_analysis.py`,
  `video_processor.py`, `creative.py`, `presets.py` and `variation.py` are byte-identical to main.
  **No CLI flag** — the CLI already exposes all seven resolved values, which are the reproducible
  execution contract.
- **The report says "Last generated recipe", never "Current".** The user may edit the seed or any
  slider afterwards; the sliders stay the execution truth and the read-out must not pretend
  otherwise.
- **Three existing guards were amended honestly, not evaded.** Two forbade `variant_lab` /
  `creative_recipe` vocabulary in `gui.py` as speculative — C2 implements them, so they left those
  lists while `presets.py` keeps the full prohibition (presets stay recipes-only with no generator
  concept). The third, which allow-listed the preset selector as the only writer of a creative
  slider, gained the lab's two buttons — and its *detection* was strengthened at the same time to
  resolve list variables transitively, so wrapping sliders in one more intermediate list can no
  longer hide a writer from it.

### Added — 2026-10-01 (Creative Controls Extra PR3: Creative Presets)

Four named recipes for the six 0–100 creative sliders, plus a `Custom` read-out. A **UI convenience
layer only**: no new planner behaviour, no new `CreativeProfile` field, no Stage-4/5/6 change, no
cache identity, no persistence.

| Preset | Cut Density | Micro Cuts | Semantic Emphasis | Energy Response | Motion Bias | Source Diversity |
|---|---|---|---|---|---|---|
| Balanced | 50 | 50 | 50 | 50 | 50 | 50 |
| Cinematic | 30 | 25 | **65** | 40 | 30 | 50 |
| Dynamic | 65 | 60 | 50 | 70 | 70 | 75 |
| High Energy | 100 | 85 | 50 | 85 | 80 | 80 |

- **The sliders remain the sole execution truth.** Selecting a preset writes those six values and
  then the name stops existing: it never reaches `CreativeProfile`, `beat_info["creative"]`,
  `process_video_guarded`, any stage, `render_info` or the filename, and the selector is deliberately
  **not** a `process_btn` input. The planner has no idea presets exist. A render is therefore still
  fully described — and still reproducible — by the six integers the user ended up with, which is
  also why the name is not persisted: if a recipe is ever retuned, an old render reproduces from *its
  recorded values* rather than from a historical label.
- **`Custom` is a state, not a recipe.** It means only "the six live values match no named recipe",
  so it has no entry in the table and selecting it writes nothing (`gr.skip()` per slider).
- **The selector is an honest read-out of the sliders, never remembered state.** Moving any slider
  recomputes the label from all six values via `matching_preset`, so a manual edit shows `Custom`
  *and* a manual edit back onto a recipe shows that recipe's name again. There is no "last selected
  preset" anywhere.
- **No event recursion, as a property of the event kind.** Both directions register on `.input()`,
  which Gradio 6.19 fires only for a *user* change (`.change()` also fires for programmatic
  updates). Writing the sliders from a preset therefore cannot re-trigger the slider handler, and
  vice versa. A test rejects any `.change()`/`.release()` binding on the seven widgets, because that
  is what an event loop would be built on.
- **Balanced is exactly current behaviour**, and resolves through the *existing* profile to `legacy`
  with no special branch anywhere in the pipeline. It is also the reset.
- **Seed independence.** The Variation Seed is in no recipe, is not an output of any preset handler,
  and the preset module has no seed concept at all. Randomize is unchanged (`inputs=[]`,
  `outputs=[variation_seed]`).
- **Not a diagonal through every slider.** Cinematic is the only preset that moves Semantic
  Emphasis — the only name that genuinely implies contextual reading — and the only one that leaves
  Source Diversity neutral, because at ~20 % fewer cuts the source-reuse pressure is already lower.
  Dynamic and High Energy leave Semantic Emphasis neutral: Stage 5 motion-gates semantic action, so
  on the action material a dense edit selects, moving it would be near-inert. High Energy is **not**
  "all sliders at 100" — exactly one control is at its limit.
- **Why High Energy's Cut Density is 100.** Cut Density has a pre-existing non-monotonic region
  around 72–82 on real material, so an intermediate value measured barely faster than Dynamic
  (density 90 → ~1.548 s average interval, only ~6 % shorter than Dynamic's ~1.650 s). 100 gives
  ~1.377 s, ~17 % shorter, which is what makes the strongest named pacing recipe actually distinct.
  Consuming that control's headroom is the accepted cost. **Cut Density itself was not modified.**
- **New stdlib-only `beatsync_fork/presets.py`**: an immutable table (`MappingProxyType` at both
  levels) and three total helpers. It imports no Gradio, no numpy, no planner, no filesystem — and
  deliberately not `creative.py` either: recipe values are plain literal integers, and the tests
  prove `normalize_control` returns each one **unchanged** rather than the module coercing them, so
  a typo is a test failure instead of a silently clamped slider.
- **`matching_preset` is total.** `None`, a string, a mapping, a wrong-length sequence, a `bool`
  element, a non-numeric element, `NaN`/`inf`, or an object whose `__iter__` raises — all answer
  `Custom`. It runs on live widget values during a UI interaction, so it may never raise. Floats are
  accepted because a Gradio slider reports `50.0`; `bool` is rejected first because it subclasses
  `int`. Preset names are matched **exactly and case-sensitively** (`"cinematic"` → `None`), so the
  displayed value and the applied recipe can never be two separately-decided things.
- **No CLI preset flag.** The CLI already exposes all six controls (`--cut-density`, `--micro-cuts`,
  `--semantic-emphasis`, `--energy-response`, `--motion-bias`, `--source-diversity`), and six
  explicit numbers are the reproducible contract. A `--preset` alias would add precedence ambiguity
  (`--preset X --motion-bias 70`) and recipe-name versioning ambiguity for no gain.
- **Source gate and preparation untouched.** `creative_preset` is absent from `source_outputs`,
  `prep_outputs`, every source and preparation handler, `live_declaration` and the render request, so
  preset interaction cannot clear a confirmation, disable Create Music Video, start a scan or disturb
  Media Library Preparation.
- **Nothing in the pipeline changed.** `src/auto_mode/*`, `src/video_analysis.py`,
  `src/video_processor.py`, `src/beatsync_fork/creative.py`, `variation.py`, `deterministic_view.py`
  and `library_prep.py` are byte-identical to main; `CACHE_CONTRACT_VERSION` stays
  `stage5_cache_v3` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`.
- **Two existing seam tests were amended, not weakened.** They previously asserted that a creative
  slider registers *no* handler and that *nothing* writes one. Each slider now registers exactly one
  — the preset-label sync — so the assertions moved to the stronger properties the originals were
  protecting: a control may have exactly one handler, it must be `.input()`, its only output is
  `creative_preset`, and the only widget permitted to write a slider is `creative_preset.input`.

### Added — 2026-10-01 (Creative Controls Extra PR2: Semantic Emphasis)

A seventh creative control: how strongly Stage 6 weights the persisted media-neutral semantic reading
against deterministic visual evidence. 0–100, **50 = current behaviour**, Stage 6 only.

- **0 does not disable Qwen.** Stage 5 is untouched, its records are untouched, and on a cold cache
  Qwen still runs and still writes the same semantics. The control changes only how an
  *already-analysed* library is interpreted at plan time. The UI help text says this explicitly.
- **The deterministic view is reconstructed, not read.** `_merge_semantic` fuses Qwen's reading into
  `quality_score`, `action_score`, `beauty_score`, `tension_score`, `soft_score` and `tags` **in
  place** and keeps no pre-fusion copy — so "deterministic evidence only" cannot be looked up. It
  *can* be recomputed: the six raw CV primitives (`motion`, `brightness`, `contrast`, `saturation`,
  `sharpness`, `colorfulness`) are never overwritten, and Stage 5's deterministic scores are a pure
  function of exactly those. New stdlib-only `beatsync_fork/deterministic_view.py` recomputes the
  pre-Qwen candidate forward from them — no inversion, no clamp undone, nothing inferred from the
  semantic side, and the input candidate is never mutated.
- **Stage 5 was deliberately not refactored to share the formulas.** Editing production Stage-5 code
  to suit a Stage-6 creative feature would risk changing persisted floating-point values and drag the
  cache contract into a PR with no business touching it. The duplication is the lesser risk
  *provided drift is loud*, so two independent defences pin it against the real Stage-5 source rather
  than against copied literals: a **fixed-point** test that runs the actual `_build_candidate` over a
  threshold-crossing matrix and requires the view to return its output unchanged, and a
  **quality-formula** test that extracts the arithmetic out of the window-measuring code and
  evaluates it (also asserting the CPU and CuPy metric paths still agree). A third test fails if
  `_merge_semantic` ever starts overwriting one of the six primitives the reconstruction depends on.
- **Formula and ordering.** `factor = 1 + ((e - 50) / 50)` → 0.0 / 1.0 / 2.0; effective score is
  `det + factor * (full - det)`, both sides through the *same* `_static_base_score` — there is no
  second scorer. Static order is now: legacy score → **Semantic Emphasis** → Energy Response →
  Motion Bias → one clamp. Energy Response blends against the **semantic-adjusted** flow score, so
  both sides of that blend describe one candidate under one interpretation.
- **L1A preserved.** Deterministic views are built **once per candidate per planner call** and reused
  for every target, for the flow column and for the per-clip diagnostic score. The table stays
  `candidates × distinct targets`; each cell costs two evaluations when the control is on. Neutral
  builds **zero** views and leaves the evaluation count exactly as it was — asserted by counting, not
  by inspecting the factor.
- **It invents nothing.** On a candidate Qwen never touched the deterministic view *is* the
  candidate, so the control is exactly inert — proved at every setting and every target.
- **Honest limitation.** The effect is target-dependent by construction: Stage 5 motion-gates
  semantic action, so on the real 509-candidate TEST1 pool the semantic/deterministic score
  divergence is mean |Δ| ≈ 0.143 on `soft` and 0.142 on `build`, but only ≈ 0.023 on `drop` and 0.029
  on `rhythm`. The factor was **not** retuned to manufacture drama on action material.
- **Real-material evidence (read-only, no cache write).** Over all 509 prepared TEST1 candidates,
  reconstructing deterministic quality and re-applying Stage 5's documented fusion reproduces the
  persisted `quality_score` with max error **exactly 0.0** on 509/509.
- **Stage 5 unchanged.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `src/video_analysis.py`,
  `src/auto_mode/stage5_qwen_scene_worker.py` and `src/beatsync_fork/library_prep.py` are
  byte-identical to main. No schema change, no migration.
- **UI / CLI.** One more slider in Creative Direction (0–100, step 1, default 50) and
  `--semantic-emphasis`. Render-request creative state: no handler, absent from
  `source_outputs`/`prep_outputs`, live `process_btn` input with pinned positional alignment.
  Randomize stays seed-only. No filename suffix — seed remains the only one.
- **Presets remain deferred** to their own PR.
- Files: `src/beatsync_fork/deterministic_view.py` (new), `src/beatsync_fork/creative.py`,
  `src/auto_mode/stage6_av_planner.py`, `src/gui.py`, `src/ui_content.py`, `src/video_processor.py`,
  `tests/test_deterministic_view.py` (new), `tests/test_semantic_emphasis.py` (new),
  `tests/test_creative_profile.py`, `tests/test_creative_controls_seam.py`,
  `tests/test_library_preparation.py`, `tests/test_no_runtime_dependency.py`.

### Added — 2026-09-30 (Creative Controls Extra PR1: Source Diversity, Micro Cuts)

Two more controls on the Creative Controls Core seam. `CreativeProfile` grows to six fields; both
new controls are 0–100 with **50 = current behaviour**.

| Control | Range | Neutral | Owned by | What it changes |
|---|---|---|---|---|
| Source Diversity | 0–100 | 50 | Stage 6 (**dynamic** half) | how hard the plan spreads across source videos |
| Micro Cuts | 0–100 | 50 | Stage 4 | the rare half-beat accent layer, and only that |

- **Source Diversity scales exactly two penalties.** `factor = 3 ** ((v - 50) / 50)` multiplies the
  planner's `-0.10` recent-source penalty and its capped `usage[video_file] * 0.012` source-usage
  penalty. The two **candidate-level** protections (`-0.28` for a recently used candidate id, and the
  capped `usage[id] * 0.10`) are byte-identical at every setting — diversity decides whether the edit
  returns to the same *source*, never whether it may repeat a *moment*. Deque lengths are unchanged.
  Base 3 is measured: on the real 509-candidate / 41-source TEST1 pool over 148 real segments it moved
  unique sources 31 → 39 and halved top-source usage 20 → 10 for a ~4 % mean legacy-score cost, with
  zero adjacent source repeats even at the reuse end; base 2 was visibly weaker and base 4 started
  producing adjacent source repeats at 0.
- **Source Diversity is dynamic and stays out of L1A.** It reads `usage`/`recent_videos`, so it is
  deliberately *not* in `ScoringControls` and not a key of the static table. A test asserts the
  `_static_base_score` evaluation count is identical at diversity 0 / 50 / 100, and that moving it
  does not trigger Energy Response's flow column.
- **Micro Cuts owns one layer.** It derives only `max_micro_cut_ratio`
  (`0.025 * 3^d`, hard-capped at 0.08) and `micro_percentile` (`96.5 − 6·d`, clamped to 90 … 99.9);
  `micro_min_gap` is untouched because it is the anti-flicker floor, and the `wave >= 0.88` gate
  inside `add_rare_micro_cuts` is untouched because measurement showed it is not the binding
  constraint. At **0** the layer is switched off via `enable_rare_micro_cuts=False` rather than scaled
  down — scaling alone cannot reach zero. Measured on the real Nero track: 0 → 0 extras (143 cuts),
  50 → 4 (147, the exact production baseline), 75 → 6 (149), 100 → 11 (154), minimum gap constant at
  0.464 s throughout.
- **Cut Density and Micro Cuts compose, in that order, and neither rewrites the other's fields.**
  Density shapes the main grid; Micro Cuts rewrites only the accent policy. `micro_cuts=50`
  short-circuits before the micro config is derived, so Cut Density's Core behaviour is bit-identical
  — a test recomputes it without any Micro Cuts involvement at densities 0/50/100 and requires exact
  arrays. The absolute accent count still scales with grid size (`max_extra` is a ratio of the
  selected grid); that pre-existing proportionality is intended and deliberately not "corrected".
- **50 is still today, exactly.** Every neutral control takes an explicit branch: `micro_cuts=50`
  passes the `CONFIG` singleton itself through (asserted by identity, not equality), and
  `source_diversity=50` runs the untouched penalty expressions (asserted at the source, so equality of
  results cannot hide a `× 1.0`).
- **Stage 5 untouched.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3`, `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`, and `src/video_analysis.py`,
  `src/auto_mode/stage5_qwen_scene_worker.py` and `src/beatsync_fork/library_prep.py` are byte-identical
  to main.
- **UI / CLI.** Two more sliders in Creative Direction (0–100, step 1, default 50) and
  `--source-diversity` / `--micro-cuts`. Both are render-request creative state: no handler, absent
  from `source_outputs`/`prep_outputs`, live `process_btn` inputs with pinned positional alignment.
  Randomize stays seed-only. No filename suffix — seed remains the only one.
- **Deferred deliberately:** Semantic Emphasis is *not* here, not even as a dormant field. It needs a
  deterministic candidate view reconstructed from Stage-5 primitives and carries a different
  drift-risk class, so it is its own PR. Presets follow after both have runtime acceptance.
- Files: `src/beatsync_fork/creative.py`, `src/auto_mode/__init__.py`,
  `src/auto_mode/stage6_av_planner.py`, `src/gui.py`, `src/ui_content.py`, `src/video_processor.py`,
  `tests/test_source_diversity.py` (new), `tests/test_micro_cuts.py` (new),
  `tests/test_creative_profile.py`, `tests/test_creative_controls_interactions.py`,
  `tests/test_creative_controls_seam.py`, `tests/test_cut_density.py`,
  `tests/test_library_preparation.py`.

### Added — 2026-09-30 (B0 + Creative Controls Core: Cut Density, Energy Response, Motion Bias)

Phase A's single creative control — the variation seed — is generalised into one resolved **Creative
Profile** carrying four controls, and three new ones are implemented on top of the boundary P2
established.

| Control | Range | Neutral | Owned by |
|---|---|---|---|
| Variation Seed | 0 / positive | 0 | Stage 6 — which of the good candidates wins |
| Cut Density | 0–100 | 50 | Stage 4 — how many beats become cuts |
| Energy Response | 0–100 | 50 | Stage 6 — how hard scoring follows the music's target |
| Motion Bias | 0–100 | 50 | Stage 6 — calm vs. dynamic source material |

- **50 is today, exactly.** `seed=0, cut_density=50, energy_response=50, motion_bias=50` reproduces
  current main's selected cut times, segment targets, candidate choices, legacy seed-0 RNG stream,
  plan length and output filenames. Every neutral control takes an explicit branch that calls the
  pre-existing legacy path rather than running the new arithmetic with a neutral coefficient, because
  even an operation-order change would make "upgrade and change nothing" untrue. Stage 4's neutral
  result is pinned against an independent recomputation of the pre-Core algorithm.
- **Mappings.** Cut Density: `f = 2 ** ((d - 50) / 50)`, so 0.5 / 1.0 / 2.0 at 0 / 50 / 100; minimum
  intervals and maximum holds divide by `f`, the global cut-ratio band multiplies by it (capped at
  0.95 / 0.98), beat steps re-quantise half-up into `[1, 8]`, and the weak-score breathing threshold
  scales by `1/f`. Energy Response: `factor = 1 + ((r - 50) / 50) * 0.6` (0.40 … 1.60), blending each
  target score against the same candidate's generic `flow` score. Motion Bias:
  `centered * 0.15 * (2 * motion - 1)`. Ordering is fixed and documented: legacy score → energy blend
  → motion shift → one clamp.
- **Stage 5 is untouched, and that is the point.** No control reaches `_qwen_config_token`,
  `_video_signature`, `_cache_path`, a Qwen request, the Qwen prompt or a persisted semantic payload.
  `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and `ANALYSIS_VERSION` stays
  `auto_av_analysis_v8_llama_vulkan_batched`; `video_analysis.py`, `stage5_qwen_scene_worker.py` and
  the preparation workflow were not modified. Cut Density *does* legitimately change
  `audio_visual_profile` (it changes `selected_beats`, hence `average_cut_interval`, `cut_count` and
  possibly `smart_preset`) — allowed, because since P2 that profile has zero executable effect inside
  Stage 5. A regression test proves two densities derive identical cache paths for the same sources.
- **L1A survives.** Energy Response and Motion Bias are render-scoped constants, so the static score
  table stays `candidates × distinct targets`; Energy Response adds one `candidates`-wide `flow`
  column and nothing else. Never `candidates × segments`.
- **UI / CLI.** Three sliders in Creative Direction (0–100, step 1, default 50) and three optional CLI
  flags (`--cut-density`, `--energy-response`, `--motion-bias`). All are render-request creative
  state: none registers a source-invalidating handler, so none can clear a source confirmation,
  disable Create Video, trigger a scan or touch Media Library Preparation. Randomize still writes the
  seed only. Filenames are unchanged — the existing `_seedNNN` suffix rule is the whole rule; the
  resolved profile is reported in the console, the success panel, `render_info["creative"]` and the
  plan summary instead.
- **Not implemented, deliberately:** Freestyle, an AI Director, Variant Lab, Creative Recipe
  persistence, per-section profiles, Micro Cuts, Source Diversity, Semantic Emphasis, L2 stage caching
  and a second Qwen pass. The future L2 invalidation boundaries are documented in `CLAUDE.md`, not
  built.
- Files: `src/beatsync_fork/creative.py` (new), `src/auto_mode/__init__.py`,
  `src/auto_mode/stage4_select.py`, `src/auto_mode/stage6_av_planner.py`, `src/gui.py`,
  `src/ui_content.py`, `src/video_processor.py`, `tests/test_creative_profile.py` (new),
  `tests/test_cut_density.py` (new), `tests/test_creative_scoring.py` (new),
  `tests/test_creative_controls_interactions.py` (new), `tests/test_creative_controls_seam.py` (new),
  `tests/test_creative_seed.py`, `tests/test_library_preparation.py`, `tests/test_scale_diagnostics.py`.

### Added — 2026-09-30 (Media Library Preparation: bounded analysis batches)

Preparation's Analyze click now submits one bounded batch of the outstanding sources instead of all
of them. Default **100**, exposed as an `Analyze batch size` control.

- **Why.** Stage 5 batches multiple videos into one shared Qwen worker, and that worker writes its
  response JSON only after its entire job loop finishes — so the parent can checkpoint completed
  semantic records only once the whole batch returns. A cold 1107-source library therefore exposed
  every source to a single all-or-nothing worker invocation. Bounding the submission bounds that
  interruption/retry exposure while still amortising one Qwen model load across each batch.
- **Not incremental worker checkpointing.** The Qwen worker was **not** modified and an interrupted
  in-flight batch does **not** resume internally — if its worker dies before producing a response,
  that batch may need repeating. The change limits how much work one failure costs, nothing more.
- **Not a library cap.** A 5000-source scan still reports 5000 needing analysis; `normalize_batch_size`
  has no upper bound and the number box carries no `maximum`. Scan still classifies the whole library
  in one pass (the P.1 rule, unchanged).
- **Batch size is execution policy, not classification identity.** It is absent from
  `LivePrepDeclaration`, `PrepScanResult` and `RuntimeIdentity`; `set_batch_size` keeps the recorded
  scan, so retuning the bound between Scan and Analyze needs no re-scan. Folder and recursive still
  invalidate. Analyze reads the **live** widget value; Scan does not receive it at all.
- **No cache or contract change.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v3` and
  `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`. `src/video_analysis.py` was not
  modified by this change at all, no cache payload gained a field, and persistence remains entirely
  with the existing `analyze_video_sources` → `_checkpoint_cache` path. P2 media-neutral semantics are
  unchanged.
- **Reporting.** The post-run summary states what was submitted and what was analysed for that batch
  and then asks for a re-scan; it never presents `old outstanding − N` as an authoritative remaining
  count, because sources can change on disk between clicks. No UI text claims a batch has a known
  duration.
- Files: `src/beatsync_fork/library_prep.py`, `src/gui.py`, `src/ui_content.py`,
  `tests/test_library_preparation.py`.

### Changed — 2026-09-30 (P2: persisted Stage-5 semantics are media-neutral)

Stage 5's Qwen semantics now describe the **video itself** rather than the video as seen through one
edit style. This establishes a deliberate architectural boundary: Stage 5 records intrinsic media
truth (persistent), while Stage 6 and any future creative/director layer interpret it for the current
project (ephemeral, per render).

- **The Qwen prompt no longer carries music or edit context.**
  `stage5_qwen_scene_worker._build_prompt()` takes no arguments and instructs the model to assess the
  moment only from what is visually present, explicitly not adapting its tags to music, song energy,
  edit style or pacing. The previous `"The music edit style is {smart_preset}."` conditioning is gone.
  The semantic schema is **unchanged** — same eight numeric keys, same `emotion` and
  `recommended_use` enums, same description contract, same greedy sampling — because the validation
  experiment tested the existing schema under a media-neutral prompt.
- **No audio profile crosses the worker boundary.** Neither the single nor the batch request JSON
  carries `audio_profile`; `_build_prompt` was its only consumer. Every private seam that existed
  solely to forward it lost the parameter. `analyze_video_sources(..., audio_profile=None, ...)` is
  retained as an integration signature with **zero** effect — it reaches no cache key, no request, no
  prompt and no persisted record.
- **`smart_preset` is out of cache identity.** `_qwen_config_token()` now takes no arguments and keys
  only `BEATSYNC_QWEN_MAX_WINDOWS`, `_FRAME_WIDTH` and `_MAX_NEW_TOKENS` — all three still re-key.
  `_qwen_prompt_style_hint` is removed. A source therefore needs one semantic analysis per compatible
  source identity + Qwen backend identity + media-semantic Qwen configuration, not one per preset.
- **`CACHE_CONTRACT_VERSION` moves `stage5_cache_v2` → `stage5_cache_v3`**, because a v3 record means
  something different from a v2 one. `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`
  (deterministic candidate scoring, window building and the candidate schema are untouched). There is
  **no migration**: v2 keys are never produced or looked up again, no v2 semantics are reused, and the
  old records are left on disk untouched — no compatibility loader, no rewriter, no automatic deletion.
  A one-off cold v3 rebuild is the intended cost.
- **Media Library Preparation is now trackless.** The audio-file control, the Stage 1–4 profile pass
  during a scan, `TrackIdentity`, the stored profile snapshot and the track staleness guard are all
  removed; preparation inputs are the folder and the recursive flag. One preparation serves every
  track, preset and creative seed, and the report states `Semantic mode: media-neutral`. The live
  stale-widget guard, the P.1 subset-only Analyze rule and the strict separation from the Create Video
  confirmation gate are unchanged. This removes the measured ~15–20 s track-profile component from
  every preparation scan.
- **Normal Create Video is unchanged.** Stages 1–4 still run and Stage 6 still receives `beat_info`,
  sections, energy, targets, the creative seed and the candidate tags/scores. Stage 6 scoring and
  planning logic were not modified.
- **Validated on real material before adoption** (23 analysed sources, 17 source groups, 10,913
  deterministic candidates, 115 identical A/B semantic moments): 115/115 decoded and tagged with zero
  failures on both sides; post-merge mean score differences ≤ ~0.023; planner seeds 0/101/202 with zero
  fallbacks and near-identical editorial scores; blind human review of 40 frames preferring the neutral
  side 10/16 decisive, with fewer editorial-leak and false-action flags. The conclusion is that
  media-neutral semantics are not materially worse on real content, remain equally usable by Stage 6,
  and stop music/edit intent leaking into persisted media semantics.
- No Freestyle, Director, Hybrid or Neutral/Music-aware mode is implemented here, and no speculative
  abstraction was added for them. `tests/test_media_neutral_semantics.py` covers the prompt, the
  request payloads, the identity invariant, the v2→v3 boundary and the creative-state boundary.

### Added — 2026-09-29 (Creative Phase A: user-controlled variation seed)

A **Variation Seed** in the UI (plus `--seed` on the CLI) that produces a different but reproducible
clip plan from the same analysed source library.

- **Seed 0 is legacy and is the default.** Stage 6 keeps its existing argmax, its existing penalties
  and its existing `_stable_rng(index, target, start)` jitter stream, unchanged. Upgrading and
  leaving the box alone cannot change anyone's edit merely because this feature now exists, so seed 0
  never reaches the new selection rule. Output filenames for seed 0 are also unchanged.
- **A positive seed changes the winner rule only.** Scoring and penalties are untouched; the planner
  takes the best 6 candidates, drops anything more than 0.12 below the best, and makes a seeded
  weighted draw favouring the near-best. The previous `rng.random() * 0.015` jitter was far too small
  to make a user-facing seed useful — that is what this replaces, for positive seeds only.
- **The seed is creative state, never analysis identity.** It rides on `beat_info["creative"]`, which
  Stage 6 already receives. `video_analysis.py` is unmodified; `CACHE_CONTRACT_VERSION` and
  `ANALYSIS_VERSION` are unchanged; the seed is absent from every cache-identity function and from
  `audio_visual_profile` (whose `smart_preset` *is* keyed into the Qwen config token). Changing the
  seed re-plans; it never re-analyses. It is likewise not wired into any source-confirmation handler,
  so it cannot clear a confirmed source set.
- New stdlib-only fork module `src/beatsync_fork/variation.py` owns seed normalisation and the
  selection rule; `tests/test_creative_seed.py` covers it, the planner wiring and both boundaries.

### Fixed — 2026-09-28 (R2: individually valid telemetry could still sum to Infinity)

Follow-up to the telemetry-boundary work below, from review of that change. R1 validated every
telemetry scalar individually — finite, non-negative, real `int`/`float` — and made the **final library
aggregate** overflow-safe via `_telemetry_total`, then claimed that newly written records carry no
non-finite optional telemetry. Two earlier sums were still using raw floating-point addition, so that
claim did not hold: `1e308 + 1e308` is `inf` from two values that each passed the R1 contract.

Reproduced against the R1 head with the real extracted production bodies:

1. **Per-job batch telemetry.** `qwen_seconds = prefetch_seconds + inference_seconds +
   amortized_model` produced `inf` from `prefetch_seconds = inference_seconds = 1e308`, and — because
   completion is correctly independent of telemetry — the job was still complete, so the record was
   checkpointed with **`"qwen_seconds": Infinity` on disk** and stayed reusable. That is precisely the
   value R1 set out to keep out of a newly written record.
2. **Current-run inference total.** `run_stats["qwen_inference_seconds"] += inference_seconds` reached
   `inf` from two jobs each reporting an individually valid `1e308`, in the shared-batch path and,
   across two successive calls, in the single/inline path. `qwen_inference_seconds_this_run` could
   therefore still be `Infinity`, contradicting the published "current-run floats stay finite" rule.

**Fixed** by routing those combinations through the **existing** `_telemetry_total` — no new helper,
and a test asserts it remains the only telemetry summer. The per-job sum becomes
`_telemetry_total((prefetch_seconds, inference_seconds, amortized_model))`; both current-run inference
accumulations become `_telemetry_total((running_total, contribution))`. The two parent-measured
`run_stats["qwen_seconds"]` accumulations were routed through it as well: they are `perf_counter`
deltas and were never at risk, so this is behaviour-preserving for every reachable value, but the
invariant is then structural instead of resting on an argument about how large a monotonic-clock delta
can be. Current-run wall time is still parent-measured and still counted once per worker invocation.

Healthy telemetry is untouched — `1.0 + 4.0 + 2.0` is still exactly `7.0`, multi-job totals still add
up (`4.0 + 6.0 = 10.0`), and a legitimate zero sum is still zero. Nothing about the R1 number contract
changed: no wall-time ceiling was reintroduced, numeric strings and bools are still rejected, and
`_is_real_number` / `_optional_telemetry_number` / `_telemetry_seconds` / `_bounded_count` /
`_is_nonnegative_count` / `_telemetry_text` / `_as_mapping` / `_record_telemetry` /
`_record_candidate_count` are unchanged. Completion, cache identity, checkpointing and semantic merge
are untouched; `CACHE_CONTRACT_VERSION` stays `stage5_cache_v2` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`; no re-key, no migration, no cache rewrite.

`tests/test_qwen_scalar_boundary.py` gains 14 tests. Six of them are red against the R1 head and green
after the fix (per-job stored sum, the on-disk `Infinity`, the amortized-model share, and the batch and
inline current-run totals); the rest are regression guards for healthy values, legitimate zero,
wall-time provenance, and record reusability. One is structural rather than behavioural: it walks both
orchestration bodies and fails on any augmented assignment to a float telemetry key, because a *new*
unsafe sum being added later is exactly the failure mode that survived R1's own review.
`tests/test_stage5_cache_completion.py` adds `_telemetry_total` to its AST extraction tuple — the
orchestration bodies now call it, so its previous "aggregation-only" note is corrected there.

### Fixed — 2026-09-28 (Qwen telemetry trust boundary: optional metadata can no longer crash or poison Stage 5)

**Scope note first, because it matters for how this reads.** This is boundary robustness, not a report
that the shipping worker misbehaves. `stage5_qwen_scene_worker.py` cannot emit any of the malformed
shapes below: it reports `time.perf_counter()` deltas, `max(1, min(32, int(slots)))` and a literal
`0.0` for VRAM. The exposure is the request/response and cache JSON that is **deliberately retained**
under `input/video_analysis_cache/` (and therefore visible and editable), plus any future worker change
or regression. The worker was not modified.

**The defect.** Optional Qwen telemetry — durations, VRAM, batch size, model id — was converted with
bare `int()`/`float()` at two independent boundaries, while the semantic-completion contract never
reads any of it.

1. **Worker-response ingestion.** `float(timing.get("inference_seconds") or 0.0)` in
   `_complete_deferred_qwen_batch` raised `ValueError` on a non-numeric string — the case originally
   reproduced while testing the Stage-5 reporting work, and deliberately left unfixed there. The same
   shape reached `model_load_seconds`, `prefetch_seconds`, `batch_size` and `peak_vram_gb`, in both the
   shared-batch and the single/inline path. The batch call site is unguarded, so a telemetry field
   could abort Stage 5 after real GPU minutes; the inline path *is* wrapped in `except Exception`,
   which was worse — a malformed `batch_size` turned a fully tagged source into `ai_enabled=False`,
   i.e. permanent re-analysis caused by a field no completion rule reads.

2. **Library aggregation.** The tail of `analyze_video_sources` reads the same kinds of field back out
   of every returned record, and on a warm run **every one of those records is a cache hit**. A record
   can satisfy `_cache_entry_is_complete` *and* `_stored_ai_cache_is_consistent` while carrying
   malformed optional telemetry, because neither rule looks at durations, VRAM, concurrency or the
   model id. Such a record stayed reusable — so nothing ever recomputed it — and crashed or poisoned
   the aggregation on every subsequent warm run. A poison pill.

**The NaN/Infinity half is the dangerous half, and it is real rather than theoretical.**
`json.loads`/`json.load` accept the bare `NaN`, `Infinity` and `-Infinity` tokens through Python's
default non-standard `parse_constant`, and `json.dump` emits them because `allow_nan` defaults to
`True`. A non-finite value therefore survives the worker response file *and* a full cache round trip,
`float()` will not reject it, and one of them makes every sum it enters non-finite **silently and
permanently**. Two tests demonstrate this end to end rather than asserting it.

Secondary findings, all reproduced: `_is_count` admits negatives (`_is_count(-5)` is `True`), so a
worker-reported `frame_count: -5` reached `qwen_frame_count_this_run`; `int(2.7)`/`int(True)` laundered
a float and a bool into a fabricated concurrency; `str(...)` turned `123` and `{"a": 1}` into invented
model ids `"123"` and `"{'a': 1}"`; `qwen_concurrency`'s `next(...)` converted only up to the first
truthy record, so whether Stage 5 crashed depended on where a malformed source happened to sort; a
truthy non-dict `timings` raised `AttributeError` before any scalar was read; and a truthy non-dict
`semantics_by_job` slipped past the empty-response branch and then raised on `.get`.

**Fixed.**

- **Counts and optional telemetry now have explicitly different trust contracts.** `_is_count`,
  `_coerce_count` and `_reported_count` are load-bearing for completion and are **unchanged** — a
  frozen D1 contract. A separate seam handles telemetry: `_is_real_number`,
  `_optional_telemetry_number`, `_telemetry_seconds`, `_telemetry_total`, `_is_nonnegative_count`,
  `_bounded_count`, `_telemetry_text`, `_as_mapping`, `_record_telemetry`, `_record_candidate_count`.
- **An accepted telemetry number must be a real `int`/`float`, not a `bool`, finite and `>= 0`.** A
  numeric string is *not* accepted: `float("2.5")` succeeding is not a reason to believe a string in a
  numeric field, and laundering it would hide a broken producer. Negatives are rejected because every
  field here is physically non-negative — and because `_fmt_seconds` already clamps display with
  `max(0.0, ...)`, so a negative stayed invisible on screen while still corrupting the total.
- **Semantic completion never depends on optional telemetry**, which is now pinned by test rather than
  by convention. 3 requested / 3 decoded / 3 returned for exactly the requested ids stays complete,
  checkpointable and cached even when `inference_seconds` is `"not-a-number"`, `NaN` or negative, and
  when `batch_size`, `peak_vram_gb`, `model_load_seconds` or `model_id` are malformed.
  `_qwen_job_completed`, `_stored_ai_cache_is_consistent`, `_cache_entry_is_complete` and
  `_checkpoint_cache` are untouched; a test asserts none of them reads a telemetry field or helper.
- **Unknown is kept distinct from a legitimate zero.** An unprovable `peak_vram_gb` is stored as
  `None`, not `0.0`, precisely because the worker reports a real `0.0` — coercing would make a corrupt
  record indistinguishable from every healthy one. A genuine `0` still reads as `0` everywhere.
- **No aggregate can be non-finite, including from individually finite parts.** Filtering NaN/Inf per
  value is not sufficient: enough finite values overflow a running total. `_telemetry_total` checks the
  accumulator and degrades to the neutral `0.0` rather than reporting `inf`. No plausible figure is
  ever fabricated to keep a number finite — unknown is preferable to false precision.
- **Every telemetry sum goes through that aggregator, not only the final library one.** Validating the
  individual scalars is necessary but *not* sufficient, and the first cut of this work got that wrong —
  see the R2 correction below.
- **Counts respect their natural bound.** Current-run decoded frames count only when the worker
  reported a real non-negative integer `<= ` the candidates that job actually submitted, so a claim
  larger than the request is not evidence. Library totals are bounded by the record's own candidate
  count, so a candidate-less record with hand-edited huge counts cannot inflate them.
- **Nested containers are normalised, the top-level one is not re-checked.**
  `beatsync_fork.qwen_progress` already proves the loaded response is an object and was not modified.
  A malformed `semantics_by_job` now degrades to the same outcome as no usable semantics: the job stays
  attempted, stays incomplete, nothing is invented, Stage 5 survives. Collapsing a non-dict to `{}` can
  only make a completion check *fail*, never pass.
- **Old cache files are tolerated on read, never rewritten.** There is no migration and no cache
  rewrite; a valid entry round-trips byte-identically. The fix for the poison pill is that the
  aggregation reads tolerantly, not that the loader rejects more.
- **New records are sanitised before they are written**, so nothing malformed reaches a cache payload —
  done at ingestion, not by making `_save_cache` reject a record and not by changing
  `json.dump(allow_nan=...)`. The writer is not the semantic-policy layer.

**Current-run reporting invariants are preserved.** `qwen_seconds_this_run` stays the parent's own
`perf_counter` measurement, counted once per worker invocation, and is untouched.
`qwen_jobs_this_run`, `qwen_requested_count_this_run`, `qwen_tag_count_this_run` and the
completed/incomplete tallies stay parent-derived. `qwen_frame_count_this_run` and
`qwen_inference_seconds_this_run` are hardened as above. The requested/decoded/tagged split and the
`tagged / requested` UI denominator are unchanged; `src/gui.py` needed no change.

**No wall-time ceiling on durations, and that is deliberate.** Bounding a worker-reported duration by
the parent's measurement of the call looks attractive, but it makes the value depend on how long the
surrounding call happened to take, so a stubbed or replayed worker — the only way this code is testable
without a GPU — has every legitimate duration silently rejected. Finiteness and sign are what make the
aggregate safe. Counts keep a bound because they have a real one in the same scope.

**No cache-contract bump and no analysis-version bump.** `CACHE_CONTRACT_VERSION` stays
`stage5_cache_v2` and `ANALYSIS_VERSION` stays `auto_av_analysis_v8_llama_vulkan_batched`. Semantic
output meaning, candidate scoring, the candidate schema, cache identity and the completion contract are
all unchanged; this is optional-telemetry sanitisation plus tolerant reading. Reading is strictly *more*
tolerant, so no record that current main accepts becomes unreadable, and no stored value is
contradicted.

`tests/test_qwen_scalar_boundary.py` is new and covers the malformed-shape matrix at both boundaries,
completion independence, the nested-container cases, the NaN/Infinity poison pill against a genuinely
reusable synthetic record, aggregate finiteness and order-independence, legitimate zero, the frozen D1
count semantics, and cache isolation. No model inference, no production run and no runtime-cache write
was involved in authoring or validating it.

### Fixed — 2026-09-28 (Stage 5 reports current-run work, not cached-library history)

**The defect, observed in production.** A fully warm run — 845/845 cache hits, **zero** sources
re-analysed, **zero** Qwen workers launched, zero model inference, zero Stage-5 cache writes, all of
it independently verified — printed a Stage-5 summary reading `Source videos: 845, visual workers: 1`,
`Qwen: enabled, model Qwen3VL-2B-Instruct-Q8_0 (llama.cpp Vulkan), batch 4`,
`Qwen performance: batch 4, 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`. Every one of
those figures was loaded from cached records and described historical work. The five-line CMD budget
was then exhausted, dropping the one line that actually described the run:
`Analysis time: …, cache 845/845`.

This was a reporting and observability defect only. The cache, the completion contract and the Qwen
runtime all behaved correctly; nothing about stored results was wrong.

**Root cause.** Three independent issues:

1. the top-level `qwen_*` aggregates sum or select over `videos`, which includes every cache hit, so
   on a warm run they are pure history — yet the UI rendered them as current performance;
2. `_video_analysis_workers(0)` returns `1` (its `video_count <= 1 → 1` contract), and `workers` was
   computed unconditionally even though the analysis block is guarded by `if jobs:`, so the summary
   claimed one analysis worker where none was used;
3. the five-line budget dropped `Analysis time …` because the historical Qwen lines came first.

A fourth, smaller issue found in the same audit: the Stage-5 START event fires *before* the cache
scan yet announced `Analyzing N source video(s)` — stronger than the system can know at that point.

**The change — additive, ephemeral, cache-neutral.** Stage 5 now returns explicit current-run
execution facts alongside the untouched library aggregates: `sources_analyzed_this_run`,
`analysis_workers_used`, `qwen_jobs_this_run`, `qwen_completed_jobs_this_run`,
`qwen_incomplete_jobs_this_run`, `qwen_frame_count_this_run`, `qwen_tag_count_this_run`,
`qwen_seconds_this_run`, `qwen_inference_seconds_this_run`. They come from an invocation-scoped
`_new_run_stats()` dict threaded only into the paths that analyse an uncached source, so a cache hit
cannot inflate them and a second call in the same process starts from zero. The existing aggregate
fields keep their values and meaning for compatibility; the UI simply stops presenting them as
current work.

**The accounting invariant, which is subtler than it looks.** `qwen_jobs_this_run` is deliberately
**not** `len(deferred_jobs)`. That would be wrong in both directions: the serial path
(`_analyze_single_video` with `defer_ai=False`) runs Qwen **inline** and never appears in
`deferred_jobs`, while a deferred source whose candidate list is empty passes through batch
orchestration without ever reaching the worker. Counting therefore happens at the two places that
genuinely submit work — immediately after `_run_qwen_worker` in the facade, and in the batch's
per-job merge loop, which only runs for sources that made it into `request_jobs`. A configured skip
(`BEATSYNC_QWEN_MAX_WINDOWS=0`) and a candidate-less source both correctly count as zero jobs. A job
that ran but did not complete is still counted, and separately tallied as incomplete, so a failure
can neither masquerade as success nor disappear.

Both recording sites are written inline rather than via a shared helper, because
`_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` are AST-extracted and executed
by `tests/test_stage5_cache_completion.py` and must stay self-contained.

**Worker truth.** `_video_analysis_workers` is unchanged — its one-job contract is relied upon — and
the call site now reports the workers actually used (`… if jobs else 0`).

**START wording.** `Analyzing N source video(s)` → `Checking N source video(s)`. The post-scan metric
(`H cached, J to analyze, W worker(s)`) remains the authority on real work.

**Console summary.** `_stage5_summary` is reordered so current-run truth wins the five-line budget:
sources/cache/analysed → current-run Qwen status → `Analysis time …, cache H/N` → library summary →
optional cached metadata, explicitly labelled as cached. The budget itself is **not** raised; it was
never the bug. Warm runs now read
`Sources: 845, cache 845/845, analyzed this run 0` / `Qwen: enabled, no inference this run`;
mixed runs read `Qwen this run: 2 job(s), 20/20 tags, in 14.0s` rather than the library's 8704/8704.

**No cache impact.** `CACHE_CONTRACT_VERSION` stays `stage5_cache_v2` and `ANALYSIS_VERSION` stays
`auto_av_analysis_v8_llama_vulkan_batched`. The new fields are top-level return metadata on a dict
that is separate from `video_data`, so they cannot reach `_checkpoint_cache`; a test pins that. No
cache re-key, no source-cache payload change, no Qwen request-schema change, no semantic-output
change, no candidate-scoring change, and no completion/checkpoint change. The 845 existing records
remain reusable.

**Not fixed, and explicitly out of scope.** Production measured **~69 s** inside Stage 5 on that warm
run — `total_elapsed` brackets the whole `analyze_video_sources` body and flows to the END event's
`elapsed_seconds`, so it was a genuine Stage-5 measurement. Later profiling, run once the same
~2.52 GB of bounded fingerprint windows were already in the OS cache, measured **~3.2–3.4 s**. The
discrepancy is **unresolved**; this work changes reporting truth, not Stage-5 performance.

**R2 — failed Qwen attempts are still current-run work.** Review of the first cut found a real
defect in the new accounting: `_complete_deferred_qwen_batch` recorded only inside its per-job merge
loop, which sits *after* the early return taken when the shared worker produces no usable response.
A worker that timed out, exited non-zero or returned unreadable output therefore reported
`qwen_jobs_this_run = 0`, and the UI rendered `Qwen: enabled, no inference this run` — false, since a
real attempt had been made on real sources. Batch accounting is now two-phase: **submission truth**
(jobs, requested candidates, the one measured shared-worker wall time) is recorded the moment
`_run_qwen_worker_batch` returns, before the empty-response branch, with every submitted job starting
incomplete; **response truth** (completion, decoded frames, merged tags, inference seconds) is
applied per job afterwards and deliberately does not re-count the job, which would double-count every
success.

R2 also separates three facts that the first cut conflated. `qwen_requested_count_this_run` (new) is
what was submitted, `qwen_frame_count_this_run` is what the worker proved it decoded, and
`qwen_tag_count_this_run` is what was actually merged. The UI's `N/M tags` denominator is now the
**requested** count: previously a job that requested 10 candidates and decoded only 8 rendered as a
flawless `8/8`, hiding the two that never arrived, and on a worker-level failure there was no decoded
count at all. Current-run decoded frames are counted only when the worker reports a real integer —
the persisted `timings["qwen_frame_count"]` keeps its legacy fallback to the requested count for
source-record compatibility, and that fallback is now prevented from leaking into current-run truth.
Current-run wall time counts each worker invocation once (one `batch_seconds`, or one single-call
duration), never the amortized per-source figures, which scale with source count.

Changed: `src/video_analysis.py`, `src/gui.py`, `tests/test_stage5_reporting_truth.py` (34 tests),
`CLAUDE.md`, `CHANGELOG-FORK.md`. Suite 651 passed / 2 skipped (617 + 34 new; same two pre-existing
`WinError 1314` symlink skips). The R2 tests execute the real `_annotate_candidates_with_qwen` and
`_complete_deferred_qwen_batch` bodies with only the worker subprocess stubbed — the first cut
asserted on shape and so never exercised the failure path it got wrong.

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

Changed: `src/auto_mode/stage5_qwen_scene_worker.py`, `tests/test_qwen_semantic_recovery.py` (24 tests),
`CLAUDE.md`, `CHANGELOG-FORK.md`. Suite 617 passed / 2 skipped (593 + 24 new; same two pre-existing
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
