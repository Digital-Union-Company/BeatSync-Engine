"""[FORK] R8 Owner Fixes (P0): Confirm must become usable on EVERY page, not only the first.

THE DEFECT, as the owner hit it and as it was then reproduced here with 205 synthetic clips at
page size 100:

    batch 1   12 kept, 5 rejected, 83 unmarked -> Confirm -> Proceed -> 100/100 sorted
    batch 2   loads automatically, operator marks clips, counter reads "Kept 18 ... Undecided 0"
              and the hint says Confirm is being enabled from the server's record
              -> CONFIRM STAYS DISABLED, and the operator cannot continue

The record was never wrong. Pressing "Re-read the server's record" by hand enabled the button
instantly, which is what identified the fault as *the server was never asked* rather than *the
server answered no*.

MECHANISM. Only Refresh moves the button, and the gallery script asks for a Refresh from exactly
one place -- `syncConfirm`, which returns early when the new readiness answer equals the last one
it saw. `readyState` was a bare boolean with no notion of which page it described, so:

    end of batch 1    readyState = true          (clips are kept)
    page transition   the resync that should have reset it raced the in-place grid swap
    batch 2, 1st keep syncConfirm(true) sees true === true -> RETURNS WITHOUT ASKING

and the button stays exactly as `_on_clips_scan` left it when the new page was published, which
for an all-undecided page is disabled.

THE FIX, asserted below: readiness is keyed to the live "session/page", every server reply is
tagged with the page it was requested for, and the repaint observer also watches the nonce
attribute and does not consume a nonce whose resync failed.

Why these are text assertions over the real `CLIPS_GALLERY_JS`: it is a JS blob inside `gui.py`,
which cannot be imported on a bare interpreter. The behavioural proof is a browser run -- recorded
in `docs/claude/r8-ux-refresh.md` -- and these pins exist so the specific lines that proof depends
on cannot be reverted silently.
"""

from __future__ import annotations

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


@pytest.fixture(scope="module")
def javascript() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index('CLIPS_GALLERY_JS = r"""')
    return source[start:source.index('""".replace(', start)]


def _body(javascript: str, name: str) -> str:
    """The source of one function in the blob, from its header to the next top-level one."""
    start = javascript.index(name)
    rest = javascript[start + len(name):]
    following = re.search(r"\n  (?:async )?function |\n  window\.__|\n  const host", rest)
    return rest[:following.start()] if following else rest


# =============================================================================================
# Readiness belongs to a page, not to the session
# =============================================================================================

def test_readiness_is_keyed_to_the_page_it_was_measured_on(javascript: str):
    """The whole fix. Without `readyKey`, the equality test in `syncConfirm` can be satisfied by
    an answer about a batch that is no longer on screen."""
    assert "let readyKey" in javascript, "readiness has no page identity"
    body = _body(javascript, "function syncConfirm(")
    assert "readyKey" in body, "syncConfirm ignores which page the answer belongs to"
    assert "key === readyKey && ready === readyState" in body, (
        "the early return must require BOTH the same page and the same answer; comparing the "
        "answer alone is the defect")


def test_a_page_with_no_grid_is_not_treated_as_a_page(javascript: str):
    """`pageKey()` is null between renders. Recording readiness against null would make the next
    real page compare equal to it."""
    body = _body(javascript, "function syncConfirm(")
    assert "if (key === null) { return; }" in body


def test_the_page_key_is_read_from_the_live_grid(javascript: str):
    """Not cached: the grid is morphed in place, so a remembered key would describe the DOM as it
    was rather than as it is."""
    body = _body(javascript, "function pageKey(")
    assert "grid()" in body
    assert "dataset.session" in body and "dataset.page" in body


# =============================================================================================
# A reply may only be applied to the page it was requested for
# =============================================================================================

def test_counts_carry_the_page_they_describe(javascript: str):
    body = _body(javascript, "function paintCounts(")
    assert "function paintCounts(counts, key)" in javascript, "paintCounts takes no page"
    assert "key !== pageKey()" in body, "a reply that outlived its page must be dropped"


def test_a_decision_reply_is_tagged_before_the_request_goes_out(javascript: str):
    """Read after the await and the grid may already have been swapped, which is exactly the
    window this guard exists to close."""
    body = _body(javascript, "async function send(")
    key_line = body.index("const key = g.dataset.session")
    assert key_line < body.index("await fetch("), "the page is captured after the request"
    assert "paintCounts(body.counts, key)" in body


def test_a_resync_reply_is_tagged_and_checked(javascript: str):
    body = _body(javascript, "window.__r5Resync =")
    assert body.index("const key = g.dataset.session") < body.index("await fetch("), \
        "the page is captured after the request"
    assert "page_changed" in body, "a reply for a superseded page must be refused"
    assert "paintCounts(body.counts, key)" in body


# =============================================================================================
# The repaint observer
# =============================================================================================

def test_the_observer_watches_the_nonce_attribute_too(javascript: str):
    """Measured in Chrome: Gradio morphs this grid in place -- the SAME `.r5-grid` element
    survives a republish -- so the nonce that exists to make a repaint detectable arrives as an
    attribute mutation. A childList-only subscription can miss a repaint whose structure happens
    to match, and a missed repaint is a missed readiness reset."""
    assert "attributeFilter" in javascript
    for watched in ("data-nonce", "data-page", "data-session"):
        assert f"'{watched}'" in javascript, watched


def test_a_failed_resync_does_not_consume_the_nonce(javascript: str):
    """Pinning `lastNonce` for a resync that returned `ok:false` discards the one later mutation
    that could have retried it."""
    body = _body(javascript, "async function resyncIfRepainted(")
    assert "lastNonce = null" in body, "a failed resync must release the nonce"
    assert "body.ok" in body


def test_a_new_page_clears_the_readiness_memory(javascript: str):
    body = _body(javascript, "async function resyncIfRepainted(")
    assert "readyKey = null" in body and "readyState = null" in body


# =============================================================================================
# What must NOT have changed
# =============================================================================================

def test_the_client_still_never_touches_the_button(javascript: str):
    """The server-authoritative model is the reason this defect was a stuck button rather than a
    wrong one. The client asks; it does not decide. A `.disabled =` here would make the counter
    text an authority."""
    assert ".disabled" not in javascript, "the gallery script must not write button state"
    assert "clips-refresh-button" in javascript, "it asks by pressing the operator's own control"


def test_the_refresh_request_is_still_coalesced(javascript: str):
    """A burst of fast decisions must still collapse into one read, or a 100-tile page turns a
    sweep of clicks into a hundred server round-trips."""
    body = _body(javascript, "function syncConfirm(")
    assert "clearTimeout(syncTimer)" in body
    assert "setTimeout(" in body


def test_manual_re_read_survives(javascript: str):
    """It is the documented recovery route, and it is what diagnosed this defect."""
    assert "window.__r5Resync" in javascript
