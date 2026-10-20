# Operating policies (always active)

This rule is deliberately **unscoped**: these policies govern every task in this repository, not a
subsystem. They are the repository-local, durable copy that travels with the repo across machines.
Keep the `DU-REPO-WORKFLOW-v1`, `PCBUS-HK-v1` and `PCBUS-WATCHDOG-v1` markers; `PCBUS-HK-v1` is a
synchronisation marker, not a version number — do not bump it for wording changes.

# Repository Workflow Policy: DU-REPO-WORKFLOW-v1

Repository-local rule (authored here, not synced from any machine-local file). It governs what a task
must do to count as finished.

## Mandatory task closeout — commit and push

**For every task that makes authorized repository changes, the default is: complete the work, commit it,
push it, and verify the remote.** Authorized work left only on local disk is not finished. The purpose of
this rule is that every completed implementation task leaves a durable GitHub state that can be
independently reviewed afterwards.

1. **Complete the requested work fully.** Not the easy part of it — all of it.

2. **Before committing, verify all of the following:**
   - the current branch;
   - `git status`;
   - the **complete** diff, not a summary of it;
   - that only files authorized by the task are included;
   - that no unrelated pre-existing user changes are staged or committed;
   - that all tests/checks applicable to the task have been run;
   - that every mandatory acceptance gate the task defines has passed.

3. **Commit** the completed authorized work.

4. **Push** the resulting commit to the appropriate remote branch.

5. **Verify the push succeeded** by comparing the local HEAD against the *actual* remote branch HEAD —
   query the remote, do not trust a possibly stale remote-tracking ref.

6. **End the task report with at least:**
   - branch name
   - commit SHA
   - remote branch SHA
   - changed paths
   - tests/checks run, and their result
   - whether the working tree is clean
   - whether push verification passed

## Exceptions — when NOT to commit or push

**An explicit task instruction always overrides the default above.** Do not commit or push when the
current task says anything equivalent to: READ ONLY · NO EDITS · NO COMMIT · NO PUSH · NO PR ·
local experiment only · investigation/audit only.

**Never commit or push merely to make a failed task appear complete.** STOP and report the exact blocker
instead of forcing a commit or push when:

- mandatory tests fail;
- an acceptance gate fails;
- the authorized scope cannot be satisfied;
- unexpected unrelated working-tree changes create ambiguity about what would be committed;
- the remote branch changed in a way that makes the planned push unsafe.

## Branch / remote safety

- Push the branch the authorized task was done on.
- Do **not** switch to `main` merely to satisfy this rule.
- Do **not** push implementation work directly to `main` when the task requires a feature-branch / PR
  workflow.
- Do **not** merge a PR unless the task explicitly authorizes the merge.
- **Never force-push**, and never rewrite published history, unless the task explicitly authorizes it.
- Before pushing, fetch or otherwise measure the target remote branch well enough to avoid blindly
  overwriting concurrent work.
- For documentation/governance tasks explicitly authorized directly on `main`, a normal fast-forward
  commit + push to `main` is allowed.

## Commit hygiene

- One completed task normally produces **one coherent commit**, unless the task asks for more.
- Commit messages describe the actual change.
- **Never `git add .`** when unrelated files may exist — stage explicit authorized paths.
- Preserve user-created or pre-existing uncommitted work that lies outside task scope.
- Runtime, cache and output files are not committed unless explicitly authorized — see the housekeeping
  policy below, whose *Git discipline* and *Source-control awareness* rules apply in full here.

## Post-push verification

A task is not durably complete until the pushed remote ref is verified. At minimum:

```
LOCAL_HEAD == REMOTE_TASK_BRANCH_HEAD
```

If they differ, report the mismatch and do **not** claim completion. Use the final state

```
TASK_COMPLETE_AND_PUSH_VERIFIED = YES
```

only when the requested work, the checks, the commit, the push, and the remote verification have all
succeeded.

---

# Housekeeping Policy: PCBUS-HK-v1

`Storage Policy: PCBUS-PF-v2`

Status: `LOCAL_POLICY_DURABLE` — this file is committed on the authoritative main line
(`origin/main` of `Digital-Union-Company/BeatSync-Engine`), so the policy travels with the repository
across machines. Keep the `PCBUS-HK-v1` marker; it is a synchronisation marker, not a version number —
do not bump it for wording changes.

**Two distinct markers, deliberately not merged.** `PCBUS-HK-v1` is the **housekeeping** marker and
is **preserved unchanged** — the hygiene rules it names are the same rules. `PCBUS-PF-v2` is a
separate **storage policy** declaration describing *where* project artifacts live and how a workspace
is owned and released. Adopting `PCBUS-PF-v2` does **not** bump the housekeeping marker, and
**`PCBUS-HK-v2` does not exist** — do not invent it, and do not claim any upstream housekeeping
marker was bumped.

**This section is operative and self-contained.** It is not a pointer to any machine-local global
configuration, and it must still govern correctly when this repository is read on a machine whose
global policy differs, is older, or is absent. Where it is weaker than a project-specific rule below,
**the project-specific rule wins**.

## The two roots are machine-scoped configuration

Project artifacts live under exactly two per-machine roots, referred to throughout as `<HOT>` and
`<DURABLE>`:

```
PROJECT_HOT_WORKSPACE_ROOT   -> <HOT>       active work      (fast local disk)
PROJECT_FILES_ROOT           -> <DURABLE>   durable storage  (large local disk)
```

**These are configuration identifiers, not paths, and this file deliberately does not hardcode their
values.** The resolution contract:

- **Every workstation has its own two declared roots.** There is no universal path, and no value is
  portable between machines.
- **Root values come from that machine's own verified configuration** — not from a guess, not from a
  value remembered from another machine, and not from a path that merely looks plausible.
- **Missing or unavailable root values mean STOP AND ASK.** If either root is undeclared, or a
  declared root is unavailable, there is no permitted location. Stop and ask for one.
- **Never guess a drive letter**, and never probe for one.
- **Never silently fall back** to `C:\tmp`, to `%TEMP%` / `$env:TEMP`, to another drive letter, or to
  a path inside this repository.
- **Never create a compatibility junction** from a retired location to either root. That resurrects
  the retired convention, hides which layer resolved a path, and defeats the reparse-point checks
  below.

This governs Claude-managed project artifacts only. It places no restriction on the OS or on this
app's own use of the system temporary directories, and the system `TEMP`/`TMP` variables are never
changed.

## Where things go

`<PROJECT_ID>` for this repository is **`BeatSync-Engine-DigitalUnion`**, the canonical identity
recorded in the machine's authoritative project registry. **Resolve it collision-safely:** it is a
*different project* from `BeatSync-Engine`, which is a separate repository on a different remote. A
name being a **prefix** of another name is **not** a match. Match the canonical identity exactly, or
an explicitly recorded alias that resolves unambiguously to exactly one project. If an identity is
ambiguous, collides, or cannot be resolved, **stop and ask** — never invent one from a directory
name, and never merge two projects because their names look similar.

| Purpose | Path |
|---|---|
| Active task workspace | `<HOT>\Projects\<PROJECT_ID>\Tasks\<TASK_ID>` |
| Isolated clone or Git sandbox | `<HOT>\Projects\<PROJECT_ID>\GitSandboxes\<TASK_ID>` |
| Build intermediates and caches | `<HOT>\Projects\<PROJECT_ID>\BuildCache\<TASK_ID>` |
| Short-lived scratch | `<HOT>\Projects\<PROJECT_ID>\Scratch\<TASK_ID>\<RUN_ID>` |
| Uncertain material **found on `<HOT>`** | `<HOT>\_QUARANTINE\<YYYY-MM-DD>\` |
| Verification records, manifests, hashes, sign-off | `<DURABLE>\Projects\<PROJECT_ID>\Evidence\<TASK_ID>` |
| Independent review material and reports | `<DURABLE>\Projects\<PROJECT_ID>\Reviews\<TASK_ID>` |
| Playtests and retained rendered media | `<DURABLE>\Projects\<PROJECT_ID>\Playtests\<SESSION_ID>` |
| Working backups | `<DURABLE>\Projects\<PROJECT_ID>\Backups\<DATE>\<DESCRIPTION>` |
| Completed task material | `<DURABLE>\Projects\<PROJECT_ID>\Archive\<YEAR>\<TASK_ID>` |
| Finalized outputs | `<DURABLE>\Projects\<PROJECT_ID>\Exports\` |
| Uncertain material **found on `<DURABLE>`** | `<DURABLE>\Quarantine\<YYYY-MM-DD>\` |

Put in a task workspace: downloads, intermediate outputs, extracted archives, debug exports, scratch
scripts, one-off logs, temporary patches, throwaway test payloads, comparison copies, temporary
screenshots.

**All new task workspaces are created on `<HOT>`** — this is not a judgement call. `<DURABLE>` takes
retained material only. Placing an active workspace on `<DURABLE>` requires owner approval or a
separate authorisation that already exists, recorded in the task's ownership record; **`<HOT>` merely
being unavailable is not such a reason** — without approval there is no permitted location, so stop
and ask.

**Plan the location before creating the file.** Do not invent other temp roots — not `C:\tmp`, not
`I:\tmp`, not `<repo>\tmp`, `<repo>\temp`, `<repo>\old`, `<repo>\backup`, `<repo>\scratch`, not
`Desktop\temp`. If a tool requires a repo-internal temp directory while it runs, remove it after the
run succeeds. Temporary storage is a workspace, never an archive: it must never be the only place an
evidence, acceptance or release artifact exists — see *Retention classes and the release rule*.

## Exclusive task ownership

A task workspace is owned by exactly one session, and ownership is established by **winning an atomic
exclusive claim — never by finding a directory that happens to exist**.

1. Resolve the canonical project identity, then choose a `<TASK_ID>` unique to this session.
2. **Verify the path is safe to claim.** Walk the intended workspace path and every ancestor up to the
   configured root, confirming each is a real directory and **not** a junction, symbolic link or other
   reparse point. A claim taken through a link silently writes outside the intended root, and the
   root rules above would stop meaning anything. Re-check immediately before any later destructive or
   irreversible operation — this is prose, so it can *refuse* a swapped path, not prevent one.
3. **The workspace directory must not exist, and creating it is itself the claim.** Create it with an
   exclusive **terminating** operation — `New-Item -ItemType Directory` with **`-ErrorAction Stop`**
   and **no `-Force`** — then confirm it actually succeeded. In PowerShell a cmdlet failure is
   non-terminating by default: it writes an error record, sets `$?` to `$false`, and *the next
   statement still runs*, so a sequence that creates the directory and then unconditionally writes an
   ownership record will adopt a live workspace and overwrite the real owner's record. **Never add
   `-Force`**, never combine a claim with `-ErrorAction SilentlyContinue`, `2>$null` or a swallowing
   `try{}catch{}`, and **never use `[System.IO.Directory]::CreateDirectory()` as evidence of
   exclusive creation** — it reports success on an existing directory either way.
4. **Only then** take a `.CLAIM` marker inside the new workspace with an exclusive create-new open and
   no sharing, treating an existing-file error as "another session owns this". Close the handle once
   it is durably written: exclusivity is guaranteed at the instant of creation, and a permanently held
   no-sharing handle would block the very ownership inspection these rules require. **A claim marker
   proves a claim was taken; it is not the ownership record.**
5. Write `WORKSPACE_LOCATION.json` immediately afterwards, **inside the workspace the task actually
   owns**, on whichever root that workspace is on. Record at minimum: project identity and how it was
   resolved, task identity, machine, owning session, root kind and workspace path, why that root was
   chosen, creation and liveness timestamps, outstanding retention obligations, and status. At
   completion it is preserved as the durable record by **copying** it to the task's separate durable
   archival or evidence destination — never by creating a `<HOT>` directory merely to hold a record.
6. **Never adopt a pre-existing task directory.** If any step fails, choose a different `<TASK_ID>`
   or stop and ask.

### Nested paths — the claim is taken at the task boundary, not the leaf

Directory creation creates missing intermediates silently, so an exclusive claim on
`Scratch\<TASK_ID>\<RUN_ID>` can succeed while creating or traversing a `Scratch\<TASK_ID>` that
another session already owns. **Leaf exclusivity alone is not sufficient.** Three kinds of directory:

| Kind | Example | Ownership |
|---|---|---|
| Approved **structural** | `Projects\<PROJECT_ID>\Scratch` | shared; owned by no task; **never claimed** |
| **Task ownership boundary** | `Scratch\<TASK_ID>` | **exclusively owned — this is what is claimed** |
| **Run child** | `Scratch\<TASK_ID>\<RUN_ID>` | inherits the boundary's ownership; needs no claim |

Claim the boundary first, write its marker and ownership record, and only then create the run child
beneath it. A later run of the same task may reuse an existing boundary **only if** its ownership
record is valid **and names the same owning session and task**. **A different session must never
claim or create a run beneath another session's task boundary.** Refuse and pick a new `<TASK_ID>`
when the boundary is foreign-owned, missing its marker or record, carrying a corrupt or ambiguous
record, or is a reparse point — and report it for owner decision rather than adopting, reusing or
cleaning it.

**Never delete a task subtree without first confirming this session owns the task-level boundary**, by
reading that boundary's ownership record. Stop, preserve and report whenever an unexpected nested
directory appears, any child names a different session, concurrent activity is detected, or ownership
cannot be established. **Do not assume ownership of a child merely because this task created the
parent.**

### Invalid records, orphaned claims and concurrent sessions

A workspace directory with no marker, a marker with no ownership record, or a record that is present
but unreadable, truncated, unparseable or missing a required field is an **orphaned workspace** —
preserve it, report it for owner decision, and never adopt, reuse, overwrite, clean or delete it, nor
take a claim marker inside it.

**An invalid ownership record is not an absent owner.** A missing or damaged record is the signature
of a crash mid-write, which is precisely when another session's data is most at risk. Never read
"available" from it.

**Never infer ownership, or staleness, from a modification timestamp.** Before touching anything,
re-check modification times **and** attempt an exclusive open. A path written recently, held open, or
lacking an ownership record naming this session is not yours. If files appear or change concurrently
and ownership cannot be established, leave them alone and treat them as externally owned.

## Capacity

Before creating a `<HOT>` workspace, evaluate

```
MIN_FREE_HOT = max(20% of <HOT> total capacity, 40 GiB)
```

from the **actual volume figures at that moment**. The formula is the rule; any specific number is
only its evaluation on one machine at one time, so no evaluated figure is recorded here.

- Approaching the reserve: report to the owner and list idle workspaces. **No deletion.**
- Below the reserve: **refuse new `<HOT>` allocations and fail loudly.** Running tasks continue
  undisturbed.
- Per-project allowances are advisory with reporting, never enforced filesystem quotas — a quota that
  fails mid-write can corrupt a working tree.

These checks are an **operator responsibility** performed at task creation and closeout. Nothing
automated performs them.

**Capacity pressure never authorises deletion of unknown or protected data.** Where low headroom meets
an unresolved retention obligation, the obligation wins: preserve the protected workspace, refuse the
unsafe allocation, report both halves (headroom against `MIN_FREE_HOT`, and which workspaces are
held), and request an owner decision. A full disk is an escalation, not a licence.

## Retention classes and the release rule

- **Disposable temporary artifacts** — scratch, build intermediates, caches, extraction directories,
  throwaway payloads. No retention obligation; do not demand a second copy of a conclusively
  disposable file merely because a task generated it.
- **Retained project artifacts** — reports, exports, completed task material. Copy to `<DURABLE>` and
  verify.
- **Unique evidence and acceptance records** — verification records, manifests, hashes, approval and
  sign-off artifacts, working backups, anything irreplaceable. Strongest obligation.
- **Recoverable or reconstructible outputs** — a re-derivation path substitutes for a second copy
  **only** when the regeneration is demonstrable in practice (inputs, toolchain and procedure all
  still exist and have been shown to reproduce the artifact) **and** the applicable retention contract
  permits re-derivation. A theoretical re-run is not equivalent to preserving unique historical
  evidence, and **anything attesting to a past state — what was observed, when, with what result —
  can never be re-derived.**
- **Independent backups** — a copy on a **different physical device**, or remote. Two directories on
  the same disk are **not** two independent backups, and a `Backups\` folder beside the data it
  protects is a *working* backup only.

> **Release rule.** A task workspace may be released only when every artifact it produced has
> satisfied its own retention obligation. For unique evidence, approval records, backups and
> irreplaceable artifacts that means **verified independent retention on a physical device other than
> the one holding `<DURABLE>`** — or on an explicitly authorised off-machine destination — **before**
> the source workspace is retired. Where only a `<DURABLE>` copy exists, the source workspace is
> **preserved, not released.**

**The reference point is `<DURABLE>`, not the source workspace.** `<HOT>` and `<DURABLE>` are normally
different devices already, so "a different device from where the work happened" would be satisfied by
every ordinary closeout copy and would protect nothing; the risk being managed is the single-device
exposure of `<DURABLE>` itself. Two consequences: a `<HOT>` copy that is itself scheduled for release
**does not count** as the surviving independent copy — a copy about to be deleted is not a backup —
and the obligation outlives the workspace, so releasing it must never be the event that reduces
unique evidence to a single copy.

**Tier-3 obligation when independent retention is unresolved.** If no independent off-device
destination is configured and authorised on this machine, then for every artifact in the unique-evidence
class: record the obligation as outstanding in the ownership record, **preserve the affected source
workspaces**, do not release or retire protected originals, and **do not invent, select or provision a
destination** — not another local volume, not attached or removable storage, not NAS, not cloud or any
remote. **An unresolved durability obligation is a reason to keep a workspace, not a reason to
proceed**, and it **overrides every ordinary cleanup instruction and category label in this file.**

## Task closeout order

```
CLASSIFY -> COPY -> VERIFY -> ACTIVATE DESTINATION -> CHECK RETENTION OBLIGATIONS -> CONSIDER SOURCE RELEASE
```

**No step may be skipped or merged into an earlier one.**

1. Verify the task's final state.
2. **CLASSIFY** every artifact into one of the classes above. Classification decides *where an
   artifact belongs*; **it never moves or deletes anything.**
3. **COPY** retained material to its canonical `<DURABLE>` destination. The source stays intact.
4. **VERIFY** integrity and provenance at the destination — file count and bytes, cryptographic hashes
   for evidence, preserved timestamps. **If verification fails the destination copy is not valid, the
   source is still the only copy, and the source must not be released, moved or deleted.** Fix or
   repeat the copy; never respond to a failed verification by trusting it anyway.
5. **ACTIVATE DESTINATION** — record the verified `<DURABLE>` copy as canonical in the ownership
   record, so later work does not resolve to the source.
6. **CHECK RETENTION OBLIGATIONS** by applying the release rule. If it is not satisfied, the workspace
   stays.
7. **CONSIDER SOURCE RELEASE** — mark a releasable `<HOT>` workspace eligible for *later assessment*,
   not for deletion.
8. Delete only the task's **own** conclusively disposable temporary files.
9. Preserve worktrees, unique commits, untracked and ignored files, stashes, and unverified artifacts.
10. Record remaining dependencies and retention obligations in the ownership record.

**A single source-destroying relocation is never an acceptable way to place an artifact that has not
yet been copied and verified at its destination** — not `Move-Item`, not a rename, not cut-and-paste,
not any cross-volume move. Across volumes such a move is copy-then-delete and removes the source
*before* verification is possible; on the same volume it is a rename leaving no second copy at all.
`<HOT>` and `<DURABLE>` are normally different volumes, so durable placement is normally the
cross-volume case. A direct move is permissible only for an artifact **already independently
protected** under the release rule, or one that is **conclusively disposable** — and the
classification must actually hold; the category label alone grants nothing.

## Repository hygiene

Do not leave disposable material in the repo: `*.tmp`, `*.bak`, `*.old`, `*.orig`, `*.copy`,
hand-numbered variants (`_v2`, `_final2`, `_new`, `_old`), temporary JSON/CSV/XLSX exports, saved API
responses, download artifacts, temporary screenshots, extracted archives, spent test payloads, debug
output, one-off SQL, disposable helper scripts, intermediate build output, superseded local candidates.

Filename variants are not version control — historical versions belong in Git history, not beside the file.

## Every task cleans up after itself

Substantial tasks run **work → verify → establish final state → housekeeping → report**. At closeout,
inspect the material your task created: delete demonstrably disposable artifacts, retain what is still
required, **classify** uncertain material for quarantine, and leave no unnecessary repo-local scratch
behind. Keep this quiet and routine; call it out only when cleanup was significant, something was
quarantined, a recurring clutter pattern appeared, cleanup could not be completed safely, or the owner
must decide.

> **This narrative is a summary, not a procedure.** The authoritative sequence is *Task closeout
> order* above, and the **release rule** governs every step of it. This section creates no independent
> cleanup path and authorises no relocation or deletion of its own. Where it appears to differ from
> *Task closeout order*, ***Task closeout order* wins** — in particular, nothing here permits moving,
> renaming or deleting an artifact before that sequence has been followed.

## Never clean what you did not create

Clean up **your own** task artifacts, and only those. Do not sweep `<HOT>\Projects\<PROJECT_ID>\` — or
any retired location such as `C:\tmp\…` — merely because your task finished. Never touch files that may
belong to another running session, another developer or process, another active task, or an unknown
producer. If files appear or change concurrently and ownership cannot be established, leave them alone
and **do not infer staleness from age**. The same applies inside the repo.

Housekeeping is not a daemon, and it is not permission to police other active workspaces. Historical
clutter left by work completed before this policy is a separate, conservative audit — never folded
into an unrelated task.

## Classify before removing — conservative by default

- **DELETE** only when demonstrably temporary, reproducible, superseded and no longer needed: extracted
  archives whose original remains, download copies, intermediate conversions, scratch output, test files
  created solely for the finished task, regenerable caches.
- **CLASSIFY FOR DURABLE PLACEMENT** — target `<DURABLE>\Projects\<PROJECT_ID>\` under `Evidence\`,
  `Reviews\`, `Playtests\`, `Backups\`, `Archive\` or `Exports\` — when material is still useful but is
  not repo material: diagnostic exports, manual backups, downloaded source material, large comparison
  files, ad-hoc reports, investigation screenshots. **Classification is not relocation.** Placement
  follows *Task closeout order* in full; the source stays intact until every check and any required
  authorisation is satisfied.
- **CLASSIFY FOR QUARANTINE** whenever deletion safety is not established. Never guess.
- **KEEP** — source-controlled files, canonical project files, active source, current config, test
  fixtures, documentation, migrations, production/deployment definitions, anything referenced by code or
  docs, current build inputs, accepted or frozen candidates, hash-bound artifacts, authoritative release
  artifacts, audit / acceptance / verification evidence, rollback material still in force, artifacts
  proving a completed milestone, required database snapshots, and anything whose ownership or purpose is
  unclear.

**Never destroy evidence just because a milestone completed.**

Housekeeping is classification-based, **never age-based**. A timestamp may help *discover* candidates;
it never authorises deletion. Age filters are actively unsafe here: deliberate test fixtures can carry
future dates, which an "older than N days" rule skips forever while catching genuinely active work.

### Quarantine is root-relative, and KEEP wins

**Quarantine on the root the material was found on**: material found on `<HOT>` is classified for
`<HOT>\_QUARANTINE\<YYYY-MM-DD>\`; material found on `<DURABLE>` is classified for
`<DURABLE>\Quarantine\<YYYY-MM-DD>\`. Leave a note recording where it came from and why it is
uncertain. Quarantined material is reported and **never deleted**, and being quarantined is never by
itself a reason to delete it later.

**Classifying something for quarantine decides where it belongs; it moves nothing.** Quarantine
placement follows exactly the same lifecycle as any other placement — CLASSIFY → COPY → VERIFY →
ACTIVATE DESTINATION → CHECK RETENTION OBLIGATIONS → CONSIDER SOURCE RELEASE. Before any relocation:

- **Establish ownership first.** If the owner cannot be established, the material **stays exactly where
  it is** and is reported. An unknown owner is a reason to stop, never a reason to tidy.
- **Never relocate externally owned or concurrently used material** — anything that may belong to
  another running session, another developer or process, another active task, or an unknown producer.
  Reporting it is the whole of the permitted action.
- **Never break a live reference.** If the original path is still referenced by an active task or
  process, or is held open, the original remains intact and in place.
- **The first operation is always a copy** — never a `Move-Item`, rename or cut-and-paste.
- **Verify the copy before activating the destination** — file count and bytes, and cryptographic
  hashes for anything that may be evidence. **If verification fails the source is still the only copy**
  and must not be released, moved or deleted.
- **Remove the source only after** the release rule's retention obligations, and any required
  authorisation, are satisfied.
- **Same-volume quarantine is not exempt.** A same-volume move is a rename: it leaves no second copy
  and destroys the original path.
- Promotion into `<DURABLE>\Quarantine\` is copy-and-verify, never a destructive move.

**Where KEEP and QUARANTINE both apply, KEEP takes precedence.** If ownership, purpose or deletion
safety is unresolved, the artifact is **KEEP**: it stays where it is and is reported. **Do not relocate
an unknown-owner file merely to organise it** — quarantine protects material whose *disposal* is
uncertain, not material whose *ownership* is uncertain.

## Existing work stays put

**Tasks already executing from a retired location — including `C:\tmp\BeatSync-Engine-DigitalUnion\`
and its `tasks\<TASK_NAME>\` children — remain at their current paths until explicitly and separately
migrated.** Never interrupt their writers and never relocate a live workspace opportunistically. Only
*new* task creation uses the roots above.

This repository has **registered linked worktrees and live task material under `C:\tmp`**. A linked
worktree registered in a repository outside the workspace being cleaned is **doubly protected**: its
objects live in that other repository, and removing the working directory leaves stale administrative
state there. Re-registering or pruning such a worktree mutates a repository this policy does not
govern and needs its own authorisation.

**Historical material in retired locations keeps its original paths.** Recorded `C:\tmp` paths in
`CHANGELOG-FORK.md`, in `docs/claude/` acceptance and measurement evidence — including hash-bound
records such as `docs/claude/d0-cold-identity/measurements.json` and its committed harness scripts —
and in test fixture contracts are **historical record and must not be rewritten** to match current
policy; doing so falsifies the evidence. Nothing in this policy authorises migrating, rewriting,
relocating or deleting any existing material, and nothing here is retroactive authority over a
directory that predates it.

## Limits of a prose-only policy

Stated plainly so this file is not mistaken for a mechanism:

- **There is no enforcement.** No hook, resolver, claim service, path validator or scheduled task is
  installed or implied by this file. Every rule above binds only a session that reads it.
- **The claim procedure cannot prevent a path being swapped** between the safety check and the
  operation. It can only refuse to proceed when it detects one.
- **Capacity checks are manual**, performed by the operator at task creation and closeout.
- **Marker presence proves nothing about behaviour.** `PCBUS-HK-v1` and `PCBUS-PF-v2` identify a rule
  set; they are not evidence that any rule was followed.
- A session that began before this text was committed is not retroactively bound by it, and must not
  be restarted or interrupted merely to activate it.

## Project-specific retention (overrides the generic rules above)

- `input/video_analysis_cache/` is a **deliberately preserved** cache — including the
  `qwen_batch_request_*.json` / `qwen_batch_response_*.json` worker files. Do not clean it as scratch.
  `gui.cleanup_on_startup()` already protects it, along with `input/audio/` and `input/video/`.
- `output/` holds the user's rendered videos. Never clear it as part of housekeeping.
- `input/processing/` and `input/gradio_uploads/` are app-managed runtime scratch — the app clears them
  itself; do not race it while a render may be running.

## Git discipline

Policy edits to a tracked `CLAUDE.md` are legitimate commits. Do not commit temp files in order to delete
them later, do not bundle unrelated source changes, and do not rewrite history. A small targeted
`.gitignore` addition is appropriate only for a genuinely recurring clutter pattern — never a large
generic ignore list.

Worktrees, additional clones and orphaned checkouts are **not** clutter by default. Never remove, prune,
reset, stash or relocate one as a housekeeping action, and never disturb an active development worktree.
If a pass uncovers state that exists nowhere else (untracked run output, acceptance evidence), stop:
cleanup of that area is blocked until the material has a second verified copy in durable retention.

## Source-control awareness

Before deleting or moving anything in the repo, establish whether it is tracked, intentionally ignored,
untracked, generated, referenced, part of the current task, or historical evidence. Never remove a tracked
file just because it looks stale. Housekeeping is workspace hygiene: it never changes application behaviour
and grants no new authority. Project security, retention, evidence and deployment rules override it.

---

# Monitoring Policy: PCBUS-WATCHDOG-v1

When waiting on an external async result (CI, deploys, long remote jobs), never rely indefinitely on a
single background monitor. After at most **20 minutes** without a surfaced terminal result: run a fresh,
independent, **read-only** query against the authoritative source, surface an interim status to the user,
and continue with another bounded interval. A fresh authoritative query beats a silent monitor — silence
is not evidence of "still running".

Before arming a monitor: verify every binary in the pipeline exists (`jq` is **not** installed in this
machine's Git Bash — use `gh --jq`), never suppress the monitor's own stderr, emit a heartbeat so total
silence is distinguishable from a healthy wait, and sanity-run the poll command once in the foreground.

Watchdog rechecks are read-only: they never authorize a rerun, retry, `workflow_dispatch`, empty commit or
force push. Do not end a turn with a monitor running and no mechanism to surface its terminal result.
