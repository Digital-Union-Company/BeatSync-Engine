"""R14-A stage A1: the read-only root-state and missing-file model.

The feature is a *refusal* -- it exists so that a Media Library report never calls a file missing
when the drive holding it is merely unplugged -- so this suite is mostly about what the model
declines to conclude.

Three things it asserts that a looser suite would miss:

1. **Each of the four clauses is independently necessary.** The satisfying case is built once and
   then exactly one clause input is broken at a time, five times over. A model that dropped any
   clause would still pass a suite that only tested the happy path.
2. **A provisional finding cannot escape as a confirmation.** ``LeafVerdict.published`` is read
   rather than ``.verdict``, so a consumer that looks before the end-of-pass anchor check sees a
   hold.
3. **Two independent guards keep the direction of travel.** The transition table is mutated at
   runtime and the clause re-check is shown to refuse the promotion anyway -- so these tests
   cannot pass while an ``UNKNOWN`` is being upgraded to ``MISSING_CONFIRMED``.

Every filesystem failure is injected through a synthetic ``OSError`` subclass carrying a chosen
``winerror``, so the Windows-specific branches are deterministic on any interpreter and no network
host is ever probed. The one real-disk reachability test uses a drive letter that is first
*verified absent*; the junction fixtures fall back to synthetic ones when the platform or the
current privileges will not create a real link.

Bans are asserted over AST **calls**, never over source text: prose naming a forbidden operation
has tripped substring bans five times in this repository, and a ban its own explanation cannot
survive forces documentation to be written around a test.
"""

from __future__ import annotations

import ast
import errno
import inspect
import os

import pytest

from beatsync_fork import library_inventory as lib


# ---------------------------------------------------------------------------------------------
# synthetic failures: a chosen winerror, on any platform
# ---------------------------------------------------------------------------------------------

def oserror(cls=FileNotFoundError, code=errno.ENOENT, winerror=None, strerror="synthetic"):
    """An ``OSError`` of ``cls`` whose ``winerror`` is exactly ``winerror``.

    ``OSError.winerror`` is a read-only descriptor on Windows and absent elsewhere, so it cannot be
    assigned on an instance. A subclass carrying a property shadows it in the MRO on every
    platform, which makes the Windows branches testable on a bare interpreter and keeps the
    expected value visible in the test rather than hidden in platform behaviour.
    """
    namespace = {"winerror": property(lambda self, _value=winerror: _value)}
    synthetic = type(f"Synthetic{cls.__name__}", (cls,), namespace)
    return synthetic(code, strerror)


def raiser(exc):
    def _raise(*_args, **_kwargs):
        raise exc
    return _raise


def listing_for(path, names, *, ok=True, anchor_reachable=True, verdict=None):
    """A hand-built :class:`Listing` for ``path``'s parent. No disk involved."""
    parent = os.path.dirname(os.path.abspath(path))
    return lib.Listing(
        directory=parent, real=parent, anchor=lib.anchor_of(parent), ok=ok,
        verdict=verdict or (lib.PRESENT if ok else lib.ACCESS_DENIED),
        names=frozenset(os.path.normcase(n) for n in names),
        anchor_reachable=anchor_reachable)


def reachable_root(path=None):
    return lib.RootState(typed=str(path or "C:\\x"), real=str(path or "C:\\x"),
                         anchor="C:\\", verdict=lib.PRESENT, anchor_reachable=True,
                         exists=True, listed=True)


def lost_root(path=None):
    return lib.RootState(typed=str(path or "C:\\x"), real=str(path or "C:\\x"),
                         anchor="C:\\", verdict=lib.ROOT_UNREACHABLE, anchor_reachable=False)


def absent_drive_letter():
    """A drive letter this machine does not have, verified absent -- or ``None``."""
    for letter in "ZYXWVUTSRQPO":
        if not os.path.isdir(f"{letter}:\\"):
            return letter
    return None


# =============================================================================================
# A. reading one failed observation: winerror is a VETO, never an admission
# =============================================================================================

def test_a_permission_errors_are_refusals_before_anything_else():
    assert lib.classify_os_error(PermissionError(errno.EACCES, "denied")) == "access_denied"
    # ERROR_ACCESS_DENIED arriving as a plain OSError is still a refusal.
    assert lib.classify_os_error(oserror(OSError, errno.EACCES, 5)) == "access_denied"


@pytest.mark.parametrize("winerror,label", [
    (3, "ERROR_PATH_NOT_FOUND"),
    (15, "ERROR_INVALID_DRIVE"),
    (21, "ERROR_NOT_READY"),
    (53, "ERROR_BAD_NETPATH"),
    (54, "ERROR_NETWORK_BUSY"),
    (55, "ERROR_DEV_NOT_EXIST"),
    (59, "ERROR_UNEXP_NET_ERR"),
    (64, "ERROR_NETNAME_DELETED"),
    (67, "ERROR_BAD_NET_NAME"),
    (433, "ERROR_NO_SUCH_DEVICE"),
    (1167, "ERROR_DEVICE_NOT_CONNECTED"),
])
def test_a_recognised_unreachable_codes_veto_a_missing_claim(winerror, label):
    """Every one of these arrives as ENOENT, which is exactly why errno cannot be the gate."""
    exc = oserror(FileNotFoundError, errno.ENOENT, winerror)
    assert exc.errno == errno.ENOENT, "the premise: errno alone cannot tell these apart"
    assert lib.classify_os_error(exc) == "unreachable", label


def test_a_an_absent_name_is_recognised_with_or_without_a_winerror():
    assert lib.classify_os_error(oserror(winerror=2)) == "leaf_absent"
    # No winerror at all (an injected or non-Windows observation): the container evidence is the
    # gate, so this must still be readable as an absent name rather than becoming a hold.
    assert lib.classify_os_error(oserror(winerror=None)) == "leaf_absent"


def test_a_an_enoent_with_an_unrecognised_winerror_is_a_hold_not_a_finding():
    """The code this module has never heard of. It must cost a hold, never a false confirmation."""
    assert lib.classify_os_error(oserror(winerror=9999)) == "unrecognised"


def test_a_everything_else_is_unrecognised():
    assert lib.classify_os_error(OSError(errno.EIO, "io")) == "unrecognised"
    assert lib.classify_os_error(IsADirectoryError(errno.EISDIR, "dir")) == "unrecognised"


# =============================================================================================
# B. a blank path never becomes the working directory
# =============================================================================================

@pytest.mark.parametrize("value", [
    None, 0, [], b"x", "", "   ", "\t\n", '""', '"   "', '" "', "%R14A_NOT_SET_ANYWHERE%x"[:0],
])
def test_b_nothing_that_fails_to_name_a_folder_resolves_to_anything(value):
    assert lib.normalize_root_text(value) == ""


def test_b_an_environment_reference_expanding_to_nothing_is_refused(monkeypatch):
    monkeypatch.setenv("R14A_BLANK", "")
    assert lib.normalize_root_text("%R14A_BLANK%") == ""
    monkeypatch.setenv("R14A_SPACES", "   ")
    assert lib.normalize_root_text("%R14A_SPACES%") == ""


def test_b_a_blank_root_is_not_inspected_and_never_abspathed():
    state = lib.probe_root("   ")
    assert state.verdict == lib.NOT_INSPECTED
    assert state.typed == "" and state.real == ""
    assert os.getcwd().lower() not in (state.real or "").lower()


def test_b_a_blank_leaf_is_not_inspected():
    verdict = lib.classify_leaf('""', lib.Listing())
    assert verdict.published == lib.NOT_INSPECTED
    assert verdict.path == ""


def test_b_the_rule_is_the_same_one_the_review_surfaces_already_use():
    """Pinned by agreement rather than by comment.

    ``clips_review.normalize_folder_text`` is the existing authority for "did the operator name a
    folder?". This module restates the rule to stay free of a 3,000-line dependency, so the two
    are compared over a shared table: a future divergence fails here instead of silently handing a
    caller the weaker rule.
    """
    from beatsync_fork import clips_review

    for value in (None, 0, "", "  ", '""', '"  "', "C:\\Library", '"C:\\Library"',
                  " C:\\Library ", "~", "%TEMP%"):
        assert lib.normalize_root_text(value) == clips_review.normalize_folder_text(value), value


# =============================================================================================
# C. containment and anchors
# =============================================================================================

def test_c_containment_is_commonpath_not_a_string_prefix():
    assert lib.is_within(r"C:\media\a.mp4", r"C:\media")
    assert lib.is_within(r"C:\media", r"C:\media")
    assert not lib.is_within(r"C:\media-evil\a.mp4", r"C:\media")
    assert not lib.is_within(r"D:\media\a.mp4", r"C:\media")       # different drives


def test_c_the_anchor_is_the_drive_or_the_share_root():
    assert lib.anchor_of(r"C:\Library\clip.mp4") == "C:" + os.sep
    assert lib.anchor_of(r"\\host\share\Library\clip.mp4") == r"\\host\share" + os.sep
    assert lib.anchor_of("") == ""


# =============================================================================================
# D. probe_root
# =============================================================================================

def test_d_a_populated_reachable_root_is_present(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"x")
    state = lib.probe_root(str(root))
    assert state.verdict == lib.PRESENT
    assert state.exists and state.listed and state.anchor_reachable and state.reachable
    assert state.entry_count == 1
    assert state.dev != -1 and state.ino != -1


def test_d_an_empty_but_reachable_root_stays_distinguishable_from_an_unavailable_one(tmp_path):
    """The single most confusable pair in the whole model.

    "There is nothing in it" is a successful observation; "I could not reach it" is the absence of
    one. A report that merged them would call an unplugged library empty.
    """
    root = tmp_path / "empty"
    root.mkdir()
    empty = lib.probe_root(str(root))
    assert empty.verdict == lib.PRESENT
    assert empty.exists and empty.listed and empty.entry_count == 0
    assert empty.reachable

    unavailable = lib.probe_root(str(root), isdir=lambda _p: False)
    assert unavailable.verdict == lib.ROOT_UNREACHABLE
    assert not unavailable.reachable
    assert empty.verdict != unavailable.verdict


def test_d_a_folder_whose_parent_is_live_is_configured_not_created(tmp_path):
    """The asymmetry that must never be read as "the disk is gone" -- and never as permission to
    create anything."""
    wanted = tmp_path / "live_parent_exists" / ""
    target = str(tmp_path / "NotCreatedYet")
    _ = wanted
    state = lib.probe_root(target)
    assert state.verdict == lib.CONFIGURED_NOT_CREATED
    assert state.anchor_reachable and state.reachable
    assert not state.exists
    assert not os.path.exists(target), "probing created the folder"


def test_d_a_folder_whose_parent_is_also_absent_is_a_hold(tmp_path):
    state = lib.probe_root(str(tmp_path / "no_such_parent" / "deeper"))
    assert state.verdict == lib.UNKNOWN
    assert state.anchor_reachable, "the drive is fine; only the path is not there"


def test_d_a_verified_absent_drive_letter_is_root_unreachable():
    letter = absent_drive_letter()
    if letter is None:
        pytest.skip("every probed drive letter is present on this machine")
    state = lib.probe_root(rf"{letter}:\Library")
    assert state.verdict == lib.ROOT_UNREACHABLE
    assert not state.reachable
    assert state.anchor == f"{letter}:" + os.sep


def test_d_an_unavailable_unc_share_is_root_unreachable_without_touching_a_network():
    """Injected throughout: no host is contacted, and the UNC branch is still exercised."""
    unc = r"\\no-such-host-r14a\share\Library"
    state = lib.probe_root(unc, isdir=lambda _p: False,
                           lstat=raiser(oserror(winerror=53)),
                           realpath=lambda p: p)
    assert state.verdict == lib.ROOT_UNREACHABLE
    assert state.anchor == r"\\no-such-host-r14a\share" + os.sep
    assert not state.reachable


def test_d_a_root_that_refuses_its_listing_is_access_denied_not_missing(tmp_path):
    root = tmp_path / "denied"
    root.mkdir()
    state = lib.probe_root(str(root), listdir=raiser(PermissionError(errno.EACCES, "denied")))
    assert state.verdict == lib.ACCESS_DENIED
    assert state.exists and state.anchor_reachable
    assert not state.listed


def test_d_a_root_that_becomes_unreachable_mid_listing_is_reported_as_such(tmp_path):
    root = tmp_path / "yanked"
    root.mkdir()
    state = lib.probe_root(str(root), listdir=raiser(oserror(winerror=21)))
    assert state.verdict == lib.ROOT_UNREACHABLE
    assert not state.reachable


def test_d_probe_root_creates_nothing_anywhere(tmp_path):
    before = {str(p) for p in tmp_path.rglob("*")}
    for candidate in ("", "   ", str(tmp_path / "nope"), str(tmp_path / "a" / "b" / "c")):
        lib.probe_root(candidate)
    assert {str(p) for p in tmp_path.rglob("*")} == before


# =============================================================================================
# E. the root may itself be a junction; a nested one may not be followed out
# =============================================================================================

def make_junction(link, target):
    """A real NTFS junction, or ``None`` when the platform or privileges will not allow one."""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return str(link)
    except Exception:
        return None


def test_e_a_root_that_is_itself_a_junction_is_followed_not_refused(tmp_path):
    real = tmp_path / "real_library"
    real.mkdir()
    (real / "clip.mp4").write_bytes(b"x")
    link = make_junction(tmp_path / "link_to_library", real)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    state = lib.probe_root(link)
    assert state.verdict == lib.PRESENT, "an explicitly selected junction root must be followed"
    assert state.is_reparse
    assert lib.is_within(state.real, str(real)) or os.path.samefile(state.real, str(real))
    assert state.entry_count == 1


def test_e_a_root_whose_link_will_not_resolve_is_a_hold():
    """Fail closed: a link whose true location is unknown makes every containment check
    meaningless, so the root is not usable rather than optimistically accepted."""

    class _Attrs:
        st_file_attributes = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = 0
        st_dev = 1
        st_ino = 2

    state = lib.probe_root(r"C:\library_link", lstat=lambda _p: _Attrs(),
                           realpath=lambda p: p, isdir=lambda _p: True,
                           listdir=lambda _p: [])
    assert state.verdict == lib.UNKNOWN
    assert state.is_reparse


def test_e_a_nested_junction_pointing_outside_the_root_is_reported_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"x")
    root = tmp_path / "library"
    root.mkdir()
    link = make_junction(root / "escape", outside)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    state = lib.probe_root(str(root))
    assert state.verdict == lib.PRESENT
    decision = lib.may_descend(state, link)
    assert decision.action == lib.SKIP_REPARSE_ESCAPES
    assert not decision.allowed
    assert decision.is_reparse


def test_e_a_nested_junction_pointing_back_inside_is_also_skipped_but_distinguished(tmp_path):
    root = tmp_path / "library"
    inner = root / "inner"
    inner.mkdir(parents=True)
    link = make_junction(root / "loop", inner)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    state = lib.probe_root(str(root))
    decision = lib.may_descend(state, link)
    assert decision.action == lib.SKIP_REPARSE_CONTAINED
    assert not decision.allowed, "contained or not, a reparse point is never entered"
    assert decision.action != lib.SKIP_REPARSE_ESCAPES, "the two cases must stay distinguishable"


def test_e_the_reparse_decision_is_the_same_synthetically(tmp_path):
    """The same two verdicts without any link at all, so the property is pinned on every machine
    rather than only where a junction can be created."""

    class _Reparse:
        st_file_attributes = 0x400
        st_mode = 0
        st_dev = 1
        st_ino = 2

    state = reachable_root(r"C:\library")
    escapes = lib.may_descend(state, r"C:\library\escape", lstat=lambda _p: _Reparse(),
                              realpath=lambda _p: r"C:\elsewhere\target")
    assert escapes.action == lib.SKIP_REPARSE_ESCAPES
    contained = lib.may_descend(state, r"C:\library\loop", lstat=lambda _p: _Reparse(),
                                realpath=lambda _p: r"C:\library\inner")
    assert contained.action == lib.SKIP_REPARSE_CONTAINED


def test_e_an_ordinary_child_inside_the_root_may_be_entered(tmp_path):
    root = tmp_path / "library"
    child = root / "set_01"
    child.mkdir(parents=True)
    state = lib.probe_root(str(root))
    decision = lib.may_descend(state, str(child))
    assert decision.action == lib.ENTER and decision.allowed


def test_e_an_ordinary_child_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    sibling = tmp_path / "library-evil"
    sibling.mkdir()
    state = lib.probe_root(str(root))
    decision = lib.may_descend(state, str(sibling))
    assert decision.action == lib.SKIP_OUTSIDE_ROOT and not decision.allowed


# =============================================================================================
# F. list_directory
# =============================================================================================

def test_f_a_readable_directory_yields_positive_evidence(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"x")
    listing = lib.list_directory(str(tmp_path))
    assert listing.ok and listing.verdict == lib.PRESENT
    assert listing.has_name(str(tmp_path / "CLIP.MP4")), "names compare case-insensitively"
    assert listing.covers(str(tmp_path / "anything.mp4"))


def test_f_an_empty_readable_directory_is_still_positive_evidence(tmp_path):
    listing = lib.list_directory(str(tmp_path))
    assert listing.ok and not listing.names


def test_f_a_refused_directory_is_not_evidence_of_anything(tmp_path):
    listing = lib.list_directory(str(tmp_path),
                                 listdir=raiser(PermissionError(errno.EACCES, "denied")))
    assert not listing.ok
    assert listing.verdict == lib.ACCESS_DENIED
    assert not listing.covers(str(tmp_path / "x.mp4"))


def test_f_an_unreachable_directory_reports_its_anchor_as_gone(tmp_path):
    listing = lib.list_directory(str(tmp_path), listdir=raiser(oserror(winerror=53)))
    assert listing.verdict == lib.ROOT_UNREACHABLE
    assert not listing.anchor_reachable


def test_f_a_listing_of_some_other_directory_does_not_cover_a_path(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    listing = lib.list_directory(str(other))
    assert listing.ok
    assert not listing.covers(str(tmp_path / "clip.mp4")), \
        "evidence about another folder must not be borrowed"


# =============================================================================================
# G. classify_leaf
# =============================================================================================

def test_g_an_observed_regular_file_is_present(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    verdict = lib.classify_leaf(str(path), lib.list_directory(str(tmp_path)))
    assert verdict.published == lib.PRESENT
    assert verdict.is_regular and verdict.is_final


def test_g_something_that_exists_but_is_not_a_regular_file_is_a_hold(tmp_path):
    directory = tmp_path / "not_a_file"
    directory.mkdir()
    verdict = lib.classify_leaf(str(directory), lib.list_directory(str(tmp_path)))
    assert verdict.published == lib.UNKNOWN
    assert not verdict.is_regular
    assert verdict.published != lib.MISSING_CONFIRMED


def test_g_a_positive_observation_wins_over_a_stale_listing(tmp_path):
    """A file created after the listing was taken is PRESENT, not missing: the status read is a
    positive observation and the listing is merely older."""
    path = tmp_path / "late.mp4"
    listing = lib.list_directory(str(tmp_path))            # taken while the folder was empty
    path.write_bytes(b"x")
    verdict = lib.classify_leaf(str(path), listing)
    assert verdict.published == lib.PRESENT


def test_g_the_fully_satisfying_case_is_provisional_and_not_yet_published(tmp_path):
    """Three clauses hold; the fourth cannot be settled yet, so nothing may be claimed."""
    path = tmp_path / "gone.mp4"
    (tmp_path / "other.mp4").write_bytes(b"x")
    verdict = lib.classify_leaf(str(path), lib.list_directory(str(tmp_path)))
    assert verdict.verdict == lib.MISSING_PROVISIONAL
    assert verdict.clauses.first_three
    assert verdict.clauses.anchor_reachable_at_listing
    assert not verdict.clauses.anchor_reachable_at_end
    assert not verdict.is_final
    assert verdict.published == lib.UNKNOWN, \
        "a provisional finding must never be published as a confirmation"
    assert not verdict.confirmed_missing


def test_g_a_vanished_file_is_a_hold_because_the_listing_showed_it(tmp_path):
    """The concurrent-change case. The listing DID show the name, so its absence now is a race,
    not an observation -- and the honest answer is a hold."""
    path = tmp_path / "racing.mp4"
    path.write_bytes(b"x")
    listing = lib.list_directory(str(tmp_path))            # the name IS in this listing
    verdict = lib.classify_leaf(str(path), listing, lstat=raiser(oserror(winerror=2)))
    assert verdict.published == lib.UNKNOWN
    assert verdict.clauses.parent_listed
    assert not verdict.clauses.name_absent
    assert "name_absent" in verdict.clauses.unmet()


def test_g_an_inaccessible_leaf_is_access_denied(tmp_path):
    verdict = lib.classify_leaf(str(tmp_path / "x.mp4"), lib.list_directory(str(tmp_path)),
                                lstat=raiser(PermissionError(errno.EACCES, "denied")))
    assert verdict.published == lib.ACCESS_DENIED


def test_g_an_unreachable_leaf_is_never_missing(tmp_path):
    verdict = lib.classify_leaf(str(tmp_path / "x.mp4"), lib.list_directory(str(tmp_path)),
                                lstat=raiser(oserror(winerror=53)))
    assert verdict.published == lib.ROOT_UNREACHABLE
    assert verdict.published != lib.MISSING_CONFIRMED


# ---------------------------------------------------------------------------------------------
# G2. each clause independently necessary
# ---------------------------------------------------------------------------------------------

def satisfying_case(tmp_path):
    """The one case in which all four clauses can hold. Every test below breaks exactly one."""
    path = str(tmp_path / "gone.mp4")
    (tmp_path / "other.mp4").write_bytes(b"x")
    return path, lib.list_directory(str(tmp_path))


def test_g2_the_control_confirms_only_when_every_clause_holds(tmp_path):
    path, listing = satisfying_case(tmp_path)
    settled = lib.retract_subtree([lib.classify_leaf(path, listing)], reachable_root(tmp_path))
    assert settled[0].published == lib.MISSING_CONFIRMED
    assert settled[0].clauses.all_four
    assert settled[0].is_final
    assert lib.confirmed_missing(settled) == (os.path.abspath(path),)


def test_g2_clause_a_broken_parent_was_never_listed(tmp_path):
    path, _ = satisfying_case(tmp_path)
    denied = lib.list_directory(str(tmp_path),
                                listdir=raiser(PermissionError(errno.EACCES, "denied")))
    settled = lib.retract_subtree([lib.classify_leaf(path, denied)], reachable_root(tmp_path))
    assert settled[0].published != lib.MISSING_CONFIRMED
    assert settled[0].published == lib.UNKNOWN
    assert "parent_listed" in settled[0].clauses.unmet()


def test_g2_clause_b_broken_the_name_was_in_the_listing(tmp_path):
    path, _ = satisfying_case(tmp_path)
    stale = listing_for(path, [os.path.basename(path), "other.mp4"])
    settled = lib.retract_subtree([lib.classify_leaf(path, stale)], reachable_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN
    assert "name_absent" in settled[0].clauses.unmet()


def test_g2_clause_c_broken_the_error_was_not_an_absent_name(tmp_path):
    path, listing = satisfying_case(tmp_path)
    verdict = lib.classify_leaf(path, listing, lstat=raiser(oserror(winerror=3)))
    settled = lib.retract_subtree([verdict], reachable_root(tmp_path))
    assert settled[0].published == lib.ROOT_UNREACHABLE
    assert settled[0].published != lib.MISSING_CONFIRMED
    assert not settled[0].clauses.leaf_absent_error


def test_g2_clause_d1_broken_the_anchor_was_already_gone_when_the_folder_was_listed(tmp_path):
    path, _ = satisfying_case(tmp_path)
    unanchored = listing_for(path, ["other.mp4"], anchor_reachable=False)
    settled = lib.retract_subtree([lib.classify_leaf(path, unanchored)],
                                  reachable_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN
    assert "anchor_reachable_at_listing" in settled[0].clauses.unmet()


def test_g2_clause_d2_broken_the_anchor_went_away_before_the_pass_ended(tmp_path):
    path, listing = satisfying_case(tmp_path)
    settled = lib.retract_subtree([lib.classify_leaf(path, listing)], lost_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN
    assert "anchor_reachable_at_end" in settled[0].clauses.unmet()
    assert "retracted" in settled[0].detail


# =============================================================================================
# H. retract_subtree: one direction of travel, enforced twice
# =============================================================================================

def test_h_a_root_lost_mid_pass_retracts_every_affected_conclusion(tmp_path):
    """The mid-pass-disconnect guard. A disk pulled during a long inspection must not leave a
    subtree of confirmed deletions behind it."""
    path, listing = satisfying_case(tmp_path)
    provisional = lib.classify_leaf(path, listing)
    already_confirmed = lib.retract_subtree([provisional], reachable_root(tmp_path))[0]
    assert already_confirmed.published == lib.MISSING_CONFIRMED

    retracted = lib.retract_subtree([provisional, already_confirmed], lost_root(tmp_path))
    assert [item.published for item in retracted] == [lib.UNKNOWN, lib.UNKNOWN]
    assert lib.confirmed_missing(retracted) == ()


def test_h_an_unknown_is_never_promoted_however_often_it_is_settled(tmp_path):
    unknown = lib.LeafVerdict(path=str(tmp_path / "x.mp4"), verdict=lib.UNKNOWN,
                              clauses=lib.Clauses(), is_final=True)
    for _ in range(3):
        unknown = lib.retract_subtree([unknown], reachable_root(tmp_path))[0]
        assert unknown.published == lib.UNKNOWN


def test_h_present_and_denied_verdicts_pass_through_untouched(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    present = lib.classify_leaf(str(path), lib.list_directory(str(tmp_path)))
    denied = lib.classify_leaf(str(tmp_path / "y.mp4"), lib.list_directory(str(tmp_path)),
                               lstat=raiser(PermissionError(errno.EACCES, "denied")))
    settled = lib.retract_subtree([present, denied], lost_root(tmp_path))
    assert [item.published for item in settled] == [lib.PRESENT, lib.ACCESS_DENIED]


def test_h_uninspected_descendants_of_a_denied_directory_stay_uninspected(tmp_path):
    """A refused directory says nothing about what is under it."""
    denied = lib.list_directory(str(tmp_path / "denied"),
                                listdir=raiser(PermissionError(errno.EACCES, "denied")))
    assert denied.verdict == lib.ACCESS_DENIED
    children = [lib.LeafVerdict(path=str(tmp_path / "denied" / name),
                                verdict=lib.NOT_INSPECTED, is_final=True)
                for name in ("a.mp4", "b.mp4")]
    settled = lib.retract_subtree(children, reachable_root(tmp_path))
    assert {item.published for item in settled} == {lib.NOT_INSPECTED}
    assert lib.confirmed_missing(settled) == ()


def test_h_a_mutated_transition_table_still_cannot_promote_an_unknown(monkeypatch, tmp_path):
    """**The mutant control.** The second guard is independent of the table, and this proves it.

    If the clause re-check in `retract_subtree` were removed, this test would fail -- so the suite
    cannot pass while an UNKNOWN is being upgraded to a confirmation.
    """
    unknown = lib.LeafVerdict(path=str(tmp_path / "x.mp4"), verdict=lib.UNKNOWN,
                              clauses=lib.Clauses(), is_final=True)
    monkeypatch.setitem(lib._FINALISATIONS, (lib.UNKNOWN, True), lib.MISSING_CONFIRMED)
    assert lib._FINALISATIONS[(lib.UNKNOWN, True)] == lib.MISSING_CONFIRMED, "mutation applied"
    settled = lib.retract_subtree([unknown], reachable_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN, \
        "the clause re-check must refuse a confirmation the table tried to grant"


def test_h_a_mutated_table_cannot_confirm_a_provisional_with_missing_clauses(monkeypatch,
                                                                             tmp_path):
    half = lib.LeafVerdict(path=str(tmp_path / "x.mp4"), verdict=lib.MISSING_PROVISIONAL,
                           clauses=lib.Clauses(parent_listed=True, name_absent=True))
    monkeypatch.setitem(lib._FINALISATIONS, (lib.MISSING_PROVISIONAL, True),
                        lib.MISSING_CONFIRMED)
    settled = lib.retract_subtree([half], reachable_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN
    assert "leaf_absent_error" in settled[0].clauses.unmet()


def test_h_the_table_is_load_bearing_too(monkeypatch, tmp_path):
    """And the first guard, independently: neutralising the table alone also refuses."""
    path, listing = satisfying_case(tmp_path)
    provisional = lib.classify_leaf(path, listing)
    monkeypatch.setitem(lib._FINALISATIONS, (lib.MISSING_PROVISIONAL, True), lib.UNKNOWN)
    settled = lib.retract_subtree([provisional], reachable_root(tmp_path))
    assert settled[0].published == lib.UNKNOWN


def test_h_retracting_mutates_nothing_it_was_given(tmp_path):
    path, listing = satisfying_case(tmp_path)
    provisional = lib.classify_leaf(path, listing)
    before = (provisional.verdict, provisional.is_final, provisional.clauses)
    lib.retract_subtree([provisional], reachable_root(tmp_path))
    assert (provisional.verdict, provisional.is_final, provisional.clauses) == before


def test_h_unrecognised_items_are_returned_untouched(tmp_path):
    marker = object()
    assert lib.retract_subtree([marker], reachable_root(tmp_path)) == (marker,)


def test_h_the_summary_counts_published_verdicts_only(tmp_path):
    path, listing = satisfying_case(tmp_path)
    provisional = lib.classify_leaf(path, listing)
    counts = lib.summarise([provisional])
    assert counts[lib.UNKNOWN] == 1
    assert counts[lib.MISSING_CONFIRMED] == 0, "a provisional must not be counted as confirmed"
    settled = lib.retract_subtree([provisional], reachable_root(tmp_path))
    assert lib.summarise(settled)[lib.MISSING_CONFIRMED] == 1


# =============================================================================================
# I. the verdict vocabulary is exactly the design's
# =============================================================================================

def test_i_the_verdict_set_is_the_documented_one():
    assert set(lib.LEAF_VERDICTS) == {
        lib.PRESENT, lib.MISSING_CONFIRMED, lib.ROOT_UNREACHABLE, lib.ACCESS_DENIED,
        lib.UNKNOWN, lib.NOT_INSPECTED}
    assert set(lib.ROOT_VERDICTS) == set(lib.LEAF_VERDICTS) | {lib.CONFIGURED_NOT_CREATED}
    assert lib.MISSING_PROVISIONAL not in lib.LEAF_VERDICTS, \
        "a working state must not be a publishable result"
    assert lib.CONFIGURED_NOT_CREATED not in lib.LEAF_VERDICTS, "root state only"


# =============================================================================================
# J. read-only and stdlib-only, asserted over CALLS so prose may name what it does not do
# =============================================================================================

def _calls() -> set:
    tree = ast.parse(inspect.getsource(lib))
    return {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}


@pytest.mark.parametrize("forbidden", [
    "os.makedirs", "os.mkdir", "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
    "os.truncate", "os.open", "os.fdopen", "os.write", "os.chmod", "os.utime", "os.link",
    "os.symlink", "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2",
    "shutil.copytree", "open", "tempfile.mkstemp", "tempfile.NamedTemporaryFile",
])
def test_j_the_module_contains_no_mutating_filesystem_operation(forbidden):
    assert forbidden not in _calls(), f"{forbidden} is reachable from the root-state model"


def test_j_the_only_filesystem_reads_are_the_four_injectable_ones():
    """A positive statement of the read surface, so a new direct call has to be added here first.

    Defaults are the only place the real `os` functions are named; every call site goes through an
    injected parameter, which is what makes each branch above testable without a disk.
    """
    tree = ast.parse(inspect.getsource(lib))
    defaults = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for value in list(node.args.defaults) + list(node.args.kw_defaults):
                if value is not None:
                    defaults.add(ast.unparse(value))
    assert {"os.listdir", "os.lstat", "os.path.isdir", "os.path.realpath"} <= defaults
    for unexpected in ("os.scandir", "os.walk", "os.stat"):
        assert unexpected not in _calls(), f"{unexpected} is called directly"


def test_j_the_module_imports_the_standard_library_only():
    """Static, and deliberately NOT a `sys.modules` check.

    A first draft of this test asserted that the forbidden names were absent from `sys.modules`,
    and it failed because a pytest plugin had imported `numpy` into the same process. That is the
    right failure: `sys.modules` is process-global and says nothing about what *this* module
    imported, so the assertion was unsound rather than merely inconvenient.

    The dynamic half of the guarantee already exists and is stronger:
    `test_no_runtime_dependency.py` imports every fork module in a **fresh subprocess** and checks
    what leaked. It discovers modules from the package directory, so this file is covered by it
    automatically -- which is also why `errno` is not imported here: that suite's stdlib
    allow-list is curated, and widening it is a separate change.
    """
    tree = ast.parse(inspect.getsource(lib))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "os", "stat", "dataclasses"}, imported
    assert "errno" not in imported, \
        "the stdlib allow-list in test_no_runtime_dependency.py is curated; widen it deliberately"


def test_j_a_whole_pass_over_a_fixture_tree_changes_nothing_on_disk(tmp_path):
    """The read-only proof: a manifest of every entry before and after, plus the directory set.

    This is what would catch a resolver that quietly created the folder it was asked about.
    """
    root = tmp_path / "library"
    (root / "set_01").mkdir(parents=True)
    (root / "clip.mp4").write_bytes(b"x" * 10)
    (root / "set_01" / "clip2.mkv").write_bytes(b"y" * 20)

    def manifest():
        rows = []
        for current, directories, files in os.walk(str(tmp_path)):
            rows.append((current, tuple(sorted(directories)), tuple(sorted(files))))
            for name in sorted(files):
                info = os.lstat(os.path.join(current, name))
                rows.append((os.path.join(current, name), info.st_size, info.st_mtime_ns))
        return tuple(rows)

    before = manifest()
    state = lib.probe_root(str(root))
    listing = lib.list_directory(str(root))
    verdicts = [lib.classify_leaf(str(root / name), listing)
                for name in ("clip.mp4", "absent.mp4", "set_01")]
    lib.may_descend(state, str(root / "set_01"))
    lib.retract_subtree(verdicts, lib.probe_root(str(root)))
    # And the folder that does not exist must still not exist.
    lib.probe_root(str(tmp_path / "never_created"))
    assert not os.path.exists(str(tmp_path / "never_created"))
    assert manifest() == before, "the pass changed something on disk"
