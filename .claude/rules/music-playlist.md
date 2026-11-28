---
paths:
  - "src/music_playlist_builder.py"
  - "src/beatsync_fork/music_playlist.py"
  - "tests/test_music_playlist.py"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

# Music Playlist Mode V1 (R16)

Several music files as **one ordered, continuous soundtrack** for Create Music Video. Single Track
remains the default and is structurally the pre-R16 render. Concatenating finished videos, or
looping one, is explicitly not the feature, and nothing here may become that.

```
tracks (ordered, frozen at render admission)
      │  decode each to canonical PCM, measure it, stream-copy the run
      ▼
PLAYLIST MASTER WAV  ──▶ Stages 1-5 (ONCE, over the WHOLE soundtrack)
PLAYLIST MASTER WAV  ──▶ R10 build_repeated_music (UNCHANGED)  ──▶ repeated master
create_music_video(whichever master, the extended beats, the extended beat_info)
```

Two modules, one direction, and they are `music_repeat` / `music_repeater`'s structural siblings —
same pure/runtime split, same `session_dir` scratch ownership, same loud-failure discipline:

| module | owns | may not |
|---|---|---|
| `beatsync_fork/music_playlist.py` | the Music Source control, the ordered-soundtrack model, the seven editing operations, the frozen identity, the RIFF geometry arithmetic, every read-out | touch the filesystem, FFmpeg, a clock, or any randomness outside `shuffle` |
| `music_playlist_builder.py` | the per-track decode, the one stream-copy join, the sample-count verification, the scratch lifetime, cancellation | decide what a playlist *means* |

`music_playlist_builder` imports the pure one; never the reverse. `PlaylistError` is a plain
`Exception` and is deliberately **neither** a `MusicRepeatError` **nor** an `AudioMixError`
subclass: the three features fail for unrelated reasons, and the existing handlers in the render
body must keep meaning exactly what they mean today.

## The playlist master simply IS "the music file", and that is the whole architecture

Everything downstream of the join is unaware a playlist happened. `local_audio_path` is rebound to
the master, so `analyze_beats_auto` receives the complete ordered soundtrack as its single music
file. Five consequences, and all five are the point:

- **Stages 1–5 run ONCE, over the whole soundtrack.** One beat grid, one feature pass, one section
  pass, one cut selection, one Stage-5 library.
- **Repeat Playlist repeats the WHOLE ordered playlist.** R10's `build_repeated_music` still
  receives `music_path=local_audio_path` and still measures its own cycle — and by the time it runs
  that cycle *is* the complete soundtrack. **Neither `music_repeat.py` nor `music_repeater.py`
  needed a change for this and neither got one**, beyond the one label described below. The owner's
  deferred R16 question ("whole playlist or each song?") is settled as **whole playlist**, and it is
  settled structurally rather than by a branch.
- **Nothing about the playlist can reach Stage-5 cache identity, a Qwen request, the prompt or a
  persisted record.** Stage 5 keys on the *video* sources; the music reaches it only as the one
  audio file it has always received. `CACHE_CONTRACT_VERSION` and `ANALYSIS_VERSION` are untouched
  and no cache payload gained a field. A test asserts the playlist names are absent from
  `video_analysis.py`, the Qwen worker, every `auto_mode` stage, `stage_cache.py`,
  `analysis_cache.py` and `library_prep.py`.
- **Voice, SFX and the frame-locked timeline needed no playlist awareness**, because all three are
  derived from the rendered audio file's own measured duration. `video_processor.py` and
  `ffmpeg_processing.py` are untouched.
- **The L2 process-local analysis cache always misses in playlist mode**, because the master is a
  fresh uuid path per render. That is correct rather than unfortunate — the soundtrack really is a
  new track — but it means a playlist re-render pays for Stages 1–3 again where a single-track
  re-render of the same file would not.

## Single Track is the legacy render structurally, not by value

**Every reference to either new module inside `_process_video_impl` sits behind a playlist guard**
— `playlist` for the work, `playlist_master_path` for the cleanup and the panel line. In Single
Track mode neither module is even *resolved*, no file is built and no work runs.
`tests/test_music_playlist.py::test_every_playlist_statement_in_the_render_body_is_behind_a_playlist_guard`
walks the real AST and asserts exactly that.

Outside those guards the feature adds precisely **four** statements, and none of them resolves
anything: two bare declarations (`playlist_master_path = None` before the `try`, so the `finally`
can always run — the same exception `repeated_master_path` already is — and `playlist_tracks = ()`),
and the two truthiness tests described below. Being exact about this matters more than the
rounder claim: "no added statement runs" would be false, and the property that actually holds is
the one the test enforces.

That is not stylistic. Several suites AST-extract `_process_video_impl`,
`_process_video_guarded_unlocked`, `process_video_guarded` and `render_selected_variants_guarded`
and execute them against synthesised namespaces containing neither module; they pass precisely
because a Single Track render never looks them up. The same constraint is why the two new handler
defaults are the literals `'single'` and `()` rather than `fork_playlist.MODE_SINGLE` /
`fork_playlist.EMPTY` — the measured def-time trap `repeat_music`'s integer literal records. Tests
pin both literals equal to the real constants.

**The feature's entire intrusion into the pre-existing primary-input checks is those two truthiness
tests**: one `elif playlist:` branch and one `not playlist and` prefix. A Single Track render
evaluates `not ()` → `True` and reaches the identical checks with the identical messages.

**Verified against pristine `main` (`13cdfa48`)**, a second worktree, the same portable interpreter,
a hash-identical FFmpeg and the identical argv: one `framemd5` digest (`cd9f9bad…`, **150 frames**)
and one audio PCM hash (`b79826cc…`) — **identical on both trees**. Do not use a container hash as
the instrument here; MP4/Matroska carry generated identifiers and two runs of the same tree differ
from each other.

## The audio master: decode each, join once, then verify the SAMPLE COUNT

```
1. DECODE EACH   ffmpeg -i <track> -vn -map 0:a:0 -c:a pcm_s24le -ar 48000 -ac 2   NN.wav
2. JOIN          ffmpeg -f concat -safe 0 -i list.txt -c copy                      master.wav
3. VERIFY        master frames == SUM of the decoded tracks' frames, EXACTLY
                 and the probed duration agrees to within 1 ms
```

- **The one-command concat *filter* was rejected**, for the same reason `-stream_loop` was rejected
  for Repeat Music: it needs no intermediates, and it makes the result something this feature
  cannot measure. Decoding each track separately means every track's contribution is a byte run
  whose sample count is a fact on disk, so step 3 is an equality check against measured reality
  rather than an assertion about what a filtergraph probably did. It is also what makes the track
  boundaries in the render report real measurements instead of a cumulative sum of container
  metadata.
- **Step 3 is exact, not a tolerance.** A stream copy of consecutive PCM runs either reproduced
  every sample or it did not; one dropped or duplicated frame would put every later track at the
  wrong offset. The ffprobe duration check is the independent cross-check, not the primary one.
- **An UNMEASURED count is never a refusal.** `parse_wav_header` answers `None` for any shape it
  does not recognise — including a `data` chunk beyond the bounded header read, and a streamed
  WAV's `0`/`0xFFFFFFFF` placeholder size — and the builder then falls back to the probed duration
  with `measured=False` recorded on the result. This is `frame_lock.FrameCountVerdict`'s rule,
  restated: refusing a render because a header was shaped unusually is a worse failure than
  reporting that the strongest check could not run.
- **`-vn -map 0:a:0` is load-bearing**, not tidiness: an MP3 with cover art presents a video
  stream, and an M4A or MKV may carry several audio tracks.
- **There is no gap between two tracks, by construction**, and nothing is faded, aligned, padded or
  loudness-matched. V1 joins the songs; it does not mix them. Measured on the real fixtures: an
  18.0000 s soundtrack against 18.0000 s of container durations.
- **Heterogeneous inputs are normalised, and that is measured**: MP3/WAV/FLAC/M4A at
  44.1/48/32/22.05 kHz, stereo *and mono*, all become one 48 kHz stereo `pcm_s24le` master, with
  the mono track's length unchanged (5.0000 s).
- **The master lives in `session_dir`**, never `get_processing_dir()`, which `create_music_video`
  clears at the start of every render — the same rule and the same reason as
  `audio_mixdown.master_path_for` and `music_repeater.repeated_master_path`. Fresh uuid path per
  render, discarded on success, failure and cancellation alike; the per-track intermediates are
  removed by the builder's own `finally`.
- **Peak scratch is about TWICE the soundtrack** — every decoded track must exist while the join
  reads them — checked up front against the real volume and stated in the Render tab's read-out
  before the operator commits. A soundtrack above `MAX_MASTER_BYTES` is refused outright: a WAV
  `data` size is a 32-bit field, so a longer one would be written with a wrapped header and would
  desynchronise.

## Validation is of the WHOLE selection, and never returns a subset

`prepare_playlist_tracks` runs **before Stage 1 and before any decoding**, so a fixable input
problem costs no analysis and no FFmpeg work. Every queued entry must survive into validation: a
non-empty playlist may never come back as a smaller valid subset, so the collection is taken whole,
each entry is checked **in playlist order**, and the *first* unusable one raises. This is
`prepare_voice_inputs`' contract, restated for the primary input — rendering four of five queued
songs is a plausible-looking result that silently ignored the request.

Checked, in this order: a usable path value; a supported extension; the file exists and is a file;
it is not empty; **its recorded size and `mtime_ns` still match** (so a file *replaced* between
queueing and rendering is reported rather than silently rendered); ffprobe reports a usable
duration. A recorded identity of `0/0` means "never probed" rather than "probed as empty" and is
not treated as a change, which is what lets a playlist restored from a bare path list still render.

**There is no fallback to a shorter soundtrack and none to the Single Track picker.** Two failure
classes, because the render body classifies them differently and must not guess:

| raised | means | `RenderOutcomeKind` |
|---|---|---|
| `PlaylistInputError` | a queued track is missing, changed, unreadable, unsupported, empty or undecodable | `SHARED_FATAL` — the files are frozen for a whole batch, so every candidate fails identically |
| `PlaylistBuildError` | producing or verifying the master failed | `UNKNOWN_FATAL` — fail-closed; a timeout, a non-zero FFmpeg or a sample-count mismatch is indistinguishable here from a broken binary or a full disk |

`RenderCancelled` propagates **unchanged** through both and is never re-typed: re-typing it would
report a user's Stop as a broken music file, and in a batch would describe a cancelled candidate as
a problem with their playlist. Both clauses are narrow and are named **before** any generic handler
— `RenderCancelled` is an ordinary `Exception`.

## The seven editing operations

All pure, all total, all returning a **new** playlist, none of them silently deduplicating.

```
add_tracks      append the chosen files in FILENAME order, keeping the queue already there
remove_tracks   drop the selected positions; the selection comes back empty
move_up         each selected track one position earlier; a blocked block moves as a whole
move_down       the mirror image
shuffle         ONE seeded reorder, performed when the button is pressed
sort_by_name    stable, case-insensitive, filename first
clear           an empty queue
```

- **Order is identity.** Two playlists holding the same files in a different order are different
  soundtracks — the beats, the sections and therefore the whole edit differ.
- **A repeated track is kept.** Listing a song twice is an intentional repeat inside the
  soundtrack; dropping it would render a shorter video than the operator asked for. The selector's
  *values* are positions rather than names, which is what keeps a duplicated song editable
  unambiguously.
- **Ordering an ADDITION is deliberate.** A native multi-select dialog returns whatever order the
  OS supplies, not the operator's click order — the measured defect `audio_mix.voice_order_key`
  exists for — so that key is reused here rather than a second ordering rule being invented.
  Measured in the browser: a picker handing over `03, 01, 02, 04` produced a queue of
  `01, 02, 03, 04`. Existing tracks are never reordered by an Add.
- **The selection FOLLOWS the tracks.** `move_up` / `move_down` return the new selection, so
  repeated presses walk a track to the end and stop. Measured in the browser: four presses of Move
  Up walked track 4 to position 2 and Move Down took it back. A stale selection pointing at
  positions that no longer exist is how the next press removes the wrong track.
- **Shuffle is explicit, seeded and one-time.** The result is written straight into the queue the
  operator is looking at and the render freezes whatever is in that queue, so **there is no
  randomisation anywhere on the render path**. The seed is printed in the notice so an arrangement
  worth keeping can be recreated. It mints through the existing `variation.random_seed()` — the one
  place in this app that mints one — and is therefore the **fifth** entry in
  `test_variant_lab.py::test_only_the_shared_helper_owns_the_draw`'s exact list.
- **Nothing on disk is ever deleted.** Remove and Clear empty a queue; the notices say so.

## The render-admission freeze

The ordered soundtrack is captured **once**, at the same render boundary the creative profile, both
mix configs and the repeat count are resolved at — so nothing downstream ever sees a live widget
value, and an operator who reorders the queue while a render or a Variant Lab batch is running
cannot reach it.

```
playlist = ()
if music_source != 'single':
    playlist = fork_playlist.freeze_entries(music_source, playlist_state)
    if not playlist:  ->  SHARED_FATAL refusal, BEFORE RENDER_STARTED_KEY is set
```

- The `!= 'single'` pre-check is the explicit legacy branch this repository prefers over "the new
  code with a neutral value". Every other value, valid or malformed, goes through the **one**
  normaliser inside `freeze_entries`, which answers `()` for anything that is not a playlist
  declaration — so there is no second interpretation of what Music Source means. A test asserts
  `gui.py` contains exactly one `freeze_entries(` call.
- **What crosses into the pipeline is plain primitives**: a tuple of
  `(path, size, mtime_ns, duration)` tuples of `str`/`int`/`float`. Gradio deep-copies `gr.State`,
  and several suites execute `_process_video_impl` against a namespace holding no fork module —
  the same discipline Freestyle's eleven-value tuple follows, for the same measured reasons.
- **An empty playlist in playlist mode is a refusal, never a fall back to the Single Track
  picker**, which would render music the operator did not ask for, or nothing at all, without
  saying which. It happens before `RENDER_STARTED_KEY`, so a refused click raises no desktop
  notification — exactly as a source-gate refusal does not.

## The Render tab

```
music_source            gr.Radio   'single' (default) | 'playlist'
single_track_group      the UNCHANGED gr.File drop zone, only WRAPPED so one mode shows at a time
playlist_group          playlist_summary · playlist_selected · 7 buttons · playlist_notice
playlist_state          gr.State(())   -- the ONE authority on what the soundtrack is
repeat_music_heading    gr.Markdown    "Repeat Music" | "Repeat Playlist"
repeat_music            UNCHANGED slider, now show_label=False
repeat_music_estimate   mode-aware, written by the ONE read-out handler
```

- **The repetition control is renamed by a HEADING, never by writing the slider.** `repeat_music`
  must stay a **zero-writer** widget — the length of the video is the operator's decision — so the
  slider carries `show_label=False` and the name lives in a sibling `gr.Markdown`. This is exactly
  the pattern the Video Source radio already uses, and the `label=` constant is still passed, so
  assistive technology and every test that reads it are unaffected. Writing `gr.update(label=…)` to
  the slider would have been the easy way and is forbidden by
  `test_music_repeat.py::test_the_slider_has_no_writer_anywhere_in_the_app`.
- **`_on_repeat_music_change` is the ONE read-out function for both modes**, which is why the seven
  playlist handlers call it rather than formatting an estimate of their own. In playlist mode it
  probes **nothing**: every queued length was measured when the track was added, and re-probing a
  40-track playlist on every slider nudge would be a visible stall for a number already known.
- **The seven buttons write one projection** — `playlist_outputs`, as ONE tuple — for the same
  reason `_cut_handoff` returns the record and the button that reads it together: writing the state
  and the things that read it separately is how a selection outlives the rows it points at.
- **Switching modes discards neither the track nor the queue.** `music_source.change` writes the
  two panels' visibility, the heading and the two read-outs — never `audio_input`, never
  `playlist_state`. Measured in the browser: a 4-track queue survived a Playlist → Single Track →
  Playlist round trip intact.
- **Add tracks opens the native multi-select dialog**, through the same hardened machinery every
  folder field in this app uses (`-STA`, the start path and filter carried in the environment
  rather than on the command line, a bounded wait). Cancel, timeout and any failure to show the
  dialog all leave the queue **exactly** as it was, so pressing Add tracks can only ever add.
  Nothing is uploaded and nothing is copied: the queue holds the operator's own absolute paths,
  which is also what keeps a 40-track playlist out of Gradio's upload scratch. `gui.py` gains **no**
  `subprocess` call site — the dialog is delegated to `desktop_folders.pick_files`, exactly as
  `pick_folder` and `pick_file` already are, and that module's `subprocess.run` site count is why.
  **The reverted `audio_input` Browse experiment is not being revived**: that measurement is about
  a `gr.File` refusing to display an external path, and this button writes a `gr.State`.
- **A file the operator chose that cannot be used is skipped and COUNTED**, never queued with an
  invented length — and the notice says how many. Measured in the browser: four files handed over
  in an unusable path form produced `Added 0 of 4 chosen file(s) … 4 file(s) were not added`.

## Isolation: it is a render request, not source identity

Neither widget is wired into a source or preparation handler, so editing the playlist cannot clear
a confirmed **video** source set, start a scan or touch Media Library Preparation. Measured in the
browser against a live confirmation: the snapshot id `598f5a9448b7` survived Add tracks, Shuffle,
Sort, Remove and both mode switches, and Create Music Video stayed enabled throughout.

The converse half is the point of the split: editing the playlist **does** invalidate every
audio-dependent read-out, because the playlist handlers are the ones that write them.

Nothing about a playlist reaches `creative.py`, `creative_recipe.py`, `variant_lab.py`,
`variant_batch.py`, `presets.py`, `director.py`, `freestyle.py`, `render_batch.py` or
`variation.py` — creative variation must never modify the soundtrack's order or length.

**The Variant Lab freezes it once**, from its own submitted arguments, before the candidate loop —
exactly as it freezes the audio file, the source set, the encoder, `voice_overflow` and the repeat
count. Comparing candidates is only meaningful when the thing that differs between them is their
creative settings. Measured on a real 2-candidate batch over a 3-track playlist: both candidates
450 frames, both 18.000000 s, and **byte-identical audio** (`ee7adf2d…`) with different video.

## Reporting

One line on the success panel, and only when the render was a playlist render — a Single Track
panel is character for character what it was. It is read back off the bus through the pure module's
single reader/formatter, never from the queue widget or the frozen tuple: the panel must report
what the render **did**, and the measured track offsets are a fact only the prepared soundtrack
knows.

```
🎼 MUSIC PLAYLIST: 3 tracks · 00:18 soundtrack
   1. 00:00  01_low_120bpm.mp3  (00:06)
   2. 00:06  02_mid_90bpm.wav   (00:05)
   3. 00:11  03_high_150bpm.flac (00:07)
🔁 REPEAT PLAYLIST: 2x - 00:18 -> 00:36
```

The repeat line is **R10's own**, from R10's one reader reading R10's one bus record. R16 changed
exactly one thing about it: `summary_line` gained an optional `label`, defaulting to the pre-R16
wording, so the panel can name the control the operator actually saw. The arithmetic, the measured
cycle and the total are untouched, there is still no second formatter, and `music_repeat.py` still
cannot tell a playlist from a single track. The playlist's own bus record carries **no** repeat
count, for the same reason: one fact with two independently-formatted views is how a panel starts
disagreeing with itself.

Progress uses the existing model: Stage-1 `STATE` events under `phase="music_playlist"`, one per
decoded track. No new stage, no new counter, no ETA.

## Measured end to end

Real portable FFmpeg, real pipeline, real browser. Full logs and JSON in the task's evidence folder.

| what | result |
|---|---|
| single track, candidate vs pristine `13cdfa48` | **identical** framemd5 `cd9f9bad…`, 150 frames, audio PCM `b79826cc…` |
| 3-track playlist + voice + SFX (CPU) | 450 frames published = contract 450; A/V **0.0 ms** apart; music heard as 220 → 660 → 1320 Hz |
| voice placement | audible at 1.1 / 2.1 / 5.1 / 6.1 / 7.1 / 11.1 / 12.1 s — **three of the seven windows past the first track** |
| Repeat Playlist 2× | 900 frames = contract; tones **A B C A B C**, not A A B B C C; 15 cuts against 7 at 1× |
| 4-track playlist via the browser, **NVENC** | 551 frames published = contract 551; audio and video both 22.040000 s |
| Variant Lab 2 candidates | identical audio bytes, 450 frames each, different video |
| builder, 36 real-FFmpeg checks | 36/36 — exact sample counts, measured boundaries, heterogeneous inputs, six loud failures, three cancellations, no scratch left on any path |

**Known limitation, measured and left open:** the L2 cache miss described above. **Not implemented
and deliberately so:** crossfades, DJ mixing, automatic loudness mastering, per-song repeat, and a
CLI flag — `video_processor.py`'s headless entry point renders a single audio file, exactly as it
did, and R16 adds nothing to its argv.
