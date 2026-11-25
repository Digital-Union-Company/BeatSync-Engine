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
| Raw Cutter (folder batch) | `_on_cut_batch_run_ui`'s terminal frame | `sink['terminal']` + the `summarise` fold |
| Audio Cutter | `_on_audio_cut`'s export terminal + its three post-analysis failures | `written`/`failures` |
| Library scan | `_run_prep_in_worker`'s terminal frame | `is_failure_report` / `REFUSAL_NOTICE` |
| Library analysis (Stage 5) | the same frame, told apart by `operation` | the same two verdicts |

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
wrong about it. `_notify_settings_on_load` is therefore the **one** `app.load` in `gui.py` that is
not a JavaScript installer: `inputs=None`, two outputs, both its own, one in-memory dictionary
read, nothing written.

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
- **The batch's own `outcome_kind` supplies only cancellation.** It is deliberately `None` for a
  batch that executed its whole selection even with local failures, so reading it would report one
  success and one failure as an unqualified success. Counts come from the candidate records.

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
