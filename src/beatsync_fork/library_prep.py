#!/usr/bin/env python3
"""Media Library Preparation (P V1): plain data, state and the report text.

The preparation workflow lets a user analyse new or changed sources **before** rendering, so a later
render finds a warm Stage-5 cache instead of discovering the work mid-run.

This module owns everything that can be decided without the runtime: the classification vocabulary,
the immutable scan result, the session state machine and the rendered report. It is stdlib-only by
the hard rule in CLAUDE.md, so it deliberately knows **nothing** about cache identity — it never
computes a cache key, never opens a cache record and never decides what "complete" means.
``video_analysis.classify_library_sources`` answers those questions with the production Stage-5
primitives and hands the verdicts here as plain strings.

Two boundaries are load-bearing:

* **Preparation is a separate workflow from the Create Video confirmation gate.** Nothing here
  touches :mod:`beatsync_fork.input_session`, its ``SourceSnapshot`` identity or the render gate. A
  preparation change must never clear a render confirmation or enable/disable Create Music Video.
* **The stored audio profile is a serialisable snapshot, not a live object.** Gradio round-trips
  state between events, so the second button click must not depend on object identity with the dict
  Stage 4 produced. :func:`profile_snapshot` makes an independent copy of plain containers and
  scalars, which is exactly what ``audio_visual_profile`` contains.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from beatsync_fork.input_report import format_seconds

DEFAULT_RECURSIVE = True

INTRO_TEXT = (
    "Choose the library folder and the track you will render with, then press Scan Library."
)

BACKEND_UNVERIFIED_TEXT = (
    "Cannot verify Qwen backend identity.\n"
    "Preparation analysis is unavailable: Stage 5 would run semantic analysis that cannot be "
    "cached, so the same sources would need analysing again on the next run.\n"
    "Check the llama.cpp binaries and the GGUF model files, then scan again."
)

RESCAN_HINT = "Press Scan Library to refresh the prepared / needs-analysis counts."


class PrepStatus(str, Enum):
    """What preparation knows about one source.

    ``SOURCE_IDENTITY_UNAVAILABLE`` mirrors Stage 5's fail-closed rule: when a source cannot be
    stat'ed or fingerprinted, ``_cache_path`` returns ``None`` and that source gets neither a lookup
    nor a write. It is therefore neither prepared nor usefully analysable, and it must not be hidden
    inside either of the other two counts.
    """

    PREPARED = "prepared"
    NEEDS_ANALYSIS = "needs_analysis"
    SOURCE_IDENTITY_UNAVAILABLE = "source_identity_unavailable"


class NeedReason(str, Enum):
    """Why a source needs analysis. Reporting detail only — both reasons select the same work.

    ``NEW_OR_CHANGED`` is deliberately one combined label. Cache identity is path + size +
    ``st_mtime_ns`` + content fingerprint, so a changed file simply re-keys and its new key has no
    record — indistinguishable from a file that was never analysed. Claiming to tell those apart
    would need a content-addressed library index, which V1 does not build.
    """

    NEW_OR_CHANGED = "new_or_changed"
    INCOMPLETE_OR_INVALID = "incomplete_or_invalid"


_STATUS_VALUES = {item.value for item in PrepStatus}
_REASON_VALUES = {item.value for item in NeedReason}


def profile_snapshot(profile: Any) -> dict:
    """An independent, serialisable copy of the Stage-4 audio profile.

    ``audio_visual_profile`` holds only plain scalars and a list of section-type strings (every
    numeric field is already coerced with ``float()``/``int()`` where it is built), so a structural
    copy is faithful. Anything unexpected degrades to ``str`` rather than raising: a report must
    never be able to fail because a future profile field carried an exotic type.
    """
    return _snapshot_value(profile) if isinstance(profile, Mapping) else {}


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


@dataclass(frozen=True, slots=True)
class SourceClassification:
    """One source's verdict, as produced by the runtime classifier."""

    path: str
    status: PrepStatus
    reason: str = ""
    """A :class:`NeedReason` value for ``NEEDS_ANALYSIS``; empty otherwise."""

    @classmethod
    def from_mapping(cls, item: Any) -> "SourceClassification":
        """Build from the classifier's plain-dict output, defaulting unknown verdicts to unavailable.

        The classifier and this module share the enum *values* rather than the objects, because
        ``video_analysis`` may not be importable wherever this state is inspected. An unrecognised
        verdict is treated as unavailable: that is the conservative direction, since it never sends a
        source to analysis on the strength of a string nobody recognises.
        """
        mapping = item if isinstance(item, Mapping) else {}
        raw_status = str(mapping.get("status", ""))
        status = (PrepStatus(raw_status) if raw_status in _STATUS_VALUES
                  else PrepStatus.SOURCE_IDENTITY_UNAVAILABLE)
        raw_reason = str(mapping.get("reason", ""))
        reason = raw_reason if raw_reason in _REASON_VALUES else ""
        return cls(path=str(mapping.get("path", "")), status=status, reason=reason)


@dataclass(frozen=True, slots=True)
class TrackIdentity:
    """The stale-scan guard for the chosen track. **Not** a cache key.

    Stage-5 cache identity is untouched by preparation: only the *derived* ``smart_preset`` ever
    reaches a cache key, through ``_qwen_config_token``. This record exists so the UI can notice that
    the user swapped or re-saved the audio file between Scan and Analyze, which would change the
    derived profile and therefore the whole classification.
    """

    path: str
    size: int
    mtime_ns: int
    smart_preset: str

    @classmethod
    def from_file(cls, path: str, smart_preset: str) -> "TrackIdentity | None":
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return cls(
            path=os.path.abspath(path),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            smart_preset=str(smart_preset),
        )

    def still_matches(self) -> bool:
        """Re-stat the track and compare. ``False`` when it moved, changed or became unreadable."""
        current = TrackIdentity.from_file(self.path, self.smart_preset)
        return current is not None and current == self


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """The Stage-5 identity inputs that are shared by every source in one scan.

    These are exactly the values ``analyze_video_sources`` computes once per invocation and threads
    into every ``_cache_path`` call. Binding them into the scan result is what makes P.1 safe: the
    full-library classification stays valid for the Analyze click only while they still hold.

    ``config_token`` already covers ``BEATSYNC_QWEN_MAX_WINDOWS``, ``_FRAME_WIDTH``,
    ``_MAX_NEW_TOKENS`` **and** the resolved ``smart_preset``, so there is nothing further to bind.
    """

    qwen_enabled: bool
    ai_available: bool
    ai_cache_disabled: bool
    backend_token: str = ""
    """Empty when AI is off (the ``no_ai`` identity) or when it could not be proven."""

    config_token: str = ""
    model_path: str = ""

    def is_usable(self) -> bool:
        """Can preparation produce durable cache records in this runtime state?

        ``ai_cache_disabled`` is the one blocking case, and it is narrower than "no AI": a
        legitimately AI-disabled run uses the existing ``no_ai`` identity and caches perfectly well.
        This is only true when AI *is* available but its strong backend identity could not be proven,
        which is precisely when Stage 5 runs analysis and persists nothing.
        """
        return not self.ai_cache_disabled

    def matches(self, other: "RuntimeIdentity | None") -> bool:
        return other is not None and self == other


def _normalized_path(value: Any) -> str:
    """Compare-ready form of a path widget value. Empty stays empty.

    ``abspath`` + ``normcase`` so a trailing separator, a relative spelling or a drive-letter case
    difference is not read as the user having changed their mind. ``abspath("")`` would be the
    process working directory, which is why the empty case short-circuits.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError):
        return os.path.normcase(text)


@dataclass(frozen=True, slots=True)
class LivePrepDeclaration:
    """What the preparation widgets declare *right now*, at the moment a button was clicked.

    Gradio delivers widget changes as separate queued events, so the stored state can lag behind
    the screen: a user can retype the folder or pick a different track and click Analyze before the
    ``change`` handler has run. The Create Video gate solves exactly this by making the live source
    controls inputs to the render request; preparation does the same.
    """

    folder: str = ""
    recursive: bool = DEFAULT_RECURSIVE
    track_path: str = ""

    @classmethod
    def from_widgets(cls, folder: Any, recursive: Any, track_path: Any) -> "LivePrepDeclaration":
        """Tolerant of the shapes Gradio hands back (``None``, a path, a non-bool truthy)."""
        return cls(
            folder="" if folder is None else str(folder),
            recursive=bool(recursive),
            track_path="" if track_path is None else str(track_path),
        )

    def describes(self, scan: "PrepScanResult | None") -> bool:
        """Does this declaration still describe the scan that was recorded?

        Practical equality only: normalised folder and track paths, exact ``recursive``. It is
        deliberately **not** an identity check — the track's size/mtime is
        :meth:`TrackIdentity.still_matches`' job, and this guard runs earlier and cheaper, before
        anything is stat'ed, probed or analysed.
        """
        if scan is None:
            return True
        return (
            _normalized_path(self.folder) == _normalized_path(scan.folder)
            and bool(self.recursive) == bool(scan.recursive)
            and _normalized_path(self.track_path) == _normalized_path(scan.track.path)
        )


@dataclass(frozen=True, slots=True)
class PrepScanResult:
    """Everything the Analyze click needs, and nothing that belongs on disk.

    Only paths and identity metadata are retained. Cached video records are never held here: a scan
    of the real 902-source library would otherwise put tens of megabytes of candidate data into
    Gradio session state.
    """

    folder: str
    recursive: bool
    track: TrackIdentity
    audio_profile: dict
    """Serialisable snapshot of ``beat_info["audio_visual_profile"]``, forwarded unchanged to
    ``analyze_video_sources`` at Analyze time."""

    runtime: RuntimeIdentity
    classifications: tuple[SourceClassification, ...] = ()
    supported_count: int = 0
    folder_scan_seconds: float = 0.0
    profile_seconds: float = 0.0
    classify_seconds: float = 0.0
    cache_identity_seconds: float = 0.0
    cache_lookup_seconds: float = 0.0

    @property
    def smart_preset(self) -> str:
        return self.track.smart_preset

    @property
    def prepared_count(self) -> int:
        return sum(1 for item in self.classifications if item.status is PrepStatus.PREPARED)

    @property
    def unavailable_count(self) -> int:
        return sum(1 for item in self.classifications
                   if item.status is PrepStatus.SOURCE_IDENTITY_UNAVAILABLE)

    @property
    def needs_analysis_count(self) -> int:
        return len(self.subset_for_analysis())

    def count_by_reason(self, reason: NeedReason) -> int:
        return sum(1 for item in self.classifications
                   if item.status is PrepStatus.NEEDS_ANALYSIS and item.reason == reason.value)

    def subset_for_analysis(self) -> tuple[str, ...]:
        """The **only** paths Analyze may send to Stage 5.

        This is the P.1 rule in one place: the Scan click paid for classifying the whole library, so
        Analyze must never hand the full list back to ``analyze_video_sources`` just to rediscover
        that almost all of it is warm.
        """
        return tuple(item.path for item in self.classifications
                     if item.status is PrepStatus.NEEDS_ANALYSIS and item.path)

    def can_analyze(self) -> bool:
        return bool(self.runtime.is_usable() and self.subset_for_analysis())

    def render_text(self) -> str:
        lines = [
            "MEDIA LIBRARY PREPARATION",
            "",
            f"Folder:            {self.folder}",
            f"Track:             {os.path.basename(self.track.path) or self.track.path}",
            f"Edit style:        {self.smart_preset or 'n/a'}",
            "",
            f"Supported videos:  {self.supported_count}",
        ]

        if not self.runtime.is_usable():
            lines.extend(["", BACKEND_UNVERIFIED_TEXT])
            return "\n".join(lines)

        needs = self.needs_analysis_count
        lines.append(f"Prepared:          {self.prepared_count}")
        lines.append(f"New / changed:     {needs}{self._reason_suffix()}")
        if self.unavailable_count:
            lines.append(f"Unreadable:        {self.unavailable_count}")
        lines.append(f"Semantic tagging:  {'enabled' if self.runtime.ai_available else 'disabled'}")
        lines.append(
            "Scan time:         "
            f"folder {format_seconds(self.folder_scan_seconds)}, "
            f"track {format_seconds(self.profile_seconds)}, "
            f"identity {format_seconds(self.cache_identity_seconds)}, "
            f"records {format_seconds(self.cache_lookup_seconds)}"
        )
        lines.append("")
        if needs:
            lines.append(f"Ready to analyze {needs} video(s).")
        elif self.supported_count:
            lines.append("Everything in this library is already prepared for this edit style.")
        else:
            lines.append("No supported videos found in this folder.")
        if self.unavailable_count:
            lines.append(
                f"{self.unavailable_count} file(s) could not be read; they are neither prepared "
                "nor analysable."
            )
        return "\n".join(lines)

    def _reason_suffix(self) -> str:
        new_or_changed = self.count_by_reason(NeedReason.NEW_OR_CHANGED)
        incomplete = self.count_by_reason(NeedReason.INCOMPLETE_OR_INVALID)
        if new_or_changed and incomplete:
            return f"  ({new_or_changed} new/changed, {incomplete} incomplete)"
        if incomplete and not new_or_changed:
            return "  (incomplete previous analysis)"
        return ""


@dataclass(frozen=True, slots=True)
class PrepSessionState:
    """Preparation UI state. Entirely separate from the Create Video source confirmation."""

    folder: str = ""
    recursive: bool = DEFAULT_RECURSIVE
    track_path: str = ""
    scan: PrepScanResult | None = None
    report_text: str = INTRO_TEXT
    notice: str = ""
    """Short one-line status shown under the buttons; never the authority on anything."""

    def can_analyze(self) -> bool:
        return self.scan is not None and self.scan.can_analyze()

    def analyze_button_label(self) -> str:
        if self.scan is None:
            return "⚙️ Analyze New / Changed"
        count = self.scan.needs_analysis_count
        if not count or not self.scan.runtime.is_usable():
            return "⚙️ Analyze New / Changed"
        return f"⚙️ Analyze {count} new / changed video{'s' if count != 1 else ''}"

    def subset_for_analysis(self) -> tuple[str, ...]:
        return () if self.scan is None else self.scan.subset_for_analysis()


# ---------------------------------------------------------------------------
# Transitions — every one returns a NEW state
# ---------------------------------------------------------------------------


def initial_state() -> PrepSessionState:
    return PrepSessionState()


def _invalidated(state: PrepSessionState, notice: str, **changes) -> PrepSessionState:
    """Apply changes and drop the recorded scan. Any classification input change lands here."""
    return replace(state, scan=None, report_text=INTRO_TEXT, notice=notice, **changes)


def set_folder(state: PrepSessionState, folder: str) -> PrepSessionState:
    return _invalidated(
        state, "Library folder changed. Scan Library again.",
        folder="" if folder is None else str(folder),
    )


def set_recursive(state: PrepSessionState, recursive: bool) -> PrepSessionState:
    return _invalidated(
        state, "Subfolder setting changed. Scan Library again.", recursive=bool(recursive)
    )


def set_track(state: PrepSessionState, track_path: str) -> PrepSessionState:
    return _invalidated(
        state, "Track changed. Scan Library again.",
        track_path="" if track_path is None else str(track_path),
    )


def record_scan(state: PrepSessionState, scan: PrepScanResult) -> PrepSessionState:
    if not scan.runtime.is_usable():
        notice = "Preparation unavailable — Qwen backend identity could not be verified."
    elif scan.needs_analysis_count:
        notice = f"{scan.needs_analysis_count} video(s) need analysis."
    else:
        notice = "Library is fully prepared for this edit style."
    return replace(state, scan=scan, report_text=scan.render_text(), notice=notice)


def record_failure(state: PrepSessionState, message: str,
                   notice: str = "Scan failed.") -> PrepSessionState:
    """A scan or a run that could not proceed: no result is recorded, so Analyze stays disabled.

    Dropping the scan is deliberate for a refused Analyze too. Every refusal
    :func:`analyze_refusal` can produce means the recorded classification no longer describes
    reality, so leaving it in place would only let the user click into the same refusal again.
    """
    return replace(state, scan=None, report_text=f"❌ {message}", notice=notice)


def record_analysis_complete(state: PrepSessionState, summary: str) -> PrepSessionState:
    """Drop the recorded scan after a preparation run and show what the run did.

    The scan must not survive: its counts describe the library as it was *before* the analysis, so
    keeping them would claim work is still outstanding that has just been done. Re-scanning is the
    user's explicit choice — preparation never silently re-classifies a 902-source library.
    """
    return replace(state, scan=None, report_text=summary, notice="Preparation run finished.")


# ---------------------------------------------------------------------------
# The Analyze gate
# ---------------------------------------------------------------------------


STALE_DECLARATION_TEXT = (
    "Preparation inputs changed since the scan. Press Scan Library again."
)


def declaration_refusal(state: PrepSessionState,
                        live: LivePrepDeclaration | None) -> str | None:
    """The stale-UI guard, run **before** anything is stat'ed, probed or analysed.

    ``live`` carries the widget values submitted with the Analyze click. Without it the recorded
    scan is the only authority, and a queued ``change`` event lets the user retarget the folder or
    the track and still have the *previous* library's classification analysed — the same race the
    Create Video gate takes live source controls to avoid.

    A mismatch is a refusal, never a silent re-target: the recorded classification describes a
    different library, and preparation does not rescan 902 sources on the user's behalf.

    Returns ``None`` when there is nothing to compare (no scan recorded, or no live declaration
    supplied by a caller that has no widgets), leaving :func:`analyze_refusal` to report that.
    """
    if state.scan is None or live is None:
        return None
    return None if live.describes(state.scan) else STALE_DECLARATION_TEXT


def analyze_refusal(state: PrepSessionState, runtime: RuntimeIdentity | None) -> str | None:
    """Why this preparation run must not start, or ``None`` when it may.

    Deliberately small. The three cheap checks are: a recorded scan exists and has work; the track
    on disk is still the one it was classified against; and the runtime identity the scan was
    classified under still holds. Per-source staleness is **not** checked — ``analyze_video_sources``
    re-derives each selected source's own identity anyway, so a file changed since the scan simply
    analyses under its new key and one that became prepared is an ordinary cache hit.
    """
    scan = state.scan
    if scan is None:
        return "No library scan recorded. Press Scan Library first."
    if not scan.runtime.is_usable():
        return BACKEND_UNVERIFIED_TEXT
    if not scan.subset_for_analysis():
        return "Nothing to analyze — every scanned source is already prepared."
    if not scan.track.still_matches():
        return "The track changed since the scan. Press Scan Library again."
    if runtime is not None and not scan.runtime.matches(runtime):
        return (
            "The Qwen backend or analysis configuration changed since the scan, so the scanned "
            "results no longer apply. Press Scan Library again."
        )
    return None


def build_scan_result(
    *,
    folder: str,
    recursive: bool,
    track: TrackIdentity,
    audio_profile: Any,
    runtime: RuntimeIdentity,
    classification_items: Iterable[Any],
    supported_count: int,
    folder_scan_seconds: float = 0.0,
    profile_seconds: float = 0.0,
    classify_seconds: float = 0.0,
    cache_identity_seconds: float = 0.0,
    cache_lookup_seconds: float = 0.0,
) -> PrepScanResult:
    """Assemble a scan result from the runtime classifier's plain output.

    Kept here rather than in the GUI so the whole shape is constructible — and testable — without
    Gradio, and so the profile is snapshotted exactly once, at the boundary.
    """
    return PrepScanResult(
        folder=str(folder),
        recursive=bool(recursive),
        track=track,
        audio_profile=profile_snapshot(audio_profile),
        runtime=runtime,
        classifications=tuple(
            SourceClassification.from_mapping(item) for item in (classification_items or ())
        ),
        supported_count=int(supported_count),
        folder_scan_seconds=float(folder_scan_seconds),
        profile_seconds=float(profile_seconds),
        classify_seconds=float(classify_seconds),
        cache_identity_seconds=float(cache_identity_seconds),
        cache_lookup_seconds=float(cache_lookup_seconds),
    )


def runtime_identity_from_classification(result: Any, *, qwen_enabled: bool) -> RuntimeIdentity:
    """Read the invocation-scoped identity back out of ``classify_library_sources``' result."""
    mapping = result if isinstance(result, Mapping) else {}
    return RuntimeIdentity(
        qwen_enabled=bool(qwen_enabled),
        ai_available=bool(mapping.get("ai_available")),
        ai_cache_disabled=bool(mapping.get("ai_cache_disabled")),
        backend_token=str(mapping.get("backend_token") or ""),
        config_token=str(mapping.get("config_token") or ""),
        model_path=str(mapping.get("qwen_model_path") or ""),
    )


def summarize_analysis_run(result: Any) -> str:
    """One short block describing what a preparation run actually did.

    Sourced exclusively from the R1 ``*_this_run`` fields. The unsuffixed ``qwen_*`` aggregates in
    the same payload sum over every returned record including cache hits, so quoting them here would
    reproduce the exact reporting defect R1 fixed — a preparation run of 4 sources reporting the
    whole library's historical tag count.
    """
    mapping = result if isinstance(result, Mapping) else {}
    analyzed = _int(mapping.get("sources_analyzed_this_run"))
    jobs = _int(mapping.get("qwen_jobs_this_run"))
    tags = _int(mapping.get("qwen_tag_count_this_run"))
    requested = _int(mapping.get("qwen_requested_count_this_run"))
    incomplete = _int(mapping.get("qwen_incomplete_jobs_this_run"))

    lines = ["PREPARATION RUN COMPLETE", "", f"Sources analyzed:  {analyzed}"]
    if jobs:
        detail = f"Qwen:              {jobs} job(s), {tags}/{requested} tags"
        if incomplete:
            detail += f", {incomplete} incomplete (not cached)"
        lines.append(detail)
    else:
        lines.append("Qwen:              no inference this run")
    lines.append(f"Analysis time:     {format_seconds(mapping.get('analysis_seconds'))}")
    lines.extend(["", RESCAN_HINT])
    return "\n".join(lines)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "BACKEND_UNVERIFIED_TEXT",
    "DEFAULT_RECURSIVE",
    "INTRO_TEXT",
    "LivePrepDeclaration",
    "NeedReason",
    "PrepScanResult",
    "PrepSessionState",
    "PrepStatus",
    "RESCAN_HINT",
    "RuntimeIdentity",
    "STALE_DECLARATION_TEXT",
    "SourceClassification",
    "TrackIdentity",
    "analyze_refusal",
    "build_scan_result",
    "declaration_refusal",
    "initial_state",
    "profile_snapshot",
    "record_analysis_complete",
    "record_failure",
    "record_scan",
    "runtime_identity_from_classification",
    "set_folder",
    "set_recursive",
    "set_track",
    "summarize_analysis_run",
]
