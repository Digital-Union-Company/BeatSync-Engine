"""Raw Audio Cutter V1: split one recording into reusable audio clips on its silences.

The whole tool is one idea — **FFmpeg's `silencedetect` says where the quiet is, and the clips are
what is left over.** Everything here is that idea made explicit and testable: the probe argv, the
detector argv, the parser for its log, the arithmetic that turns silences into segments, the
extraction argv, and the manifest. No FFmpeg is required to test any of it except the two runners
at the bottom, which are injected everywhere they are used.

**What this is not.** It does not detect words, sentences, speakers, or music-versus-speech. It has
no model and no transcription. A continuous passage with no qualifying silence inside it stays one
segment however long it is, and two words separated by a long enough pause become two clips whether
or not that is a sensible place to cut. Saying so plainly is part of the contract: R8 is where
classification would live, and this is deliberately not it.

Stdlib only (`os`, `re`, `json`, `subprocess`, `dataclasses`), so the fork package's
no-runtime-import rule still holds.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

# ---------------------------------------------------------------------------------------------
# Defaults — the four numbers the operator actually turns
# ---------------------------------------------------------------------------------------------

#: Below this level counts as silence. dBFS, so it is negative and *lower* is stricter: -50 keeps
#: only near-digital-silence, -20 will call ordinary room tone a gap.
DEFAULT_THRESHOLD_DB = -35.0

#: Quiet shorter than this is not a gap. This is the control that decides whether a breath between
#: two words splits them: at 0.5 s a normal inter-word pause does not, a deliberate beat does.
DEFAULT_MIN_SILENCE = 0.5

#: Kept on each side of a detected sound so the clip does not start on the attack of the first
#: syllable or chop the decay of the last. Never allowed to reach into a neighbour — see
#: :func:`plan_segments`.
DEFAULT_PAD = 0.10

#: Shorter results than this are dropped rather than written. A 0.1 s fragment is a click, not a
#: clip, and a library full of them is worse than one with gaps.
DEFAULT_MIN_CLIP = 0.30

#: Output encodings. WAV PCM is the default because it is what every editor and the existing Audio
#: Layers voice input accept without negotiation.
FORMAT_WAV = "wav"
FORMAT_FLAC = "flac"
FORMATS = (FORMAT_WAV, FORMAT_FLAC)

#: 16-bit, 48 kHz: the requested default, and the rate the rest of the app already mixes at.
OUTPUT_SAMPLE_RATE = 48000

PROBE_TIMEOUT_SECONDS = 60
DETECT_TIMEOUT_SECONDS = 900
EXTRACT_TIMEOUT_SECONDS = 300

#: Containers worth offering. The real gate is "does FFprobe find an audio stream", not the
#: extension — a `.mkv` with one audio track is a perfectly good source and a `.wav` that is
#: actually empty is not.
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma")
VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")
SOURCE_SUFFIXES = AUDIO_SUFFIXES + VIDEO_SUFFIXES


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceInfo:
    """What the probe found. `has_audio` false means refuse — there is nothing to cut."""
    path: str
    duration: float
    has_audio: bool
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0

    def describe(self) -> str:
        if not self.has_audio:
            return "no audio stream"
        parts = [format_timecode(self.duration)]
        if self.codec:
            parts.append(self.codec)
        if self.sample_rate:
            parts.append(f"{self.sample_rate} Hz")
        if self.channels:
            parts.append("mono" if self.channels == 1 else f"{self.channels} ch")
        return "  ·  ".join(parts)


@dataclass(frozen=True)
class Segment:
    """One planned clip, in SOURCE time. `index` is 1-based and is what the filename carries."""
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------------------------
# Probe: is there audio at all, and how long is it
# ---------------------------------------------------------------------------------------------

def build_probe_command(ffprobe: str, source: str) -> list[str]:
    """The one probe argv: first audio stream plus container duration, as JSON.

    No ``-nostdin``: that is an **ffmpeg** option. FFprobe does not reject it cleanly — it reads
    the next token as its value and dies — which is a measured trap this repository has already
    paid for once. The runner detaches stdin instead.
    """
    return [str(ffprobe), "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "json",
            str(source)]


def parse_probe(text: str, path: str = "") -> SourceInfo:
    """Read the probe JSON. Anything unreadable answers `has_audio=False` rather than raising.

    A source with no audio stream yields an empty `streams` list, which is exactly the case the
    GUI must refuse in words — so it is a normal answer here, not an error.
    """
    try:
        payload = json.loads(text or "{}")
    except (ValueError, TypeError):
        return SourceInfo(path=path, duration=0.0, has_audio=False)
    if not isinstance(payload, dict):
        return SourceInfo(path=path, duration=0.0, has_audio=False)

    streams = payload.get("streams") or []
    stream = streams[0] if isinstance(streams, list) and streams else None
    duration = _as_float((payload.get("format") or {}).get("duration"))

    if not isinstance(stream, dict):
        return SourceInfo(path=path, duration=duration or 0.0, has_audio=False)

    return SourceInfo(
        path=path,
        duration=duration or 0.0,
        has_audio=True,
        codec=str(stream.get("codec_name") or ""),
        sample_rate=int(_as_float(stream.get("sample_rate")) or 0),
        channels=int(_as_float(stream.get("channels")) or 0),
    )


def _as_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):   # NaN / inf
        return 0.0
    return number


# ---------------------------------------------------------------------------------------------
# Detect: where the silence is
# ---------------------------------------------------------------------------------------------

def build_silence_command(ffmpeg: str, source: str, threshold_db: float = DEFAULT_THRESHOLD_DB,
                          min_silence: float = DEFAULT_MIN_SILENCE) -> list[str]:
    """The detector argv. Decodes audio only and writes nothing.

    ``-f null -`` is the point: `silencedetect` is an analysis filter whose product is its log on
    stderr, so there is no output file and the source is never opened for writing. ``-vn`` keeps a
    video source's pictures out of the decode entirely, which is most of the speed on an MP4.
    """
    return [str(ffmpeg), "-hide_banner", "-nostdin",
            "-i", str(source),
            "-vn",
            "-af", f"silencedetect=noise={float(threshold_db):g}dB:d={float(min_silence):g}",
            "-f", "null", "-"]


_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9]*\.?[0-9]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9]*\.?[0-9]+)")


def parse_silence_log(text: str, duration: float) -> list[tuple[float, float]]:
    """`(start, end)` for every detected silence, in order.

    Two real shapes have to be handled and both come up constantly:

    * a recording that **ends** quiet emits a `silence_start` with no matching `silence_end`,
      because the filter never sees the level come back up. That silence runs to `duration`.
    * a recording that **begins** quiet emits `silence_start: 0`, which is ordinary.

    Lines are read in file order rather than paired by regex over the whole blob, so an interleaved
    warning between the two halves of one silence cannot mis-pair them.
    """
    limit = max(0.0, float(duration or 0.0))
    silences: list[tuple[float, float]] = []
    pending: float | None = None

    for line in (text or "").splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            if pending is not None:                      # two starts in a row: keep the first
                continue
            pending = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END.search(line)
        if end_match and pending is not None:
            end = float(end_match.group(1))
            if limit:
                end = min(end, limit)
            if end > pending:
                silences.append((pending, end))
            pending = None

    if pending is not None and limit and pending < limit:
        silences.append((pending, limit))                # trailing silence, no end line

    silences.sort()
    return silences


def sound_intervals(duration: float, silences) -> list[tuple[float, float]]:
    """The complement of the silences over `[0, duration]` — the parts that make noise.

    This is what makes leading and trailing silence incapable of producing an empty clip: they are
    simply not in the complement. Overlapping or out-of-order silences are merged by the running
    cursor rather than trusted.
    """
    limit = max(0.0, float(duration or 0.0))
    if limit <= 0.0:
        return []

    sounds: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(tuple(s) for s in silences):
        start = max(0.0, min(float(start), limit))
        end = max(0.0, min(float(end), limit))
        if start > cursor:
            sounds.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < limit:
        sounds.append((cursor, limit))

    return [(a, b) for a, b in sounds if b > a]


def plan_segments(duration: float, silences, pad: float = DEFAULT_PAD,
                  min_clip: float = DEFAULT_MIN_CLIP) -> list[Segment]:
    """Turn silences into the clips that would be written. Writes nothing.

    **Padding can never make two clips overlap, and that is enforced by construction rather than
    by a later repair pass.** Each side is allowed at most *half the gap it is reaching into*, so
    two neighbours separated by a 0.6 s silence with a 0.5 s pad setting grow by 0.3 s each and meet
    exactly at the midpoint; they can touch, never overlap, and never reorder. Clamping after the
    fact would have to decide which of two already-overlapping clips to shorten, and every answer
    to that is arbitrary.

    Segments shorter than `min_clip` **after** padding are dropped, then the survivors are
    renumbered from 1 so the indices in the filenames have no holes.
    """
    limit = max(0.0, float(duration or 0.0))
    pad = max(0.0, float(pad or 0.0))
    min_clip = max(0.0, float(min_clip or 0.0))

    sounds = sound_intervals(limit, silences)
    planned: list[Segment] = []

    for position, (start, end) in enumerate(sounds):
        previous_end = sounds[position - 1][1] if position > 0 else 0.0
        next_start = sounds[position + 1][0] if position + 1 < len(sounds) else limit

        pad_before = min(pad, max(0.0, start - previous_end) / 2.0)
        pad_after = min(pad, max(0.0, next_start - end) / 2.0)

        padded_start = max(0.0, start - pad_before)
        padded_end = min(limit, end + pad_after)

        if padded_end - padded_start >= min_clip:
            planned.append(Segment(index=0, start=padded_start, end=padded_end))

    return [Segment(index=number, start=s.start, end=s.end)
            for number, s in enumerate(planned, start=1)]


# ---------------------------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(source: str, fallback: str = "audio") -> str:
    """A filename-safe stem taken from the source's own name, so clips stay traceable to it."""
    base = os.path.splitext(os.path.basename(str(source or "")))[0]
    cleaned = _UNSAFE.sub("_", base).strip("._-")
    return cleaned[:60] or fallback


def clip_filename(stem: str, index: int, fmt: str = FORMAT_WAV) -> str:
    """`stem_part_001.wav`. Zero-padded because **Audio Layers plays files in filename order**, and
    `part_10` sorting before `part_2` would silently reorder someone's voice-over."""
    extension = FORMAT_FLAC if str(fmt) == FORMAT_FLAC else FORMAT_WAV
    return f"{stem}_part_{int(index):03d}.{extension}"


def build_extract_command(ffmpeg: str, source: str, segment: Segment, destination: str,
                          fmt: str = FORMAT_WAV,
                          sample_rate: int = OUTPUT_SAMPLE_RATE) -> list[str]:
    """One clip's argv.

    ``-n`` rather than ``-y``: if the destination somehow exists, FFmpeg must refuse and say so.
    Overwriting a file in a library the operator is building is the one failure this tool must not
    have, and the run directory is fresh per cut so a collision already means something is wrong.

    ``-ss`` before ``-i`` is the fast accurate seek; ``-map 0:a:0`` with ``-vn`` takes exactly the
    first audio stream, so a video source contributes its sound and nothing else.
    """
    duration = max(0.0, float(segment.end) - float(segment.start))
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-n",
               "-ss", f"{float(segment.start):.6f}",
               "-i", str(source),
               "-t", f"{duration:.6f}",
               "-vn", "-map", "0:a:0", "-map_metadata", "-1"]
    if str(fmt) == FORMAT_FLAC:
        command += ["-c:a", "flac", "-sample_fmt", "s16"]
    else:
        command += ["-c:a", "pcm_s16le"]
    command += ["-ar", str(int(sample_rate)), str(destination)]
    return command


def run_directory_name(stem: str, stamp: str) -> str:
    """`AudioCutter_<stem>_<stamp>`. A fresh directory per cut is what makes overwriting
    impossible without any existence check racing a second press."""
    return f"AudioCutter_{stem}_{stamp}"


# ---------------------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------------------

MANIFEST_NAME = "audio_manifest.json"
MANIFEST_SCHEMA = "beatsync-raw-audio-cutter-1"


def manifest_payload(source: SourceInfo, segments, settings: dict, written) -> dict:
    """The record of one cut. Source path, every segment's source timing, and the output names.

    `written` maps a segment index to the filename that actually landed, so a clip that failed is
    visibly absent rather than silently implied by the plan.
    """
    rows = []
    for segment in segments:
        name = (written or {}).get(segment.index)
        rows.append({
            "index": int(segment.index),
            "start": round(float(segment.start), 6),
            "end": round(float(segment.end), 6),
            "duration": round(float(segment.duration), 6),
            "output": name or None,
            "written": bool(name),
        })
    return {
        "schema": MANIFEST_SCHEMA,
        "source": {
            "path": str(source.path),
            "duration": round(float(source.duration), 6),
            "codec": source.codec,
            "sample_rate": int(source.sample_rate),
            "channels": int(source.channels),
        },
        "settings": dict(settings or {}),
        "totals": {
            "planned": len(rows),
            "written": sum(1 for row in rows if row["written"]),
        },
        "segments": rows,
    }


# ---------------------------------------------------------------------------------------------
# Presentation helpers (pure, so the GUI stays a view)
# ---------------------------------------------------------------------------------------------

def format_timecode(seconds: float) -> str:
    """`M:SS.s` under an hour, `H:MM:SS.s` beyond it."""
    total = max(0.0, float(seconds or 0.0))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    rest = total - hours * 3600 - minutes * 60
    if hours:
        return f"{hours}:{minutes:02d}:{rest:04.1f}"
    return f"{minutes}:{rest:04.1f}"


#: A clip larger than this is described rather than embedded. 2.5 s of 48 kHz 16-bit mono is about
#: 240 kB, so an ordinary voice clip is nowhere near it; the cap exists so that pointing the cutter
#: at an hour-long take and pressing a tile cannot try to inline a 300 MB player.
MAX_PREVIEW_BYTES = 25 * 1024 * 1024

_MEDIA_TYPES = {FORMAT_WAV: "audio/wav", FORMAT_FLAC: "audio/flac"}


def preview_markup(path: str, label: str = "") -> str:
    """A self-contained `<audio controls>` for one clip, with the audio inlined as a data URI.

    **Why inline rather than a file path.** Gradio will only serve a file from its own cache, the
    working directory or the system temp directory; an exported clip lives wherever the operator
    chose, so handing its path to an audio component raises `InvalidPathError`. The fix is
    emphatically *not* to widen `allowed_paths` -- that would expose a whole tree to every request,
    and this application already refused to do it once, which is why the Clips Preview gallery has
    a token-gated middleware instead. One clip, inlined on demand, needs neither.

    The browser's own control supplies play/pause, scrubbing and the running time, so there is no
    custom player to maintain.
    """
    safe_label = _escape(label or os.path.basename(str(path or "")))
    if not path or not os.path.isfile(path):
        return f'<p><strong>{safe_label}</strong><br>That clip is no longer on disk.</p>'

    size = os.path.getsize(path)
    if size > MAX_PREVIEW_BYTES:
        megabytes = size / (1024 * 1024)
        return (f'<p><strong>{safe_label}</strong><br>This clip is {megabytes:.1f} MB, which is '
                f'too large to preview in the page. It was still exported — play it from the '
                f'output folder.</p>')

    import base64

    extension = os.path.splitext(path)[1].lstrip(".").lower()
    media_type = _MEDIA_TYPES.get(extension, "audio/wav")
    try:
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError as failure:
        return f'<p><strong>{safe_label}</strong><br>It could not be read: {_escape(str(failure))}</p>'

    return (f'<p style="margin:0 0 6px 0"><strong>{safe_label}</strong></p>'
            f'<audio controls preload="metadata" style="width:100%" '
            f'src="data:{media_type};base64,{encoded}"></audio>')


def _escape(text: str) -> str:
    """Stdlib escaping, so a filename can never carry markup or script into the page."""
    import html

    return html.escape(str(text or ""), quote=True)


def segment_rows(segments, stem: str, fmt: str = FORMAT_WAV) -> list[list]:
    """Table rows for the plan: clip number, source start, source end, length, output name."""
    return [[segment.index,
             format_timecode(segment.start),
             format_timecode(segment.end),
             f"{segment.duration:.2f}s",
             clip_filename(stem, segment.index, fmt)]
            for segment in segments]


def plan_summary(source: SourceInfo, silences, segments, settings: dict) -> str:
    """The honest read-out. Says what was found, and says what the tool does not know."""
    if not source.has_audio:
        return ("❌ No audio stream in this file.\n"
                "Raw Audio Cutter needs a file with sound in it — an audio file, or a video "
                "whose audio track is still present.")
    if source.duration <= 0:
        return "❌ This file reports no duration, so there is nothing to cut."

    lines = [
        f"Source:  {os.path.basename(source.path)}  ·  {source.describe()}",
        "",
        f"Silences found:   {len(silences)}",
        f"Clips planned:    {len(segments)}",
    ]
    if segments:
        total = sum(s.duration for s in segments)
        lines.append(f"Audio kept:       {format_timecode(total)} of "
                     f"{format_timecode(source.duration)}")
        longest = max(segments, key=lambda s: s.duration)
        lines.append(f"Longest clip:     {longest.duration:.2f}s  (clip {longest.index})")
    lines += [
        "",
        f"Settings: silence below {settings.get('threshold_db')} dB lasting at least "
        f"{settings.get('min_silence')}s is a gap; "
        f"{settings.get('pad')}s kept on each side; "
        f"clips under {settings.get('min_clip')}s dropped.",
    ]
    if not segments:
        lines += [
            "",
            "Nothing would be exported. Either the whole file is below the silence threshold, or "
            "every sound in it is shorter than the minimum clip length. Try a lower (more "
            "negative) threshold, a longer minimum silence, or a shorter minimum clip.",
        ]
    lines += [
        "",
        "This splits on silence only. It does not detect words or sentences, and it does not "
        "tell speech from music — a continuous passage stays one clip however long it is.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# The two process launches
#
# Here rather than in `gui.py` because two suites pin that module's `subprocess.run` site count at
# exactly two. `run_duration_command` and the native picker already set this precedent.
# ---------------------------------------------------------------------------------------------

def run_probe_command(argv, timeout: int = PROBE_TIMEOUT_SECONDS) -> str:
    """Execute a probe argv windowless and bounded; return stdout, or `""` on any failure.

    ``stdin=DEVNULL`` because FFprobe has no ``-nostdin`` and will otherwise inherit — and can
    block on — whatever stdin the parent has. Measured: it hung a test suite until its timeout.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if int(completed.returncode) != 0:
        return ""
    return completed.stdout or ""


def run_media_command(argv, timeout: int = DETECT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Execute an FFmpeg argv; return `(returncode, stderr)`.

    **stderr, not stdout** — `silencedetect` reports there, and so do FFmpeg's errors, so the same
    runner serves the detector and the extractor. A failure to launch is reported as a non-zero
    code with the reason as text rather than as an exception, because one unreadable source must
    not end a cut.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {timeout}s"
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        return 1, str(failure)
    return int(completed.returncode), completed.stderr or ""


__all__ = [
    "DEFAULT_THRESHOLD_DB", "DEFAULT_MIN_SILENCE", "DEFAULT_PAD", "DEFAULT_MIN_CLIP",
    "FORMAT_WAV", "FORMAT_FLAC", "FORMATS", "OUTPUT_SAMPLE_RATE",
    "AUDIO_SUFFIXES", "VIDEO_SUFFIXES", "SOURCE_SUFFIXES",
    "PROBE_TIMEOUT_SECONDS", "DETECT_TIMEOUT_SECONDS", "EXTRACT_TIMEOUT_SECONDS",
    "SourceInfo", "Segment",
    "build_probe_command", "parse_probe",
    "build_silence_command", "parse_silence_log", "sound_intervals", "plan_segments",
    "safe_stem", "clip_filename", "build_extract_command", "run_directory_name",
    "MANIFEST_NAME", "MANIFEST_SCHEMA", "manifest_payload",
    "format_timecode", "segment_rows", "plan_summary",
    "MAX_PREVIEW_BYTES", "preview_markup",
    "PENDING", "SELECTED", "REJECTED", "DECISIONS",
    "MAX_EMBEDDED_BYTES", "DEFAULT_PAGE_SIZE",
    "gallery_markup", "decision_counts", "counts_line",
    "run_probe_command", "run_media_command",
]


# ---------------------------------------------------------------------------------------------
# [FORK] R7.1 -- the review gallery
#
# Tiles instead of a dropdown, with hover-to-play. Two decisions shape everything below.
#
# **Audio is embedded, but BOUNDED.** A tile plays instantly on hover only if its bytes are already
# in the page, and the alternative -- fetching on hover -- needs an HTTP route, which would mean
# either widening Gradio's `allowed_paths` (refused: it exposes a whole tree to every request) or
# standing up a second authorised media endpoint. For a V1 reviewing one recording's clips, a byte
# budget is the honest trade: everything inside it is instant, everything past it says so plainly
# and is still playable from the output folder.
#
# **Classification is client-side until Confirm**, which is the requirement -- nothing moves until
# the operator says so. The page keeps the decisions and writes them into one hidden field that the
# Confirm handler reads.
# ---------------------------------------------------------------------------------------------

PENDING = "PENDING"
SELECTED = "SELECTED"
REJECTED = "REJECTED"
DECISIONS = (PENDING, SELECTED, REJECTED)

#: Total embedded audio allowed in one page. ~40 MB of base64 is roughly 30 MB of WAV, which is
#: about two minutes of 48 kHz mono -- comfortably more than a review page of spoken clips, and far
#: short of what makes a page sluggish.
MAX_EMBEDDED_BYTES = 40 * 1024 * 1024

#: Hard cap on tiles per page, so an enormous run is paged rather than rendered in one go.
DEFAULT_PAGE_SIZE = 60

_DECISION_CLASS = {PENDING: "r7-pending", SELECTED: "r7-selected", REJECTED: "r7-rejected"}
_DECISION_BADGE = {PENDING: "?", SELECTED: "\u2713", REJECTED: "\u2717"}


def gallery_markup(entries, decisions=None, names=None,
                   budget: int = MAX_EMBEDDED_BYTES) -> str:
    """The review grid.

    `entries` are the dicts the cut handler produced. `decisions` and `names` are the operator's
    current state, so a repaint preserves them.

    Every interpolated value goes through :func:`_escape`, so a filename can carry no markup into
    the page.
    """
    if not entries:
        return ('<div class="r7-empty">No clips to review. Cut a recording first.</div>')

    decisions = dict(decisions or {})
    names = dict(names or {})
    spent = 0
    tiles = []

    for entry in entries:
        key = str(entry.get("index"))
        decision = decisions.get(key, PENDING)
        if decision not in DECISIONS:
            decision = PENDING
        label = names.get(key) or entry.get("name") or ""
        path = entry.get("path") or ""

        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        if size and spent + size <= budget:
            media = _audio_element(path)
            spent += size
            note = ""
        else:
            media = ""
            note = ('<div class="r7-note">not loaded for preview on this page '
                    '\u2014 it was still exported</div>')

        duration = float(entry.get("end", 0.0)) - float(entry.get("start", 0.0))
        tiles.append(
            f'<div class="r7-tile {_DECISION_CLASS[decision]}" tabindex="0" role="button"'
            f' data-idx="{_escape(key)}" data-dur="{duration:.3f}"'
            f' data-name="{_escape(entry.get("name") or "")}">'
            f'<div class="r7-head">'
            f'<span class="r7-num">{_escape(f"{entry.get('index')}")}</span>'
            f'<span class="r7-badge">{_DECISION_BADGE[decision]}</span>'
            f'</div>'
            f'<div class="r7-time">{format_timecode(duration)}'
            f' <span class="r7-src">{format_timecode(float(entry.get("start", 0.0)))}'
            f'\u2192{format_timecode(float(entry.get("end", 0.0)))}</span></div>'
            f'<div class="r7-wave"><span class="r7-state">hover to play</span></div>'
            f'{media}{note}'
            f'<input class="r7-name" type="text" value="{_escape(label)}"'
            f' aria-label="file name" spellcheck="false">'
            f'</div>')

    return (f'<div class="r7-grid" id="r7-grid" data-count="{len(entries)}">'
            + "".join(tiles) + "</div>")


def _audio_element(path: str) -> str:
    """One tile's audio, inlined. `preload="none"` is pointless for a data URI, so it is absent."""
    import base64

    extension = os.path.splitext(path)[1].lstrip(".").lower()
    media_type = _MEDIA_TYPES.get(extension, "audio/wav")
    try:
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return ""
    return f'<audio class="r7-audio" loop src="data:{media_type};base64,{encoded}"></audio>'


def decision_counts(entries, decisions) -> dict:
    """`{SELECTED, REJECTED, PENDING, TOTAL}` for the page, read from the server's own entry list.

    Counting from `entries` rather than from the decisions dict is deliberate: a decision for a
    clip that is not on this page must not be able to inflate the totals, and a clip with no
    decision at all must count as pending rather than vanish.
    """
    decisions = dict(decisions or {})
    counts = {SELECTED: 0, REJECTED: 0, PENDING: 0}
    for entry in entries:
        decision = decisions.get(str(entry.get("index")), PENDING)
        counts[decision if decision in counts else PENDING] += 1
    counts["TOTAL"] = len(entries)
    return counts


def counts_line(counts: dict) -> str:
    return (f"Keep {counts[SELECTED]}   \u00b7   Discard {counts[REJECTED]}   \u00b7   "
            f"Undecided {counts[PENDING]}   \u00b7   Total {counts['TOTAL']}")
