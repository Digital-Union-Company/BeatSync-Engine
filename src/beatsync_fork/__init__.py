#!/usr/bin/env python3
"""Digital Union fork additions for BeatSync Engine.

Everything in this package is fork-specific. Upstream never creates this directory, so new features
land here with (near) zero merge-conflict surface; upstream modules are touched only at small call
sites marked ``# [FORK]``.

**Import rule for this package:** modules here must not import the upstream runtime
(``logger``, ``paths``, ``gradio``, ``cupy``, ``cv2``, ``librosa``, ``numpy``). Fork modules stay
importable — and therefore testable — on a bare Python interpreter without the portable runtime
installed. ``tests/test_no_runtime_dependency.py`` enforces this.
"""

from __future__ import annotations

FORK_NAME = "Digital-Union-Company/BeatSync-Engine"
"""Fork repository, as ``owner/name``."""

FORK_VERSION = "0.1.0"
"""Fork version. Independent of upstream's release tags."""

UPSTREAM_PROJECT = "Merserk/BeatSync-Engine"
"""Upstream repository this fork derives from."""

UPSTREAM_BASELINE_COMMIT = "06679c1f28c6d0b56c901495e52497cccebc744a"
"""Upstream ``main`` commit this fork branched from. Update on a deliberate upstream sync."""

FORK_MODIFICATIONS_BEGAN = "2026-09-26"
"""Date fork modifications began, recorded for AGPL-3.0 section 5(a). See CHANGELOG-FORK.md."""


def fork_identity() -> str:
    """One-line fork identity for console banners, reports and run manifests."""
    return f"{FORK_NAME} v{FORK_VERSION} (fork of {UPSTREAM_PROJECT} @ {UPSTREAM_BASELINE_COMMIT[:7]})"


__all__ = [
    "FORK_MODIFICATIONS_BEGAN",
    "FORK_NAME",
    "FORK_VERSION",
    "UPSTREAM_BASELINE_COMMIT",
    "UPSTREAM_PROJECT",
    "fork_identity",
]
