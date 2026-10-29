---
paths:
  - src/beatsync_fork/analysis_cache.py
  - tests/test_analysis_cache_sharing.py
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# The shared Stage-5 analysis cache (FUN Shared Analysis Cache)

## The problem, and why it was never a migration

The cache directory was derived from `logger.ROOT_DIR`, so every checkout got its own
`input/video_analysis_cache`. Production had thousands of records; a fresh playtest tree had none,
and re-analysing the same library from cold is measured in **hours**.

The records were always portable. Stage-5 identity is
`contract | version | abspath | size | mtime_ns | fingerprint | backend | qwen-config`, and **none of
those components is the directory** — so a record written by one installation was always lookupable
by another under the identical filename. Only its *location* was installation-specific.

That is why this feature copies nothing, moves nothing and rewrites nothing. It points several
installations at one folder. `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and no
record was invalidated. The identity half of the contract is
`.claude/rules/stage5-cache-identity.md` ("The directory is not identity"); this file owns the
setting, the resolution and the import.

## The configuration lives outside every checkout, and that is the feature

```
%LOCALAPPDATA%\BeatSync\shared-config.json      per user, per machine, outside every version
```

**Storing the path in a checkout's own `state/preferences.json` would not solve anything** — a new
playtest tree would still start blind, which *is* the problem. `LOCALAPPDATA` rather than a roaming
location because a cache folder is a property of this machine's disks: roaming it would propagate a
path that may not exist elsewhere.

Two keys, deliberately independent:

```
video_analysis_cache_folder    ""      a path, remembered even while sharing is off
video_analysis_cache_enabled   false   the switch
```

They are separate so that turning sharing off does not make the operator retype a path that took a
Browse dialog to pick, and so an empty path cannot read as "off" when it really means "not set up
yet". Defaults reproduce the pre-feature behaviour exactly.

The switch is read **tolerantly** — the file is human-editable, so a hand-written `"yes"` works and
anything unrecognised means *off*. The failure mode of a misread switch must be "my shared cache was
not used", never "BeatSync would not start". Only the known keys are ever written, so a stray value
in a caller's dictionary cannot be persisted. The write is `mkstemp` + `fsync` + `os.replace` in the
same directory, the pattern `user_prefs`, `qwen_partials` and `_save_cache` already use.

A **missing** file is a first run and reports nothing. A file that is present but **unreadable**
reports, for the same reason `user_prefs` does: silently falling back to the defaults would look
identical to a first run and the operator would never learn their shared cache had stopped being
used.

### `BEATSYNC_SHARED_CONFIG` is a requirement, not a convenience

Every reader and writer goes through `analysis_cache.config_path()`, which honours the override
first. Without it a test suite would read and write the **operator's real** configuration — the one
their production install and every playtest depend on — and a test that saved a cache folder would
silently re-point production at a temporary directory that pytest then deletes.

`tests/conftest.py` pins it **session-wide and autouse**, before any test runs. Deliberately not a
per-test fixture a suite has to request: forgetting it would be invisible on a machine with no shared
config and destructive on the owner's.

## Resolution is once per invocation

`video_analysis.resolve_cache_location()` is the **one** resolver, and the GUI reaches the same one
(through a local import, since `gui.py`'s module-level import order is load-bearing). A Settings
panel that computed the location independently could show one folder while the renderer used
another — the most misleading thing this feature could do.

It returns a frozen `CacheLocation` of plain strings and bools, so it is deepcopy-safe and carries no
handle to anything:

```
path        the directory to use -- ALWAYS usable; falls back to the local default on every failure
source      "local" | "shared"
configured  what the operator asked for, whether or not it worked
requested   did the operator ask for a shared cache at all
available   did they get it
notice      why not, in words, or ""
```

`path` always being usable and `available` being separate is the point: refusing to analyse would be
a worse outcome than analysing locally, but the half that must be *reported* is whether the operator
got what they asked for.

### The parent-folder rule is the whole safety argument

```
J:\BeatSyncCache  and  J:\       exists   ->  CREATE the folder and use it
J:\BeatSyncCache  and  J:\   does NOT exist  ->  REPORT it; use the local cache; create NOTHING
```

Same shape of input, same switch, opposite outcome — decided only by whether the parent exists. A
mistyped or new leaf on a live disk is recoverable and is created, like every other cache directory
in this app. A missing ancestor means the drive is unplugged or the share is down.

**Creating the folder unconditionally would turn the second case into a silent cold start against an
unexpected empty location** — an hour of re-analysis with nothing on screen to explain it. That is
the specific outcome the owner asked to be *told* about. The notice names the configured path, names
the local fallback, and says what the fallback costs ("sources analysed before will be analysed
again"). A permission failure is reported with its reason rather than swallowed.

## Record recognition is by shape, never by exclusion

A record filename is `{8 hex}_{24 hex}.json` — two truncated lowercase SHA-1 digests from
`_cache_path`. `is_record_name` matches that shape and nothing else, which is pure string work and
touches no disk.

**Matching the shape rather than listing what to exclude is deliberate.** The same directory also
holds the retained `qwen_batch_request_*.json` / `qwen_batch_response_*.json` and the A1 per-job
`*.partial.json`, all of which are **intentionally** kept for debugging. An exclusion list would need
updating every time the worker gained an artifact, and a missed one would be copied into a shared
folder or counted as an analysed video. So a file type this module has never heard of is skipped by
default.

`measure()` is a **flat** listing, not a walk: a Stage-5 cache has no subdirectories, and `os.walk`
descends an NTFS junction — the measured hazard `cache_admin` has to prune for. A counter has no
business following a reparse point. A missing folder is a first run with zero counts, never an error;
an unreadable or vanished entry is skipped rather than failing the whole count, because a shared
folder legitimately has other processes writing into it.

## Import is additive, one-way, and never overwrites

`import_records(source, destination, contract)` copies reusable records in and stops there.

```
copied            a new record arrived at the destination
already_present   the destination already had that name -- KEPT, byte for byte
incompatible      a different cache generation, unreadable, no source path, or still deferred
failed            the copy itself failed
skipped           non-record files, never copied
```

- **The source folder is never modified.** There is no write, rename or delete aimed at it, which is
  what keeps a production cache byte-identical after being imported from.
- **The no-replace guarantee is the exclusive create itself** (`O_CREAT | O_EXCL`), not a preceding
  `exists()` check — so there is no window between asking and acting. Same reasoning as
  `_publish_share_no_replace` and `collision.publish`.
- **A failed write removes only its own attempt**: the name was claimed by that very call moments
  earlier, and no reader could have accepted a zero-length record as valid.
- **The contract string is a parameter, not a constant here**, so this module cannot drift from
  `CACHE_CONTRACT_VERSION`. A record of an older generation is reported `incompatible` rather than
  copied: its filename could never be looked up again, so copying it would move dead weight into a
  shared folder.
- **Importing a folder onto itself is refused**, compared by `normcase(abspath(...))` rather than by
  string, because a no-op reporting "all already present" looks like a success and teaches the
  operator nothing.
- **Old records are never deleted**, from either side, here or later. That is the operator's
  decision and there is no code path for it.

### `looks_reusable` is a transport question, not a second completion rule

It asks the three things a *copier* must ask — does it parse as a JSON object, does it name the
source it describes, does its `cache_contract` match the running generation — plus a rejection of
`ai_deferred`, which is a half-finished analysis by the existing rule's own definition.
`_cache_entry_is_complete` in `video_analysis.py` remains the **only** authority on whether a record
may be *reused*, and it runs unchanged on every lookup after the import, against the copied bytes.

## The module deletes nothing, and computes no identity

Two absences, both asserted by test over the **unparsed AST** rather than the raw characters (the
docstring names what it must not do, and a substring scan cannot tell an explanation from a
violation):

- **No deletion.** No `shutil`, no `rmtree`, no `os.rmdir`, no `os.remove`. Exactly **two**
  `os.unlink` calls exist and both remove a file the same call created: the config writer's temporary
  and a record copy whose write failed. `input/video_analysis_cache/` is preserved by policy and a
  shared cache is read by several installations, so a cleanup path here would be the single most
  dangerous thing this module could grow.
- **No identity.** No `hashlib`, no `sha1`, no `blake2`, no `_video_signature`, no `st_mtime_ns`, no
  contract constants. The record filename is opaque: recognised by shape, otherwise copied verbatim.

Stdlib only (`json`, `os`, `tempfile`, `dataclasses`), so CLAUDE.md's hard rule holds and every branch
is testable on a bare interpreter.

## The GUI surface

Settings → **Video Analysis Cache**, immediately *before* Cache Management, because it answers
"where is the analysis cache?" and that panel's most expensive button acts on the answer. Collapsed
by default — set up once and left alone — and opened on load when a folder is already configured, so
a setting in force is never hidden behind a shut drawer.

```
shared_cache_folder      textbox + the existing native Browse Folder helper
shared_cache_enabled     checkbox
shared_cache_save_btn    writes the per-user file, then reports the resolved location
shared_cache_inspect_btn measures the resolved folder; reads only
cache_import_source      textbox + Browse
cache_import_btn         the one-time additive copy
shared_cache_status      the ONLY output of all three handlers
```

- **All three handlers write only `shared_cache_status`.** None appears in `source_outputs` or
  `prep_outputs`, none takes `_RENDER_LOCK`, none joins a render lane, and none touches a creative,
  audio or render widget — the cache's location is not part of a render request, so changing it
  cannot clear a confirmed source set, disable Create Video or trigger a scan.
  `concurrency_limit=None` for the measured R3 reason every other non-render registration here
  carries.
- **Save refuses to switch sharing on with no folder.** `enabled=True` with a blank path resolves
  straight back to the local cache, so the operator would believe sharing was on. The refusal comes
  before the write.
- **Save says the setting applies to the next analysis.** Stage 5 freezes its directory per
  invocation, so a render already running keeps the folder it started with. Saying so beats leaving
  it to be discovered.
- **One report function** renders both the Check and the Save output, so the screen cannot show two
  different answers to "where is the cache, and what is in it?".
- **The reuse limitations are stated plainly**, because they are what an operator would otherwise
  discover the hard way. Every claim is a property of the existing identity contract, not a new
  restriction: moving or renaming a video re-keys it; editing it re-keys it (the point); AI-on and
  AI-off records are separate; changing the model or the three AI detail settings starts a fresh set
  and leaves the old one alone.

### Cache Management now acts on the resolved folder, and warns when it is shared

`_cache_categories()` points the analysis category at `_analysis_cache_location().path`, so Inspect
reports what is really in use and Clear acts on it. When a shared folder is in force the category's
description is replaced by `INFO_CACHE_ANALYSIS_SHARED`, which **names the folder and says other
installations read it** — clearing a local cache costs one installation some re-analysis; clearing a
shared one costs every installation on the machine, including the production install the operator is
not currently looking at. That is a materially different decision and the panel has to say so before
it is made.

The other three categories are untouched and stay installation-local: they hold this app's own
transient files, which are not shareable and must never follow a cache setting. Containment is
unchanged and still `cache_admin`'s — `realpath` + `commonpath` against exactly one named root.
Handing it a different root does not weaken that; it is still one named directory, never a discovered
one.
