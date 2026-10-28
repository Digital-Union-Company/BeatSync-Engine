"""The filename-collision policy (R8 UX Refresh, Phase 7).

`beatsync_fork.collision` is stdlib-only and takes the caller's rename as an argument, so every
branch here -- the retry loop, exhaustion, a non-collision error -- is exercised without a disk.
The two cases that genuinely need a filesystem (both real publication paths, through real
`os.rename`) are at the bottom and use `tmp_path`.

The organising claim of this suite: **the default policy must be provably identical to the
pre-R8 behaviour.** A new setting that quietly changes what the app does for an operator who
never opened Settings would be a defect, not a feature, so `SKIP` is asserted to attempt exactly
one name and to leave every source file where it was.
"""

from __future__ import annotations

import os

import pytest

from beatsync_fork import audio_review
from beatsync_fork import clips_review
from beatsync_fork import collision


# ---------------------------------------------------------------------------------------------
# resolve_policy is total
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None, "", "   ", "nonsense", "WIPE", 7, 0, [], {}, object(), True, b"suffix",
])
def test_an_unrecognised_policy_resolves_to_the_safe_default(value):
    """It is read from a Gradio radio, a hand-editable JSON file and an absent key, so it has to
    answer something for anything. It answers SKIP, because a corrupt setting must never be read
    as permission to invent filenames."""
    assert collision.resolve_policy(value) == collision.SKIP


@pytest.mark.parametrize("value,expected", [
    ("skip", collision.SKIP),
    ("suffix", collision.SUFFIX),
    ("SUFFIX", collision.SUFFIX),
    ("  Suffix  ", collision.SUFFIX),
])
def test_a_recognised_policy_resolves_case_and_whitespace_insensitively(value, expected):
    assert collision.resolve_policy(value) == expected


def test_the_default_is_the_pre_r8_behaviour():
    assert collision.DEFAULT_POLICY == collision.SKIP


# ---------------------------------------------------------------------------------------------
# the candidate sequence
# ---------------------------------------------------------------------------------------------

def test_skip_offers_exactly_one_name():
    """The load-bearing assertion for backward compatibility: under the default there is nothing
    to retry, so the behaviour cannot differ from a single no-replace rename."""
    assert list(collision.candidate_names("clip.wav", collision.SKIP)) == ["clip.wav"]


def test_suffix_starts_with_the_requested_name_then_numbers_it():
    """The first candidate is the unmodified name under BOTH policies, so a clip that does not
    collide always keeps the name it was given. Suffixes only ever appear on a real collision."""
    assert list(collision.candidate_names("clip.wav", collision.SUFFIX, 3)) == [
        "clip.wav", "clip_01.wav", "clip_02.wav", "clip_03.wav"]


def test_the_documented_example_is_what_it_produces():
    """Exactly the sequence the R8 task specified."""
    assert list(collision.candidate_names("clip.wav", collision.SUFFIX, 3))[:4] == [
        "clip.wav", "clip_01.wav", "clip_02.wav", "clip_03.wav"]


@pytest.mark.parametrize("name,stem,extension", [
    ("clip.wav", "clip", ".wav"),
    ("clip.WAV", "clip", ".WAV"),              # case is preserved exactly
    ("a.tar.gz", "a.tar", ".gz"),              # only the LAST extension is the extension
    ("README", "README", ""),
    (".gitignore", ".gitignore", ""),          # a dotfile is a name, not an extension
    ("", "", ""),
])
def test_the_extension_is_preserved_exactly(name, stem, extension):
    assert collision.split_name(name) == (stem, extension)


def test_a_numbered_name_keeps_its_extension():
    assert list(collision.candidate_names("take.FLAC", collision.SUFFIX, 1))[-1] == "take_01.FLAC"
    assert list(collision.candidate_names("README", collision.SUFFIX, 1))[-1] == "README_01"


# ---------------------------------------------------------------------------------------------
# publish: the rename is the authority, and it retries
# ---------------------------------------------------------------------------------------------

def _rename_rejecting(taken_names):
    """A fake no-replace rename. Records what was attempted; raises for an occupied name."""
    attempted = []

    def rename(source, destination):
        attempted.append(os.path.basename(destination))
        if os.path.basename(destination) in taken_names:
            raise FileExistsError(destination)

    return rename, attempted


def test_a_free_name_is_taken_on_the_first_try():
    rename, attempted = _rename_rejecting(set())
    destination, tries = collision.publish("s.wav", "lib", "clip.wav", rename,
                                           policy=collision.SUFFIX)
    assert os.path.basename(destination) == "clip.wav"
    assert tries == 1
    assert attempted == ["clip.wav"], "it tried more names than it needed"


def test_an_occupied_name_advances_and_retries():
    rename, attempted = _rename_rejecting({"clip.wav", "clip_01.wav"})
    destination, tries = collision.publish("s.wav", "lib", "clip.wav", rename,
                                           policy=collision.SUFFIX)
    assert os.path.basename(destination) == "clip_02.wav"
    assert tries == 3
    assert attempted == ["clip.wav", "clip_01.wav", "clip_02.wav"]


def test_retrying_is_driven_by_the_rename_failing_not_by_an_exists_check():
    """The safety property, asserted structurally: there is no `os.path.exists` anywhere in the
    module. A pre-check would leave a window in which another process could create the file
    between the check and the move; here the check IS the move.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(collision))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("os.path.exists", "os.path.isfile", "os.stat", "os.listdir", "os.scandir"):
        assert forbidden not in calls, f"{forbidden} reintroduces a check-then-act window"


def test_skip_refuses_rather_than_inventing_a_name():
    rename, attempted = _rename_rejecting({"clip.wav"})
    with pytest.raises(collision.NoFreeName) as failure:
        collision.publish("s.wav", "lib", "clip.wav", rename, policy=collision.SKIP)
    assert attempted == ["clip.wav"], "SKIP attempted a name the operator never approved"
    assert failure.value.attempts == 1


def test_one_attempt_is_how_a_caller_recognises_the_plain_taken_name_case():
    """Callers branch on `attempts`, never on the policy they passed, so that the default policy's
    operator-visible wording stays byte-identical to the pre-R8 sentence. This is the signal they
    branch on."""
    rename, _ = _rename_rejecting({"clip.wav"})
    with pytest.raises(collision.NoFreeName) as skip_failure:
        collision.publish("s.wav", "lib", "clip.wav", rename, policy=collision.SKIP)
    assert skip_failure.value.attempts == 1

    rename, _ = _rename_rejecting({"clip.wav", "clip_01.wav", "clip_02.wav"})
    with pytest.raises(collision.NoFreeName) as suffix_failure:
        collision.publish("s.wav", "lib", "clip.wav", rename, policy=collision.SUFFIX, limit=2)
    assert suffix_failure.value.attempts > 1


def test_exhaustion_moves_nothing_and_says_how_many_were_tried():
    rename, attempted = _rename_rejecting({"clip.wav", "clip_01.wav", "clip_02.wav"})
    with pytest.raises(collision.NoFreeName) as failure:
        collision.publish("s.wav", "lib", "clip.wav", rename, policy=collision.SUFFIX, limit=2)
    assert attempted == ["clip.wav", "clip_01.wav", "clip_02.wav"]
    assert failure.value.attempts == 3
    assert "Nothing was moved" in str(failure.value)


def test_an_error_that_is_not_a_collision_propagates_untouched():
    """A cross-volume refusal and a sharing violation are the caller's to describe -- it already
    has exact wording for both -- so they must not be swallowed into a retry or reworded here."""
    def cross_volume(source, destination):
        raise OSError(18, "cross-device link")

    with pytest.raises(OSError) as failure:
        collision.publish("s", "d", "x.wav", cross_volume, policy=collision.SUFFIX)
    assert failure.value.errno == 18
    assert not isinstance(failure.value, collision.NoFreeName)


def test_the_taken_predicate_is_injectable():
    """So a caller whose rename signals a taken name some other way can still use the loop."""
    calls = []

    def rename(source, destination):
        calls.append(destination)
        if len(calls) < 3:
            raise PermissionError("pretend this means taken")

    destination, tries = collision.publish(
        "s.wav", "lib", "clip.wav", rename, policy=collision.SUFFIX,
        taken=lambda exc: isinstance(exc, PermissionError))
    assert tries == 3 and os.path.basename(destination) == "clip_02.wav"


def test_nothing_in_the_module_deletes_copies_or_overwrites():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(collision))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("os.remove", "os.unlink", "os.rmdir", "os.replace", "shutil.rmtree",
                      "shutil.move", "shutil.copy", "shutil.copy2", "open"):
        assert forbidden not in calls, f"{forbidden} is reachable from the publisher"


# ---------------------------------------------------------------------------------------------
# describe: silent on the ordinary case
# ---------------------------------------------------------------------------------------------

def test_a_file_that_kept_its_own_name_is_not_annotated():
    """A report that announced "kept its own name" on every line would bury the lines that
    matter."""
    assert collision.describe(os.path.join("lib", "clip.wav"), 1) == ""


def test_a_renamed_file_is_named_in_the_report():
    note = collision.describe(os.path.join("lib", "clip_02.wav"), 3)
    assert "clip_02.wav" in note


# ---------------------------------------------------------------------------------------------
# Both real publication paths, through real os.rename
# ---------------------------------------------------------------------------------------------

def _video_entries(paths):
    entries = []
    for index, path in enumerate(paths, start=1):
        stat = os.stat(path)
        entries.append(clips_review.ClipEntry(
            clip_id=f"c{index:04d}", index=index, path=path, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, dev=getattr(stat, "st_dev", -1),
            ino=getattr(stat, "st_ino", -1), suffix=os.path.splitext(path)[1]))
    return entries


def _three_colliding(tmp_path, name):
    paths = []
    for index in (1, 2, 3):
        folder = tmp_path / f"take{index}"
        folder.mkdir()
        clip = folder / name
        clip.write_bytes(f"CONTENT-{index}".encode())
        paths.append(str(clip))
    library = tmp_path / "library"
    library.mkdir()
    return paths, str(library)


@pytest.mark.parametrize("sorter", ["video", "audio"])
def test_the_default_policy_publishes_one_and_leaves_the_rest_untouched(tmp_path, sorter):
    """Three clips that all want one name. Exactly one lands; the other two stay exactly where
    they were, with their bytes intact, and the existing file is not overwritten."""
    name = "clip.mp4" if sorter == "video" else "clip.wav"
    paths, library = _three_colliding(tmp_path, name)
    entries = _video_entries(paths)

    if sorter == "video":
        outcomes = clips_review.sort_decided_clips(
            entries, lambda e: clips_review.SELECTED,
            {clips_review.SELECTED: library}, collision.SKIP)
    else:
        outcomes = audio_review.sort_clips(
            entries, lambda e: audio_review.SELECTED, lambda e: "",
            {audio_review.SELECTED: library}, policy=collision.SKIP)

    assert sum(1 for o in outcomes if o.ok) == 1
    assert sorted(os.listdir(library)) == [name]
    survivors = [p for p in paths if os.path.isfile(p)]
    assert len(survivors) == 2, "a refused source was moved or lost"
    assert any("already there" in (o.detail or "") for o in outcomes if not o.ok)
    # the one that landed keeps the original bytes of whichever clip won
    with open(os.path.join(library, name), "rb") as handle:
        assert handle.read().startswith(b"CONTENT-")


@pytest.mark.parametrize("sorter", ["video", "audio"])
def test_the_suffix_policy_publishes_all_three_without_overwriting(tmp_path, sorter):
    name = "clip.mp4" if sorter == "video" else "clip.wav"
    stem, extension = collision.split_name(name)
    paths, library = _three_colliding(tmp_path, name)
    entries = _video_entries(paths)

    if sorter == "video":
        outcomes = clips_review.sort_decided_clips(
            entries, lambda e: clips_review.SELECTED,
            {clips_review.SELECTED: library}, collision.SUFFIX)
    else:
        outcomes = audio_review.sort_clips(
            entries, lambda e: audio_review.SELECTED, lambda e: "",
            {audio_review.SELECTED: library}, policy=collision.SUFFIX)

    assert all(o.ok for o in outcomes), [o.detail for o in outcomes]
    assert sorted(os.listdir(library)) == [
        name, f"{stem}_01{extension}", f"{stem}_02{extension}"]
    assert not [p for p in paths if os.path.isfile(p)], "a source was left behind"

    # three distinct files, so nothing was overwritten on the way
    contents = set()
    for written in os.listdir(library):
        with open(os.path.join(library, written), "rb") as handle:
            contents.add(handle.read())
    assert len(contents) == 3, "two clips ended up with the same bytes, so one was overwritten"

    # the report names the files that were renamed, and ONLY those
    renamed = [o for o in outcomes if "renamed to" in (o.detail or "")]
    assert len(renamed) == 2
    kept = [o for o in outcomes
            if os.path.basename(o.destination) == name]
    assert len(kept) == 1 and not kept[0].detail, "an unrenamed clip was annotated"
