---
paths:
  - "src/beatsync_fork/desktop_notify.py"
  - "tests/test_desktop_notifications.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Desktop notifications for a finished long job (R17-A)

One Windows desktop notification, through the already-open Chrome tab, when an **authoritative**
long-running job reaches a terminal state. OFF by default.

```
a job's own terminal path  ──▶  _notify_<family>(request, …)      translate, never derive
                           ──▶  _notify_publish(request, …)       mint one id, append to the ledger
.then() chained on the SAME click
                           ──▶  _notify_drain(request)            pop; apply the app toggle
                           ──▶  notify_envelope (hidden Textbox)   one JSON envelope
notify_envelope.change(js=…)
                           ──▶  window.__bsNotify.dispatch(text)   dedupe, then new Notification()
```

Two modules, one direction. `beatsync_fork/desktop_notify.py` decides **what a notification may
say**; `gui.py` decides **when a job is terminal** and performs every side effect. The pure module
mints no identity, reads no clock, keeps no ledger and touches no browser — so every branch of the
wording, the four-way outcome fold and the content-safety rule is executable on a bare interpreter.

## What is authorised, and what is explicitly not

Seven event sources, all of them places where the server already holds a reliable terminal result:

| Job | Terminal point | Outcome from |
|---|---|---|
| Music video render | `process_video_guarded`, beside `lifecycle.mark_terminal` | `session_state[RENDER_OUTCOME_KEY]` |
| Variant Lab batch | `render_selected_variants_guarded`, after `RenderBatchOutcome` is built | that object's candidate records |
| Raw Cutter (single) | the three terminal yields of `_on_cut_run` | the `done`/`failed`/`completed` lists |
| Raw Cutter (folder batch) | `_on_cut_batch_run_ui`'s terminal frame | `sink['terminal']` chooses the *rule*; for the two "ran to the end" classes the `summarise` fold then decides (R2 / F1) |
| Audio Cutter | `_on_audio_cut`'s export terminal + its three post-analysis failures | `written`/`failures` |
| Library scan | `_run_prep_in_worker`'s terminal frame | `is_failure_report` / `REFUSAL_NOTICE` |
| Library analysis (Stage 5) | the same frame, told apart by `operation` | those two **plus** `library_prep.analysis_run_outcome` over the analyzer's `*_this_run` counts (R2 / F3) |

**Scoped by real job lifecycle, never by a timer, a guessed duration or "the FFmpeg process
disappeared".** And deliberately absent:

- **No service worker, Push subscription, daemon, background poller, timer, browser extension or
  external service.** Not one of the five scripts contains `fetch`, `XMLHttpRequest`, `WebSocket`,
  `EventSource`, `sendBeacon`, `serviceWorker`, `PushManager`, `setInterval` or `setTimeout`, and
  `tests/test_desktop_notifications.py` scans every one of them for all of it. There is no new
  endpoint and no new backend poll: the browser is **told** when a job finished.
- **No new job or progress coordinator.** Every outcome is read from the classification its own
  subsystem already published.
- **Verify Sources and Sample Decode are not event sources.** Both are bounded read-only
  diagnostics that report in seconds, and neither is in the accepted scope. `_PREP_NOTIFY_JOBS` has
  no key for them, so a missing key sends nothing.
- **A refusal announces nothing.** A refused Raw Cutter press, a batch refused before admission,
  a refused render (the mutex was held), a declined preparation pass and the Audio Cutter's three
  input-validation refusals all performed no work — the same discipline that gives a refused cut no
  activity panel. The one deliberate exception is the Audio Cutter's three **post-analysis**
  failures: the analysis actually ran, so a failure there is a terminal outcome worth announcing.
- **A GATE-refused render announces nothing either, and that needed a marker.** The gate records
  `SHARED_FATAL`, exactly as a real pipeline failure does — correct for batch continuation, since
  every candidate would be refused identically, and wrong for a notification. `RENDER_STARTED_KEY`
  is the one thing that can tell them apart: `False` before the gate, `True` immediately before the
  render stream is constructed, read only by the wrapper's terminal path.

  The **Variant Lab batch deliberately does not** suppress the same case. A batch is the thing an
  operator walks away from, its notification carries the counts that say what happened
  (`0 completed, 1 failed, 3 not attempted`), and a run that stopped on its first candidate is
  exactly what they came back expecting to hear about.

## Why a ledger, and why it is keyed by session

Every one of the seven producers has a pinned `outputs` list and a pinned emitted arity; several are
AST-extracted and executed by their own suites; one is the render wrapper whose body three further
suites freeze. Appending a component to all of them would have been a change to every one of those
contracts, for a feature that must not alter any job's behaviour. So:

- **the producer writes a per-session ledger** — a bounded `OrderedDict` of **plain data**, guarded
  by its own lock, the same shape as the `_verify_revisions` map. Nothing live may enter it: no
  thread, handle, lifecycle, component, path or `Popen`;
- **the delivery is a `.then()` chained onto the same click**, so no `inputs` list, no `outputs`
  list and no emitted tuple anywhere changed. `inputs=[]` and `outputs=[notify_envelope]` are both
  exact: nothing on screen decides what is delivered, and nothing else on screen is written.

**A `visible=False` Textbox is not in the DOM, and its `.change` fires anyway — measured, in both
directions.** `document.getElementById('notify-envelope')` is `null` in the served app on the
pinned `gradio==6.19.0`, and a genuine Raw Cutter job still reached the dispatcher with the full
envelope. The channel does not depend on the component being rendered, so do not "fix" it by making
the component visible-but-CSS-hidden.

**Keyed by session, not by job type.** A per-job-type slot would be drained by whichever session's
event ran next — wrong on a machine with two browser sessions, which this app supports. Each
producer therefore takes `request: "gr.Request" = None`, **injected** by Gradio and absent from
every `inputs` list, so the browser gains no new way to name an owner and a record can only ever be
read back by the session that produced it.

**`''` is a refusal, not a shared bucket.** A publisher with no usable session publishes nothing and
a drain with no session reads nothing — failing towards silence, because the alternative is exactly
the cross-session leak the ledger exists to prevent.

**The annotation is a STRING (`"gr.Request"`).** Parameter annotations are evaluated at `def` time,
and these bodies are executed against synthesised namespaces in which `gr` does not exist — the same
def-time trap `repeat_music`'s integer literal and `voice_overflow`'s string literal record. Gradio
resolves it through `typing.get_type_hints` against this module's own globals, so injection is
unaffected. `tests/conftest.py` carries the one shared `submitted_parameters` exclusion that every
positional `inputs`-to-signature contract now applies.

## One notification per terminal event, and no duplicates

Three independent guards, and none relies on another:

1. **Mint once.** `_notify_publish` is the only place an identity is created — one `uuid4().hex` per
   terminal event — and the only writer of the ledger. Its six callers are the six translators, and
   `_PRODUCER_CALLS` pins the exact number of calls each handler may make.
2. **Consume once.** `_notify_drain` **pops**. A record exists once, is read once and is gone, so a
   reconnect, a repeated progress frame, a second `.then()` or a refused click following a finished
   job cannot re-deliver it.
3. **The client ledger.** `localStorage` (shared by every tab of the origin, which is the point —
   `sessionStorage` would not be), bounded to `SEEN_LIMIT`, and **written before the notification is
   sent**: Chrome, Windows or Focus Assist may refuse to display one and there is no retry path, so
   recording first makes a second attempt for one terminal event impossible. The `tag` is
   `beatsync-<id>`, so Chrome itself collapses a duplicate underneath both of these.

**Nothing is published from a `finally`.** An abandoned Gradio stream unwinds through one, and
abandonment is not a terminal outcome — `.then()` does not run for an abandoned event either, so a
record published there would sit in the ledger until some later click delivered it out of context.

One residual, bounded and deliberately left: a stream abandoned in the gap *between* the publish
and the terminal yield leaves a record that the session's **next** drain delivers alongside its
own. Late, never false — the job really did finish — and the queue carries both. Closing it would
mean publishing from somewhere that cannot tell an abandonment from a completion, which is the
larger error.

## The app toggle gates DELIVERY

`user_prefs.DESKTOP_NOTIFICATIONS`, stored as the string `"on"` / `"off"` in the existing `options`
section — **appended**, never inserted, so a `preferences.json` written before R17-A loads unchanged
and reads as off. A boolean would have been dropped on every load (`load_options` keeps only
non-empty strings) and the toggle would have silently reset itself. `is_enabled` is total and only
the exact token means on: a corrupt or hand-edited value reads as **off**, which is the direction a
notification setting must fail in.

The gate sits in `_notify_drain`, at the moment of delivery, because *"the BeatSync toggle disables
delivery even if Chrome permission remains granted"* is a statement about delivery. A record
produced while the toggle was on is **popped and discarded** rather than held back for a later click
to deliver out of context.

**The panel re-reads the setting per browser session, through one server-side `app.load`.**
`create_ui()` runs once per process, so a value computed at construction time is baked into the
config every later session receives. Measured: with the toggle ON and the gate delivering, a tab
opened afterwards rendered the checkbox unchecked and the sentence "Desktop notifications are OFF".
A panel whose whole job is to state what the application will do must not be the thing that is
wrong about it. That `app.load` is therefore the **one** in `gui.py` that is not a JavaScript
installer: `inputs=None`, outputs that are all its own panel's, in-memory dictionary reads only,
nothing written.

> **Since Settings Preference Consistency R1 the registered function is `_settings_on_load`, not
> `_notify_settings_on_load`.** The collision-policy radio two components away had the identical
> defect and was left with it — measured against real `gradio==6.19.0` on `2097822a`, a fresh
> session was served the construction-time `skip` while `suffix` was saved and in force. It was
> fixed by **widening this registration**, because the authorised `app.load(` count is pinned at
> five and "exactly one server-side load" is itself an asserted contract. `_settings_on_load`
> composes the existing readers — `_notify_settings_on_load()` for the pair here,
> `_collision_policy()` for the policy — and `_notify_settings_on_load` keeps its two-value shape
> because Reset defaults unpacks it. Full contract: `.claude/rules/gui-integration.md`
> (*Settings preference consistency*).

A callable `value=` was tried first and **rejected on measurement**: it re-evaluates per session
as wanted, but the implicit load dependency it creates on the pinned `gradio==6.19.0` leaves a
progress overlay on those components that never clears — an ever-rising stopwatch over a settings
line — and a component's `value=` takes no `show_progress`. The explicit `app.load` does, so
`show_progress='hidden'` is load-bearing rather than cosmetic. The tracker still never settles; at
`opacity: 0` it is invisible, and a real job run in that tab afterwards completed and notified
normally, so it blocks nothing. Recorded rather than worked around.

**Every option writer must pass the COMPLETE option set.** `save_preferences` replaces the whole
file and fills any option key the caller omits with its built-in default, so the single-key form
silently resets the other option. That was invisible while there was one option and became a real
regression the moment there were two: `_on_collision_policy_change` and `_on_save_folder_prefs` were
both corrected, and a test pins that every `save_preferences` call with an options argument passes
`dict(_FOLDER_OPTIONS)`, `options` or `empty_options()`.

**…and every option *reader on screen* must follow a write it did not make (R2 / F2).** Reset
defaults writes `empty_options()`, which returns this option to `off` in the file **and** in
`_FOLDER_OPTIONS` — the dictionary the delivery gate reads — while writing neither the checkbox nor
the sentence. So pressing it with notifications ON left the panel saying *"Desktop notifications
are ON in BeatSync"* over an application that would send nothing: the same class of defect
`_notify_settings_on_load` exists to prevent, from the other direction. `pref_reset_btn.click` now
writes `notify_enabled` and `notify_app_state`, both from `_notify_settings_on_load()` rather than
from literals, which also makes the **persistence-failure** path right for free — a failed save
leaves `_FOLDER_OPTIONS` untouched, so the pair reports what is still in force.

**A programmatic write that changes nothing writes nothing (R2 / F2).** Gradio's `.change` fires
for a *function update* as well as a user gesture (`.input` is the user-only listener), and this
component has two legitimate programmatic writers — `_notify_settings_on_load` and now Reset. Both
re-entered `_on_notifications_enabled_change`, so **opening a tab rewrote `preferences.json` and
raised an "ON" toast for a page the operator had only just loaded**. The handler now returns early
when the resolved boolean already equals the stored one. `.change` is deliberately **kept**: the
collision-policy radio two registrations away uses it for exactly this reason (Reset writes that
too), and swapping the operator's own toggle onto an event this repository has not measured against
the pinned `gradio==6.19.0` would be a worse trade than a three-line guard. The comparison is on
the resolved booleans, so a hand-edited value — which `is_enabled` reads as off — is not rewritten
by an echo of the off state it already means.

> **The collision radio needed the same guard and did not have one.** The sentence above says the
> radio "uses `.change` for exactly this reason", which was true of the *listener* and not of the
> *guard*: `_on_collision_policy_change` had none, so Reset's own echo produced a second save and a
> second toast, and a **failed** Reset echoed the default policy back and genuinely persisted it.
> Settings Preference Consistency R1 added the matching guard, comparing resolved policies through
> `_collision_policy()`. It is also what makes the widened `app.load` above safe: without it, every
> new browser session would rewrite `preferences.json` and toast a save nobody asked for.

## Content: no caller may supply text

`build(job_kind, outcome, *, notification_id, counts=None)` takes a job-kind token, an outcome token
and integers. **The words come from the tables in the pure module.** A source path, a media
filename, an output folder or an FFmpeg stderr line has no parameter to arrive through, which is a
stronger guarantee than sanitising a string a caller passed in. `is_safe_text` is applied to the
composed result as an **independent** backstop, and `build` answers `None` rather than emit anything
that reads as a path, a URL or a log line.

- **An unknown job kind, an unknown outcome or an unusable id answers `None`.** A future caller that
  forgets to classify itself announces nothing instead of announcing the cheerful default. "Do not
  announce success while the result is unknown" is a refusal, not a fallback.
- **A count that is not a whole non-negative `int` is dropped, never guessed.** `3.0 completed`
  would make an operator distrust the whole line, and a clamped negative would report a number
  nobody measured. A supplied **zero does** appear: `0 completed` after a batch of forty is
  information.
- **Four outcomes, worded distinctly.** `finished` / `finished with problems` / `failed` /
  `cancelled`. A batch where every item failed is a **failure**, not a partial — calling it partial
  would be the cheerful reading of a total loss. Cancellation wins over everything, because a
  stopped job's counts describe how far it got rather than what it achieved.

  **R2 / F1: the Raw Cutter folder batch did not obey that rule, and now does.** It was the one
  job family whose outcome was *translated* from a terminal class rather than folded from counts,
  and `terminal_status` answers `completed_with_failures` for **any** failed source — so a batch of
  three unreadable files, `0 completed, 3 failed`, nothing on disk, announced `finished with
  problems`. See *R2* below for the split that replaced the table.
- **The batch's own `outcome_kind` supplies only cancellation.** It is deliberately `None` for a
  batch that executed its whole selection even with local failures, so reading it would report one
  success and one failure as an unqualified success. Counts come from the candidate records.
- **`total_count` is where a translator's arithmetic lives, not the call site.** Summing two or
  three subsystem counts into one "this went right" tally is a thing several translators need; it
  is done once, in the pure module, where a value it cannot read is dropped rather than raising. A
  translator runs on a finished job's terminal path **outside** `_notify_publish`'s guard, so
  arithmetic there is arithmetic that could cost a finished job its final frame — which is not a
  hypothetical: R2 found `_notify_cut`'s crashed branch doing `(written or 0) > 0`, and a
  property test over every shipped translator is what found it.

## The browser half

Five registrations carry JavaScript, all with `fn=None` — no server call in any of them. The
installer (`app.load(js=NOTIFY_JS)`, the fourth authorised installer) holds **every** rule; the
other four are one-line delegates, so no registration can grow a second interpretation of a payload.

- **The permission read-out is painted by the page, because only the page knows.** A server-rendered
  `granted` would be a guess that goes stale the moment the operator changes Chrome Site Settings.
  `_notify_status_panel_html` ships a placeholder that states **no** permission state, so a page
  whose script never ran reads as "the check did not happen" rather than as a confident lie. It is a
  `gr.HTML`, not a `gr.Markdown`: the Markdown sanitizer strips every `data-*` attribute before it
  reaches the browser (measured on the pinned `gradio==6.19.0` — the same limitation `prep_status`
  records).
- **…and it is painted again when the panel ARRIVES, because Gradio renders a tab lazily.** The
  first build painted only on install, and the Settings panel does not exist at `app.load` time:
  measured in the served app, a fresh page that opened Settings before running anything sat on
  "Checking this browser…" forever — telling the truth about itself while showing the operator
  nothing. A `MutationObserver` on `document.documentElement` fixes it. Deliberately not a timer:
  these scripts are asserted to contain no `setInterval`/`setTimeout`, and `paint()` returns
  immediately when the state has not changed, so a render emitting a frame a second costs one
  `getElementById` and one `getAttribute`.
- **The app-setting line and the browser-status line are separate components on purpose.** They
  answer different questions with different authorities, and keeping them apart lets the server
  rewrite its own half without erasing the live answer the page painted into the other.
- **`requestPermission()` has exactly one implementation and one route to it.** It lives in the
  installer and is reachable only through `window.__bsNotify.request()`, which only
  `NOTIFY_REQUEST_JS` — wired to one button — invokes. So a page load, a render and a saved-toggle
  change can none of them produce a prompt. The installer's last act on load is `paint()`.
- **Six entry points on `window.__bsNotify`, not five, since R3**: `paint`, `state`, `request`,
  `test`, `recheck`, `dispatch`. The script *count* is unchanged — `NOTIFY_REFRESH_JS` simply
  delegates to `recheck()` instead of `paint()` — so the four-delegate, one-installer shape and the
  `fn=None` allowlist in `tests/test_open_output_folder.py` are untouched.
- **Support is measured, not assumed.** `typeof window.Notification === "function"`, the presence of
  `requestPermission`, and `window.isSecureContext === true`. `http://127.0.0.1:7860` is normally a
  potentially trustworthy loopback origin, but an operator reaching the app by another address is
  exactly the case where a promise of alerts would be false. A missing API reads as **unsupported**,
  never as a denial.
- **Clicking a notification focuses the page and does nothing else.** No navigation, no tab switch,
  no render, no cleanup, no retry. The installer contains no `window.location`, `window.open`,
  `href`, `.submit()` or `.click()`.
- **Every word the page shows comes from the pure module**, substituted in through `json.dumps` —
  the R5-P3 rule, and for the same measured reason: these sentences contain apostrophes and curly
  quotes, and dropping them bare into a JS literal is how a whole script stops compiling.
- **An empty envelope is ignored.** The component starts empty and the drain writes the empty value
  whenever a click produced no terminal event, so "no notification on page load" and "no
  notification for a refused click" are structural rather than checks that could be forgotten.

## R2: three outcome-accuracy findings, and the one client-side correction

R17-A's own rule was *"do not announce success while the result is unknown"*, and three of its seven
job families did not obey it. All three are **translation** defects: nothing about a job's result,
progress, cancellation or concurrency changed, and no new event source, component or `outputs` list
appeared.

**F1 — the Raw Cutter folder batch.** Its four `TERMINAL_*` classes now split two ways instead of
feeding one table:

```
_CUT_BATCH_RUN_OUTCOMES   CANCELLED, FAILED      translated: counts cannot express either
_CUT_BATCH_RAN_TO_END     COMPLETED, …WITH_FAILURES   folded from the measured counts
unrecognised / absent     -> nothing, as before (a run that never started)
```

A test asserts the two sets **partition** the batch module's own vocabulary, so a new terminal class
cannot fall silently into the `return` branch that means "never started".

**What counts as "this source went right" is three outcomes, not one**, and each was measured
against the real classifier rather than reasoned about:

| contributes to | why |
|---|---|
| `completed` | obvious |
| `no_clips` | read and planned successfully, genuinely nothing worth exporting — a real answer about the footage. Without it, two such sources plus one unreadable one folded to `0 completed, 1 failed` and would have been announced as a FAILURE of a run that mostly worked |
| `partial` | exported some clips and lost the rest; counted in `failed` and in neither of the above, so a single-source batch that put 7 of 10 clips on disk would have been announced as a FAILURE with the clips sitting in the output folder. It contributes to **both** sides — the same reading `_notify_cut` gives a crash that left clips behind |

`publish()` carries `no_clips` and `partial` out of the fold it already computes, exactly as R11's
`clips` and R17-A's `failed`/`cancelled` ride along, and both are correct at the pre-backfill call
site by the same equivalence argument (a `not_attempted` row is in neither).

**And `record_lost` makes the fifth rendering agree with the other four.** A clean run whose
manifest never reached disk shows the headline `Batch finished but the record was lost`, an activity
panel of PARTIAL/FAILED, and a summary that has always disclosed it — while the toast said
`finished`. It qualifies a SUCCESS and nothing else, mirroring the panel's own override, which fires
only when the tone was otherwise `done`.

**F2 — Reset defaults and the programmatic-write loop.** Both halves are in *The app toggle gates
DELIVERY* above.

**F3 — the library analysis pass.** `is_failure_report` recognises only the marker
`record_failure` writes, so a pass whose shared Qwen worker **died** — real GPU minutes, nothing
cached, no exception, no marker — was announced as `Library analysis finished`.
`library_prep.analysis_run_outcome` is the structural answer, read from the analyzer's own
`*_this_run` counts and `source_count`, never from the rendered summary:

```
qwen_incomplete_jobs_this_run > 0        -> incomplete   (issued, therefore NOT cached)
source_count < submitted                 -> incomplete   (a submitted path no longer existed)
non-mapping / missing or malformed count
  / completed + incomplete != jobs       -> unknown
otherwise                                -> complete
```

Four things it deliberately does not do:

- **It has no `nothing_completed` token.** "No Qwen job completed" is not "nothing was cached" — a
  candidate-less source is legitimately checkpointed with no Qwen work — so a total-loss verdict the
  payload cannot support is not invented.
- **`incomplete` and `unknown` share a verb and stay separate tokens.** They are different facts and
  `library_prep` keeps them apart; what they share is the only honest thing a four-word toast can
  say about either, `finished with problems`, whose body sends the operator to the panel that has the
  numbers. An absent verdict, or one from a future vocabulary, lands there too — **never** on success.
- **A Scan is asked a different question.** There is no Stage-5 batch for its verdict to be about and
  its own faults arrive through `failed`, so an absent verdict is a plain success for a scan only.
- **A warm batch that ran no job at all is COMPLETE.** `sources_analyzed_this_run == 0` with every
  submitted source a cache hit is a success, not a shortfall.

The verdict travels in a **caller-owned sink**, the shape `_on_cut_batch_run` already uses:
`_on_prep_analyze_click` creates one plain dict per click, `_prep_analyze_impl` writes it on the
success path only, and `_run_prep_in_worker` reads it **after** `thread.join()` — so the join is the
happens-before and no lock is involved. Per-click rather than a module-level slot deliberately: Scan
and Analyze take no mutex, so two browser sessions genuinely overlap and a shared slot would let one
session's verdict describe the other's run (the mistake UX-PREP-ACTIVITY R2 corrected for
`_prep_phase`). It is **not** a field of `PrepSessionState`: it describes one run, so every other
transition would have to clear it or it would outlive its run.

**One residual limitation, recorded rather than guessed at.** A source the analyzer opened but could
not decode is counted as analysed, is correctly not cached, and issues no Qwen job — so the
classifier reads such a run as complete. The payload carries no count for it, and adding one would
mean changing the Stage-5 orchestrator to make a notification easier to classify. The panel still
reports the batch's own numbers and the next scan still shows the source as needing analysis.

**The one client-side correction.** `remember()` re-reads `localStorage` each time, so the durable
ledger was always right — but the `delivered` snapshot taken one line above the dispatch loop was
not, so two items carrying the **same id inside one envelope** each passed the check and called the
constructor. The server cannot produce that (`_notify_publish` mints a fresh `uuid4` per terminal
event, and a test executes that half), but "exactly one `new Notification()` per terminal event" is
the dispatcher's own guarantee about the payload it is handed and should not rest on a property of
the other side. One line: `delivered.push(item.id)` beside the `remember`. **Asserted structurally**
— there is no JavaScript engine in the suite — so it is the live smoke that observes it, and this
sentence is the record of which of the two that is.

## R3: the two diagnostics had to be repeatable and responsive

The owner granted the permission, got a **real Windows notification** from the first test, and then
nothing from any click afterwards. Two defects, both in the panel, neither near a job.

**The fixed tag.** `TEST_TAG` was `beatsync-test`, always. Two notifications sharing a tag are one
notification to the API: the second **replaces** the first in place, silently — no banner, no sound,
only the Action Center entry's text changing. The constructor returned normally every time, which is
why nothing in the app could tell. `TEST_TAG_PREFIX` replaces it and the browser appends
`<Date.now()>-<counter>` per click: the clock makes it unique across reloads, the counter inside one
millisecond.

```
test notification   beatsync-test-<clock>-<n>    FRESH per deliberate click   (R3)
job notification    beatsync-<id>                STABLE per terminal event    (unchanged)
```

**The job tag is deliberately untouched**, and a test asserts the dispatch entry point mints
nothing — a job's notification is identified by the terminal event it describes, which is what makes
the browser-level collapse underneath the client ledger a real duplicate guard rather than a
coincidence. `renotify` was considered and rejected: it is specified for
`ServiceWorkerRegistration.showNotification`, this feature has no service worker **by accepted
scope**, and with a unique tag there is nothing left for it to re-alert. A test scans every script
for it.

**The silent buttons.** `paint()` returns early when `data-state` is unchanged — correct for the
MutationObserver that calls it on every DOM mutation, and the reason "Re-check browser status"
produced *no observable effect whatsoever* in the common case. `say()` is the acknowledgement, it is
written on **every** diagnostic click, and `paint` is deliberately left exactly as it was:

```
paint()      idempotent, early-returns, called by the MutationObserver   -- NEVER writes feedback
say(text)    always writes, stamped with the browser's own clock         -- the acknowledgement
recheck()    paint() + checked()        <- NOTIFY_REFRESH_JS now delegates here, not to paint
test()       fresh tag -> fire -> paint() + say(sentence(result))
request()    requestPermission -> settle -> paint() + checked()
```

**The timestamp is load-bearing, not decoration.** Pressing Re-check twice on a settled permission
produces the same sentence both times — which is exactly the complaint — and a changing stamp
resolves it without inventing a state change that did not happen. `FEEDBACK_TIME_TEMPLATE` and
`CHECK_SENTENCE_TEMPLATE` are substituted into by the client, and `feedback_line` / `check_sentence`
are the Python **reference implementations** of those substitutions, in the same spirit as
`decode_envelope`: there is no JavaScript engine in the suite, so the two replacements the client
performs are pinned structurally and the composition rule itself is executed in Python.

**`fire()` answers which of five things happened, instead of a bare boolean.** The dispatcher
ignores the return exactly as before — a job's notification has no panel to report into — and the
test button needs it, because *"Chrome has not been asked"*, *"Chrome is blocking this address"*,
*"this browser has no API"* and *"the constructor threw"* need four different things from the
operator and must not share one sentence. `PERMISSION_GRANTED` is deliberately **not** a key of
`TEST_FEEDBACK_SENTENCES`: a granted permission always resolves to `requested` or `error`, so a
caller reporting the state itself has skipped the construction and falls to *unknown* — never to a
success.

**The success wording describes the REQUEST.** *"Test notification requested successfully. Whether
it appears is Windows' decision — Focus Assist, Do Not Disturb or a notification setting can still
suppress it."* A constructor that returned proves the browser accepted the call and nothing more. A
test bans `was shown`, `was displayed`, `was delivered` and `guaranteed` from that sentence.

**The acknowledgement is a second `div`, not a component.** `notify_status` is the one component in
this panel with **no** server-side writer, and this is another thing only the browser knows; adding
a component would have put a new name into a panel whose writer sets are pinned, for a line the
server can never fill in. It ships **empty** (`data-feedback="none"`), so a freshly loaded page
carries no stale claim about a browser action that never happened.

**Three wording hazards are tested rather than assumed away**, each measured against this
repository's own machinery: a `$` in a JavaScript `String.replace` *replacement* is a substitution
pattern (`$&`, `$'`) and would silently mangle a sentence; a `//` inside a string literal breaks the
comment stripper every structural scan here runs the scripts through; and a sentence containing a
placeholder token would be rewritten by the next replacement. A further test asserts that **no
`__TOKEN__` survives into any shipped script** — a missed substitution is a syntax error that stops
the whole installer compiling in the browser, and no other assertion in the suite would notice.

### What R3's smoke established

Same isolation recipe as R2 — redirected `PREFS_PATH`, `BEATSYNC_SHARED_CONFIG` into the task
workspace, `BEATSYNC_DISABLE_QWEN=1`, no render — but on **port 7860**, deliberately: Chrome's
permission is per-**origin**, so 7861 is a different origin that can only ever read `default`.
Nothing was listening on 7860–7863 and no BeatSync process existed, so nothing was displaced.

**With the real Notification API** (permission `default`): a test click produced
`Nothing was sent, because Chrome has not been asked yet…` with a timestamp and **no permission
prompt**; two Re-check clicks four seconds apart produced the identical sentence with **two
different timestamps**; a fresh page load produced zero notifications, zero permission requests and
an empty feedback line.

**With a recording stub**, through the real buttons: three test clicks → **3 constructor calls, 3
distinct tags**, all `beatsync-test-…`, one title, success feedback, and **zero** `requestPermission`
calls. The pre-R3 behaviour, replayed in the same tab, → **3 calls, 1 tag**. The permission button
called `requestPermission` exactly **once** and sent nothing. Denied, unsupported and
constructor-threw each produced their own sentence. A job envelope carrying one id twice still
delivered **2 of 3**, a replay delivered **0**, the tags were `beatsync-<id>`, and the job delivery
did **not** overwrite the operator's acknowledgement line.

**Not verified:** an OS-visible Windows toast. The Chrome profile holding the owner's grant for
`http://127.0.0.1:7860` is not the one the automation drives (it reads `default` there), granting a
permission is a human action on browser UI, and the second connected Chrome turned out not to be on
this machine at all. Everything up to and including the constructor call is verified; whether
Windows drew anything is not, and must not be claimed.

**The test notification is deliberately NOT gated on the app toggle.** It is the operator's way of
checking *Chrome*, so gating it on the BeatSync setting would make the one diagnostic in the panel
useless in exactly the state they are trying to get out of. The whole route is `NOTIFY_TEST_JS` →
`window.__bsNotify.test()` → `fire(...)`, whose only gate is the browser's own `granted`; it reads
no setting and calls no server function, and a test pins that so a later change cannot quietly make
it conditional.

## R4: the producer must hand the publisher the BROWSER session

A finished **Variant Lab batch** announced nothing. Two real 2-candidate batches, both *2 / 2
succeeded*, produced **zero** notifications; a Raw Cutter folder batch run straight afterwards in
the same tab and session delivered normally, which is what isolates the fault to the one producer
rather than to the ledger, the drain, the envelope or the dispatcher.

```python
def render_selected_variants_guarded(…, request: "gr.Request" = None):
    batch_request, refusal = fork_render_batch.build_request(…)   # NOT `request` — R4
    …
    _notify_render_batch(request, outcome)                        # the injected session
```

The body used to assign `build_request`'s `RenderBatchRequest` to `request`, **shadowing the
injected parameter**. Every later `request.count` / `request.candidates` was right for the batch
record and the terminal notification call was wrong, so `_notify_session_key`'s
`getattr(request, 'session_hash', None)` answered `None`, the key was `''`, and `_notify_publish`
returned at its `if not key` guard.

**It failed silently, and that is a property of the design rather than an oversight.** `''` is a
refusal (see *Why a ledger, and why it is keyed by session*), so an unattributable record is
dropped on purpose — the safe direction for a cross-session leak and the invisible direction for a
producer handing over the wrong object. There is deliberately **no** louder failure here: raising
would put a notification's problem on a finished job's terminal path, which is exactly what
`_notify_publish`'s guard exists to prevent. The protection is therefore structural, upstream of
the publisher:

- **No producer may rebind its injected `request`.** A test walks all seven and fails on any
  `ast.Name(id="request")` in a `Store`/`Del` context. "A request" is a natural name for more than
  one thing in a 300-line handler, which is precisely why the rule is mechanical.
- **The counterpart is pinned too** — `batch_request, refusal = fork_render_batch.build_request(`
  and `_notify_render_batch(request, outcome)` — because the absence rule alone would also be
  satisfied by deleting the request entirely.

**And the behavioural half is what actually catches a wrong argument.** Before R4 the call site was
asserted only as an AST **call count** (`_PRODUCER_CALLS`), which was true: the call existed,
exactly once. Nothing asserted *what was passed*. The one suite that executed this handler's real
body (`tests/test_delivery_share.py`) stubbed `_notify_render_batch` to a no-op, and the suite that
executed the translator never ran the handler. So R4's tests exec the shipped wrapper **into the
same namespace as the shipped ledger**, and a record has to travel the whole way from the terminal
path to a decoded envelope: success, partial, cancellation, total failure, a refused batch, session
isolation, consume-once and the delivery toggle. Nine of the sixteen fail against pre-fix
`bdb342fd`; the headline one reports *"expected one batch notification, got 0"*.

**When adding a producer, assert delivery end to end.** A stub on the notification path makes a
producer test pass while proving nothing about the notification, which is how this shipped.

## The browser lifecycle boundary — state it, do not work around it

V1 works while an authorised BeatSync page is **actually open** and receives the job's terminal
result, even with another application in the foreground. A page `Notification()` is **not** a promise
that an alert will fire after the tab is closed, after Chrome has shut down or after the session has
disconnected. Windows Focus Assist, Do Not Disturb, Chrome's own notification settings and OS policy
can all suppress display.

The Settings panel says all of this. **Do not replace that honesty with a service worker or Push
infrastructure** — R17-B is a separate future decision to *investigate* whether such a thing could
ever be justified, not permission to add one.

## What the live smoke established, and what it did not

Measured on Windows with Chrome against an isolated instance on port 7861 and synthetic media, with
the owner's production instance on 7860 untouched throughout. Chrome's permission for that origin
was **`default`**, and granting it needs a human click on browser UI the automation cannot reach,
so `window.Notification` was replaced with a recording stub of the same shape.

**Verified:** `isSecureContext` true on the loopback origin; the panel reporting `not requested`
(never "denied") against a real `default`; no notification on page load; the toggle persisting and
reloading; `collision_policy` surviving a toggle write (the option-preservation fix); genuine
completed Raw Cutter, Audio Cutter and Library scan jobs each producing exactly one truthful
notification; a genuine Audio Cutter failure producing a failure notification with **no invented
counts**; the on-screen report carrying the full `E:\…` path while the notification carried none;
a gate-refused render producing **none**; the toggle OFF suppressing delivery on a completed job;
a replayed envelope, an empty one and a malformed one all suppressed; a second tab receiving
**nothing** from the first tab's job; the test notification; and the permission button invoking
`requestPermission()` exactly once.

**Not verified, and these are the ones to be careful about claiming:**

- **OS-visible delivery.** Everything up to and including the `Notification` constructor call is
  verified; whether Windows drew a toast is not. Never describe this feature as proven to alert.
- **A genuinely backgrounded tab.** The automation could not produce one — both tabs reported
  `visibilityState === 'visible'` and `hasFocus() === true`, and no `visibilitychange` ever fired.
- **A successful full render and a Variant Lab batch.** Deliberately not run live:
  `create_music_video` clears a **process-global** `input/processing` directory that a second
  instance shares, so a successful render beside the owner's live instance could have destroyed
  in-flight clips of a render they had started. Their terminal points and outcome translations are
  executed against the shipped `gui.py` bodies in the test suite instead.
- **A cancelled job.** The synthetic fixtures finish in under a second.

### What R2's smoke added

Same method — Chrome against an isolated instance on **7861**, synthetic media, Chrome's permission
still `default` so `window.Notification` was again a recording stub. Two things were different and
both are worth copying: **`PREFS_PATH` was redirected into the task workspace from the launcher** and
the two preference globals reloaded from it *before* `create_ui()`, so Save defaults, Reset defaults
and the toggle never reached the owner's `state/preferences.json` at all (measured untouched,
same mtime and hash before and after); and `BEATSYNC_SHARED_CONFIG` pointed the Stage-5 record store
at the workspace, so neither the production local cache (14 235 records) nor the configured shared
cache on `D:` (11 794) was written — all 8 records of the session landed in the isolated folder.
`input/processing`, `input/gradio_uploads` and `output/` were all verified unchanged.

**Newly verified, on real media through the shipped app:**

- **A real failed folder batch announces a FAILURE.** Two genuinely unreadable files (FFprobe
  exits 1 on both): panel `⚠️ Batch finished with failures`, counts `0 completed / 2 failed`,
  0 clips — and the toast `Raw Cutter batch failed · 0 completed, 2 failed`. On the pre-R2 head the
  same run said `finished with problems`.
- **A real successful folder batch** (3 sources, 8 clips) → exactly one `Raw Cutter batch finished ·
  3 completed, 0 failed`, while the on-screen summary carried the full `E:\…` path the toast did not.
- **A real cancelled folder batch** — the case R17-A could not produce, reached with 4 × 90 s 720p
  sources and Cancel pressed during the first: `Raw Cutter batch cancelled · 0 completed, 0 failed,
  1 cancelled, 3 not attempted`. Zero `completed` and still CANCELLED, which is the translated
  branch holding.
- **A real incomplete library analysis** — one source deleted between Scan and Analyze:
  `Submitted 3 / Sources analyzed 2`, no failure marker, and the toast `Library analysis finished
  with problems`. Pre-R2 it said `Library analysis finished`. A complete analysis (3 of 3) and a
  Library scan each still produce one plain `finished`.
- **The duplicate-within-one-envelope fix, both ways.** The **pre-fix** dispatch loop, extracted
  from `32165fb` and run in the same tab, called the constructor **3** times for an envelope
  carrying one id twice plus one distinct id; the shipped loop calls it **2**. Replayed, empty,
  whitespace, malformed, wrong-version, non-array and id-less envelopes all deliver nothing, and the
  ledger holds at 64 after 70 events.
- **Reset defaults, and the write that is not a change.** With the toggle ON and the collision
  policy on `suffix`: a reload rendered the checkbox ON and the sentence ON **with no toast** and a
  **byte-identical, same-mtime** `preferences.json` — the programmatic-write guard. Reset then left
  the checkbox unticked, the sentence OFF, the policy back on `skip`, the file cleared, and again
  **no** second toast; a publish afterwards delivered nothing, so the gate agreed with the sentence.
- **The test notification fires with the app toggle ON and OFF**, once each, with the pure module's
  wording.

**Still not verified, and the list has not shortened in the ways that matter:** an OS-visible
Windows toast (the permission is still `default`); a genuinely backgrounded tab; a successful render
or a Variant Lab batch *through the GUI*. The render path was instead exercised **headless** and
separately isolated — `video_processor.py` on an 8 s track and 3 synthetic sources, with the cache
redirected and `-o` into the workspace — and the result is frame-exact: the plan's 200 frames were
published as **200 counted frames**, 8.000000 s against 8.000000 s of audio, two streams
(h264 + `pcm_s24le`), full decode to null exit 0. That is evidence about the **renderer**, not about
the render's notification, whose terminal point remains covered by the suite alone.

**One honest wrinkle this created.** The preparation panel's terminal chip reads `COMPLETED` for the
incomplete-analysis run while the toast says `finished with problems`. They answer different
questions — the chip asks whether the *pass* ran to completion (`is_failure_report` /
`REFUSAL_NOTICE`, unchanged by R2), the toast asks whether the work it set out to do succeeded — and
the report between them carries the `Submitted 3 / Sources analyzed 2` that explains the gap.
Teaching the chip the verdict means changing `prep_activity`'s own pinned contract, which is a
different subsystem and a separate decision; it is recorded here rather than done.

## What this feature must never do

- Change any job's result, progress, cancellation or concurrency behaviour. The envelope is written
  by a chained event; no producing handler's `inputs`, `outputs`, emitted arity or generator shape
  changed, and `_notify_publish` **cannot raise** into a terminal path — a notification is a
  courtesy, a job's result is not.
- Reach a Stage-5 request, the Qwen prompt, a persisted record or any cache identity.
  `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched and no cache payload gained a field.
- Write a render, source, preparation or creative widget. The whole panel's writer set is
  `notify_app_state`, and `notify_status` has **no** server-side writer at all.
- Retain new media metadata, or put a path, filename, folder or log line into a notification.
- Claim a guaranteed alert, or imply that BeatSync revoked or restored a Chrome permission.
