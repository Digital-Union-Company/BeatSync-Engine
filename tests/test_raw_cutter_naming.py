"""Raw Cutter R4-C: synthetic, source-independent output naming.

[FORK] Digital-Union. The feature is one opt-in checkbox, **Rename output clips**, default OFF.
This suite exists to hold two separate claims apart, because proving only one of them would be
worthless:

1. **With the checkbox off, nothing changed.** Not "nothing important" -- the exact filenames, the
   exact run-directory name and the exact mirrored batch layout, asserted as literal strings and
   literal tuples with the synthetic mode as the negative control. A mode that quietly altered
   today's naming would break every operator's existing folder of clips.
2. **With it on, no generated name carries the source's identity.** Not the basename, not the
   stem, not the parent folder's name, and not in a staging file either -- a staging name that
   leaked it would leak for the whole length of an encode, which on a five-minute clip is the
   entire visible window.

Three tiers, and the boundary between them is deliberate:

* the **pure contract** (`beatsync_fork.raw_cutter`, `beatsync_fork.raw_cutter_batch`) is imported
  and called as ordinary code, which is what the stdlib-only hard rule buys;
* the **export adapter** is reached by AST-extracting `export_segments` and its two helpers and
  executing them against a synthesised parent and a REAL temporary filesystem, with FFmpeg
  replaced by a writer. So the rename, the no-replace publication, the containment check and the
  staging cleanup are the shipped code running on real files -- only the encode is stubbed;
* the **GUI seam** is AST assertions about `gui.py`, which cannot be imported on a bare
  interpreter.

Real-FFmpeg media integrity -- that the mode adds no transcode and no second pass, and that CFR
and VFR frame timing are untouched -- is in `tests/test_raw_cutter_ffmpeg.py`, where the binary
is available. It is NOT claimed here.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import subprocess

import pytest

from beatsync_fork import raw_cutter as rc
from beatsync_fork import raw_cutter_batch as batch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_EXPORT = os.path.join(_REPO_ROOT, "src", "raw_cutter_export.py")

#: One fixed date for every expectation below, so a test can never pass because it recomputed the
#: same clock reading the code did. 18 October 2026 -- the date in the owner's own examples.
DATE = "18102026"
OTHER_DATE = "19102026"

ON = rc.OutputNaming(synthetic=True, source_index=1, job_date=DATE)
OFF = rc.OutputNaming(synthetic=False, source_index=1, job_date=DATE)


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined")


def _code_only(node) -> str:
    """`ast.unparse` with docstrings removed, so prose can never satisfy a code assertion.

    The project has been bitten four times by an assertion that a name is ABSENT passing or
    failing on a docstring that merely mentioned it. Strip first, always.
    """
    clone = ast.parse(ast.unparse(node))
    for element in ast.walk(clone):
        if isinstance(element, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(element, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                element.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(clone))


def _widget(tree: ast.Module, name: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"{name} is not assigned a widget")


def _click(tree: ast.Module, widget: str, event: str = "click") -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == event
                and getattr(node.func.value, "id", None) == widget):
            return node
    raise AssertionError(f"{widget}.{event}(...) not found")


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node) -> list[str]:
    return [getattr(e, "id", ast.unparse(e)) for e in node.elts]


# =============================================================================================
# 1. Default OFF: the exact legacy spelling, with synthetic as the negative control
# =============================================================================================

def test_no_naming_argument_at_all_is_the_legacy_spelling():
    """The compatibility floor. Every pre-R4-C call site passes nothing, so this is what it gets.

    Literal strings on purpose: a substring check for `_part_` would also pass a name that had
    started carrying a date or lost its stem.
    """
    assert rc.segment_filename(r"J:\Footage\long_recording.mkv", 1, 3) == \
        "long_recording_part_001.mp4"
    assert rc.segment_filename(r"J:\Footage\long_recording.mkv", 12, 12) == \
        "long_recording_part_012.mp4"
    assert rc.run_directory_name(r"J:\Footage\long_recording.mkv", "20261018_153000") == \
        "long_recording_raw_parts_20261018_153000"
    assert rc.staging_filename(r"J:\Footage\long_recording.mkv", 2, 3, "a1b2c3") == \
        ".long_recording_part_002.a1b2c3.part.mp4"


def test_an_explicitly_off_naming_record_is_identical_to_passing_nothing():
    """`OFF` must be indistinguishable from the default, or the GUI would have two legacy modes."""
    for index, total in ((1, 1), (7, 9), (1000, 1200)):
        assert rc.segment_filename("clip.mp4", index, total, naming=OFF) == \
            rc.segment_filename("clip.mp4", index, total)
        assert rc.staging_filename("clip.mp4", index, total, "tok", naming=OFF) == \
            rc.staging_filename("clip.mp4", index, total, "tok")
    assert rc.run_directory_name("clip.mp4", "stamp", naming=OFF) == \
        rc.run_directory_name("clip.mp4", "stamp")


def test_the_default_naming_record_is_the_legacy_mode():
    """The checkbox default is OFF, so the record's own default has to agree with it."""
    default = rc.OutputNaming()
    assert default.synthetic is False
    assert default.source_index == 1
    assert default.job_date == ""
    assert rc.segment_filename("clip.mp4", 1, 1, naming=default) == "clip_part_001.mp4"


def test_the_negative_control_synthetic_naming_really_does_differ():
    """Without this, every assertion above would also pass against a feature that did nothing."""
    legacy = rc.segment_filename(r"J:\Footage\long_recording.mkv", 1, 3)
    synthetic = rc.segment_filename(r"J:\Footage\long_recording.mkv", 1, 3, naming=ON)
    assert legacy != synthetic
    assert legacy == "long_recording_part_001.mp4"
    assert synthetic == "File001_18102026_001.mp4"


def test_junk_in_place_of_a_naming_record_reads_as_legacy_never_as_synthetic():
    """A decision this consequential must not be flipped by an unrelated object.

    Duck typing would: anything carrying a truthy `synthetic` attribute would silently switch the
    mode, and the mode is what the whole no-leak guarantee rests on.
    """
    class Impostor:
        synthetic = True
        source_index = 1
        job_date = DATE
        source_tag = "File001"
        date_field = DATE

    for junk in (None, True, 1, "yes", object(), Impostor(), {"synthetic": True}):
        assert rc.segment_filename("clip.mp4", 1, 1, naming=junk) == "clip_part_001.mp4"
        assert rc.run_directory_name("clip.mp4", "s", naming=junk) == "clip_raw_parts_s"


# =============================================================================================
# 2. The synthetic filename format
# =============================================================================================

def test_one_source_one_clip():
    assert rc.segment_filename("anything.mp4", 1, 1, naming=ON) == "File001_18102026_001.mp4"


def test_one_source_several_clips_are_numbered_within_that_source():
    names = [rc.segment_filename("anything.mp4", index, 3, naming=ON) for index in (1, 2, 3)]
    assert names == ["File001_18102026_001.mp4",
                     "File001_18102026_002.mp4",
                     "File001_18102026_003.mp4"]


def test_several_sources_each_restart_their_own_clip_counter():
    """The owner's own examples. `File002_..._001` is the SECOND source's FIRST clip -- the
    trailing counter is per-source, not a running total across the batch."""
    assert [rc.segment_filename("x.mp4", clip, 2, naming=ON.for_source(source))
            for source, clip in ((1, 1), (1, 2), (2, 1), (2, 2))] == \
        ["File001_18102026_001.mp4", "File001_18102026_002.mp4",
         "File002_18102026_001.mp4", "File002_18102026_002.mp4"]


def test_the_owners_worked_example_is_reproduced_exactly():
    assert rc.segment_filename("x.mp4", 14, 20, naming=ON.for_source(10)) == \
        "File010_18102026_014.mp4"


@pytest.mark.parametrize("extension", [".mp4", ".mkv", ""])
def test_the_extension_is_still_the_callers_to_choose(extension):
    assert rc.segment_filename("x.mp4", 1, 1, extension, naming=ON) == \
        f"File001_18102026_001{extension}"


def test_the_whole_name_is_ascii_safe_and_contains_no_path_separator():
    for path in (r"J:\Footage\über\Ünïcödé — ★ clip.mp4", "a" * 400 + ".mp4",
                 "con.mp4", "NUL.MP4", "lpt1.mp4", "....mp4", " .mp4"):
        name = rc.segment_filename(path, 1, 1, naming=ON)
        assert name == "File001_18102026_001.mp4", path
        assert name.isascii()
        assert "/" not in name and "\\" not in name and ".." not in name


# ---- the no-leak guarantee itself -----------------------------------------------------------

#: Source paths chosen so that every one of them has a basename a legacy name WOULD carry. Each
#: entry is `(path, tokens that must not appear)`, and each token is one the LEGACY name really
#: does carry -- asserted below, so the no-leak claim has a negative control rather than passing
#: on tokens no spelling of the name would ever hold.
#:
#: Note the unicode case names `clip` rather than `Ünïcödé`: `safe_stem` already replaces
#: non-ASCII with `_`, so the legacy name is `n_c_d_clip_part_001.mp4` -- the ASCII remnant is
#: what a legacy name leaks from such a file, and it is what the synthetic name must not.
_LEAKY_SOURCES = [
    (r"J:\Footage\long_recording.mkv", ("long_recording", "recording")),
    (r"J:\cameraA\sub\video01.mp4", ("video01",)),
    (r"J:\Footage\Ünïcödé clip.mp4", ("clip",)),
    (r"J:\Footage\%s.mp4" % ("verylongsourcename" * 6), ("verylongsourcename",)),
    (r"J:\Footage\con.mp4", ("con",)),
    (r"J:\Footage\lpt1.mp4", ("lpt1",)),
    (r"J:\Private Wedding 2026\bride and groom.mov", ("bride", "groom")),
]


@pytest.mark.parametrize("path,forbidden", _LEAKY_SOURCES)
def test_no_synthetic_name_carries_any_part_of_the_source_identity(path, forbidden):
    """The point of the feature, measured on every name the cutter generates.

    Note `con` is checked case-insensitively against the WHOLE name, which is strictly harder than
    it looks -- it would catch an implementation that kept a sanitised stem anywhere, including
    inside the staging marker.
    """
    produced = [
        rc.segment_filename(path, 1, 3, naming=ON),
        rc.segment_filename(path, 3, 3, naming=ON),
        rc.staging_filename(path, 1, 3, "a1b2c3", naming=ON),
        rc.run_directory_name(path, "20261018_153000", naming=ON),
    ]
    for name in produced:
        for token in forbidden:
            assert token.casefold() not in name.casefold(), (name, token)


@pytest.mark.parametrize("path,forbidden", _LEAKY_SOURCES)
def test_the_same_names_DO_leak_the_basename_when_the_mode_is_off(path, forbidden):
    """The negative control for the test above: at least one legacy name really does carry it.

    Without this, a builder that returned a constant for both modes would satisfy the no-leak
    assertion and nobody would notice that the default mode had been destroyed.
    """
    legacy = " ".join((
        rc.segment_filename(path, 1, 3),
        rc.staging_filename(path, 1, 3, "a1b2c3"),
        rc.run_directory_name(path, "20261018_153000"),
    ))
    assert all(token.casefold() in legacy.casefold() for token in forbidden), legacy


def test_two_sources_with_identical_basenames_in_different_folders_get_different_names():
    """`cameraA/video01.mp4` and `cameraB/video01.mp4` -- the collision the batch layout exists
    for. Under synthetic naming it cannot arise, because the name comes from the position."""
    first = rc.segment_filename(r"J:\cameraA\video01.mp4", 1, 1, naming=ON.for_source(1))
    second = rc.segment_filename(r"J:\cameraB\video01.mp4", 1, 1, naming=ON.for_source(2))
    assert first == "File001_18102026_001.mp4"
    assert second == "File002_18102026_001.mp4"
    assert first != second


# =============================================================================================
# 3. Counters: minimum three digits, wider when needed, never truncated and never wrapped
# =============================================================================================

@pytest.mark.parametrize("index,expected", [
    (1, "File001"), (9, "File009"), (10, "File010"), (99, "File099"), (100, "File100"),
    (999, "File999"), (1000, "File1000"), (1234, "File1234"), (99999, "File99999"),
])
def test_the_source_field_is_at_least_three_digits_and_widens_instead_of_wrapping(index, expected):
    assert rc.synthetic_source_tag(index) == expected
    assert rc.segment_filename("x.mp4", 1, 1, naming=ON.for_source(index)) == \
        f"{expected}_18102026_001.mp4"


@pytest.mark.parametrize("index,total,expected", [
    (1, 1, "001"), (1, 999, "001"), (999, 999, "999"),
    (1, 1000, "0001"), (1000, 1000, "1000"), (1000, 1200, "1000"), (1200, 1200, "1200"),
    (12345, 12345, "12345"),
])
def test_the_clip_field_is_at_least_three_digits_and_widens_instead_of_wrapping(
        index, total, expected):
    assert rc.segment_filename("x.mp4", index, total, naming=ON) == \
        f"File001_18102026_{expected}.mp4"


def test_more_than_999_of_either_counter_stays_unique():
    """Uniqueness is the property that actually matters: two clips sharing a name would be a
    no-replace refusal at best and a lost clip at worst."""
    names = [rc.segment_filename("x.mp4", clip, 1200, naming=ON.for_source(source))
             for source in range(1, 1201) for clip in (1, 1200)]
    assert len(set(names)) == len(names)


def test_the_clip_counter_is_padded_to_the_total_so_one_sources_clips_always_sort():
    """Within a source the field width comes from the real clip TOTAL, exactly as the legacy
    name's does, so every clip of a 1200-clip source is four digits and sorts correctly."""
    names = [rc.segment_filename("x.mp4", clip, 1200, naming=ON) for clip in range(1, 1201)]
    assert names == sorted(names)
    assert names[0] == "File001_18102026_0001.mp4"
    assert names[-1] == "File001_18102026_1200.mp4"


# =============================================================================================
# 3b. The job-wide source width (R4-C correction of the published Q-1 limitation)
#
# The source field is padded to the width of the job's COMPLETE frozen source count, decided once
# at submission. The superseded spelling derived the width from each index, which left a >999
# batch mixing three- and four-digit folders so that an ordinary filename sort read
# `...File100, File1000, File1001, File101...`. Identity and uniqueness were never affected; the
# browsing order was, and the owner chose to correct it.
# =============================================================================================

#: Every total the width contract is asserted at, including both sides of all three boundaries.
_WIDTH_TOTALS = (1, 2, 9, 10, 99, 100, 999, 1000, 1001, 1200, 10000)


def _job(total, date=DATE):
    """One job record exactly as a batch of ``total`` sources freezes it at submission."""
    return rc.OutputNaming(synthetic=True, job_date=date, source_total=total)


def _expected_width(total):
    """The contract, written independently of the implementation: `max(3, len(str(total)))`."""
    return max(3, len(str(total)))


@pytest.mark.parametrize("total", _WIDTH_TOTALS)
def test_every_source_in_one_job_shares_the_width_of_the_frozen_total(total):
    """The load-bearing property: ONE width per job, derived from the total and nothing else.

    Asserted across the whole frozen list rather than at a sample, because a mixed width is
    exactly what the superseded spelling produced and a sampled test could step over it.
    """
    width = _expected_width(total)
    tags = [_job(total).for_source(position).source_tag
            for position in range(1, total + 1)]
    assert len(tags) == total
    assert {len(tag) for tag in tags} == {len(rc.SYNTHETIC_PREFIX) + width}, \
        "one job must not mix source-index widths"
    assert tags[0] == f"{rc.SYNTHETIC_PREFIX}{1:0{width}d}"
    assert tags[-1] == f"{rc.SYNTHETIC_PREFIX}{total:0{width}d}"
    assert rc.source_index_width(total) == width


@pytest.mark.parametrize("total", _WIDTH_TOTALS)
def test_the_source_field_sorts_lexically_and_numerically_at_every_scale(total):
    """The requirement the correction exists for: plain lexical order IS discovery order."""
    tags = [_job(total).for_source(position).source_tag
            for position in range(1, total + 1)]
    assert tags == sorted(tags), "ordinary filename sorting must follow the source order"
    assert len(set(tags)) == total, "and the tags must still be unique"


@pytest.mark.parametrize("total", _WIDTH_TOTALS)
def test_no_source_index_is_truncated_or_wrapped_at_any_total(total):
    """Every tag must read back as its own position -- no lost leading digit, no modulo."""
    prefix = len(rc.SYNTHETIC_PREFIX)
    for position in (1, total // 2 or 1, total):
        tag = _job(total).for_source(position).source_tag
        assert tag.startswith(rc.SYNTHETIC_PREFIX)
        digits = tag[prefix:]
        assert digits.isdigit() and int(digits) == position, tag


@pytest.mark.parametrize("total,position,expected", [
    (1, 1, "File001"),                                    # one-source Single Video job
    (10, 1, "File001"), (10, 2, "File002"), (10, 10, "File010"),
    (999, 1, "File001"), (999, 999, "File999"),
    (1000, 1, "File0001"), (1000, 2, "File0002"),
    (1000, 999, "File0999"), (1000, 1000, "File1000"),
    (1001, 1, "File0001"), (1001, 1001, "File1001"),
    (10000, 1, "File00001"), (10000, 10000, "File10000"),
])
def test_the_owners_worked_examples_are_reproduced_exactly(total, position, expected):
    """The owner's own examples, as literal strings, for both the tag and the whole filename."""
    naming = _job(total).for_source(position)
    assert naming.source_tag == expected
    assert rc.segment_filename("anything.mp4", 1, 1, naming=naming) == \
        f"{expected}_{DATE}_001.mp4"


@pytest.mark.parametrize("total", [1, 2, 9, 10, 99, 100, 999])
def test_a_batch_of_999_or_fewer_is_byte_for_byte_what_pr_72_published(total):
    """The correction is confined to what it had to change. At or below 999 sources -- every
    realistic batch -- the published names are reproduced exactly, three digits throughout."""
    for position in (1, total):
        published = f"{rc.SYNTHETIC_PREFIX}{position:03d}"
        assert _job(total).for_source(position).source_tag == published
        # And identical to what a caller that supplies no total at all still gets.
        assert rc.synthetic_source_tag(position) == published


@pytest.mark.parametrize("total", _WIDTH_TOTALS)
def test_the_folder_tag_and_the_filename_prefix_agree_at_every_scale(total):
    """One record feeds both builders, so a `File0007` clip can never land in a `File007` folder.

    This is the seam the single-authority design protects: the batch layout and the exporter read
    the width from the same frozen record instead of each working it out locally.
    """
    naming = _job(total)
    probe = sorted({min(max(1, p), total)
                    for p in (1, 2, total // 2, total - 1, total)})
    paths = [rf"{_ROOT}\source_{i}.mp4" for i in range(1, total + 1)]
    layout = batch.plan_item_directories(paths, _ROOT, naming=naming)
    assert len(layout) == total
    for position in probe:
        folder, = layout[position - 1]
        filename = rc.segment_filename(paths[position - 1], 1, 1,
                                       naming=naming.for_source(position))
        staging = rc.staging_filename(paths[position - 1], 1, 1, "tok12345",
                                      naming=naming.for_source(position))
        assert filename.startswith(folder + "_"), (folder, filename)
        assert staging == f".{filename[:-len('.mp4')]}.tok12345.part.mp4"
    assert len({len(parts[0]) for parts in layout}) == 1


def test_for_source_preserves_the_frozen_total():
    """The invariance the whole design rests on. `for_source` describes a SOURCE; the total
    describes the JOB, and dropping it is precisely how the folders and the filenames would end up
    with two different widths."""
    job = _job(1200)
    assert job.source_total == 1200
    aimed = job.for_source(7)
    assert aimed.source_total == 1200
    assert aimed.source_tag == "File0007"
    # Chained, and the job record is still untouched.
    assert aimed.for_source(42).source_total == 1200
    assert aimed.for_source(42).source_tag == "File0042"
    assert job.source_index == 1 and job.source_total == 1200


def test_the_frozen_total_is_immutable_like_the_rest_of_the_record():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _job(1200).source_total = 3                           # type: ignore[misc]


def test_an_early_source_is_padded_for_the_whole_batch_not_for_itself():
    """Source 1 of a 1,000-source job is `File0001`, not `File001`. This is the single assertion
    that the width is a property of the job rather than of the value being formatted."""
    assert _job(1).for_source(1).source_tag == "File001"
    assert _job(999).for_source(1).source_tag == "File001"
    assert _job(1000).for_source(1).source_tag == "File0001"
    assert _job(10000).for_source(1).source_tag == "File00001"


@pytest.mark.parametrize("junk", [None, 0, -5, "", "abc", 1.9, [1], {}])
def test_a_nonsense_total_is_absorbed_and_still_never_truncates(junk):
    """Malformed input is absorbed here exactly as everywhere else in this module -- and the
    no-truncation floor means even a nonsense total cannot damage an index."""
    naming = rc.OutputNaming(synthetic=True, job_date=DATE, source_total=junk)
    assert naming.for_source(1).source_tag == "File001"
    assert naming.for_source(1000).source_tag == "File1000"
    assert int(naming.for_source(1234).source_tag[len(rc.SYNTHETIC_PREFIX):]) == 1234


def test_an_understated_total_widens_rather_than_losing_a_digit():
    """Defensive, and deliberately NOT how the product calls it: the floor exists so a wrong total
    can only ever make one name wider, never truncate or wrap it."""
    assert rc.source_index_width(10, 1000) == 4
    assert rc.OutputNaming(synthetic=True, job_date=DATE,
                           source_total=10).for_source(1000).source_tag == "File1000"


def test_the_superseded_per_index_width_fails_the_sort_order_requirement():
    """Executable negative control. The old rule is reimplemented here and shown to FAIL the
    requirement the correction was authorized for, while the shipped rule passes it -- so this
    test would still fail if the implementation quietly reverted."""
    def superseded(position):                                 # the published PR #72 spelling
        return f"{rc.SYNTHETIC_PREFIX}{position:0{max(3, len(str(position)))}d}"

    old = [superseded(i) for i in range(1, 1002)]
    new = [_job(1001).for_source(i).source_tag for i in range(1, 1002)]

    assert old != sorted(old), "the superseded rule must genuinely mis-sort, or this proves nothing"
    assert old[99:101] == ["File100", "File101"] and old[999] == "File1000"
    assert sorted(old).index("File1000") < sorted(old).index("File101"), \
        "File1000 sorting before File101 is the defect"
    # The shipped rule: uniform width, so lexical order is the source order.
    assert new == sorted(new)
    assert new[99:101] == ["File0100", "File0101"] and new[999] == "File1000"
    assert len({len(tag) for tag in new}) == 1
    assert len({len(tag) for tag in old}) == 2, "the control really did mix widths"


def test_the_batch_layout_reads_the_width_from_the_record_rather_than_recomputing_it():
    """Structural, and it is the anti-drift guarantee: if this function worked the width out from
    `len(source_paths)` there would be two rules over the same fact, and the day they disagreed the
    folders and the clips inside them would be padded differently."""
    layout = _code_only(_func(_tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork",
                                                 "raw_cutter_batch.py")),
                              "plan_item_directories"))
    assert "naming.for_source(position).source_tag" in layout, \
        "the layout must take its tag from the frozen job record"
    assert "source_index_width" not in layout, "the width is not recomputed in this layer"
    assert "synthetic_source_tag(" not in layout, \
        "going straight to the builder would bypass the record's frozen total"


def test_a_counter_is_never_renumbered_by_a_skipped_or_failed_source():
    """The requirement is explicit. The index IS the position in the frozen list, so source 3 is
    `File003` whatever happened to 1 and 2 -- which is what keeps a partial run's manifest
    readable."""
    job = rc.OutputNaming(synthetic=True, job_date=DATE)
    assert job.for_source(3).source_tag == "File003"
    assert job.for_source(3).source_tag == job.for_source(3).source_tag
    # And the job record itself is never mutated by aiming it at a source.
    assert job.source_index == 1
    assert job.for_source(7).source_index == 7
    assert job.source_index == 1


def test_a_nonsense_source_index_is_absorbed_rather_than_crashing_a_run():
    for junk in (None, 0, -5, "", "abc", 1.9, [1]):
        assert rc.OutputNaming(synthetic=True, job_date=DATE).for_source(junk).source_tag \
            in ("File001", "File002")
    assert rc.OutputNaming(synthetic=True, job_date=DATE).for_source(0).source_tag == "File001"
    assert rc.synthetic_source_tag(None) == "File001"


def test_the_naming_record_is_frozen():
    """Job-scoped and immutable is the whole concurrency story: two browser sessions share this
    process, so a record a later frame could edit would re-name a job already running."""
    assert dataclasses.is_dataclass(rc.OutputNaming)
    assert dataclasses.fields(rc.OutputNaming)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ON.synthetic = False                                  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ON.source_index = 9                                   # type: ignore[misc]


# =============================================================================================
# 4. The date: captured once, DDMMYYYY, never re-read
# =============================================================================================

def test_the_date_field_is_the_job_date_in_ddmmyyyy():
    assert rc.OutputNaming(synthetic=True, job_date="18102026").date_field == "18102026"
    assert rc.segment_filename("x.mp4", 1, 1,
                               naming=rc.OutputNaming(synthetic=True, job_date="01012027")) == \
        "File001_01012027_001.mp4"


def test_a_batch_that_crosses_midnight_keeps_the_date_it_started_with():
    """One record for the whole job means every source and every clip carries one date.

    The structural half of the guarantee -- that nothing downstream reads a clock -- is asserted
    separately against the modules' own source, below.
    """
    job = rc.OutputNaming(synthetic=True, job_date=DATE)
    dates = {rc.segment_filename("x.mp4", clip, 4, naming=job.for_source(source)).split("_")[1]
             for source in range(1, 6) for clip in range(1, 5)}
    assert dates == {DATE}


def test_a_different_job_date_produces_different_names():
    """The negative control: the date is really in the name, not decoration."""
    first = rc.segment_filename("x.mp4", 1, 1,
                                naming=rc.OutputNaming(synthetic=True, job_date=DATE))
    second = rc.segment_filename("x.mp4", 1, 1,
                                 naming=rc.OutputNaming(synthetic=True, job_date=OTHER_DATE))
    assert first != second
    assert first == "File001_18102026_001.mp4"
    assert second == "File001_19102026_001.mp4"


@pytest.mark.parametrize("value,expected", [
    ("18102026", "18102026"),
    ("18-10-2026", "18102026"),
    ("2026-10-18", "20261018"),
    ("", rc.JOB_DATE_FALLBACK),
    (None, rc.JOB_DATE_FALLBACK),
    ("tomorrow", rc.JOB_DATE_FALLBACK),
    ("1810202", rc.JOB_DATE_FALLBACK),
    ("181020261", rc.JOB_DATE_FALLBACK),
])
def test_a_malformed_job_date_is_absorbed_conspicuously_never_silently_replaced(value, expected):
    """Absorbed like every other malformed input here, but visible: `00000000` says the caller
    supplied no date, instead of hiding behind whatever today happens to be."""
    assert rc.normalize_job_date(value) == expected
    assert rc.JOB_DATE_FALLBACK == "00000000"


def test_neither_pure_module_reads_a_clock_anywhere():
    """Structural, and it is the real guarantee behind the midnight requirement: a per-clip clock
    read would reintroduce the defect however carefully the caller passed its date down."""
    for module in ("raw_cutter.py", "raw_cutter_batch.py"):
        tree = _tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork", module))
        rendered = _code_only(tree)
        for forbidden in ("datetime", "time.time", "time.localtime", "strftime", "today",
                          "perf_counter", "import time"):
            assert forbidden not in rendered, f"{module} reaches {forbidden}"


def test_the_export_adapter_never_builds_a_name_or_a_date_of_its_own():
    """The adapter performs; it does not decide. A date formatted here would be a second source
    of truth that could disagree with the job's own."""
    export = _code_only(_func(_tree(_EXPORT), "export_segments"))
    assert "segment_filename" in export and "staging_filename" in export
    for forbidden in ("strftime", "datetime", "OutputNaming(", "synthetic", "File0"):
        assert forbidden not in export, f"export_segments builds its own {forbidden}"


# =============================================================================================
# 5. Staging names and the run directory
# =============================================================================================

def test_the_staging_name_is_source_independent_and_keeps_its_per_attempt_token():
    first = rc.staging_filename(r"J:\Footage\long_recording.mkv", 1, 3, "aaaa1111", naming=ON)
    second = rc.staging_filename(r"J:\Footage\long_recording.mkv", 1, 3, "bbbb2222", naming=ON)
    assert first == ".File001_18102026_001.aaaa1111.part.mp4"
    assert second == ".File001_18102026_001.bbbb2222.part.mp4"
    assert first != second, "two attempts must never collide on a staging name"
    # The three properties the delivery-share lesson bought, all still true.
    assert first.startswith("."), "staging artifacts stay out of the way in a listing"
    assert ".part." in first and first.endswith(".mp4"), \
        "FFmpeg infers the muxer from the extension, so the real one must stay LAST"
    assert "long_recording" not in first


def test_the_single_video_run_directory_is_source_independent():
    assert rc.run_directory_name(r"J:\Footage\long_recording.mkv", "20261018_153000",
                                 naming=ON) == "RawCutter_Single_20261018_153000"
    assert rc.SINGLE_DIRECTORY_PREFIX == "RawCutter_Single_"


def test_the_run_directory_keeps_the_run_timestamp_because_that_is_not_source_identity():
    """Dropping the timestamp would make two runs collide on the folder name, and the refusal
    would then be a usability bug rather than a safety feature."""
    first = rc.run_directory_name("a.mp4", "20261018_153000", naming=ON)
    second = rc.run_directory_name("a.mp4", "20261018_153001", naming=ON)
    assert first != second
    assert first.endswith("20261018_153000") and second.endswith("20261018_153001")


def test_an_empty_run_timestamp_still_produces_a_usable_folder_name():
    assert rc.run_directory_name("a.mp4", "", naming=ON) == "RawCutter_Single_run"
    assert rc.run_directory_name("a.mp4", "   ", naming=ON) == "RawCutter_Single_run"


def test_repeated_runs_with_the_same_inputs_produce_the_same_names():
    """Determinism is the property the manifest's reproducibility rests on."""
    for _ in range(3):
        assert rc.segment_filename("x.mp4", 2, 5, naming=ON.for_source(4)) == \
            "File004_18102026_002.mp4"
        assert rc.synthetic_source_tag(4) == "File004"


def test_the_source_tag_is_one_string_shared_by_the_folder_and_the_filename_prefix():
    """Two builders would be two things to keep in step, and a `File003` clip inside a `File002`
    folder is worse than either name alone."""
    tag = rc.synthetic_source_tag(3)
    assert batch.plan_item_directories(["a.mp4", "b.mp4", "c.mp4"], r"J:\in",
                                       naming=ON)[2] == (tag,)
    assert rc.segment_filename("c.mp4", 1, 1, naming=ON.for_source(3)).startswith(tag + "_")


# =============================================================================================
# 6. Renaming is not a planning input
# =============================================================================================

def test_the_naming_mode_is_absent_from_the_plan_signature():
    """Same reasoning as Remove Audio: it changes what a clip is CALLED, never where it begins or
    ends, so including it would refuse a plan that is still perfectly valid and force a needless
    re-analysis of a long video."""
    source = rc.SourceInfo(path=r"J:\a.mp4", duration=60.0, size_bytes=10, mtime_ns=20)
    assert rc.plan_signature(source, 50, 5, 300) == rc.plan_signature(source, 50, 5, 300)
    signature = _code_only(_func(_tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork",
                                                    "raw_cutter.py")), "plan_signature"))
    for forbidden in ("naming", "rename", "synthetic", "job_date"):
        assert forbidden not in signature, forbidden


def test_the_planner_itself_knows_nothing_about_naming():
    """`plan_segments` decides WHERE cuts fall. A naming argument reaching it would make the two
    decisions inseparable and the plan unable to be re-named without being re-planned."""
    planner = _code_only(_func(_tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork",
                                                  "raw_cutter.py")), "plan_segments"))
    for forbidden in ("naming", "OutputNaming", "synthetic", "job_date"):
        assert forbidden not in planner, forbidden


# =============================================================================================
# 7. The batch output layout
# =============================================================================================

_ROOT = r"J:\in"
_BATCH_SOURCES = [rf"{_ROOT}\cameraA\video01.mp4",
                  rf"{_ROOT}\cameraB\video01.mp4",
                  rf"{_ROOT}\top.mp4"]


def test_the_batch_layout_is_unchanged_when_the_mode_is_off():
    """Exact tuples, and `naming=OFF` must equal passing nothing. The mirrored input structure IS
    the default product behaviour, so it is pinned literally rather than described."""
    expected = (("cameraA", "video01"), ("cameraB", "video01"), ("top",))
    assert batch.plan_item_directories(_BATCH_SOURCES, _ROOT) == expected
    assert batch.plan_item_directories(_BATCH_SOURCES, _ROOT, naming=OFF) == expected
    assert batch.plan_item_directories(_BATCH_SOURCES, _ROOT, naming=None) == expected


def test_the_batch_layout_is_flat_synthetic_folders_when_the_mode_is_on():
    assert batch.plan_item_directories(_BATCH_SOURCES, _ROOT, naming=ON) == \
        (("File001",), ("File002",), ("File003",))


def test_the_synthetic_batch_layout_leaks_no_source_basename_and_no_source_folder_name():
    """Mirroring is dropped rather than sanitised, because sanitising a leak still leaks it:
    `cameraA` survives `safe_component` intact."""
    layout = batch.plan_item_directories(_BATCH_SOURCES, _ROOT, naming=ON)
    flattened = "/".join("/".join(parts) for parts in layout).casefold()
    for token in ("video01", "cameraa", "camerab", "top"):
        assert token not in flattened, token
    # Negative control: the default layout genuinely does carry all four.
    mirrored = "/".join("/".join(p) for p in
                        batch.plan_item_directories(_BATCH_SOURCES, _ROOT)).casefold()
    for token in ("video01", "cameraa", "camerab", "top"):
        assert token in mirrored, token


@pytest.mark.parametrize("paths", [
    [rf"{_ROOT}\a\video01.mp4", rf"{_ROOT}\b\video01.mp4"],                   # same basename
    [rf"{_ROOT}\deep\deeper\deepest\x.mp4", rf"{_ROOT}\y.mp4"],               # different depths
    [rf"{_ROOT}\Ünïcödé — ★.mp4", rf"{_ROOT}\sub\Ünïcödé — ★.mkv"],           # unicode
    [rf"{_ROOT}\{'verylongname' * 10}.mp4", rf"{_ROOT}\{'verylongname' * 10}.mkv"],
    [rf"{_ROOT}\con.mp4", rf"{_ROOT}\NUL.mp4", rf"{_ROOT}\lpt1\aux.mp4"],     # reserved
    [rf"{_ROOT}\clip#1.mp4", rf"{_ROOT}\clip@1.mp4"],                         # sanitise-collision
])
def test_every_hard_naming_case_still_produces_one_flat_unique_safe_component(paths):
    layout = batch.plan_item_directories(paths, _ROOT, naming=ON)
    assert len(layout) == len(paths)
    assert all(len(parts) == 1 for parts in layout), layout
    assert len({parts[0].casefold() for parts in layout}) == len(paths), layout
    for position, (parts, _path) in enumerate(zip(layout, paths), start=1):
        assert parts == (rc.synthetic_source_tag(position),)
        assert parts[0].isascii() and parts[0] == batch.safe_component(parts[0])


def test_the_synthetic_layout_is_unique_across_the_whole_frozen_source_set():
    """Uniqueness here is structural rather than negotiated -- the component is the position -- so
    the assertion is over a set large enough to cross the three-digit boundary.

    [FORK] R4-C correction: the record carries the frozen total, so all 1,500 components are four
    digits and the directory listing sorts in discovery order.
    """
    paths = [rf"{_ROOT}\same_name.mp4"] * 1500
    layout = batch.plan_item_directories(paths, _ROOT, naming=_job(len(paths)))
    assert len(layout) == 1500
    assert len({parts[0] for parts in layout}) == 1500
    assert layout[0] == ("File0001",) and layout[999] == ("File1000",)
    assert layout[1499] == ("File1500",)
    assert [parts[0] for parts in layout] == sorted(parts[0] for parts in layout)


def test_a_wide_batch_still_leaks_no_source_basename_or_folder_name():
    """The privacy claim has to survive the correction: the components are still derived from the
    position, the frozen total and nothing taken from the paths."""
    paths = ([rf"{_ROOT}\cameraA\secret_wedding.mp4"] * 500
             + [rf"{_ROOT}\nested\deeper\secret_wedding.mkv"] * 500
             + [rf"{_ROOT}\CON.mp4"] * 200)
    naming = _job(len(paths))
    layout = batch.plan_item_directories(paths, _ROOT, naming=naming)
    flattened = "/".join("/".join(parts) for parts in layout).casefold()
    for token in ("secret_wedding", "cameraa", "nested", "deeper", "con.mp4"):
        assert token not in flattened, token
    assert all(len(parts) == 1 and parts[0].isascii() for parts in layout)
    # And the filenames built from the same record are equally clean.
    for position in (1, 500, 501, 1200):
        name = rc.segment_filename(paths[position - 1], 1, 1,
                                   naming=naming.for_source(position))
        assert "secret" not in name.casefold() and "con" not in name.casefold()
        assert name.startswith(f"File{position:04d}_")


def test_a_wide_batch_that_crosses_midnight_still_carries_one_date_and_one_width():
    """Both frozen facts together: the correction added a second job-scoped field, and neither it
    nor the date may vary across the sources of one job."""
    naming = _job(1200)
    names = [rc.segment_filename("x.mp4", clip, 3, naming=naming.for_source(source))
             for source in (1, 2, 999, 1000, 1200) for clip in (1, 3)]
    assert {name.split("_")[1] for name in names} == {DATE}
    assert {len(name.split("_")[0]) for name in names} == {len("File0001")}


def test_a_single_video_job_is_one_source_wide_with_as_many_clips_as_it_needs():
    """Single Video freezes a one-source list, so the source field is the three-digit minimum
    however many clips the plan produces -- the clip field is what widens there."""
    naming = _job(1).for_source(1)
    assert rc.run_directory_name("x.mp4", "20261018_153000", naming=naming) == \
        "RawCutter_Single_20261018_153000"
    assert rc.segment_filename("x.mp4", 1, 1, naming=naming) == f"File001_{DATE}_001.mp4"
    assert rc.segment_filename("x.mp4", 7, 12, naming=naming) == f"File001_{DATE}_007.mp4"
    assert rc.segment_filename("x.mp4", 7, 1200, naming=naming) == f"File001_{DATE}_0007.mp4"
    assert rc.staging_filename("x.mp4", 2, 4, "tok1", naming=naming) == \
        f".File001_{DATE}_002.tok1.part.mp4"


def test_both_gui_handlers_freeze_a_source_total_on_their_one_naming_record():
    """The GUI seam, and it is load-bearing: the record is the sole authority for the width, so a
    handler that built one without a total would reintroduce the mixed-width defect no matter how
    correct the pure module is. `gui.py` cannot be imported here, so this is asserted against its
    real source."""
    gui = _tree(_GUI)
    batch_body = _code_only(_func(gui, "_on_cut_batch_run"))
    assert batch_body.count("fork_cutter.OutputNaming(") == 1, "still one record for the batch"
    assert "source_total=total" in batch_body, \
        "the batch must freeze the width from its own frozen source count"
    # `total` is the length of the frozen list the manifest and the progress counter also use.
    assert "total = len(sources)" in batch_body

    single_body = _code_only(_func(gui, "_on_cut_run"))
    assert single_body.count("fork_cutter.OutputNaming(") == 1
    assert "source_total=1" in single_body, "Single Video freezes a one-source job"


def test_no_layer_outside_the_record_decides_the_source_width():
    """`source_index_width` is called in exactly one place -- the tag builder -- so there is one
    rule over one frozen number, which is what stops the folders and the filenames from drifting."""
    for relative in ("src/gui.py", "src/raw_cutter_export.py",
                     "src/beatsync_fork/raw_cutter_batch.py"):
        rendered = _code_only(_tree(os.path.join(_REPO_ROOT, *relative.split("/"))))
        assert "source_index_width" not in rendered, relative


def test_the_synthetic_layout_needs_no_hash_tag_or_counter_tie_break():
    """The default layout's collision machinery is correct and must stay, but it has nothing to do
    here -- and a stray `__<hash>` suffix would be derived from the source path, which leaks."""
    layout = batch.plan_item_directories([rf"{_ROOT}\clip#1.mp4", rf"{_ROOT}\clip@1.mp4"],
                                         _ROOT, naming=ON)
    assert layout == (("File001",), ("File002",))
    assert all("__" not in parts[0] and "outside_" not in parts[0] for parts in layout)


def test_a_source_outside_the_scan_root_still_gets_a_contained_synthetic_component():
    """The default path falls back to `outside_<hash>`; the synthetic one never needs to, because
    it never consults the path at all. Either way nothing may contain `..`."""
    layout = batch.plan_item_directories([r"K:\elsewhere\x.mp4", rf"{_ROOT}\y.mp4"],
                                         _ROOT, naming=ON)
    assert layout == (("File001",), ("File002",))
    assert all(".." not in part and "/" not in part and "\\" not in part
               for parts in layout for part in parts)


def test_an_empty_source_list_plans_an_empty_layout_in_both_modes():
    assert batch.plan_item_directories([], _ROOT, naming=ON) == ()
    assert batch.plan_item_directories([], _ROOT) == ()


# =============================================================================================
# 8. The manifest: provenance retained, schema additive, existing readers unaffected
# =============================================================================================

def _settings(**extra):
    return batch.BatchSettings(sensitivity=50, threshold=0.27, min_seconds=5.0, max_seconds=300.0,
                               remove_audio=True, encoder="libx264", recursive=True, **extra)


def _items(settings):
    layout = batch.plan_item_directories(_BATCH_SOURCES, _ROOT,
                                         naming=ON if settings.rename_output_clips else OFF)
    return [
        batch.BatchItem(index=1, source_path=_BATCH_SOURCES[0], relative_parts=layout[0],
                        outcome=batch.OUTCOME_COMPLETED, clips_planned=2, clips_written=2,
                        output_directory=os.path.join(r"J:\out\RawCutter_Batch_x", *layout[0])),
        batch.BatchItem(index=2, source_path=_BATCH_SOURCES[1], relative_parts=layout[1],
                        outcome=batch.OUTCOME_NO_CLIPS),
        batch.BatchItem(index=3, source_path=_BATCH_SOURCES[2], relative_parts=layout[2],
                        outcome=batch.OUTCOME_UNREADABLE, error="no video stream"),
    ]


def _payload(rename):
    settings = _settings(rename_output_clips=rename)
    return batch.manifest_payload(_items(settings), 3, settings, r"J:\out\RawCutter_Batch_x",
                                  _ROOT, "2026-10-18T15:30:00", "2026-10-18T15:40:00",
                                  600.0, False)


def test_the_manifest_schema_name_is_unchanged():
    """A reader keys off this. Changing it is a breaking migration and is explicitly out of scope
    for an additive feature."""
    assert batch.MANIFEST_SCHEMA == "beatsync-raw-cutter-batch-1"
    for rename in (False, True):
        assert _payload(rename)["schema"] == "beatsync-raw-cutter-batch-1"


def test_every_pre_existing_manifest_key_still_exists_with_the_same_meaning():
    """The compatibility claim, measured rather than asserted in prose: the OFF payload must be
    key-for-key and value-for-value what it was, and the ON payload must differ in exactly one
    place."""
    off, on = _payload(False), _payload(True)
    assert set(off) == set(on)
    assert set(off["settings"]) == set(on["settings"])
    assert set(off["totals"]) == set(on["totals"])
    assert off["totals"] == on["totals"]
    for key in off:
        if key in ("settings", "sources"):
            continue
        assert off[key] == on[key], key
    differing = {k for k in off["settings"] if off["settings"][k] != on["settings"][k]}
    assert differing == {"rename_output_clips"}, differing


def test_the_renaming_flag_is_recorded_so_a_reader_need_not_infer_the_mode():
    assert _payload(False)["settings"]["rename_output_clips"] is False
    assert _payload(True)["settings"]["rename_output_clips"] is True
    # And it defaults off, so a pre-R4-C construction of `BatchSettings` records `false`.
    assert batch.BatchSettings().rename_output_clips is False


def test_the_manifest_maps_each_original_source_to_its_synthetic_output_directory():
    """The provenance requirement. The exported NAMES carry nothing; the record carries
    everything, which is the separation the owner asked for."""
    rows = _payload(True)["sources"]
    assert [row["source"] for row in rows] == _BATCH_SOURCES
    assert [row["relative_output"] for row in rows] == ["File001", "File002", "File003"]
    assert rows[0]["output_directory"].endswith(os.path.join("RawCutter_Batch_x", "File001"))
    assert rows[0]["clips_written"] == 2 and rows[0]["clips_planned"] == 2
    # One record per DISCOVERED source, synthetic naming or not.
    assert len(rows) == 3
    assert [row["index"] for row in rows] == [1, 2, 3]


def test_the_synthetic_mapping_is_a_bijection_over_the_whole_discovered_set():
    """Two sources sharing an output identity would make the record unusable for audit, which is
    the only reason the record exists in this mode."""
    rows = _payload(True)["sources"]
    assert len({row["source"] for row in rows}) == len(rows)
    assert len({row["relative_output"] for row in rows}) == len(rows)


def test_the_outcome_vocabulary_and_classification_are_untouched_by_renaming():
    """Renaming must not be able to change what the batch says HAPPENED to a source."""
    for rename in (False, True):
        settings = _settings(rename_output_clips=rename)
        items = _items(settings)
        assert [i.outcome for i in items] == [batch.OUTCOME_COMPLETED, batch.OUTCOME_NO_CLIPS,
                                              batch.OUTCOME_UNREADABLE]
        totals = batch.summarise(items, 3)
        assert (totals.completed, totals.no_clips, totals.failed) == (1, 1, 1)
        assert batch.terminal_status(items, 3, False) == batch.TERMINAL_COMPLETED_WITH_FAILURES


def test_the_settings_line_is_byte_for_byte_unchanged_when_renaming_is_off():
    """It is read by the report, the live panel and several assertions, so the OFF spelling is
    pinned exactly and the ON clause is additive."""
    assert _settings().describe() == (
        "sensitivity 50 (threshold 0.27), 5s-300s clips, audio removed, encoder libx264")
    assert _settings(rename_output_clips=True).describe() == (
        "sensitivity 50 (threshold 0.27), 5s-300s clips, audio removed, encoder libx264, "
        "output clips renamed")


def test_the_batch_summary_still_names_no_source_basename_it_was_not_given():
    """The report legitimately names a source that needs attention -- that is its job, and it is
    not an output name. What it must not do is invent a different mapping from the manifest's."""
    settings = _settings(rename_output_clips=True)
    text = batch.summary_text(_items(settings), 3, settings, r"J:\out\RawCutter_Batch_x",
                              12.0, False, input_root=_ROOT)
    assert "File001" not in text or "RawCutter_Batch_x" in text
    assert "video01.mp4" in text, "a source needing attention must still be identifiable"
    assert "output clips renamed" in text


# =============================================================================================
# 9. The export adapter, executed against a real filesystem (FFmpeg replaced by a writer)
# =============================================================================================

@pytest.fixture
def exporter():
    """`export_segments` + its two helpers, AST-extracted and executed against a synthesised
    parent. The real filesystem work runs; only the FFmpeg child is replaced.

    The fake `_run_media_command` writes the staging file the real binary would, so the rename,
    the no-replace publication, the containment check and `_discard_staging` are all the shipped
    code acting on real files. `fork_cutter` is the REAL module, so the names under test are the
    ones the product builds.
    """
    tree = _tree(_EXPORT)
    wanted = ("SegmentResult", "export_segments", "_discard_staging", "_validate_output",
              "build_segment_command", "CutterCancelled", "CutterJob",
              "EXPORT_TIMEOUT_SECONDS", "PROBE_TIMEOUT_SECONDS")
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in wanted)
             or (isinstance(n, ast.Assign) and len(n.targets) == 1
                 and getattr(n.targets[0], "id", None) in wanted)]
    found = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    assert not set(wanted) - found, f"the adapter no longer defines {set(wanted) - found}"

    # `encodes` and `probes` are kept APART on purpose. `_validate_output` runs an FFprobe through
    # the same seam, so a single counter conflates "the second clip" with "the first clip's
    # validation" -- which is exactly the mistake that made an early draft of the partial-failure
    # case assert the wrong clip had failed. `fail` therefore selects by destination substring,
    # not by call ordinal.
    state = {"encodes": [], "probes": [], "calls": [], "fail": set(), "bytes": 1024,
             "before_write": None}

    def run_media(cmd, timeout, lifecycle=None):
        destination = cmd[-1]
        state["calls"].append(list(cmd))
        if cmd[0] == "ffprobe":
            state["probes"].append(list(cmd))
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return subprocess.CompletedProcess(cmd, 0, "video", "")
            return subprocess.CompletedProcess(cmd, 1, "", "no such file")
        state["encodes"].append(list(cmd))
        if state["before_write"] is not None:
            state["before_write"](destination)
        if any(token in os.path.basename(destination) for token in state["fail"]):
            return subprocess.CompletedProcess(cmd, 1, "", "injected FFmpeg failure")
        with open(destination, "wb") as handle:
            handle.write(b"\0" * state["bytes"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    namespace = {
        "os": os, "subprocess": subprocess, "uuid": __import__("uuid"),
        "threading": __import__("threading"), "time": __import__("time"),
        "dataclass": dataclasses.dataclass, "Callable": object, "Sequence": object,
        "fork_cutter": rc, "FFMPEG_PATH": "ffmpeg", "FFPROBE_PATH": "ffprobe", "MAX_THREADS": 4,
        "_run_media_command": run_media,
        "_short_ffmpeg_error": lambda stderr, max_chars=400: (stderr or "")[-max_chars:],
        "__annotations__": {},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _EXPORT, "exec"),   # noqa: S102
         namespace)
    namespace["_state"] = state
    return namespace


def _plan(count, length=4.0):
    segments = tuple(rc.Segment(index=i + 1, start=i * length, end=(i + 1) * length)
                     for i in range(count))
    return rc.SegmentPlan(segments=segments, source_duration=count * length)


def _source(tmp_path, name="long_recording.mkv"):
    path = tmp_path / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"source bytes" * 64)
    return rc.SourceInfo(path=str(path), duration=60.0, width=160, height=120, fps=25.0,
                         has_audio=False, size_bytes=path.stat().st_size,
                         mtime_ns=path.stat().st_mtime_ns)


def _sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _export(exporter, source, plan, run_dir, naming=None, job=None, progress=None):
    os.makedirs(run_dir, exist_ok=True)
    seen = []
    results = exporter["export_segments"](
        source=source, plan=plan, run_directory=run_dir, remove_audio=True, encoder="libx264",
        job=job, on_progress=(progress if progress is not None else
                              (lambda i, t, n: seen.append(n))),
        on_complete=None, naming=naming)
    return results, seen


def test_the_adapter_writes_the_legacy_names_when_no_naming_is_supplied(exporter, tmp_path):
    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), str(tmp_path / "out"))
    assert all(r.ok for r in results), [r.error for r in results]
    assert sorted(os.listdir(tmp_path / "out")) == ["long_recording_part_001.mp4",
                                                    "long_recording_part_002.mp4",
                                                    "long_recording_part_003.mp4"]


def test_the_adapter_writes_the_synthetic_names_when_the_mode_is_on(exporter, tmp_path):
    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), str(tmp_path / "out"),
                         naming=ON.for_source(7))
    assert all(r.ok for r in results), [r.error for r in results]
    assert sorted(os.listdir(tmp_path / "out")) == ["File007_18102026_001.mp4",
                                                    "File007_18102026_002.mp4",
                                                    "File007_18102026_003.mp4"]


def test_nothing_in_the_run_directory_carries_the_source_basename(exporter, tmp_path):
    """Measured on the DIRECTORY, after the fact, which is the only check that would also catch a
    stray staging artifact or a sidecar nobody thought about."""
    source = _source(tmp_path, "my_private_wedding.mkv")
    _export(exporter, source, _plan(2), str(tmp_path / "out"), naming=ON)
    listing = os.listdir(tmp_path / "out")
    assert listing, "nothing was written at all"
    for name in listing:
        assert "wedding" not in name.casefold(), name
        assert "private" not in name.casefold(), name


def test_the_name_given_to_the_progress_callback_is_the_name_that_lands_on_disk(
        exporter, tmp_path):
    """The requirement is that the operator is not shown a filename the clip is not written
    under. One builder feeds both, so this measures that it really is one builder."""
    source = _source(tmp_path)
    results, announced = _export(exporter, source, _plan(3), str(tmp_path / "out"), naming=ON)
    published = [os.path.basename(r.path) for r in results if r.ok]
    assert announced == published
    assert announced == sorted(os.listdir(tmp_path / "out"))


def test_the_staging_file_on_disk_is_source_independent_while_the_encode_runs(exporter, tmp_path):
    """Observed mid-encode, not inferred: the fake FFmpeg inspects the directory at the moment
    the real binary would be writing. A staging name that leaked would leak for the whole encode.
    """
    source = _source(tmp_path, "my_private_wedding.mkv")
    observed: list[list[str]] = []
    exporter["_state"]["before_write"] = lambda dest: observed.append(
        sorted(os.listdir(os.path.dirname(dest))) + [os.path.basename(dest)])

    _export(exporter, source, _plan(2), str(tmp_path / "out"), naming=ON)
    assert observed, "the fake encoder never ran"
    for snapshot in observed:
        for name in snapshot:
            assert "wedding" not in name.casefold(), snapshot
    # And the staging name really was the synthetic one, carrying its attempt token.
    staging = [n for snapshot in observed for n in snapshot if ".part." in n]
    assert staging, observed
    assert all(n.startswith(".File001_18102026_") and n.endswith(".part.mp4") for n in staging), \
        staging


def test_no_staging_artifact_survives_a_successful_synthetic_export(exporter, tmp_path):
    source = _source(tmp_path)
    _export(exporter, source, _plan(3), str(tmp_path / "out"), naming=ON)
    leftovers = [n for n in os.listdir(tmp_path / "out")
                 if n.startswith(".") or ".part." in n]
    assert leftovers == [], leftovers


def test_a_synthetic_export_never_replaces_an_existing_destination(exporter, tmp_path):
    """No-replace publication is unchanged by the mode, and the refusal is reported per clip
    rather than ending the run."""
    out = tmp_path / "out"
    out.mkdir()
    occupied = out / "File001_18102026_002.mp4"
    occupied.write_bytes(b"somebody else's clip")
    before = _sha(occupied)

    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), str(out), naming=ON)
    by_index = {r.index: r for r in results}
    assert by_index[1].ok and by_index[3].ok
    assert not by_index[2].ok
    assert "already exists" in by_index[2].error
    assert _sha(occupied) == before, "an existing file was overwritten"
    assert occupied.read_bytes() == b"somebody else's clip"


def test_a_failed_synthetic_clip_removes_only_its_own_staging_artifact(exporter, tmp_path):
    """The destination is never removed on any path -- if it exists it belongs to somebody else,
    and this run has no claim on it."""
    out = tmp_path / "out"
    out.mkdir()
    stranger = out / "File001_18102026_003.mp4"
    stranger.write_bytes(b"not ours")
    exporter["_state"]["fail"] = {"_001.", "_002."}

    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), str(out), naming=ON)
    assert [r.ok for r in results] == [False, False, False]
    assert stranger.exists() and stranger.read_bytes() == b"not ours"
    assert [n for n in os.listdir(out) if ".part." in n] == [], os.listdir(out)
    assert sorted(os.listdir(out)) == ["File001_18102026_003.mp4"]


def test_a_partial_synthetic_export_publishes_the_good_clips_and_reports_the_rest(
        exporter, tmp_path):
    exporter["_state"]["fail"] = {"_002."}
    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), str(tmp_path / "out"), naming=ON)
    assert [r.ok for r in results] == [True, False, True]
    assert sorted(os.listdir(tmp_path / "out")) == ["File001_18102026_001.mp4",
                                                    "File001_18102026_003.mp4"]
    assert "injected FFmpeg failure" in results[1].error


def test_cancellation_keeps_every_synthetic_clip_already_published(exporter, tmp_path):
    source = _source(tmp_path)
    job = exporter["CutterJob"]()
    kept = []

    def stop_after_two(index, total, name):
        kept.append(name)
        if index == 2:
            job.cancel()

    with pytest.raises(exporter["CutterCancelled"]):
        _export(exporter, source, _plan(5), str(tmp_path / "out"), naming=ON, job=job,
                progress=stop_after_two)

    on_disk = sorted(os.listdir(tmp_path / "out"))
    assert on_disk == ["File001_18102026_001.mp4", "File001_18102026_002.mp4"], on_disk
    assert [n for n in on_disk if ".part." in n] == []


def test_a_synthetic_export_modifies_no_source_file(exporter, tmp_path):
    source = _source(tmp_path)
    before = (_sha(source.path), os.path.getsize(source.path))
    _export(exporter, source, _plan(3), str(tmp_path / "out"), naming=ON)
    assert (_sha(source.path), os.path.getsize(source.path)) == before
    # And the source is never an output argument of any command the adapter built.
    for command in exporter["_state"]["calls"]:
        assert command[-1] != source.path
        assert os.path.abspath(command[-1]) != os.path.abspath(source.path)


def test_every_synthetic_destination_resolves_inside_the_run_directory(exporter, tmp_path):
    out = os.path.abspath(str(tmp_path / "out"))
    source = _source(tmp_path)
    results, _ = _export(exporter, source, _plan(3), out, naming=ON)
    for result in results:
        assert os.path.commonpath([out, os.path.abspath(result.path)]) == out
    for command in exporter["_state"]["calls"]:
        assert os.path.commonpath([out, os.path.abspath(command[-1])]) == out


def test_the_mode_changes_the_output_path_and_nothing_else_about_the_command(exporter, tmp_path):
    """The media-integrity claim, at the argv level: one FFmpeg child per clip either way, and the
    two command lines differ only in the file being written.

    The real-binary half of this -- that the frames and their timestamps come out identical -- is
    in `tests/test_raw_cutter_ffmpeg.py`, which has a binary to measure.
    """
    source = _source(tmp_path)

    _export(exporter, source, _plan(2), str(tmp_path / "legacy"))
    legacy = [list(c) for c in exporter["_state"]["encodes"]]
    legacy_probes = len(exporter["_state"]["probes"])
    exporter["_state"]["encodes"] = []
    exporter["_state"]["probes"] = []
    _export(exporter, source, _plan(2), str(tmp_path / "synthetic"), naming=ON)
    synthetic = [list(c) for c in exporter["_state"]["encodes"]]

    assert len(legacy) == 2 and len(synthetic) == 2, "one FFmpeg child per clip, either way"
    assert legacy_probes == len(exporter["_state"]["probes"]) == 2, \
        "one validation probe per clip, either way -- no extra pass"
    for first, second in zip(legacy, synthetic):
        assert first[:-1] == second[:-1], "the mode changed an encoding argument"
        assert first[-1] != second[-1]
        assert os.path.basename(first[-1]) != os.path.basename(second[-1])


# =============================================================================================
# 10. The GUI seam
# =============================================================================================

@pytest.fixture(scope="module")
def gui_tree() -> ast.Module:
    return _tree(_GUI)


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


def test_the_checkbox_exists_and_starts_off(gui_tree):
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree, "cut_rename_output").keywords}
    assert kwargs["value"] == "False", "the requirement is default OFF"
    assert kwargs["label"] == "LABEL_CUT_RENAME_OUTPUT"
    assert kwargs["info"] == "INFO_CUT_RENAME_OUTPUT"
    assert kwargs["elem_id"] == "'cut-rename-output'"
    assert ast.unparse(_widget(gui_tree, "cut_rename_output").func) == "gr.Checkbox"


def test_the_checkbox_label_and_help_live_in_ui_content(gui_source):
    with open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8") as handle:
        content = handle.read()
    assert "LABEL_CUT_RENAME_OUTPUT = " in content
    assert "INFO_CUT_RENAME_OUTPUT = " in content
    assert "Rename output clips" in content, "the operator-facing wording the owner specified"


def test_the_checkbox_is_declared_exactly_once_and_sits_in_the_raw_cutter_tab(gui_source,
                                                                              gui_tree):
    assert gui_source.count("cut_rename_output = gr.") == 1
    cutter = gui_source.index("gr.Tab(LABEL_TAB_RAW_CUTTER")
    advanced = gui_source.index("gr.Tab(LABEL_TAB_ADVANCED")
    assert "cut_rename_output = gr." in gui_source[cutter:advanced]
    # Beside the other shared execution settings, not inside either mode's own group.
    assigns = [n for n in ast.walk(gui_tree)
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and getattr(n.targets[0], "id", None) == "cut_rename_output"]
    assert len(assigns) == 1
    remove_audio = gui_source.index("cut_remove_audio = gr.")
    rename = gui_source.index("cut_rename_output = gr.")
    single = gui_source.index("with gr.Column(visible=True) as cut_single_actions")
    assert remove_audio < rename < single, "it must be in the shared settings group"


def test_the_checkbox_feeds_both_modes_as_one_shared_setting(gui_tree):
    for widget in ("cut_run_btn", "cut_batch_start_btn"):
        inputs = _names(_kwargs(_click(gui_tree, widget))["inputs"])
        assert "cut_rename_output" in inputs, widget
        assert inputs.count("cut_rename_output") == 1, widget
        # [FORK] R6: cut_output_profile was appended AFTER this one, so renaming is now second
        # from the end. The guarantee is unchanged: both shared settings were APPENDED, so the
        # reviewed order of the ORIGINAL inputs is untouched.
        assert inputs[-2:] == ["cut_rename_output", "cut_output_profile"], widget


def test_the_mode_switch_still_cannot_touch_the_new_setting(gui_tree):
    """Switching Single Video / Folder Batch must not move an execution setting, or the two
    workflows could silently change each other's naming."""
    body = _code_only(_func(gui_tree, "_on_cut_mode_change"))
    assert "cut_rename_output" not in body


def test_renaming_is_not_a_staleness_listener(gui_tree, gui_source):
    """It changes what a clip is CALLED, so it cannot make a plan stale. Listing it here would
    disable Cut Video and demand a fresh Analyze of a long video for nothing."""
    for node in ast.walk(gui_tree):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
                and "cut_sensitivity" in ast.unparse(node.iter)):
            assert "cut_rename_output" not in ast.unparse(node.iter)
            break
    else:
        raise AssertionError("the planning-input change loop was not found")


def test_the_single_cut_resolves_one_naming_record_from_one_clock_reading(gui_tree):
    """Frozen at submission is the concurrency guarantee: a second browser session toggling the
    checkbox must not be able to re-name a job already running."""
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    assert body.count("datetime.datetime.now()") == 1, \
        "the run timestamp and the job date must describe the same instant"
    assert body.count("fork_cutter.OutputNaming(") == 1
    assert "naming=naming" in body
    assert "prepare_run_directory" in body


def test_the_batch_resolves_one_naming_record_for_the_whole_run(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert body.count("fork_cutter.OutputNaming(") == 1
    assert body.count("datetime.datetime.now()") >= 1
    assert "naming=naming" in body, "the layout must be planned from the same record"
    assert "naming.for_source(position)" in body, \
        "each source's naming must derive from the job record, not from a mutable counter"
    # No second naming record, and no per-source date.
    assert "strftime('%d%m%Y')" in body
    assert body.count("strftime('%d%m%Y')") == 1


def test_the_batch_naming_record_is_built_once_outside_the_per_source_loop(gui_tree):
    handler = _func(gui_tree, "_on_cut_batch_run")
    for loop in [n for n in ast.walk(handler) if isinstance(n, (ast.For, ast.While))]:
        rendered = _code_only(loop)
        assert "OutputNaming(" not in rendered, "one naming record for the whole batch"
        assert "strftime" not in rendered, "no clock read inside the loop"


def test_the_presentation_wrapper_passes_the_setting_straight_through(gui_tree):
    ui = _func(gui_tree, "_on_cut_batch_run_ui").args
    core = _func(gui_tree, "_on_cut_batch_run").args
    ui_required = [a.arg for a in ui.args[:len(ui.args) - len(ui.defaults)]]
    core_required = [a.arg for a in core.args[:len(core.args) - len(core.defaults)]]
    assert ui_required == core_required
    assert "rename_output" in ui_required
    assert "rename_output" in _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))


def test_this_feature_added_no_fifth_top_level_tab(gui_source, gui_tree):
    """R4-C adds no tab of its own. The fifth tab belongs to R5 and nothing else.

    Amended for R5-P1, which shipped the reserved fifth tab this assertion was waiting for. The
    original `count("gr.Tab(") == 4` has been replaced rather than renumbered: a bare count would
    pass for *any* fifth tab, including one R4-C had added by mistake, which is the thing this
    test exists to catch. So the tab set is now asserted by **identity** — the four that existed
    when R4-C shipped, plus the one R5 reserved, and no sixth.
    """
    # Amended for R7, which added the Raw Audio Cutter tab. The claim this test actually makes is
    # unchanged and is asserted below by identity: no tab of this feature's own exists, and none of
    # its widgets is declared in somebody else's tab.
    expected = ("LABEL_TAB_QUICK_RENDER", "LABEL_TAB_CREATIVE_STUDIO", "LABEL_TAB_RAW_CUTTER",
                "LABEL_TAB_ADVANCED", "LABEL_TAB_AUDIO_CUTTER", "LABEL_TAB_CLIPS_PREVIEW")
    assert gui_source.count("gr.Tab(") == len(expected), (
        "the top-level tab set changed: it must be exactly the four tabs R4-C shipped against "
        "plus the R5 Clips Preview Selector")
    for label in expected:
        assert f"gr.Tab({label}" in gui_source, f"{label} is not a top-level tab"
    # The fifth one is R5's, and R4-C still contributes none: no Raw Cutter widget is declared
    # inside that tab.
    #
    # Taken from the `with gr.Tab(...)` node's own body, not from a source slice. A slice from the
    # tab to `source_outputs = [` was tried and was wrong: the Raw Cutter's event wiring sits
    # between the two, so the slice legitimately mentions every cutter widget and the assertion
    # fired on correct code.
    clips_tab = next(
        (node for node in ast.walk(gui_tree)
         if isinstance(node, ast.With)
         and any("gr.Tab(LABEL_TAB_CLIPS_PREVIEW" in ast.unparse(item.context_expr)
                 for item in node.items)),
        None)
    assert clips_tab is not None, "the Clips Preview tab is not a `with gr.Tab(...)` block"
    body = "\n".join(ast.unparse(statement) for statement in clips_tab.body)
    for widget in ("cut_rename_output", "cut_source", "cut_batch_folder", "cut_run_btn"):
        assert f"{widget} = gr." not in body, (
            f"{widget} is declared inside the Clips Preview tab; R4-C's widgets belong to "
            "Raw Cutter")


def test_the_feature_touches_no_render_or_gate_widget(gui_tree):
    for widget in ("cut_run_btn", "cut_batch_start_btn"):
        outputs = _names(_kwargs(_click(gui_tree, widget))["outputs"])
        for forbidden in ("status_output", "video_output", "process_btn", "source_state",
                          "source_report", "confirm_btn", "prep_status", "prep_report"):
            assert forbidden not in outputs, (widget, forbidden)
