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

## Sessions are scoped per owner

The owner of a review session is the **server-injected** `gr.Request.session_hash`. It is a real
handler parameter with a default (so the handler stays callable), is never listed in a click's
`inputs`, and absent identity **fails closed** — there is deliberately no shared anonymous
registry to fall into.

The HTTP layer authorizes on the server-minted `session_id` and asks the registry
(`owner_of`) whose it is, so **no owner identity is ever read from a request**. A browser cannot
claim to be another session; it can only present an unguessable id the server already bound.

**A rescan revokes only that owner's sessions.** The R5-P0 prototype revoked every session
process-wide, which would have let one operator's rescan destroy another tab's authorization;
that correction is pinned by a test in both directions. A revoked page also *clears its clip map*,
so it holds no file identities alive.

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

Optimistic concurrency: a decision may carry the revision it last saw; a mismatch is refused with
the current value so a stale second tab resyncs instead of silently overwriting. A superseded scan
is refused as `stale_scan`.

## Durability

Append-only JSONL journal plus a compacting snapshot, under `state/clips-review/`. One
`os.write` + `os.fsync` per record, so a torn write can only damage the last line — which is
discarded on load rather than guessed at. The snapshot lands via `os.replace`. Compaction is
bounded by a record count.

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

**R5 does not take `_RENDER_LOCK` and joins no concurrency group.** A read-only gallery is not a
render, and its poster pass is one short bounded read per clip rather than the four concurrent
reads D0 traced timeouts to. Every registration carries `concurrency_limit=None` for the measured
S2-A R3 reason: a full concurrency group leaves the event **queued** rather than delivering it.
If poster generation is ever parallelised against a drive a render is reading, that becomes a
measured question — not an assumption to bake in.

## Confirm

Disabled while anything is undecided, and when enabled it **reports**. It reads the durable record
and says what a later authorized phase would do. It moves nothing, and there is no code behind it
that could.
