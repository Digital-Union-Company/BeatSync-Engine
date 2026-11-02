---
paths:
  - "src/beatsync_fork/transfer_guard.py"
  - "tests/test_transfer_guard.py"
  - "tests/test_clips_transfer_overlay.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. The review surface this guards is
> `.claude/rules/clips-preview.md`; the handler seam is `.claude/rules/gui-integration.md`.

# R13 Video Review Transfer Working Overlay

Pressing **Confirm** (or **Proceed**) in Video Review moves files. Before R13 it did so with no
immediate acknowledgement, which had two consequences the owner named in one sentence: a long
transfer looked like a dead button, and an impatient second click was a second submission.

R13 makes the existing transfer **observable** and **single-flight**. It is deliberately not a new
transfer engine, not a new tab, not a second review surface and not a second collision policy.

## What changed, in four places and no more

```
beatsync_fork/transfer_guard.py     NEW   the single-flight authority, pure policy
                                    +     [F-1] begin_decision / end_decision / deciding,
                                          DecisionSlot, DecisionInFlight, _conflicting_transfer
                                    +     [F-2] identity sets on Reservation / ActiveTransfer, one
                                          identity on DecisionSlot, the clip_in_progress rule, and
                                          conflict() as the advisory read busy() delegates to
beatsync_fork/clips_review.py       +     iter_sorted_clips, TransferTally, tally_outcomes
gui.py  _clips_confirm_frames       ->    the handler is now a GENERATOR
                                    +     [F-1] a DecisionInFlight refusal, and the decision
                                          snapshot RE-DERIVED under the reservation
                                    +     [F-2] the page's identities are part of the claim
gui.py  _clips_decide               ->    [F-1] CLAIMS a decision slot instead of reading the
                                          advisory busy(), and releases it in a finally
                                    +     [F-2] the clip's identity is part of that claim
gui.py  _clips_transfer_busy_text    NEW  [F-2] refusal code -> sentence, the only such mapping
gui.py  _on_clips_refresh           +     [F-2] greys Confirm for a cross-root clip conflict too
```

`sort_decided_clips` keeps its exact signature and return type, `move_decided_clip` is **untouched**,
and `collision.py` is untouched. Nothing about *where a file goes* changed.

## The admission rule is a test-and-set, and that is the whole safety argument

`TransferGuard.begin` claims the slot inside one lock. A disabled HTML button is a courtesy that
does not survive a second browser tab, a replayed event or any caller that never saw the UI — the
same reasoning `_RENDER_LOCK` already embodies for renders — and `if not busy: start()` is a
check-then-act race that passes a single-threaded test and fails a concurrent one.
`tests/test_transfer_guard.py` runs 24 real threads at a barrier against exactly that.

**Operation identity, because the lock scope is a design decision rather than a detail:**

| Component | Exclusive? | Why it is recorded |
|---|---|---|
| `owner` | **yes** | A duplicate submission is always a duplicate from the same owner |
| `root_key` | **yes, across owners** | Two tabs have two owner keys and could otherwise sort one folder at once |
| `identities` | **yes, across owners AND roots** (F-2) | The folder is a *proxy* for "which files", and the wrong one when two sessions scan an ancestor and a descendant. The identity set is the exact conflict unit |
| `generation` | no | Re-scanning must not entitle a second *concurrent* transfer. Read again at the end — see below |
| `signature` | no | Diagnosis only |

The three refusals are **separate codes** (`transfer_in_progress`, `folder_in_progress`,
`clip_in_progress`) because they mean different things to an operator: told "this page is already
being sorted" about somebody else's session, they would go looking for a press they never made —
and told "this same folder" about a *different* folder that merely contains the clips, they would
go looking at the wrong folder. `gui._clips_transfer_busy_text` is the one place a code becomes a
sentence, so a screen cannot name a reason the guard did not give; `TransferGuard.conflict` is the
advisory read that supplies the code, and it is literally `_conflicting_transfer` under the lock
rather than a second copy of the rules.

**Rule order is load-bearing**: owner, then folder, then clip. Each code must be the most specific
true statement available.

**Four constraints on the guard, each of which something would otherwise get wrong:**

* **The lock is never held across a file operation.** `begin` takes it, installs the record and
  returns; the *record* is what marks the owner busy. A lock held across a batch of renames would
  block the `_clips_view` read the decision endpoint performs on every classification, so a tile
  click would hang instead of being refused.
* **Release and progress are both token-matched.** A transfer finishing late must not un-busy the
  page a *newer* one is sorting, and a late progress update must not write into a newer record.
  From inside the function the stale and the live reservation are indistinguishable without the
  token — the same discipline `_end_verify_session` uses, for the same reason.
* **`end` is idempotent and lives in a `finally`.** It has to be safe on success, on a partial
  batch, on an exception and on an **abandoned generator**, where Gradio closing a dropped event
  raises `GeneratorExit` at the yield.
* **`busy()` is ADVISORY and is labelled so in the source.** Reading it and then starting would be
  the very race `begin` removes. Its callers use it to choose a *message* **and nothing else**.
  Admission is `begin`; a durable decision is `begin_decision`; those two are the only authorities.
  It used to be documented as *also* refusing a classification, and that was the F-1 defect — a
  read cannot refuse anything, because what it reports may change before the caller acts on it.
* **A durable decision is CLAIMED, not permitted by a check (F-1).** `begin_decision` /
  `end_decision` take a short-lived slot covering one `store.decide`, deciding under the **same**
  lock as `begin` and by the **same** rules. `begin` refuses `DecisionInFlight` while a
  conflicting decision is still being written. The pair is mutually exclusive, which buys the
  invariant the whole feature needs:

  > Once `begin` has returned a reservation, no durable decision touching any identity on its page
  > can be in flight and none can start until it is released — **whatever owner or root that
  > decision arrives under** — so a snapshot taken **after** admission is the batch's final truth,
  > and a decision the server accepted can never be silently ignored by a transfer already
  > running.

  The invariant is stated over **identities** because that is what the durable record is keyed on,
  and the bound is exact: it says nothing about a clip reachable under two *different* identities
  (see the hard-link limitation below).

  Neither call ever waits; both raise, and the caller turns that into an explicit retry. A
  conflict is a sub-10-millisecond coincidence between two human-paced actions, so a queue would
  add a blocking path to save nobody any time and a busy-wait would add an unbounded stall.

`transfer_guard.py` performs **no path operations at all** — the caller normalises the folder key
(`gui._clips_transfer_key`, `realpath` + `normcase`). A policy object that silently treated two
spellings of one directory as different would be worse than one that never looks. An unresolvable
root answers `''`, which the guard reads as *no folder claim* rather than as a claim on every
unknown folder; the owner rule still covers the duplicate press, so that direction is fail-safe.
**An unnamed root answers `''` before any resolution is attempted**, because resolving nothing
yields the working directory — which for this app is its own install folder. That is the PR #83
defect shape, and the ordering that avoids it is deliberate.

## The handler is a generator, and the frame shape is the contract

A function cannot say "working" and then work. `_clips_confirm_frames` is what both buttons are
wired to:

| Press | Frames |
|---|---|
| Any refusal (no identity, nothing scanned, unreadable record, nothing kept, destinations stopped validating, **busy** — this page, this folder, or [F-2] a different folder holding these clips) | **exactly 1**, byte-for-byte the frame the pre-R13 function returned |
| **A decision still being written** (`DecisionInFlight`, F-1) | **exactly 1**, before the slot is claimed |
| **A decision that landed while this press derived its plan** (F-1) | **exactly 1**, *after* the slot is claimed and before the Working frame — so the slot is released by the same `finally` |
| The sweep gate | **exactly 1**, and it claims **no slot** |
| A real sort | **1 + N + 1** — Working, bounded progress, terminal |

* **No Working frame for a press that moves nothing.** A spinner for an operation that has not
  been authorised yet is the dead-button complaint inverted. The gate claims no slot either: a
  gate the operator never answers must not leave the page marked busy.
* **The slot is claimed AFTER every refusal that changes nothing** — the same discipline
  `ScanCoordinator` follows, where a scan rejected for a bad folder never takes a generation.
* **Nothing is yielded from the `finally`.** Yielding during a close raises `RuntimeError`, which
  would replace a clean release with a second failure.
* **`_on_clips_confirm` is kept** as the collected form: it drains to the last frame, which is
  exactly equivalent to the pre-R13 return value. Several suites drive it as an ordinary function
  and a caller that does not need progress should not have to care that progress exists.
* **`_clips_working_frame` leaves the report, the grid and the counts alone** (`gr.update()`). The
  operator is watching the batch being sorted, and blanking the page they pressed Confirm on would
  hide it mid-flight. The grid stays clickable, which is safe *because the server refuses the
  classification* — a disabled tile would be presentation, and presentation is never the boundary
  here.

**`concurrency_limit=None` is unchanged on both registrations and is load-bearing.** A lane of 1
would leave the second press **queued** rather than letting the server refuse it — the measured
S2-A R3 trap — and a queued duplicate that ran later against a spent batch is strictly worse than
one refused now.

## Progress is counted, never predicted

The numerator is **recorded `MoveOutcome`s**: `iter_sorted_clips` yields each one *after* its move
has already happened or been refused, so a consumer counting them is counting facts.

* **No rate, no percentage, no estimate, no time remaining.** Same rule as R12's activity panel: a
  number that was not measured is not shown. A test walks every frame for those words.
* **`done` and `moved` are both shown** rather than one standing in for the other. A skipped clip
  is *done* and was not *moved*, and collapsing them would make "Moving 12 / 84" a lie as soon as
  one filename was taken.
* **`moved` / `skipped` / `failed` close the report, and the three sum to `total`.**
  `SKIPPED_STATUSES` is `missing`, `changed`, `occupied`, `cross_volume`, `in_use` — refusals this
  code made deliberately. `failed` is an error it could not classify. Collapsing them would make a
  healthy no-overwrite refusal read as a malfunction.
* **A partial batch keeps the real denominator.** `tally_outcomes(..., total=)` exists because
  counting the outcomes a crashed transfer managed to collect would silently rebase it and report
  "9 of 9" for a batch of 84.
* **The frame budget bounds the cost, never the truth.** `PROGRESS_FRAME_BUDGET = 50`;
  `should_emit` always emits the **last** outcome, so the closing figure is a measurement rather
  than a rounded stride. A batch of 50 or fewer reports every clip.
* **`should_emit` reads no clock, and `transfer_guard` imports no clock.** A throttle driven by
  elapsed seconds would make the frame sequence depend on how fast the disk is, which is precisely
  the unverifiable progress this feature replaces. Both are pinned — **by AST**, not by substring,
  because the prose explaining each ban naturally contains the banned token and this repository
  has tripped its own substring bans that way five times.

## A classification and a sort are mutually exclusive (F-1)

A transfer captures its decision snapshot before it starts moving. A decision that lands outside
that snapshot would be honoured by the screen and ignored by the batch: the operator would watch a
clip they just marked Keep land in Discarded, with the durable record agreeing with neither.

**The original R13 code got this wrong, and it is worth stating plainly because the shape recurs.**
`_clips_decide` read `_CLIPS_TRANSFERS.busy(...)` and then committed. That refused the obvious case
— a classification arriving *during* a sort — and missed the one that mattered: a classification
that passed the read *before* a transfer was admitted still committed *after* it, was answered
`ok: True`, was durable, and was invisible to the batch, with **both halves reporting success**.
Reproduced in both directions on the shipped handler: `PENDING→SELECTED` swept into Discarded, and
`SELECTED→REJECTED` moved into the Media Library, each with a terminal frame saying
`✅ Sorted all 3 clip(s).`

**A post-admission snapshot re-read does not close it on its own, and that was measured rather than
argued.** Against a probe carrying only that re-read, a classification suspended inside its own
commit re-read as *unchanged*, became durable immediately afterwards, and the transfer moved the
file by the stale snapshot anyway. The re-read closes the case where the decision *completed*
before admission; it cannot see one that is mid-commit.

So both halves are in place, and neither is sufficient alone:

| half | closes | mechanism |
|---|---|---|
| `begin_decision` / `DecisionInFlight` | a decision **straddling** admission | one locked test-and-set shared with `begin` |
| the snapshot re-read under the reservation | a decision that **completed** between the snapshot and admission | `store.snapshot_of(identities) != snapshot` → refuse |

`_clips_decide` still answers `transfer_in_progress` for **every** reason — this owner's own
transfer, another session's transfer on the same folder, and (F-2) another session's transfer on a
different folder holding this clip — so the page resyncs exactly as before. The second tab never
saw a greyed-out tile, which is why this cannot live in the browser.

## The conflict unit is the CLIP, not the folder (F-2)

The owner rule asks *whose press is this*. The folder rule asks *which directory*. **Neither asks
the only question that decides whether two operations collide: which clips.**

Two review sessions that scanned an **ancestor and a descendant** of one tree have two owner keys
and two different `root_key`s — and the same clip in both batches, under the **same durable
decision identity**, because decisions are keyed on the file and not on the path it was reached
through. So a classification accepted from the child page was answered `ok: True`, was made
durable, and was ignored by a sort already running on the parent page, which filed the clip by its
older snapshot. Independent review reproduced it on the F-1 tree as
`accepted=True durable=REJECTED landed=library` under a terminal frame reading
`✅ Sorted all 4 clip(s).` — the same "accepted but ignored" conjunction as F-1, reached by a route
neither of the first two rules could see.

**The correction is one more rule in the same atomic decision, not a second mechanism.**

```
begin(owner, generation, signature, root_key, identities)   <- the page's identities, frozen
begin_decision(owner, root_key, identity)                   <- the one identity it may change
_conflicting_transfer(owner, root_key, identities)          <- owner, then folder, then OVERLAP
```

* **No second mutex and no second engine.** Both claims still decide under the one existing
  `TransferGuard` lock, in one critical section, by one shared `_conflicting_transfer`.
* **`begin` checks the pending-decision map too**, because a decision *in flight* holds no
  `ActiveTransfer` to be scanned — that is the half of F-2 the identity rule alone cannot cover,
  and it mirrors F-1's `DecisionInFlight` exactly.
* **The set is bounded and immutable.** Bounded by the page (`MAX_CLIPS_PER_PAGE`), and copied into
  a `frozenset` by `begin`, so a caller that later mutates its own list cannot change what the
  conflict decision was made against. `note_progress` touches only the two counters.
* **`isdisjoint` rather than `&`**, so no intermediate set is allocated inside the lock and the
  scan short-circuits on the first shared identity. Still no I/O, no path operation and no clock
  in a critical section.
* **The claim covers exactly what the transfer will act on.** `_clips_confirm_frames` passes
  `_clips_identities(view)`, which is the same map `decision_of` reaches every clip through — a
  test compares the reservation's set against the page's own identity map, because a claim aimed at
  a different set would be protecting the wrong clips.
* **It is identity overlap, NOT a path-prefix test.** Two nested folders whose pages share no clip
  stay fully independent — a parent scanned *non-recursively* beside its own child is the case a
  prefix rule would have wrongly refused, and it has its own test.
* **`root_key == ''` no longer means unprotected across owners.** An unresolvable root switches the
  folder rule off; the identity is still known, so the clip is still excluded. That case the folder
  rule could never cover at all.
* **Two transfers over an overlapping batch are now excluded as well** — strictly worse than the
  classification case, since both would `os.rename` the same path and the loser would find it gone.
  The owner/folder pair never excluded that either.

The refusal still goes through the existing `refuse(...)` helper, so: nothing is appended, no
revision moves, validation happens before `store.decide` is reached, and the already-authorized
page is handed the server's current decision, revision and counts so a tile resyncs instead of
guessing. The slot is released in a `finally` covering every path — refusal, `StaleWrite`, a
durability failure and a defect — because a slot that outlived its decision would refuse this
owner's transfers until the app restarted, a worse failure than the one being corrected.

It is a **window, not a latch** — both claims end with the operation that took them. A latch that
outlived the batch would be a new way to strand the page.

**The re-read refuses rather than re-plans, deliberately.** Re-planning would have to re-derive
`plan.signature`, and a sweep the operator armed against the old signature must not be silently
re-aimed at a batch they never agreed to. A retry costs one press, re-derives everything and goes
through the gate again — which is what the signature is for.

`_on_clips_refresh` also stops *enabling* Confirm while a transfer is active. That is presentation
only and can never enable: Confirm's enabled state is still derived from `store.counts()` by
`_clips_render_current` and `_on_clips_scan` and nowhere else (R5-P3 / F8). It exists so the screen
does not contradict the Working state and invite a press the server is going to refuse.
`_clips_working_frame` is the **fifth** entry in the mover identity set in
`tests/test_clips_gui_integration.py`, and the enabler set is **unchanged** — which is the
assertion that matters: R13 added a transfer, not a second authority over the button.

## The R11 interaction, and the branch it decides

A page can stop being current *while a sort is running*: the operator starts a newer scan, or the
Raw Cutter's "Review these clips" retires it through `ScanCoordinator.invalidate` (R11 / F-1).

**The moves are not abandoned.** The operator pressed Confirm on that page, and stopping mid-batch
for an unrelated navigation action would leave a partial sort for no reason. What the transfer must
not do is hand the browser a page the server no longer holds — which is exactly what re-scanning
the retired root would do, reinstating the defect R11 closed.

So the generation recorded at `begin` is read again at the end, and the two branches are the two
`_on_clips_scan` already has:

```
is_current  ->  re-enumerate and publish the next batch      (unchanged pre-R13 behaviour)
superseded  ->  _clips_render_current(owner, …)              the AUTHORITATIVE page, not this one
```

That is the R5-P3 / F1 rule applied to a transfer, and it is why **no second notion of "the current
page" is introduced**. A test pins that `_clips_confirm_frames` names
`_CLIPS_SCANS.is_current(owner, generation)` and reaches no other coordinator method — not `begin`,
not `publish`, not `discard`, not `invalidate`, not `forget`.

Consequences, each covered by a test:

* the counts are still reported truthfully — everything in them really happened;
* the message says the page was replaced, so the grid above is not a repaint of it;
* **durable decisions survive a retirement mid-sort** — the coordinator holds pages, not decisions;
* two locks, two spans, **never nested**: `_clips_view`, `newest` and even `invalidate` all still
  answer *while* a batch is being sorted.

## What R13 did NOT change, and must not acquire

* **The two-step confirmation.** A batch with unmarked clips still needs an explicit Proceed on
  that exact signature, the signature is still a change detector rather than a credential, and the
  sweep still writes **no durable decision** — an unmarked clip swept to Discarded stays `PENDING`.
* **Collision behaviour.** `collision.SKIP` and `collision.SUFFIX` are reused as they are, read
  from the operator's saved preference. No date stamp, no hash, no hand-rolled counter — a test
  bans those names in the handler body. Single-flight is also what makes the named hazard
  unreachable: a second concurrent run of one batch could otherwise take the next free suffix and
  leave `clip0000_01.mp4` beside `clip0000.mp4`.
* **The no-overwrite guarantee.** One same-volume `os.rename`, enforced by the call itself rather
  than by an `exists()` pre-check; `os.replace` is still never used; nothing is deleted; a
  cross-volume request is still refused in words rather than degraded into a copy.
* **Path containment and destination validation.** `validate_roots` runs on every press, against
  the destinations carried on the view rather than re-read from the widgets.
* **One transfer engine.** `sort_decided_clips` is now `list(iter_sorted_clips(...))` and a test
  pins that it is a single `return` naming it, with identical parameters. Two loops would be two
  orderings, two sets of refusals and two collision paths to keep in step by hand.

## Open and recorded rather than claimed closed

* **Cancelling a transfer is not implemented**, deliberately. A half-sorted batch is a legitimate
  state and is reported as one, but there is no Stop button and no token: the operator cannot
  currently interrupt a long sort from the UI. Abandoning the Gradio event *does* stop the loop and
  release the slot, but that is a consequence of the frame being dropped and is **not** a cancel
  anybody pressed — do not describe it as one, and do not build a Cancel on it without its own
  design.
* **The progress figure is server-side text, not a browser tick.** Unlike R12's activity panel
  there is no `setInterval`, so between two emitted frames the number on screen is stale by up to
  one stride. It is never *wrong* — it is a figure that was true when it was emitted.
* **`root_key` exclusion rests on `realpath`.** Two genuinely different paths that resolve to one
  directory are caught; a path that `realpath` cannot resolve claims no *folder* — though since
  F-2 it still claims its clips, so that direction is no longer unprotected.
* **What the identity rule does NOT cover, stated exactly.** The conflict unit is the **durable
  decision identity**. A clip reachable under **two different identities** is therefore not
  detected: two hard links to one set of bytes are two identities, and that is not attempted. Path
  *aliases* are a different thing and are covered — junctions, `..`, case and trailing separators
  all collapse to one `root_key` and one identity, measured against a real NTFS junction.
* **It is a window, not a tree lock.** The exclusion lasts exactly as long as the reservation; a
  released transfer frees its clips in the same instant. Nothing accumulates, and a page the
  operator never sorted holds nothing.
* **The decision slot is held across one `live_identity_ok` open and one journal append (F-1).** A
  Confirm pressed inside that window is refused with "press Confirm again" rather than admitted —
  the right trade, since the alternative is admitting it onto a snapshot about to be contradicted.
  Measured at roughly 5–6 ms for one `_clips_decide`, so it is not observable at human pace; on a
  contended disk the open can take longer, and the refusal then lasts as long as the open does.
* **Two refusals in a row are possible in principle.** If a classification really did land inside
  the retry's window too, the retry is refused for the same reason. Each refusal is honest and
  moves nothing, and the window requires a decision to land in a sub-millisecond gap, so there is
  no livelock at operator pace — but nothing *bounds* the retries, and this is a property rather
  than a guarantee.
* **`TransferGuard.refused` still undercounts** (review MINOR M-2). The advisory short-circuit in
  `_clips_confirm_frames` returns before `begin`, so the ordinary duplicate press never reaches the
  counter. Diagnostic only, and deliberately not "fixed" by moving the authority.
* **`_clips_decide` answers `transfer_in_progress` even when the cause is another session's folder
  transfer** (review MINOR M-3). `begin_decision` now raises the distinct `folder_in_progress`, and
  the call site deliberately maps both to one code: the page resyncs from the attached record
  either way, and changing the wire code would churn an existing assertion for a cosmetic gain.
* **The slot is released before the terminal repaint** (review MINOR M-1). A duplicate press in
  that window is admitted and runs a second read-only attempt: measured at 0 files moved, 0
  suffixes, `❌ Nothing was moved`, with the two concurrent scans resolved by the existing
  generation discipline. Holding the slot across a read-only re-enumeration would lengthen the
  classification-refusal window for no integrity gain, so it is documented rather than changed.
* **Audio Review has no overlay.** The two surfaces already share `collision.py`, so this is the
  natural next thing to share — but it is not implemented here and R13 claims nothing about it.
