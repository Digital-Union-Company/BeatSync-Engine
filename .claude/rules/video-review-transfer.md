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
beatsync_fork/clips_review.py       +     iter_sorted_clips, TransferTally, tally_outcomes
gui.py  _clips_confirm_frames       ->    the handler is now a GENERATOR
gui.py  _clips_decide               +     one refusal while a transfer is active
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
| `generation` | no | Re-scanning must not entitle a second *concurrent* transfer. Read again at the end — see below |
| `signature` | no | Diagnosis only |

The two refusals are **separate codes** (`transfer_in_progress`, `folder_in_progress`) because they
mean different things to an operator: told "this page is already being sorted" about somebody
else's session, they would go looking for a press they never made.

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
  the very race `begin` removes. Its callers use it to choose a *message*, and to refuse a
  classification. Admission is `begin` and nothing else.

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
| Any refusal (no identity, nothing scanned, unreadable record, nothing kept, destinations stopped validating, **busy**) | **exactly 1**, byte-for-byte the frame the pre-R13 function returned |
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

## Classification is refused on the server while a batch is being sorted

A transfer captures its decision snapshot before it starts moving. A decision accepted afterwards
would be honoured by the screen and ignored by the batch: the operator would watch a clip they just
marked Keep land in Discarded, with the durable record agreeing with neither.

`_clips_decide` therefore refuses `transfer_in_progress`, and **both** reasons are checked — this
owner's own transfer, and another session's transfer on the same folder. The second tab never saw a
greyed-out tile, which is exactly why the check cannot live in the browser.

The refusal goes through the existing `refuse(...)` helper, so: nothing is appended, no revision
moves, validation happens before `store.decide` is reached, and the already-authorized page is
handed the server's current decision, revision and counts so a tile resyncs instead of guessing.

It is a **window, not a latch** — it ends with the transfer. A latch that outlived the batch would
be a new way to strand the page.

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
  directory are caught; a path that `realpath` cannot resolve is not claimed at all. A hard link to
  the same bytes under two directory names is **not** detected, and is not attempted.
* **Audio Review has no overlay.** The two surfaces already share `collision.py`, so this is the
  natural next thing to share — but it is not implemented here and R13 claims nothing about it.
