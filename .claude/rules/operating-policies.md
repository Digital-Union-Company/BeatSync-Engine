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

Status: `LOCAL_POLICY_DURABLE` — this file is committed on the authoritative main line
(`origin/main` of `Digital-Union-Company/BeatSync-Engine`), so the policy travels with the repository
across machines. Keep the `PCBUS-HK-v1` marker; it is a synchronisation marker, not a version number —
do not bump it for wording changes.

## Temporary work goes outside the repository

Standard temp workspace: **`C:\tmp\BeatSync-Engine-DigitalUnion\`**. Per-task scratch goes in
`C:\tmp\BeatSync-Engine-DigitalUnion\tasks\<TASK_NAME>\`; create only the subdirectories a task needs
(`work\`, `downloads\`, `exports\`, `extracted\`, `logs\`, `backups\`, `quarantine\`).

Put there: downloads, intermediate outputs, extracted archives, debug exports, scratch scripts, one-off
logs, temporary patches, throwaway test payloads, comparison copies, temporary screenshots.

**Plan the location before creating the file.** Do not invent other temp roots — not `I:\tmp`, not
`<repo>\tmp`, `<repo>\temp`, `<repo>\old`, `<repo>\backup`, `<repo>\scratch`, not `Desktop\temp`. If a
tool requires a repo-internal temp directory while it runs, remove it after the run succeeds. Create
`C:\tmp\` if missing. Temporary storage is a workspace, never an archive: it must never be the only
place an evidence, acceptance or release artifact exists.

## Repository hygiene

Do not leave disposable material in the repo: `*.tmp`, `*.bak`, `*.old`, `*.orig`, `*.copy`,
hand-numbered variants (`_v2`, `_final2`, `_new`, `_old`), temporary JSON/CSV/XLSX exports, saved API
responses, download artifacts, temporary screenshots, extracted archives, spent test payloads, debug
output, one-off SQL, disposable helper scripts, intermediate build output, superseded local candidates.

Filename variants are not version control — historical versions belong in Git history, not beside the file.

## Every task cleans up after itself

Substantial tasks run **work → verify → establish final state → housekeeping → report**. At closeout,
inspect the material your task created: delete demonstrably disposable artifacts, retain what is still
required, move uncertain material to `C:\tmp\BeatSync-Engine-DigitalUnion\quarantine\<YYYY-MM-DD>\` with
a note on origin and why it is uncertain, and leave no unnecessary repo-local scratch behind. Keep this
quiet and routine; call it out only when cleanup was significant, something was quarantined, a recurring
clutter pattern appeared, cleanup could not be completed safely, or the owner must decide.

## Never clean what you did not create

Clean up **your own** task artifacts, and only those. Do not sweep `C:\tmp\BeatSync-Engine-DigitalUnion\`
merely because your task finished. Never touch files that may belong to another running session, another
developer or process, another active task, or an unknown producer. If files appear or change concurrently
and ownership cannot be established, leave them alone and **do not infer staleness from age**. The same
applies inside the repo.

## Classify before removing — conservative by default

- **DELETE** only when demonstrably temporary, reproducible, superseded and no longer needed: extracted
  archives whose original remains, download copies, intermediate conversions, scratch output, test files
  created solely for the finished task, regenerable caches.
- **MOVE** to `C:\tmp\BeatSync-Engine-DigitalUnion\` when still useful but not repo material: diagnostic
  exports, manual backups, downloaded source material, large comparison files, ad-hoc reports.
- **QUARANTINE** to `C:\tmp\BeatSync-Engine-DigitalUnion\quarantine\<YYYY-MM-DD>\` whenever deletion
  safety is not established. Never guess.
- **KEEP** — source-controlled files, canonical project files, active source, current config, test
  fixtures, documentation, production/deployment definitions, anything referenced by code or docs,
  accepted or frozen candidates, hash-bound artifacts, authoritative release artifacts, audit /
  acceptance / verification evidence, rollback material still in force, and anything whose ownership or
  purpose is unclear.

**Never destroy evidence just because a milestone completed.**

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
