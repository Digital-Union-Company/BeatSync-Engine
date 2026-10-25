---
paths:
  - "src/beatsync_fork/clips_review.py"
  - "src/beatsync_fork/clips_review_transfer.py"
  - "src/clips_media_server.py"
  - "tests/test_clips_review.py"
  - "tests/test_clips_media_server.py"
  - "tests/test_clips_review_transfer.py"
  - "tests/test_clips_review_transfer_security.py"
  - "tests/test_clips_review_transfer_journal_integrity.py"
  - "tests/test_clips_review_transfer_recovery_semantics.py"
  - "tests/test_clips_review_lifecycle_authority.py"
  - "tests/test_clips_review_authority_rebind_safety.py"
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

**P1 itself contains no transfer code, not even disabled.** No flag, constant or dead branch can be
switched on to make P1 move a file; a test asserts the media layer contains no mutating filesystem
call at all. Transfer is a separate phase requiring its own authorization after independent review,
and designing it does not authorize it.

[R5-P45] **The P2 transfer core now exists as an isolated module and is still not reachable.**
`src/beatsync_fork/clips_review_transfer.py` is written and tested; it is *not* wired into
`_on_clips_confirm`, no HTTP route exposes it, and `src/gui.py` does not import it. Three tests pin
exactly that, alongside the unchanged media-layer guard. Its sources come only from
`ClipRegistry.page_entries`, so `plan_transfer` has no source-path parameter a browser could fill.
Enabling Confirm to call it is a further phase with its own authorization.

### [R5-P47] What the transfer engine may and may not do, after an independent review found four MAJORs

An independent adversarial review (R5-P46) reproduced four MAJOR defects in the R5-P45 engine. The
corrections below are **contract**, not implementation detail, and each exists because the
alternative was measured and found unsafe.

**Approval is a content fingerprint, and its absence refuses.** A decision is transferable only if
it carries the SHA-256 of the bytes the operator classified *and* the file still hashes to it.
Note what this rule already says above about metadata identity: it is accepted as sufficient
**"because P1 mutates nothing, the residual case costs a stale preview, never a wrong move"**. P2
makes the wrong move, so it cannot inherit that reasoning — a same-length edit with a restored
`mtime` passes every metadata check and was measured publishing unreviewed content. `decide()` now
takes an optional `content_sha256`; `plan_transfer` **refuses** an item without one. The P1 HTTP
decision path does not yet record one, so **no decision made through today's UI is transferable**,
and that is the intended fail-closed state. Never back-fill a digest onto an older decision and
call it historical approval.

**The engine never deletes a source. Not anywhere, not by any path.** The cross-volume path
publishes a verified copy and then stops at `SOURCE_REMOVAL_BLOCKED`, leaving the original in
place. This is deliberate and measured: on Windows, with this package's allowlist, there is no way
to delete *the object whose identity was proven* rather than whatever currently owns a pathname.
`os.unlinkat`/`dir_fd` do not exist; a held `O_RDONLY` handle makes the path undeletable
(winerror 32); and `O_TEMPORARY` **is** a handle-anchored delete but its disposition cannot be
cancelled without `ctypes`, so the only race-free ordering commits to deleting an object before it
can be checked. A test enumerates every `os.unlink`/`os.remove` call site and requires each to
target this engine's own temporary. **Do not add a source delete back** without an owner decision
on the missing Windows capability; a safe failure beats an unsafe successful move.

**Same-volume is a relocation, not a deletion, and that asymmetry is the whole design.** A
no-replace `os.rename` moves the object, so a bad outcome is reversible: the content is verified
against the approved fingerprint *before* the rename, and if the published object still fails to
verify the rename is **undone** by renaming it back (measured: the reverse rename succeeds onto the
vacated path, and refuses with winerror 183 if anything has taken it). The result is `ROLLED_BACK`,
never `REFUSED`. **No status may claim "nothing moved" after an irreversible step** — that was the
original defect, reported while the source path was already empty.

**A frozen plan is not authority.** `execute_transfer` requires the live `store` and `registry` and
re-reads the decision, its revision, the scan generation and the session's validity per item. The
check is made **binding** rather than merely recent by a transfer **lease**: `ReviewStore.decide`
refuses a leased identity with `transfer_in_progress`, so the decision cannot move across the
commit point, and a second execution of the same plan cannot acquire the lease. `expected_revisions`
is **mandatory** — it used to default to `None`, which silently skipped the only stale-decision
defence. The lease is **process-local** and guards the in-process store the single Gradio app owns;
it is not a cross-process file lock and must not be described as one.

**Three smaller rules.** The no-replace probe must observe a *collision-specific* refusal
(`FileExistsError`/183) **and** a successful rename onto a free name **and** unaltered probe
payloads — any `OSError` used to count as proof, so the precondition was established by the very
failures that should have refused it. Reparse inspection is tri-state and **`UNKNOWN` refuses**: a
path whose attributes cannot be read is not an ordinary file. And `recover()` takes its expected
digest from the **plan's approved fingerprint**, never from whichever hash field the journal last
carried, because a forged record used to redefine what recovery believed and a same-length stranger
then passed as a valid publication; recovery recommends and **never deletes**.

Still true, and unchanged by any of this: the engine remains unreachable from the GUI.

### [R5-P51] A transfer holds the authority it depends on, and conflicting lifecycle work is refused

`execute_transfer` revalidated the decision, its revision, the scan generation and the session
**per item**, and the R5-P47 lease made `decide` refuse a leased identity — so the *decision* could
not move across the commit point. **Nothing else was held.** Measured on the R5-P50 candidate, all
of `ClipRegistry.revoke_owner`, `ClipRegistry.new_session`, `ReviewStore.begin_scan`,
`ClipRegistry.publish_page`, `ScanCoordinator.begin` and a whole new scan were **accepted** after
`_revalidate()` returned and before `os.rename`; the clip published anyway and reported `COMPLETE`.
In two of those the session was already revoked at the commit point, and in another the gallery had
already gone blank. The window is O(file size), because the source is hashed in between.

**The owner-selected strategy is a binding lease, not a narrower recheck.** `LifecycleAuthority` is
an owner-scoped gate that every authority-changing operation consults **before** it mutates
anything. A conflicting same-owner operation is refused **fail-fast** with the existing 409-class
token `transfer_in_progress`; there is no queue, no wait, no silent invalidation and no implicit
cancellation of the transfer, and the operation may simply be retried once the lease releases.
A final pre-commit revalidation would only shrink the window and is **not** what this is.

**`ScanCoordinator.begin` is the guard that matters most, and a `begin_scan`-only fix misses it.**
The real P1 flow is `begin → authority → begin_scan → new_session → publish_page → publish`, and
`begin` makes a new generation the newest *before* the other three run. Since `view()` returns `{}`
whenever the stored generation is not the newest, **`begin` alone blanks the operator's gallery** —
so a guard placed only on `ReviewStore.begin_scan` would refuse the scan *after* the old page had
stopped being current, which is precisely the "refused scan left the gallery blank" failure. The
guard therefore sits in `begin`, before the counter moves, and a refused scan changes nothing: no
generation, no `_newest` entry, no durable scan record, no minted session or page. Tests assert the
authoritative view is **byte-identical** across a refused scan.

**Eviction is not an exemption.** The three caps (`MAX_OWNERS`, `MAX_SESSIONS_PER_OWNER`,
`MAX_PAGES_PER_SESSION`) and `ScanCoordinator._prune` all revoke authority. A leased owner is
**skipped** rather than evicted, and if every tracked owner is leased the cap is allowed to exceed:
the cap bounds memory, a lease protects an irreversible operation already in flight.

**The §6 inverse is resolved explicitly.** "Generation changed but `ReviewStore` still shows the old
scan" is **not** valid authority. `acquire` refuses with `scan_in_flight` while a newer generation
has not published its view, so a transfer cannot take authority over a page that is already
logically superseded. It becomes acquirable again as soon as the transition completes.

**Lock ordering, one direction only.**
`ScanCoordinator` lock → `ClipRegistry._lock` → `ReviewStore._lock` → **`LifecycleAuthority._lock`
(leaf — never held while acquiring another)**. The one method that must read the coordinator,
`acquire`, does so **before** taking its own lock, and a test asserts that ordering on the source
because a cycle here would be a deadlock rather than a failing assertion. `begin_transfer` likewise
acquires the authority *before* `ReviewStore._lock`, so no store → coordinator edge exists. No
subsystem lock is held across hashing, copying, FFmpeg work or `fsync`.

**Release discipline.** The lease is released on `COMPLETE`, `REFUSED`, `PARTIAL`, `ROLLED_BACK`,
`RECOVERY_REQUIRED`, `PUBLISHED_UNJOURNALLED` and on an unexpected exception. One plan cannot
release another's and an owner mismatch releases nothing — a release is an authority change too.
A failed acquisition rolls back only what that caller took.

**Scope, stated rather than implied.** Process-local, exactly like the R5-P47 decision lease. A
second process opening the same review-state directory is **outside** this mechanism and is **not**
claimed to be protected. This is not a cross-process lock.

**Binding is explicit.** Each subsystem works unbound, which is what lets the existing P1 suites
construct bare objects unchanged — so **production must bind**, and `gui.py` does, in one call
beside the objects it already creates. That call imports nothing from `clips_review_transfer`,
creates no route, moves no media and does not change Confirm; the only other GUI change is a typed
handler so a refused `begin` repaints the operator's **existing** page rather than an empty one.

### [R5-P50] "Nothing needs a human" is not "the plan is done"

`recover()` aggregated as `RECOVERY_REQUIRED if needs_operator else COMPLETE`, so `COMPLETE` meant
*"nothing needs a human"* while reusing a constant that means *"done"* everywhere else in the
engine. Measured on two real volumes: a cross-volume copy-only transfer returned
`SOURCE_REMOVAL_BLOCKED`, the batch correctly reported `PARTIAL` with `advance_page=False`, and
`recover()` on the identical filesystem answered `COMPLETE` / `needs_operator=False` — while its own
finding read `operator_may_remove_duplicate`. The same aggregation called a three-clip batch
COMPLETE with two clips still sitting in the review folder. An R5-P48 mutation control proved **no
test pinned either behaviour**.

**Three signals, independent by construction.** `RecoveryReport.plan_finished` answers *is the plan
done?* and is the **only** completion signal a caller may gate on; `needs_operator` answers *does a
human have something to do?*; and `state` is one of `RECOVERY_STATES` — `COMPLETE`,
`RECOVERY_INCOMPLETE` (known remaining work, nothing ambiguous) or `RECOVERY_REQUIRED` (evidence
inconsistent, unavailable or needing a decision). **`needs_operator=False` is never a synonym for
`plan_finished=True`**, and `plan_finished` defaults to `False` so a report built any other way is
never mistaken for a finished plan.

**Completion needs evidence, not just matching bytes.** Matching content at the destination with an
empty source path is **necessary and not sufficient** — an unjournalled publication looks exactly
like that on disk. An item is reported finished only when the durable journal's last record for it
is `COMPLETE`. `SOURCE_REMOVAL_BLOCKED` and `PUBLISHED_UNJOURNALLED` are terminal but are **never**
completion, however well the bytes match. Where every item carries its own durable `COMPLETE`
record, an absent *batch* summary is reported as a qualification rather than escalated: the items
are individually proven, so escalating would be the opposite inaccuracy.

**`operator_may_remove_duplicate` is unfinished work AND an operator action.** It contributes to
neither `plan_finished` nor a `needs_operator=False` claim — both used to be wrong there at once.
`retry_safe` is unfinished work needing **no** operator decision. The action vocabulary is
deliberately still exactly four values; the added precision lives in each finding's `detail` and in
the aggregate state.

**Recovery stays strictly non-destructive.** It deletes, moves, retries, replaces and rewrites
nothing; a test spies on `os.unlink`/`remove`/`rename`/`replace`/`rmdir`/`truncate` and requires
**zero** calls. A recommended action is never permission to perform it. `RECOVERY_INCOMPLETE` is
deliberately **not** in `STATES`: no item is ever in it and no journal record carries it, and
adding it there would make `_history_fault` accept it as an item state.

### [R5-P49] Reporting may not retract a completed operation, and a journal append may not vanish

A second independent review (R5-P48) confirmed all eight corrections above and found **three new
MAJORs**. Two of them, F1 and F4, are corrected here; **F2 and F3 remain OPEN** and are restated
below so nothing reads this section as overall clearance.

**Once an irreversible filesystem step succeeds, no later failure may report that nothing moved.**
Every journal append after the commit point used to be unguarded. `TransferJournal.append` turns
any `OSError` into `TransferRefused`, and `execute_transfer`'s broad `except TransferRefused`
produced `ItemOutcome(REFUSED)` — whose defaults assert `source_removed=False` and
`published_path=""` — about a clip already in the Media Library with its source path empty. A real
Windows sharing violation on the journal file reproduced it. Worse, the **batch-summary** append sat
outside the per-item `try`, so a persistent journal failure raised and the caller received **no
`TransferResult` at all**: no outcomes, no counts, no sign a file had moved.

Seven situations are now distinguishable by state alone: nothing published (`REFUSED`); published,
verified and journalled (`COMPLETE`); **published and verified but not recorded**
(`PUBLISHED_UNJOURNALLED`, new); verification failed and rollback worked (`ROLLED_BACK`);
verification failed and rollback did not (`RECOVERY_REQUIRED`); cross-volume copy with the deletion
withheld (`SOURCE_REMOVAL_BLOCKED`); and state unestablishable (`RECOVERY_REQUIRED`).
`PUBLISHED_UNJOURNALLED` carries the real published path, byte count and SHA-256, reports
`source_removed` according to what actually happened to the source (`True` same-volume, `False`
cross-volume), is **never** `COMPLETE`, keeps `advance_page` false and **stops the batch** — the
durability guarantee a page depends on is gone, so further unwitnessed publications must not happen.
A batch whose *summary* cannot be journalled is downgraded from `COMPLETE` to `PARTIAL` for the same
reason, without rewriting any item outcome. **A journal failure is never permission to repeat, undo
or delete a completed operation**, and no speculative rollback is attempted to tidy up reporting;
a test counts the renames and requires exactly one. Pre-mutation appends deliberately still *raise*:
there no outcome list exists, nothing has moved, and refusing loudly is right.

**`O_APPEND` writes at the end of the file, not the end of the last record.** With an unterminated
fragment on disk the next append was concatenated onto it, the two became one unparseable line, and
the **newly committed event was silently lost** — gone after a cold reopen, with the write having
reported success. `append` now classifies the final bytes first and establishes a boundary:
a terminated or empty file is untouched; a **complete record that merely lost its newline** gets a
terminator and is preserved as real history (misclassifying it as damage would discard a committed
record — the R5-P15 lesson); a **provably torn** fragment is preserved and closed off with a
self-describing `tail_separator` record carrying no `clip_id` or `state`, so it can never be read as
an outcome; a **malformed complete** record is surfaced, never discarded; and an **uninspectable**
tail **refuses the write** — being unable to read the end of the file is not evidence that it ends
on a record boundary. Nothing is ever truncated or rewritten, every repair is itself an append,
repair is idempotent, and a crash part-way through a separator leaves either a clean boundary or a
new torn fragment that the next append repairs. `records()` now marks damaged lines `damaged: True`,
`damage()` reports them, and `recover()` **escalates** on them, because a damaged record carries no
`clip_id` and a per-clip reading of the history would otherwise ignore it entirely.

Appends are serialised by a **process-local lock**. Classify-then-append is not atomic across
processes, so a second process writing the same journal is **outside** this mechanism and is not
claimed to be safe — the same honest scope as the process-local transfer lease, and deliberately
not described as a cross-process file lock.

**What R5-P49 does NOT close, stated here so no reader infers clearance:**

- **F2 — CLOSED by R5-P50**, see the next section. (At the time of the R5-P49 commit it was open:
  `recover()` aggregated intentionally-unfinished work into `COMPLETE`/`needs_operator=False`. The
  journal-integrity escalation R5-P49 added was a *different* check and did not fix it.)
- **F3 (MAJOR, OPEN)** — session, scan and page revocation are still accepted after `_revalidate()`
  and before the commit, and the item still completes. `ReviewStore.begin_scan`,
  `ClipRegistry.revoke_owner` and the transfer-lease model are deliberately **untouched** here.
- **F5 (ADVISORY, OPEN)** — the rollback's target directory is still never no-replace-probed.
- **The cross-volume source removal remains withheld**, so the accepted
  `STAGE → VERIFY → PUBLISH → REMOVE SOURCE` contract is still **unmet** and remains an owner
  decision. No `ctypes`, no native helper and no pathname source delete were added.
- **Decisions written by today's P1 HTTP path remain non-transferable**; no fingerprint was
  back-filled and neither `gui.py` nor any HTTP route was touched.

### [R5-P53] The exclusion is a reservation, not a check — in both directions

Independent review **R5-P52** reproduced two MAJOR races against the R5-P51 candidate and returned
`R5_P52_PR75_CHANGES_REQUIRED`. R5-P51's guard was a **check-then-act**, and that is the whole of
what went wrong:

```
guard(owner, op)          # takes authority._lock, reads the lease map, RELEASES the lock
with self._lock:          # a SEPARATE critical section
    ...mutate...
```

`LifecycleAuthority.acquire()` took no subsystem lock at all, so the ordering *guard succeeds →
transfer acquires → transfer revalidates → mutation executes → transfer publishes* was accepted on
**all five** lifecycle operations. Measured: the clip moved, the batch reported `COMPLETE` with
`advance_page=True`, and the authority was already destroyed — gallery blank
(`ScanCoordinator.begin`), session revoked (`new_session`, `revoke_owner`), page revoked
(`publish_page`) or `current_scan` replaced (`begin_scan`). Separately, `acquire` probed the
coordinator outside its lock, so a scan taking a generation between the probe and the lease record
went unnoticed (**F53-B**).

**The correction, and the distinction that matters.** A lifecycle mutation now calls
`reserve_mutation()`, which refuses while a lease exists and otherwise grants a token **and bumps a
per-owner monotonic epoch**, both inside the authority's lock. The token is held *across* the
subsystem mutation and released in a `finally`. `acquire()` refuses while any reservation is
pending, and re-validates the epoch it captured before its lock-free coordinator probe. Both
directions are therefore arbitrated by **one lock**, rather than by two moments in time:

| | lifecycle mutation | transfer acquisition |
|---|---|---|
| reserves/installs under | `LifecycleAuthority._lock` | the same lock |
| refuses when | a lease is held → `transfer_in_progress` | a reservation is pending → `lifecycle_mutation_pending` |
| | | the epoch moved under the probe → `lifecycle_epoch_moved` |
| | | a scan is mid-flight → `scan_in_flight` |

**The reservation is a logical token, never a mutex.** The lock is taken only to grant and to
release it, so no SHA-256 hashing, journal `fsync`, media copy or FFmpeg call ever runs under it.
A transfer leases for the whole batch and the authority lock is uncontended throughout.

**Reservations deliberately do not exclude each other.** Two same-owner lifecycle mutations are
legitimate — two tabs rescanning — and the per-owner generation contract in *One scan at a time per
owner* exists precisely so the newer supersedes the older. `_pending` is therefore a dict of tokens
per owner, not a flag, and a test pins that two concurrent same-owner scans still both complete.
Serialising them here would have broken the scan contract to fix a transfer bug.

**Eviction decides and licenses in one act.** `ScanCoordinator._prune` and the registry's
owner-cap eviction previously chose a victim with a `guarded()` probe whose answer expired with the
lock. They now call `try_reserve_mutation(victim)`, which returns `None` when the victim is leased
*or* already being mutated — so a transfer cannot acquire between choosing the victim and deleting
its state. If every tracked owner is unavailable the cap is allowed to exceed: a cap bounds memory,
a lease protects an irreversible operation in flight.

`guard()` and `guarded()` survive as **diagnostic probes only**. A test walks every mutation site's
AST and fails if any of them calls either, because reverting one site to a check is exactly the
defect R5-P52 measured.

**`ScanCoordinator.forget()` is reserved now too.** R5-P52 recorded it as the one coordinator
mutator with neither a lease check nor a caller; "unreachable today" is not a safety property, so
it refuses under a lease like any other mutation. `publish` and `discard` remain deliberately
unreserved — they write only `_views`, which `_revalidate` never reads — and that conclusion
carries its own test rather than being asserted in prose.

**Rebinding can no longer orphan a lease.** `bind_lifecycle_authority()` refuses to point a
subsystem at a *different* authority while any lease is live (R5-P52 measured the alternative: the
live lease stayed on the old instance, the new one saw none, and a mutation that should have been
refused succeeded). Rebinding the *same* instance stays idempotent, and attaching a subsystem whose
authority is still `None` is always allowed — that is the lazy `_clips_store()` path and P1
startup, neither of which changed.

### R5-P55 / F54-1 — rebinding is linearizable, not merely checked

**R5-P53's rebinding gate was half-built, and it was itself a check-then-act.** It asked
`has_any_lease()` and nothing about `_pending`. Independent review **R5-P54** reproduced the
consequence 3/3 on real files: a rebind taken while a lifecycle mutation held a reservation
orphaned that reservation, the transfer then acquired on the newly bound authority, and the clip
**moved** with `batch=COMPLETE`, `advance_page=True` and no refusal record in the journal — while
the session and page the operation had been planned against were already revoked. Bit-for-bit the
F53-A outcome the reservation protocol exists to prevent, one layer up.

**Adding `has_any_pending()` to the old predicate is not the fix.** R5-P54 said so explicitly, and
it is right: asking a question and then assigning leaves the identical window, because a
reservation granted between the question and the assignment is orphaned just as completely. That is
the defect class R5-P52 already proved fatal.

So the gate is a **two-phase quiescence barrier** with a stated linearization point:

1. Work out which authorities are actually being **replaced**. Initial attachment (`current is
   None`) and idempotent same-authority binding detach nothing, so they have no live state to
   orphan, take **no barrier**, and cannot be made to refuse — which is what keeps `gui.py`'s
   import-time bind and the lazy `_clips_store()` attachment working unchanged. A startup path
   that can fail closed on a check it does not need is an outage, not a safeguard.
2. If something *is* being replaced, `LifecycleAuthority.begin_rebind()` is called on every
   distinct authority whose live state is at stake — each outgoing one **and** the incoming one.
   Each call observes that authority quiescent and installs the barrier **in one critical
   section**, after which `reserve_mutation`, `try_reserve_mutation` and `acquire` all refuse with
   `lifecycle_authority_rebinding`. Any `False` refuses the whole rebinding.
3. Only once **every** barrier is installed are the three attribute assignments performed.
4. Every barrier is released in a `finally`, on the refusal path too.

**Quiescent means three things, not one:** no lease, no pending reservation, **and no acquisition
in flight**. The third is load-bearing and was not in R5-P54's proposed patch. `acquire` is not one
critical section — it deliberately drops the lock to probe the coordinator — so between its two
arbitrations no lease is installed yet and `_leases` alone reports the authority idle. A rebinding
that began *and finished* inside that interval would be accepted and the lease would then be
installed on an authority nothing consults: **F54-1 with the lease and the reservation exchanged.**
`_acquiring` is incremented before the first arbitration and cleared in a `finally`, so the barrier
spans the whole acquisition.

**Linearization point: the last successful `begin_rebind()`.** After it, every involved authority
is simultaneously free of leases, free of reservations, free of in-flight acquisitions and refusing
new ones, and stays so until the binder finishes — only the binder that installed a barrier can
lift it. A concurrent `reserve_mutation` or `acquire` therefore either completed strictly *before*
that point, in which case some `begin_rebind` observes it and the whole rebinding fails closed, or
is ordered strictly *after* it, in which case it is refused. There is no interleaving in which one
is accepted and then detached.

**No new deadlock and no new lock edge.** The barriers are taken one authority at a time, never two
locks at once; `begin_rebind` **never waits** — it refuses, because a barrier that blocked on a
live reservation would invert the documented order; and it never blocks `release_mutation` or
`release`, so a live operation can always drain and an operator retry always succeeds. The barrier
takes only the authority's own leaf lock, and nothing hashes, copies, journals or calls FFmpeg
inside it.

**Partial target sets stay supported for attachment and refused for replacement.** Partial binding
*is* the production arrangement — `gui.py` binds the registry and coordinator, and the store joins
later — so it is not forbidden. What may not happen is a partial **replacement** that leaves one
subsystem consulting a different gate from its siblings while a mutation is mid-flight. All seven
subsets are pinned by test.

**A release frees by identity, never by equality.** `MutationReservation` is a frozen dataclass, so
it has a generated `__eq__` and two reservations carrying the same four field values compare equal
while being different objects. `release_mutation` matches with `is not`. Measured for R5-P55:
relaxing that to `!=` survives the whole R5-P53 atomic-lifecycle suite untouched, because that
suite's forged-token case differs in `operation` and so fails an equality test too. The
field-for-field forgery is the only shape that distinguishes the two, and it now carries a test.

**What R5-P55 does NOT close**, stated so no reader infers clearance:

* **A direct `target.lifecycle_authority = other` assignment bypasses `bind_lifecycle_authority`
  entirely.** No property of the API prevents it. The only reason F54-1 was never reachable in the
  shipped application is that the one direct assignment `gui.py` performs — in `_clips_store()` —
  is an *initial* attachment, on a store constructed three lines earlier whose authority is still
  `None`; initial attachment detaches nothing and so cannot orphan anything. That is a property of
  today's source rather than of the design, so it is **pinned by an AST test**: if a second binding
  call appears, or that assignment becomes a replacement, the test fails and the bypass is
  re-assessed instead of shipping silently. `gui.py` was **not modified by R5-P55**.
* **F54-5 (ADVISORY) is unchanged.** The `current is None` carve-out still lets an unbound
  subsystem join a *different* authority while another holds a live lease. Preserving lazy
  attachment requires the carve-out; closing it is a separate decision.
* **F54-2 (MINOR) is not patched, and is no longer reachable.** `ClipRegistry._new_session_locked`'s
  eviction release and `ReviewStore.end_transfer` re-read `self.lifecycle_authority` instead of the
  local their reservation or lease was taken from. Both are now unreachable *by construction*: a
  pending reservation and a live lease each refuse the rebind that would be needed to make the
  re-read resolve to a different object. Recorded, not relied upon.
* **F54-3 (MINOR) and F54-4 (ADVISORY) are untouched** and still open — the two
  outcome-preserving/atomicity-removing assertions R5-P54 asked for, and the unreserved
  `publish`/`discard`, which must be **re-opened when the GUI transfer handler is written**: the
  natural implementation (run a transfer, then repaint the gallery) is exactly the shape that makes
  `publish` reachable under a live lease.
* Everything R5-P53 left open stays open: **F5**, **cross-volume source removal**,
  **decision-time content fingerprints**, **cross-process locking** (the model is process-local and
  must never be described otherwise) and **GUI/HTTP integration**. The engine is still unwired and
  Confirm still moves nothing. Independent review **R5-P56** is required.

> **R5-P56 ran, and returned `R5_P56_PR75_CHANGES_REQUIRED`.** F54-1 was independently confirmed
> closed (3/3 refused, filesystem unchanged) and the `_acquiring` accounting was clean on all
> fourteen fault paths. But the barrier above was broken **three more ways**, each reproduced to a
> completed, irreversible move: **F56-1** (the outgoing snapshot was stale), **F56-2** (a partial
> replacement of an *idle* authority split the gate, with no concurrency at all) and **F56-3** (the
> coordinator pointer could be retargeted, or left absent, outside the barrier). Three MINORs
> followed: **F56-4** (the barrier-spans-the-assignments property was unasserted — a mutation
> removing it survived the whole suite), **F56-5** (barrier ownership was call discipline, not code)
> and **F56-6** (a raise mid-assignment left a partial binding). R5-P57 retires the capability; see
> the next section.

### [R5-P57] The authority topology is immutable: bound once, never replaced

**The owner's decision, and the reason for it.** Four protocols in a row tried to make runtime
authority replacement safe — R5-P51's predicate, R5-P53's gate, R5-P55's two-phase quiescence
barrier — and three independent reviews broke all of them, each time through the same permission:
*an idle authority may be replaced.* Each fix was more elaborate than the last and left a smaller
version of the identical hole. BeatSync has never needed the capability: the supported architecture
is **one process-local authority per app**, connecting one `ClipRegistry`, one `ScanCoordinator` and
one lazily created `ReviewStore`. So the capability is **removed**, not guarded a fourth time.

`bind_lifecycle_authority` now supports exactly three operations, and the third is a refusal:

| Rule | Operation | Outcome |
|---|---|---|
| **A** | attach an **unbound** component to the authority | performed |
| **B** | re-attach a component to the **same** object (`is`) | no-op, permitted |
| **C** | replace a bound component with a **different** object | **refused, always** |

**Rule C refuses unconditionally.** Not "while a lease is held", not "while a reservation is
pending" — *always*, including when every authority involved is completely idle. That is the whole
of the correction, and it is what R5-P53 and R5-P55 both got wrong: an idle authority is not a safe
one to detach, it is merely one whose next operation has not started yet. The refusal reason is
unchanged (`lifecycle_authority_rebind_refused`), so operator- and test-visible wording did not move;
what changed is that it no longer depends on quiescence.

**Rule D — partial binding stays supported, and can no longer split the graph.** Partial binding
*is* the production arrangement: `gui.py` binds the registry and coordinator at import, and the
store joins in `_clips_store()` once constructed. What partial binding can no longer do is
*replace* anything, so the F56-2 split is unreachable through the API. Coherence of the whole graph
is additionally proved where both halves are in hand — at execution time, by
`lifecycle_topology_fault(registry, store)`, which refuses
`lifecycle_authority_incoherent` before a plan is executed. A binder handed only the store
genuinely cannot see the registry, so this check belongs at the execution site and not at the
binding site.

**Rule E — the coordinator pointer is write-once too.** `authority._coordinator` may go
`None` → one object and never object → object. Supplying the coordinator it already has is
idempotent; supplying a different one is refused with `lifecycle_coordinator_rebind_refused`. This
closes F56-3's retarget *without* having to ask whether an acquisition is in flight — which is
exactly what the deleted `_acquiring` bookkeeping existed to answer.

**An absent coordinator is not an idle one.** A bound authority with `_coordinator is None` would
make `acquire` skip the scan-in-flight arbitration rather than perform it, and R5-P56 measured a
lease granted in precisely the mid-scan state that arbitration exists to refuse. **Both layers fail
closed when the supported binding API established the topology**: `lifecycle_topology_fault`
refuses the plan with `lifecycle_coordinator_unavailable`, and `acquire` itself refuses with the
same reason, because `ReviewStore.begin_transfer` reaches `acquire` directly and the execution-site
check alone would not cover it.

**That qualifier is load-bearing, and R5-P58 / F58-1 measured why.** The acquire-site half is keyed
on `_bound`, and `_bound` tracks *binder use* — nothing else. A graph whose registry **and** store
were attached by direct assignment only, never through `bind_lifecycle_authority`, therefore has
`_bound=False` and `_coordinator=None`; `acquire` treats it as a bare unit-test instance and
**grants a lease with no scan-in-flight arbitration**, including through
`ReviewStore.begin_transfer`. Measured: `bound_flag=false`,
`topology=lifecycle_coordinator_unavailable`, `acquire_direct=GRANTED`,
`store_begin_transfer=GRANTED`.

The execution-site layer still refuses, so `execute_transfer` refuses and **nothing moves** — and
the shape is not production-reachable, because `gui.py:6297` binds the registry and coordinator
through the API at import, so the module-default authority is `_bound=True` with its coordinator
established before `_clips_store()` can run. So: do **not** read this as unconditional protection
for arbitrary direct attribute assignment. The accurate guarantee is the one stated above, and
**any future production transfer integration must retain the binding precondition it rests on** —
see the R5-P59 integration acceptance gate below.

The discriminator is `LifecycleAuthority._bound`, write-once, set when the binder attaches the
authority to any subsystem. It answers one question only: is this a **live gate** that owes its
callers arbitration, or a bare instance a unit test built to exercise the lease algebra? A bound
gate with no coordinator refuses; a never-bound instance is left alone, which is what keeps the
pre-R5-P51 P1 suites valid **without** weakening the runtime contract. Production is always bound.

**Linearization point: the single critical section under `_BINDING_LOCK`.** Every decision and
every write happen inside it, decision first, and both re-read the live attributes rather than a
snapshot — F56-1 was precisely a decision taken against a snapshot followed by a write that re-read
nothing. `_BINDING_LOCK` is a plain module-level lock used *only* by the binding API; it adds **no
edge** to the lifecycle order, because nothing holding a lifecycle lock ever takes it and
`bind_lifecycle_authority` takes no other lock at all while holding it. Nothing is hashed, copied,
journalled, fsynced or executed under it. An AST test pins that.

**Why a second binder cannot overwrite the first completed binding**, as a property rather than a
hope: the write loop assigns only where the live value is `None`, and it reads that value inside the
same critical section as the write. A second binder with a different authority finds a non-`None`
value in the decision phase and refuses; one with the same authority finds every target already
bound and writes nothing. Last-writer-wins is not expressible.

**The retired API is deleted, not deprecated.** `begin_rebind`, `end_rebind`, `is_rebinding`,
`acquiring_count`, `_rebinding`, `_acquiring`, `rebind_barriers` and `LIFECYCLE_REBINDING` are
**gone**, and a test asserts they stay gone. That is how F56-4 and F56-5 are dispositioned: there is
no barrier left to span the assignments and no barrier ownership left to enforce. R5-P56 was
explicit that a reachable weakness is not fixed by changing its documentation — so the operation
itself was removed. F56-6 goes with them: the decision phase precedes every write, so a refusal
cannot be partial.

**Superseded test assumptions, named rather than quietly dropped.** Two places asserted that an
*idle* authority could be replaced, and both are now their own negation:

* `test_clips_review_atomic_lifecycle.py::test_24` — its tail asserted *"once released, rebinding is
  permitted again"*. It now asserts the refusal is identical once the lease is released.
* `tests/test_clips_review_authority_rebind_safety.py` — R5-P55's 32-case hot-rebinding suite is
  replaced by the R5-P57 immutable-topology matrix (**39** collected cases; R5-P59 adds six more,
  for 45).

**Provenance of the two superseded suites, corrected by R5-P59 / F58-5.** They do **not** live in
the same place, and the earlier blanket claim that both were "preserved verbatim in the R5-P53 and
R5-P55 evidence packages" was inaccurate. Measured:

| Superseded material | Where it actually is | Verified |
|---|---|---|
| `test_clips_review_atomic_lifecycle.py` (the `test_24` tail) | `Evidence\R5-P53-PR75-ATOMIC-LIFECYCLE-RESERVATION\artifacts\` | present, byte-identical to `39727bcc` and `6532bf81` |
| the 32-case rebinding suite | **Git only**, at `6532bf81c08519410775cfc6addb95c578de1813` (916 lines, 32 cases) — the R5-P55 commit that *created* it; confirmed absent in that commit's parent | retrieved and counted |

The rebinding suite is **not** in the R5-P55 evidence package: that package holds 29 manifested
files and no copy of it, and its `artifacts/source.diff` covers only
`src/beatsync_fork/clips_review.py`. **Nothing was lost.** This is a corrected provenance
*statement* — not a recovered file, not a previously missing source, and not a retention failure.
No historical evidence package was edited, repaired or regenerated to match, and no Git history was
rewritten.

**Nothing else was weakened.** Initial attachment, lazy attachment, same-authority idempotence
(including under a live lease and under a live reservation), the reservation/lease exclusion in both
orderings, epoch validation, bounded acquisition retries, scan-generation behaviour and per-owner
isolation are all unchanged and still asserted.

**What R5-P57 does NOT close**, stated so no reader infers clearance: a direct
`target.lifecycle_authority = other` assignment still bypasses the binding API — Python offers no
cheap prohibition, and the one such assignment the app performs (in `_clips_store()`) is an
*initial attachment* onto a store constructed three lines earlier, pinned by an AST test rather than
assumed. `lifecycle_topology_fault` catches a bypass that **precedes** the check — see the R5-P59
correction below for why that is not the same as catching *any* bypass.
Everything earlier phases left open stays open: **F5**, **cross-volume source removal**,
**decision-time content fingerprints**, **cross-process locking** (process-local, and never to be
described otherwise) and **GUI/HTTP integration** — the engine is still unwired, Confirm still moves
nothing, and `gui.py` was **not modified by R5-P57 at all**. Independent review **R5-P58** is
required.

### [R5-P58 / R5-P59] The independent verdict was QUALIFIED, and what that does and does not mean

**R5-P58 ran and returned `R5_P58_PR75_IMMUTABLE_AUTHORITY_TOPOLOGY_QUALIFIED`.** All three R5-P56
MAJORs were independently reproduced on the frozen `6532bf81` control to a measured unsafe outcome
and all three flip to safe on the candidate. Across 16 Rule C combinations, 12 coordinator
write-once cases, 11 `_bound` cases, 8 threaded concurrency experiments (up to 24 concurrent
binders), 12 lock/exception cases and 16 independent mutations, **no API-reachable path was found**
that replaces a bound component's authority, retargets an established coordinator or manufactures a
split gate.

**QUALIFIED is not an unconditional pass, and must never be restated as one.** Six MINOR findings
were recorded; none is a reachable vulnerability, and four were over-strong statements in this
project's own prose rather than defects in the code. R5-P59 dispositions them. Two things that do
**not** follow from the verdict:

* **Isolation safety is not product readiness.** The engine is unwired — no production transfer
  caller, no HTTP route — so every reachability judgement in R5-P58 is a judgement about *today*.
* **The immutable-authority design is not reopened.** R5-P59 deliberately does **not** add guards
  against direct Python attribute writes, which are explicitly outside the supported API. Three
  successive protocols were broken by trying to make a hot replacement safe; the capability's
  removal is the fix, and narrowing a theoretical direct-assignment hazard is not a fourth attempt.

**F58-2 — a raising assignment does not always leave a partial state.** The binder's docstring says
a raising assignment leaves a partial state that `lifecycle_topology_fault` reports at execution
time. That is right for a *later* write and wrong for the **first** one. Three outcomes are
possible, and they are not the same thing:

| Case | Resulting graph | Detected by `lifecycle_topology_fault`? |
|---|---|---|
| **No state change** — the *first* write (`registry`) raises | nothing assigned; `_bound=false`, `_coordinator=None` | **No.** The unbound/unbound pair returns `""` via the documented legacy carve-out, and `acquire` grants. |
| **Detectable partially bound state** — a *later* write raises | some components bound, others not — a split | **Yes** — `lifecycle_authority_incoherent`, refused before any lease |
| **Fully bound coherent state** — all writes succeed | one authority, coordinator established | n/a — this is the supported state |

The first case is **not** a partial-state refusal and must not be described as one. It is also not
a regression: the graph is left exactly as permissive as it was *before* the bind, the caller does
receive the exception, and the lock is free. Only a hostile subclass with a raising property setter
reaches it. ⚠ The superseded wording still stands in the `bind_lifecycle_authority` docstring
(`src/beatsync_fork/clips_review.py`, "Exceptions." paragraph); R5-P59 was not authorized to modify
that file, so **this table is the authoritative statement** and the docstring is a known residual
to correct in the next change that legitimately touches it.

**F58-3 — `lifecycle_topology_fault` checks the topology at the point it runs, and no later.** It
runs **once**, at the top of `execute_transfer`, before the lease. So:

* it catches incoherence **already present at that boundary** — which is the F56-2 shape, and is
  what it exists for;
* it **cannot by itself** guarantee safety against a *subsequent* unsupported direct attribute
  mutation. R5-P58 measured a direct `registry.lifecycle_authority = other` performed **after** the
  check and before publish reaching a completed, irreversible move (`revoke=ACCEPTED`,
  `owner_of=<revoked:unknown_session>`, `state=COMPLETE`, `moved=1`). The check *would* have caught
  it if re-run — it reported `lifecycle_authority_incoherent` at the instant of the swap.

There is **no production caller** that performs such a mutation. The correction here is to the
claim, not to the code: R5-P59 deliberately adds **no** extra pre-rename check, because a second
check would only shrink the window rather than close it, and turning this into a new
synchronization feature is the mistake R5-P51 → R5-P56 made three times. The operative rule is a
runtime obligation instead: **runtime use must preserve the immutable authority bindings
established by the supported API.**

⚠ **Residual, for F58-1, F58-2 and F58-3 alike.** All three over-strong claims also appear in the
`bind_lifecycle_authority` docstring in `src/beatsync_fork/clips_review.py`, which R5-P59 was **not**
authorized to modify (§10 of its work order names that file under "no implementation changes"). So
the superseded wording still stands there, and **this rule file is the authoritative statement of
all three corrections**. The docstrings are documentation-only, change no behaviour, and should be
brought into line by the next change that legitimately edits that file — the GUI/HTTP integration
phase being the obvious candidate, since it must satisfy the acceptance gate below in any case.
This is flagged rather than quietly accepted because a stale docstring is exactly how an
over-strong claim survives the closeout meant to retire it.

**F58-4 — the topology check does not validate the coordinator's own authority pointer.**
`lifecycle_topology_fault` proves that the registry and the store consult one authority, and that
the authority has *a* coordinator. It does **not** independently prove that the coordinator's own
`lifecycle_authority` is that same object. By direct mutation one can reach registry+store on `A`
while `A._coordinator` is a `ScanCoordinator` bound to `B`; a scan through that coordinator reserves
on `B`, so `A`'s epoch never moves and `acquire`'s epoch validation cannot see it. R5-P58's
conclusion, recorded as it was reached: the mismatch **was constructible by unsupported direct
mutation**, **no supported binding-API path created it** (`API_REACHABLE: false` — every ordering is
refused by Rule C), and **no production caller created it**. A depth-of-check observation, not a
defect.

> **GUI/HTTP integration acceptance gate (R5-P59, mandatory).** A runtime about to expose
> destructive transfer must prove that the **registry, the store, the coordinator and the
> authority's own coordinator reference form one coherent bound topology** — all four, by identity,
> with `_bound` true and `_coordinator` established — before any transfer entry point is reachable.
> F58-1 through F58-4 are each evaluated against reachability *today*, with the engine unwired;
> wiring the GUI or adding an HTTP route **invalidates that evaluation** and requires re-deriving
> all four. This gate is recorded here and deliberately **not implemented** by R5-P59.

**F58-6 — Rule C's identity comparison is now pinned.** R5-P58's independent mutation M1 relaxed the
binder's `current is not shared` to `current != shared` and **survived the entire 8237-case suite**:
the identity-vs-equality pin existed for `lifecycle_topology_fault`
(`test_29b_…identity_not_equality`) but not for the binder one layer up. R5-P59 adds the symmetric
pin — `test_34` (behavioural, four shapes), `test_35` (the reservation ordering) and `test_36` (the
source contract) in `tests/test_clips_review_authority_rebind_safety.py`.

The classification is stated rather than inflated: M1 is an **equivalent mutant for every authority
this repository can construct**. `LifecycleAuthority` defines no `__eq__`, no subclass of it exists
anywhere in `src/`, and the single production bind site omits `authority=` entirely. It is a
**mutation-coverage gap, not a reachable vulnerability** — and in particular **not** a destructive
MAJOR. Under the mutant the binder returns a **false `ACCEPTED`**, but phase 2's write-once `is None`
re-read still refuses to overwrite a bound slot, so **the replacement does not happen and no clip
moves**. Measured both ways. That is exactly why the new tests assert **the refusal** and not "the
clip stayed put": a test scored on the filesystem alone passes against the mutant and pins nothing.


**Lock order is unchanged and still acyclic:**
`ScanCoordinator` → `ClipRegistry` → `ReviewStore` → **`LifecycleAuthority` (leaf)**. Every nested
edge runs downhill; the only method that reads another subsystem is `acquire`, and it holds no lock
of its own while doing so.

**What R5-P53 does NOT close**, stated so no reader infers clearance: **F5** (the rollback target is
still never no-replace-probed), the **cross-volume source removal** (still withheld, still an owner
decision, no `ctypes` and no native helper), **decision-time content fingerprints** (today's P1 HTTP
path still records none, so no existing decision is transferable), **cross-process locking** (the
model is process-local and must not be described otherwise), and **GUI/HTTP integration** — the
engine is still unwired, Confirm still moves nothing, and `gui.py` was **not modified by R5-P53 at
all**. Independent review **R5-P54** is required.

> **R5-P54 ran, and returned `R5_P54_PR75_CHANGES_REQUIRED`.** Both R5-P52 MAJOR races were
> independently confirmed closed, and one new MAJOR — **F54-1** — was reproduced 3/3 in the
> rebinding gate described immediately above. R5-P55 corrects it; see the next section. Four
> lower-severity findings (F54-2, F54-3 MINOR; F54-4, F54-5 ADVISORY) remain open and are
> enumerated at the end of that section.

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
too. `_append` therefore closes off an unterminated tail before writing after it. A tail that does
not parse becomes a self-contained discardable line (`journal_tails_repaired` counts it); a tail
that parses whole is a record and is simply terminated (`journal_tails_completed` counts it — see
"A newline-less line is not necessarily an unfinished event"). Either way every fully committed
record before it is preserved and the next record is intact.

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

#### A newline-less line is not necessarily an unfinished event

[R5-P15 / N7-R3] **R5-P13 stamped that marker onto *every* newline-less tail, and for one shape
that destroyed data.** An independent review measured it: a decision that had been **reported
successful**, whose line lost only its terminating newline — one byte, which external truncation is
enough to do — was recovered correctly on the next start, turned into "damage this process
accounted for" by the marker, and erased by the following `compact()`. The parent commit, which
wrote a bare newline, preserved it. The reasoning that licensed this ("an unterminated line is one
whose decision was never reported successful") is true of **this writer's own interrupted writes**
and not of the file, which is exactly the gap the N7-R2 half of the same commit exists to close for
*terminated* damage. **Do not restore unconditional marker-stamping**, and do not reintroduce any
claim that newline-less implies unfinished.

The discriminator is the strict validator read the other way round: **truncating one of this
writer's records always leaves JSON that will not parse**, because the closing brace is the last
byte before the newline. So a newline-less final line that parses *whole* was written whole.
`_terminate_damaged_tail` therefore classifies before it writes anything —
`_classify_unterminated_tail`, over a bounded backwards read (`_journal_tail_bytes`, capped by
`MAX_TAIL_INSPECT_BYTES`; identifying one line must never become a pass over a journal holding
`COMPACT_AFTER_RECORDS` records):

| Final unterminated line | Answer |
|---|---|
| Parses whole **and** satisfies the replay validator | **complete the record**: write the missing newline and nothing else, preserving its exact original bytes. `journal_tails_completed` counts it; no marker is written |
| Does not parse, and no whole JSON value is hiding in front of the bytes that stopped it | the F4 torn fragment: marker-first repair, exactly as above. `journal_tails_repaired` counts it |
| Parses whole but describes no record this build can account for | `PersistenceError`, nothing written |
| A complete value followed by unidentifiable bytes — **including bytes that are not UTF-8 text** (R5-P17 / N7-R6) | `PersistenceError`, nothing written |
| Damage this writer cannot have produced — most importantly unidentifiable bytes *in front of* a whole record (R5-P19 / N7-R7) | `PersistenceError`, nothing written |
| Cannot be read, or is larger than the inspection bound | `PersistenceError`, nothing written |

Validation uses the **same** `_apply` the replay path runs, against **throwaway** dicts — a tail is
never published merely by being classified, and the reader can never refuse a tail the repair
accepted.

**Crash-order, stated separately for the two repairs, because they are different writes:**

* *Completing a valid record* is a **one-byte** write. `os.write` either accepts that byte or it
  does not, so there is no partial state to recover from and no evidence marker is needed: the
  record was already whole, and the only thing in question was its terminator.
* *Repairing a torn fragment* is the marker-plus-newline write whose internal order is load-bearing,
  unchanged and still frozen: every prefix of it leaves the fragment unterminated, hence still the
  trailing damage, so the next append repairs it again. Calculate the real payload length when
  testing this — the production timestamp makes it **46 bytes**, not the 31 of a synthetic
  `"at":0.0` fixture, and a fixture's length is not evidence about the writer's.

**The fourth row is the one combined shape worth naming.** A complete record with a *partial* marker
behind it can only come from an earlier build — the corrected repair never writes a marker after a
line that parses. Both available answers destroy data: stamping a marker over it is N7-R3 with an
extra step, and discarding it as marker-accounted damage is N7-R3 reached through the **read** path,
which is reachable because a short write really can land a whole marker and still miss its newline.
Telling "a committed decision" from "a record mid-write" needs provenance this format does not
carry, so `_replay_journal` consults `_tail_hides_a_complete_record` **before** the marker amnesty
and both paths refuse. Nothing is destroyed: every byte is kept, and removing the stray marker
recovers every decision at its original revision.

A reported success now means the complete record is on disk and recoverable. **A write that fails
raises `PersistenceError` and the endpoint answers 503** — the operator must never see a tick for
a decision that is not on disk. Do not swallow it, and do not reintroduce a single unchecked
`os.write`.

[R5-P5 / N2 · R5-P7 / N6] **Failing to *inspect* the journal is not an answer either.** The tail
probe raises `PersistenceError` when it cannot determine whether the tail is terminated: an
unknown journal state is not a licence to append, nor to write a repair into a file whose
condition could not be read.

#### Bytes written and text successfully decoded are different facts

[R5-P17 / N7-R6] **A torn write stops on a byte boundary, not a character boundary, so the writer
can leave a journal line that is not UTF-8 text at all — and both halves of the store got that
wrong in opposite directions.** An independent review interrupted a real `decide()` one byte into
the UTF-8 encoding of `é` in an identity. `_classify_unterminated_tail` read the
`UnicodeDecodeError` as proof of an unfinished write, the marker-first repair fired, the next
decision was **reported successful** — and then every restart refused the store, because
`_replay_journal` decoded the whole file strictly in text mode and one undecodable byte anywhere
aborted the entire replay. Not only was the reported success unrecoverable; the decisions
committed *before* the tear were unreachable too, and a `compact()` from the still-live store
then truncated the only bytes that held the fragment. The ASCII equivalent recovered normally
throughout, which is what made the gap invisible. The same shape was present on the parent
commit, so it is an uncovered F4/N2 preservation gap rather than a regression.

**The invariant this adds: a decision may be reported successful only if the replay mechanism
will accept the journal it leaves behind.** Writer and reader must agree about what is
recoverable; a classifier that grants a repair the reader will refuse is a false success however
carefully each half is written on its own.

So the journal is read **as bytes and decoded one line at a time**, and an undecodable line is
classified rather than fatal — by the same three questions in the same order, asked bytewise:

| Undecodable journal line | Answer |
|---|---|
| A whole JSON value sits in front of the bytes that stopped the decode | `PersistenceError`, nothing written — it may be a committed decision, and this is checked **before** the marker amnesty |
| The bytes are not a shape this writer can have produced (R5-P19 / N7-R7) | `PersistenceError`, nothing written — **and the marker amnesty below is skipped**, because a forged-looking or externally damaged line may be hiding a whole record the amnesty would discard unseen |
| It ends with a complete, valid `tail-repaired` marker | discarded; this process's own repair already accounted for it |
| It is **terminated** and carries no valid marker | `PersistenceError`, nothing written — terminated damage is external, wherever it sits |
| It is the final line, unterminated and unaccounted for | the F4 torn fragment: tolerated on read, marker-repaired on the next append |

`_bytes_hide_a_complete_record` and `_bytes_damage_is_accounted_for` are the byte-level entry
points, and the second one **delegates to the text validator** — one marker validator, reached two
ways, so the repair and the reader cannot drift apart. A genuine interruption inside a character
can never reach the first row: the character is inside the record, so the text before it is always
unbalanced JSON. That is pinned by a real-writer matrix over two-, three- and four-byte characters
at **every proper prefix** of each, not argued from the shape.

**Hard constraints on that byte path, all of them load-bearing:**

* **No lenient decoding, anywhere.** No error handler is passed to any decode on this path —
  nothing dropped, nothing replaced, no surrogate escapes. A substituted byte would turn an
  unaccountable record into a readable-looking one, which is the failure mode being removed.
  `_longest_text_prefix` decodes a strict *prefix* and is not an exception to this: it answers one
  question whose only positive answer is a **refusal**, so reading less than is there can cost a
  reason to refuse and can never manufacture a reason to accept. The guard that keeps this true is
  structural — it reads the call sites off the syntax tree — because a substring scan would match
  this very rule's prose, which is how such a guard gets deleted instead of satisfied.
* **Do not widen the inspection bound to reach a Unicode tail.** `MAX_TAIL_INSPECT_BYTES` still
  governs the *writer*: an oversize final line is refused rather than classified, while the reader
  tolerates it as the torn tail it is. Both halves are asserted.
* **Do not relax the marker check.** An interrupted marker leaves fewer bytes than the needle, or
  a needle with nothing parsable after it; either is `False`, the fragment stays unterminated, and
  the next append repairs it again. One byte short of the closing brace is not evidence.
* **Two deliberate consequences of leaving text mode.** Byte offsets in the refusal message are
  now counted from the start of the file rather than from whichever decode buffer the text layer
  happened to be filling, and a lone `\r` no longer separates records. This writer emits `\n`
  through an `O_BINARY` descriptor and never produced either shape.

**The second confirmed route, closed by the same classification.** A *complete, committed* event
with an invalid byte appended refuses on a fresh load — it always did — but an **already open**
store marker-stamped it as damage and reported a further decision successful. That is observable
at the HTTP boundary: pre-fix the second decision returned `200 ok:true`, now it returns 503
`not_durably_recorded` with both durable files byte-identical. The fresh-load 503 never covered it,
which is why the warm-store path is tested explicitly. `src/clips_media_server.py` is unchanged by
this: raising the right class is the whole fix.

Error text stays path-free, as everywhere else on this boundary. It may legitimately mention UTF-8
and a byte offset — that is the N7-R2 diagnosability contract — but never a pathname, a filename or
a record's contents.

**This is not universal recovery from arbitrary corrupt bytes, and must not be described as one.**
It bounds exactly which undecodable fragments are repairable, and everything else refuses without
writing a byte.

#### A fragment is torn only where it can be *shown* to be torn

[R5-P19 / N7-R7] **Every classifier up to R5-P17 reached "torn fragment" by elimination, and
elimination is unsound here.** The questions it asked were all of the form *have I found a reason
to refuse?* — a whole record in the readable prefix, a valid marker, a terminator. An independent
review built the one input for which no such reason can ever be found: it put a single invalid
byte **in front of** a whole committed decision event and removed that event's newline.
`_longest_text_prefix` reads from offset zero, so the readable prefix of that line is **empty** —
and empty was being scored as "no complete record is hiding" when what it actually means is
"nothing could be read at all". Measured consequence, in order: cold recovery reported the
committed identity as `PENDING,0`; a warm `decide()` returned `SELECTED,1`; the repair stamped the
intact record as accounted-for damage; `compact()` truncated the journal and snapshotted the loss.
Through the unchanged ASGI middleware the warm decision answered **HTTP 200 `ok:true`** with
counts, and the previously accepted `REJECTED,2` reopened as the older `SELECTED,1`.

The correction replaces elimination with a **positive** test derived from the writer, not from the
shape of the damage. `_append` writes exactly two payloads through `_write_all`, which writes every
byte or raises, and the newline is the last byte of each — so an interruption leaves a *proper
prefix* with no newline in it, and the next append may leave another against it. An unterminated
line this writer produced is therefore, and can only be:

```
<proper prefix of one record>  [<one truncated multi-byte character>]  <repair-marker prefixes>
```

Three properties of that template do all the work, and each one refuses a shape that previously
destroyed data:

* **the readable part must be an unfinished record** — it opens with `{`, it does not parse whole,
  it is **grammatical all the way through**, and no whole JSON value starts at any position where
  an object *structurally* opens inside it. An *empty* readable part is not an unfinished
  record, which is the N7-R7 defect itself; `{x` followed by an entire committed decision is not
  one either, which is the same loss with a plausible-looking first byte.
* **the undecodable part must be a well-formed partial character** (`_is_truncated_character`),
  not merely unreadable bytes. A strict decode cannot answer this — an incomplete sequence and an
  invalid one raise with the same `start` and `end` — so the UTF-8 ranges are written out, and
  overlong, surrogate and out-of-range prefixes are rejected because no encoder here emits them.
  This is what refuses a leading `0xff` in front of an intact event.
* **whatever follows it must be repair evidence and nothing else**
  (`_is_repair_evidence_so_far`). This is what refuses the same committed event hidden behind a
  *genuine* truncated character. Two chains the real writer does produce, and which an earlier
  draft of this grammar wrongly refused, are pinned by test: `…,"at":` immediately followed by a
  whole second marker, and the 12-byte stub `{"k":"tail-r` immediately followed by one.

**The marker amnesty is now conditional on that test, not preceded by it.** The amnesty runs only
for damage the writer could have left, because the measured loss includes a variant where the
corrupted line *already carries a valid marker* — and the amnesty would discard it without ever
being able to see the record inside. Gating rather than reordering is deliberate: every existing
refusal message, including the N7-R2 byte-offset one, stays exactly where it was.

Hard constraints, all of them load-bearing:

* **Do not restore classification by elimination.** "I did not find a record" and "I could not
  read anything" are different answers and the second one must refuse.
* **Do not strip, skip or leniently decode the invalid bytes.** Automatic recovery from externally
  corrupted leading bytes is explicitly *not* attempted — the refusal is an availability cost, and
  the record is still on disk byte for byte, so removing the foreign bytes recovers every decision
  at its original revision. A test asserts exactly that.
* **Do not treat a `{` byte as identifying a record.** The interior probe in
  `_is_an_unfinished_record` never applies, parses out or trusts what it finds; finding a whole
  value there is only ever a **reason to refuse**, which is why acting on "this might be an
  object" is safe here and would not be anywhere else. Its budget (`MAX_HIDDEN_RECORD_PROBES`)
  exists so one pathological line cannot make a linear scan quadratic, and **exceeding it is a
  refusal** — a fragment whose interior could not be ruled out is not ruled out.
* **And do not treat a `{` *character* as identifying one either.** [R5-P21] The probe may only be
  asked at positions where an object genuinely opens; see the next subsection for why, and for why
  that is not the same as skipping braces inside strings.
* **Do not broaden the marker amnesty.** `_damage_is_accounted_for` is still the only validator
  that decides whether a complete marker accounts for damage; `_is_repair_evidence_so_far` only
  bounds what may be *present* in the suffix. **N7-R4 is untouched**: a forged marker on a
  writer-shaped fragment is still accepted, and that remains an open, accepted limitation.
* **The same rule applies to the readable path.** Decodable-but-unidentifiable bytes in front of a
  whole committed event (`xx{…}`) reached the identical loss, because
  `_tail_hides_a_complete_record` also looks only from offset zero.
  `_text_is_an_interrupted_write` closes it, by splitting the line into the fragment the writer
  tore and the evidence it appended, and requiring both halves to be what the writer can leave.

*Residual, recorded rather than claimed closed:* a non-finite `at` (`NaN`/`Infinity`) is not
writer-shaped under `_TAIL_REPAIR_NUMBER`, so such a line refuses rather than being repaired.
`time.time()` cannot produce one, the direction is fail-closed, and the separate question of
whether the marker *grammar* should exclude them (raised by R5-P18) is an owner decision that this
correction does not make.

#### A brace the writer put inside a string is not a record boundary

[R5-P21] **The positive test above was right and its interior probe was written wrongly, and an
independent R5-P20 review measured what that cost.** The probe asked `raw_decode` at *every*
interior `{`, including one inside a JSON string — so the real writer's own
`identity="clip-{}é"` made the `{}` in that string look like a whole hidden object, and a
**genuine** torn write was classified `unaccounted_damage`. Reproduced here on all three of the
reviewer's mandatory inputs through the public API with a real short `os.write`: the interrupted
`decide()` raised as it should, and then cold recovery *and* the next warm append both refused, so
decisions committed before the tear became unreachable until someone edited the file by hand. The
immediate parent recovered every one of them. A fail-closed availability loss rather than a data
loss — but it broke the F4/N6 torn-write guarantee for the core API, which R5-P19 had explicitly
promised to preserve.

**The fix is a grammar, and `_structural_object_openings` is the whole of it.** It validates the
fragment as a *proper prefix of one JSON object* and returns the positions at which an object
structurally opens inside it; `None` means "not a prefix of one record", which refuses.

**Skipping braces inside strings would not have been enough, and must not be substituted for
this.** A quote-tracker can be talked into reading a committed event as somebody's text: `{"a":"`
in front of a whole `{"k":"decide",…}` opens a string that the *event's own first quote* closes,
and from there the tracker is scanning the event's bytes as string content. Requiring the whole
prefix to be grammatical removes the hiding place rather than stepping around it — there is no
unexplained prefix left for anything to sit behind, so a string's boundaries are where the grammar
says they are instead of where a heuristic guessed. `{"a":"{"k":…` fails at the bare `k`, exactly
as `{x{…}` fails at the `x`. Eight such shapes are pinned by test, and they need three *different*
guards between them: the grammar refuses the opened-string ones, the structural probe refuses the
nested-object and array ones, and `_is_repair_evidence_so_far` refuses the ones where the event
sits where only marker prefixes may sit. Any one of the three reverted to a brace-skipper passes
some of those rows and discards a committed decision on the others.

Consequences worth stating, because each is a thing not to undo:

* **The probe budget counts structural candidates only.** Literal braces in an identity are
  content and consume none of it, so a legitimate value with 400 of them is still recoverable.
  `MAX_HIDDEN_RECORD_PROBES` is unchanged, and exceeding it is still a refusal.
* **Leniency is placed on purpose, and in one direction only.** Inside a token the scanner is
  looser than JSON — `01` and `1e` pass as numbers, string contents are not validated — because
  the caller has already established that no whole value parses at offset zero. A token this
  scanner accepts but JSON would reject can therefore only make the prefix look *finished*, which
  is a refusal. At every token *boundary* it is exact.
* **The fragment/evidence split is found, not assumed.** The old 26-character
  `_TAIL_REPAIR_OPENING` anchor cannot see a repair interrupted before it got that far, and the
  resulting stub stayed glued to the fragment. That was harmless while the fragment only had to
  *not parse*; it is not harmless now that it has to be grammatical, because the ordinary tear
  lands inside a string and the stub's own `"` closes it. Candidates are walked from the right and
  the walk stops at the first suffix that is not repair evidence — sound because evidence validity
  is monotone in that direction — bounded by the same probe ceiling, and exhausting it refuses.
* **Nothing here recovers anything.** No embedded event is ever applied, extracted or trusted; the
  only outcomes remain "torn, which the caller knows how to repair" and "unaccountable", and the
  second writes no byte. No lenient decoding, no byte substitution, no new format, no dependency.

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
| An **unterminated** final journal line that **parses whole** | replayed as the ordinary event it is — it was written whole, only its terminator is missing (R5-P15 / N7-R3) |
| An **unterminated** final journal line that does **not** parse — *including one that is not UTF-8 text* — **and is a shape this writer can have produced** | that one line discarded, everything before it preserved (F4 · R5-P17 / N7-R6 · R5-P19 / N7-R7) |
| An unreadable journal line that **is** terminated, anywhere in the file | `PersistenceError`, nothing written |
| A complete value followed by unidentifiable bytes, anywhere in the file — *text or not* | `PersistenceError`, nothing written — checked **before** the marker amnesty below (R5-P15 / N7-R3 · R5-P17 / N7-R6) |
| Unidentifiable bytes *in front of* a whole record, or any other shape this writer cannot have produced — *text or not* | `PersistenceError`, nothing written; the marker amnesty is **skipped** for it (R5-P19 / N7-R7) |
| An unreadable line carrying its own attached `tail-repaired` marker — *even when the fragment it accounts for is not text* | discarded; the repair already accounted for it (R5-P17 / N7-R6) |
| A parsable event with a schema or kind this build cannot account for | `PersistenceError`, nothing written |
| Bytes that are not valid UTF-8, in a line none of the rows above account for | `PersistenceError`, nothing written. **Not** a blanket rule about the file: one undecodable byte no longer condemns the whole journal, which was the N7-R6 defect (R5-P17) |
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
are ones that never represented a reported success — an unterminated fragment **that does not
parse and that this writer can be shown to have produced** (R5-P19 / N7-R7), or one already closed
off by its own marker. A newline-less line that parses whole is a record, not a fragment, and it is
kept (R5-P15 / N7-R3). A damaged line whose history cannot be accounted for never becomes
compactable at all: the store refuses before `compact()` can be reached.

There is **no migration concern** from this tightening: R5 is unmerged, so no deployed install has
ever written a `state/clips-review/` journal or snapshot.

#### Two bounded limitations, recorded rather than claimed closed

[R5-P15 / N7-R4 · N7-R5] Both were raised by an independent review, both were classified as
non-blocking, and **neither is closed by the N7-R3 correction, nor by the N7-R6 one, nor by the
N7-R7 one.** They are written down here so that no later change can quietly assume they were. In
particular the byte-level marker check added for N7-R6 makes the marker *findable* in a line that
is not text; it does not make it *authentic*, and nothing about N7-R4 changes. N7-R7 narrows
*when* the amnesty is consulted — only for writer-shaped damage — but a forged marker on a
writer-shaped fragment is still accepted, which is exactly N7-R4 and is still open.

* **N7-R4 — `tail-repaired` is an auditable format convention, not authenticated evidence of its
  own origin.** Anything that can write to the journal can write a well-formed marker, and
  `_damage_is_accounted_for` returns `True` for it; a review appended a forged marker to unrelated
  corrupted content and the discard was accepted. That is **outside** the accidental-corruption
  model this design targets — random damage does not synthesise a valid marker object — and closing
  it would need provenance the format does not carry. So: do **not** describe the marker as proof of
  origin, and do **not** restore the earlier claim that a marker "can only reach a line through that
  repair" — that was a statement about this writer, not about the file, and N7-R3 falsified its
  load-bearing half without any forgery at all. What is true is narrower and worth keeping: the
  repair only fires on an unterminated tail, it never marks a line that parses whole, and the
  amnesty is reached only after `_tail_hides_a_complete_record` has ruled out a whole record in
  front of it. No signature, MAC, trust store or journal redesign is authorised for this.
* **N7-R5 — duplicate JSON keys make readable content ambiguous.** Ordinary JSON parsing keeps the
  *last* duplicate, so a syntactically valid snapshot whose `records` property appears twice loads
  as the second one, with `corrupt_records_discarded` at `0`. The legitimate writer cannot produce
  it (`json.dump` of a `dict` has no duplicate keys) and it is not a plausible shape for random
  corruption, so it is bounded and non-blocking. No duplicate-key parser redesign is authorised
  for this.

Recovery is **not** universal: these rules bound what *can* be recovered from external corruption
and make everything else refuse without writing. Do not restate them as recovery from arbitrary
damage.

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
