---
paths:
  - "src/beatsync_fork/clips_review.py"
  - "src/clips_media_server.py"
  - "tests/test_clips_review.py"
  - "tests/test_clips_media_server.py"
  - "docs/claude/clips-preview-selector-v1.md"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. GUI seams are in `.claude/rules/gui-integration.md`.

# R5 Clips Preview Selector

Post-cut media triage in the **fifth** top-level tab: browse, play and classify the clips the Raw
Cutter produced. **R5-P1 is read-only.** The specification is
`docs/claude/clips-preview-selector-v1.md`; this file is the operative contract.

## What is implemented, and what deliberately is not

| | |
|---|---|
| **P1 implements** | discovery, authorized preview playback, poster frames, server-authoritative classification, durable decisions, the 100-clip gallery |
| **P1 does NOT implement** | any file movement — no move, copy, stage, quarantine execution or delete |

**There is no transfer code, not even disabled.** No flag, constant or dead branch can be switched
on to make P1 move a file; a test asserts the media layer contains no mutating filesystem call at
all. Transfer is a separate phase requiring its own authorization after independent review, and
designing it does not authorize it.

## The one idea everything rests on

**A filesystem path never travels through the browser.** The server mints a random opaque
`clip_id` (`secrets.token_urlsafe(16)`) per enumerated file and keeps the path in a server-side
map. A request carries only `(session, page, clip)` tokens.

So traversal is **impossible by construction, not filtered**: `src/clips_media_server.py` contains
no `os.path.join` at all, and a test asserts that. `../`, `%2e%2e%2f`, `C:\Windows\win.ini`, a UNC
path and a null byte are all refused because they are ids that are not in any page — never because
a filter recognised them. **Never introduce path-joining into the HTTP layer**, and never derive
an id from a path: an id that encoded one would be reversible, and an id that hashed one would be
guessable for a known filename.

## The Gradio integration is one public parameter

```python
app.launch(..., app_kwargs={"middleware": [Middleware(ClipMediaMiddleware, ...)]})
```

`Blocks.launch(app_kwargs=...)` forwards to the FastAPI constructor, which accepts
`middleware=[...]`. That is the whole integration. **Do not** replace the launcher, start a second
Uvicorn, use `gr.mount_gradio_app`, or touch `allowed_paths` / `set_static_paths` /
`blocked_paths`. Localhost binding stays.

It must be a middleware because Gradio serves only its `created_paths`
(`[upload_dir, cache_folder]`): an arbitrary operator media path is served **403**. Widening
`allowed_paths` to a media root would expose that whole tree to every request the browser can
make, and copying a page of clips into Gradio's cache would duplicate gigabytes to draw
thumbnails. Both were rejected for the same reason the S2 download button was.

The middleware is **pure ASGI**, not `BaseHTTPMiddleware`, so the body streams in bounded 64 KiB
chunks and peak memory does not scale with clip size. It claims exactly four prefixes and passes
everything else through untouched; a test pins both halves of that.

## Six authorization gates, then the descriptor

Per request: session exists → not revoked → page exists → not revoked → `compare_digest` on the
page token → clip is in *that* page. Only then is the file opened.

**Both media routes do this, including posters.** [R5-P3 / F5] The poster route used to authorize
page membership and then open the cached JPEG with no live-source check, so a replaced or
truncated source made `/r5clip` answer 409 while `/r5poster` kept serving a thumbnail of a file
that no longer existed in that form — the operator saw a frame they could neither play nor
classify. It now calls the same `open_entry` and closes the descriptor immediately: one
implementation, so the two routes cannot drift into checking different things. Never reduce the
poster route to a membership check again.

**Identity is revalidated on the opened file descriptor, never on the pathname.** `os.stat(path)`
followed by `open(path)` is two different objects, and the name can be repointed between them by a
rename or a reparse point, so `open_entry` opens first and compares `os.fstat(fd)` against the
size, mtime, `st_ino` and `st_dev` captured at enumeration. A structural test forbids a path
`stat` from being the deciding check. **Never "optimise" this back to checking the path.**

### What the identity check promises — state it exactly

It detects a replaced file (different inode), truncation or growth (size) and ordinary rewrites
(mtime). It is **not a content hash**: a same-size edit that also restores the original mtime and
inode is not detected. Hashing every clip on every request would re-read gigabytes per page, so
the guarantee is documented rather than overclaimed — and because P1 mutates nothing, the residual
case costs a stale preview, never a wrong move. Do not describe this as tamper-proof.

## Sessions are scoped per owner — and what that owner key actually is

The owner key for a review session is the Gradio browser session hash. `gr.Request` **injects**
it into the handler: a real parameter with a default (so the handler stays callable), never listed
in a click's `inputs`. Absent identity **fails closed** — there is deliberately no shared anonymous
registry to fall into.

**State the provenance accurately** (corrected in R5-P3 after the independent review, F3; the
earlier wording here claimed the opposite and a future session would have read it as true):

* The value is **client-originated**. `gradio/route_utils.py` reads it from the request body
  (`PredictBody.session_hash`), and the browser mints it with
  `Math.random().toString(36).substring(2)` — roughly 11 base-36 characters from a generator that
  is **not** cryptographically secure.
* So it is **not** an authenticated operator identity, and a non-conforming local client can
  choose any owner key it likes, including a fixed one shared between two clients.
* Do **not** write "server-injected" as though it meant server-*minted*, and do not write
  "nothing the browser sends can forge it".

What does **not** rest on it: the HTTP media layer authorizes on the server-minted
`secrets.token_urlsafe` `session_id` and asks the registry (`owner_of`) whose it is, so **no owner
identity is ever read from an HTTP request**. A browser cannot claim to be another session there;
it can only present an unguessable id the server already bound.

So the owner key is a **scoping** key, not an authentication boundary: its job is to keep one
browser session's pages and rescans from colliding with another's. The app stays localhost-only
and claims no multi-user authentication; a separately reviewed network-exposure design would have
to supply real identity before any of this could be called one.

**A rescan revokes only that owner's sessions.** The R5-P0 prototype revoked every session
process-wide, which would have let one operator's rescan destroy another tab's authorization;
that correction is pinned by a test in both directions. A revoked page also *clears its clip map*,
so it holds no file identities alive.

## One scan at a time per owner, by generation

[R5-P3 / F1] A scan changes authoritative state in three steps — `begin_scan`, `new_session`
(which **revokes** the owner's previous sessions) and `publish_page` — and then spends seconds in
the poster pass before it has anything to show. The original handler ran that sequence with no
critical section and no notion of which scan was newest, so an older scan finishing last
republished its own already-revoked view and left every tile answering `403 unknown_session` until
the operator scanned again. Reachable with a double-click: the Gradio event carries
`concurrency_limit=None`, which on 6.19.0 means *no limit*.

`ScanCoordinator` owns both the per-owner monotonic generation counter and the authoritative
per-owner view, in one object, because the two must be consulted together:

1. refusals that change nothing happen **before** `begin()`, so a scan rejected for a bad folder
   never takes a generation and cannot supersede a running one;
2. `begin()` publishes the scan as "newest" *while it is still running* — comparing against the
   last **completed** view would not see a scan that has started and not yet finished, which is
   why a completion-only check is insufficient;
3. the authority-changing calls run inside `authority(owner, generation)`, which re-checks the
   generation **while holding the owner's lock** and raises `SupersededScan` otherwise;
4. the poster pass runs **outside** that lock — and takes no render lock and joins no concurrency
   group;
5. `publish()` installs the view only if the generation is still newest.

Two consequences that are requirements, not side effects. A **superseded** scan repaints the
*authoritative* page rather than the one it built — handing the browser its own grid would display
a revoked session as current, since whichever response arrives last is what the operator sees. And
a **newest scan that fails** calls `discard()`, so the operator is left with an honest empty state
rather than an older revoked page silently resurrected.

The locks are per owner, so two operators never block each other. A narrow
`concurrency_limit=1` on the scan event would be acceptable as defence in depth, but Gradio queue
serialization is **not** the fix and must not be relied on as one.

## The server owns the decision, not the DOM

Decisions POST to `/r5decide`; the server revalidates the id, the page, the live identity, the
transition and the revision, then writes durably **before** responding. A tile is repainted only
from a server response, and `/r5state` repaints the whole page from the record — which is the
proof the DOM is a rendering, not the record.

Two things that look like details and are not:

* **Decisions do not go through a Gradio event.** Measured on the pinned `gradio==6.19.0`: setting
  a hidden Textbox's value and dispatching `input` does **not** make Gradio fire `.input()` — no
  request is issued at all. The Gradio-4 hidden-textbox channel is gone. Do not reintroduce it.
* **The grid markup carries a nonce.** A `gr.HTML` update whose markup is byte-identical to the
  previous value is **not delivered**, which made "re-read the server's record" look like a no-op.
  Do not remove the nonce.

### Optimistic concurrency is MANDATORY, and strictly typed

[R5-P3 / F2] A decision **must** state the revision it believes it is replacing. The original code
derived it with `int(payload.get('rev'))` inside a `try/except (TypeError, ValueError)` that
assigned `None`, and the store skips the check when the expected revision is `None` — so a
missing, `null`, string, bool, float, list or dict revision all became *an explicit request to
skip the concurrency check*. A fail-open, and reachable from the shipped client: `parseInt` of a
tampered non-numeric `data-rev` is `NaN`, and `JSON.stringify(NaN)` is `null`.

`parse_expected_revision` accepts a genuine, non-negative JSON **integer** and nothing else.
Rejected with stable codes: `revision_required` (absent or `null`), `malformed_revision` (string
— including `"3"` — bool, float, negative, list, dict), `revision_conflict` (a well-formed integer
that is not the server's current one). **Never widen this back to a coercion**, and never let a
malformed value reach `ReviewStore.decide` as `None`; `decide` keeps `None` for internal callers
only and now refuses a non-integer outright rather than passing it through `int()`.

No rejected request appends a journal record or moves a revision — validation happens before
`decide` is reached. A refusal returns the server's current decision, revision and counts to the
already-authorized page, so a stale tab resyncs instead of guessing.

Client side: validate the **whole** `data-rev` attribute against `/^(0|[1-9][0-9]*)$/` before
sending. Do not use `parseInt`, which accepts a numeric prefix and yields `NaN` otherwise. An
invalid DOM revision must send **nothing**, resync from `/r5state`, and surface a visible failure
if even that fails.

A superseded scan is refused as `stale_scan`.

## Durability

Append-only JSONL journal plus a compacting snapshot, under `state/clips-review/`. The snapshot
lands via `os.replace`. Compaction is bounded by a record count.

[R5-P3 / F4] **Write every byte, and never trust one `os.write` call.** `os.write` may accept
fewer bytes than offered and say so in its return value with no exception; the original code
discarded that return value, so a positive short write left a truncated record on disk while the
API reported the decision recorded. `_write_all` loops until the buffer is drained, treats a
zero-byte write as failure rather than looping on it, and raises `PersistenceError` with the byte
count on an `OSError`.

**The old claim that "a torn write can only damage the last line" was wrong.** Because the journal
is `O_APPEND` and a record is one line, an append landing after a newline-less tail merges with it
into a single unparsable line — so a *later, fully successful* decision was silently destroyed
too. `_append` therefore closes off an unterminated tail with a newline before writing after it
(`journal_tails_repaired` counts it). The damaged record becomes a self-contained discardable
line, every fully committed record before it is preserved, and the next record is intact.

A reported success now means the complete record is on disk and recoverable. **A write that fails
raises `PersistenceError` and the endpoint answers 503** — the operator must never see a tick for
a decision that is not on disk. Do not swallow it, and do not reintroduce a single unchecked
`os.write`.

**A write that fails raises `PersistenceError` and the endpoint answers 503.** The operator must
never see a tick for a decision that is not on disk. Do not swallow it.

Location is load-bearing: **never** beside the source media, and **never** under
`input/gradio_uploads` — `cleanup_on_startup` deletes everything under `input/` except `audio`,
`video` and `video_analysis_cache`, so a journal there would be destroyed by the next app start.

Identity invalidation is structural: the decision key is
`sha256(realpath | size | mtime_ns)`, so changed bytes hash to a different key and an old decision
simply does not apply.

## Discovery reuses the Raw Cutter's ordering

`input_manager.scan_folder` with `SERVABLE_SUFFIXES`, which equals
`raw_cutter_export.SUPPORTED_SOURCE_SUFFIXES` (asserted). **Do not re-walk the tree** — a second
traversal is a second ordering policy, and the two features would come to disagree about "the
files, in order". A structural test forbids `os.walk`, `scandir`, `glob` and a local `sorted(` in
discovery.

Consequences worth keeping: `batch_manifest.json` is excluded for free as an unsupported
extension — which matters, because it is the file that holds the real source paths; a zero-byte
file is rejected; and a file with real bytes that Chromium cannot decode **is** enumerated,
because an unplayable clip must still be classifiable.

## Path relationships are refused before anything is read

`validate_roots` compares **resolved real paths** with `commonpath` (never a string prefix, so
`media-evil` is not inside `media`) and cross-checks Windows `(st_dev, st_ino)`. Refusals:
destination inside input, input inside destination, identical or nested destinations, a
destination equal to the input, and any protected runtime directory (`bin/`,
`input/video_analysis_cache/`, `input/audio/`, `input/video/`, `input/gradio_uploads/`, `output/`,
`state/`).

A reparse point is **resolved, not banned on sight** — a destination that merely is a junction is
legitimate. But a reparse point `realpath` could **not** follow is refused as
`unresolved_reparse_point`: its true location is unknown, so every containment comparison would be
meaningless. That is the fail-closed path for alias cases this environment cannot create, and it
is what carries the boundary while the directory-symlink case remains skipped for want of
elevation.

**Validation creates no directory**, not even a destination.

## Posters

One frame, `-ss` **before** `-i` so it seeks instead of decoding — which is why the cost is
duration-independent (measured: 73.6 ms for a 15 s 1080p clip, 73.7 ms for a 30 s one). The argv
is built in `clips_review.build_poster_command` and executed through an injected runner, the same
split `media_sample` uses, so the GUI never becomes a second command-construction site.

**Four workers is a ceiling, not a suggestion.** D0 traced clip-extraction timeouts to four
concurrent FFmpeg-class readers on one mechanical drive; `POSTER_MAX_WORKERS` clamps to it and a
test pins the clamp. Raising it needs its own measurement against an active render.

A poster failure is **not** an error: the tile shows "preview unavailable / still classifiable"
and remains classifiable. The cache is keyed by clip identity and pruned to a bounded file count.

### Publication is atomic

[R5-P3 / F6] FFmpeg **never writes the published filename.** It writes
`<key>.<token>.part.jpg`, created `O_CREAT|O_EXCL` in the same directory, which is then validated
and `os.replace`d into `<key>.jpg`. Three defects came from the earlier direct-write form:

* a killed or timed-out FFmpeg left a partial JPEG *at the cache path*, and the hit test was
  `getsize(destination) > 0`, so that clip showed a broken image **forever** — a size check cannot
  tell "finished" from "stopped half way", which is why the test is now `is_complete_jpeg`
  (start-of-image **and** end-of-image markers);
* a reader could observe a half-written poster mid-generation;
* a *failing* attempt ran `os.unlink(destination)`, which could delete a perfectly **valid**
  published poster. An attempt now only ever removes its own temporary file.

Concurrent attempts for the same key each own a unique temporary and each publish atomically, so
the destination is always a complete JPEG and cannot be destroyed by a loser. `prune_poster_cache`
matches published names only (`^[0-9a-f]{32}\.jpg$`), so it cannot delete somebody's in-flight
attempt. The argv states `-c:v mjpeg -f image2` explicitly rather than relying on the output
filename's extension.

## The fifth tab is LAST, and that position is contractual

Placed after Advanced Tools. Two existing contracts depend on it:

1. Advanced Tools stays at `_TABS` index 3, so `test_the_occasional_tools_are_on_the_advanced_tab`
   and `test_library_prep_is_the_last_block_in_the_advanced_tab` need no index change.
2. The tab's markup stays **out** of the `source[cutter:advanced]` block that
   `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans for `gr.Accordion`.

And one trap, measured in R5-P0: a tab placed last falls **inside** the source span
`test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui` guards (~39 k
characters, running past `source_outputs = [` into the event wiring). That guard is a substring
check enforcing the approved sample resource policy by omission, so the banned widget token must
not appear in this tab's span — **including in a comment explaining why**. Prose naming a banned
token trips a substring ban exactly as code does; it has now happened five times in this
repository. Page size is a Number widget.

## Isolation from the render pipeline

R5 touches none of: Create Music Video admission, source confirmation, the Stage-5 cache, Raw
Cutter planning or export, shared render outcome state, the Qwen worker, or cutter cancellation
and progress. No Clips Preview event writes `status_output`, `video_output`, `process_btn`,
`source_state`, anything in `source_outputs` or `prep_outputs`, any creative or audio widget, or
any render state — pinned by test.

**R5 does not take `_RENDER_LOCK` and joins no concurrency group.** Every registration carries
`concurrency_limit=None` for the measured S2-A R3 reason: a full concurrency group leaves the event
**queued** rather than delivering it.

### Render contention: a declared limitation, not a safety claim

[R5-P3 / F7] The earlier wording here justified the absent render lock by calling the poster pass
"one short bounded read per clip rather than the four concurrent reads D0 traced timeouts to".
**That was false about this code.** `POSTER_MAX_WORKERS = 4` and `_on_clips_scan` uses the
default, so the poster pass **is** four concurrent FFmpeg-class readers — the same shape as the D0
condition. State it accurately:

* Four workers is a **measured performance configuration**, not evidence of safety under a
  concurrent render. The 3.34× figure was measured on an **SSD**, with no render running.
* An SSD benchmark establishes nothing about a spinning disk. D0 traced clip-extraction timeouts
  to four concurrent FFmpeg-class readers on one mechanical drive, and that risk is
  drive-dependent.
* **No mutual exclusion against a render is implemented** for the gallery. A scan started while a
  render is reading the same mechanical drive can reproduce the D0 contention condition.
* That case is **unmeasured** and stands as an explicitly disclosed operational limitation. It is
  a qualification on the feature, not a resolved question.

Do not convert this into a render-lock or a concurrency group without measuring first — and do not
restate it as safety. Lowering `POSTER_MAX_WORKERS` is the cheap mitigation if the case ever bites;
raising it needs its own measurement against an active render on a mechanical drive.

## Confirm

Disabled while anything is undecided, and when enabled it **reports**. It reads the durable record
and says what a later authorized phase would do. It moves nothing, and there is no code behind it
that could.

[R5-P3 / F8] Its enabled state comes **only** from `_on_clips_scan` / `_on_clips_refresh`, both of
which derive it from `store.counts()` — the durable record. That is deliberate and must stay:
decisions post straight to `/r5decide` rather than through a Gradio event, so a decision response
*cannot* enable the button, and no amount of DOM editing can either. A clip put back to PENDING
therefore disables Confirm again on the next Refresh.

The honest consequence is a window in which the counters read "Undecided 0" while Confirm is still
greyed out. Rather than leave the operator to guess, `paintCounts` appends
`CLIPS_REFRESH_TO_ENABLE_HINT` when `PENDING` reaches zero. That line is **display text only** —
it enables nothing and duplicates no decision authority into JavaScript. Do not "fix" this by
having the client enable the button.
