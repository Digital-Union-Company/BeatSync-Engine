"""Fork identity surface."""

from __future__ import annotations

import re

import beatsync_fork


def test_identity_constants_present():
    assert beatsync_fork.FORK_NAME == "Digital-Union-Company/BeatSync-Engine"
    assert beatsync_fork.UPSTREAM_PROJECT == "Merserk/BeatSync-Engine"
    assert re.fullmatch(r"\d+\.\d+\.\d+", beatsync_fork.FORK_VERSION)
    assert re.fullmatch(r"[0-9a-f]{40}", beatsync_fork.UPSTREAM_BASELINE_COMMIT)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", beatsync_fork.FORK_MODIFICATIONS_BEGAN)


def test_fork_identity_line_is_usable_in_one_line():
    line = beatsync_fork.fork_identity()
    assert "\n" not in line
    assert beatsync_fork.FORK_VERSION in line
    assert beatsync_fork.UPSTREAM_BASELINE_COMMIT[:7] in line


def test_public_surface_is_small():
    """This package is intentionally minimal: identity only, no behaviour."""
    assert set(beatsync_fork.__all__) == {
        "FORK_MODIFICATIONS_BEGAN",
        "FORK_NAME",
        "FORK_VERSION",
        "UPSTREAM_BASELINE_COMMIT",
        "UPSTREAM_PROJECT",
        "fork_identity",
    }
