#!/usr/bin/env python3
"""Presentation of an :class:`~beatsync_fork.input_manager.InputSet`.

Kept separate from scanning so the numbers have exactly one producer and this module stays a pure
function of them. :meth:`InputReport.as_dict` is the machine-readable form later phases (run manifest,
edit plan) can embed; :meth:`InputReport.render_text` is the human-readable block a UI or console can
show.

Deliberately UI-framework agnostic — no Gradio import, no widgets. Status: **not wired into
``gui.py``.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from beatsync_fork.input_manager import InputSet, RejectReason

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(size: int) -> str:
    """Human-readable byte size using 1024-based units."""
    value = float(max(0, int(size)))
    for unit in _BYTE_UNITS:
        if value < 1024.0 or unit == _BYTE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} {_BYTE_UNITS[-1]}"  # pragma: no cover - loop always returns


def format_seconds(seconds: float) -> str:
    """One-decimal seconds for the report block.

    [FORK] Digital-Union (L0). Deliberately total: a report is a diagnostic, so a non-numeric or
    non-finite value renders as ``0.0s`` rather than raising inside a UI callback. Negatives clamp
    for the same reason — the module stays a pure, deterministic function of the scan it describes.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "0.0s"
    if value != value or value in (float("inf"), float("-inf")):
        return "0.0s"
    return f"{max(0.0, value):.1f}s"


@dataclass(frozen=True, slots=True)
class InputReport:
    """Flat, serialisable summary of one folder scan."""

    root: str
    recursive: bool
    discovered: int
    supported: int
    ready: int
    rejected: int
    rejected_by_reason: dict[str, int]
    path_collisions: int
    duplicate_groups: int
    duplicate_extra_files: int
    fingerprint_errors: int
    duplicates_checked: bool
    total_ready_bytes: int
    scan_seconds: float
    is_ready: bool

    @classmethod
    def from_input_set(cls, input_set: InputSet) -> "InputReport":
        by_reason = input_set.rejected_by_reason()
        return cls(
            root=input_set.root,
            recursive=input_set.recursive,
            discovered=input_set.discovered_count,
            supported=input_set.supported_count,
            ready=input_set.ready_count,
            rejected=input_set.rejected_count,
            rejected_by_reason={
                reason.value: count for reason, count in by_reason.items() if count
            },
            path_collisions=input_set.path_collisions,
            duplicate_groups=len(input_set.duplicate_groups),
            duplicate_extra_files=input_set.duplicate_extra_files,
            fingerprint_errors=len(input_set.fingerprint_errors),
            duplicates_checked=input_set.duplicates_checked,
            total_ready_bytes=input_set.total_ready_bytes,
            scan_seconds=input_set.scan_seconds,
            is_ready=input_set.is_ready(),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for run manifests and edit plans."""
        return {
            "root": self.root,
            "recursive": self.recursive,
            "discovered": self.discovered,
            "supported": self.supported,
            "ready": self.ready,
            "rejected": self.rejected,
            "rejected_by_reason": dict(self.rejected_by_reason),
            "path_collisions": self.path_collisions,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_extra_files": self.duplicate_extra_files,
            "fingerprint_errors": self.fingerprint_errors,
            "duplicates_checked": self.duplicates_checked,
            "total_ready_bytes": self.total_ready_bytes,
            "scan_seconds": round(self.scan_seconds, 3),
            "is_ready": self.is_ready,
        }

    def render_text(self) -> str:
        """Fixed-width summary block.

        Reports counts only. It never asserts that a count matches the user's intent — that
        confirmation belongs to the caller, and is the point of the whole design.
        """
        scope = "recursive" if self.recursive else "top level only"
        lines = [
            f"Folder:      {self.root}  ({scope})",
            f"Discovered:  {self.discovered}",
            f"Supported:   {self.supported}",
            f"Rejected:    {self.rejected}{self._reason_suffix()}",
            f"Ready:       {self.ready}",
        ]

        if self.duplicates_checked:
            lines.append(
                f"Duplicates:  {self.duplicate_groups} group(s), "
                f"{self.duplicate_extra_files} extra copy/copies (reported, not removed)"
            )
        else:
            lines.append("Duplicates:  not checked")

        if self.fingerprint_errors:
            lines.append(
                f"Unhashable:  {self.fingerprint_errors} "
                f"(duplication unknown; still counted as ready)"
            )
        if self.path_collisions:
            lines.append(f"Same file seen twice: {self.path_collisions} (counted once)")

        lines.append(f"Total size:  {format_bytes(self.total_ready_bytes)}")
        # [FORK] Digital-Union (L0): the scan cost was already measured and serialised but never
        # shown. It is the first thing that grows with the library, so a user aiming at 5,000+
        # sources can see it without instrumenting anything.
        lines.append(f"Scan time:   {format_seconds(self.scan_seconds)}")
        lines.append("")
        lines.append("INPUT READY" if self.is_ready else "NO USABLE SOURCE FILES")
        return "\n".join(lines)

    def _reason_suffix(self) -> str:
        if not self.rejected_by_reason:
            return ""
        ordered = [
            f"{reason.value}={self.rejected_by_reason[reason.value]}"
            for reason in RejectReason
            if reason.value in self.rejected_by_reason
        ]
        return "  (" + ", ".join(ordered) + ")"


def render_input_set(input_set: InputSet) -> str:
    """Convenience: build a report from an :class:`InputSet` and render it."""
    return InputReport.from_input_set(input_set).render_text()


__all__ = ["InputReport", "format_bytes", "format_seconds", "render_input_set"]
