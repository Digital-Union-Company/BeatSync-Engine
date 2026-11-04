"""The optional ``max_entries`` ceiling: what it bounds, and what it provably does not.

R14-A A2, owner-approved Option A-prime. The ceiling **refuses** rather than returning a short
``InputSet``, for two measured reasons:

1. A short success would be a *containment* lie. Measured on CPython 3.14.3: a ceiling reached
   before a nested junction means the junction is never seen, so the scan that would otherwise have
   refused instead returns figures — and a truncated result is therefore no evidence that the tree
   is contained. ``test_g_*`` pins both halves of that.
2. ``InputSet``'s contract is "this is a total". Adding a success mode that means "this is not a
   total" would weaken the one guarantee the module exists to provide, for every reader of the type.

What the ceiling honestly is: a **classification-entry** ceiling. ``os.walk`` materialises one
directory completely before yielding its first entry, and ``_nested_link_kind`` takes one
``os.lstat`` per child directory before any of that directory's files appear. ``test_f_*`` asserts
that floor deliberately — it exists so no later change can quietly claim a wall-clock or
peak-memory bound.

Fixture trees live in pytest's own ``tmp_path`` (CLAUDE.md / PCBUS-HK-v1: no repository-local
scratch). Real NTFS junctions where the platform allows; injected doubles as the portable controls.
"""

from __future__ import annotations

import ast
import os

import pytest
from conftest import write_file

from beatsync_fork.input_manager import (
    InputScanError,
    InputScanLimitExceeded,
    RejectReason,
    scan_folder,
)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


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


def assert_invariant(result):
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    ), "accounting invariant broken"


def mixed_tree(tmp_path, name="library"):
    """A tree with every rejection reason represented, so the cap is exercised against real counts.

    Returns ``(root, entry_count)``. The entry count is asserted by its own test below, because
    every bound assertion here is meaningless if the fixture's size drifts.
    """
    root = str(tmp_path / name)
    for index in range(6):
        write_file(os.path.join(root, f"a{index:02d}.mp4"), b"A" * (16 + index))
    for index in range(4):
        write_file(os.path.join(root, "nested", f"b{index:02d}.mkv"), b"B" * (32 + index))
    for index in range(3):
        write_file(os.path.join(root, f"note{index}.txt"), b"N")
    write_file(os.path.join(root, "zero.mp4"), b"")
    return root, 14


def manifest(root):
    """``(relpath, size, mtime_ns)`` for every entry, plus the directory set."""
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


# =============================================================================================
# guard the guard
# =============================================================================================

def test_fixture_entry_count_is_what_every_bound_test_assumes(tmp_path):
    root, expected = mixed_tree(tmp_path)
    assert scan_folder(root, recursive=True, detect_duplicates=False).discovered_count == expected


# =============================================================================================
# A - max_entries=None preserves historical scanner results
# =============================================================================================

_PUBLISHED = (
    "root", "recursive", "extensions", "discovered_count", "path_collisions",
    "duplicates_checked",
)


def _published_figures(result):
    return {
        **{name: getattr(result, name) for name in _PUBLISHED},
        "paths": result.paths,
        "rejected": tuple((item.path, item.reason, item.detail) for item in result.rejected),
        "ready_count": result.ready_count,
        "rejected_count": result.rejected_count,
        "supported_count": result.supported_count,
        "total_ready_bytes": result.total_ready_bytes,
        "rejected_by_reason": result.rejected_by_reason(),
        "duplicate_group_count": len(result.duplicate_groups),
        "fingerprint_error_count": len(result.fingerprint_errors),
        "is_ready": result.is_ready(),
    }


@pytest.mark.parametrize("detect_duplicates", [False, True])
@pytest.mark.parametrize("recursive", [False, True])
def test_a_none_is_indistinguishable_from_not_passing_it(tmp_path, detect_duplicates, recursive):
    """The default must be the old behaviour, not merely similar to it."""
    root, _ = mixed_tree(tmp_path)
    # Duplicate content, so detect_duplicates=True has something to find in both modes.
    write_file(os.path.join(root, "dup_one.mp4"), b"SAME" * 8)
    write_file(os.path.join(root, "dup_two.mp4"), b"SAME" * 8)

    without = scan_folder(root, recursive=recursive, detect_duplicates=detect_duplicates)
    explicit = scan_folder(root, recursive=recursive, detect_duplicates=detect_duplicates,
                           max_entries=None)

    assert _published_figures(without) == _published_figures(explicit)
    assert_invariant(explicit)


def test_a_none_keeps_duplicate_detection_working(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "one.mp4"), b"IDENTICAL" * 4)
    write_file(os.path.join(root, "two.mp4"), b"IDENTICAL" * 4)
    result = scan_folder(root, recursive=True, detect_duplicates=True, max_entries=None)
    assert result.duplicates_checked is True
    assert len(result.duplicate_groups) == 1
    assert result.duplicate_groups[0].extra_copies == 1


# =============================================================================================
# B/C - exactly N succeeds, N+1 refuses. The off-by-one lives here and nowhere else.
# =============================================================================================

def test_b_exactly_n_entries_succeeds(tmp_path):
    root, total = mixed_tree(tmp_path)
    result = scan_folder(root, recursive=True, detect_duplicates=False, max_entries=total)
    assert result.discovered_count == total
    assert_invariant(result)


def test_c_one_entry_below_the_count_refuses(tmp_path):
    root, total = mixed_tree(tmp_path)
    with pytest.raises(InputScanLimitExceeded) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=total - 1)
    assert caught.value.limit == total - 1
    assert caught.value.counted_at_least == total - 1


def test_c_a_ceiling_above_the_count_succeeds(tmp_path):
    root, total = mixed_tree(tmp_path)
    result = scan_folder(root, recursive=True, detect_duplicates=False, max_entries=total + 1)
    assert result.discovered_count == total


@pytest.mark.parametrize("delta", [-3, -2, -1])
def test_c_every_ceiling_below_the_count_refuses(tmp_path, delta):
    root, total = mixed_tree(tmp_path)
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=total + delta)


def test_c_the_refusal_names_the_limit_and_says_the_inventory_did_not_complete(tmp_path):
    root, total = mixed_tree(tmp_path)
    with pytest.raises(InputScanLimitExceeded) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=3)
    message = str(caught.value)
    assert "3" in message
    assert "not completed" in message
    assert "no count for this folder is a total" in message
    assert root.lower() in message.lower()


def test_c_counted_at_least_is_a_lower_bound_never_a_total(tmp_path):
    root, total = mixed_tree(tmp_path)
    with pytest.raises(InputScanLimitExceeded) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=5)
    assert caught.value.counted_at_least == 5
    assert caught.value.counted_at_least < total


def test_c_no_input_set_is_produced_when_the_ceiling_is_reached(tmp_path):
    """The whole point of Option A-prime: there is no short InputSet to misread."""
    root, _ = mixed_tree(tmp_path)
    produced = None
    try:
        produced = scan_folder(root, recursive=True, detect_duplicates=False, max_entries=2)
    except InputScanLimitExceeded:
        pass
    assert produced is None


def test_c_the_limit_error_is_an_input_scan_error(tmp_path):
    """So all seven existing `except InputScanError` call sites keep working untouched."""
    root, _ = mixed_tree(tmp_path)
    with pytest.raises(InputScanError):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=1)
    assert issubclass(InputScanLimitExceeded, InputScanError)
    assert issubclass(InputScanLimitExceeded, ValueError)


# =============================================================================================
# D - invalid ceilings refuse BEFORE any filesystem access
# =============================================================================================

@pytest.mark.parametrize("bad", [0, -1, -100])
def test_d_non_positive_integers_raise_value_error(tmp_path, bad):
    root, _ = mixed_tree(tmp_path)
    with pytest.raises(ValueError) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=bad)
    assert not isinstance(caught.value, InputScanError)
    assert "max_entries" in str(caught.value)


@pytest.mark.parametrize("bad", [1.5, "10", [], object(), 2.0])
def test_d_non_integers_raise_value_error(tmp_path, bad):
    """`raises(ValueError)` ALONE proves nothing here -- see the bool test below for why."""
    root, _ = mixed_tree(tmp_path)
    with pytest.raises(ValueError) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=bad)
    assert not isinstance(caught.value, InputScanError)
    assert "max_entries" in str(caught.value)


@pytest.mark.parametrize("bad", [True, False])
def test_d_bool_is_rejected_even_though_it_is_an_int(tmp_path, bad):
    """`isinstance(True, int)` is True in Python, so `max_entries=True` would silently mean 1.

    The `not isinstance(..., InputScanError)` guard is the whole test, and review finding F-6 is
    why: `InputScanError` subclasses `ValueError`, so a bare `pytest.raises(ValueError)` ALSO
    catches `InputScanLimitExceeded`. Restore bool acceptance and `max_entries=True` becomes a
    ceiling of 1, the fixture tree exceeds it, and the limit refusal satisfies the bare `raises`
    -- so the test passed while the behaviour it names was gone. The guard distinguishes
    "rejected as a bad argument" from "accepted, then the scan refused", which is the only
    difference that matters.
    """
    root, _ = mixed_tree(tmp_path)
    with pytest.raises(ValueError) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=bad)
    assert not isinstance(caught.value, InputScanError), (
        "a limit refusal was accepted as proof of argument validation: the ceiling was USED, "
        "not rejected")
    assert "max_entries" in str(caught.value)
    assert "bool" in str(caught.value)


@pytest.mark.parametrize("bad", [True, False, 1.5, "10", 0, -1])
def test_d_every_invalid_ceiling_is_rejected_before_the_filesystem_is_touched(tmp_path,
                                                                             monkeypatch, bad):
    """The validation half of F-6, across every rejected shape rather than just `0`.

    A refusal that happened only after a traversal would be the scan talking, not the validator,
    and the two are told apart here by the filesystem staying untouched.
    """
    root, _ = mixed_tree(tmp_path)
    touched = []
    for name in ("exists", "isdir"):
        real = getattr(os.path, name)

        def spy(path, _real=real, _name=name):
            touched.append(_name)
            return _real(path)

        monkeypatch.setattr(os.path, name, spy)
    monkeypatch.setattr(os, "walk", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the tree was walked despite an invalid ceiling")))

    with pytest.raises(ValueError) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=bad)
    assert not isinstance(caught.value, InputScanError)
    assert touched == []


def test_d_validation_happens_before_the_filesystem_is_touched(tmp_path, monkeypatch):
    """A bad ceiling is a caller bug; it must not depend on, or report on, the folder."""
    touched = []
    for name in ("exists", "isdir"):
        real = getattr(os.path, name)

        def spy(path, _real=real, _name=name):
            touched.append(_name)
            return _real(path)

        monkeypatch.setattr(os.path, name, spy)

    with pytest.raises(ValueError):
        scan_folder(str(tmp_path / "does_not_exist_at_all"), max_entries=0)
    assert touched == []


def test_d_an_invalid_ceiling_outranks_an_invalid_root(tmp_path):
    """Deterministic ordering: the caller's own bug is reported first, always."""
    with pytest.raises(ValueError) as caught:
        scan_folder("", max_entries=0)
    assert not isinstance(caught.value, InputScanError)


# =============================================================================================
# E - both recursive and non-recursive modes honour the ceiling
# =============================================================================================

def test_e_non_recursive_ceiling_counts_only_the_top_level(tmp_path):
    root = str(tmp_path / "library")
    for index in range(5):
        write_file(os.path.join(root, f"top{index}.mp4"), b"T")
    for index in range(9):
        write_file(os.path.join(root, "deep", f"deep{index}.mp4"), b"D")

    flat = scan_folder(root, recursive=False, detect_duplicates=False)
    assert flat.discovered_count == 5

    assert scan_folder(root, recursive=False, detect_duplicates=False,
                       max_entries=5).discovered_count == 5
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=False, detect_duplicates=False, max_entries=4)
    # The same ceiling that is comfortable flat is exceeded recursively.
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=5)


def test_e_recursive_ceiling_spans_subdirectories(tmp_path):
    root = str(tmp_path / "library")
    for folder in ("a", "b", "c"):
        for index in range(4):
            write_file(os.path.join(root, folder, f"{folder}{index}.mp4"), b"X")
    assert scan_folder(root, recursive=True, detect_duplicates=False).discovered_count == 12
    assert scan_folder(root, recursive=True, detect_duplicates=False,
                       max_entries=12).discovered_count == 12
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=11)


# =============================================================================================
# F - what the ceiling counts, and what it provably cannot bound
# =============================================================================================

def test_f_the_ceiling_counts_discovered_entries_not_supported_files(tmp_path):
    """A ceiling on accepted media alone would leave a folder of junk entirely unbounded."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "only.mp4"), b"M")
    for index in range(20):
        write_file(os.path.join(root, f"junk{index:02d}.txt"), b"J")

    full = scan_folder(root, recursive=True, detect_duplicates=False)
    assert full.ready_count == 1
    assert full.discovered_count == 21

    # A ceiling of 5 must refuse even though only ONE supported file exists.
    with pytest.raises(InputScanLimitExceeded) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=5)
    assert caught.value.counted_at_least == 5


def test_f_the_ceiling_stops_the_classification_loop_not_just_the_result(tmp_path, monkeypatch):
    """`os.stat` must run at most `max_entries` times: a post-hoc filter would call it for all."""
    root = str(tmp_path / "library")
    for index in range(40):
        write_file(os.path.join(root, f"c{index:03d}.mp4"), b"C")

    calls = []
    real_stat = os.stat

    def counting_stat(path, *args, **kwargs):
        calls.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=7)
    assert len(calls) <= 7, f"classification continued past the ceiling: {len(calls)} stat calls"


def test_f_one_large_directory_is_still_enumerated_whole(tmp_path, monkeypatch):
    """THE HONEST FLOOR. Deleting this test would let a false bound claim pass review.

    ``os.walk`` fills its ``dirs``/``nondirs`` lists completely before yielding, so a ceiling of 1
    against one big directory still lists every entry in it. The ceiling bounds entries
    *classified*, never entries *listed*, and this asserts exactly that.
    """
    root = str(tmp_path / "library")
    for index in range(300):
        write_file(os.path.join(root, f"f{index:04d}.mp4"), b"F")

    enumerated = []
    real_scandir = os.scandir

    class Counting:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._inner)
            enumerated.append(entry.name)
            return entry

    monkeypatch.setattr(os, "scandir", lambda path=None: Counting(real_scandir(path)))
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=1)
    assert len(enumerated) == 300, (
        "the ceiling must NOT be described as a time or memory bound: one directory is still "
        f"enumerated whole ({len(enumerated)} entries listed for a ceiling of 1)"
    )


def test_f_every_child_directory_is_lstat_ed_before_any_file_is_yielded(tmp_path, monkeypatch):
    """The second half of the floor: the containment gate costs one lstat per child directory."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"T")
    for index in range(25):
        os.makedirs(os.path.join(root, f"sub{index:03d}"), exist_ok=True)

    seen = []
    real_lstat = os.lstat

    def counting_lstat(path, *args, **kwargs):
        seen.append(str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", counting_lstat)
    result = scan_folder(root, recursive=True, detect_duplicates=False, max_entries=1)
    assert result.discovered_count == 1
    subdirectory_probes = [p for p in seen if os.path.basename(p).startswith("sub")]
    assert len(subdirectory_probes) == 25


# =============================================================================================
# G - a ceiling never launders a containment refusal
# =============================================================================================

def test_g_a_junction_in_the_root_still_refuses_with_the_junction_message(tmp_path):
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "aaa.mp4"), b"A")
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 99)
    if make_junction(os.path.join(root, "zzz_link"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    for ceiling in (None, 1, 2, 50):
        with pytest.raises(InputScanError) as caught:
            scan_folder(root, recursive=True, detect_duplicates=False, max_entries=ceiling)
        assert "junction or mount point" in str(caught.value), (
            f"ceiling {ceiling} changed which refusal fired")


def test_g_a_ceiling_reached_before_a_deeper_junction_still_refuses(tmp_path):
    """It refuses for the LIMIT, and publishes nothing — so the tree is never called contained.

    Without a ceiling this tree refuses for the junction. With a small ceiling the junction is
    never reached. Either way no ``InputSet`` is produced, which is why a short result can never be
    mistaken for evidence of containment.
    """
    root = str(tmp_path / "library")
    for index in range(12):
        write_file(os.path.join(root, "aaa_first", f"in{index:02d}.mp4"), b"I")
    os.makedirs(os.path.join(root, "zzz_later"), exist_ok=True)
    outside = str(tmp_path / "outside")
    write_file(os.path.join(outside, "ESCAPED.mp4"), b"X" * 99)
    if make_junction(os.path.join(root, "zzz_later", "link"), outside) is None:
        pytest.skip("junctions unavailable on this platform")

    with pytest.raises(InputScanError) as uncapped:
        scan_folder(root, recursive=True, detect_duplicates=False)
    assert "junction or mount point" in str(uncapped.value)

    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=3)


def test_g_an_unlistable_directory_outranks_the_ceiling(tmp_path, monkeypatch):
    """The stronger, safety-bearing refusal must not be masked by the weaker one."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "aaa_dir", "one.mp4"), b"O")
    write_file(os.path.join(root, "aaa_dir", "two.mp4"), b"T")

    real_scandir = os.scandir
    target = os.path.normcase(os.path.join(root, "aaa_dir"))

    def failing(path=None):
        if path is not None and os.path.normcase(str(path)) == target:
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path) if path is not None else real_scandir()

    monkeypatch.setattr(os, "scandir", failing)
    with pytest.raises(InputScanError) as caught:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=1)
    assert not isinstance(caught.value, InputScanLimitExceeded)
    assert "traversal failed" in str(caught.value)


# =============================================================================================
# H - an explicitly selected junction ROOT still works, ceiling or not
# =============================================================================================

def test_h_a_selected_junction_root_is_usable_with_a_ceiling(tmp_path):
    real = str(tmp_path / "real_library")
    for index in range(4):
        write_file(os.path.join(real, f"clip{index}.mp4"), b"C" * (8 + index))
    link = make_junction(str(tmp_path / "chosen"), real)
    if link is None:
        pytest.skip("junctions unavailable on this platform")

    result = scan_folder(link, recursive=True, detect_duplicates=False, max_entries=4)
    assert result.ready_count == 4
    assert_invariant(result)
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(link, recursive=True, detect_duplicates=False, max_entries=3)


# =============================================================================================
# I - the seven existing callers stay unbounded by default
# =============================================================================================

def test_i_no_existing_call_site_passes_max_entries():
    """Structural proof that production behaviour is untouched.

    AST, never substring matching: this repository has produced false positives five times from
    prose that merely *names* a banned token. ``library_inventory`` is excluded because stage A2 is
    the one deliberate bounded caller, and it is asserted separately below.
    """
    offenders = []
    found = 0
    for directory, _dirnames, filenames in os.walk(_SRC):
        if "__pycache__" in directory:
            continue
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            relative = os.path.relpath(path, _SRC)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if label not in {"scan_folder", "scan_library_folder"}:
                    continue
                found += 1
                if relative.replace(os.sep, "/") == "beatsync_fork/library_inventory.py":
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "max_entries":
                        offenders.append(f"{relative}:{node.lineno}")

    assert found >= 7, f"expected the known call sites, found {found}"
    assert offenders == [], (
        f"these call sites pass max_entries, so they are no longer unbounded: {offenders}")


def test_i_stage_a2_is_the_one_bounded_caller_and_it_passes_the_approved_constant():
    """The positive half: A2 must actually be bounded, by the owner-approved value."""
    from beatsync_fork import library_inventory

    assert library_inventory.MAX_INVENTORY_ENTRIES_PER_ROOT == 200_000

    path = os.path.join(_SRC, "beatsync_fork", "library_inventory.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    bounded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if label not in {"scanner", "scan_folder", "scan"}:
            continue
        bounded.extend(k.arg for k in node.keywords if k.arg == "max_entries")
    assert bounded, "stage A2 does not pass max_entries to the scanner"


def test_i_the_defaults_of_every_public_scanner_parameter_are_unchanged():
    import inspect

    parameters = inspect.signature(scan_folder).parameters
    names = list(parameters)
    assert names == ["root", "recursive", "extensions", "detect_duplicates", "chunk_bytes",
                     "max_entries"], "parameter order changed, which would break positional callers"
    assert parameters["recursive"].default is True
    assert parameters["extensions"].default is None
    assert parameters["detect_duplicates"].default is True
    assert parameters["max_entries"].default is None


def test_i_positional_calls_still_work_exactly_as_before(tmp_path):
    """The new parameter is appended last, so every positional contract survives."""
    root, total = mixed_tree(tmp_path)
    positional = scan_folder(root, True, None, False)
    assert positional.discovered_count == total
    assert positional.duplicates_checked is False


# =============================================================================================
# T - the scan modifies nothing, ceiling or not
# =============================================================================================

@pytest.mark.parametrize("ceiling", [None, 3])
def test_t_the_fixture_is_byte_identical_before_and_after(tmp_path, ceiling):
    root, _ = mixed_tree(tmp_path)
    before = manifest(root)
    try:
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=ceiling)
    except InputScanLimitExceeded:
        pass
    assert manifest(root) == before


def test_t_no_file_is_opened_when_duplicate_detection_is_off(tmp_path, monkeypatch):
    import builtins

    root, _ = mixed_tree(tmp_path)
    opened = []
    real_open = builtins.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    scan_folder(root, recursive=True, detect_duplicates=False, max_entries=None)
    assert opened == []


# =============================================================================================
# negative controls - prove the tests above can actually fail
# =============================================================================================

def test_negative_control_a_truncating_scanner_would_be_caught(tmp_path):
    """A hand-written truncating variant must fail the assertions this suite makes."""
    root, total = mixed_tree(tmp_path)
    truncated = scan_folder(root, recursive=True, detect_duplicates=False)
    short = type(truncated)(
        root=truncated.root, recursive=truncated.recursive, extensions=truncated.extensions,
        files=truncated.files[:2], rejected=(), duplicate_groups=(), fingerprint_errors=(),
        discovered_count=total, path_collisions=0, duplicates_checked=False, scan_seconds=0.0)
    # The invariant is what catches it: a filtered result no longer reconciles.
    assert short.discovered_count != (
        short.ready_count + short.rejected_count + short.path_collisions)


def test_negative_control_an_off_by_one_ceiling_would_be_caught(tmp_path):
    """If the check were `>` instead of `>=`, exactly-N-minus-one would wrongly succeed."""
    root, total = mixed_tree(tmp_path)
    # The real implementation refuses here. A `>` implementation would have returned total-1 rows.
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=total - 1)
    # And exactly-N must still succeed, which rules out the opposite slip.
    assert scan_folder(root, recursive=True, detect_duplicates=False,
                       max_entries=total).discovered_count == total


def test_negative_control_counting_only_supported_files_would_be_caught(tmp_path):
    """Pins `test_f_*`: a ceiling applied to accepted media only would NOT refuse here."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "only.mp4"), b"M")
    for index in range(20):
        write_file(os.path.join(root, f"junk{index:02d}.txt"), b"J")
    result_unbounded = scan_folder(root, recursive=True, detect_duplicates=False)
    assert result_unbounded.ready_count == 1 < 5, "fixture no longer discriminates"
    with pytest.raises(InputScanLimitExceeded):
        scan_folder(root, recursive=True, detect_duplicates=False, max_entries=5)


def test_negative_control_the_reject_reasons_are_all_still_reachable(tmp_path):
    """Guards `_counts_from_input_set`'s transcription: the reasons must exist to be counted."""
    root, _ = mixed_tree(tmp_path)
    counts = scan_folder(root, recursive=True, detect_duplicates=False).rejected_by_reason()
    assert counts[RejectReason.UNSUPPORTED_EXTENSION] == 3
    assert counts[RejectReason.EMPTY_FILE] == 1
