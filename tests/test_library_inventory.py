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
    """A positively-observed root.

    ``anchor`` is derived from the path rather than hardcoded: a literal ``"C:\\"`` happened to
    agree with ``tmp_path`` on the machine this was written on, but it is not a property of the
    path handed in, and a suite whose root fixtures disagree with their own leaves on any other
    machine would be measuring the wrong thing.
    """
    where = str(path or "C:\\x")
    return lib.RootState(typed=where, real=where, anchor=lib.anchor_of(where),
                         verdict=lib.PRESENT, anchor_reachable=True, exists=True, listed=True)


def lost_root(path=None):
    where = str(path or "C:\\x")
    return lib.RootState(typed=where, real=where, anchor=lib.anchor_of(where),
                         verdict=lib.ROOT_UNREACHABLE, anchor_reachable=False)


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
    lib.list_directory(str(root), root=state)
    lib.list_directory(str(tmp_path / "never_created"), root=state)
    # And the folder that does not exist must still not exist.
    lib.probe_root(str(tmp_path / "never_created"))
    assert not os.path.exists(str(tmp_path / "never_created"))
    assert manifest() == before, "the pass changed something on disk"


# =============================================================================================
# K. F-1: the storage anchor is the EFFECTIVE target's, not the typed path's
#
# The review that found F-1 measured it on a real cross-volume NTFS junction: a root selected as
# `E:\...\Link` resolving onto `D:\...` recorded `E:\` as its anchor, so clause (d) watched a
# volume holding none of the files and `D:` could vanish without retracting anything. None of the
# 106 tests that existed then could see it, because every fixture was single-volume and the one
# junction test built its link INSIDE tmp_path. These tests are deliberately injected rather than
# volume-dependent, so the property is pinned on every machine.
# =============================================================================================

def xvol_realpath(link, target):
    """A `realpath` that resolves exactly one path onto another volume."""
    def _resolve(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(link)):
            return target
        return os.path.realpath(path)
    return _resolve


def volume_gone(drive):
    """An `isdir` for which one whole volume has stopped answering."""
    def _isdir(path):
        try:
            if os.path.splitdrive(os.path.abspath(path))[0].upper() == drive.upper():
                return False
        except (OSError, ValueError):
            pass
        return os.path.isdir(path)
    return _isdir


def test_k_probe_root_anchors_on_the_resolved_target_not_the_typed_path():
    """**The assertion whose absence let the corrected-anchor mutant survive.**

    If the anchor is taken before resolution this fails, which is the whole point: the previous
    suite passed identically with the anchor derived either way.
    """
    state = lib.probe_root(r"E:\Lib\Link", realpath=xvol_realpath(r"E:\Lib\Link", r"D:\Real"),
                           isdir=lambda _p: True, listdir=lambda _p: ["clip.mp4"],
                           lstat=raiser(OSError(errno.EINVAL, "not a link")))
    assert state.real == r"D:\Real"
    assert state.anchor == lib.anchor_of(state.real), "the anchor must name the resolved storage"
    assert state.anchor == "D:" + os.sep
    assert state.anchor != lib.anchor_of(r"E:\Lib\Link"), "the typed volume is not the storage"


def test_k_list_directory_anchors_on_the_resolved_target_too():
    """Clause (d1) is recorded by `list_directory`, so it must anchor the same way."""
    listing = lib.list_directory(r"E:\Lib\Link",
                                 realpath=xvol_realpath(r"E:\Lib\Link", r"D:\Real"),
                                 isdir=lambda _p: True, listdir=lambda _p: ["other.mp4"])
    assert listing.ok
    assert listing.anchor == "D:" + os.sep
    assert listing.anchor == lib.anchor_of(listing.real)


def test_k_target_volume_lost_while_the_link_volume_stays_reachable_is_never_missing():
    """The F-1 reproduction, deterministically. `E:` never goes away; `D:` does."""
    link, target = r"E:\Lib\Link", r"D:\Real"
    resolve = xvol_realpath(link, target)
    listing = lib.list_directory(link, realpath=resolve, isdir=lambda _p: True,
                                 listdir=lambda _p: ["other.mp4"])
    provisional = lib.classify_leaf(os.path.join(link, "gone.mp4"), listing,
                                    lstat=raiser(oserror(winerror=2)))
    assert provisional.verdict == lib.MISSING_PROVISIONAL, "the first three clauses do hold"

    final = lib.probe_root(link, realpath=resolve, isdir=volume_gone("D:"),
                           listdir=raiser(oserror(winerror=21)),
                           lstat=raiser(OSError(errno.EINVAL, "x")))
    assert final.anchor == "D:" + os.sep
    assert not final.anchor_reachable, "the storage holding the files is gone"
    assert lib.anchor_of(link) == "E:" + os.sep and os.path.isdir  # the link volume is fine

    settled = lib.retract_subtree([provisional], final)
    assert settled[0].published == lib.UNKNOWN
    assert settled[0].published != lib.MISSING_CONFIRMED
    assert lib.confirmed_missing(settled) == ()


def test_k_a_resolved_unc_share_that_goes_away_is_never_missing():
    """The same defect where the resolved target is a share rather than a drive.

    Entirely injected: no host is contacted.
    """
    link, share = r"C:\LibLink", r"\\filer01\media\lib"
    resolve = xvol_realpath(link, share)
    listing = lib.list_directory(link, realpath=resolve, isdir=lambda _p: True,
                                 listdir=lambda _p: ["other.mp4"])
    assert listing.anchor == r"\\filer01\media" + os.sep, "the share root is the anchor"
    provisional = lib.classify_leaf(os.path.join(link, "gone.mp4"), listing,
                                    lstat=raiser(oserror(winerror=2)))

    final = lib.probe_root(link, realpath=resolve,
                           isdir=lambda p: not str(p).startswith("\\\\"),
                           listdir=raiser(oserror(winerror=53)),
                           lstat=raiser(OSError(errno.EINVAL, "x")))
    assert final.verdict == lib.ROOT_UNREACHABLE
    assert not final.anchor_reachable
    assert lib.retract_subtree([provisional], final)[0].published == lib.UNKNOWN


def test_k_a_root_whose_path_will_not_resolve_offers_no_anchor_to_trust():
    """A lexical fallback must never stand in as proof that the real target is reachable."""
    state = lib.probe_root(r"C:\LibLink", realpath=raiser(OSError(errno.EACCES, "nope")),
                           isdir=lambda _p: True)
    assert state.verdict == lib.UNKNOWN
    assert state.real == ""
    assert not state.anchor_reachable, "an unresolved target is not a reachable one"
    assert not state.positively_observed


def test_k_an_unresolvable_reparse_target_cannot_settle_a_missing_claim(tmp_path):
    """The contradictory case: the LINK's volume is live, so `anchor_reachable` is True, while the
    root's own verdict is a hold. One true boolean is not positive evidence."""

    class _Reparse:
        st_file_attributes = 0x400
        st_mode = 0o040000
        st_dev = 1
        st_ino = 2

    state = lib.probe_root(r"C:\LibLink", lstat=lambda _p: _Reparse(), realpath=lambda p: p,
                           isdir=lambda _p: True, listdir=lambda _p: [])
    assert state.verdict == lib.UNKNOWN and state.is_reparse
    assert state.anchor_reachable, "the link itself sits on a live volume"
    assert not state.positively_observed, "a hold is not an observation"

    leaf = r"C:\LibLink\gone.mp4"
    provisional = lib.classify_leaf(leaf, listing_for(leaf, ["other.mp4"]),
                                    lstat=raiser(oserror(winerror=2)))
    assert provisional.verdict == lib.MISSING_PROVISIONAL
    assert lib.retract_subtree([provisional], state)[0].published == lib.UNKNOWN


def test_k_a_selected_junction_root_is_still_followed_and_anchored_on_its_target(tmp_path):
    """The correction must not break the legitimate case: an explicitly selected junction root is
    followed, and its anchor is its target's."""
    real = tmp_path / "real_library"
    real.mkdir()
    (real / "clip.mp4").write_bytes(b"x")
    link = make_junction(tmp_path / "link_to_library", real)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    state = lib.probe_root(link)
    assert state.verdict == lib.PRESENT and state.is_reparse and state.entry_count == 1
    assert state.anchor == lib.anchor_of(state.real)
    assert state.positively_observed


@pytest.mark.skipif(not os.environ.get("BEATSYNC_TEST_XVOL_DIR"),
                    reason="set BEATSYNC_TEST_XVOL_DIR to a writable directory on a DIFFERENT "
                           "volume to exercise a real cross-volume junction")
def test_k_a_real_cross_volume_junction_records_its_target_volume(tmp_path):
    """The opt-in real-hardware form of the injected tests above.

    The independent review measured this on real storage (link on `E:`, target on `D:`): the
    anchor answered `E:\\` before the fix and `D:\\` after. It is opt-in because a second volume
    is not a property of every machine, and a test must not invent one.
    """
    base = os.environ["BEATSYNC_TEST_XVOL_DIR"]
    if os.path.splitdrive(os.path.abspath(base))[0].upper() == \
            os.path.splitdrive(os.path.abspath(str(tmp_path)))[0].upper():
        pytest.skip("BEATSYNC_TEST_XVOL_DIR is on the same volume as tmp_path")
    target = os.path.join(base, "r14a_xvol_fixture")
    os.makedirs(target, exist_ok=True)
    try:
        link = make_junction(tmp_path / "xvol_link", target)
        if link is None:
            pytest.skip("this platform or privilege level cannot create a junction")
        state = lib.probe_root(link)
        assert state.verdict == lib.PRESENT and state.is_reparse
        assert state.anchor == lib.anchor_of(state.real)
        assert state.anchor != lib.anchor_of(str(tmp_path)), "the volumes really do differ"
    finally:
        if os.path.isdir(target) and not os.listdir(target):
            os.rmdir(target)


# =============================================================================================
# L. F-2: finalisation is bound to the root and storage that produced the evidence
#
# `retract_subtree` previously read one boolean - `RootState.anchor_reachable` - and nothing else,
# so any reachable root could settle any leaf. Jurisdiction and outcome are now separate
# questions: evidence from another root is returned untouched (neither confirmed nor retracted,
# so its own root's pass can still settle it), and the outcome is read from the root's whole
# observation rather than one flag.
# =============================================================================================

def provenance_chain(tmp_path, name="rootA"):
    """A root, a listing taken explicitly under it, and a provisional finding from that listing."""
    root = tmp_path / name
    root.mkdir()
    (root / "other.mp4").write_bytes(b"x")
    state = lib.probe_root(str(root))
    listing = lib.list_directory(str(root), root=state)
    leaf = str(root / "gone.mp4")
    return root, state, listing, lib.classify_leaf(leaf, listing)


def test_l_the_root_identity_travels_the_whole_chain(tmp_path):
    """Listing -> leaf -> finalisation -> published verdict, with provenance intact throughout."""
    root, state, listing, provisional = provenance_chain(tmp_path)
    assert state.root_key, "the root has an established identity"
    assert listing.root_key == state.root_key, "the listing records the root it was taken under"
    assert provisional.root_key == state.root_key, "and the leaf verdict carries it onward"
    settled = lib.retract_subtree([provisional], lib.probe_root(str(root)))
    assert settled[0].root_key == state.root_key, "finalisation preserves it"
    assert settled[0].published == lib.MISSING_CONFIRMED


def test_l_the_correct_reachable_root_does_confirm(tmp_path):
    root, _state, _listing, provisional = provenance_chain(tmp_path)
    settled = lib.retract_subtree([provisional], lib.probe_root(str(root)))
    assert settled[0].published == lib.MISSING_CONFIRMED
    assert settled[0].clauses.all_four and settled[0].is_final
    assert lib.confirmed_missing(settled) == (os.path.abspath(str(root / "gone.mp4")),)


def test_l_a_root_on_another_drive_cannot_confirm_another_roots_leaf(tmp_path):
    _root, _state, _listing, provisional = provenance_chain(tmp_path)
    elsewhere = lib.RootState(typed=r"Q:\other", real=r"Q:\other", anchor="Q:" + os.sep,
                              verdict=lib.PRESENT, anchor_reachable=True, exists=True,
                              listed=True)
    assert elsewhere.positively_observed, "it really is a reachable, observed root"
    settled = lib.retract_subtree([provisional], elsewhere)
    assert settled[0].published != lib.MISSING_CONFIRMED
    assert lib.confirmed_missing(settled) == ()


def test_l_an_unrelated_root_on_the_SAME_drive_cannot_confirm_either(tmp_path):
    """Sharing a drive letter is not sharing an inspection scope. A drive-letter comparison would
    pass this and must not be what the guard rests on."""
    _rootA, stateA, _lst, provisional = provenance_chain(tmp_path, "rootA")
    rootB = tmp_path / "rootB"
    rootB.mkdir()
    stateB = lib.probe_root(str(rootB))
    assert stateB.positively_observed
    assert lib.anchor_of(stateB.real) == lib.anchor_of(stateA.real), "same volume, by construction"
    settled = lib.retract_subtree([provisional], stateB)
    assert settled[0].published != lib.MISSING_CONFIRMED
    assert lib.confirmed_missing(settled) == ()


def test_l_nested_roots_keep_distinct_provenance(tmp_path):
    """The case containment alone cannot answer: a leaf under the inner root IS under the outer
    one, so only explicit identity can tell the two inspections apart."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (inner / "other.mp4").write_bytes(b"x")
    st_outer, st_inner = lib.probe_root(str(outer)), lib.probe_root(str(inner))
    assert st_outer.root_key != st_inner.root_key
    assert lib.is_within(str(inner / "gone.mp4"), str(outer)), "containment cannot separate them"

    provisional = lib.classify_leaf(str(inner / "gone.mp4"),
                                    lib.list_directory(str(inner), root=st_inner))
    assert lib.retract_subtree([provisional], st_outer)[0].published != lib.MISSING_CONFIRMED
    assert lib.retract_subtree([provisional], st_inner)[0].published == lib.MISSING_CONFIRMED


def test_l_foreign_evidence_is_returned_untouched_not_destroyed(tmp_path):
    """A wrong-root call must not consume the finding either: the right root's pass still has to
    be able to settle it. This is what makes a per-root loop over one verdict list safe."""
    rootA, _stateA, _lst, provisional = provenance_chain(tmp_path, "rootA")
    rootB = tmp_path / "rootB"
    rootB.mkdir()

    passed_over = lib.retract_subtree([provisional], lib.probe_root(str(rootB)))[0]
    assert passed_over.verdict == lib.MISSING_PROVISIONAL, "still provisional, not retracted"
    assert not passed_over.is_final
    assert passed_over.published == lib.UNKNOWN

    settled = lib.retract_subtree([passed_over], lib.probe_root(str(rootA)))
    assert settled[0].published == lib.MISSING_CONFIRMED, "its own root can still settle it"


def test_l_an_unknown_root_state_is_not_positive_evidence_even_when_reachable(tmp_path):
    """Case 10: a contradictory pair - `verdict=UNKNOWN` with `anchor_reachable=True`."""
    _root, state, _lst, provisional = provenance_chain(tmp_path)
    contradictory = lib.RootState(typed=state.typed, real=state.real, anchor=state.anchor,
                                  verdict=lib.UNKNOWN, anchor_reachable=True)
    assert contradictory.reachable, "the one boolean says yes"
    assert not contradictory.positively_observed, "the observation as a whole does not"
    settled = lib.retract_subtree([provisional], contradictory)
    assert settled[0].published == lib.UNKNOWN
    assert "anchor_reachable_at_end" in settled[0].clauses.unmet()


@pytest.mark.parametrize("verdict", [lib.ROOT_UNREACHABLE, lib.CONFIGURED_NOT_CREATED,
                                     lib.NOT_INSPECTED, lib.UNKNOWN])
def test_l_no_non_observed_verdict_can_confirm_however_reachable_it_claims_to_be(verdict,
                                                                                 tmp_path):
    _root, state, _lst, provisional = provenance_chain(tmp_path)
    claimed = lib.RootState(typed=state.typed, real=state.real, anchor=state.anchor,
                            verdict=verdict, anchor_reachable=True, exists=True, listed=True)
    assert lib.retract_subtree([provisional], claimed)[0].published != lib.MISSING_CONFIRMED


def test_l_a_state_with_no_established_location_cannot_confirm(tmp_path):
    _root, state, _lst, provisional = provenance_chain(tmp_path)
    nowhere = lib.RootState(typed=state.typed, real="", anchor=state.anchor,
                            verdict=lib.PRESENT, anchor_reachable=True, exists=True, listed=True)
    assert nowhere.root_key == ""
    assert not nowhere.positively_observed
    assert lib.retract_subtree([provisional], nowhere)[0].published != lib.MISSING_CONFIRMED


def test_l_a_same_root_access_denied_final_state_may_still_confirm(tmp_path):
    """Case 11, and the design's anchor-versus-directory distinction, preserved deliberately.

    The storage is demonstrably still there and the parent listing already succeeded; an ACL
    change at the end of a pass does not make a deleted file undeleted. Turning this into a
    storage-disconnection finding would be the opposite error to F-1.
    """
    root, _state, _lst, provisional = provenance_chain(tmp_path)
    denied = lib.probe_root(str(root), listdir=raiser(PermissionError(errno.EACCES, "denied")))
    assert denied.verdict == lib.ACCESS_DENIED
    assert denied.anchor_reachable and denied.exists and not denied.listed
    assert denied.positively_observed, "present but refused is still an observation"
    assert lib.retract_subtree([provisional], denied)[0].published == lib.MISSING_CONFIRMED


def test_l_a_root_lost_after_its_listing_still_retracts(tmp_path):
    """Case 12: the mid-pass-disconnect guard, through the provenance-bearing path."""
    root, _state, _lst, provisional = provenance_chain(tmp_path)
    lost = lib.probe_root(str(root), isdir=lambda _p: False)
    assert lost.verdict == lib.ROOT_UNREACHABLE and not lost.positively_observed
    settled = lib.retract_subtree([provisional], lost)
    assert settled[0].published == lib.UNKNOWN
    assert settled[0].is_final
    assert "retracted" in settled[0].detail
    assert lib.confirmed_missing(settled) == ()


def test_l_a_leaf_that_vanishes_concurrently_stays_unknown(tmp_path):
    """Case 13, through the provenance-bearing path: the listing showed the name, so its absence
    now is a race rather than an observation."""
    root = tmp_path / "rootA"
    root.mkdir()
    leaf = root / "racing.mp4"
    leaf.write_bytes(b"x")
    state = lib.probe_root(str(root))
    listing = lib.list_directory(str(root), root=state)
    verdict = lib.classify_leaf(str(leaf), listing, lstat=raiser(oserror(winerror=2)))
    assert lib.retract_subtree([verdict], lib.probe_root(str(root)))[0].published == lib.UNKNOWN


def test_l_an_empty_reachable_root_stays_distinct_from_an_unreachable_one(tmp_path):
    """Case 14, restated against the new accessors: the most confusable pair in the model."""
    root = tmp_path / "empty"
    root.mkdir()
    empty = lib.probe_root(str(root))
    gone = lib.probe_root(str(root), isdir=lambda _p: False)
    assert empty.verdict == lib.PRESENT and empty.listed and empty.entry_count == 0
    assert empty.positively_observed
    assert gone.verdict == lib.ROOT_UNREACHABLE and not gone.positively_observed
    assert empty.verdict != gone.verdict


def test_l_a_normal_same_volume_library_is_unaffected(tmp_path):
    """Case 1: the ordinary path must be exactly as it was."""
    root = tmp_path / "library"
    root.mkdir()
    for name in ("a.mp4", "b.mkv", "c.mp4"):
        (root / name).write_bytes(b"x")
    state = lib.probe_root(str(root))
    assert state.verdict == lib.PRESENT and state.entry_count == 3
    assert state.anchor == lib.anchor_of(str(root)) == lib.anchor_of(state.real)
    listing = lib.list_directory(str(root), root=state)
    present = lib.classify_leaf(str(root / "a.mp4"), listing)
    absent = lib.classify_leaf(str(root / "gone.mp4"), listing)
    settled = lib.retract_subtree([present, absent], lib.probe_root(str(root)))
    assert [item.published for item in settled] == [lib.PRESENT, lib.MISSING_CONFIRMED]


def test_l_a_listing_offered_for_a_root_it_is_not_under_is_refused(tmp_path):
    """The `root=` keyword must not become a way to label foreign evidence."""
    rootA = tmp_path / "rootA"
    rootB = tmp_path / "rootB"
    rootA.mkdir()
    rootB.mkdir()
    (rootB / "other.mp4").write_bytes(b"x")
    stateA = lib.probe_root(str(rootA))
    mislabelled = lib.list_directory(str(rootB), root=stateA)
    assert not mislabelled.ok
    assert mislabelled.verdict == lib.NOT_INSPECTED
    assert not mislabelled.covers(str(rootB / "gone.mp4"))
    verdict = lib.classify_leaf(str(rootB / "gone.mp4"), mislabelled,
                                lstat=raiser(oserror(winerror=2)))
    assert verdict.published != lib.MISSING_CONFIRMED


def test_l_a_subdirectory_listing_is_valid_evidence_for_its_root(tmp_path):
    """And the legitimate use of `root=`: a child directory inside the root carries the root's
    identity, not its own."""
    root = tmp_path / "library"
    child = root / "set_01"
    child.mkdir(parents=True)
    (child / "other.mp4").write_bytes(b"x")
    state = lib.probe_root(str(root))
    listing = lib.list_directory(str(child), root=state)
    assert listing.ok and listing.root_key == state.root_key
    provisional = lib.classify_leaf(str(child / "gone.mp4"), listing)
    assert provisional.root_key == state.root_key
    settled = lib.retract_subtree([provisional], lib.probe_root(str(root)))
    assert settled[0].published == lib.MISSING_CONFIRMED


def test_l_legacy_callers_without_an_explicit_root_still_work_by_containment(tmp_path):
    """`root=` is optional, so a caller inspecting one directory keeps working - and a leaf that
    is not under the root being finalised is still refused."""
    root = tmp_path / "library"
    root.mkdir()
    (root / "other.mp4").write_bytes(b"x")
    listing = lib.list_directory(str(root))
    assert listing.root_key == ""
    inside = lib.classify_leaf(str(root / "gone.mp4"), listing)
    assert inside.root_key == ""
    assert lib.retract_subtree([inside], lib.probe_root(str(root)))[0].published == \
        lib.MISSING_CONFIRMED

    sibling = tmp_path / "library-evil"
    sibling.mkdir()
    assert lib.retract_subtree([inside], lib.probe_root(str(sibling)))[0].published != \
        lib.MISSING_CONFIRMED


# =============================================================================================
# M. F-6: the evidence's own RESOLVED directory must belong to the root being inspected
#
# F-1 fixed the root's anchor and F-2 fixed the root's identity. Neither asked where the
# *evidence* came from, so one level of nesting reinstated the original defect: a junction
# `<root>\Link` resolving onto another volume is lexically a child of the root's typed text, so
# `list_directory` accepted it on the typed-containment disjunct, stamped it with the root's
# `root_key`, and finalisation then settled a leaf beneath it against the root's storage -- which
# was reachable -- while saying nothing at all about the storage that actually held the file.
#
# Two properties are pinned here, and neither is a drive-letter comparison:
#
#   * containment is measured on the RESOLVED directory, in `list_directory` and again at
#     finalisation through `LeafVerdict.listing_real`;
#   * a same-volume junction escaping to a sibling is refused identically -- its anchor is
#     *equal* to the root's, which is asserted below so these tests cannot pass on an
#     anchor comparison alone.
#
# Every test asserts the physical provenance (`Listing.real`, `LeafVerdict.listing_real`) as well
# as the published verdict, because the pre-fix code recorded the correct resolved location and
# then discarded it -- a suite that only reads the verdict cannot tell the two apart.
# =============================================================================================

def nested_junction_case(tmp_path, *, target, root_name="Library"):
    """A root with a real nested junction in it, pointing at ``target``.

    Returns ``(root_path, root_state, junction_path)`` or ``None`` when a junction cannot be made.
    """
    root = tmp_path / root_name
    root.mkdir()
    (root / "present.mp4").write_bytes(b"x")
    link = make_junction(root / "Link", target)
    if link is None:
        return None
    return str(root), lib.probe_root(str(root)), link


def escape_outcome(root, root_state, directory, *, explicit_root):
    """One full pass over ``directory`` offered as ``root_state``'s evidence."""
    listing = lib.list_directory(directory, **({"root": root_state} if explicit_root else {}))
    leaf = lib.classify_leaf(os.path.join(str(directory), "gone.mp4"), listing,
                             lstat=raiser(oserror(winerror=2)))
    settled = lib.retract_subtree([leaf], lib.probe_root(root))[0]
    return listing, leaf, settled


# --- 1 & 2: cross-volume nested junction, with and without an explicit root ------------------

@pytest.mark.parametrize("explicit_root", [True, False])
def test_m_a_nested_cross_volume_junction_cannot_confirm_a_deletion(tmp_path, explicit_root):
    """**The F-6 control.** Injected so the far volume is deterministic on any machine.

    Pre-fix this published MISSING_CONFIRMED with the detail "the storage holding it was still
    there", for both call shapes. The explicit-root shape is refused at `list_directory`; the
    no-root shape has no containment guard there at all and is refused at finalisation.
    """
    root = tmp_path / "Library"
    root.mkdir()
    (root / "present.mp4").write_bytes(b"x")
    link, far = str(root / "Link"), r"D:\OtherMedia"
    resolve = xvol_realpath(link, far)

    state = lib.probe_root(str(root))
    assert state.positively_observed, "the root itself is perfectly healthy"
    listing = lib.list_directory(link, realpath=resolve, isdir=lambda _p: True,
                                 listdir=lambda _p: ["present.mp4"],
                                 **({"root": state} if explicit_root else {}))
    # The resolved location really is on the other volume -- the F-1 fix gets this right, and
    # discarding it was F-6.
    assert listing.real == far
    assert listing.anchor == "D:" + os.sep
    assert listing.anchor != lib.anchor_of(state.real), "the volumes really do differ"

    if explicit_root:
        assert not listing.ok, "a folder resolving outside the root is not that root's evidence"
        assert listing.verdict == lib.NOT_INSPECTED
        assert "does not lie under" in listing.detail

    leaf = lib.classify_leaf(os.path.join(link, "gone.mp4"), listing,
                             lstat=raiser(oserror(winerror=2)))
    settled = lib.retract_subtree([leaf], lib.probe_root(str(root)))[0]
    assert settled.published == lib.UNKNOWN
    assert settled.published != lib.MISSING_CONFIRMED
    assert lib.confirmed_missing([settled]) == ()
    assert "storage holding it was still there" not in settled.detail


def test_m_the_no_root_shape_is_refused_by_the_recorded_listing_provenance(tmp_path):
    """Why `listing_real` has to exist: without an explicit root there is nothing else to check.

    The leaf's typed path IS under the root, the root IS positively observed, and `root_key` is
    empty on both sides -- so every other signal available at finalisation says yes.
    """
    root = tmp_path / "Library"
    root.mkdir()
    (root / "present.mp4").write_bytes(b"x")
    link, far = str(root / "Link"), r"D:\OtherMedia"
    listing = lib.list_directory(link, realpath=xvol_realpath(link, far),
                                 isdir=lambda _p: True, listdir=lambda _p: ["present.mp4"])
    assert listing.ok and listing.root_key == "", "no root was named, so there is no identity"
    leaf = lib.classify_leaf(os.path.join(link, "gone.mp4"), listing,
                             lstat=raiser(oserror(winerror=2)))
    assert leaf.verdict == lib.MISSING_PROVISIONAL, "the first three clauses do hold"
    assert leaf.listing_real == far, "the physical origin travelled with the verdict"

    state = lib.probe_root(str(root))
    assert lib.is_within(leaf.path, state.real), "the leaf's TYPED path is under the root"
    assert state.positively_observed
    assert lib._same_inspection_root(state, leaf), "jurisdiction alone says yes"
    assert not lib._listing_belongs_to_root(state, leaf), "and containment says no"
    settled = lib.retract_subtree([leaf], state)[0]
    assert settled.published == lib.UNKNOWN
    assert settled.is_final and "does not lie under" in settled.detail
    assert "anchor_reachable_at_end" in settled.clauses.unmet()


# --- 3: same-volume escape, where no anchor comparison could help ----------------------------

@pytest.mark.parametrize("explicit_root", [True, False])
def test_m_a_same_volume_junction_escaping_to_a_sibling_is_refused(tmp_path, explicit_root):
    """A REAL junction, and the case that proves the guard is not a drive-letter test.

    The escape target is on the same volume as the root, so the anchors are *equal*; only the
    resolved location distinguishes them.
    """
    outside = tmp_path / "SiblingOutside"
    outside.mkdir()
    (outside / "present.mp4").write_bytes(b"x")
    case = nested_junction_case(tmp_path, target=outside)
    if case is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    root, state, link = case
    assert lib.anchor_of(str(outside)) == lib.anchor_of(state.real), \
        "same volume by construction: an anchor comparison cannot separate these"

    listing, leaf, settled = escape_outcome(root, state, link, explicit_root=explicit_root)
    assert lib.is_within(os.path.abspath(link), root), "the junction IS a typed child of the root"
    assert not lib.is_within(listing.real or str(outside), root), \
        "but its resolved location is not"
    if explicit_root:
        assert not listing.ok and listing.verdict == lib.NOT_INSPECTED
    else:
        assert listing.ok and leaf.listing_real
        assert not lib.is_within(leaf.listing_real, root)
    assert settled.published == lib.UNKNOWN
    assert settled.published != lib.MISSING_CONFIRMED


# --- 4: the target volume is gone while the link's own volume answers ------------------------

def test_m_the_target_volume_may_be_gone_while_the_link_volume_answers(tmp_path):
    """The whole point of clause (d), at one level of nesting.

    The root is on a live volume and is positively observed at the end of the pass. The storage
    that actually held the file is gone -- and is corroborated as gone by probing it directly.
    """
    root = tmp_path / "Library"
    root.mkdir()
    (root / "present.mp4").write_bytes(b"x")
    link, far = str(root / "Link"), r"D:\OtherMedia"
    resolve = xvol_realpath(link, far)
    listing = lib.list_directory(link, realpath=resolve, isdir=lambda _p: True,
                                 listdir=lambda _p: ["present.mp4"])
    leaf = lib.classify_leaf(os.path.join(link, "gone.mp4"), listing,
                             lstat=raiser(oserror(winerror=2)))
    assert leaf.verdict == lib.MISSING_PROVISIONAL

    # D: has gone away. The root -- and the volume the LINK sits on -- have not.
    end_root = lib.probe_root(str(root), isdir=volume_gone("D:"))
    end_target = lib.probe_root(far, realpath=resolve, isdir=volume_gone("D:"),
                                listdir=raiser(oserror(winerror=21)),
                                lstat=raiser(OSError(errno.EINVAL, "x")))
    assert end_root.verdict == lib.PRESENT and end_root.positively_observed, \
        "the approved root is genuinely, truthfully fine"
    assert end_target.verdict == lib.ROOT_UNREACHABLE and not end_target.positively_observed, \
        "and the storage that held the file is genuinely gone"
    settled = lib.retract_subtree([leaf], end_root)[0]
    assert settled.published == lib.UNKNOWN
    assert lib.confirmed_missing([settled]) == ()


# --- 5 & 6: the legitimate cases, which must be entirely unaffected --------------------------

@pytest.mark.parametrize("explicit_root", [True, False])
def test_m_a_selected_junction_root_and_its_children_still_confirm(tmp_path, explicit_root):
    """The correction must not reject junction-backed roots.

    `probe_root` resolves the root first, so a child reached through the selected link resolves
    *inside* `state.real` and resolved containment already admits it. This is why the typed
    disjunct was never needed.
    """
    real = tmp_path / "real_library"
    child = real / "set_01"
    child.mkdir(parents=True)
    (child / "other.mp4").write_bytes(b"x")
    (real / "other.mp4").write_bytes(b"x")
    link = make_junction(tmp_path / "link_to_library", real)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    state = lib.probe_root(link)
    assert state.verdict == lib.PRESENT and state.is_reparse and state.positively_observed

    for directory in (link, os.path.join(link, "set_01")):
        listing = lib.list_directory(directory, **({"root": state} if explicit_root else {}))
        assert listing.ok, f"{directory} is legitimate evidence for its own junction root"
        assert lib.is_within(listing.real, state.real), "it resolves INSIDE the resolved root"
        leaf = lib.classify_leaf(os.path.join(directory, "gone.mp4"), listing)
        assert leaf.listing_real and lib.is_within(leaf.listing_real, state.real)
        settled = lib.retract_subtree([leaf], lib.probe_root(link))[0]
        assert settled.published == lib.MISSING_CONFIRMED, \
            f"a real deletion under {directory} must still be reportable"
        assert settled.clauses.all_four


@pytest.mark.parametrize("explicit_root", [True, False])
def test_m_an_ordinary_subdirectory_is_still_valid_evidence(tmp_path, explicit_root):
    root = tmp_path / "library"
    child = root / "set_02"
    child.mkdir(parents=True)
    (child / "other.mp4").write_bytes(b"x")
    state = lib.probe_root(str(root))
    listing = lib.list_directory(str(child), **({"root": state} if explicit_root else {}))
    assert listing.ok
    leaf = lib.classify_leaf(str(child / "gone.mp4"), listing)
    assert leaf.listing_real == listing.real
    settled = lib.retract_subtree([leaf], lib.probe_root(str(root)))[0]
    assert settled.published == lib.MISSING_CONFIRMED


# --- 7..10: the F-2 properties, re-pinned through the new guard ------------------------------

def test_m_nested_roots_still_keep_distinct_provenance(tmp_path):
    """Both guards now apply, and they answer differently -- which is the design."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (inner / "other.mp4").write_bytes(b"x")
    st_outer, st_inner = lib.probe_root(str(outer)), lib.probe_root(str(inner))
    assert st_outer.root_key != st_inner.root_key
    leaf = lib.classify_leaf(str(inner / "gone.mp4"),
                             lib.list_directory(str(inner), root=st_inner))
    # The inner listing IS physically under the outer root, so containment alone would admit it.
    assert lib._listing_belongs_to_root(st_outer, leaf), "containment cannot separate them"
    assert not lib._same_inspection_root(st_outer, leaf), "identity can, and does"
    assert lib.retract_subtree([leaf], st_outer)[0].published != lib.MISSING_CONFIRMED
    assert lib.retract_subtree([leaf], st_inner)[0].published == lib.MISSING_CONFIRMED


def test_m_foreign_root_evidence_is_still_returned_untouched(tmp_path):
    """A jurisdiction mismatch is still not consumed -- only a *contradiction* is failed closed."""
    _rootA, _stA, _lst, provisional = provenance_chain(tmp_path, "rootA")
    rootB = tmp_path / "rootB"
    rootB.mkdir()
    passed_over = lib.retract_subtree([provisional], lib.probe_root(str(rootB)))[0]
    assert passed_over.verdict == lib.MISSING_PROVISIONAL, "untouched, not retracted"
    assert not passed_over.is_final
    settled = lib.retract_subtree([passed_over], lib.probe_root(str(tmp_path / "rootA")))
    assert settled[0].published == lib.MISSING_CONFIRMED, "its own root can still settle it"


def test_m_a_same_root_access_denied_still_confirms(tmp_path):
    root, _state, _lst, provisional = provenance_chain(tmp_path)
    denied = lib.probe_root(str(root), listdir=raiser(PermissionError(errno.EACCES, "denied")))
    assert denied.verdict == lib.ACCESS_DENIED and denied.positively_observed
    assert lib._listing_belongs_to_root(denied, provisional), \
        "the listing was taken under this very root"
    assert lib.retract_subtree([provisional], denied)[0].published == lib.MISSING_CONFIRMED


def test_m_mid_pass_root_loss_still_retracts(tmp_path):
    root, _state, _lst, provisional = provenance_chain(tmp_path)
    lost = lib.probe_root(str(root), isdir=lambda _p: False)
    assert lost.verdict == lib.ROOT_UNREACHABLE and not lost.positively_observed
    settled = lib.retract_subtree([provisional], lost)[0]
    assert settled.published == lib.UNKNOWN
    assert "retracted" in settled.detail, "the disconnect wording, not the containment wording"
    assert "anchor_reachable_at_end" in settled.clauses.unmet()


# --- 11: may_descend refuses it, and so does the direct call --------------------------------

def test_m_may_descend_refuses_the_escape_and_so_does_list_directory(tmp_path):
    """`may_descend` is a real guard, but it is not the enforced sequence.

    Nothing couples the two functions, so `list_directory` must refuse the same directory on its
    own -- including when `lstat` cannot decide whether the child is a reparse point at all.
    """
    outside = tmp_path / "SiblingOutside"
    outside.mkdir()
    (outside / "present.mp4").write_bytes(b"x")
    case = nested_junction_case(tmp_path, target=outside)
    if case is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    root, state, link = case

    assert lib.may_descend(state, link).action == lib.SKIP_REPARSE_ESCAPES
    undecidable = lib.may_descend(state, link, lstat=raiser(OSError(errno.EIO, "undecidable")))
    assert not undecidable.is_reparse, "the reparse status could not be established"
    assert undecidable.action == lib.SKIP_OUTSIDE_ROOT, \
        "and it is still refused, because containment is measured on the resolved location"
    assert not undecidable.allowed

    # Now bypass may_descend entirely, which is what a path-verifying caller would do.
    listing = lib.list_directory(link, root=state)
    assert not listing.ok and listing.verdict == lib.NOT_INSPECTED


# --- 12: absent or contradictory provenance fails closed ------------------------------------

def test_m_a_missing_claim_with_no_recorded_listing_provenance_fails_closed(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    state = lib.probe_root(str(root))
    hand_built = lib.LeafVerdict(
        path=str(root / "gone.mp4"), verdict=lib.MISSING_PROVISIONAL,
        clauses=lib.Clauses(parent_listed=True, name_absent=True, leaf_absent_error=True,
                            anchor_reachable_at_listing=True))
    assert hand_built.listing_real == "", "no origin was recorded"
    assert lib._same_inspection_root(state, hand_built), "jurisdiction says yes"
    assert not lib._listing_belongs_to_root(state, hand_built), "provenance cannot be checked"
    settled = lib.retract_subtree([hand_built], state)[0]
    assert settled.published == lib.UNKNOWN
    assert lib.confirmed_missing([settled]) == ()


def test_m_a_contradictory_listing_provenance_fails_closed(tmp_path):
    """`root_key` matches this root exactly, and the recorded origin is somewhere else entirely."""
    root = tmp_path / "library"
    root.mkdir()
    state = lib.probe_root(str(root))
    contradictory = lib.LeafVerdict(
        path=str(root / "gone.mp4"), verdict=lib.MISSING_PROVISIONAL, root_key=state.root_key,
        listing_real=r"D:\SomewhereElse",
        clauses=lib.Clauses(parent_listed=True, name_absent=True, leaf_absent_error=True,
                            anchor_reachable_at_listing=True))
    assert lib._same_inspection_root(state, contradictory), "the identity matches exactly"
    settled = lib.retract_subtree([contradictory], state)[0]
    assert settled.published == lib.UNKNOWN
    assert settled.is_final, "a contradiction is settled, not left hanging as another root's"


@pytest.mark.parametrize("verdict", [lib.PRESENT, lib.ACCESS_DENIED, lib.ROOT_UNREACHABLE,
                                     lib.UNKNOWN, lib.NOT_INSPECTED])
def test_m_verdicts_that_assert_nothing_are_not_held_hostage_to_provenance(tmp_path, verdict):
    """Failing closed applies to a missing CLAIM. Everything else still passes through."""
    root = tmp_path / "library"
    root.mkdir()
    item = lib.LeafVerdict(path=str(root / "x.mp4"), verdict=verdict, is_final=True)
    assert item.listing_real == ""
    assert lib._listing_belongs_to_root(lib.probe_root(str(root)), item)
    settled = lib.retract_subtree([item], lib.probe_root(str(root)))[0]
    assert settled.published == verdict


# --- the review's surviving M5a gap: no second pass may promote a retracted verdict ----------

def test_m_a_retracted_verdict_is_never_promoted_by_a_second_pass(tmp_path):
    """Closes the test gap the independent review found by mutation.

    Deleting ``(MISSING_PROVISIONAL, False)`` from the transition table survived the whole suite,
    because the verdict then stayed MISSING_PROVISIONAL and ``published`` already launders that to
    UNKNOWN -- but a *second* finalisation against a healthy root would then promote it to
    MISSING_CONFIRMED, which the module says is impossible. So the internal verdict is asserted,
    not only the published one, and the second pass is actually performed.
    """
    path, listing = satisfying_case(tmp_path)
    provisional = lib.classify_leaf(path, listing)
    assert provisional.verdict == lib.MISSING_PROVISIONAL

    retracted = lib.retract_subtree([provisional], lost_root(tmp_path))[0]
    assert retracted.verdict == lib.UNKNOWN, \
        "the retraction must REPLACE the provisional verdict, not merely hide it"
    assert retracted.published == lib.UNKNOWN and retracted.is_final

    for _ in range(3):
        retracted = lib.retract_subtree([retracted], reachable_root(tmp_path))[0]
        assert retracted.verdict == lib.UNKNOWN
        assert retracted.published != lib.MISSING_CONFIRMED
    assert lib.confirmed_missing([retracted]) == ()


# --- opt-in: the same nested escape on real cross-volume hardware ---------------------------

@pytest.mark.skipif(not os.environ.get("BEATSYNC_TEST_XVOL_DIR"),
                    reason="set BEATSYNC_TEST_XVOL_DIR to a writable directory on a DIFFERENT "
                           "volume to exercise a real cross-volume nested junction")
def test_m_a_real_nested_cross_volume_junction_cannot_confirm_a_deletion(tmp_path):
    """The opt-in real-hardware form of the injected F-6 control above.

    Opt-in because a second volume is not a property of every machine, and a test must not invent
    one. The storage loss is still an injected `isdir`: nothing is unplugged or unmounted.
    """
    base = os.environ["BEATSYNC_TEST_XVOL_DIR"]
    if os.path.splitdrive(os.path.abspath(base))[0].upper() == \
            os.path.splitdrive(os.path.abspath(str(tmp_path)))[0].upper():
        pytest.skip("BEATSYNC_TEST_XVOL_DIR is on the same volume as tmp_path")
    target = os.path.join(base, "r14a_f6_fixture")
    os.makedirs(target, exist_ok=True)
    try:
        with open(os.path.join(target, "present.mp4"), "wb") as handle:
            handle.write(b"x")
        case = nested_junction_case(tmp_path, target=target)
        if case is None:
            pytest.skip("this platform or privilege level cannot create a junction")
        root, state, link = case
        far_drive = os.path.splitdrive(os.path.abspath(target))[0]
        assert os.path.splitdrive(os.path.realpath(link))[0].upper() == far_drive.upper(), \
            "the junction really does resolve across the volume boundary"

        for explicit_root in (True, False):
            listing, leaf, _ = escape_outcome(root, state, link, explicit_root=explicit_root)
            end_root = lib.probe_root(root, isdir=volume_gone(far_drive))
            assert end_root.positively_observed, "the root's own volume still answers"
            settled = lib.retract_subtree([leaf], end_root)[0]
            assert settled.published == lib.UNKNOWN, \
                f"explicit_root={explicit_root} must not confirm a deletion on lost storage"
            if explicit_root:
                assert not listing.ok
    finally:
        stray = os.path.join(target, "present.mp4")
        if os.path.isfile(stray):
            os.remove(stray)
        if os.path.isdir(target) and not os.listdir(target):
            os.rmdir(target)
