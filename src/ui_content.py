#!/usr/bin/env python3
"""
UI Content for BeatSync Engine
Focused on Auto Mode.
"""

# ============================================================================
# MAIN UI CONTENT
# ============================================================================

UI_TITLE = "🎵 BeatSync Engine"

UI_MAIN_DESCRIPTION = """Create music videos that cut to the beat. Upload audio and video clips 
to automatically generate a video synchronized with your music's rhythm."""

# ============================================================================
# SYSTEM STATUS
# ============================================================================

def get_system_performance_info(cpu_count, max_threads, parallel_workers, python_status, 
                                cuda_status, ffmpeg_status, gpu_status, gpu_device, nvenc_status):
    """System performance overview."""
    return f"""## 🚀 System Status
- **Python**: {python_status} | **CUDA**: {cuda_status}
- **CPU**: {cpu_count} threads | **FFmpeg**: {ffmpeg_status}
- **GPU**: {gpu_status}{gpu_device} | **NVENC**: {nvenc_status}
- **Parallel Processing**: {parallel_workers} workers | **Input**: Local project folder"""

PORTABLE_SETUP_INFO = """## 📦 Portable Setup
Self-contained installation - Python 3.13.14, CUDA 13.3, and FFmpeg included. 
No system dependencies required."""

GPU_ACCELERATION_INFO = """## ⚡ GPU Acceleration
- **Audio Analysis**: 5-10x faster with GPU (CuPy + CUDA)
- **Video Encoding**: 2-3x faster with NVENC hardware encoder
- **Auto-Detection**: Uses GPU automatically when NVIDIA card detected"""

NVENC_BENEFITS_INFO = """## 🎬 NVENC Hardware Encoding
2-3x faster than CPU encoding with comparable quality. Automatically enabled when available."""

EXPORT_OPTIONS_INFO = """## 🎬 Export Modes
- **NVIDIA NVENC H.264**: GPU-accelerated H.264 (fast, .mkv)
- **NVIDIA NVENC HEVC**: GPU-accelerated H.265 (smaller files, .mkv)
- **CPU H.264**: Software encoding (.mkv)
- **ProRes 422 Proxy**: Frame-perfect lossless (.mov)"""

PRORES_MODE_INFO = """## 🎯 ProRes 422 Proxy Mode
Frame-perfect cuts with zero quality loss. Converts input to ProRes (I-frames only), 
then uses lossless concatenation. Larger files, perfect accuracy.

**How it works:**
1. Converts input videos to ProRes 422 Proxy (all I-frames)
2. Extracts segments with exact frame counts (frame-perfect)
3. Concatenates with stream copy (zero quality loss)
4. Adds music track with PCM audio

**Best for:** Professional editing, archival, maximum quality"""

# ============================================================================
# MODE DESCRIPTION
# ============================================================================

AUTO_MODE_DESCRIPTION = """**🤖 Auto Mode - Audio-Visual Intelligence**

Fully automatic music + video analysis with rhythmic source matching:

**Features:**
- 🎵 **Song Structure Detection**: Intro/Verse/Chorus/Bridge/Outro
- ⚡ **Energy Analysis**: High/Medium/Low energy per section
- 🎯 **Rhythm Pattern Recognition**: Kick/Clap/Bass/Hi-hat patterns
- 🎬 **Video Moment Analysis**: Motion, quality, scene changes, action/beauty
- 🧠 **Qwen3-VL Semantic Tags**: Drop/soft/build/action/emotion matching
- 🎼 **Planned Clip Selection**: Chooses source moments instead of random clips

**How it works:**
- Analyzes your song's energy and structure
- Builds a cache of strong source-video moments
- Matches aggressive drops to action/high-motion scenes
- Matches soft melodic parts to beautiful or emotional scenes
- Detects dominant rhythm patterns per section
- Automatically adjusts cut frequency
- Keeps the edit rhythmic while avoiding weak or repetitive source moments

**Example:**
- Aggressive drop → Action/combat/chase/high-motion shots
- Beautiful bridge → Soft, clean, emotional shots
- Build-up → Tension/camera-motion shots
- Fast rhythmic chorus → More frequent beat-locked cuts

**Best for:** Complete hands-off AMV/GMV creation with rhythmic visual matching"""

# ============================================================================
# PERFORMANCE GUIDE
# ============================================================================

def get_performance_guide(cpu_count, parallel_workers, python_status, cuda_status, 
                         ffmpeg_status, gpu_available, gpu_info, nvenc_available):
    """Concise performance guide."""
    
    gpu_text = ""
    if gpu_available:
        nvenc_text = "NVENC: 2-3x faster encoding" if nvenc_available else ""
        gpu_text = f"""**GPU Acceleration** ({gpu_info}):
- Audio analysis: 5-10x faster
- {nvenc_text}

"""
    
    return f"""**System**: {cpu_count} CPU threads | {parallel_workers} parallel workers

**Portable Components**:
- Python: {python_status}
- CUDA: {cuda_status}
- FFmpeg: {ffmpeg_status}

{gpu_text}**Processing Modes**:
- **NVENC H.264**: GPU-accelerated (fastest)
- **NVENC HEVC**: GPU-accelerated (smaller files)
- **CPU H.264**: Software encoding
- **ProRes 422 Proxy**: Lossless (converts to ProRes, then concatenates)

**Temp Files**: `./temp/` folder (local only, auto-cleaned)

**FPS**: Leave empty to auto-detect from input video, or set custom (24/30/60)

**Speed Estimates** (3-min video):
- CPU only: ~2-3 min
- GPU + NVENC: ~45-90 sec ⚡
- ProRes: ~3-5 min (conversion) + instant (concat)

**RAM Usage**: Constant with FFmpeg (no batch processing needed)

**Parallel Workers**:
- More workers = faster processing
- Recommended: {parallel_workers} (auto-calculated from CPU)
- With NVENC: Can use more workers (GPU handles encoding)"""

# ============================================================================
# GUIDES
# ============================================================================

NVENC_GUIDE_ACTIVE = """**🚀 NVENC Auto-Enabled!**
GPU hardware encoding: 2-3x faster than CPU.
Select NVIDIA NVENC H.264 or HEVC for best performance.

---

"""

QUICK_START_GUIDE = """### 💡 Quick Start

**🤖 Auto Mode:**
1. Upload audio + videos
2. Click "Create Music Video"
3. Done! Automatic audio-visual cuts

**🎯 Lossless (ProRes):**
- Select **ProRes 422 Proxy** mode for frame-perfect quality

---

"""

# ============================================================================
# SYSTEM INFO PANEL
# ============================================================================

def get_system_info_panel(python_status, cuda_status, cpu_count, max_threads, 
                         ffmpeg_status, gpu_status, gpu_device, nvenc_status, 
                         librosa_version):
    """Compact system info."""
    return f"""**System:**
Python: {python_status} | CUDA: {cuda_status}
CPU: {cpu_count} cores ({max_threads} threads)
FFmpeg: {ffmpeg_status} | Librosa: {librosa_version}
GPU: {gpu_status}{gpu_device} | NVENC: {nvenc_status}

**Files:**
- Python: `bin/python-3.13.14-embed-amd64/`
- CUDA: `bin/CUDA/v13.3/`
- FFmpeg: `bin/ffmpeg/ffmpeg.exe`
- Temp: `./temp/` (local only)
- Output: `./output/`

**Mode:**
- 🤖 Auto: Audio-visual rhythmic intelligence"""

# ============================================================================
# STATUS MESSAGES
# ============================================================================

def get_ready_status(python_status, cuda_status, max_threads, cpu_count, ffmpeg_status,
                    gpu_available, gpu_info, nvenc_available):
    """Ready status message."""
    return '✅ Ready to process!\n\nUpload audio and video files to begin.'

# ============================================================================
# SUCCESS MESSAGES
# ============================================================================

def _format_auto_section_summary(sections_info):
    """Return a safe section summary for all Auto Mode versions.

    Supports both the older flat dictionaries:
        {"section": "chorus", "selected_beats": 8, "total_beats": 32, "selection_ratio": 0.25}
    and the newer V3.2 wave dictionaries:
        {"section": {"type": "chorus", ...}, "selected_count": 8, "beat_count": 32, "density": 0.25}
    """
    if not sections_info:
        return ""

    lines = ["Sections analyzed and processed:"]
    for item in sections_info[:12]:
        if not isinstance(item, dict):
            continue

        raw_section = item.get('section', item.get('type', 'section'))
        if isinstance(raw_section, dict):
            section_name = raw_section.get('type') or raw_section.get('section') or raw_section.get('name') or 'section'
        else:
            section_name = raw_section

        section_name = str(section_name).replace('_', ' ').strip().title() or 'Section'

        selected = item.get('selected_beats', item.get('selected_count', item.get('cuts', 0)))
        total = item.get('total_beats', item.get('beat_count', item.get('beats', 0)))
        ratio = item.get('selection_ratio', item.get('density', None))

        try:
            selected_i = int(selected)
        except Exception:
            selected_i = 0
        try:
            total_i = int(total)
        except Exception:
            total_i = 0

        if ratio is None:
            ratio = selected_i / total_i if total_i > 0 else 0.0
        try:
            ratio_f = float(ratio)
        except Exception:
            ratio_f = 0.0

        lines.append(f"      - {section_name}: {selected_i}/{total_i} beats ({ratio_f * 100:.1f}%)")

    if len(sections_info) > 12:
        lines.append(f"      - ... plus {len(sections_info) - 12} more sections")

    return "\n".join(lines)


def get_success_message_auto(total_cuts, total_beats, tempo, sections_info,
                            python_str, cuda_str, max_threads, cpu_count,
                            parallel_workers, gpu_info, encoder_info,
                            codec_info, fps_info, filename, audio_info,
                            audio_duration=None, output_fps=None,
                            total_processing_seconds=None, processing_label=None,
                            variation_text=None,
                            # [FORK] Digital-Union (Freestyle V1): optional and appended last, so
                            # every existing caller renders exactly as before.
                            freestyle_text=None):
    """Success message for Auto mode. Compatible with Auto Mode V1/V2/V3/V3.2."""

    section_summary = _format_auto_section_summary(sections_info)
    try:
        audio_duration_f = float(audio_duration)
    except Exception:
        audio_duration_f = 0.0
    try:
        output_fps_f = float(output_fps)
    except Exception:
        output_fps_f = 0.0
    try:
        total_seconds_i = int(round(float(total_processing_seconds)))
    except Exception:
        total_seconds_i = 0
    processing_text = processing_label or encoder_info
    fps_text = f"{output_fps_f:.1f}" if output_fps_f else str(fps_info).split()[0]
    # [FORK] Digital-Union (Phase A): optional, so existing callers render exactly as before.
    # A seed is only worth reading back when the user actually chose one.
    variation_line = f"Creative variation: {variation_text}\n" if variation_text else ""
    # [FORK] Digital-Union (Freestyle V1): the section rules, as the numbers that actually ran.
    # Omitted when Freestyle had no effect, so a today-style render's panel is byte-identical.
    # The label lives here rather than in `FreestyleDeclaration.describe()`, exactly as it does for
    # the variation line above: `describe()` then stays a bare list of resolved numbers, which is
    # what makes it read correctly inside the Stage-4 console line's parentheses too.
    freestyle_line = f"Freestyle: {freestyle_text}\n" if freestyle_text else ""

    return f"""✅ Video created successfully!

Statistics:
Video processing: {processing_text}
{variation_line}{freestyle_line}Total cuts: {total_cuts}
Audio duration: {audio_duration_f:.2f} seconds
Output FPS: {fps_text}
{total_beats} beats detected at {tempo:.1f} BPM

{section_summary}
Total time processing: {total_seconds_i} seconds

Output: {filename}"""


# ============================================================================
# CONSOLE MESSAGES
# ============================================================================

CONSOLE_SEPARATOR = "=" * 70

def get_startup_header(cpu_count, max_threads, parallel_workers, python_status, 
                      cuda_status, librosa_version, ffmpeg_status, gpu_available, 
                      gpu_info, nvenc_available):
    """Startup header."""
    gpu_line = f"   GPU: {gpu_info} (Auto-enabled)" if gpu_available else "   GPU: Not available (CPU only)"
    nvenc_line = f"   NVENC: Available (Auto-enabled)" if nvenc_available else "   NVENC: Not available"
    
    return f"""{CONSOLE_SEPARATOR}
🎵 BeatSync Engine
{CONSOLE_SEPARATOR}
   Python: {python_status}
   CUDA: {cuda_status}
   FFmpeg: {ffmpeg_status}
   Librosa: {librosa_version}
   CPU: {cpu_count} threads ({max_threads} max per encode)
   Parallel Workers: {parallel_workers}
   {gpu_line}
   {nvenc_line}
   Mode: 🤖 Auto
   ProRes 422 Proxy: ENABLED"""

# ============================================================================
# INPUT LABELS & INFO
# ============================================================================

LABEL_AUDIO_FILE = "🎵 Audio File (MP3/WAV/FLAC)"

# ============================================================================
# [FORK] Digital-Union (FUN R10 Repeat Music)
# Play the selected song 1 to 30 times and render a full-length music video. The repetitions get
# NEWLY PLANNED shots -- the music repeats, the edit does not -- so the info text must not promise
# or imply a looped video.
# ============================================================================

LABEL_REPEAT_MUSIC = "🔁 Repeat Music"
INFO_REPEAT_MUSIC = (
    "How many times to play the song, 1 to 30. **1x is a normal render.** Above that the video "
    "gets longer and every repetition is given **freshly planned shots** — it is not a looped "
    "copy of the first pass. The music itself is never slowed, stretched or re-pitched, and your "
    "music file is never modified."
)
#: Shown under the slider before a render. Content comes from
#: `beatsync_fork.music_repeat.estimate_markdown`; this is only the empty state.
REPEAT_MUSIC_ESTIMATE_INITIAL = (
    "**Repeat Music: 1x** - load a music track to see the estimated output length."
)

# ============================================================================
# [FORK] Digital-Union: Audio Layers V1 (D)
# See src/beatsync_fork/audio_mix.py for the placement rules and src/audio_mixdown.py for the
# mixdown. Voice changes the final audio only — the video edit is still decided entirely by the
# main music, which is the only audio BeatSync analyses.
# ============================================================================

LABEL_AUDIO_LAYERS = "🎙️ Audio Layers"
INFO_AUDIO_LAYERS = (
    "Add spoken voice over the music. **Clips play in filename order** — name them `01_intro.wav`, "
    "`02_quote.wav`, `03_outro.wav` to control the sequence; the order you pick them in the file "
    "dialog is not used. Voice affects the **final audio only**: the cuts, the shot choices and the "
    "whole video edit are still decided by your main music, which is the only audio analysed. "
    "Leave this empty and nothing about your render changes."
)

LABEL_VOICE_FILES = "🎤 Voice clips (MP3/WAV/FLAC)"
INFO_VOICE_FILES = (
    "Played in filename order, never overlapping each other, and never extending past the music."
)

LABEL_VOICE_START_DELAY = "⏱️ Start delay (seconds)"
INFO_VOICE_START_DELAY = (
    "How long to wait before the first clip may start, so speech does not begin at 0:00. The clip "
    "then lands on the first suitable beat after this point."
)

LABEL_VOICE_MIN_GAP = "↔️ Minimum gap (seconds)"
INFO_VOICE_MIN_GAP = (
    "Shortest silence between the end of one voice clip and the start of the next."
)

LABEL_VOICE_AVOID_DROPS = "🚫 Avoid drops"
INFO_VOICE_AVOID_DROPS = (
    "Keep speech entirely out of drop and finale sections — a clip may not even run into one. "
    "Turn this off to allow talking over the biggest moments."
)

LABEL_MUSIC_UNDER_VOICE = "🔉 Music under voice"
INFO_MUSIC_UNDER_VOICE = (
    "Percent of the normal music level while voice is speaking. 35 = music at 35% of its usual "
    "level under speech; 100 = no ducking at all; 0 = music silent under speech. The music fades "
    "down and back up smoothly around each clip."
)

# [FORK] R7.3 (voice overflow): what to do when the music runs out before the clips do. The
# default is the historical behaviour, so an operator who never touches this sees no change.
LABEL_VOICE_OVERFLOW = "🧩 When voice clips do not fit"
INFO_VOICE_OVERFLOW = (
    "What to do when a clip has nowhere legal left to play — the music ends first, or *Avoid "
    "drops* blocks the only remaining room.\n\n"
    "• **Strict / Error** (default) — refuse the render and name the clip. Nothing is dropped or "
    "shortened behind your back. This is how BeatSync has always behaved.\n\n"
    "• **Skip clips that do not fit** — leave that clip out and carry on. A shorter clip later in "
    "the sequence can still land, because skipping one clip does not move the position the next "
    "one starts looking from. The order is never rearranged and nothing is shortened.\n\n"
    "• **Trim final usable clip, then skip remaining** — the same, except **one** clip may be cut "
    "short to use up the room that is actually left (at least 0.5 s of it, on a beat, still "
    "respecting *Avoid drops*). Everything after that is skipped.\n\n"
    "Skipped and trimmed clips are always listed by filename in **Voice placement** below — "
    "nothing is dropped silently. Your audio files are never modified."
)
CHOICE_VOICE_OVERFLOW_STRICT = "Strict / Error (refuse the render)"
CHOICE_VOICE_OVERFLOW_SKIP = "Skip clips that do not fit"
CHOICE_VOICE_OVERFLOW_TRIM = "Trim final usable clip, then skip remaining"

LABEL_AUDIO_LAYERS_REPORT = "Voice placement"
PLACEHOLDER_AUDIO_LAYERS_REPORT = (
    "No voice clips selected. Add clips above and render to see where they land."
)

# ============================================================================
# [FORK] Digital-Union: Smart Mix / SFX Pool V1 (E)
# See src/beatsync_fork/smart_mix.py for the five placement rules and src/audio_mixdown.py for the
# library scan and the FFmpeg streams. SFX land in the same final master Audio Layers already
# produces: they change the final audio only, never the video edit.
# ============================================================================

LABEL_SMART_MIX = "🔊 Smart Mix / SFX"
INFO_SMART_MIX = (
    "Add sound-design accents — impacts on strong hits, risers into drops, atmosphere beds in "
    "intros and breakdowns, transitions at section changes and short vocal shots. Point this at a "
    "folder whose **first-level subfolders name the roles** (`Impacts/`, `Risers/`, `Atmosphere/`, "
    "`Transitions/`, `VocalShots/`). Placement is deterministic and seedless: the same library, the "
    "same track and the same settings always give the same result. SFX affect the **final audio "
    "only** — the cuts and shot choices are still decided by your main music. Leave the folder "
    "empty and nothing about your render changes."
)

LABEL_SFX_FOLDER = "📂 SFX library folder"
PLACEHOLDER_SFX_FOLDER = r"D:\Audio\SFX"
INFO_SFX_FOLDER = (
    "Local folder read in place — nothing is copied. Accepted subfolder names: Impacts, Risers, "
    "Atmosphere (or Ambience), Transitions, VocalShots. Anything else is ignored and reported. "
    "MP3/WAV/FLAC only."
)

LABEL_SFX_ROLES = "🎚️ Enabled roles"
INFO_SFX_ROLES = (
    "Only ticked roles are scanned, probed and placed. Unticking a role is the clean way to use "
    "part of a library."
)

LABEL_SFX_AMOUNT = "✨ SFX Amount"
INFO_SFX_AMOUNT = (
    "How busy the sound design is. 0 = no SFX at all. Higher values lower the impact threshold, "
    "shorten the minimum spacing and raise the transition and vocal-shot limits. Risers and "
    "atmospheres follow the song's structure and are not affected by this. Density is always "
    "bounded — this control cannot turn the mix into noise."
)

LABEL_SFX_LEVEL = "🔊 SFX Level"
INFO_SFX_LEVEL = (
    "Percent of full level for every SFX. 50 = half level, 100 = full, 0 = silent (useful for "
    "checking placement without hearing it). This is a plain level, not decibels, and it does not "
    "duck the music — only voice does that."
)

LABEL_SMART_MIX_REPORT = "SFX placement"
PLACEHOLDER_SMART_MIX_REPORT = (
    "No SFX library selected. Choose a folder above and render to see what was placed."
)

LABEL_VIDEO_FILES = "🎥 Video Files (MP4/MKV)"

# [FORK] R8 UX Refresh (Phase 6): the Output Video Profile replaces the free-text FPS box.
# `LABEL_CUSTOM_FPS` / `INFO_CUSTOM_FPS` are deleted rather than kept: a frame rate with no
# resolution beside it was half a setting, and the half that was missing is where the real defect
# lived -- the render took its resolution from the first source and stretched everything else
# to match.
# [FORK] R8 UX Refresh (Phase 9): optional soft edges on exported audio clips.
LABEL_AUDIO_FADE_EDGES = "🌊 Smooth clip edges (fade in/out)"
INFO_AUDIO_FADE_EDGES = (
    "Softens the very start and end of each clip so it blends under music instead of clicking "
    "in. The fade uses **only the Padding** around each clip, never the speech itself — so a clip "
    "with little room gets a short fade, and one with none gets a sharp edge. Off by default."
)

LABEL_OUTPUT_PROFILE = "🎞️ Output Video Profile"
#: [FORK] R8 Final Polish: three lines to one. The sentence that was cut said a fixed profile
#: letterboxes rather than stretching — true, and it is the reassuring half, but it described
#: behaviour an operator sees in the result rather than a choice they have to make here. It sat
#: in the one column that decides whether Create Music Video is above the fold at 1440x900.
INFO_OUTPUT_PROFILE = (
    "Size and frame rate of the finished video. **Auto** follows the first source; a fixed "
    "profile letterboxes instead of stretching."
)

# GPU & Processing
LABEL_GPU_STATUS = "⚡ GPU Acceleration Status"

LABEL_PROCESSING_MODE = "🎬 Processing Mode"

# Performance
LABEL_PARALLEL_WORKERS = "⚡ Parallel Workers"
INFO_PARALLEL_WORKERS = "Clips processed simultaneously. More workers with GPU."

# Output
LABEL_OUTPUT_FILENAME = "📝 Output Filename"
INFO_OUTPUT_FILENAME = "Timestamp added automatically (.mkv or .mov)"

# ============================================================================
# [FORK] Digital-Union: creative direction (Phase A — variation seed)
# See src/beatsync_fork/variation.py for the selection rule these labels describe.
# ============================================================================

LABEL_VARIATION_SEED = "🎲 Variation Seed"
INFO_VARIATION_SEED = (
    "0 = default BeatSync selection. Any positive number picks a different but reproducible "
    "clip plan from the same analysed sources — same seed always gives the same edit. "
    "Changing this does not re-analyse your videos."
)
LABEL_RANDOMIZE_SEED = "🎲 Randomize"

# ============================================================================
# [FORK] Digital-Union: creative direction (Creative Controls Core)
# See src/beatsync_fork/creative.py for the mappings these labels describe.
# All three are 0..100 with 50 meaning "exactly what BeatSync does today", and none of them
# re-analyses anything: they change planning, never the video analysis cache.
# ============================================================================

LABEL_CUT_DENSITY = "✂️ Cut Density"
INFO_CUT_DENSITY = (
    "Sparse ↔ Dense. 50 = current BeatSync behaviour. Lower holds shots longer, higher cuts more "
    "often — always on the detected beat/bar/phrase grid. Changing this does not re-analyse your "
    "videos."
)

LABEL_ENERGY_RESPONSE = "⚡ Energy Response"
INFO_ENERGY_RESPONSE = (
    "Weak ↔ Strong target matching. 50 = current BeatSync behaviour. Higher follows the music's "
    "drop/build/soft targets harder; lower favours generally good-looking moments. The cut timing "
    "and the music analysis are unchanged either way."
)

LABEL_MOTION_BIAS = "🎥 Motion Bias"
INFO_MOTION_BIAS = (
    "Calm ↔ Dynamic. 50 = current BeatSync behaviour. Lower prefers steadier shots, higher prefers "
    "moving ones. Only the choice of source moment changes — never the cut timing."
)

LABEL_SOURCE_DIVERSITY = "🗂️ Source Diversity"
INFO_SOURCE_DIVERSITY = (
    "Reuse ↔ Diverse. 50 = current BeatSync behaviour. Higher spreads the edit across more of your "
    "source videos; lower lets a strong source come back more often. Protection against repeating "
    "the same moment is unaffected, and so are the cut timing and the music analysis."
)

LABEL_SEMANTIC_EMPHASIS = "🧠 Semantic Emphasis"
INFO_SEMANTIC_EMPHASIS = (
    "Visual Metrics ↔ Semantic Context. 50 = current BeatSync behaviour. Lower leans on measured "
    "visual metrics (motion, sharpness, colour, exposure); higher gives more weight to what the "
    "scene was understood to contain. This does not switch analysis off — it only changes how your "
    "already-analysed library is interpreted. Expect a clearer effect on soft and building sections "
    "than on obvious action."
)

LABEL_MICRO_CUTS = "✨ Micro Cuts"
INFO_MICRO_CUTS = (
    "Fewer ↔ More accents. 50 = current BeatSync behaviour. This is only the rare extra half-beat "
    "cut on the biggest impacts — 0 turns it off entirely. The main cut rhythm is Cut Density; this "
    "never makes the edit flickery."
)

# ============================================================================
# [FORK] Digital-Union: AI Director V2
# See src/beatsync_fork/director.py and src/beatsync_fork/director_media.py.
#
# V1's copy said the Director "has not watched your footage" and stopped there, which was complete
# while V1 was wholly media-blind. V2 needs a sharper distinction, because one half changed and the
# other did not:
#
#   THE MODEL        reads the user's words only. It never receives footage, frames, filenames, a
#                    media summary, a Stage-5 record or any count — so there is no media
#                    prompt-injection surface at all.
#   LOCAL BEATSYNC   may read one small aggregate that a CURRENT, FULLY PREPARED Media Library scan
#                    already produced, and use it to relax an over-strong Source Diversity request.
#                    Deterministic code, no model involved, no re-analysis.
#
# Three claims this copy must never make: that the instruction reproduces a result (a prompt is not
# a recipe identifier, and the Variation Seed is minted fresh every time); that the Director has
# watched the footage or understands the media library; and that the adjustment is an improvement —
# it is a measured trade-off that may allow more adjacent source reuse. Applying moves the existing
# Creative Controls below; Create Music Video is still the only thing that renders.
# ============================================================================

LABEL_AI_DIRECTOR = "🎬 AI Director"
INFO_AI_DIRECTOR = (
    "**Describe the edit you want and get a proposal for the controls below.** Review it, then "
    "apply it if you like it — applying writes the Variation Seed and the six Creative Controls, "
    "and nothing else. It never renders automatically, never re-analyses your videos and never "
    "touches your confirmed source files. Afterwards you can edit any control by hand, explore "
    "from there with Variant Lab, or press Create Music Video.\n\n"
    "The model reads your words only: it has not watched your footage, it does not know the track, "
    "and it does not look at the settings currently on screen — each instruction is read as a fresh "
    "description of the edit you want.\n\n"
    "If you have a **fully prepared** Media Library, BeatSync may then relax a very strong Source "
    "Diversity request on its own: when a prepared library has only a few effective sources, extra "
    "diversity pressure cannot spread the edit any wider, so it only costs clip quality. That check "
    "is ordinary local arithmetic over the counts your scan already produced — nothing is sent to "
    "the model and nothing is re-analysed — and relaxing it may allow more adjacent source reuse. "
    "The proposal shows the original value, the adjusted value and the reason."
)

LABEL_DIRECTOR_INSTRUCTION = "📝 Instruction"
PLACEHOLDER_DIRECTOR_INSTRUCTION = "Describe the editing style you want…"
INFO_DIRECTOR_INSTRUCTION = (
    "One or two sentences about pacing, energy, calm or movement, and how much the edit should "
    "spread across your source videos. Nothing happens until you press Generate Proposal."
)

LABEL_GENERATE_PROPOSAL = "🧠 Generate Proposal"
LABEL_APPLY_PROPOSAL = "⬅️ Apply Proposal"

LABEL_DIRECTOR_PROPOSAL = "Director proposal"
PLACEHOLDER_DIRECTOR_PROPOSAL = (
    "No proposal yet. Describe the edit you want above and press Generate Proposal. "
    "A proposal changes nothing until you apply it."
)

LABEL_DIRECTOR_STATUS = "Director status"
PLACEHOLDER_DIRECTOR_STATUS = (
    "Generate Proposal asks the local model for a set of controls. Apply Proposal writes them into "
    "the Creative Controls below. Create Music Video is still the only thing that renders."
)

# ============================================================================
# [FORK] Digital-Union: creative presets (Creative Controls Extra PR3)
# See src/beatsync_fork/presets.py for the four recipes this label describes. A preset is only a
# named set of values for the six sliders below — the sliders stay the one thing that is rendered.
# ============================================================================

LABEL_CREATIVE_PRESET = "🎛️ Creative Preset"
INFO_CREATIVE_PRESET = (
    "A starting point for the six controls below. Picking one just moves those sliders — you can "
    "still adjust any of them afterwards, and the selector then reads Custom. Balanced is current "
    "BeatSync behaviour and is also the way back to it. The Variation Seed is never changed by a "
    "preset, and no preset re-analyses your videos."
)

# ============================================================================
# [FORK] Digital-Union: Freestyle V1
# See src/beatsync_fork/freestyle.py. Two things this copy must say plainly, because both are
# places a user could reasonably assume otherwise: a section style applies only FIVE of the six
# controls (Micro Cuts stays global), and every instance of a section type shares one rule. The
# summary shows `Base` rather than an inherited number on purpose — the global controls have five
# legitimate writers, so a number there could not be kept honest.
# ============================================================================

LABEL_FREESTYLE = "🎚️ Freestyle (per-section rules)"
INFO_FREESTYLE = (
    "**Let different parts of the song be edited differently.** Pick a style for a section type "
    "and every section of that type uses it; the rest of the track keeps your global Creative "
    "Controls above.\n\n"
    "A section style sets **five** controls — Cut Density, Semantic Emphasis, Energy Response, "
    "Motion Bias and Source Diversity. **Micro Cuts and the Variation Seed stay global** for the "
    "whole render, so a style named *Cinematic* applies Cinematic's five values and keeps your "
    "Micro Cuts exactly as you set it.\n\n"
    "Section types are detected from the music while rendering, so rules are matched by type "
    "rather than by position: if the track has two choruses, both use the *chorus* rule. A type "
    "that does not occur simply never applies. Nothing here re-analyses your videos, starts a "
    "render, or affects your confirmed source files."
)

LABEL_FREESTYLE_ENABLED = "Enable Freestyle"
INFO_FREESTYLE_ENABLED = (
    "Off by default. With this off — or on with every section left at Base — the render is exactly "
    "what it would be without Freestyle at all."
)

# [FORK] R8 Owner Fixes (P1): the state badge. The owner's complaint was that a small checkbox is
# easy to miss, so the panel now SAYS which mode it is in rather than leaving the operator to
# read a tick box. Two states, two sentences, and the off state names what is used instead --
# "nothing happens" is not an explanation.
#
# These are display text written by `_on_freestyle_change` from the live checkbox value. They
# carry no authority: the checkbox is still the only gate, exactly as it was.
FREESTYLE_BADGE_OFF = (
    "### ⚪ Freestyle is OFF\n\n"
    "Every section of the song is edited with the six global **Creative Controls** above."
)
FREESTYLE_BADGE_ON = (
    "### 🟢 Freestyle is ON\n\n"
    "The per-section rules below replace the global Creative Controls for the section types you "
    "set. Micro Cuts and the Variation Seed stay global."
)

# ============================================================================
# [FORK] Digital-Union: GUI Refresh A — tabs
# Layout only. No execution setting is duplicated and no new authority exists.
#
# [FORK] R8 UX Refresh (Commit A): seven top-level tabs, named for what the operator is
# trying to do rather than for how the feature arrived. Four renames and two new tabs:
#
#   Quick Render     -> Render            (there is only one render workflow; "Quick" implied
#                                          a second, faster one that never existed)
#   Creative Studio  -> Creative Settings (it is where creative configuration lives, and the
#                                          preset selector moved here to join its six sliders)
#   Advanced Tools   -> Audio Mix         ("Advanced Tools" named a drawer, not a job; the
#                                          non-audio tenant moved to Media Library)
#   Clips Preview  \
#   Audio Preview   >-> Media Library     (three views of one library, now sections of one tab)
#   Library Prep   /
#   (new)            -> Settings          (preferences were buried at the foot of Clips Preview)
#
# The old constant names are deliberately NOT kept as aliases: a stale alias is how a label
# and the thing it names drift apart, and every reference is in this repository.
# ============================================================================

LABEL_TAB_RENDER = "⚡ Render"
LABEL_TAB_CREATIVE_SETTINGS = "🎨 Creative Settings"
LABEL_TAB_RAW_CUTTER = "✂️ Raw Cutter"
LABEL_TAB_AUDIO_MIX = "🎚️ Audio Mix"
LABEL_TAB_MEDIA_LIBRARY = "📚 Media Library"
LABEL_TAB_SETTINGS = "⚙️ Settings"

# The three Media Library sections. These are `gr.Tab`s nested inside the Media Library tab,
# so they are sections of a workflow rather than top-level destinations.
LABEL_SECTION_VIDEO_REVIEW = "🖼️ Video Review"
LABEL_SECTION_AUDIO_REVIEW = "🎧 Audio Review"
LABEL_SECTION_PREPARE_VERIFY = "📚 Prepare & Verify"

INFO_TAB_MEDIA_LIBRARY = (
    "**Everything about the footage and audio you already have.** Review what a cutter "
    "produced, file it into your library or your discard folder, and prepare or verify a "
    "library before a render reads it.\n\n"
    "Each section works on its own folder, so you can review a folder here without running "
    "a cutter first — and reviewing never moves a file until you confirm."
)

INFO_TAB_SETTINGS = (
    "**Preferences and maintenance.** Default folders are remembered between sessions and are "
    "only ever suggestions: nothing here is created, scanned or moved when settings load."
)

# ============================================================================
# [FORK] Digital-Union: R5 Clips Preview Selector (P1 — read-only review)
# Post-cut triage in its own tab: browse, play and classify the clips the Raw
# Cutter produced. P1 moves nothing; transfer is a separate authorized phase.
# ============================================================================

# [FORK] R8 UX Refresh (Phase 10): shortened from 606 characters. The Media Library tab's own
# intro already says what the three sections are for, so this no longer repeats it — and the
# "nothing moves until you confirm" promise, which is the one an operator needs before clicking,
# is kept.
INFO_TAB_CLIPS_PREVIEW = (
    "**Mark each clip keep or discard.** Left click keeps, right click discards, ↺ undoes. "
    "Decisions are recorded on the server, so a reload or a restart cannot change them — and "
    "**nothing moves until you press Confirm**."
)

LABEL_CLIPS_INPUT = "📂 Clips folder (the clips to review)"
INFO_CLIPS_INPUT = (
    "Read in place — nothing is uploaded, copied or modified. Point this at a Raw Cutter output "
    "folder, or any folder of clips. The enumeration order is the Raw Cutter's own, so the two "
    "always agree about what 'the files, in order' means."
)

LABEL_CLIPS_RECURSIVE = "📁 Include subfolders"
INFO_CLIPS_RECURSIVE = (
    "On by default, matching Folder Batch. A Raw Cutter batch writes one folder per source, so "
    "leaving this on is what finds the clips inside them."
)

# [FORK] R8 Owner Fixes (P2): "would go" is left over from when Confirm only reported a plan.
# It has moved files since UX V3, and a label in the conditional is the one place an operator
# looks to find out whether anything is at stake.
LABEL_CLIPS_LIBRARY = "✅ Media Library folder — kept clips are MOVED here"
INFO_CLIPS_LIBRARY = (
    "Validated when you scan, written when you confirm — keeping a clip moves the file, it does "
    "not copy it. It must not be inside the clips folder, must not contain it, "
    "and must not be the discard folder or a protected runtime directory — those are refused before "
    "anything is read, and the check follows junctions and symlinks rather than comparing the text "
    "you typed."
)

LABEL_CLIPS_REJECTED = "🗑️ Discarded folder — clips are MOVED here, never deleted"
INFO_CLIPS_REJECTED = (
    "Discarding is quarantine, not deletion: a discarded clip is moved here, intact and under its "
    "original name, for you "
    "to deal with outside this tool. There is no retention expiry and no permanent delete."
)

LABEL_CLIPS_PAGE_SIZE = "Clips per page"
INFO_CLIPS_PAGE_SIZE = (
    "100 is the measured default: a warm page of 100 becomes interactive in about half a second, "
    "with one video decoder at most, because the grid shows poster frames and only creates a "
    "player when you hover. Lower it if your clips are unusually large."
)

LABEL_CLIPS_CHECK = "🔎 Check folders"
LABEL_CLIPS_SCAN = "📂 Scan and build page"
LABEL_CLIPS_REFRESH = "🔁 Re-read the server's record"
# [FORK] UX V4: the label was left over from the phase when Confirm genuinely moved nothing. It
# now sorts, so a button that still said "moving is not enabled yet" told the operator the exact
# opposite of what pressing it does to their files -- the one place a stale label is dangerous
# rather than untidy.
LABEL_CLIPS_CONFIRM = "✅ Confirm — file this page into its folders"
LABEL_CLIPS_REPORT = "Scan report"
LABEL_CLIPS_COUNTS = "Decisions (server record)"
LABEL_CLIPS_CONFIRM_OUT = "Confirm"

CLIPS_REPORT_INITIAL_TEXT = "Press Check folders, then Scan."
CLIPS_COUNTS_INITIAL_TEXT = "Kept 0   ·   Discarded 0   ·   Undecided 0   ·   Total 0"
CLIPS_GALLERY_INITIAL_HTML = (
    '<div class="r5-empty">Nothing scanned yet. Choose a clips folder and press Scan.</div>'
)
# [FORK] R8 Final Polish: the old text described the pre-keep-only rule ("every clip on the page
# is decided"), which is no longer how Confirm is admitted. Video Review now matches Audio Review:
# mark the good clips, and the rest are handled through an explicit, batch-specific gate.
CLIPS_CONFIRM_DISABLED_TEXT = (
    "Mark at least one clip to Keep. Anything you leave undecided is offered for the Discarded "
    "folder in a second step that you confirm separately — nothing moves until then."
)

CLIPS_SWEEP_GATE_HEADING = (
    "⚠️  Some clips are still unmarked. Nothing has moved yet — here is what Confirm would do:"
)
CLIPS_SWEEP_GATE_PROMPT = (
    "Press Proceed to sort it exactly as listed, or Cancel to go back and mark more clips."
)
CLIPS_SWEEP_CANCELLED_TEXT = (
    "Cancelled. Nothing was moved and every decision is exactly as you left it."
)
# [FORK] R8 Final Polish: the honest counterpart to selecting a preset.
#
# Selecting `Balanced` resets the six base sliders and NOTHING else. It does not clear the
# Variation Seed, it does not disable Freestyle, and it does not touch Audio Mix. This action
# covers the other half, and equally does not touch the sliders -- the two are disjoint on
# purpose, so neither can be mistaken for "reset everything".
LABEL_CLEAR_VARIATIONS = "🧹 Clear creative variations"
INFO_CLEAR_VARIATIONS = (
    "Sets the Variation Seed back to 0 and turns Freestyle off. **Your six sliders and the "
    "preset are left alone**, and so is everything in Audio Mix — to reset the sliders, pick a "
    "preset above."
)

LABEL_CLIPS_PROCEED = "➡️ Proceed — sort it as listed"
LABEL_CLIPS_SWEEP_CANCEL = "↩️ Cancel — go back and mark more clips"
# [R5-P3 / F8] Classifying posts straight to the server rather than through a Gradio event, which
# is deliberate -- but it means a decision cannot re-enable the Confirm button by itself. Without
# this line the operator would see "Undecided 0" beside a greyed-out Confirm and no explanation.
# It is display text: it enables nothing, and only Refresh moves the button, from the durable
# record.
# [UX] The client now asks Refresh to run itself the moment the all-decided state changes, so the
# operator no longer has to press anything. The wording says what just happened rather than giving
# an instruction, because the old text told the operator to do a job the page already does. The
# mechanism is unchanged: this string is still display text, it still enables nothing, and the
# button is still moved only by the server reading the durable record.
# [UX V4] The native folder dialog and the remembered folder choices.
LABEL_BROWSE_FOLDER = "📁 Browse…"
INFO_BROWSE_FOLDER = (
    "Opens the normal Windows folder dialog on this computer. Cancelling leaves the path alone, "
    "and you can still type or paste a path yourself."
)
LABEL_PREF_SECTION = "⭐ Default folders"
INFO_PREF_SECTION = (
    "Filled in automatically each time BeatSync starts. Saving only remembers the paths — it "
    "never creates a folder, moves anything, or scans."
)
LABEL_PREF_RAW_OUTPUT = "Default Raw Cutter output"
LABEL_PREF_SAVE = "💾 Save defaults"
LABEL_PREF_RESET = "↺ Reset defaults"
LABEL_PREF_STATUS = "Default folders"
PREF_STATUS_INITIAL = (
    "Set the folders you use every session, then press Save defaults. Nothing is created or "
    "moved by saving."
)

# ---------------------------------------------------------------------------------------------
# [FORK] R8 UX Refresh: the Settings tab's new content.
# ---------------------------------------------------------------------------------------------

LABEL_PREF_RENDER_OUTPUT = "Render output"
INFO_PREF_RENDER_OUTPUT = (
    "Where finished music videos are published. Leave blank to use the `output` folder inside "
    "BeatSync itself. Only affects the final video — temporary working files and caches stay "
    "where they are."
)

# [FORK] R8 Final Polish: the three purpose groups the folder defaults are read in. Nine paths in
# one undifferentiated list is why this panel was hard to scan; the paths themselves, their keys
# and their saved values are unchanged.
LABEL_PREF_GROUP_VIDEO = "🎬 Render and video folders"
LABEL_PREF_GROUP_AUDIO = "🎙️ Audio and voice folders"
LABEL_PREF_GROUP_REVIEW = "📂 Review destinations"
INFO_PREF_GROUP_REVIEW = (
    "Where a review files what you decide. Keeping and discarding are both *moves* — nothing is "
    "ever deleted, and the original filename is preserved."
)

# [FORK] R8 Owner Fixes (P1): the four audio destinations, explained rather than renamed.
#
# The owner read them as duplicated. They are not duplicates -- the Audio Cutter files a fresh
# cut, Audio Review files a clip reviewed later, and those are genuinely different decisions --
# but four entries with near-identical names is a fair complaint. So the Cutter's pair is now
# presented as the ordinary Voice library / Discarded pair, Audio Review's pair is an OPTIONAL
# override in a collapsed panel, and Audio Review falls back to the Cutter's pair when its own
# is left blank.
#
# Nothing is consolidated: a stored Audio Review folder that differs keeps winning, and no save
# ever copies one key's value into another.
LABEL_PREF_VOICE_PAIR = "🎙️ Voice library and Discarded (used by both audio tabs)"
INFO_PREF_VOICE_PAIR = (
    "The two folders audio reviewing files into. **Audio Review uses these as well** unless you "
    "set it its own pair below."
)
LABEL_PREF_AUDIO_OVERRIDES = "Separate folders for Audio Review (optional)"
INFO_PREF_AUDIO_OVERRIDES = (
    "Leave both blank and Audio Review files into the Voice library and Discarded folders above "
    "— that is the usual setup. Fill one in only if reviewing a clip *later* should send it "
    "somewhere different from a fresh cut.\n\n"
    "Anything already saved here is kept exactly as it is: filling these in never changes the "
    "folders above, and the folders above never overwrite these."
)

#: [FORK] R8 Owner Fixes (P2): what the status box is actually describing. The owner saw it
#: naming old paths while new ones were visible in the fields, which is correct behaviour --
#: the box reports SAVED settings and the fields were edited but not yet saved -- and reads as a
#: contradiction unless it says so.
PREF_STATUS_SCOPE = (
    "(This describes your saved settings. Edits above take effect when you press Save defaults.)"
)

# [FORK] R8 UX Refresh (Commit C): the Media Library as a video source.
CHOICE_SOURCE_MEDIA_LIBRARY = "Use Media Library — the folder set in Settings"
STATUS_LIBRARY_SOURCE_UNSET = (
    "No default video Media Library is set yet. Open Settings → Default folders, set "
    "\"Media Library folder\", then pick this option again. Your current source folder was left "
    "as it was."
)
STATUS_LIBRARY_SOURCE_MISSING = (
    "The saved Media Library folder is not there any more: {path}. It was filled in anyway so "
    "you can correct it — nothing was created."
)

# [FORK] R8 UX Refresh (Commit C): the one truthful creative read-out on the Render tab.
LABEL_CREATIVE_PROFILE = "🎨 Current Creative Profile"
CREATIVE_PROFILE_PLACEHOLDER = (
    "The creative settings this render will use appear here."
)
CREATIVE_PROFILE_PLAIN_HINT = (
    "A plain preset render — nothing else is active."
)
CREATIVE_PROFILE_MODIFIED_HINT = (
    "Change any of this in Creative Settings."
)
LABEL_OPEN_CREATIVE_SETTINGS = "🎨 Open Creative Settings"

LABEL_COLLISION_SECTION = "📄 When a destination filename already exists"
CHOICE_COLLISION_SKIP = "Skip and report it"
CHOICE_COLLISION_SUFFIX = "Rename automatically (clip_01, clip_02, …)"
INFO_COLLISION_POLICY = (
    "Applies when filing reviewed clips into a Media Library or Discarded folder. An existing "
    "file is **never** overwritten under either setting."
)

STATUS_COLLISION_SAVED_SKIP = (
    "Saved: a clip whose name is already taken will be skipped and reported."
)
STATUS_COLLISION_SAVED_SUFFIX = (
    "Saved: a clip whose name is already taken will be renamed clip_01, clip_02, and so on. "
    "Nothing is ever overwritten."
)

LABEL_CACHE_SECTION = "🧹 Cache management"
INFO_CACHE_SECTION = (
    "Disposable files BeatSync creates while it works. Press Inspect to see what is there, then "
    "tick what you want removed.\n\n"
    "Only these four folders can be cleared. Your source media, finished videos, Media "
    "Libraries, Discarded folders, saved settings and review decisions are never touched."
)
LABEL_CACHE_POSTERS = "Preview thumbnails"
INFO_CACHE_POSTERS = "Video Review poster images. Rebuilt automatically on the next scan."
LABEL_CACHE_PROCESSING = "Temporary processing files"
INFO_CACHE_PROCESSING = "Clip fragments left over from finished renders."
LABEL_CACHE_UPLOADS = "Retired browser uploads"
INFO_CACHE_UPLOADS = (
    "Files earlier sessions copied in through the browser, set aside at startup instead of "
    "being deleted. Check these before clearing — for a file you dragged in, this copy may be "
    "the only one you still have a path to."
)
LABEL_CACHE_ANALYSIS = "Video analysis cache (Stage 5)"
INFO_CACHE_ANALYSIS = (
    "⚠️ Expensive to rebuild. Clearing this makes BeatSync re-run the full video and Qwen "
    "analysis the next time it reads those sources, which can take many minutes."
)
LABEL_CACHE_INSPECT = "🔍 Inspect cache"
LABEL_CACHE_SELECT = "Clear which caches?"
LABEL_CACHE_CONFIRM = "Yes, delete the selected caches"
INFO_CACHE_CONFIRM = "Clearing cannot be undone."
LABEL_CACHE_CLEAR = "🧹 Clear selected cache"
LABEL_CACHE_REPORT = "Cache"
PLACEHOLDER_CACHE_REPORT = (
    "Press Inspect cache to see how much space each category is using. Nothing is deleted until "
    "you select categories, tick the confirmation and press Clear."
)
#: [FORK] Digital-Union (FUN Shared Analysis Cache): the warning the Clear panel must carry once a
#: shared folder is in use. Clearing a *local* cache costs one installation some re-analysis;
#: clearing a *shared* one costs every installation on the machine, including the production
#: install the operator is not currently looking at. That is a materially different decision and
#: the panel has to say so before it is made, not after.
INFO_CACHE_ANALYSIS_SHARED = (
    "⚠️ This is a SHARED analysis cache at {path}. Other BeatSync installations on this computer "
    "read the same folder, so clearing it makes every one of them re-analyse from scratch — not "
    "just this one."
)

# =============================================================================================
# [FORK] Digital-Union (FUN Shared Analysis Cache): Settings -> Video Analysis Cache.
#
# The owner's second problem: every isolated playtest got its own cold cache, because the cache
# directory is derived from the checkout's own root. The analysis records were always portable --
# identity is the source file plus the AI backend, and the folder is not part of it -- so this
# panel does not migrate anything. It points several installations at one folder.
# =============================================================================================

LABEL_SHARED_CACHE_SECTION = "🗂️ Video analysis cache"
INFO_SHARED_CACHE_SECTION = (
    "Where BeatSync keeps what it learned about your source videos. Analysing a large library "
    "from scratch takes a long time, and by default each BeatSync version keeps its own copy — so "
    "a new version starts over.\n\n"
    "Point this at one folder and every BeatSync version you run as this Windows user will share "
    "it, including future ones. The setting is stored outside the program folder, so a new version "
    "finds it on its own with no setup."
)
LABEL_SHARED_CACHE_FOLDER = "Shared cache folder"
INFO_SHARED_CACHE_FOLDER = (
    "Pick a folder on a disk with room to spare, and leave it there. It does not have to be "
    "inside BeatSync."
)
LABEL_SHARED_CACHE_ENABLED = "Use the shared cache folder"
INFO_SHARED_CACHE_ENABLED = (
    "Off means each BeatSync version keeps its own cache, exactly as before."
)
LABEL_SHARED_CACHE_SAVE = "💾 Save cache setting"
LABEL_SHARED_CACHE_INSPECT = "🔍 Check cache folder"
LABEL_SHARED_CACHE_STATUS = "Cache location"
PLACEHOLDER_SHARED_CACHE_STATUS = (
    "Press Check cache folder to see where the analysis cache is and how much is in it."
)
#: Said plainly, because the limits are the part an operator will otherwise discover the hard way.
#: Every claim here is a property of the existing identity contract, not a new restriction:
#: identity is `abspath + size + mtime_ns + content fingerprint + AI backend + AI settings`.
INFO_SHARED_CACHE_LIMITS = (
    "**What is reused, and what is not.** A source video is recognised by its full path, its size, "
    "its modification time and a sample of its contents — so:\n\n"
    "- moving or renaming a video means it is analysed again, even though nothing about it "
    "changed;\n"
    "- editing or re-encoding a video means it is analysed again, which is the point;\n"
    "- analysis done with AI tagging on and analysis done with it off are kept separately, so "
    "neither is used in place of the other;\n"
    "- changing the AI model, or the three AI detail settings, starts a fresh set of records. The "
    "old ones are left alone, not deleted."
)
STATUS_SHARED_CACHE_SAVED_ON = (
    "✅ Saved. BeatSync will use the shared analysis cache at {path}.\n"
    "This takes effect on the next analysis — a render already running keeps the folder it "
    "started with."
)
STATUS_SHARED_CACHE_SAVED_OFF = (
    "✅ Saved. Sharing is off, so this BeatSync version will use its own analysis cache at "
    "{path}.\nThe shared folder was not changed or deleted."
)
STATUS_SHARED_CACHE_SAVE_NEEDS_FOLDER = (
    "Pick a folder first — nothing was saved. Sharing cannot be switched on without somewhere to "
    "share to."
)
STATUS_SHARED_CACHE_SAVE_FAILED = "❌ {reason}"
#: The one report both the Check button and the Save button render, so the screen cannot show two
#: different answers to "where is the cache?".
STATUS_SHARED_CACHE_REPORT = (
    "{kind} analysis cache\n"
    "  Folder:        {path}\n"
    "  Analysed videos: {records}\n"
    "  Records size:  {record_bytes}\n"
    "  Other files:   {others} ({other_bytes})  — retained AI worker logs, kept on purpose\n"
    "  Settings file: {config}"
)
STATUS_SHARED_CACHE_MISSING = (
    "{kind} analysis cache\n"
    "  Folder:        {path}\n"
    "  This folder does not exist yet. It is created the first time an analysis runs.\n"
    "  Settings file: {config}"
)
STATUS_SHARED_CACHE_UNREADABLE = (
    "{kind} analysis cache\n"
    "  Folder:        {path}\n"
    "  ⚠️ Could not be read: {error}\n"
    "  Nothing was created or changed.\n"
    "  Settings file: {config}"
)

LABEL_CACHE_IMPORT_SOURCE = "Import records from this folder"
INFO_CACHE_IMPORT_SOURCE = (
    r"An existing BeatSync version's cache folder, for example I:\BeatSync\input\video_analysis_cache. "
    "Copying is one-way and additive: nothing in the folder you copy FROM is changed, and a record "
    "already in the shared folder is kept as it is."
)
LABEL_CACHE_IMPORT = "📥 Import existing cache"
STATUS_CACHE_IMPORT_REFUSED = "Nothing was copied. {reason}"
STATUS_CACHE_IMPORT_DONE = (
    "📥 Import finished.\n"
    "  From: {source}\n"
    "  To:   {destination}\n"
    "  Copied:          {copied} ({copied_bytes})\n"
    "  Already there:   {already_present}  — kept exactly as they were\n"
    "  Not reusable:    {incompatible}  — written by a different analysis version\n"
    "  Failed:          {failed}\n"
    "  Skipped:         {skipped} other file(s) — media, logs and worker files are never copied\n"
    "Nothing was deleted from either folder."
)

CLIPS_REFRESH_TO_ENABLE_HINT = (
    "At least one clip kept — Confirm is being enabled from the server's record."
)
# [UX V3] Why a clip stayed where it was. Each of these means the original is untouched: the
# sorter only ever renames, so anything short of success leaves the file exactly as it was.
CLIPS_MOVE_REASONS = {
    "missing": "it is no longer where the scan found it",
    "changed": "it changed after it was scanned",
    "occupied": "that name is already taken in the destination",
    "cross_volume": "the destination is on a different drive (V1 moves within one drive only)",
    "in_use": "something still has the file open — close it and press Confirm again",
    "failed": "the move failed",
}
# [R13] The Working overlay. The first of these is shown BEFORE the first file moves, which is the
# whole point of the feature: a long transfer used to look like a dead button.
CLIPS_WORKING_STARTING_TEXT = (
    "⏳ Working — sorting {total} clip(s).\n"
    "Nothing has moved yet. Leave this tab open; Confirm is unavailable until it finishes."
)
# `{done}` is RECORDED outcomes, never a prediction and never a timer. `{moved}` is how many of
# them actually moved, which is why both numbers are shown rather than one standing in for the
# other -- a skipped clip is counted as done and was not moved.
CLIPS_WORKING_PROGRESS_TEXT = "⏳ Moving {done} / {total} clips — {moved} moved so far."
CLIPS_WORKING_PROGRESS_WITH_STAYED_TEXT = (
    "⏳ Moving {done} / {total} clips — {moved} moved, {stayed} left in place so far."
)
# The honest closing tally, appended to the existing per-clip report. `skipped` and `failed` are
# deliberately separate: a taken filename or a file somebody still has open is a refusal this code
# made on purpose, while a failure is an error it could not classify.
CLIPS_TRANSFER_TALLY_TEXT = "Moved {moved} · skipped {skipped} · failed {failed} · of {total}."
CLIPS_TRANSFER_BUSY_TEXT = (
    "⏳ Nothing was started: this page is already being sorted ({done} of {total} clip(s) done).\n"
    "Wait for it to finish — pressing Confirm again cannot make it go faster, and the server "
    "refuses a second run so that no clip is moved twice."
)
CLIPS_TRANSFER_FOLDER_BUSY_TEXT = (
    "⏳ Nothing was started: another review session is already sorting this folder "
    "({done} of {total} clip(s) done).\n"
    "Wait for it to finish. Two sessions sorting one folder is refused by the server, not just "
    "discouraged in the browser."
)
CLIPS_TRANSFER_BLOCKED_DECISION_TEXT = (
    "This page is being sorted right now, so its clips cannot be re-classified until it finishes."
)
# The transfer finished, but the page it was sorting is no longer the current one -- an operator
# re-scanned, or the Raw Cutter's "Review these clips" retired it (R11 / F-1). The counts below are
# still exactly what happened; what is NOT shown is a repaint of a page the server no longer holds.
CLIPS_TRANSFER_PAGE_MOVED_ON_TEXT = (
    "Video Review has moved on to a different page since this sort started, so the gallery below "
    "shows what the server holds now rather than the folder that was just sorted."
)
CLIPS_TRANSFER_STALE_NOTE_TEXT = (
    "\n\nThis page was replaced while the sort was running, so the grid above is not a repaint of "
    "it. Everything reported here really happened; press Scan Folder on a folder to review it "
    "again."
)
CLIPS_TRANSFER_CRASHED_TEXT = (
    "❌ The sort stopped early: {error}\n\n"
    "Everything reported above already happened and nothing was overwritten or deleted. The "
    "clips that did not move are still where they were, so pressing Confirm again is safe."
)
# [R5-P3 / F1] A scan that was overtaken by a newer scan of the same browser session. It changed
# nothing, and the gallery it is replacing shows the newest page rather than its own, whose
# session the newer scan already revoked.
CLIPS_SCAN_SUPERSEDED_TEXT = (
    "This scan was superseded by a newer one and was discarded.\n"
    "Nothing was changed. The gallery below shows the newest scan's page."
)
CLIPS_CONFIRM_READY_TEXT = (
    "Every clip on this page is decided.\n\n"
    "**Moving files is NOT enabled in this phase.** Nothing has been moved, renamed or deleted. "
    "The decisions are recorded durably on the server and will still be here when the transfer "
    "step is separately authorized and reviewed."
)

# ============================================================================
# [FORK] Digital-Union: Raw Cutter V1
# Split one long source video at its existing edits, to curate footage before
# it enters the media library. Independent of the render pipeline.
# ============================================================================

INFO_TAB_RAW_CUTTER = (
    "**Pull the usable shots out of one long video.** Point it at a single file, press Analyze to "
    "see which shots it would keep, then press Cut Video to write them into a new folder. "
    "It needs no audio track, no confirmed source set and no prepared library — it is a standalone "
    "footage-prep tool, and it never modifies the file you give it.\n\n"
    "**It keeps clean shots and skips the rest.** Material shorter than your minimum is left "
    "behind rather than glued to its neighbour, because a clip that cuts in the middle is not "
    "worth having in a source library. Gaps between the exported clips are normal and the plan "
    "tells you exactly how much was skipped and why."
)

LABEL_CUT_SOURCE = "🎬 Source video (full path to one file)"
INFO_CUT_SOURCE = (
    "The file is read in place — nothing is uploaded or copied, so a multi-gigabyte source costs "
    "nothing to point at. MP4, MKV, MOV, M4V, AVI and WebM are accepted."
)
PLACEHOLDER_CUT_SOURCE = r"J:\Footage\long_recording.mkv"

LABEL_CUT_SOURCE_INFO = "Source details"
PLACEHOLDER_CUT_SOURCE_INFO = "Press Analyze to probe the file."

LABEL_CUT_OUTPUT = "📁 Output folder"
INFO_CUT_OUTPUT = (
    "Each run creates its own fresh, timestamped subfolder here, so a second run can never "
    "overwrite the first. Existing files are never replaced."
)
PLACEHOLDER_CUT_OUTPUT = r"J:\Raw Parts"

LABEL_CUT_SENSITIVITY = "🔍 Scene detection sensitivity"
INFO_CUT_SENSITIVITY = (
    "How eager the detector is to call something a cut. Higher finds more scene changes, including "
    "softer ones like a fast pan or a lighting change; lower keeps only hard cuts. 50 is the same "
    "threshold the rest of BeatSync uses."
)

LABEL_CUT_MIN_SECONDS = "⏱️ Minimum clip length (seconds)"
INFO_CUT_MIN_SECONDS = (
    "The shortest shot worth keeping. A shot below this is **skipped entirely** — it is never "
    "glued to the shot next to it to make up the length, because that would produce a clip with "
    "a cut in the middle of it."
)

LABEL_CUT_MAX_SECONDS = "⏳ Maximum clip length (seconds)"
INFO_CUT_MAX_SECONDS = (
    "The longest single clip to write. A longer shot is divided into several clips — all from "
    "inside that same continuous take, so nothing ever spans a scene change."
)

LABEL_CUT_REMOVE_AUDIO = "🔇 Remove audio from the clips"
INFO_CUT_REMOVE_AUDIO = (
    "On by default: source clips for a music video do not need their own sound, and dropping the "
    "stream makes the files smaller. Turn it off to keep the original audio (re-encoded to AAC). "
    "A source with no audio works either way."
)

# [FORK] Digital-Union (Raw Cutter R4-C): synthetic output naming, opt-in and off by default.
# Shared between Single Video and Folder Batch, like every other execution setting in this tab.
LABEL_CUT_RENAME_OUTPUT = "🏷️ Rename output clips"
INFO_CUT_RENAME_OUTPUT = (
    "Off by default. Turn it on to name the clips File001_DDMMYYYY_001.mp4 and so on, with no "
    "part of your original filenames or folder names in any name the cutter creates. The date is "
    "the day the job started, fixed once, so a long run crossing midnight stays consistent. In "
    "Folder Batch the per-source folders become File001, File002, … and batch_manifest.json "
    "still records which original video each one came from."
)

LABEL_CUT_ANALYZE = "🔎 Analyze / Preview"
LABEL_CUT_RUN = "✂️ Cut Video"
LABEL_CUT_CANCEL = "⏹️ Cancel cutting"

LABEL_CUT_PLAN_SUMMARY = "Plan summary"
PLACEHOLDER_CUT_PLAN_SUMMARY = (
    "Analyze a video to see which shots it found, which ones it would export, and which it would "
    "skip for being too short."
)
LABEL_CUT_PLAN_TABLE = "Detected shots — kept and skipped"
LABEL_CUT_RUN_REPORT = "Cut report"
PLACEHOLDER_CUT_RUN_REPORT = "Nothing has been cut yet."

INFO_CUT_ACTIONS = (
    "Analyze never writes anything. Cut Video re-checks the file and your settings first and "
    "refuses a plan that no longer matches them."
)

CUT_NO_PLAN_TEXT = "Press Analyze / Preview first — there is no plan to cut yet."
CUT_STALE_PLAN_TEXT = (
    "The source file or the detection settings changed since this plan was made, so it no longer "
    "describes this video. Press Analyze / Preview again."
)
CUT_BUSY_TEXT = "A cut job is already running. Wait for it to finish, or press Cancel cutting."
CUT_RENDER_BUSY_TEXT = (
    "A music video render is running. The Raw Cutter waits rather than competing with it for the "
    "same drive — try again once the render has finished."
)
CUT_CANCELLED_TEXT = "Cutting cancelled. Clips already written were kept; the partial one was removed."
CUT_CANCEL_REQUESTED_TEXT = "Cancellation requested — finishing the current clip, then stopping."
CUT_NOTHING_RUNNING_TEXT = "No cut job is running."

CUT_NOTHING_TO_EXPORT_TEXT = (
    "This plan has no clips to export — nothing in the source is long enough at these settings. "
    "That is a real answer about the footage, not an error. Lower the minimum clip length, or "
    "lower the sensitivity so fewer scene changes are detected, then Analyze again."
)

#: Shown to a RENDER / VERIFY / SAMPLE caller that found the mutex held by a Raw Cutter job.
CUTTER_BUSY_MESSAGE = (
    "✂️ A Raw Cutter job is running. It is writing video to disk, so BeatSync is not starting "
    "anything else against the same drive until it finishes — wait for it, or cancel it on the "
    "Raw Cutter tab."
)

#: Shown to the CUT caller when one of the read-only media diagnostics holds the mutex.
CUT_MEDIA_BUSY_TEXT = (
    "A media check (Verify Sources or Sample Decode) is using the drive right now. It is short — "
    "try Cut Video again in a moment."
)

# ============================================================================
# [FORK] Digital-Union: Raw Cutter Batch V1
# One Start Batch press processes a whole folder with no further interaction.
# It shares the Raw Cutter tab and every execution setting with Single Video
# mode — there is exactly one sensitivity, minimum, maximum and Remove Audio
# control on the screen, not two.
# ============================================================================

LABEL_CUT_MODE = "How much do you want to cut?"
CUT_MODE_SINGLE = "Single Video"
CUT_MODE_BATCH = "Folder Batch"
INFO_CUT_MODE = (
    "**Single Video** is the original workflow: Analyze to preview the plan, then Cut Video. "
    "**Folder Batch** processes an entire folder automatically — one press, no per-file preview "
    "and no confirmation, which is the point of it."
)

LABEL_CUT_BATCH_FOLDER = "📂 Source folder (every video inside it)"
INFO_CUT_BATCH_FOLDER = (
    "Read in place — nothing is uploaded or copied. Every MP4, MKV, MOV, M4V, AVI and WebM found "
    "here is processed in turn, in a fixed order."
)
PLACEHOLDER_CUT_BATCH_FOLDER = r"J:\Footage\Camera A"

LABEL_CUT_BATCH_RECURSIVE = "📁 Include subfolders"
INFO_CUT_BATCH_RECURSIVE = (
    "On by default. The folder structure is mirrored in the output, so clips from different "
    "subfolders never land in the same place even when the filenames are identical."
)

LABEL_CUT_BATCH_COUNT = "🔢 Count files"
LABEL_CUT_BATCH_DISCOVERY = "Files found"
PLACEHOLDER_CUT_BATCH_DISCOVERY = (
    "Optional: press Count files to see how many videos are in the folder. It reads nothing but "
    "the file list."
)

LABEL_CUT_BATCH_START = "🚀 Start Batch"
LABEL_CUT_BATCH_CANCEL = "⏹️ Cancel Batch"

LABEL_CUT_BATCH_STATUS = "Batch progress"
PLACEHOLDER_CUT_BATCH_STATUS = "No batch is running."
LABEL_CUT_BATCH_SUMMARY = "Batch summary"
PLACEHOLDER_CUT_BATCH_SUMMARY = (
    "The final per-source report appears here, and a full machine-readable record is written into "
    "the batch folder."
)

INFO_CUT_BATCH_ACTIONS = (
    "Start Batch runs the whole folder without stopping to ask. One broken or empty video does not "
    "end the batch — it is recorded and the next one starts. Cancel Batch stops at the next safe "
    "point and keeps every finished clip."
)

#: There is deliberately no batch-specific "busy" text: a refused Start Batch goes through the same
#: audited four-way `_cut_busy_message()` table Single Video mode uses, so the refusal names the
#: real mutex holder (cutter / render / verify / sample) instead of guessing.
CUT_BATCH_NOTHING_RUNNING_TEXT = "No batch is running."
CUT_BATCH_CANCEL_REQUESTED_TEXT = (
    "Cancel requested — finishing the clip being written now, then stopping. No further source "
    "video will be started, and everything already written is kept."
)
CUT_BATCH_DONE_STATUS = "Batch finished. See the summary below."
CUT_BATCH_CANCELLED_STATUS = "Batch cancelled. Finished clips were kept — see the summary below."

# [FORK] Digital-Union (Raw Cutter progress P1): the live progress panel.
#
# Every string here describes work that is ACTUALLY happening or a count that was ACTUALLY
# recorded. There is deliberately no ETA and no synthetic percentage: the only determinate
# fraction the Raw Cutter can honestly report is published clips over planned clips, because that
# is the only quantity it measures. Scene detection is one opaque FFmpeg pass, so it is shown as a
# named phase with an indeterminate bar rather than a number that would be invented.
LABEL_CUT_PROGRESS = "Live progress"

CUT_PHASE_PROBE_TEXT = "Reading the source file…"
CUT_PHASE_DETECT_TEXT = "Detecting scene changes…"
CUT_PHASE_PLAN_TEXT = "Planning the shots…"
CUT_PHASE_ANALYZING_TITLE = "Analyzing"
CUT_PHASE_CUTTING_TITLE = "Cutting"
CUT_PHASE_STOPPING_TITLE = "Stopping"

#: Shown while an indeterminate phase runs, so an operator can tell "slow" from "stuck".
CUT_PROGRESS_INDETERMINATE_NOTE = "No percentage is shown because this step cannot be measured."

#: Shown while a single clip is encoding and the published count has not moved yet.
CUT_PROGRESS_ENCODING_NOTE = (
    "A long clip can hold the percentage still — the bar only moves when a clip is actually "
    "finished and written."
)

CUT_ANALYZE_DONE_NO_WORK_TEXT = (
    "Analysis finished, but this plan exports nothing, so Cut Video stays unavailable."
)
CUT_ANALYZE_FAILED_TITLE = "Analysis failed"
CUT_PLAN_STALE_TITLE = "Plan needs refreshing"
CUT_CUT_DONE_TITLE = "Cutting finished"
CUT_CUT_FAILED_TITLE = "Cutting failed"
CUT_CUT_REFUSED_TITLE = "Nothing was cut"
CUT_CANCEL_REQUESTED_TITLE = "Cancellation requested"
CUT_BATCH_RUNNING_TITLE = "Batch running"
CUT_BATCH_DONE_TITLE = "Batch finished"
CUT_BATCH_FAILED_TITLE = "Batch stopped"
CUT_BATCH_CANCELLED_TITLE = "Batch cancelled"
#: [FORK] Digital-Union (P19-R4): the batch ran to the end of its list, but some sources failed.
#: `raw_cutter_batch.terminal_status` has always distinguished this from a clean run; the panel
#: used to flatten both into the same green "Batch finished", which is the one reading an operator
#: must not be given — the per-source failures are in the summary and the manifest either way.
CUT_BATCH_PARTIAL_TITLE = "Batch finished with failures"
#: [FORK] Digital-Union (P19-R4): every source succeeded, but the machine-readable record did not
#: reach disk, so the run cannot be audited afterwards. The clips are real; the evidence is not.
CUT_BATCH_RECORD_LOST_TITLE = "Batch finished — record NOT saved"
#: A batch that was refused before it ever reached finalisation has no terminal class, and the
#: panel says exactly that rather than inventing one.
CUT_BATCH_NOT_RUN_TITLE = "Batch did not run"
CUT_SETTINGS_CHANGED_TEXT = (
    "A planning setting changed, so the previous plan no longer describes this source. "
    "Press Analyze / Preview again before cutting."
)

INFO_TAB_CREATIVE_SETTINGS = (
    "**Shape how the edit feels.** Everything here is optional — leave it all alone and you get "
    "the straightforward render the Render tab describes. Start with a Creative Preset, then "
    "adjust the six sliders if you want to; the preset is just a name for those six numbers, so "
    "moving any slider reads back as *Custom*."
)

INFO_TAB_AUDIO_MIX = (
    "**Layer voice and sound effects over the music.** Optional — a render with none of this set "
    "uses the music track alone.\n\n"
    "*Library preparation and source checks moved to Media Library → Prepare & Verify.*"
)

# [FORK] R8 UX Refresh (Commit C): `LABEL_ACTIVE_SETTINGS` / `ACTIVE_SETTINGS_PLACEHOLDER` are
# deleted rather than renamed. The box they titled listed optional extras but said nothing about
# the six creative controls, because the Creative Preset radio sat on the same tab and stated that
# part itself -- and the pair was misleading together, since the selector could read `Balanced`
# while Freestyle, a Variation Seed and SFX were all armed. One resolved line replaces both; see
# `LABEL_CREATIVE_PROFILE` above and `beatsync_fork/effective_profile.py`.

LABEL_FREESTYLE_SECTION_PREFIX = "🎵"

LABEL_FREESTYLE_SUMMARY = "Freestyle rules"

# [FORK] Digital-Union (Random Freestyle V2)
LABEL_SURPRISE_ME = "🎲 Surprise me"
INFO_SURPRISE_ME = (
    "**Every section type starts on *Random*.** Random generates that section's five controls "
    "— Cut Density, Semantic Emphasis, Energy Response, Motion Bias and Source Diversity — from "
    "the Variation Seed, inside a band that suits the section: calmer intros and outros, harder "
    "drops and finales.\n\n"
    "**Surprise me rolls a new Variation Seed** and shows what the Random sections become. Because "
    "the seed is global, it also changes the overall variation of the edit — that is the same one "
    "number the Randomize button above rolls, and the same one the success panel prints, so any "
    "render stays reproducible from it.\n\n"
    "**Anything you set yourself is left alone.** A section switched to Base or to a named style is "
    "your choice and Surprise me never touches it — only sections still showing *Random* are "
    "regenerated. This is seeded generation, not an AI model: no inference runs and nothing is "
    "sent anywhere."
)

# ============================================================================
# [FORK] Digital-Union: Variant Lab V1 (C2)
# See src/beatsync_fork/variant_lab.py for the resolvers these labels describe. Variant Lab writes
# the Variation Seed, the six sliders and (E2, opt-in) the three audio levels — and nothing else.
# It never renders, and it never re-analyses your videos.
# ============================================================================

LABEL_VARIANT_LAB = "🧪 Variant Lab"
# [FORK] R8 Owner Fixes (P2): the lead sentence and the help panel.
#
# The lab carried roughly 3.9 KB of permanently visible explanation -- six Markdown blocks plus
# two long `info=` strings -- above controls that are themselves advanced. Every word of it is
# still available; it is now one click away instead of always on screen, and `INFO_VARIANT_LAB`
# and friends are reused verbatim inside the panel rather than rewritten, so there is no second
# version of the explanation to drift.
LABEL_VARIANT_HELP = "ℹ️ How Variant Lab works"
INFO_VARIANT_LAB_LEAD = (
    "Generate recipes near your current six sliders, compare them, and apply one. "
    "**Nothing renders and no setting changes until you press Apply.**"
)
#: A short `info=` for the Master Seed. The full explanation -- what makes a draw repeat, and why
#: it is not the Variation Seed -- moved into the help panel.
INFO_MASTER_SEED_SHORT = "Repeats a draw. 0 means a fresh random one each time."

INFO_VARIANT_LAB = (
    "Explore beyond the seed. Tick the controls that may vary, give each one an allowed range, "
    "choose how far from your current settings to wander, and generate one recipe. "
    "**Your current six sliders above are the starting point**, whatever preset they came from — "
    "and generating writes the result back into them, so the next Generate starts from the new "
    "values. Generating moves the Variation Seed, those sliders and — only if you tick something "
    "under Audio variation — the three audio levels: it never starts a render, never re-analyses "
    "your videos, and never affects your confirmed source files."
)

LABEL_MASTER_SEED = "🧬 Master Creative Seed"
INFO_MASTER_SEED = (
    "The seed a recipe is generated from — not the same thing as the Variation Seed above, which "
    "Variant Lab fills in for you. A Master Seed repeats a draw only when the starting slider "
    "values and the Lab settings below (ranges, ticked controls, Spread) are the same. Because "
    "Generate overwrites the sliders, pressing it twice in a row gives two different recipes: to "
    "replay an old Master Seed, set the starting preset or values back first, and put the Lab "
    "settings back the way they were. What a draw produces is written into widgets you can see — "
    "the Variation Seed, the six sliders, and the three audio levels when you tick them under "
    "Audio variation — and it is those widgets, never the Master Seed, that the render reads. "
    "Everything else is still yours and is never generated: your source videos, voice clips, "
    "voice timing, Avoid drops, the SFX folder and the enabled SFX roles. "
    "Leave this at 0 and a fresh one is created and shown here."
)

LABEL_VARIATION_SPREAD = "🎚️ Variation Spread"
INFO_VARIATION_SPREAD = (
    "Conservative ↔ Crazy. How far a generated recipe may wander from your current sliders — not "
    "how high the values go: each control moves up or down independently, so a wild recipe might "
    "be dense cuts with calm footage. 0 keeps the six controls exactly where they are, but you "
    "still get a fresh Variation Seed, so the clip choices change. The same Spread also drives "
    "Audio variation below, where 0 genuinely changes nothing — there is no audio equivalent of "
    "the Variation Seed."
)

LABEL_VARIANT_RANDOMIZE = "🎯 Controls that may vary"
INFO_VARIANT_RANDOMIZE = (
    "Unticked controls are left exactly as they are, and their range below is ignored."
)

INFO_VARIANT_RANGES = (
    "**Allowed ranges** — a generated value for a ticked control always lands inside its range. "
    "These stay put when you change preset or move a slider; if your current value falls outside a "
    "range, variation is centred on the nearest edge instead. A min above its max is read as a "
    "fixed value, never silently swapped."
)

# [FORK] Digital-Union (Variant Lab Audio / E2 V1): the audio subsection of the existing Variant
# Lab. Three controls only, and the copy is explicit about what is deliberately NOT here — a user
# who expects the voice timing or the SFX roles to vary should learn that from the UI, not from a
# render that surprised them.
INFO_VARIANT_AUDIO = (
    "**Audio variation** — optional, and off until you tick something. The same Master Seed and "
    "Variation Spread above drive these, and generating writes the result into the Audio Layers and "
    "Smart Mix sliders. Only these three levels can vary: your voice clips, SFX folder, enabled SFX "
    "roles, voice timing and *Avoid drops* are always left exactly as you set them."
)

LABEL_VARIANT_AUDIO_RANDOMIZE = "🔊 Audio levels that may vary"
INFO_VARIANT_AUDIO_RANDOMIZE = (
    "Nothing is ticked by default, so audio is untouched until you ask for it. Unticked levels are "
    "left exactly as they are, and their range below is ignored."
)

INFO_VARIANT_AUDIO_RANGES = (
    "**Allowed audio ranges** — read exactly like the ranges above: a ticked level always lands "
    "inside its range, a value outside its range is centred on the nearest edge, and a min above "
    "its max is read as a fixed value rather than silently swapped."
)

LABEL_GENERATE_VARIANT = "✨ Generate Variant"
LABEL_NEW_VARIANT = "🎲 New Variant"

LABEL_VARIANT_REPORT = "Last generated recipe"
PLACEHOLDER_VARIANT_REPORT = (
    "No variant generated yet. Set a Variation Spread and press Generate Variant."
)

# ============================================================================
# [FORK] Digital-Union: Variant Lab C3 V1 — compare several candidates, apply one
# See src/beatsync_fork/variant_batch.py. Generating candidates produces *settings*, never videos:
# the copy below has to make that unmistakable, because "Generate Variants" is exactly the phrase a
# user would expect to produce several clips. Create Music Video remains the only render action.
# ============================================================================

INFO_VARIANT_COMPARE = (
    "**Compare several candidates** — generate a few complete settings at once, look at them side "
    "by side, then write one into the controls above. These are *settings*, not videos: nothing "
    "here renders, analyses or touches your source files. Every candidate starts from the same "
    "current values, so they are alternatives to each other rather than a drift in one direction."
)

LABEL_CANDIDATE_COUNT = "🔢 Candidates"
INFO_CANDIDATE_COUNT = (
    "How many candidates one press generates, from 2 to 12. They are free to make — the limit is "
    "just how many you can usefully read at once."
)

LABEL_GENERATE_VARIANTS = "🧮 Generate Variants"
LABEL_APPLY_VARIANT = "⬅️ Apply Selected Variant"

LABEL_VARIANT_BATCH_TABLE = "Last generated batch"
PLACEHOLDER_VARIANT_BATCH_TABLE = (
    "No candidates yet. Press Generate Variants to create several at once and compare them here."
)

LABEL_VARIANT_CANDIDATE = "🎯 Selected candidate"
INFO_VARIANT_CANDIDATE = (
    "Pick a row from the table above. Nothing changes until you press Apply Selected Variant."
)

LABEL_VARIANT_BATCH_STATUS = "Batch status"
PLACEHOLDER_VARIANT_BATCH_STATUS = (
    "Generate Variants creates candidates. Apply Selected Variant writes one into the controls. "
    "Create Music Video is still the only thing that renders."
)

# ============================================================================
# [FORK] Digital-Union: C3-R0 → C3-R1B-b — render 2 to 4 compared candidates
# See src/beatsync_fork/render_batch.py. This copy has three jobs: make clear that rendering is a
# real commitment of up to FOUR renders (Cancel exists since C3-R1A, see below — it is NOT
# instant); make clear that it uses the *stored* candidate settings rather than whatever the
# Variant Lab controls happen to say now; and, since C3-R1B-b, explain in the user's own terms
# which failures let the batch carry on and which stop it. Deliberately phrased as *what happens*
# — a user does not need the words CANDIDATE_LOCAL or SHARED_FATAL to understand "specific to that
# candidate" versus "affects every candidate".
# ============================================================================

LABEL_RENDER_CANDIDATES = "🎬 Candidates to render"
INFO_RENDER_CANDIDATES = (
    "Tick 2 to 4 candidates from the list above. Nothing happens until you press Render "
    "Selected Variants."
)

LABEL_RENDER_SELECTED = "🎞️ Render Selected Variants"

LABEL_RENDER_BATCH_SUMMARY = "Render batch result"
PLACEHOLDER_RENDER_BATCH_SUMMARY = (
    "No batch rendered yet. Tick 2 to 4 candidates above and press Render Selected Variants."
)

INFO_RENDER_SELECTED = (
    "**This makes 2 to 4 real videos**, one after the other — it is the only button here that "
    "renders anything other than Create Music Video. Tick between two and four candidates; they "
    "render in list order, not the order you ticked them. Each candidate uses the settings it was "
    "generated with, so editing the Variant Lab controls afterwards does not change what gets "
    "rendered. Everything that is *not* a candidate setting — your audio, voice clips, SFX "
    "folder, source videos, output name, encoder and FPS — is taken as it stands the moment you "
    "press the button, and later edits do not affect the batch already running.\n\n"
    "**Four videos is four times the wait**, so pick the number you actually want to watch.\n\n"
    "**If one candidate fails, the rest may still run.** When the problem is specific to that "
    "candidate's own settings — say its SFX amount, or an output name already taken — that one is "
    "recorded as failed and the batch moves on to the next. When the problem affects every "
    "candidate equally — a missing source video, an unusable voice clip, a full disk — the batch "
    "stops and the remaining candidates are not attempted. Either way, every video that already "
    "finished is yours to keep, and the summary says exactly what happened to each candidate.\n\n"
    "**Cancel Active Render** can stop this batch, but not instantly: video encoding stops within "
    "moments, while an in-progress AI scene-analysis step is never interrupted mid-call and finishes "
    "naturally first. Cancelling stops at the next safe point — whatever has already durably "
    "rendered is kept, and no further candidate is started, including after a candidate that just "
    "failed. Each file is named for its candidate, so none can overwrite another or anything "
    "already in your output folder. The comparison list is not used up — you can still apply a "
    "candidate, or render the same selection again."
)

LABEL_CANCEL_RENDER = "⏹️ Cancel Active Render"
# [FORK] R8 UX Refresh (Phase 10): shortened from 530 characters.
#
# The original was accurate and sat permanently on the Render tab, where it was the single
# largest block of text on the screen -- taller than the Create Music Video button it sat under.
# Everything it said is still true and still worth saying; it is said in one line, with the
# qualifications that only matter once Cancel has been pressed moved into the status panel's own
# wording, which is where someone actually reads them.
# [FORK] Digital-Union (FUN Stage-5 Cancel): both strings below were CORRECTED, not just reworded.
# They promised that "an AI scene-analysis step finishes its current call first", which was an
# honest description of `BOUNDARY_ONLY_CANCEL` and is now false: video analysis stops during the
# pass. Leaving the old copy in place would be the worse kind of stale text — it would tell the
# owner to expect the exact wait the fix removed.
#
# What is still true and still said: it is not instant. A native decoder call cannot be interrupted
# once entered, so cancellation lands at the next frame, window or source boundary — moments, not
# the rest of the library.
INFO_CANCEL_RENDER = (
    "Stops the render in progress. Not instant, but close: encoding and video analysis both stop "
    "within moments. Anything already finished is kept, including source videos that finished "
    "analysing."
)
STATUS_CANCEL_REQUESTED = (
    "⏹️ Cancel requested — stopping at the next safe point. Encoding and video analysis both stop "
    "within moments; source videos that already finished analysing keep their cache entries."
)
STATUS_CANCEL_NOTHING_ACTIVE = "Nothing is currently rendering."

# [FORK] Digital-Union (Deliverable Export V1-B-S2): open the output folder.
#
# The product owner rejected a browser download in favour of this. The distinction the copy has to
# carry is that this opens Explorer on the machine **running** BeatSync, which for a local
# loopback-only app is the same machine the browser is on — but saying "your" folder without
# qualification would be a lie if the page were ever opened from another device.
LABEL_OPEN_OUTPUT_FOLDER = "📂 Open Output Folder"
# [FORK] R8 UX Refresh (Phase 10): shortened from 501 characters, keeping every claim a test
# pins -- it still says WHOSE machine opens ("running BeatSync"), still names Explorer, and still
# promises no download. The master/share distinction it used to explain here is stated by the
# render's own status panel, which names whichever files it actually wrote.
INFO_OPEN_OUTPUT_FOLDER = (
    "Opens the folder your finished videos go to, in Windows File Explorer on the computer "
    "running BeatSync. Works before your first render."
)
#: Deliberately **not** "Explorer opened". `os.startfile` hands the path to the Windows shell and
#: returns immediately; it reports that the request was accepted, not that a window appeared. Only
#: a human looking at the desktop can confirm the latter, so the copy promises exactly what was
#: measured and no more.
STATUS_OPEN_FOLDER_REQUESTED = "📂 Asked Windows to open the output folder: {path}"
STATUS_OPEN_FOLDER_MISSING = (
    "The output folder does not exist yet: {path}. It is created when BeatSync starts and when a "
    "render saves its first file — nothing was created or changed just now."
)
STATUS_OPEN_FOLDER_UNSUPPORTED = (
    "Opening a folder is a Windows-only action and this system does not support it. The output "
    "folder is still {path}."
)
STATUS_OPEN_FOLDER_FAILED = (
    "Windows could not open the output folder {path} ({reason}). Nothing was created, moved or "
    "deleted, and any finished render is still saved there."
)

#: The status one cancelled render attempt reports. Deliberately **neutral about candidates**: the
#: pipeline function that emits it (`gui._process_video_impl`) is shared by an ordinary Create Music
#: Video click, which has no candidates at all, and by one candidate of a C3 batch. Naming a
#: candidate here would be wrong for the single render — and would also make a render-path function
#: reference Variant Lab's vocabulary, which `tests/test_variant_lab.py` forbids on purpose, because
#: that is how batch state would start leaking toward the renderer. The batch wrapper adds its own
#: "Rendering candidate N / 2" prefix above this line, which is where that context belongs.
STATUS_RENDER_CANCELLED = "⏹️ Cancelled. Nothing was rendered."

#: [FORK] Digital-Union (FUN Stage-5 Cancel): the same cancellation, when it happened during the
#: source-video analysis pass.
#:
#: It is a separate string because the two situations leave the user in different positions, and a
#: real incident proved it: an entire library was selected by accident against a cold cache, and the
#: generic "Nothing was rendered" would not have told the owner that the hour of work they had just
#: stopped was *analysis*, nor that the part of it that finished is kept and will not be repeated.
#: Both of those are the facts that decide what they do next.
#:
#: It is selected from the exception's structured `phase` field, never by inspecting this text or
#: the exception's message.
STATUS_RENDER_CANCELLED_ANALYSIS = (
    "⏹️ Render cancelled during video analysis. Nothing was rendered.\n"
    "Source videos that finished analysing keep their cache entries, so they will not be "
    "analysed again. The rest were left untouched."
)

INFO_VARIANT_APPLY = (
    "**Apply Selected Variant** writes that candidate's Variation Seed, six sliders and three "
    "audio levels into the controls above, exactly as a single Generate Variant would — then you "
    "press Create Music Video yourself. Applying uses up the list, because it moves the starting "
    "values the candidates were measured against; generate again to explore from where you landed. "
    "If you edit any setting after generating, Apply refuses rather than writing a candidate that "
    "no longer describes your screen.\n\n"
    "During generation the Master Seed acts as the **batch root**, and each candidate gets its own "
    "derived master, shown in the table. Applying puts the chosen candidate's master in the box. "
    "Neither number on its own brings a candidate back: you need the same starting values, ranges, "
    "ticked controls and Spread as well."
)

# ============================================================================
# [FORK] Digital-Union: video-source mode + confirmation gate labels
# See src/beatsync_fork/input_session.py for the behaviour these labels describe.
# ============================================================================

LABEL_SOURCE_MODE = "🎬 Video Source"
CHOICE_SOURCE_LOCAL_FOLDER = "Local folder — recommended for large libraries"
CHOICE_SOURCE_BROWSER_FILES = "Browser files"
INFO_SOURCE_MODE = (
    "Local folder reads your files in place — nothing is copied, and the counts are exact. "
    "Browser files uploads copies into the app's temp folder."
)

LABEL_SOURCE_FOLDER = "📂 Source Folder"
PLACEHOLDER_SOURCE_FOLDER = r"J:\New folder\Cuts"
INFO_SOURCE_FOLDER = "Local path to the folder holding your .mp4 / .mkv source videos."

LABEL_SOURCE_RECURSIVE = "Include subfolders"
LABEL_SCAN_FOLDER = "🔍 Scan Folder"
LABEL_SOURCE_REPORT = "📋 Source Status"
LABEL_CONFIRM_SOURCES = "✅ Confirm source files"

# [FORK] R8 Owner Fixes (P2): the permanent instruction is gone.
#
# It read "Create Music Video stays disabled until you confirm the source list" and was printed
# unconditionally -- including while the list WAS confirmed and Stage 6 was actively rendering,
# where it is simply false. The fact it carried is still worth stating, so it is stated as a
# property of the gate rather than as an instruction about the button's current state, and the
# live, changing half is `confirm_status`, which already tracks the real source state.
#
# The gate itself is untouched: confirmation authority and render admission are unchanged.
INFO_CONFIRMATION_GATE = (
    "Source confirmation covers the actual files, not just the count, and is re-checked before "
    "rendering."
)

# ============================================================================
# [FORK] Digital-Union (P V1 / P2): media library preparation labels
# See src/beatsync_fork/library_prep.py for the workflow these labels describe.
#
# P2: the persisted Stage-5 semantics are media-neutral, so preparation needs no track and applies
# to no particular edit style. Nothing here may imply otherwise.
# ============================================================================

LABEL_PREP_SECTION = "📚 Media Library Preparation"
INFO_PREP_SECTION = (
    "Analyze new or changed videos ahead of rendering so future renders can reuse the prepared "
    "visual-analysis cache. The analysis describes the video itself, so one preparation serves every "
    "track and every edit style, and no audio is needed here. Nothing is rendered. This is separate "
    "from the Video Source selection above."
)

LABEL_PREP_FOLDER = "📂 Library Folder"
INFO_PREP_FOLDER = (
    "Local path to your persistent video library. Browser uploads are not supported here: they "
    "live in temporary folders that are cleared on restart, so preparing them would be pointless."
)
LABEL_PREP_RECURSIVE = "Include subfolders"
LABEL_PREP_BATCH_SIZE = "Analyze batch size"
INFO_PREP_BATCH_SIZE = (
    "How many outstanding videos one Analyze click submits. Scanning always classifies the whole "
    "library; this only bounds how much work goes to a single analysis run, so an interruption "
    "costs at most one batch. Scan again after each batch to continue. Changing this does not "
    "require a new scan."
)
LABEL_PREP_SCAN = "🔍 Scan Library"
LABEL_PREP_ANALYZE = "⚙️ Analyze New / Changed"
LABEL_PREP_REPORT = "📋 Preparation Status"

# ----------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source verification.
# ----------------------------------------------------------------------------
# Deliberately worded so that a metadata read can never be mistaken for a decode guarantee. The
# button says "Verify Sources", not "Check for corruption": it reads container headers, and a file
# with an intact header and a shredded middle passes. The report repeats that limit in full.
LABEL_PREP_VERIFY = "🩺 Verify Sources"
LABEL_PREP_VERIFY_REPORT = "🩺 Source Verification (read-only)"
INFO_PREP_VERIFY = (
    "Read-only check of the library folder above. It answers two separate questions quickly: which "
    "sources still have a reusable analysis cache, and which have basic media problems. It reads "
    "metadata only — it does not decode every frame, so it cannot prove a video is fully intact. "
    "Nothing is analyzed, cached, repaired, re-rendered or modified."
)
PREP_VERIFY_INITIAL_TEXT = (
    "Press Verify Sources to check the library folder above.\n"
    "Read-only: no analysis, no cache changes, no rendering, no file modification."
)
#: Shown when the folder or the subfolder setting changes. The previous report described a
#: different library snapshot, so it is cleared rather than left on screen looking current.
PREP_VERIFY_STALE_TEXT = (
    "The library folder or subfolder setting changed; the previous verification no longer applies.\n"
    "Press Verify Sources again to check the current folder."
)
PREP_VERIFY_NO_FOLDER_TEXT = (
    "No library folder selected. Enter a folder path above, then press Verify Sources."
)
#: Verification and rendering both hammer the same source drive, so they are mutually exclusive.
#: This is the refusal the operator sees; it never cancels or disturbs the running render.
PREP_VERIFY_RENDER_BUSY_TEXT = (
    "⏳ A render is running. Source verification reads the same drive the render is reading from, "
    "so it will not start while a render is active — that competition is what made clip extraction "
    "time out in the first place. Wait for the render to finish, then press Verify Sources again."
)
#: The mirror image: shown by the render path when verification holds the shared mutex.
#
#: [FORK] R3 reworded: it previously said renders "wait for it", which was never what happened —
#: the render path refuses on a non-blocking acquire and does not queue behind verification. A
#: message that promises waiting when the request was actually declined is the same class of
#: inaccuracy R3 removed from the Verify side.
VERIFY_BUSY_MESSAGE = (
    "⏳ Source verification is running. It reads the same drive a render reads from, so this render "
    "was not started — verification is bounded and read-only, so it will not be long. Press Create "
    "Music Video again once it finishes."
)
#: [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): the Verify-side refusal when the mutex is held by
#: another VERIFICATION rather than by a render. Telling the operator a render is running when one
#: is not is a wrong answer, and the two situations need different advice.
PREP_VERIFY_ALREADY_RUNNING_TEXT = (
    "⏳ A source verification is already running. Only one runs at a time, because they read the "
    "same drive. Nothing was started or cancelled — wait for the running check to finish, then "
    "press Verify Sources again."
)

# ----------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the one-video sample-decode diagnostic.
#
# Worded to keep S2-B's question distinct from S2-A's. S2-A reads container metadata; this asks
# FFmpeg to actually produce frames. The phrasing says "up to 30 OUTPUT frames" on purpose:
# `-frames:v` bounds what is written out, while decoder pre-roll is unbounded and invisible, so
# "30 frames were decoded" would be a claim the diagnostic cannot make. And it says what it is NOT
# proof of, because a clean short sample is exactly the result most likely to be over-read.
LABEL_PREP_SAMPLE = "🎞️ Sample Decode — Check One Video"
LABEL_PREP_SAMPLE_PATH = "Video file to sample"
LABEL_PREP_SAMPLE_SEEK = "Position to sample (seconds)"
#: [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2, R1 MINOR-3): the sample field takes ONE FILE, so it
#: needs its own example. It previously borrowed `PLACEHOLDER_SOURCE_FOLDER`, which shows a
#: directory with no filename and no extension -- an operator following it would enter a folder and
#: get `SAMPLE_INVALID_REQUEST`, while the field's own label says "Video file to sample". A
#: placeholder is an illustrative path only: it is not a default value, is never submitted, and
#: promises nothing about the file existing.
PLACEHOLDER_SAMPLE_VIDEO = r"J:\New folder\Cuts\clip.mp4"
LABEL_PREP_SAMPLE_REPORT = "🎞️ Sample Decode (read-only)"
INFO_PREP_SAMPLE = (
    "Ask FFmpeg to produce up to 30 output frames at the chosen position in ONE video. The output "
    "is discarded. This does not verify the entire video or its renderability — it answers a "
    "different question from Verify Sources, which reads metadata only. Nothing is analyzed, "
    "cached, repaired, re-rendered or modified."
)
PREP_SAMPLE_INITIAL_TEXT = (
    "Enter one .mp4 or .mkv file above, choose a position, then press Sample Decode.\n"
    "Read-only: no analysis, no cache changes, no rendering, no file modification."
)
#: Shown when the file or the position changes. The previous report described a different sample,
#: so it is cleared rather than left on screen looking current.
PREP_SAMPLE_STALE_TEXT = (
    "The video file or sample position changed; the previous sample decode no longer applies.\n"
    "Press Sample Decode again to check the current selection."
)
PREP_SAMPLE_NO_PATH_TEXT = (
    "No video file selected. Enter the full path to one .mp4 or .mkv file above, then press "
    "Sample Decode."
)
#: The sample decode reads the same drive a render reads from, so it is refused rather than queued.
PREP_SAMPLE_RENDER_BUSY_TEXT = (
    "⏳ A render is running. A sample decode reads the same drive the render is reading from, so it "
    "was not started. Nothing was cancelled — wait for the render to finish, then press Sample "
    "Decode again."
)
PREP_SAMPLE_VERIFY_BUSY_TEXT = (
    "⏳ A source verification is running. It reads the same drive, so this sample decode was not "
    "started. Nothing was cancelled — wait for the verification to finish, then press Sample "
    "Decode again."
)
PREP_SAMPLE_ALREADY_RUNNING_TEXT = (
    "⏳ A sample decode is already running. Only one runs at a time, because they read the same "
    "drive. Nothing was started or cancelled — wait for the running sample to finish, then press "
    "Sample Decode again."
)
#: The render-side refusal when a SAMPLE DECODE holds the shared mutex. A third holder means the
#: render path can no longer say "another render is running" by elimination.
SAMPLE_BUSY_MESSAGE = (
    "⏳ A sample decode is running. It reads the same drive a render reads from, so this render was "
    "not started — the sample is bounded and read-only, so it will not be long. Press Create Music "
    "Video again once it finishes."
)
#: The verify-side refusal when a SAMPLE DECODE holds the mutex.
PREP_VERIFY_SAMPLE_BUSY_TEXT = (
    "⏳ A sample decode is running. It reads the same drive, so source verification was not "
    "started. Nothing was cancelled — wait for the sample to finish, then press Verify Sources "
    "again."
)
#: [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the NEUTRAL refusal, for the one window where the
#: mutex is provably held but the diagnostic holder token has not been published yet.
#
# `acquire()` succeeding and `_mark_lock_holder()` running are two statements, so a caller that
# fails the acquire in between reads an empty token while the lock is genuinely held. The previous
# boolean hid that window by attributing it to a render BY ELIMINATION, which is exactly the
# inaccuracy a third holder makes untenable. An unknown holder now says so instead of naming the
# wrong operation: the refusal is still truthful and still terminal, it just declines to guess.
MEDIA_BUSY_UNKNOWN_TEXT = (
    "⏳ A bounded media operation is already running. Only one runs at a time, because they read "
    "the same drive. Nothing was started or cancelled — wait for it to finish, then try again."
)

def get_gpu_status_info(gpu_available, gpu_info, nvenc_available):
    """GPU status info."""
    if gpu_available and nvenc_available:
        return f'✅ {gpu_info} | NVENC Enabled'
    elif gpu_available:
        return f'✅ {gpu_info} | NVENC Not available'
    else:
        return '❌ CPU mode only'

def get_processing_mode_info_nvenc():
    """Processing mode info with NVENC."""
    return 'GPU (NVENC): High quality | CPU: High quality | ProRes: Max quality'

def get_processing_mode_info_cpu():
    """Processing mode info without NVENC."""
    return 'CPU: H.264 encoding | ProRes: Max quality (NVENC not available)'

def get_parallel_workers_label(recommended_workers):
    """Parallel workers label."""
    return f'⚡ Parallel Workers (Recommended: {recommended_workers})'

def get_parallel_workers_info():
    """Parallel workers info."""
    return 'Clips processed simultaneously. More workers with GPU.'

# [FORK] Digital-Union (R6): Raw Cutter output video profiles.
LABEL_CUT_OUTPUT_PROFILE = "Output video profile"
INFO_CUT_OUTPUT_PROFILE = (
    "Same as source exports every clip exactly as the Raw Cutter always has — the source's own "
    "resolution and frame timing, untouched. A fixed profile re-encodes each exported clip to "
    "one size and one constant frame rate, padding rather than stretching so nothing is "
    "distorted. Converting to a fixed rate duplicates or drops frames to reach it; the clip "
    "plays at normal speed either way. It changes only the exported file — never where a cut is "
    "placed. One profile applies to a whole Folder Batch."
)

# =============================================================================================
# [FORK] Digital-Union (R7): Raw Audio Cutter V1 — split one recording on its silences.
# =============================================================================================

LABEL_TAB_AUDIO_CUTTER = "🎙️ Audio Cutter"

INFO_TAB_AUDIO_CUTTER = (
    "**Pull reusable audio clips out of one recording.** Point it at a voice memo, an interview, "
    "a take of dialogue or any video with an audio track, press Analyze to see where it would "
    "cut, then press Cut Audio to write the clips. The source file is read in place and is never "
    "modified.\n\n"
    "**It cuts on silence, and only on silence.** It does not recognise words or sentences, and "
    "it cannot tell speech from music — a continuous passage stays one clip however long it is, "
    "and a long enough pause splits one whether or not that is a sensible place to break. The "
    "four settings below are how you tune that."
)

LABEL_AUDIO_SOURCE = "🎙️ Source recording (full path to one file)"
INFO_AUDIO_SOURCE = (
    "Read in place — nothing is uploaded or copied. WAV, MP3, FLAC, M4A and any video whose "
    "audio track is still present (MP4, MKV, MOV…). The file is accepted on whether FFprobe "
    "finds an audio stream in it, not on its extension."
)
LABEL_BROWSE_FILE = "🎙️ Browse file…"

LABEL_AUDIO_OUTPUT = "📁 Output folder"
INFO_AUDIO_OUTPUT = (
    "Each cut creates its own fresh, timestamped subfolder here, so a second run can never "
    "overwrite the first. Existing files are never replaced."
)

LABEL_AUDIO_THRESHOLD = "🔉 Silence threshold (dB)"
INFO_AUDIO_THRESHOLD = (
    "Anything quieter than this counts as silence. The number is negative and lower is stricter: "
    "-50 keeps only near-total silence, -20 will treat ordinary room tone as a gap. -35 is a "
    "sensible starting point for spoken recordings."
)
LABEL_AUDIO_MIN_SILENCE = "⏸️ Minimum silence (seconds)"
INFO_AUDIO_MIN_SILENCE = (
    "Quiet shorter than this is not a gap, so it will not split a clip. This is the control that "
    "decides whether the breath between two words separates them: at 0.5s an ordinary inter-word "
    "pause does not, a deliberate beat does."
)
LABEL_AUDIO_PAD = "➕ Padding (seconds)"
INFO_AUDIO_PAD = (
    "Kept on each side of every detected sound, so a clip does not begin on the attack of the "
    "first syllable or cut off the tail of the last. Padding never reaches into a neighbouring "
    "clip: each side takes at most half the gap it is growing into, so two clips can meet but "
    "never overlap."
)
LABEL_AUDIO_MIN_CLIP = "✂️ Minimum clip length (seconds)"
INFO_AUDIO_MIN_CLIP = (
    "Results shorter than this are dropped rather than written. A 0.1s fragment is a click, not "
    "a clip."
)
LABEL_AUDIO_FORMAT = "💾 Export format"
# [R7.2 Phase 5] The MP3 sentence is deliberately blunt. Offering MP3 beside two lossless formats
# invites the reading that it is simply "the small one", and from an MP3 source it invites the
# worse reading that the original frames are being copied. Neither is true: the cutter decodes the
# audio to find the silences, so MP3 output is always a fresh lossy encode. Saying so is the
# honest version of a feature whose real benefit is file size.
INFO_AUDIO_FORMAT = (
    "**WAV** is 16-bit PCM at 48 kHz — what Audio Layers and every editor accept without "
    "negotiation. **FLAC** is the same audio losslessly compressed, for a smaller library. "
    "**MP3** at 192 kbps is about **a quarter** of the WAV size for a mono recording, or about an "
    "eighth for stereo, and works in Audio Layers too — but it is a **lossy re-encode**, "
    "including when your source is already an MP3, because the silences are found by decoding the "
    "audio and there is no stream copy. Choose MP3 for smaller files, not for better quality."
)

LABEL_AUDIO_ANALYZE = "🔎 Analyze Audio"
LABEL_AUDIO_CUT = "✂️ Cut Audio"
LABEL_AUDIO_PLAN = "Plan summary"
LABEL_AUDIO_TABLE = "Planned clips"
LABEL_AUDIO_REPORT = "Cut report"
AUDIO_PLAN_INITIAL_TEXT = (
    "Choose a recording and press Analyze Audio to see where it would cut. Analyze writes nothing."
)
AUDIO_REPORT_INITIAL_TEXT = "Nothing has been cut yet."
INFO_AUDIO_BUTTONS = (
    "Analyze never writes anything. Cut Audio re-checks the file and your settings first and "
    "refuses a plan that no longer matches them."
)

LABEL_AUDIO_REVIEW = "🎧 Exported clips"
INFO_AUDIO_REVIEW = (
    "Pick a clip to hear it. Keep files it into your voice library under the name in the box; "
    "Discard moves it to the Discarded folder. Discarding is quarantine, never deletion, and an "
    "existing name is refused rather than overwritten."
)
LABEL_AUDIO_PLAYER = "Listen"
LABEL_AUDIO_CLIP_NAME = "✏️ Save as"
INFO_AUDIO_CLIP_NAME = (
    "Edit the name before you keep it if you like — the extension is always taken from the real "
    "file. Audio Layers plays files in filename order, so a leading number keeps a sequence in "
    "order."
)
LABEL_AUDIO_KEEP = "✅ Keep → voice library"
LABEL_AUDIO_DISCARD = "🗑️ Discard → quarantine"
LABEL_AUDIO_FILE_REPORT = "Filing report"
AUDIO_FILE_REPORT_INITIAL_TEXT = "Nothing has been filed yet."

LABEL_AUDIO_LIBRARY = "✅ Voice library folder (where KEPT clips go)"
INFO_AUDIO_LIBRARY = (
    "Validated when you file a clip, never written to before that. It must not be inside the "
    "folder of clips being reviewed."
)
LABEL_AUDIO_DISCARDED = "🗑️ Discarded folder (quarantine, never deletion)"
INFO_AUDIO_DISCARDED = (
    "A discarded clip is moved here intact, for you to deal with outside this tool. There is no "
    "retention expiry and no permanent delete."
)

LABEL_PREF_AUDIO_LIBRARY = "Default voice library"
LABEL_PREF_AUDIO_DISCARDED = "Default Audio Cutter Discarded folder"
LABEL_PREF_AUDIO_OUTPUT = "Default Audio Cutter output"
LABEL_PREF_AUDIO_PREVIEW_LIBRARY = "Default Audio Preview Media Library"
LABEL_PREF_AUDIO_PREVIEW_DISCARDED = "Default Audio Preview Discarded folder"

INFO_AUDIO_LAYERS_TIP = (
    "**Using these clips with Audio Layers.** Open **Advanced Tools → 🎤 Audio Layers (Voice & "
    "SFX)** and choose the exported clips as the voice files. They are played **in filename "
    "order**, which is why the clips are numbered. **Start Delay** sets how far into the track "
    "speech may begin, **Minimum Gap** separates one clip from the next, and **Music Under "
    "Voice** controls how far the music is ducked while a clip plays. Nothing about Audio Layers "
    "changes in R7 — these files are ordinary WAV/FLAC and it already accepts them."
)


# [FORK] Digital-Union (R7.1): Audio Layers Comfort Mix. OPTIONAL and OFF by default.
LABEL_VOICE_AUTO_BALANCE = "\u2696\ufe0f Auto Balance Voice & Music"
INFO_VOICE_AUTO_BALANCE = (
    "Off by default, and off means the mix is exactly what it has always been. "
    "On, BeatSync measures how loud the music and your voice clips actually are (EBU R128) and "
    "evens them out BEFORE mixing, so the speech does not have to be rescued by ducking the music "
    "harder. Corrections are bounded and a near-silent clip is never amplified. It is a more "
    "comfortable balance, not a louder master."
)
LABEL_MUSIC_RETURN = "\u23f3 Music return / fade back (seconds)"
INFO_MUSIC_RETURN = (
    "How long the music takes to come back up to full after a voice clip ends. The original "
    "behaviour is 0.4s, which on a loud track can feel like the music jumps back in. With Auto "
    "Balance on, around 2s is a good starting point. This changes only how the music returns \u2014 "
    "it never moves a voice clip or a video cut."
)
LABEL_BALANCE_REPORT = "Auto Balance measurement"


# [FORK] Digital-Union (R7.1, extended in R7.2): the audio review gallery.
INFO_AUDIO_GALLERY = (
    "**Hover a tile to hear it** \u2014 it loops until you move away, and only one clip ever plays "
    "at a time. Audio is fetched **when you hover**, so clip size and page size no longer limit "
    "what will play. **Left-click keeps**, **right-click discards**, **middle-click** (or **P**) "
    "puts a clip back to undecided. With a tile focused, **S** / **R** / **P** do the same and the "
    "**arrow keys** move between clips. **Shift+wheel** scrubs inside a tile. Edit a name in its "
    "box before confirming if you want to.\n\n"
    "**Nothing moves until you press Confirm and Sort.** Classifying is just a decision.\n\n"
    "**You only have to mark the clips you want to KEEP.** Anything you leave unmarked is treated "
    "as discarded \u2014 but never without asking you first, and never deleted."
)
LABEL_AUDIO_ENABLE_HOVER = "\U0001f50a Enable hover audio"
INFO_AUDIO_ENABLE_HOVER = (
    "Chrome blocks sound until you have interacted with the page. If a tile says "
    "\u201cclick Enable hover audio\u201d, press this once."
)
LABEL_AUDIO_CONFIRM = "\u2705 Confirm and Sort"
LABEL_AUDIO_COUNTS = "Decisions"
AUDIO_COUNTS_INITIAL_TEXT = "Keep 0   \u00b7   Discard 0   \u00b7   Undecided 0   \u00b7   Total 0"

# [R7.2 Phase 3] The admission rule changed, so this text had to. R7.1 refused Confirm while
# anything was undecided, which made the operator click Reject on every clip they did not want.
# Confirm now needs one KEPT clip and sweeps the rest -- after asking.
AUDIO_CONFIRM_DISABLED_TEXT = (
    "Nothing was moved \u2014 mark at least one clip to KEEP first.\n\n"
    "Left-click a tile (or press S with it focused) to keep it. You do not have to reject the "
    "others: anything you leave unmarked is offered for discarding when you press Confirm."
)

# The empty grid, as markup rather than an empty string. `gr.HTML('')` renders nothing at all,
# which looks like a broken page rather than an empty one.
AUDIO_GALLERY_EMPTY_HTML = (
    '<div class="r7-empty">No clips to review yet.</div>'
)

AUDIO_SWEEP_GATE_HEADING = (
    "\u26a0\ufe0f  Some clips are still unmarked. Nothing has moved yet \u2014 here is what "
    "Confirm would do:"
)
AUDIO_SWEEP_GATE_PROMPT = (
    "Press **Proceed** to sort it exactly as listed above, or **Cancel** to go back and mark more "
    "clips. Nothing is deleted either way: discarding moves a file into your Discarded folder.\n"
    "This confirmation applies to THIS batch only. If you change a decision or a folder "
    "afterwards, you will be asked again."
)
AUDIO_SWEEP_CANCELLED_TEXT = (
    "Cancelled. Nothing was moved and every clip is exactly as you left it.\n"
    "Mark the clips you want to keep, then press Confirm and Sort again."
)
LABEL_AUDIO_PROCEED = "\u27a1\ufe0f Proceed \u2014 sort it as listed"
LABEL_AUDIO_SWEEP_CANCEL = "\u21a9\ufe0f Cancel \u2014 go back and mark more"

# [R7.2 Phase 4] The live destination read-out. It is a report, not a gate: the gallery stays
# usable while the folders are being corrected, and only sorting is withheld.
LABEL_AUDIO_DESTINATION_STATUS = "Destination folders"
AUDIO_DESTINATION_STATUS_INITIAL = (
    "Set a Media Library folder and a Discarded folder. They are checked as you type, and again "
    "before anything is moved."
)

# ---------------------------------------------------------------------------------------------
# [FORK] Digital-Union (R7.2 Phase 2): the Audio Review section.
#
# [FORK] R8 UX Refresh (Commit A): this was `LABEL_TAB_AUDIO_PREVIEW`, a top-level tab. It is now
# a section of Media Library, and its label is `LABEL_SECTION_AUDIO_REVIEW`, declared beside the
# other tab labels. The old constant is deleted rather than aliased -- the engine, the widgets and
# every handler are unchanged, only the container is.
# ---------------------------------------------------------------------------------------------

# [FORK] R8 UX Refresh (Phase 10): shortened from 678 characters, and the heading is gone --
# this is now a SECTION inside Media Library, so the section tab already names it and a `###`
# here repeated the label immediately above it.
#
# Two claims were kept because they are the ones that change what an operator does: that it works
# on any folder without the cutter having run (otherwise they will not try it), and that nothing
# is deleted (otherwise they will not press Confirm). The supported-format list moved to the
# folder field's own hint, where it is relevant at the moment of choosing a folder.
INFO_TAB_AUDIO_PREVIEW = (
    "**Review and sort any folder of audio clips** \u2014 the Audio Cutter does not need to have run "
    "in this session, so you can cut today and sort next week.\n\n"
    "Nothing is scanned until you press **Scan Folder**, nothing moves until you press "
    "**Confirm and Sort**, and discarding means *moved to your Discarded folder* \u2014 never "
    "deleted."
)
LABEL_AUDIO_PREVIEW_FOLDER = "\U0001f4c2 Folder of audio clips to review"
INFO_AUDIO_PREVIEW_FOLDER = (
    "The folder holding the clips. An Audio Cutter output folder "
    "(\u201cAudioCutter_\u2026\u201d) is the usual one, but any folder of audio works.\n\n"
    # [FORK] R8 Phase 10: moved here from the section intro. The format list matters at the
    # moment an operator chooses a folder, not as a permanent paragraph above the whole section.
    "Reads **WAV**, **FLAC**, **MP3**, M4A, AAC, OGG and Opus."
)
LABEL_AUDIO_PREVIEW_RECURSIVE = "Include subfolders"
INFO_AUDIO_PREVIEW_RECURSIVE = (
    "Off by default, so one Audio Cutter run is one review batch. Turn it on to sweep a whole "
    "tree of recordings in one pass."
)
LABEL_AUDIO_PREVIEW_SCAN = "\U0001f50d Scan Folder"
LABEL_AUDIO_PREVIEW_PAGE_SIZE = "Clips per page"
INFO_AUDIO_PREVIEW_PAGE_SIZE = (
    "How many tiles to draw at once. Decisions are kept per file, not per page, so paging never "
    "loses one."
)
LABEL_AUDIO_PREVIEW_PREV = "\u25c0 Previous page"
LABEL_AUDIO_PREVIEW_NEXT = "Next page \u25b6"
LABEL_AUDIO_PREVIEW_PAGE = "Position"
AUDIO_PREVIEW_PAGE_INITIAL = "No clips loaded."
LABEL_AUDIO_PREVIEW_REPORT = "Scan report"
AUDIO_PREVIEW_REPORT_INITIAL = (
    "Choose a folder and press Scan Folder.\n"
    "Nothing is read until you do \u2014 loading your saved folders does not scan anything."
)
LABEL_AUDIO_PREVIEW_LIBRARY = "\u2705 Media Library folder (where KEPT clips go)"
LABEL_AUDIO_PREVIEW_DISCARDED = "\U0001f5d1\ufe0f Discarded folder (quarantine, never deletion)"
# [FORK] R8 UX Refresh (Commit A): both cutters now offer the same action, worded the same way,
# because they do the same thing into the same place -- Media Library. The Audio Cutter's goes to
# Audio Review and the Raw Cutter's to Video Review.
LABEL_AUDIO_OPEN_IN_PREVIEW = "\U0001f4da Open results in Media Library"
INFO_AUDIO_OPEN_IN_PREVIEW = (
    "Loads this cut's output folder into **Media Library → Audio Review** so you can classify it "
    "later. It fills the folder in for you; you still press Scan Folder there."
)

# [FORK] Digital-Union (Raw Cutter R11): the button now hands over the FINISHED JOB's own output
# folder rather than the parent output widget, so it is named after what it does. It is available
# once a cut has written clips and unavailable before that -- which is the honest state, because
# there is no particular job to review yet. Video Review's own folder box stays editable, so
# reviewing never requires having run the cutter.
LABEL_CUT_OPEN_LIBRARY = "\U0001f3ac Review these clips"
INFO_CUT_OPEN_LIBRARY = (
    "Available once a cut has written clips. It loads **that job's own output folder** into "
    "**Media Library → Video Review** and sets subfolders to match the job — included for a "
    "folder batch, excluded for a single video. It only fills the folder in; you still press "
    "Scan Folder there, and nothing is read, moved or renamed until you do."
)
#: Pressed with no finished job behind it. It names the reason rather than the folder, because
#: there is no folder: falling back to the parent output widget is exactly what R11 removed.
CUT_HANDOFF_NONE_TEXT = (
    "Cut some clips first — there is no finished job to review yet.\n"
    "You can also type or browse to any folder in Video Review and press Scan Folder."
)
#: The job finished and wrote clips, but its folder is no longer there. Reported, never repaired,
#: and never silently replaced by its parent: a moved or deleted output folder is something the
#: operator needs to know about, not something to paper over with a folder that still exists.
CUT_HANDOFF_MISSING_TEXT = (
    "That job's output folder is no longer there:\n{folder}\n"
    "It may have been moved, renamed or deleted since the cut finished. Nothing was changed."
)

# [R7.2] Every selector here is a CLASS, and that is a requirement rather than a tidy-up. R7.1 had
# one gallery and keyed its whole stylesheet on `#r7-grid`; R7.2 adds the Audio Preview tab's grid,
# whose id is different, so an id-keyed rule would leave the second gallery completely unstyled --
# tiles with no borders, no decision colours and no hover highlight. `.r7-empty` is also a SIBLING
# of the grid rather than a child (the empty state replaces the grid entirely), so the old
# `#r7-grid .r7-empty` descendant rule never matched anything at all.
AUDIO_GALLERY_CSS = """
<style>
.r7-grid { display:flex; flex-wrap:wrap; gap:10px; }
.r7-grid .r7-tile { width:190px; border:2px solid #3a3a3a; border-radius:8px; padding:8px;
  background:#1f1f1f; cursor:pointer; user-select:none; transition:border-color .12s,
  box-shadow .12s; }
.r7-grid .r7-tile:focus { outline:none; box-shadow:0 0 0 2px #6ea8fe; }
.r7-grid .r7-tile.r7-playing { border-color:#6ea8fe; box-shadow:0 0 10px rgba(110,168,254,.55); }
.r7-grid .r7-tile.r7-selected { border-color:#2ea043; background:#132b18; }
.r7-grid .r7-tile.r7-rejected { border-color:#d14; background:#2b1316; }
.r7-grid .r7-tile.r7-pending { border-color:#3a3a3a; }
.r7-grid .r7-head { display:flex; justify-content:space-between; align-items:center;
  font-weight:600; font-size:13px; }
.r7-grid .r7-badge { font-size:15px; }
.r7-grid .r7-time { font-size:12px; opacity:.85; margin:3px 0; }
.r7-grid .r7-src { opacity:.6; }
.r7-grid .r7-wave { height:26px; display:flex; align-items:center; justify-content:center;
  border-radius:4px; background:#161616; font-size:11px; opacity:.7; margin-bottom:6px;
  text-align:center; padding:0 4px; }
.r7-grid .r7-tile.r7-playing .r7-wave { background:#16263d; opacity:1; }
/* A clip the browser genuinely could not fetch or decode. Amber, not red: red is the Discard
   decision, and "I could not play this" must not look like "you rejected this". The tile stays
   fully classifiable -- an unplayable clip is still one the operator may want to keep. */
.r7-grid .r7-tile.r7-failed { border-color:#e3b341; }
.r7-grid .r7-tile.r7-failed .r7-wave { background:#2b2410; opacity:1; color:#e3b341; }
.r7-grid .r7-name { width:100%; box-sizing:border-box; font-size:11px; padding:3px 4px;
  border-radius:4px; border:1px solid #3a3a3a; background:#141414; color:inherit; }
.r7-empty { opacity:.7; padding:12px; }
.r7-enable-hover.r7-needed button { outline:2px solid #e3b341 !important; }
/* The hidden decision channels. NOT `visible=False`: Gradio drops an invisible component from the
   DOM entirely, and the page has to be able to write into these. */
#audio-decisions, #audio-preview-decisions { display:none !important; }
</style>
"""
