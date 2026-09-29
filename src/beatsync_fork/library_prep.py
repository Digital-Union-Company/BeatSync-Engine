#!/usr/bin/env python3
"""Media Library Preparation: plain data, state and the report text.

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
* **(P2) Preparation is media-neutral, and therefore trackless.** Persisted Stage-5 semantics
  describe the video itself, so no track, tempo, section, edit style or creative state reaches a
  cache key — see ``_qwen_config_token`` in ``video_analysis.py``. There is consequently no audio
  input to this workflow, no Stage 1–4 pass during a scan, and nothing here that could bind a
  preparation to one song or one edit style. Music/edit interpretation is downstream, in Stage 6 and
  in any future creative/director layer, and it is ephemeral per render.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from beatsync_fork.input_report import format_seconds

DEFAULT_RECURSIVE = True

SEMANTIC_MODE_TEXT = "media-neutral"
"""What the persisted Stage-5 semantics describe. Not a user-selectable mode — a statement of the
P2 contract, shown so the report never implies track or edit-style dependence."""

INTRO_TEXT = (
    "Choose the library folder, then press Scan Library."
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
    would need a content-addressed library index, which this workflow does not build.
    """

    NEW_OR_CHANGED = "new_or_changed"
    INCOMPLETE_OR_INVALID = "incomplete_or_invalid"


_STATUS_VALUES = {item.value for item in PrepStatus}
_REASON_VALUES = {item.value for item in NeedReason}


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
class RuntimeIdentity:
    """The Stage-5 identity inputs that are shared by every source in one scan.

    These are exactly the values ``analyze_video_sources`` computes once per invocation and threads
    into every ``_cache_path`` call. Binding them into the scan result is what makes P.1 safe: the
    full-library classification stays valid for the Analyze click only while they still hold.

    ``config_token`` covers ``BEATSYNC_QWEN_MAX_WINDOWS``, ``_FRAME_WIDTH`` and ``_MAX_NEW_TOKENS`` —
    the whole of result-affecting media-semantic Qwen configuration after P2 — so there is nothing
    further to bind. In particular there is no edit style to bind any more.
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
    the screen: a user can retype the folder or toggle the recursive box and click Analyze before the
    ``change`` handler has run. The Create Video gate solves exactly this by making the live source
    controls inputs to the render request; preparation does the same.

    After P2 the declaration is the whole of preparation's input surface: folder and recursive.
    """

    folder: str = ""
    recursive: bool = DEFAULT_RECURSIVE

    @classmethod
    def from_widgets(cls, folder: Any, recursive: Any) -> "LivePrepDeclaration":
        """Tolerant of the shapes Gradio hands back (``None``, a path, a non-bool truthy)."""
        return cls(
            folder="" if folder is None else str(folder),
            recursive=bool(recursive),
        )

    def describes(self, scan: "PrepScanResult | None") -> bool:
        """Does this declaration still describe the scan that was recorded?

        Practical equality only: normalised folder, exact ``recursive``. It is deliberately **not**
        an identity check — it runs earlier and cheaper than anything that stats, probes or analyses.
        """
        if scan is None:
            return True
        return (
            _normalized_path(self.folder) == _normalized_path(scan.folder)
            and bool(self.recursive) == bool(scan.recursive)
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
    runtime: RuntimeIdentity
    classifications: tuple[SourceClassification, ...] = ()
    supported_count: int = 0
    folder_scan_seconds: float = 0.0
    classify_seconds: float = 0.0
    cache_identity_seconds: float = 0.0
    cache_lookup_seconds: float = 0.0

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
            f"Semantic mode:     {SEMANTIC_MODE_TEXT}",
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
            f"identity {format_seconds(self.cache_identity_seconds)}, "
            f"records {format_seconds(self.cache_lookup_seconds)}"
        )
        lines.append("")
        if needs:
            lines.append(f"Ready to analyze {needs} video(s).")
        elif self.supported_count:
            lines.append("Everything in this library is prepared.")
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


def record_scan(state: PrepSessionState, scan: PrepScanResult) -> PrepSessionState:
    if not scan.runtime.is_usable():
        notice = "Preparation unavailable — Qwen backend identity could not be verified."
    elif scan.needs_analysis_count:
        notice = f"{scan.needs_analysis_count} video(s) need analysis."
    else:
        notice = "Library is fully prepared."
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
    the recursive flag and still have the *previous* library's classification analysed — the same
    race the Create Video gate takes live source controls to avoid.

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

    Deliberately small. The cheap checks are: a recorded scan exists and has work, and the runtime
    identity the scan was classified under still holds. Per-source staleness is **not** checked —
    ``analyze_video_sources`` re-derives each selected source's own identity anyway, so a file
    changed since the scan simply analyses under its new key and one that became prepared is an
    ordinary cache hit.
    """
    scan = state.scan
    if scan is None:
        return "No library scan recorded. Press Scan Library first."
    if not scan.runtime.is_usable():
        return BACKEND_UNVERIFIED_TEXT
    if not scan.subset_for_analysis():
        return "Nothing to analyze — every scanned source is already prepared."
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
    runtime: RuntimeIdentity,
    classification_items: Iterable[Any],
    supported_count: int,
    folder_scan_seconds: float = 0.0,
    classify_seconds: float = 0.0,
    cache_identity_seconds: float = 0.0,
    cache_lookup_seconds: float = 0.0,
) -> PrepScanResult:
    """Assemble a scan result from the runtime classifier's plain output.

    Kept here rather than in the GUI so the whole shape is constructible — and testable — without
    Gradio.
    """
    return PrepScanResult(
        folder=str(folder),
        recursive=bool(recursive),
        runtime=runtime,
        classifications=tuple(
            SourceClassification.from_mapping(item) for item in (classification_items or ())
        ),
        supported_count=int(supported_count),
        folder_scan_seconds=float(folder_scan_seconds),
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
    "SEMANTIC_MODE_TEXT",
    "STALE_DECLARATION_TEXT",
    "SourceClassification",
    "analyze_refusal",
    "build_scan_result",
    "declaration_refusal",
    "initial_state",
    "record_analysis_complete",
    "record_failure",
    "record_scan",
    "runtime_identity_from_classification",
    "set_folder",
    "set_recursive",
    "summarize_analysis_run",
]
