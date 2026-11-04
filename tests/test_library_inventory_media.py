"""R14-A stage A2: the bounded, read-only media inventory.

The one property every test here defends: **a figure is either a total or it is labelled as not
one.** ``RootInventory.totals`` returns ``None`` for anything that is not authoritative, so a
consumer cannot be handed a short count by accident, and the negative controls at the bottom prove
these assertions can actually fail.

Design matrix rows covered (``BEATSYNC_R14A_SCOPE_DESIGN.md`` §11): 1, 2, 7, 14 — plus the owner's
A2 acceptance list J–T. Row 6 (a junction out of the approved root) is covered here with an
explicitly documented divergence from the design text: the *deployed* scanner **refuses** such a
root outright rather than reporting the link in a ``skipped`` list and continuing, so A2 reports
``INVENTORY_INCOMPLETE`` plus the named link through Tier B. The design's "reported in ``skipped``,
not counted" predates PR #88 and is not weakened here — see
``test_q_a_junction_root_is_incomplete_not_a_smaller_total``.

Fixtures are synthetic and live in pytest's own ``tmp_path`` (CLAUDE.md / PCBUS-HK-v1). Nothing
reads operator media, a production cache or a shared-cache record.
"""

from __future__ import annotations

import ast
import os

import pytest
from conftest import write_file

from beatsync_fork import collision, library_inventory as li
from beatsync_fork.input_manager import InputScanError, InputScanLimitExceeded, scan_folder

SUFFIXES = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


# =============================================================================================
# helpers
# =============================================================================================

def make_junction(link, target):
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return str(link)
    except Exception:
        return None


def manifest(root):
    """``(relpath -> (size, mtime_ns))`` plus the directory set, for the read-only proof."""
    files = {}
    directories = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            directories.add(os.path.relpath(os.path.join(dirpath, name), root))
        for name in filenames:
            full = os.path.join(dirpath, name)
            info = os.stat(full)
            files[os.path.relpath(full, root)] = (info.st_size, info.st_mtime_ns)
    return files, directories


def inspect(roots, **kwargs):
    """Call ``inspect_library`` with the scanner contract the caller is required to inject.

    There is no default for ``scan``/``scan_error``/``limit_error`` on purpose: A2 must not import
    ``input_manager``, because ``test_library_inventory.py::test_j_the_module_imports_the_standard
    _library_only`` pins this module's imports to ``{__future__, os, stat, dataclasses}``. Wiring
    them here is what the GUI will do too.
    """
    kwargs.setdefault("suffixes", SUFFIXES)
    kwargs.setdefault("scan", scan_folder)
    kwargs.setdefault("scan_error", InputScanError)
    kwargs.setdefault("limit_error", InputScanLimitExceeded)
    kwargs.setdefault("candidate_names",
                      lambda name: collision.candidate_names(name, collision.SUFFIX))
    return li.inspect_library(roots, **kwargs)


def one(roots, **kwargs):
    result = inspect(roots, **kwargs)
    assert len(result.roots) == 1
    return result.roots[0]


# =============================================================================================
# J - matrix row 1: a normal reachable library
# =============================================================================================

def test_j_three_mp4_and_one_mkv(tmp_path):
    root = str(tmp_path / "library")
    sizes = {}
    for index in range(3):
        path = write_file(os.path.join(root, f"clip{index}.mp4"), b"M" * (10 + index))
        sizes[path] = 10 + index
    path = write_file(os.path.join(root, "extra.mkv"), b"K" * 40)
    sizes[path] = 40

    item = one([(li.LIBRARY_ROLE, root)])
    assert item.root.verdict == li.PRESENT
    assert item.completeness == li.INVENTORY_COMPLETE
    assert item.reason == li.REASON_NONE
    assert item.authoritative is True

    totals = item.totals
    assert totals is not None
    assert totals.ready == 4
    assert totals.ready_bytes == sum(sizes.values())
    assert totals.discovered == 4
    assert totals.rejected == 0
    assert totals.path_collisions == 0
    assert totals.reconciles is True
    assert item.duplicate_basenames == ()


def test_j_bytes_come_from_the_scanner_and_match_the_filesystem(tmp_path):
    root = str(tmp_path / "library")
    for index in range(5):
        write_file(os.path.join(root, "nested", f"c{index}.mp4"), b"Z" * (100 + index * 7))
    item = one([(li.LIBRARY_ROLE, root)])
    on_disk = sum(os.path.getsize(os.path.join(dp, n))
                  for dp, _d, fs in os.walk(root) for n in fs)
    assert item.totals.ready_bytes == on_disk


def test_j_the_role_and_requested_path_are_carried_through(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a.mp4"), b"A")
    item = one([(li.LIBRARY_ROLE, root)])
    assert item.role == li.LIBRARY_ROLE
    assert item.requested == root
    assert item.limit == li.MAX_INVENTORY_ENTRIES_PER_ROOT


# =============================================================================================
# K - matrix row 2: empty but reachable is a first run, NOT an error and NOT missing
# =============================================================================================

def test_k_empty_but_reachable_is_zero_media_and_complete(tmp_path):
    root = str(tmp_path / "library")
    os.makedirs(root)
    item = one([(li.LIBRARY_ROLE, root)])
    assert item.root.verdict == li.PRESENT
    assert item.root.exists is True
    assert item.completeness == li.INVENTORY_COMPLETE
    assert item.totals.ready == 0
    assert item.totals.discovered == 0
    assert item.reason == li.REASON_NONE


def test_k_a_folder_that_does_not_exist_is_not_the_same_as_an_empty_one(tmp_path):
    absent = str(tmp_path / "never_created")
    item = one([(li.LIBRARY_ROLE, absent)])
    assert item.root.verdict == li.CONFIGURED_NOT_CREATED
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_ROOT_NOT_USABLE
    assert item.totals is None
    assert not os.path.exists(absent), "the inspection created the folder"


# =============================================================================================
# L - unsupported, empty and unreadable entries are each counted separately
# =============================================================================================

def test_l_every_rejection_reason_is_reported(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "good.mp4"), b"G" * 12)
    write_file(os.path.join(root, "notes.txt"), b"N")
    write_file(os.path.join(root, "readme.md"), b"R")
    write_file(os.path.join(root, "hollow.mp4"), b"")

    item = one([(li.LIBRARY_ROLE, root)])
    totals = item.totals
    assert totals.ready == 1
    assert totals.unsupported == 2
    assert totals.empty == 1
    assert totals.unreadable == 0
    assert totals.not_a_file == 0
    assert totals.discovered == 4
    assert totals.reconciles is True


def test_l_an_unreadable_file_is_named_not_lost(tmp_path, monkeypatch):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "fine.mp4"), b"F")
    bad = write_file(os.path.join(root, "locked.mp4"), b"L")

    real_stat = os.stat
    target = os.path.normcase(bad)

    def failing(path, *args, **kwargs):
        if os.path.normcase(str(path)) == target:
            raise PermissionError(13, "Access is denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing)
    item = one([(li.LIBRARY_ROLE, root)])
    assert item.totals.unreadable == 1
    assert item.totals.ready == 1
    assert item.totals.reconciles is True


def test_l_counts_reconcile_with_the_scanner_itself(tmp_path):
    """A2 transcribes; it must not recompute. Same tree, same numbers."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a.mp4"), b"A")
    write_file(os.path.join(root, "b.txt"), b"B")
    write_file(os.path.join(root, "c.mkv"), b"")
    direct = scan_folder(root, recursive=True, extensions=SUFFIXES, detect_duplicates=False)
    totals = one([(li.LIBRARY_ROLE, root)]).totals
    assert totals.discovered == direct.discovered_count
    assert totals.ready == direct.ready_count
    assert totals.ready_bytes == direct.total_ready_bytes
    assert totals.path_collisions == direct.path_collisions


# =============================================================================================
# M - matrix row 7: duplicate BASENAMES, with contents deliberately not compared
# =============================================================================================

def test_m_duplicate_basename_with_different_contents(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a", "clip.mp4"), b"X" * 16)
    write_file(os.path.join(root, "b", "clip.mp4"), b"Y" * 32)

    item = one([(li.LIBRARY_ROLE, root)])
    assert len(item.duplicate_basenames) == 1
    group = item.duplicate_basenames[0]
    assert group.count == 2
    assert group.extra_copies == 1
    assert group.contents_compared is False
    assert {os.path.basename(p) for p in group.paths} == {"clip.mp4"}
    # Two distinct real files: the NAMES collide, the content question was never asked.
    assert item.totals.ready == 2
    assert item.totals.ready_bytes == 48


def test_m_basename_grouping_is_case_insensitive(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a", "Clip.MP4"), b"X")
    write_file(os.path.join(root, "b", "clip.mp4"), b"Y")
    item = one([(li.LIBRARY_ROLE, root)])
    assert len(item.duplicate_basenames) == 1
    assert item.duplicate_basenames[0].count == 2


def test_m_unique_names_produce_no_groups(tmp_path):
    root = str(tmp_path / "library")
    for index in range(4):
        write_file(os.path.join(root, f"unique{index}.mp4"), b"U")
    assert one([(li.LIBRARY_ROLE, root)]).duplicate_basenames == ()


def test_m_no_file_is_opened_by_the_default_inventory(tmp_path, monkeypatch):
    """S, and the content half of row 7: zero reads, so no content comparison can have happened."""
    import builtins

    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a", "clip.mp4"), b"SAME" * 8)
    write_file(os.path.join(root, "b", "clip.mp4"), b"SAME" * 8)

    opened = []
    real_open = builtins.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    item = one([(li.LIBRARY_ROLE, root)])
    assert opened == []
    assert item.duplicate_basenames[0].contents_compared is False


def test_m_duplicate_basenames_helper_is_pure(tmp_path):
    groups = li.duplicate_basenames(["x/clip.mp4", "y/clip.mp4", "z/other.mp4", ""])
    assert len(groups) == 1
    assert groups[0].name == os.path.normcase("clip.mp4")
    assert groups[0].paths == ("x/clip.mp4", "y/clip.mp4")


# =============================================================================================
# N - destination collision prediction, and an inaccessible destination
# =============================================================================================

def test_n_occupied_names_are_predicted_from_one_listing(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    write_file(os.path.join(root, "fresh.mp4"), b"F")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")

    item = one([(li.LIBRARY_ROLE, root)],
               destinations={li.LIBRARY_ROLE: destination})
    forecast = item.destination
    assert forecast is not None
    assert forecast.known is True
    assert forecast.binding is False
    assert forecast.occupied_names == ("clip.mp4",)
    conflict = forecast.conflicts[0]
    assert conflict.occupied is True
    assert conflict.alternatives
    assert all(os.path.normcase(name) != os.path.normcase("clip.mp4")
               for name in conflict.alternatives)


def test_n_alternatives_come_from_collision_candidate_names(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")
    write_file(os.path.join(destination, "clip_01.mp4"), b"OLD")

    forecast = one([(li.LIBRARY_ROLE, root)],
                   destinations={li.LIBRARY_ROLE: destination}).destination
    expected = list(collision.candidate_names("clip.mp4", collision.SUFFIX))
    assert forecast.conflicts[0].alternatives[0] == "clip_02.mp4"
    assert forecast.conflicts[0].alternatives[0] in expected


def test_n_an_unreachable_destination_is_unknown_never_zero(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    absent = str(tmp_path / "no_such_destination")

    forecast = one([(li.LIBRARY_ROLE, root)],
                   destinations={li.LIBRARY_ROLE: absent}).destination
    assert forecast.known is False
    assert forecast.conflicts == ()
    assert forecast.occupied_names == ()
    assert "UNKNOWN" in forecast.detail
    assert forecast.state != li.PRESENT


def test_n_an_unreadable_destination_is_unknown(tmp_path):
    """Injected, not monkeypatched: A1's primitives bind ``listdir=os.listdir`` as a *default*, so
    patching ``os.listdir`` would never reach them. Injection is the module's own idiom."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")

    real_listdir = os.listdir
    target = os.path.normcase(os.path.realpath(destination))

    def failing(path):
        if os.path.normcase(os.path.realpath(str(path))) == target:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path)

    forecast = one([(li.LIBRARY_ROLE, root)],
                   destinations={li.LIBRARY_ROLE: destination},
                   listdir=failing).destination
    assert forecast.known is False
    assert forecast.state == li.ACCESS_DENIED
    assert forecast.conflicts == ()


def test_n_the_destination_is_never_created(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    absent = str(tmp_path / "must_not_appear")
    one([(li.LIBRARY_ROLE, root)], destinations={li.LIBRARY_ROLE: absent})
    assert not os.path.exists(absent)


def test_n_no_destination_configured_means_no_forecast(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    assert one([(li.LIBRARY_ROLE, root)]).destination is None


def test_n_a_blank_destination_is_not_the_working_directory(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    forecast = one([(li.LIBRARY_ROLE, root)], destinations={li.LIBRARY_ROLE: "   "}).destination
    assert forecast.known is False
    assert forecast.destination == ""
    assert forecast.state == li.NOT_INSPECTED


def test_n_the_destination_is_listed_exactly_once(tmp_path, monkeypatch):
    root = str(tmp_path / "library")
    for index in range(6):
        write_file(os.path.join(root, f"clip{index}.mp4"), b"C")
    destination = str(tmp_path / "published")
    for index in range(6):
        write_file(os.path.join(destination, f"clip{index}.mp4"), b"OLD")

    listings = []
    real_listdir = os.listdir
    target = os.path.normcase(os.path.realpath(destination))

    def spy(path):
        if os.path.normcase(os.path.realpath(str(path))) == target:
            listings.append(str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", spy)
    forecast = one([(li.LIBRARY_ROLE, root)],
                   destinations={li.LIBRARY_ROLE: destination}).destination
    assert len(forecast.conflicts) == 6
    # probe_root lists it once to establish PRESENT, list_directory once for the names. Never
    # once per candidate name, which is what this test exists to prevent.
    assert len(listings) <= 2, f"the destination was listed {len(listings)} times"


# =============================================================================================
# O - root failure and INCOMPLETE reporting
# =============================================================================================

@pytest.mark.parametrize("verdict_setup", ["access_denied", "unresolvable"])
def test_o_an_unusable_root_publishes_no_total(tmp_path, verdict_setup):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    injected = {}

    if verdict_setup == "access_denied":
        real_listdir = os.listdir
        target = os.path.normcase(os.path.realpath(root))

        def failing(path):
            if os.path.normcase(os.path.realpath(str(path))) == target:
                raise PermissionError(13, "Access is denied", str(path))
            return real_listdir(path)

        injected["listdir"] = failing
        expected = li.ACCESS_DENIED
    else:
        def failing_realpath(path, **kwargs):
            raise OSError(1, "cannot resolve")

        injected["realpath"] = failing_realpath
        expected = li.UNKNOWN

    item = one([(li.LIBRARY_ROLE, root)], **injected)
    assert item.root.verdict == expected
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_ROOT_NOT_USABLE
    assert item.totals is None
    assert item.counts is None


def test_o_a_root_lost_mid_pass_loses_its_total(tmp_path):
    """Clause (d) applied to totals: the end-of-pass re-probe can only DEMOTE."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")

    calls = {"n": 0}
    real_probe = li.probe_root

    def probe(path, **kwargs):
        calls["n"] += 1
        state = real_probe(path, **kwargs)
        if calls["n"] > 1:
            # The second probe is the end-of-pass re-check: the storage has gone.
            return li.RootState(typed=state.typed, real=state.real, anchor=state.anchor,
                                verdict=li.ROOT_UNREACHABLE, anchor_reachable=False)
        return state

    item = one([(li.LIBRARY_ROLE, root)], probe=probe)
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_ROOT_LOST
    assert item.totals is None
    assert item.counts is None


def test_o_a_root_still_there_at_the_end_keeps_its_total(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    item = one([(li.LIBRARY_ROLE, root)])
    assert item.completeness == li.INVENTORY_COMPLETE
    assert item.totals.ready == 1


def test_o_a_traversal_failure_is_incomplete_with_tier_b_diagnostics(tmp_path, monkeypatch):
    """Matrix row 14: no count published as a total; Tier B names every unlistable folder."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "ok", "good.mp4"), b"G")
    write_file(os.path.join(root, "bad_one", "x.mp4"), b"X")
    write_file(os.path.join(root, "bad_two", "y.mp4"), b"Y")

    bad = {os.path.normcase(os.path.join(root, "bad_one")),
           os.path.normcase(os.path.join(root, "bad_two"))}
    real_scandir = os.scandir
    real_listdir = os.listdir

    def failing_scandir(path=None):
        if path is not None and os.path.normcase(str(path)) in bad:
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path) if path is not None else real_scandir()

    def failing_listdir(path):
        if os.path.normcase(str(path)) in bad:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path)

    # `os.scandir` IS resolved at call time inside `os.walk`, so Tier A's failure is patched;
    # Tier B's `listdir` is a bound default and must be injected instead.
    monkeypatch.setattr(os, "scandir", failing_scandir)

    item = one([(li.LIBRARY_ROLE, root)], listdir=failing_listdir)
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_SCAN_REFUSED
    assert item.totals is None

    diag = item.diagnostics
    assert diag is not None and diag.ran is True
    assert diag.is_lower_bound is True
    assert diag.complete is False
    named = {os.path.normcase(p) for p in diag.unreadable_directories}
    assert bad <= named, f"Tier B named only {named}, not every unlistable folder"
    assert diag.files_seen >= 1  # a lower bound: `ok/good.mp4` was reachable


# =============================================================================================
# P - limit exceeded: no authoritative totals, and the bound is announced
# =============================================================================================

def test_p_limit_exceeded_publishes_no_totals(tmp_path):
    root = str(tmp_path / "library")
    for index in range(12):
        write_file(os.path.join(root, f"c{index:02d}.mp4"), b"C")

    item = one([(li.LIBRARY_ROLE, root)], max_entries=4)
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_LIMIT_EXCEEDED
    assert item.totals is None
    assert item.counts is None
    assert item.limit == 4
    assert item.counted_at_least == 4


def test_p_the_announced_bound_appears_in_the_read_out(tmp_path):
    root = str(tmp_path / "library")
    for index in range(12):
        write_file(os.path.join(root, f"c{index:02d}.mp4"), b"C")
    result = inspect([(li.LIBRARY_ROLE, root)], max_entries=4)
    text = "\n".join(li.inventory_lines(result))
    assert "INVENTORY_INCOMPLETE" in text
    assert li.REASON_LIMIT_EXCEEDED in text
    assert "limit of 4" in text
    assert "at least 4" in text
    assert "not a total" in text


def test_p_a_ceiling_above_the_tree_is_a_normal_complete_inventory(tmp_path):
    root = str(tmp_path / "library")
    for index in range(12):
        write_file(os.path.join(root, f"c{index:02d}.mp4"), b"C")
    item = one([(li.LIBRARY_ROLE, root)], max_entries=12)
    assert item.completeness == li.INVENTORY_COMPLETE
    assert item.totals.ready == 12


def test_p_the_default_ceiling_is_the_owner_approved_value(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    assert li.MAX_INVENTORY_ENTRIES_PER_ROOT == 200_000
    assert one([(li.LIBRARY_ROLE, root)]).limit == 200_000


def test_p_a_complete_read_out_never_says_incomplete(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a", "clip.mp4"), b"A")
    write_file(os.path.join(root, "b", "clip.mp4"), b"B")
    text = "\n".join(li.inventory_lines(inspect([(li.LIBRARY_ROLE, root)])))
    assert "INVENTORY_INCOMPLETE" not in text
    assert "2 media files" in text
    assert "contents were NOT compared" in text


# =============================================================================================
# Q - Tier B: directory errors and unsafe links
# =============================================================================================

def test_q_a_junction_root_is_incomplete_not_a_smaller_total(tmp_path):
    """Matrix row 6, as the DEPLOYED scanner actually behaves.

    The design text says a junction out of the approved root is "reported in ``skipped``, not
    counted". The merged scanner is stricter: it refuses the whole root, because ``InputSet`` has no
    field for an excluded directory and a quiet exclusion would publish a complete-looking total.
    A2 therefore reports INVENTORY_INCOMPLETE and names the link through Tier B. The divergence is
    documented rather than resolved by weakening the scanner.
    """
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "inside.mp4"), b"I")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 999)
    if make_junction(os.path.join(root, "escape"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    item = one([(li.LIBRARY_ROLE, root)])
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_SCAN_REFUSED
    assert item.totals is None
    assert "junction or mount point" in item.detail

    diag = item.diagnostics
    assert diag is not None and diag.ran is True
    assert any("escape" in os.path.basename(p) for p in diag.skipped_links)
    # And nothing from beyond the link reached any figure Tier B reported.
    assert all("ESCAPED" not in p for p in diag.skipped_links)
    assert diag.files_seen == 1  # only `inside.mp4`; a lower bound, and it excludes the escape


def test_q_tier_b_never_follows_a_nested_junction(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "kept.mp4"), b"K")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 999)
    write_file(os.path.join(outside, "deeper", "ESCAPED2.mkv"), b"Y" * 777)
    if make_junction(os.path.join(root, "escape"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    state = li.probe_root(root)
    diag = li.diagnose_root(state, suffixes=SUFFIXES)
    assert diag.ran is True
    assert diag.files_seen == 1
    assert diag.bytes_seen == 1
    assert len(diag.skipped_links) == 1
    assert diag.complete is True


def test_q_tier_b_containment_holds_when_the_reparse_predicate_blinds(tmp_path):
    """``is_reparse_point`` is a REPORTING predicate and answers False without link metadata.

    ``may_descend`` additionally requires resolved-path containment, which is what still refuses
    the escape. Without this, Tier B would have a silent hole exactly where A1's accepted MINOR
    F-5 bites.
    """
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "kept.mp4"), b"K")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 999)
    link = make_junction(os.path.join(root, "escape"), outside)
    if link is None:
        pytest.skip("junctions unavailable on this platform")

    real_lstat = os.lstat

    class NoAttributes:
        def __init__(self, inner):
            self.st_mode = inner.st_mode
            self.st_size = inner.st_size

    def blind(path, *args, **kwargs):
        return NoAttributes(real_lstat(path, *args, **kwargs))

    # The predicate genuinely degrades...
    assert li.is_reparse_point(link) is True
    assert li.is_reparse_point(link, lstat=blind) is False
    # ...and may_descend still refuses, on containment rather than on the link bit.
    state = li.probe_root(root)
    decision = li.may_descend(state, link, lstat=blind)
    assert decision.allowed is False
    assert decision.action == li.SKIP_OUTSIDE_ROOT

    diag = li.diagnose_root(state, suffixes=SUFFIXES, lstat=blind)
    assert diag.files_seen == 1
    assert diag.outside_root and not diag.skipped_links
    assert all("ESCAPED" not in p for p in diag.outside_root + diag.skipped_links)


def test_q_tier_b_announces_its_own_bounds(tmp_path):
    root = str(tmp_path / "library")
    for index in range(10):
        write_file(os.path.join(root, f"d{index}", "clip.mp4"), b"C")
    state = li.probe_root(root)

    capped_dirs = li.diagnose_root(state, suffixes=SUFFIXES, max_directories=3)
    assert capped_dirs.bound_reached == "directories"
    assert capped_dirs.complete is False

    capped_files = li.diagnose_root(state, suffixes=SUFFIXES, max_entries=2)
    assert capped_files.bound_reached == "entries"
    assert capped_files.complete is False


def test_q_tier_b_defaults_are_bounded(tmp_path):
    assert li.MAX_DIAGNOSTIC_DIRECTORIES > 0
    assert li.MAX_DIAGNOSTIC_ENTRIES > 0


def test_q_tier_b_refuses_a_root_with_no_established_location():
    diag = li.diagnose_root(li.RootState())
    assert diag.ran is False
    assert diag.complete is False


def test_q_tier_b_only_runs_after_a_tier_a_failure(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    assert one([(li.LIBRARY_ROLE, root)]).diagnostics is None


def test_q_tier_b_can_be_switched_off(tmp_path):
    root = str(tmp_path / "library")
    for index in range(6):
        write_file(os.path.join(root, f"c{index}.mp4"), b"C")
    item = one([(li.LIBRARY_ROLE, root)], max_entries=2, diagnostics=False)
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.diagnostics is None


# =============================================================================================
# R - stop between roots; uninspected roots labelled correctly
# =============================================================================================

def test_r_a_stop_leaves_later_roots_not_inspected(tmp_path):
    first = str(tmp_path / "one")
    second = str(tmp_path / "two")
    write_file(os.path.join(first, "a.mp4"), b"A")
    write_file(os.path.join(second, "b.mp4"), b"B")

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2  # let the first root finish, then stop

    result = inspect([(li.LIBRARY_ROLE, first), (li.REJECTED_ROLE, second)],
                     include_rejected=True, should_stop=should_stop)
    assert result.stopped is True
    assert result.roots[0].completeness == li.INVENTORY_COMPLETE
    assert result.roots[0].totals.ready == 1
    assert result.roots[1].completeness == li.NOT_INSPECTED
    assert result.roots[1].reason == li.REASON_STOPPED
    assert result.roots[1].totals is None
    assert result.complete is False


def test_r_a_stop_before_anything_leaves_every_root_not_inspected(tmp_path):
    first = str(tmp_path / "one")
    write_file(os.path.join(first, "a.mp4"), b"A")
    result = inspect([(li.LIBRARY_ROLE, first)], should_stop=lambda: True)
    assert result.stopped is True
    assert result.roots[0].completeness == li.NOT_INSPECTED
    assert result.roots[0].reason == li.REASON_STOPPED
    assert result.roots[0].totals is None


def test_r_no_stop_means_everything_is_inspected(tmp_path):
    first = str(tmp_path / "one")
    second = str(tmp_path / "two")
    write_file(os.path.join(first, "a.mp4"), b"A")
    write_file(os.path.join(second, "b.mp4"), b"B")
    result = inspect([(li.LIBRARY_ROLE, first), (li.REJECTED_ROLE, second)],
                     include_rejected=True, should_stop=lambda: False)
    assert result.stopped is False
    assert result.complete is True
    assert all(item.authoritative for item in result.roots)


def test_r_a_stop_before_the_forecast_says_so_rather_than_looking_unconfigured(tmp_path):
    """A dropped phase must not be indistinguishable from a phase that was never asked for."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # pass the pre-root check, stop before the forecast

    item = one([(li.LIBRARY_ROLE, root)], destinations={li.LIBRARY_ROLE: destination},
               should_stop=should_stop)
    # Tier A completed, so its counts really are totals and are kept.
    assert item.completeness == li.INVENTORY_COMPLETE
    assert item.totals.ready == 1
    # But the forecast is present-and-unknown, not absent.
    assert item.destination is not None
    assert item.destination.known is False
    assert item.destination.state == li.NOT_INSPECTED
    assert "stopped" in item.destination.detail
    assert item.destination.conflicts == ()
    assert "UNKNOWN" in "\n".join(li.inventory_lines(
        li.LibraryInventory(roots=(item,))))


def test_r_the_read_out_names_a_stopped_root(tmp_path):
    first = str(tmp_path / "one")
    write_file(os.path.join(first, "a.mp4"), b"A")
    result = inspect([(li.LIBRARY_ROLE, first)], should_stop=lambda: True)
    assert "NOT INSPECTED" in "\n".join(li.inventory_lines(result))


# =============================================================================================
# scope: explicit roles only, and no automatic discovery
# =============================================================================================

def test_scope_the_discarded_folder_is_skipped_unless_requested(tmp_path):
    library = str(tmp_path / "library")
    rejected = str(tmp_path / "rejected")
    write_file(os.path.join(library, "a.mp4"), b"A")
    write_file(os.path.join(rejected, "b.mp4"), b"B")

    default = inspect([(li.LIBRARY_ROLE, library), (li.REJECTED_ROLE, rejected)])
    assert default.roots[0].authoritative is True
    assert default.roots[1].completeness == li.NOT_INSPECTED
    assert default.roots[1].reason == li.REASON_NOT_REQUESTED
    assert default.roots[1].totals is None

    asked = inspect([(li.LIBRARY_ROLE, library), (li.REJECTED_ROLE, rejected)],
                    include_rejected=True)
    assert asked.roots[1].authoritative is True
    assert asked.roots[1].totals.ready == 1


@pytest.mark.parametrize("blank", ["", "   ", '""', '"   "', None, 0])
def test_scope_an_unnamed_root_is_not_inspected_at_all(tmp_path, monkeypatch, blank):
    """``os.path.abspath("")`` is the working directory, which is the bug this refuses."""
    monkeypatch.chdir(tmp_path)
    write_file(os.path.join(str(tmp_path), "stray.mp4"), b"S")
    item = one([(li.LIBRARY_ROLE, blank)])
    assert item.root.verdict == li.NOT_INSPECTED
    assert item.completeness == li.NOT_INSPECTED
    assert item.reason == li.REASON_NOT_REQUESTED
    assert "no folder was named" in item.detail
    assert item.totals is None
    assert item.counts is None


@pytest.mark.parametrize("quoted", ["''", "'   '"])
def test_scope_a_single_quoted_blank_publishes_no_total_either(tmp_path, monkeypatch, quoted):
    """A2 observation, not a defect it may fix here.

    ``normalize_root_text`` strips **double** quotes only, so ``"''"`` survives as a literal
    relative name. That is A1's merged, independently reviewed rule, and the suite pins it to
    ``clips_review.normalize_folder_text`` over a shared table — changing it belongs to a task that
    owns both. What matters for A2 holds either way: it resolves to a *non-existent child* of the
    working directory rather than to the working directory itself, so no total is published, the
    stray media beside it is never counted, and nothing is created.
    """
    monkeypatch.chdir(tmp_path)
    write_file(os.path.join(str(tmp_path), "stray.mp4"), b"S")
    before = sorted(os.listdir(str(tmp_path)))

    item = one([(li.LIBRARY_ROLE, quoted)])
    assert item.totals is None
    assert item.counts is None
    assert item.completeness == li.INVENTORY_INCOMPLETE
    assert item.reason == li.REASON_ROOT_NOT_USABLE
    assert item.root.verdict == li.CONFIGURED_NOT_CREATED
    assert sorted(os.listdir(str(tmp_path))) == before


def test_scope_no_roots_means_no_results(tmp_path):
    result = inspect([])
    assert result.roots == ()
    assert result.complete is False


def test_scope_only_the_roots_handed_in_are_touched(tmp_path, monkeypatch):
    """No drive enumeration, no sibling discovery: every listing lies under a named root."""
    library = str(tmp_path / "library")
    write_file(os.path.join(library, "a.mp4"), b"A")
    write_file(os.path.join(str(tmp_path / "sibling"), "b.mp4"), b"B")

    listed = []
    real_listdir = os.listdir
    real_scandir = os.scandir

    monkeypatch.setattr(os, "listdir", lambda p: (listed.append(str(p)), real_listdir(p))[1])

    def spy_scandir(path=None):
        if path is not None:
            listed.append(str(path))
            return real_scandir(path)
        return real_scandir()

    monkeypatch.setattr(os, "scandir", spy_scandir)
    one([(li.LIBRARY_ROLE, library)])

    outside = [p for p in listed if not li.is_within(p, library)]
    assert outside == [], f"the inspection read outside its root: {outside}"


# =============================================================================================
# T - matrix row 15: nothing on disk changes
# =============================================================================================

@pytest.mark.parametrize("ceiling", [None, 200_000, 2])
def test_t_the_fixture_tree_is_identical_before_and_after(tmp_path, ceiling):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "a", "clip.mp4"), b"A" * 10)
    write_file(os.path.join(root, "b", "clip.mp4"), b"B" * 20)
    write_file(os.path.join(root, "notes.txt"), b"N")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")

    before_root = manifest(root)
    before_destination = manifest(destination)
    inspect([(li.LIBRARY_ROLE, root)], destinations={li.LIBRARY_ROLE: destination},
            max_entries=ceiling)
    assert manifest(root) == before_root
    assert manifest(destination) == before_destination


def test_t_no_folder_anywhere_is_created(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    before = sorted(os.listdir(str(tmp_path)))
    inspect([(li.LIBRARY_ROLE, root), (li.REJECTED_ROLE, str(tmp_path / "nope"))],
            include_rejected=True,
            destinations={li.LIBRARY_ROLE: str(tmp_path / "also_nope")})
    assert sorted(os.listdir(str(tmp_path))) == before


# =============================================================================================
# structural guards
# =============================================================================================

#: Dotted spellings that actually mutate the filesystem or persisted state.
#:
#: Qualified deliberately. A bare-name deny-list containing ``replace`` would flag
#: ``dataclasses.replace`` -- which builds a new frozen record and touches nothing -- and a guard
#: that cries wolf gets deleted. ``os.replace`` is the dangerous one, and the qualifier is what
#: tells them apart.
_MUTATING_DOTTED = {
    "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace", "os.makedirs", "os.mkdir",
    "os.truncate", "os.removedirs", "os.renames",
    "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile",
    "collision.publish",
    "user_prefs.save_preferences",
    "analysis_cache.save_config", "analysis_cache.copy_record", "analysis_cache.import_records",
    "analysis_cache.resolve_location", "analysis_cache._cache_path",
    "cache_admin.clear",
    "input_manager.find_duplicate_groups", "input_manager.fingerprint_file",
}

#: Bare spellings that are unsafe regardless of qualifier.
_MUTATING_BARE = {"rmtree", "makedirs", "removedirs", "renames", "publish", "save_preferences"}

_A2_FUNCTIONS = {"inspect_library", "diagnose_root", "forecast_destination",
                 "duplicate_basenames", "inventory_lines", "_counts_from_input_set",
                 "_basename_key"}


def _dotted(func):
    """``os.path.realpath`` -> ``"os.path.realpath"``; a bare name -> itself; else ``""``."""
    parts = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif parts:
        return ""
    return ".".join(reversed(parts))


def test_guard_no_a2_function_references_a_mutating_call():
    """AST over real source, never substring matching: prose naming a banned token is not a call."""
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    found = set()
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in _A2_FUNCTIONS:
            continue
        found.add(node.name)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            dotted = _dotted(sub.func)
            bare = dotted.rsplit(".", 1)[-1] if dotted else ""
            if dotted in _MUTATING_DOTTED or bare in _MUTATING_BARE:
                offenders.append(f"{node.name} -> {dotted} (line {sub.lineno})")

    assert found == _A2_FUNCTIONS, f"guard went stale, missed {_A2_FUNCTIONS - found}"
    assert offenders == [], f"A2 references mutating calls: {offenders}"


def test_guard_the_mutating_guard_is_not_vacuous():
    """Prove the guard would fire. A guard nobody has seen fail is not a guard."""
    probe = ast.parse(
        "def inspect_library():\n"
        "    os.replace(a, b)\n"
        "    shutil.rmtree(c)\n"
        "    collision.publish(d, e, f, g)\n"
        "    dataclasses.replace(h, i=1)\n"
    )
    hits = []
    for node in probe.body:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            dotted = _dotted(sub.func)
            bare = dotted.rsplit(".", 1)[-1] if dotted else ""
            if dotted in _MUTATING_DOTTED or bare in _MUTATING_BARE:
                hits.append(dotted)
    assert sorted(hits) == ["collision.publish", "os.replace", "shutil.rmtree"]
    assert "dataclasses.replace" not in hits, "the guard must not flag a frozen-record copy"


def test_guard_collision_publish_is_never_called():
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert label != "publish", f"collision.publish called at line {node.lineno}"


def test_guard_the_inventory_module_imports_no_runtime():
    """Belt and braces beside ``test_no_runtime_dependency.py``'s auto-discovery."""
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    for banned in ("gradio", "numpy", "cv2", "librosa", "cupy", "logger", "paths",
                   "video_analysis", "gui", "ffmpeg_processing"):
        assert banned not in imported, f"library_inventory imports {banned}"
    assert imported <= {"__future__", "os", "stat", "time", "dataclasses", "beatsync_fork"}


def test_guard_the_scanner_is_the_only_traversal_authority():
    """A2 must not contain a second ``os.walk``: Tier B is built from A1's primitives."""
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    walks = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
             in {"walk", "fwalk"}]
    assert walks == [], f"library_inventory calls os.walk at {walks}"


def test_guard_the_scanner_call_catches_both_refusals_in_the_right_order():
    """A refusal must never escape unhandled, and the SUBCLASS must be caught first.

    ``InputScanLimitExceeded`` subclasses ``InputScanError``, so an ``except scan_error`` clause
    placed first would swallow the limit case and report ``scan_refused`` instead of
    ``limit_exceeded`` — the same ordering hazard CLAUDE.md records for ``RenderCancelled``. This
    asserts the order in the real source, not just that both names appear.
    """
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        order = []
        for handler in node.handlers:
            if handler.type is None:
                order.append("BARE")
                continue
            order.extend(sub.id for sub in ast.walk(handler.type) if isinstance(sub, ast.Name))
        if not {"scan_error", "limit_error"} <= set(order):
            continue
        calls = [sub for statement in node.body for sub in ast.walk(statement)
                 if isinstance(sub, ast.Call)
                 and (getattr(sub.func, "attr", None) or getattr(sub.func, "id", None))
                 in {"scanner", "scan", "scan_folder"}]
        assert calls, "the guarded try block does not contain the scanner call"
        assert order.index("limit_error") < order.index("scan_error"), (
            "except scan_error precedes except limit_error, so the limit refusal would be "
            "reported as a plain scan refusal")
        assert "BARE" not in order, "a bare except would swallow unrelated failures"
        checked.append(node.lineno)

    assert checked, "the A2 scanner call is not inside a try that catches both refusals"


def test_guard_the_limit_refusal_is_reported_as_the_limit_not_as_a_scan_refusal(tmp_path):
    """The runtime half of the ordering guard above."""
    root = str(tmp_path / "library")
    for index in range(10):
        write_file(os.path.join(root, f"c{index}.mp4"), b"C")
    assert issubclass(InputScanLimitExceeded, InputScanError)
    item = one([(li.LIBRARY_ROLE, root)], max_entries=3)
    assert item.reason == li.REASON_LIMIT_EXCEEDED
    assert item.reason != li.REASON_SCAN_REFUSED
    assert item.counted_at_least == 3


def test_guard_the_scanner_contract_is_required_not_defaulted(tmp_path):
    """No default means no caller can forget it and no refusal can escape unhandled."""
    import inspect as inspect_module

    parameters = inspect_module.signature(li.inspect_library).parameters
    for name in ("scan", "scan_error", "limit_error"):
        assert parameters[name].default is inspect_module.Parameter.empty, (
            f"{name} acquired a default, so a caller can now omit it")
        assert parameters[name].kind is inspect_module.Parameter.KEYWORD_ONLY

    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    with pytest.raises(TypeError):
        li.inspect_library([(li.LIBRARY_ROLE, root)], suffixes=SUFFIXES)
    with pytest.raises(ValueError):
        li.inspect_library([(li.LIBRARY_ROLE, root)], suffixes=SUFFIXES, scan=scan_folder,
                           scan_error=InputScanError, limit_error=None)


def test_guard_the_injected_scanner_is_the_one_that_runs(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs.get("max_entries"))
        return scan_folder(*args, **kwargs)

    one([(li.LIBRARY_ROLE, root)], scan=spy)
    assert calls == [li.MAX_INVENTORY_ENTRIES_PER_ROOT]


def test_guard_the_module_still_imports_only_the_stdlib_set_a1_pinned(tmp_path):
    """The reason the scanner is injected at all. Mirrors A1's own guard, which must stay green."""
    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "os", "stat", "dataclasses"}, imported


def test_guard_the_clock_is_injected_so_findings_carry_an_observed_at(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    assert inspect([(li.LIBRARY_ROLE, root)]).observed_at == 0.0
    stamped = inspect([(li.LIBRARY_ROLE, root)], clock=lambda: 1234.5)
    assert stamped.observed_at == 1234.5


def test_guard_alternatives_are_absent_rather_than_invented_without_the_injection(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    destination = str(tmp_path / "published")
    write_file(os.path.join(destination, "clip.mp4"), b"OLD")

    forecast = one([(li.LIBRARY_ROLE, root)], destinations={li.LIBRARY_ROLE: destination},
                   candidate_names=None).destination
    assert forecast.known is True
    assert forecast.occupied_names == ("clip.mp4",)
    assert forecast.conflicts[0].alternatives == ()


def test_guard_the_scanner_is_called_with_duplicate_detection_off(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    seen = {}

    def spy(path, **kwargs):
        seen.update(kwargs)
        return scan_folder(path, **kwargs)

    one([(li.LIBRARY_ROLE, root)], scan=spy)
    assert seen["detect_duplicates"] is False
    assert seen["recursive"] is True
    assert tuple(seen["extensions"]) == SUFFIXES
    assert seen["max_entries"] == li.MAX_INVENTORY_ENTRIES_PER_ROOT


def test_guard_the_caller_supplies_the_suffixes(tmp_path):
    """A2 must not import the review layer's constant; the caller hands it in."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mov"), b"C")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    narrow = one([(li.LIBRARY_ROLE, root)], suffixes=(".mp4",))
    assert narrow.totals.ready == 1
    assert narrow.totals.unsupported == 1
    wide = one([(li.LIBRARY_ROLE, root)], suffixes=SUFFIXES)
    assert wide.totals.ready == 2


# =============================================================================================
# negative controls
# =============================================================================================

def test_negative_control_totals_would_expose_a_short_count(tmp_path):
    """If `totals` returned `counts` unconditionally, a limited root would publish figures."""
    root = str(tmp_path / "library")
    for index in range(12):
        write_file(os.path.join(root, f"c{index:02d}.mp4"), b"C")
    item = one([(li.LIBRARY_ROLE, root)], max_entries=4)
    assert item.counts is None and item.totals is None
    # The structural half: a hand-built incomplete record with counts still refuses to hand
    # them over, so a future change that attaches counts on failure is still caught here.
    forged = li.RootInventory(role=li.LIBRARY_ROLE, completeness=li.INVENTORY_INCOMPLETE,
                              reason=li.REASON_LIMIT_EXCEEDED,
                              counts=li.MediaCounts(discovered=4, ready=4))
    assert forged.authoritative is False
    assert forged.totals is None


def test_negative_control_a_refused_junction_scan_is_not_complete(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "inside.mp4"), b"I")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 999)
    if make_junction(os.path.join(root, "escape"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    item = one([(li.LIBRARY_ROLE, root)])
    # Had A2 swallowed the refusal and counted what it could, this would be a 1-file total.
    assert item.completeness != li.INVENTORY_COMPLETE
    assert item.totals is None
    result = inspect([(li.LIBRARY_ROLE, root)])
    assert result.complete is False
    assert item in result.incomplete_roots


def test_negative_control_an_unsafe_diagnostic_walk_would_be_caught(tmp_path):
    """Proof the Tier B assertions discriminate: os.walk DOES follow the junction here."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "kept.mp4"), b"K")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 999)
    if make_junction(os.path.join(root, "escape"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    naive = [os.path.join(dp, n)
             for dp, _d, fs in os.walk(root, followlinks=False) for n in fs]
    assert any("ESCAPED" in p for p in naive), (
        "fixture no longer discriminates: os.walk did not follow the junction")

    diag = li.diagnose_root(li.probe_root(root), suffixes=SUFFIXES)
    assert diag.files_seen == 1, "Tier B followed the junction"


def test_negative_control_unknown_destination_is_not_zero_conflicts(tmp_path):
    """A caller reading an empty tuple as "no conflicts" is the bug `known` exists to prevent."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "clip.mp4"), b"C")
    reachable = str(tmp_path / "published")
    os.makedirs(reachable)

    empty_but_known = one([(li.LIBRARY_ROLE, root)],
                          destinations={li.LIBRARY_ROLE: reachable}).destination
    unknown = one([(li.LIBRARY_ROLE, root)],
                  destinations={li.LIBRARY_ROLE: str(tmp_path / "absent")}).destination

    assert empty_but_known.conflicts == () and unknown.conflicts == ()
    assert empty_but_known.known is True
    assert unknown.known is False, "the two cases must be distinguishable despite both being empty"


def test_negative_control_the_counting_invariant_would_catch_a_transcription_slip():
    bad = li.MediaCounts(discovered=10, ready=3, unsupported=1)
    assert bad.reconciles is False
    good = li.MediaCounts(discovered=10, ready=3, unsupported=1, empty=6)
    assert good.reconciles is True
