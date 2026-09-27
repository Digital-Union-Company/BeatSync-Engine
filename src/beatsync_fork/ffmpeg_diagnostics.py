#!/usr/bin/env python3
"""Turn FFmpeg stderr into one short, useful failure reason.

Phase 3A spent a whole forensic task recovering a line FFmpeg had already printed. A Stage 6 render
died with ``283 clip(s) failed; refusing to concatenate an incomplete timeline``, while the actual
cause sat in stderr:

    [h264_nvenc @ 0000…] Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0
    [h264_nvenc @ 0000…] The minimum required Nvidia driver for nvenc is 610.00 or newer

``extract_clip_segment_ffmpeg`` printed that text and returned a bare ``False``; the GUI redirects
stdout into ``QuietConsole``, so the reason was discarded and every failed clip reported the same
generic string. This module is the missing step: a bounded summariser that picks the *most specific*
line FFmpeg emitted, so the existing Phase 2A structured warning path can carry it to the panel.

Two design rules worth keeping:

* **Generic, not NVIDIA-special-cased.** Nothing here knows about drivers or NVENC. It ranks lines by
  how diagnostic they look, and anchors on the generic consequence FFmpeg prints after a real failure
  (``Error while opening encoder`` → ``Task finished with error code`` → ``Conversion failed!``) to
  locate the root cause just before it. Special-casing the driver text would have fixed exactly one
  incident and taught us nothing about the next.
* **Bounded, single line.** A failing llama.cpp/Vulkan run can emit megabytes. The status panel is a
  status line, not a log sink — the full text still reaches the console exactly as before.

Stdlib-only, like the rest of ``beatsync_fork``, so the whole thing is testable on a bare interpreter
with no FFmpeg, CUDA or GPU present.
"""

from __future__ import annotations

import re
from typing import Sequence

MAX_REASON_CHARS = 240
"""Hard ceiling for one propagated reason. Matches ``video_processor._short_error``'s budget, so the
reason survives Stage 6's own truncation without being cut twice."""

MAX_REASON_LINES = 2
"""How many FFmpeg lines may be joined. Two is enough for the common
"what failed" + "what is required" pair and still fits the panel."""

_ADDRESS = re.compile(r"\[([^\]@]+?)\s*@\s*[0-9A-Fa-fx]+\]")
"""``[h264_nvenc @ 000001c31bb96680]`` → ``[h264_nvenc]``. The heap address is noise that changes every
run; dropping it keeps a reason stable enough to compare, deduplicate and assert on."""

_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Lines that carry no diagnosis at all: banner, build flags, stream inventory, progress meter.
_NOISE_PREFIXES = (
    "ffmpeg version", "built with", "configuration:", "libav", "libsw", "libpost",
    "input #", "output #", "metadata:", "duration:", "stream #", "stream mapping:",
    "press [q]", "frame=", "video:", "audio:", "subtitle:", "encoder      :",
    "  encoder", "handler_name", "creation_time", "major_brand", "minor_version",
    "compatible_brands", "vendor_id", "title           :", "language        :",
    "last message repeated", "[q] command received",
)
_NOISE_CONTAINS = (
    "muxing overhead",
    "deprecated pixel format",
    "use -b:v",
)

# Real failures, but *consequences* of an earlier root cause. Kept as a fallback only.
_GENERIC_MARKERS = (
    "error while opening encoder",
    "error sending frames to consumers",
    "could not open encoder before eof",
    "task finished with error code",
    "terminating thread with return code",
    "nothing was written into output file",
    "conversion failed",
    "error opening output file",
    "error opening output files",
    "error initializing output stream",
    "error while filtering",
    "finishing stream without any data written to it",
)

# What a genuinely informative FFmpeg failure line tends to contain.
_SPECIFIC_MARKERS = (
    "does not support", "not supported", "unsupported", "no such file", "no such device",
    "invalid", "cannot", "can't", "unable to", "failed", "denied", "out of memory",
    "minimum required", "required:", "impossible to convert", "no capable devices",
    "function not implemented", "timed out", "timeout", "permission", "not found",
    "incorrect", "mismatch", "too large", "too many", "device lost", "driver",
    "protocol not found", "moov atom not found", "invalid data found",
)


def _clean(line: str) -> str:
    line = _ADDRESS.sub(r"[\1]", line)
    line = _CONTROL.sub(" ", line)
    return _WHITESPACE.sub(" ", line).strip()


def _is_noise(lowered: str) -> bool:
    if not lowered:
        return True
    if lowered.startswith(_NOISE_PREFIXES):
        return True
    return any(token in lowered for token in _NOISE_CONTAINS)


def _select(lines: Sequence[str], max_lines: int) -> list[str]:
    """Pick the specific lines that actually explain the failure.

    FFmpeg prints the root cause immediately before the generic consequence it triggers
    (``Error while opening encoder`` → ``Task finished with error code`` → ``Conversion failed!``).
    Choosing the specific lines *closest to that boundary* is what separates a fatal cause from an
    earlier warning FFmpeg recovered from — in the Phase 3A capture the nvdec hwaccel init failed and
    was silently fallen back on four lines before the fatal NVENC driver mismatch, so simply taking the
    first diagnostic line reported the wrong one. With no consequence anchor present, the earliest
    specific lines are the best available answer.
    """
    cleaned = [(_clean(raw), _clean(raw).lower()) for raw in lines]
    kept = [(text, low) for text, low in cleaned if not _is_noise(low)]

    anchor = None
    for index, (_text, low) in enumerate(kept):
        if any(marker in low for marker in _GENERIC_MARKERS):
            anchor = index
            break

    def specifics(window):
        return [text for text, low in window
                if any(marker in low for marker in _SPECIFIC_MARKERS)
                and not any(marker in low for marker in _GENERIC_MARKERS)]

    limit = max(1, int(max_lines))
    if anchor is not None:
        before = specifics(kept[:anchor])
        if before:
            return before[-limit:]
    everything = specifics(kept)
    if everything:
        return everything[:limit]
    consequences = [text for text, low in kept
                    if any(marker in low for marker in _GENERIC_MARKERS)]
    if consequences:
        return consequences[:1]
    return [kept[-1][0]] if kept else []


def bound(text: str, limit: int = MAX_REASON_CHARS) -> str:
    """Collapse to one line and truncate with an ellipsis. Never returns more than ``limit`` chars."""
    collapsed = _WHITESPACE.sub(" ", _CONTROL.sub(" ", str(text or ""))).strip()
    limit = max(1, int(limit))
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def summarize_ffmpeg_failure(
    stderr: str,
    limit: int = MAX_REASON_CHARS,
    max_lines: int = MAX_REASON_LINES,
) -> str:
    """Best short explanation of why FFmpeg failed, or ``""`` if stderr says nothing useful.

    Specific lines beat generic consequence lines, and the ones nearest the first consequence beat
    earlier ones — so the real capture reports the NVENC driver/API mismatch rather than either the
    ``Error while opening encoder`` that followed it or the recovered nvdec warning that preceded it.
    """
    if not stderr:
        return ""
    chosen = _select(str(stderr).splitlines(), max_lines)
    if not chosen:
        return ""
    return bound(" | ".join(chosen), limit)


def describe_output_problem(exists: bool, size: int | None) -> str:
    """Reason for the "FFmpeg said success but produced nothing" case.

    Kept separate from stderr summarising because there is usually no stderr to quote here, and
    inventing one would be worse than saying plainly what was observed.
    """
    if not exists:
        return "FFmpeg reported success but the output file is missing"
    if not size:
        return "FFmpeg reported success but the output file is empty"
    return ""


def describe_exception(exc: BaseException, limit: int = MAX_REASON_CHARS) -> str:
    """One bounded line for an invocation that raised instead of returning a code.

    ``subprocess.TimeoutExpired``'s ``str()`` embeds the whole argv, which is long and useless in a
    status panel, so the timeout is described by its own numbers instead.
    """
    timeout = getattr(exc, "timeout", None)
    if timeout is not None and type(exc).__name__ == "TimeoutExpired":
        return bound(f"FFmpeg timed out after {timeout}s", limit)
    text = str(exc).strip()
    name = type(exc).__name__
    return bound(f"{name}: {text}" if text else name, limit)


__all__ = [
    "MAX_REASON_CHARS",
    "MAX_REASON_LINES",
    "bound",
    "describe_exception",
    "describe_output_problem",
    "summarize_ffmpeg_failure",
]
