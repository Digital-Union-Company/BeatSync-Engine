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
5. `publish()` installs the view only if the generation is still newest;
6. [R5-P9 / N1] the handler then renders its response **from the authoritative view**, re-read
   after publication — never from the locals it built. Publishing successfully is not the end of
   the race: the poster pass and the journal compaction both take real time, and a second scan can
   supersede this one afterwards, after which `entries`, `session_id` and `page_token` name a
   session `new_session` has already revoked. A structural test pins the `gallery_html` arguments
   to the re-read view, because a behavioural test can only catch the interleavings it schedules.

Three consequences that are requirements, not side effects. A **superseded** scan repaints the
*authoritative* page rather than the one it built — handing the browser its own grid would display
a revoked session as current, since whichever response arrives last is what the operator sees. A
**newest scan that fails** calls `discard()`, so the operator is left with an honest empty state
rather than an older revoked page silently resurrected. And [R5-P9 / N4] **a stored view is
current only while its own generation is still the newest**: `begin()` makes a new scan the newest
*before* that scan revokes the previous session, so for the whole interval up to its `publish()`
the stored view named an already-revoked session. `ScanCoordinator.view()` therefore answers `{}`
during a transition, and every reader — the handler, Refresh and the `/r5state` read — sees the
same honest "no current page" rather than a grid whose every tile answers `403 unknown_session`.
An empty gallery is a state the operator can act on; a page of dead credentials is not.

**What this does *not* promise.** A server-side generation check cannot order HTTP responses. If a
superseded scan's response is assembled before the newer scan publishes, it paints the empty state
and can still arrive *after* the newer scan's grid, leaving the browser momentarily empty while a
live page exists. Do not claim response ordering that is not proved at the client boundary.

[R5-P10 / N1 — disposition, do not widen] **Authority is safe in that residual schedule;
presentation can be stale.** A late response cannot regain media or decision authority: its
credentials are revoked, so `open_authorized` answers `unknown_session` and `_clips_state` answers
`not_the_active_page`. What it can do is paint a dead grid.

The recovery claim must be stated narrowly, because the earlier wording here was too broad and an
independent review measured why. **Manual Refresh is a possible recovery route** — the server-side
handler renders the authoritative view, and a real browser Refresh click after three overlapping
physical Scan clicks was observed to work. **Automatic recovery from an expired page is not fully
implemented and not independently established.** The shipped `MutationObserver` calls
`__r5Resync` on a new grid nonce, but `__r5Resync` only paints an *authorized* page: it does not
request a fresh gallery when a stale page receives a refusal, and the observer ignores the
returned failure. A probe that injected earlier grid markup into the Gradio host left the old grid
in place and produced no recovery message — though it manipulated Gradio-managed DOM directly, so
it does **not** prove that a genuinely reordered Gradio response defeats Refresh either. A
network-level reordered-response browser test is the open evidence gap. So: do not write that
client resync recovers an untrustworthy page, and do not write that the stale-presentation window
is closed. R5-P11 made no N1 change.

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

[R5-P10 / N3 — disposition, do not overclaim] One `app.load(fn=None, inputs=None, outputs=None,
js=CLIPS_GALLERY_JS)` is the single approved JavaScript registration, and an AST guard in
`tests/test_open_output_folder.py` reports any other event registration that carries `js=`/`_js=`
or opaque `**kwargs`. **It is a review guard, not an authentication boundary, and its coverage is
bounded.** It catches an explicit named `js=` on any of its maintained event names, dictionary or
opaque unpacking on those names, and `.then`/`.success` unpacking. It does **not** cover every
JS-capable event on pinned Gradio 6.19.0 — runtime inspection also lists `double_click`, `copy`,
`delete`, `example_select`, `option_select`, `pause_recording`, `preview_open`/`preview_close`,
`start_recording` and `stop_recording` — nor an aliased `register = button.click; register(...)`,
nor `getattr(button, 'click')(...)`. Those are **hypothetical future edits**, not bypasses present
in the source; no current production bypass was found. Do not describe the guard as covering every
possible alias or dynamically constructed registration. R5-P11 made no N3 change.

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
too. `_append` therefore closes off an unterminated tail before writing after it
(`journal_tails_repaired` counts it). The damaged record becomes a self-contained discardable
line, every fully committed record before it is preserved, and the next record is intact.

[R5-P13 / N7-R2] **The repair writes durable evidence, appended onto the damaged fragment, and
the order within that one write is load-bearing.** It appends
`{"k":"tail-repaired","at":…}` followed by the newline — the marker *first*, the terminator last.
Two reasons, and both were measured rather than reasoned:

* A bare newline is indistinguishable from external corruption of a historical record, which is
  the thing recovery must now refuse (below). Once a fragment has been closed off it is no longer
  the final line, so a rule of "only the last line may be damaged" would refuse a store whose
  later decision had **already been reported successful** — the N7 defect's false-failure mirror.
* `_write_all` guarantees all-the-bytes-or-a-raise per call but **not** atomicity across an
  interruption. Writing `"\n" + marker` has a one-byte hole: interrupted after the newline alone,
  the fragment is terminated and undocumented, the next append sees a *terminated* tail and writes
  no marker, and the restart after that refuses a reported success. With the marker first, every
  prefix of the write leaves the fragment still unterminated — hence still the torn tail — and the
  next append repairs it again. `test_n7r2_an_interrupted_repair_still_recovers_every_reported_success`
  walks every prefix, and the mutation that restores newline-first order fails seven tests.

Do not move the newline to the front, do not split the repair into two writes, and do not put the
marker on a line of its own: the evidence must be attached to the damage it accounts for.

A reported success now means the complete record is on disk and recoverable. **A write that fails
raises `PersistenceError` and the endpoint answers 503** — the operator must never see a tick for
a decision that is not on disk. Do not swallow it, and do not reintroduce a single unchecked
`os.write`.

[R5-P5 / N2 · R5-P7 / N6] **Failing to *inspect* the journal is not an answer either.** The tail
probe raises `PersistenceError` when it cannot determine whether the tail is terminated: an
unknown journal state is not a licence to append, nor to write a repair into a file whose
condition could not be read.

### Start-up recovery is all-or-nothing, and an incomplete one is never compacted

[R5-P11 / N7] **This closed one half of a confirmed permanent loss of committed decisions — the
half where a state file could not be *read*.** The other half, readable state this build cannot
account for, stayed open until R5-P13; read the next subsection with this one, and do not restore
any claim that R5-P11 alone eliminated N7. Earlier reports
and an earlier version of this rule called it transient under-reporting that "loses no decision
and self-heals on a later readable start". That was **wrong**, and the correction matters more
than the wording: an independent review reproduced the loss, and the R5-P11 remediation
reproduced it again on the frozen baseline four ways — a real Windows exclusive-sharing lock and
controlled fault injection, each against the snapshot and against the journal, plus the real
2,000-record automatic threshold.

**The mechanism, exactly.** `_load` caught `OSError` in the same clause as `ValueError`, so an
unreadable snapshot was recorded as "corruption"; and an `OSError` part way through the journal
replay was swallowed with whatever had already been applied kept. Either way the constructor
returned a store that could not be told from a healthy one, holding an **empty or truncated**
record. That store was fully writable and fully compactable, and `compact()` then wrote that
memory over the real snapshot and truncated the journal. After that the decisions were gone: the
bytes that still held them had been replaced from a record that was never authoritative.
Measured, pre-fix: a 369-byte snapshot replaced by a 146-byte one, a 419-byte journal truncated
to zero, three committed decisions permanently `PENDING, 0`.

Two consequences that are easy to get wrong:

* **The destructive step needed no new decision at all.** In the journal-denial case the append
  was correctly refused by the N2/N6 fix and `compact()` destroyed the record anyway. So the guard
  cannot live only on the write path.
* **It needed no operator action at all.** `maybe_compact()` fires automatically at
  `COMPACT_AFTER_RECORDS`, so an ordinary scan was enough.

**The contract now.** Nothing is published until the snapshot and the journal have both been read
in full: memory is assembled in local dicts and installed in one step, and only then is the store
marked recovered. A failure propagates out of `__init__` as a `PersistenceError` — which is
strictly better than a store that looks healthy, because there is then no object to append to and
nothing to compact from. `_append` and `compact` additionally check the recovery flag, because
`compact()` neither reads nor needs the files it replaces, so "raise from `_load`" is not by
itself sufficient. **Nothing in `_load` writes**, so a refused load leaves both copies
byte-for-byte as they were and a later healthy attempt recovers everything, at the original
revisions, with the scan state restored and no manual repair.

### A readable file is not necessarily a recoverable file

[R5-P13 / N7-R1 · N7-R2] **R5-P11 closed this for files that could not be *read*. It left the loss
route fully open for files that could be read but not accounted for, and an independent review
measured both halves on the frozen candidate.** The rule's earlier claim — that recovery is
complete whenever both files are readable, and that corruption tolerance is safely bounded to
"statements about content" — was wrong, and it was wrong in the direction that destroys data.

* **N7-R1, the decisive one.** Three decisions were committed and compacted. The snapshot was
  valid JSON and still held all three complete records, plus one entry missing its `decision`.
  Materialising the records raised, the **whole** snapshot was discarded, `_recovered` was set, a
  new decision was accepted, and `compact()` replaced a 381-byte snapshot
  (`efdac66e…`) with a 144-byte one (`929bbca7…`) holding none of the three. Restart read all
  three as `PENDING, 0`. The automatic 2,000-record `maybe_compact()` did the same with **no
  operator action at all**.
* **N7-R2.** One committed decision's journal line was replaced by malformed content while later
  valid decisions remained: the line was skipped, recovery was declared complete, and compaction
  truncated the 313 bytes that still held it. A JSON-decodable `decide` event with no `identity`
  did the same while leaving `corrupt_records_discarded` at **0**, so even the counter said nothing
  was wrong. A non-numeric revision escaped as a bare `ValueError` and non-UTF-8 bytes as a
  `UnicodeDecodeError` — outside the typed refusal both boundaries are built on.

**The invariant, stated so it cannot be satisfied by accident: a store may become writable and
compactable only when it can JUSTIFY that the whole persisted record has been accounted for.**
Reaching end of file is not that justification. Neither is a corruption counter, a readable
journal, a plausible set of decisions, or a matching count.

**Why an unusable snapshot cannot be tolerated by reading more carefully.** `compact()` truncates
the journal, so **whenever a snapshot exists at all, the journal is by construction only the delta
written since that snapshot landed.** A readable journal is therefore indistinguishable from a
readable *whole history* except by knowledge the store does not have. This format carries no
genesis marker and no provenance, and inventing one implicitly is how the same defect returns
wearing a check — so an unusable snapshot **fails closed**. The case where the journal genuinely
is complete refuses too, deliberately and with a test that says so; nothing is lost by it, because
nothing is written and removing the damaged snapshot recovers everything from the journal alone.

**Conditions, kept distinct.** Conflating any two of them is how this defect was reached, twice:

| Condition | Answer |
|---|---|
| Both files genuinely absent | a fresh store; ordinary first run |
| A valid snapshot, with or without later journal events | replayed in full |
| Snapshot content that cannot be accounted for — bad JSON, bad structure, an unknown decision, a non-integer revision, bad UTF-8 | `PersistenceError`, nothing written |
| An **unterminated** final journal line | that one line discarded, everything before it preserved (F4) |
| An unreadable journal line that **is** terminated, anywhere in the file | `PersistenceError`, nothing written |
| An unreadable line carrying its own attached `tail-repaired` marker | discarded; the repair already accounted for it |
| A parsable event with a schema or kind this build cannot account for | `PersistenceError`, nothing written |
| A journal that is not valid UTF-8 | `PersistenceError`, nothing written |
| A temporary I/O failure (lock, ACL, device, unresolved pathname) | `PersistenceError`, nothing written |
| A replay that cannot be shown to be complete | `PersistenceError`, nothing written |

Two of those rows carry reasoning that is easy to lose:

* **Terminated damage is not a torn write.** `_append` writes a record and its newline in one
  `_write_all`, which writes every byte or raises — so a line this process terminated is a line it
  wrote in full, and a full record parses. Terminated damage is therefore external, may well have
  stood for a committed decision, and refuses wherever it sits. An earlier draft of this fix
  tolerated the whole trailing *run* of unreadable lines; that laundered a corrupted historical
  record whenever a torn tail happened to follow it, and the first compaction after the next repair
  truncated it away.
* **A parsable event was written whole.** Truncating one of this writer's records always leaves
  JSON that will not parse, so strictness about schema costs the torn-tail tolerance nothing. The
  two concerns are kept in separate methods on purpose: damage is `_replay_journal`'s business,
  meaning is `_apply`'s. `at` is the only optional field, because it is display metadata rather
  than part of the decision. A validator this strict must not reject the writer, which
  `test_n7_every_event_this_module_writes_satisfies_its_own_replay_validator` pins.

A transient read failure is **not** corruption, and corruption is **not** a licence to publish an
empty history. N7 is never "fixed" by discarding a snapshot or deleting journal data: **the
retained bytes are what the retry recovers from, and a refused load writes nothing at all**, so
every forensic byte of both copies survives the refusal. The only bytes compaction may ever erase
are ones that never represented a reported success — an unterminated fragment, or one already
closed off by its own marker.

There is **no migration concern** from this tightening: R5 is unmerged, so no deployed install has
ever written a `state/clips-review/` journal or snapshot.

**Absence is established from the directory listing, not inferred from a failure.** Do **not** use
`os.path.exists` or `os.path.lexists` as evidence that a state file is safely absent: they answer
`False` for a path that merely stopped *resolving* — a share that went away, a removed volume, a
traversal-denied ACL — exactly as readily as for a name that was never there. Measured:
`os.path.exists(r'\\no-such-host\share\file.txt')` is `False`. `FileNotFoundError` is not
"missing" either, for the same six Windows codes the N6 fix separated. So `_absence_is_proven`
requires the error class *and* code that mean an absent leaf, *and* an enumerable directory, *and*
the basename genuinely not in it; a listing that cannot be read, or that still contains the name,
fails closed. One measured detail worth keeping: `builtins.open` sets `errno` but **no**
`winerror` (only `os.path.getsize` supplies one), so on the real read path the decision rests
entirely on the listing — do not build a diagnosis there that requires the native code.

**The refusal is reportable, and that is part of the contract.** `PersistenceError` is the one
class both boundaries already handle, so every durability failure from the constructor — including
a state directory that cannot be created — raises it and nothing else. `gui._clips_store()`
assigns the module global from a local that only exists once the constructor returned, so a
refusal is **never cached**: a passing exclusive share must not become a process-lifetime outage,
and a half-loaded store must never become the one every later caller writes through.
`_on_clips_scan` obtains the store **before** `ScanCoordinator.begin()`, so a refusal takes no
generation, supersedes no running scan, blanks no current page, opens no review session and never
starts the poster pass. Scan, Refresh and Confirm all say plainly that the saved decisions could
not be read, that nothing was changed or lost, and that the retry is the next step — Refresh's
outputs are `(gallery, counts, Confirm)` with no status box, so its **gallery** carries the
message. A grid of `PENDING` tiles, "Undecided 0", or a readiness report derived from a record
that could not be read are all false statements with the authority of true ones; none of them may
be produced. `/r5decide` and `/r5state` deliberately do **not** catch it: the middleware already
answers that class with a path-free `503 not_durably_recorded`, which is the correct answer for a
classification that cannot be read or written, and catching it would make an unreadable record
indistinguishable from an ordinary refusal.

[R5-P13] **No view-layer change was needed for the stricter contract, and that is the point.**
Every new condition raises the same `PersistenceError` from the same constructor, so the handler
and middleware paths above cover them unmodified — which the GUI and HTTP tests assert against
*genuinely corrupt bytes*, with no fault injection at all. Read the operator wording as "could not
be read **as a complete record**": that is what it now means, and it is still the honest thing to
say, because the alternative reading an operator might make — a folder nobody has reviewed — is
exactly what it exists to prevent.

**The carve-out for a missing journal is conditioned on the open descriptor, never on the error
class.** "The file is not there" is a definite answer only when no append is in flight.
`FileNotFoundError` does **not** mean "missing": Python maps six Windows codes onto it —
`ERROR_FILE_NOT_FOUND` (2), `ERROR_PATH_NOT_FOUND` (3), `ERROR_INVALID_DRIVE` (15),
`ERROR_BAD_NETPATH` (53), `ERROR_BAD_NET_NAME` (67) and `ERROR_BAD_PATHNAME` (161) — and five of
those say *the pathname did not resolve* while the journal still sits on disk with a torn tail.
Reading all six as proof of a terminated tail reinstated the exact false success the repair exists
to prevent, through all three of the probe's I/O steps. So `_append` passes the descriptor it
already holds down to the probe, and **once that descriptor is held no inspection failure of any
class may be read as "terminated"**. Do not substitute `os.path.exists` or `os.path.lexists`: they
answer "no" for an unresolvable path exactly as readily as for an absent one, which is the
confusion being removed.

Stated precisely, because the distinction matters to anyone editing it: the descriptor parameter
is **not** dereferenced or validated by the probe — its only use is `open_handle is None`. What
guarantees the production caller supplies a genuinely open handle is the **closed call graph**:
`_journal_tail_is_terminated` has exactly one caller, `_terminate_damaged_tail`, which has exactly
one caller, `_append`, which opens the journal first. Keep that call graph closed; a future caller
passing `None` while an append is in flight would reopen the hole, and only the structure prevents
it.

**Every client-visible persistence error carries numbers only.** `PersistenceError` reaches the
browser in the 503 body, and `str(OSError)` appends `filename` — and `filename2` — whenever the
failing call was made by pathname, so interpolating the exception put the journal's absolute path
in that body. It was reachable with no fault injection at all: a read-only ACL on the journal, or
another process holding it with no sharing. All four messages go through `_io_detail`, which emits
`errno`, plus `winerror` **when CPython supplies one**, plus the short-write byte count. `winerror`
is not guaranteed: it is present from `os.path.getsize`, and absent from `builtins.open`,
`os.open`, `os.write` and `os.fsync`, which set `errno` only — so do not build a diagnosis that
requires it. Keep `raise … from exc` everywhere, so the full exception, path included, stays
reachable internally through the traceback. The same rule applies to the operator-visible status
box, not just the 503 body: `gui.py` formats a bare `OSError` through `io_detail` and leaves
`PersistenceError` and `Refusal` messages as-is, because those are already path-free.

**Testing for an absent path must be separator-insensitive.** `str(OSError)` runs `repr()` over
its filenames, which doubles every backslash, and a JSON body doubles them again — so
`assert path not in message` passes against a message that *does* leak the path. Compare with the
separators stripped from both sides, and prove the assertion is live against the vulnerable
version before trusting it.

[R5-P9 / N5] **Compaction is an optimisation over the record, never the record itself, and its
failure must not cost a scan.** `maybe_compact()` runs after the scan has already published a page
and minted live credentials; letting an `OSError` from it escape the handler destroyed a scan that
had entirely succeeded. Contain it, report it honestly in the status box, and serve the page.
Contain the two I/O classes only: a `TypeError` from a programming mistake must still surface.
Both failure points are covered by regression tests, because they fail at opposite ends of the
operation — before the snapshot lands, and after it has landed but before the journal is
truncated. A failed compaction also leaves the store **recovered**: it is a failed optimisation,
the record in memory is still the one that was recovered, and the store stays writable.

**State the reassurance accurately** — [R5-P11] corrected after an independent review, and the
earlier wording is a good example of a claim that is true of the tested failure points and false
in general. The status box used to tell the operator that every decision already made "is still in
the journal". **The journal alone is not the record.** After a successful compaction the journal
is empty and the snapshot is the authoritative copy, so the promise is recoverability from the
**snapshot plus the journal**, not residence in either one. Do not restore any wording that names
a single copy as the record: the shipped message says every decision "stays recoverable from the
snapshot plus the journal, and a failed compaction discards neither", which is what the storage
contract guarantees and no more.

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

[F7 — disposition] **Still UNMEASURED.** Neither the R5-P10 review nor the R5-P11 N7 remediation
measured it: no benchmark, no Qwen inference, no production render and no owner footage was used
by either. A representative synthetic 1/2/4-worker HDD contention test with a comparable
concurrent render on the same mechanical drive is a separate, separately authorized
pre-deployment gate. Do not record it as closed, mitigated or low-risk until that measurement
exists.

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
