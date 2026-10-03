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
                            variation_text=None):
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

    return f"""✅ Video created successfully!

Statistics:
Video processing: {processing_text}
{variation_line}Total cuts: {total_cuts}
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

LABEL_CUSTOM_FPS = "🎞️ Custom FPS (Frame Rate)"
INFO_CUSTOM_FPS = "Leave empty for auto-detect, or enter value (24/30/60)"

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
# [FORK] Digital-Union: Variant Lab V1 (C2)
# See src/beatsync_fork/variant_lab.py for the resolvers these labels describe. Variant Lab writes
# the Variation Seed, the six sliders and (E2, opt-in) the three audio levels — and nothing else.
# It never renders, and it never re-analyses your videos.
# ============================================================================

LABEL_VARIANT_LAB = "🧪 Variant Lab"
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
# [FORK] Digital-Union: C3-R0 — render exactly two compared candidates
# See src/beatsync_fork/render_batch.py. This copy has two jobs: make clear that rendering is a
# real, uninterruptible commitment of two renders, and make clear that it uses the *stored*
# candidate settings rather than whatever the Variant Lab controls happen to say now.
# ============================================================================

LABEL_RENDER_CANDIDATES = "🎬 Candidates to render"
INFO_RENDER_CANDIDATES = (
    "Tick exactly two candidates from the list above. Nothing happens until you press Render "
    "Selected Variants."
)

LABEL_RENDER_SELECTED = "🎞️ Render Selected Variants"

LABEL_RENDER_BATCH_SUMMARY = "Render batch result"
PLACEHOLDER_RENDER_BATCH_SUMMARY = (
    "No batch rendered yet. Tick two candidates above and press Render Selected Variants."
)

INFO_RENDER_SELECTED = (
    "**This makes two real videos**, one after the other — it is the only button here that "
    "renders anything other than Create Music Video. Each candidate uses the settings it was "
    "generated with, so editing the Variant Lab controls afterwards does not change what gets "
    "rendered. Everything that is *not* a candidate setting — your audio, voice clips, SFX "
    "folder, source videos, output name, encoder and FPS — is taken as it stands the moment you "
    "press the button, and later edits do not affect the batch already running.\n\n"
    "There is **no Stop button in this version**, so treat it as a commitment to two renders. If "
    "the first one fails the second is not attempted; if the second fails the first video is "
    "still yours. Each file is named for its candidate, so neither can overwrite the other or "
    "anything already in your output folder. The comparison list is not used up — you can still "
    "apply a candidate, or render the same pair again."
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

INFO_CONFIRMATION_GATE = (
    "Create Music Video stays disabled until you confirm the source list. "
    "Confirmation covers the actual files, not just the count, and is re-checked before rendering."
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
