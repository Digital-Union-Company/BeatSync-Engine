"""Containment: a nested junction must never put files from elsewhere into a scan.

The defect these tests pin
--------------------------
``_iter_candidate_paths`` walks with ``os.walk(..., followlinks=False)``. ``os.walk`` decides
descent with ``os.path.islink()``, which is **False** for an NTFS junction, so ``followlinks=False``
does not stop one and the walk enters its target. Measured on CPython 3.14.3 / Windows against the
pristine tree: a junction inside the selected folder contributed 2 files and **1776 of 1808 reported
bytes**, while the ``InputSet`` accounting invariant still held — so nothing in the result looked
wrong.

The contract now::

    nested junction / mount point -> pruned, and the scan REFUSES (InputScanError naming the link)
    nested symlink                -> pruned, scan SUCCEEDS   (pre-existing, intended, tested)
    undecidable link metadata     -> REFUSES (fail closed)
    the selected root itself      -> never classified, so a chosen junction root stays usable

Why a junction refuses and a symlink does not: ``os.walk`` already declines to descend a symlink, and
that silent exclusion is long-standing and pinned by
``test_input_manager_paths.py::test_symlinked_directory_is_not_traversed``. A junction is the case
``os.walk`` actually descends, there is no established semantics to preserve for it, and quietly
dropping it would shrink the operator's source set while still publishing a complete-looking total.
``InputSet`` has no field for "directories I excluded", and adding one would be silent unless all
five callers learned to display it — so the scan refuses and names the link instead.

Refusal is uniform for *every* nested reparse point, including one whose target stays inside the
root. That is deliberate: it needs no path resolution, so it introduces no new undecidable case, and
the operator's remedy is the same either way. The alternative — resolve each link and silently prune
only the contained ones, which loses no files because their real paths are walked anyway — is a
reasonable future refinement, not a correctness requirement.

Real junctions are used where the platform allows (``_winapi.CreateJunction`` needs no elevation);
injected ``os.lstat`` doubles are the cross-platform negative controls, following the idiom in
``test_input_manager_traversal_errors.py``.
"""

from __future__ import annotations

import os
import stat

import pytest
from conftest import write_file

from beatsync_fork.input_manager import InputScanError, RejectReason, scan_folder

REPARSE = 0x400


# =============================================================================================
# helpers
# =============================================================================================

def make_junction(link, target):
    """A real NTFS junction, or ``None`` when the platform or privileges will not allow one."""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return str(link)
    except Exception:
        return None


def fake_directory_stat(*, symlink: bool = False, attributes: int | None = None):
    """A minimal ``os.lstat`` result for a directory."""

    class Result:
        st_mode = (stat.S_IFLNK if symlink else stat.S_IFDIR) | 0o755
        st_size = 0
        st_mtime_ns = 0

    if attributes is not None:
        Result.st_file_attributes = attributes
    return Result()


def patch_lstat_for(monkeypatch, basename: str, replacement):
    """Make ``os.lstat`` answer ``replacement()`` for one directory basename only.

    ``_nested_link_kind`` resolves ``os.lstat`` from the ``os`` namespace at call time, so this is
    what unreadable or exotic link metadata looks like from the scanner's point of view. Everything
    else keeps the real implementation, so pytest's own machinery is untouched.
    """
    real_lstat = os.lstat

    def fake(path, *args, **kwargs):
        if os.path.basename(os.path.normpath(os.fspath(path))) == basename:
            return replacement()
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake)


def assert_invariant(result):
    """The counting invariant must hold on every successful result."""
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    ), "accounting invariant broken"


def escaping_tree(tmp_path, link_name="Escape"):
    """Selected root with media of its own, plus a junction to a sibling holding two levels."""
    root = str(tmp_path / "library")
    inside_top = write_file(os.path.join(root, "top.mp4"), b"T" * 13)
    inside_nested = write_file(os.path.join(root, "set_01", "inside.mp4"), b"A" * 11)
    outside = str(tmp_path / "outside")
    escaped_one = write_file(os.path.join(outside, "ESCAPED_1.mp4"), b"X" * 999)
    escaped_two = write_file(os.path.join(outside, "level2", "ESCAPED_2.mkv"), b"Y" * 777)
    link = make_junction(os.path.join(root, link_name), outside)
    return root, link, (inside_top, inside_nested), (escaped_one, escaped_two)


# =============================================================================================
# A - an ordinary folder is completely unaffected
# =============================================================================================

def test_a_ordinary_recursive_folder_counts_are_unchanged(tmp_path):
    root = str(tmp_path / "library")
    top = write_file(os.path.join(root, "top.mp4"), b"t" * 10)
    nested = write_file(os.path.join(root, "set_01", "clip.mkv"), b"n" * 20)
    deeper = write_file(os.path.join(root, "set_01", "deep", "clip.mp4"), b"d" * 30)

    result = scan_folder(root, detect_duplicates=False)

    assert set(result.paths) == {top, nested, deeper}
    assert result.ready_count == 3 and result.rejected_count == 0
    assert result.discovered_count == 3
    assert result.total_ready_bytes == 60
    assert_invariant(result)


def test_a_two_plain_subdirectories_are_both_still_entered(tmp_path):
    """Guards against a pruning bug that drops ordinary directories along with links."""
    root = str(tmp_path / "library")
    first = write_file(os.path.join(root, "aaa", "one.mp4"), b"1")
    second = write_file(os.path.join(root, "zzz", "two.mp4"), b"2")

    result = scan_folder(root, detect_duplicates=False)

    assert set(result.paths) == {first, second}
    assert_invariant(result)


# =============================================================================================
# B - a nested junction escaping to a sibling is never traversed
# =============================================================================================

def test_b_nested_junction_to_a_sibling_outside_the_root_refuses(tmp_path):
    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)

    message = str(caught.value)
    assert link in message, "the refusal must name the link the operator has to deal with"
    assert "junction or mount point" in message
    assert "select that target as a source folder" in message.lower()
    for path in escaped:
        assert path not in message


def test_b_the_escaped_files_are_never_stated(tmp_path, monkeypatch):
    """Not merely absent from the result: never touched."""
    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    seen: list[str] = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        seen.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    with pytest.raises(InputScanError):
        scan_folder(root, detect_duplicates=False)

    outside_real = os.path.realpath(os.path.dirname(escaped[0]))
    leaked = [p for p in seen if os.path.realpath(p).startswith(outside_real + os.sep)]
    assert leaked == [], f"stat'ed outside the selected root: {leaked}"


def test_b_injected_reparse_child_refuses_on_every_platform(tmp_path, monkeypatch):
    """The cross-platform negative control for B."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    os.makedirs(os.path.join(root, "pretend_link"), exist_ok=True)
    patch_lstat_for(monkeypatch, "pretend_link",
                    lambda: fake_directory_stat(attributes=REPARSE))

    with pytest.raises(InputScanError, match="junction or mount point"):
        scan_folder(root, detect_duplicates=False)


# =============================================================================================
# C - a nested junction to another volume is never traversed
# =============================================================================================

@pytest.mark.skipif(not os.environ.get("BEATSYNC_TEST_XVOL_DIR"),
                    reason="set BEATSYNC_TEST_XVOL_DIR to a writable directory on a DIFFERENT "
                           "volume to exercise a real cross-volume junction")
def test_c_nested_junction_to_another_volume_refuses(tmp_path):
    base = os.environ["BEATSYNC_TEST_XVOL_DIR"]
    if os.path.splitdrive(os.path.abspath(base))[0].upper() == \
            os.path.splitdrive(os.path.abspath(str(tmp_path)))[0].upper():
        pytest.skip("BEATSYNC_TEST_XVOL_DIR is on the same volume as tmp_path")

    target = os.path.join(base, "scanner_junction_r1_fixture")
    os.makedirs(target, exist_ok=True)
    escaped = write_file(os.path.join(target, "OTHER_VOLUME.mp4"), b"V" * 321)

    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    link = make_junction(os.path.join(root, "XVol"), target)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    assert os.path.splitdrive(os.path.realpath(link))[0].upper() != \
        os.path.splitdrive(os.path.abspath(root))[0].upper(), "the volumes really do differ"

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)
    assert link in str(caught.value)
    assert os.path.exists(escaped), "the scan must not have disturbed the other volume"


# =============================================================================================
# D - a junction whose target stays inside the root
# =============================================================================================

def test_d_contained_junction_alias_is_refused_not_traversed(tmp_path):
    """D-2: descent is prevented for ALL nested reparse points, contained ones included.

    Nothing is lost by refusing here — the aliased files are reachable at their real paths — but
    the operator is told rather than silently given a differently-shaped result.
    """
    root = str(tmp_path / "library")
    real_clip = write_file(os.path.join(root, "real", "clip.mp4"), b"R" * 50)
    link = make_junction(os.path.join(root, "Alias"), os.path.join(root, "real"))
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)
    assert link in str(caught.value)
    assert os.path.exists(real_clip)


def test_d_a_self_referential_junction_cannot_make_the_walk_unbounded(tmp_path):
    """A junction pointing at its own root: pruned at the first level, so no runaway descent."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    link = make_junction(os.path.join(root, "Loop"), root)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)
    assert link in str(caught.value)


# =============================================================================================
# E - an explicitly selected junction root still works
# =============================================================================================

def test_e_explicitly_selected_junction_root_is_usable(tmp_path):
    """The operator chose this folder, so its own link-ness is not a hazard to report."""
    real = str(tmp_path / "real_library")
    clip = write_file(os.path.join(real, "clip.mp4"), b"x" * 7)
    nested = write_file(os.path.join(real, "deeper", "clip2.mkv"), b"y" * 9)
    link = make_junction(str(tmp_path / "chosen_link"), real)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    result = scan_folder(link, detect_duplicates=False)

    assert result.ready_count == 2
    assert result.total_ready_bytes == 16
    assert {os.path.realpath(p) for p in result.paths} == {
        os.path.realpath(clip), os.path.realpath(nested)
    }
    assert_invariant(result)


def test_e_a_junction_root_whose_own_child_is_a_junction_still_refuses(tmp_path):
    """Selecting a junction root does not whitelist links *inside* it."""
    real = str(tmp_path / "real_library")
    write_file(os.path.join(real, "clip.mp4"), b"x")
    outside = str(tmp_path / "far")
    write_file(os.path.join(outside, "FAR.mp4"), b"f" * 100)
    inner = make_junction(os.path.join(real, "Inner"), outside)
    link = make_junction(str(tmp_path / "chosen_link"), real)
    if inner is None or link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    with pytest.raises(InputScanError, match="junction or mount point"):
        scan_folder(link, detect_duplicates=False)


# =============================================================================================
# F - two levels below an escaping junction
# =============================================================================================

def test_f_neither_level_below_an_escaping_junction_is_counted(tmp_path, monkeypatch):
    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")
    first_level, second_level = escaped

    opened: list[str] = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    with pytest.raises(InputScanError):
        scan_folder(root, detect_duplicates=False)

    for path in (first_level, second_level):
        assert path not in opened
        assert os.path.realpath(path) not in [os.path.realpath(p) for p in opened]


# =============================================================================================
# G - undecidable metadata fails closed
# =============================================================================================

def test_g_unreadable_child_directory_metadata_fails_closed(tmp_path, monkeypatch):
    """An lstat failure is not evidence of an ordinary folder."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    os.makedirs(os.path.join(root, "opaque"), exist_ok=True)

    def boom():
        raise PermissionError(13, "Permission denied")

    patch_lstat_for(monkeypatch, "opaque", boom)

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)
    message = str(caught.value)
    assert "opaque" in message
    assert "unknown whether reading it would leave the selected folder" in message


def test_g_missing_link_attributes_fail_closed_on_windows(tmp_path, monkeypatch):
    """Absent ``st_file_attributes`` is undecidable on Windows and ordinary on POSIX."""
    root = str(tmp_path / "library")
    top = write_file(os.path.join(root, "top.mp4"), b"t")
    nested = write_file(os.path.join(root, "blind", "clip.mp4"), b"n")
    patch_lstat_for(monkeypatch, "blind", lambda: fake_directory_stat())

    if os.name == "nt":
        with pytest.raises(InputScanError, match="no link metadata"):
            scan_folder(root, detect_duplicates=False)
    else:
        result = scan_folder(root, detect_duplicates=False)
        assert set(result.paths) == {top, nested}
        assert_invariant(result)


def test_g_a_plain_directory_is_not_mistaken_for_a_link(tmp_path, monkeypatch):
    """The negative control for G: attributes present, reparse bit clear -> ordinary folder."""
    root = str(tmp_path / "library")
    top = write_file(os.path.join(root, "top.mp4"), b"t")
    nested = write_file(os.path.join(root, "plain", "clip.mp4"), b"n")
    patch_lstat_for(monkeypatch, "plain", lambda: fake_directory_stat(attributes=0x10))

    result = scan_folder(root, detect_duplicates=False)

    assert set(result.paths) == {top, nested}
    assert_invariant(result)


# =============================================================================================
# H - a directory disappearing mid-inspection
# =============================================================================================

def test_h_directory_vanishing_during_inspection_yields_no_partial_input_set(tmp_path,
                                                                            monkeypatch):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    doomed = os.path.join(root, "doomed")
    write_file(os.path.join(doomed, "clip.mp4"), b"d")

    real_lstat = os.lstat

    def vanishing(path, *args, **kwargs):
        if os.path.basename(os.path.normpath(os.fspath(path))) == "doomed":
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", vanishing)

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, detect_duplicates=False)
    assert "doomed" in str(caught.value)


def test_h_a_directory_that_cannot_be_listed_still_raises(tmp_path, monkeypatch):
    """The pre-existing all-or-nothing behaviour is not weakened by the new guard."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    write_file(os.path.join(root, "blocked", "hidden.mp4"), b"h")

    real_scandir = os.scandir

    def failing(path=".", *args, **kwargs):
        if os.path.basename(os.path.normpath(os.fspath(path))) == "blocked":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing)

    with pytest.raises(InputScanError):
        scan_folder(root, detect_duplicates=False)


# =============================================================================================
# I - directory symlink behaviour is preserved exactly
# =============================================================================================

def test_i_real_directory_symlink_is_still_a_successful_silent_exclusion(tmp_path):
    """Mirrors test_symlinked_directory_is_not_traversed: success, not a refusal."""
    root = str(tmp_path / "library")
    real = write_file(os.path.join(root, "real", "clip.mp4"), b"x")
    try:
        os.symlink(os.path.join(root, "real"), os.path.join(root, "alias"),
                   target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover - platform
        pytest.skip(f"directory symlinks unavailable: {exc}")

    result = scan_folder(root, detect_duplicates=False)

    assert result.paths == (real,)
    assert result.discovered_count == 1
    assert_invariant(result)


def test_i_injected_symlink_child_is_excluded_without_refusing(tmp_path, monkeypatch):
    """The cross-platform form of I, so the symlink contract is pinned where symlinks need
    privileges. A symlink also carries the reparse attribute, so this proves the symlink check
    is consulted *first*."""
    root = str(tmp_path / "library")
    top = write_file(os.path.join(root, "top.mp4"), b"t")
    write_file(os.path.join(root, "aliased", "clip.mp4"), b"a")
    patch_lstat_for(monkeypatch, "aliased",
                    lambda: fake_directory_stat(symlink=True, attributes=REPARSE))

    result = scan_folder(root, detect_duplicates=False)

    assert result.paths == (top,), "a symlinked directory is excluded, not refused"
    assert result.discovered_count == 1
    assert_invariant(result)


# =============================================================================================
# J - non-recursive scanning is untouched
# =============================================================================================

def test_j_nonrecursive_scan_is_unchanged_by_the_guard(tmp_path):
    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert result.paths == (inside[0],), "only the top-level file, and no refusal"
    assert result.discovered_count == 1
    assert_invariant(result)
    for path in escaped:
        assert path not in result.paths


def test_j_nonrecursive_scan_never_consults_link_metadata(tmp_path, monkeypatch):
    """The non-recursive branch must not acquire the new guard's failure modes."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")
    os.makedirs(os.path.join(root, "opaque"), exist_ok=True)

    def boom():
        raise PermissionError(13, "Permission denied")

    patch_lstat_for(monkeypatch, "opaque", boom)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert result.ready_count == 1
    assert_invariant(result)


# =============================================================================================
# K - mixed supported / unsupported accounting survives
# =============================================================================================

def test_k_mixed_media_accounting_is_retained(tmp_path):
    root = str(tmp_path / "library")
    good = write_file(os.path.join(root, "set_01", "clip.mp4"), b"g" * 5)
    write_file(os.path.join(root, "set_01", "notes.txt"), b"n" * 3)
    write_file(os.path.join(root, "set_02", "poster.jpg"), b"j" * 4)
    empty = write_file(os.path.join(root, "set_02", "empty.mkv"), b"")

    result = scan_folder(root, detect_duplicates=False)

    assert result.paths == (good,)
    assert result.discovered_count == 4
    assert result.rejected_count == 3
    reasons = result.rejected_by_reason()
    assert reasons[RejectReason.UNSUPPORTED_EXTENSION] == 2
    assert reasons[RejectReason.EMPTY_FILE] == 1
    assert empty in {item.path for item in result.rejected}
    assert_invariant(result)


# =============================================================================================
# L - the invariant holds on every successful result produced here
# =============================================================================================

def test_l_invariant_holds_across_every_successful_shape(tmp_path):
    """One place that re-asserts the invariant over several shapes at once."""
    plain = str(tmp_path / "plain")
    write_file(os.path.join(plain, "a.mp4"), b"a")
    write_file(os.path.join(plain, "sub", "b.mkv"), b"bb")
    write_file(os.path.join(plain, "sub", "c.txt"), b"ccc")

    results = [
        scan_folder(plain, detect_duplicates=False),
        scan_folder(plain, recursive=False, detect_duplicates=False),
        scan_folder(plain, extensions=(".mp4",), detect_duplicates=False),
    ]

    chosen = make_junction(str(tmp_path / "chosen"), plain)
    if chosen is not None:
        results.append(scan_folder(chosen, detect_duplicates=False))

    assert len(results) >= 3
    for result in results:
        assert_invariant(result)
        for item in result.files:
            assert item.size > 0


# =============================================================================================
# M - a refusal reaches the caller as a refusal, never as an empty success
# =============================================================================================

def test_m_clips_review_discovery_reports_the_refusal(tmp_path):
    """The Video Review consumer turns it into a visible error, not an empty gallery."""
    from beatsync_fork import clips_review

    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    found = clips_review.discover_clips(root, True)

    assert found.error, "the refusal must not become a successful empty discovery"
    assert link in found.error
    assert found.files == ()
    assert found.discovered_count == 0


def test_m_audio_review_discovery_reports_the_refusal(tmp_path):
    from beatsync_fork import audio_review

    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    found = audio_review.discover_audio(root, True)

    assert found.error and link in found.error
    assert found.files == ()


def test_m_the_source_gate_invalidates_instead_of_confirming(tmp_path):
    """input_session must drop any confirmation rather than accept a refused scan."""
    from beatsync_fork import input_session

    root, link, inside, escaped = escaping_tree(tmp_path)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    state = input_session.SourceSessionState(folder_path=root, recursive=True)
    after = input_session.scan_folder_action(state)

    assert after.current is None and after.confirmed is None
    assert "SCAN FAILED" in after.report_text
    assert link in after.report_text


def test_m_every_scan_folder_caller_in_the_repository_catches_the_refusal():
    """Structural: the refusal is only safe because every caller already handles it.

    Checked by ``ast`` over the real source rather than by importing, because two of the call sites
    live in ``gui.py``, which pulls in Gradio and the analysis runtime. This is the guard that stops
    a *future* caller from quietly turning a refusal into an empty success.
    """
    import ast

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    names = {"scan_folder", "scan_library_folder"}
    unguarded: list[str] = []
    found = 0

    for directory, _dirnames, filenames in os.walk(src):
        if "__pycache__" in directory:
            continue
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)

            guarded: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                # Both spellings must count: `except InputScanError` is an ast.Name, while
                # `except input_manager.InputScanError` is an ast.Attribute, and four of the
                # real call sites use the dotted form.
                caught: set[str] = set()
                for handler in node.handlers:
                    if handler.type is None:
                        continue
                    for sub in ast.walk(handler.type):
                        if isinstance(sub, ast.Name):
                            caught.add(sub.id)
                        elif isinstance(sub, ast.Attribute):
                            caught.add(sub.attr)
                if "InputScanError" not in caught:
                    continue
                for statement in node.body:
                    for sub in ast.walk(statement):
                        if isinstance(sub, ast.Call):
                            guarded.add(id(sub))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                label = getattr(target, "attr", None) or getattr(target, "id", None)
                if label not in names:
                    continue
                found += 1
                if id(node) not in guarded:
                    unguarded.append(f"{os.path.relpath(path, src)}:{node.lineno}")

    # A floor, not an exact count: seven call sites exist today (two in gui.py, one each in
    # raw_cutter_export, audio_review, clips_review, and two in input_session). The floor is what
    # stops this test passing vacuously if the walk or the name matching ever stops finding them.
    assert found >= 5, f"expected to find the known call sites, found {found}"
    assert unguarded == [], (
        "these scan_folder call sites do not catch InputScanError, so a containment refusal "
        f"would escape or be mistaken for an empty result: {unguarded}"
    )


def test_m_a_refused_rescan_cannot_silently_keep_an_old_confirmation(tmp_path):
    """A junction appearing after a clean confirmation must invalidate, not pass through."""
    from beatsync_fork import input_session

    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"t")

    state = input_session.SourceSessionState(folder_path=root, recursive=True)
    scanned = input_session.scan_folder_action(state)
    assert scanned.current is not None, "baseline: a clean folder scans"

    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 50)
    link = make_junction(os.path.join(root, "Escape"), outside)
    if link is None:
        pytest.skip("this platform or privilege level cannot create a junction")

    rescanned = input_session.scan_folder_action(scanned)

    assert rescanned.current is None and rescanned.confirmed is None
    assert "SCAN FAILED" in rescanned.report_text
