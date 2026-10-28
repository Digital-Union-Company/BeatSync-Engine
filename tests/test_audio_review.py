"""R7.2 Audio review: the shared core, the sort plan, and the destination rules.

`beatsync_fork.audio_review` is stdlib-only, so all of this is ordinary imported-code testing with
no Gradio, no FFmpeg and no browser. The GUI seams that consume it are in
`tests/test_audio_cutter.py` (the cutter's review half) and `tests/test_audio_preview_tab.py` (the
standalone tab).

The assertions worth reading are the ones about **what the operator is promised before anything
moves**: that no page budget can make a valid clip unplayable, that an unmarked clip is never
discarded without a confirmation attached to that exact batch, and that two destination folders
which resolve to the same place are refused rather than merged.
"""

from __future__ import annotations

import json
import os

import pytest

from beatsync_fork import audio_review as review
from beatsync_fork import clips_review as core


def _tile(clip_id, name="a_part_001.wav", number=1, duration=2.5, span=""):
    return review.AudioTile(clip_id=clip_id, number=number, name=name, duration=duration,
                            span=span)


# =============================================================================================
# Phase 1: the audio is NOT in the page
# =============================================================================================

def test_a_tile_carries_a_url_and_no_bytes():
    """The whole of Phase 1. R7.1 inlined base64 under a cumulative budget, so a valid clip became
    unplayable because EARLIER clips had spent the page's allowance."""
    markup = review.gallery_html([_tile("CLIP1")], {}, "SESS", "PAGE")
    assert "base64" not in markup
    assert "data:audio" not in markup
    assert "<audio" not in markup, "the player is created on hover, not shipped in the markup"
    assert 'data-audio="/r7audio/SESS/PAGE/CLIP1"' in markup


def test_page_weight_does_not_grow_with_clip_size():
    """The property the phase exists to obtain: an hour-long WAV costs the same markup as a
    one-second one, because neither is in the page."""
    small = review.gallery_html([_tile("C", duration=0.5)], {}, "S", "P")
    large = review.gallery_html([_tile("C", duration=3600.0)], {}, "S", "P")
    # Only the rendered length and the dataset number differ; nothing scales with bytes.
    assert abs(len(small) - len(large)) < 40


def test_no_tile_can_ever_say_it_was_not_loaded_for_this_page():
    """There is no loading limit any more, so the message that described one must be gone rather
    than reworded. A clip is either playable or it reports a real failure."""
    tiles = [_tile(f"C{n}", number=n) for n in range(1, 200)]
    markup = review.gallery_html(tiles, {}, "S", "P")
    assert "not loaded" not in markup
    assert "still exported" not in markup
    assert markup.count("data-audio=") == 199, "every tile gets a URL, however many there are"


def test_a_tile_carries_its_number_duration_and_optional_source_window():
    markup = review.gallery_html(
        [_tile("C", number=7, duration=2.5, span="0:01.0→0:03.5")], {}, "S", "P")
    assert ">7<" in markup
    assert 'data-dur="2.500"' in markup
    assert "0:02.5" in markup
    assert "0:01.0" in markup and "0:03.5" in markup


def test_a_tile_with_no_known_duration_says_so_rather_than_guessing():
    """`None` must render an honest dash. A fabricated length is worse than an unknown one: the
    operator uses it to decide whether a clip is the one they want."""
    markup = review.gallery_html([_tile("C", duration=None)], {}, "S", "P")
    assert "—" in markup
    assert "0:00.0" not in markup


def test_every_tile_is_focusable_so_the_keyboard_works():
    assert 'tabindex="0"' in review.gallery_html([_tile("C")], {}, "S", "P")


def test_every_tile_starts_pending():
    markup = review.gallery_html([_tile("A"), _tile("B")], {}, "S", "P")
    assert markup.count("r7-pending") == 2
    assert "r7-selected" not in markup and "r7-rejected" not in markup


def test_existing_decisions_and_edited_names_survive_a_repaint():
    markup = review.gallery_html(
        [_tile("A", name="01 greeting"), _tile("B")],
        {"A": review.SELECTED, "B": review.REJECTED}, "S", "P")
    assert "r7-selected" in markup and "r7-rejected" in markup
    assert 'value="01 greeting"' in markup


def test_an_unknown_decision_is_read_as_pending():
    assert "r7-pending" in review.gallery_html([_tile("A")], {"A": "NONSENSE"}, "S", "P")


def test_a_filename_cannot_carry_markup_into_the_gallery():
    """The audio gallery deliberately shows the filename -- the operator is naming files -- so the
    escaping matters more here than it does for the video grid, which shows no name at all."""
    markup = review.gallery_html(
        [_tile("A", name='<img src=x onerror="alert(1)">')], {}, "S", "P")
    assert "<img" not in markup
    assert "&lt;img" in markup
    assert "onerror" not in markup or "&quot;" in markup


def test_a_session_or_page_token_cannot_carry_markup_either():
    markup = review.gallery_html([_tile("A")], {}, '"><script>x</script>', "P")
    assert "<script>" not in markup


def test_an_empty_page_says_so_and_still_carries_a_nonce():
    """"This folder is now empty" is a repaint that MUST be delivered -- it is exactly what a
    fully-sorted batch produces -- and a `gr.HTML` update with byte-identical markup is not
    delivered by the pinned Gradio."""
    first = review.gallery_html([], {}, "S", "P", empty_message="Nothing here.")
    second = review.gallery_html([], {}, "S", "P", empty_message="Nothing here.")
    assert "Nothing here." in first
    assert first != second, "two empty repaints must differ, or the second is dropped"


def test_a_grid_always_carries_a_fresh_nonce():
    tiles = [_tile("A")]
    assert (review.gallery_html(tiles, {}, "S", "P")
            != review.gallery_html(tiles, {}, "S", "P"))


# =============================================================================================
# Two galleries, one document
# =============================================================================================

def test_each_grid_names_its_own_hidden_field_and_counts_box():
    """[R7.2] Two galleries exist. R7.1's client found its machinery by document-wide id, so two
    grids under that scheme would both write the Audio Cutter's field -- the Audio Preview tab's
    decisions would land on the cutter's clips, silently, and only when both tabs had been used."""
    cutter = review.gallery_html([_tile("A")], {}, "S", "P",
                                 grid_id=review.GRID_CUTTER,
                                 store_id="audio-decisions", counts_id="audio-counts")
    preview = review.gallery_html([_tile("A")], {}, "S", "P",
                                  grid_id=review.GRID_PREVIEW,
                                  store_id="audio-preview-decisions",
                                  counts_id="audio-preview-counts")
    assert 'id="r7-grid"' in cutter and 'data-store="audio-decisions"' in cutter
    assert 'id="r7-grid-preview"' in preview
    assert 'data-store="audio-preview-decisions"' in preview
    assert review.GRID_CUTTER != review.GRID_PREVIEW


def test_the_two_surfaces_serve_through_different_prefixes():
    """Each prefix is bound to its own registry at middleware construction, because
    `ClipRegistry.new_session` revokes an owner's previous sessions: one shared registry would let
    an Audio Preview scan silently revoke the cutter's live gallery."""
    assert review.MEDIA_PREFIX_CUTTER != review.MEDIA_PREFIX_PREVIEW
    assert review.MEDIA_PREFIX == review.MEDIA_PREFIX_CUTTER
    assert set(review.MEDIA_PREFIXES) == {review.MEDIA_PREFIX_CUTTER,
                                          review.MEDIA_PREFIX_PREVIEW}
    cutter = review.gallery_html([_tile("A")], {}, "S", "P",
                                 media_prefix=review.MEDIA_PREFIX_CUTTER)
    preview = review.gallery_html([_tile("A")], {}, "S", "P",
                                  media_prefix=review.MEDIA_PREFIX_PREVIEW)
    assert review.MEDIA_PREFIX_PREVIEW not in cutter
    assert review.MEDIA_PREFIX_CUTTER + "/" not in preview


# =============================================================================================
# Counts
# =============================================================================================

def test_counts_are_taken_from_the_server_tiles_not_the_payload():
    """A decision for a clip that is not on this page must not inflate the totals, and a clip with
    no decision must count as pending rather than vanish."""
    counts = review.decision_counts([_tile("A"), _tile("B")],
                                    {"A": review.SELECTED, "ZZZ": review.REJECTED})
    assert counts == {review.SELECTED: 1, review.REJECTED: 0, review.PENDING: 1, "TOTAL": 2}


def test_the_counts_line_reads_like_the_clips_preview_one():
    line = review.counts_line(review.decision_counts([_tile("A")], {}))
    assert all(part in line for part in ("Keep 0", "Discard 0", "Undecided 1", "Total 1"))


def test_the_decision_vocabulary_is_the_clips_preview_one():
    """One vocabulary across both review features. Two copies is how they come to disagree."""
    assert (review.PENDING, review.SELECTED, review.REJECTED) == (
        core.PENDING, core.SELECTED, core.REJECTED)
    assert review.DECISIONS == core.DECISIONS


# =============================================================================================
# Paging  [Phase 2]
# =============================================================================================

@pytest.mark.parametrize("junk", [None, "", "abc", float("nan"), [], {}])
def test_page_size_is_total_over_anything_a_number_widget_can_hand_over(junk):
    assert review.resolve_page_size(junk) == review.DEFAULT_PAGE_SIZE


def test_page_size_is_clamped_to_the_registry_bound():
    assert review.resolve_page_size(0) == 1
    assert review.resolve_page_size(-5) == 1
    assert review.resolve_page_size(10 ** 9) == review.MAX_PAGE_SIZE
    assert review.MAX_PAGE_SIZE == core.MAX_CLIPS_PER_PAGE


def test_an_empty_folder_is_page_one_of_one_not_one_of_zero():
    assert review.page_count(0, 60) == 1
    assert review.resolve_page(1, 0, 60) == 1


def test_page_counts_round_up():
    assert review.page_count(60, 60) == 1
    assert review.page_count(61, 60) == 2
    assert review.page_count(120, 60) == 2
    assert review.page_count(121, 60) == 3


def test_an_out_of_range_page_is_clamped_rather_than_refused():
    """The page the operator is on goes out of range whenever a batch is sorted and the folder
    shrinks. Showing the last real page is right; an error is not."""
    assert review.resolve_page(99, 10, 5) == 2
    assert review.resolve_page(0, 10, 5) == 1
    assert review.resolve_page(-3, 10, 5) == 1
    assert review.resolve_page("nonsense", 10, 5) == 1


def test_page_slices_cover_every_file_exactly_once():
    total, size = 137, 60
    seen = []
    for page in range(1, review.page_count(total, size) + 1):
        start, count = review.page_slice(page, total, size)
        seen.extend(range(start, start + count))
    assert seen == list(range(total)), "no file may be skipped or shown twice"


def test_freeze_paths_captures_identity_and_skips_what_it_cannot_stat(tmp_path):
    """A file that cannot be stat'ed now is skipped rather than published with a guessed
    identity -- publishing one would mean the first fetch answered 409 for a file that was merely
    busy at scan time."""
    good = tmp_path / "a.wav"
    good.write_bytes(b"x" * 10)
    frozen = review.freeze_paths([str(good), str(tmp_path / "gone.wav")], 0, 10)
    assert len(frozen) == 1
    index, path, size, mtime_ns, dev, ino = frozen[0]
    assert (index, path, size) == (1, str(good), 10)
    assert mtime_ns > 0
    assert (dev, ino) == (os.stat(good).st_dev, os.stat(good).st_ino)


def test_freeze_paths_numbers_from_the_slice_start(tmp_path):
    """So a tile on page 3 shows its position in the whole folder, not its position on the page."""
    paths = []
    for n in range(5):
        clip = tmp_path / f"{n}.wav"
        clip.write_bytes(b"x")
        paths.append(str(clip))
    frozen = review.freeze_paths(paths, 2, 2)
    assert [row[0] for row in frozen] == [3, 4]


# =============================================================================================
# Discovery
# =============================================================================================

def test_discovery_reports_its_failures_as_prose_never_as_an_exception():
    """Every caller is a Gradio event handler."""
    for bad in (None, "", "   ", 7):
        assert review.discover_audio(bad, True).error
    assert "not found" in review.discover_audio(r"Z:\no\such\folder", True).error.lower()


def test_discovery_refuses_a_file_where_a_folder_was_expected(tmp_path):
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"x")
    assert "Not a folder" in review.discover_audio(str(clip), True).error


def test_discovery_finds_the_three_formats_the_owner_asked_for(tmp_path):
    for name in ("a.wav", "b.flac", "c.mp3", "d.txt", "e.mp4", "manifest.json"):
        (tmp_path / name).write_bytes(b"x" * 16)
    found = review.discover_audio(str(tmp_path), False)
    assert not found.error
    names = sorted(os.path.basename(media.path) for media in found.files)
    assert names == ["a.wav", "b.flac", "c.mp3"]


def test_discovery_excludes_a_manifest_and_a_video_for_free(tmp_path):
    """The audio manifest the cutter leaves in its own output folder must not become a reviewable
    clip, and neither must a video sitting beside the audio."""
    (tmp_path / "audio_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clip.mp4").write_bytes(b"x" * 16)
    (tmp_path / "real.wav").write_bytes(b"x" * 16)
    found = review.discover_audio(str(tmp_path), True)
    assert [os.path.basename(m.path) for m in found.files] == ["real.wav"]


def test_discovery_can_be_told_not_to_descend(tmp_path):
    (tmp_path / "top.wav").write_bytes(b"x" * 16)
    nested = tmp_path / "deeper"
    nested.mkdir()
    (nested / "low.wav").write_bytes(b"x" * 16)
    shallow = review.discover_audio(str(tmp_path), False)
    deep = review.discover_audio(str(tmp_path), True)
    assert len(shallow.files) == 1
    assert len(deep.files) == 2


def test_every_servable_suffix_has_a_content_type_a_browser_will_accept():
    """Enumerating a container the browser cannot play would reintroduce the confusion Phase 1
    exists to remove, so the two tables must agree."""
    for suffix in review.SERVABLE_SUFFIXES:
        media_type = review.content_type_for(suffix)
        assert media_type.startswith("audio/"), (suffix, media_type)
    assert review.content_type_for(".mp3") == "audio/mpeg"
    assert review.content_type_for(".wav") == "audio/wav"
    assert review.content_type_for(".flac") == "audio/flac"
    # Anything unknown must not be guessed at.
    assert review.content_type_for(".exe") == "application/octet-stream"
    assert review.content_type_for(None) == "application/octet-stream"


def test_the_three_formats_the_owner_named_are_all_servable():
    for suffix in (".wav", ".flac", ".mp3"):
        assert suffix in review.SERVABLE_SUFFIXES


def test_a_servable_suffix_set_can_never_contain_a_manifest_or_a_video():
    for forbidden in (".json", ".mp4", ".mkv", ".txt", ".py"):
        assert forbidden not in review.SERVABLE_SUFFIXES


# =============================================================================================
# Phase 3: the sort plan
# =============================================================================================

def test_the_plan_keeps_explicit_and_unmarked_discards_apart():
    """Collapsing them would hide the one number an operator needs to sanity-check after agreeing
    to a sweep: "14 discarded" cannot tell them whether they rejected 14 clips or forgot 12."""
    tiles = [_tile("K1"), _tile("K2"), _tile("R1"), _tile("U1"), _tile("U2"), _tile("U3")]
    plan = review.plan_sort(tiles, {"K1": review.SELECTED, "K2": review.SELECTED,
                                    "R1": review.REJECTED})
    assert plan.keep == ("K1", "K2")
    assert plan.rejected == ("R1",)
    assert plan.unmarked == ("U1", "U2", "U3")
    assert plan.total == 6
    assert set(plan.discarded) == {"R1", "U1", "U2", "U3"}


def test_confirm_is_admitted_by_one_kept_clip_not_by_a_fully_decided_page():
    """The owner's actual request: mark the good clips and press Confirm. R7.1 made them click
    Reject on every clip they did not want."""
    tiles = [_tile("A"), _tile("B")]
    assert review.plan_sort(tiles, {}).can_sort is False
    assert review.plan_sort(tiles, {"A": review.REJECTED}).can_sort is False
    assert review.plan_sort(tiles, {"A": review.SELECTED}).can_sort is True


def test_a_plan_only_needs_the_destinations_it_will_actually_write():
    """An operator who keeps everything must never be asked to name a quarantine folder."""
    tiles = [_tile("A")]
    keep_only = review.plan_sort(tiles, {"A": review.SELECTED})
    assert keep_only.needs_library is True
    assert keep_only.needs_discarded is False
    assert keep_only.has_unmarked is False

    drop_only = review.plan_sort([_tile("A"), _tile("B")],
                                 {"A": review.SELECTED, "B": review.REJECTED})
    assert drop_only.needs_discarded is True


def test_the_signature_changes_with_every_decision_and_every_destination():
    """It is what makes a one-time confirmation apply to ONE batch. Change anything the operator
    was shown and the confirmation is void."""
    tiles = [_tile("A"), _tile("B")]
    base = review.plan_sort(tiles, {"A": review.SELECTED}, "L", "D").signature

    assert review.plan_sort(tiles, {"A": review.SELECTED}, "L", "D").signature == base
    assert review.plan_sort(tiles, {"A": review.REJECTED}, "L", "D").signature != base
    assert review.plan_sort(tiles, {"A": review.SELECTED, "B": review.SELECTED},
                            "L", "D").signature != base
    assert review.plan_sort(tiles, {"A": review.SELECTED}, "OTHER", "D").signature != base
    assert review.plan_sort(tiles, {"A": review.SELECTED}, "L", "OTHER").signature != base
    assert review.plan_sort([_tile("A")], {"A": review.SELECTED}, "L", "D").signature != base


def test_the_signature_ignores_only_the_order_decisions_arrive_in():
    """Two identical batches must agree however the page enumerated them, or the gate would
    re-arm on a repaint that changed nothing."""
    a = review.plan_sort([_tile("A"), _tile("B")],
                         {"A": review.SELECTED, "B": review.REJECTED}, "L", "D")
    b = review.plan_sort([_tile("B"), _tile("A")],
                         {"B": review.REJECTED, "A": review.SELECTED}, "L", "D")
    assert a.signature == b.signature


def test_the_signature_is_case_insensitive_about_a_windows_path():
    """`J:\\Lib` and `j:\\lib` are the same folder on Windows, so they must not re-arm the gate."""
    tiles = [_tile("A")]
    lower = review.plan_sort(tiles, {"A": review.SELECTED}, r"j:\lib", r"j:\bin").signature
    upper = review.plan_sort(tiles, {"A": review.SELECTED}, r"J:\LIB", r"J:\BIN").signature
    assert lower == upper


def test_the_preview_states_all_four_counts_and_promises_no_deletion():
    """Exactly the shape the owner asked to see before the moves happen."""
    plan = review.plan_sort(
        [_tile(f"K{n}") for n in range(6)] + [_tile(f"R{n}") for n in range(2)]
        + [_tile(f"U{n}") for n in range(12)],
        {**{f"K{n}": review.SELECTED for n in range(6)},
         **{f"R{n}": review.REJECTED for n in range(2)}})
    text = "\n".join(review.plan_preview_lines(plan, r"J:\Lib", r"J:\Bin"))
    assert "6 clip(s) selected to KEEP." in text
    assert "2 clip(s) explicitly rejected." in text
    assert "12 unmarked clip(s) will ALSO be moved to Discarded." in text
    assert "Nothing will be permanently deleted." in text
    assert r"J:\Lib" in text and r"J:\Bin" in text


def test_the_preview_says_nothing_about_unmarked_clips_when_there_are_none():
    plan = review.plan_sort([_tile("A")], {"A": review.SELECTED})
    text = "\n".join(review.plan_preview_lines(plan, "L", ""))
    assert "unmarked" not in text
    assert "Nothing will be permanently deleted." in text


# =============================================================================================
# Phase 3: filing
# =============================================================================================

def _entry_for(path):
    info = os.stat(path)
    return core.ClipEntry(clip_id="C" + os.path.basename(path), index=1, path=str(path),
                          size=info.st_size, mtime_ns=info.st_mtime_ns,
                          dev=info.st_dev, ino=info.st_ino, suffix=".wav")


def test_a_kept_clip_moves_and_an_unmarked_one_is_left_alone_without_confirmation(tmp_path):
    """The sweep is an explicit parameter precisely so it cannot happen by omission."""
    source = tmp_path / "src"
    source.mkdir()
    library = tmp_path / "lib"
    discarded = tmp_path / "bin"
    keep = source / "keep.wav"
    unmarked = source / "unmarked.wav"
    keep.write_bytes(b"k" * 16)
    unmarked.write_bytes(b"u" * 16)
    entries = [_entry_for(keep), _entry_for(unmarked)]
    decisions = {entries[0].clip_id: review.SELECTED}

    outcomes = review.sort_clips(
        entries, lambda e: decisions.get(e.clip_id, review.PENDING), lambda e: "",
        {review.SELECTED: str(library), review.REJECTED: str(discarded)},
        unmarked_as=review.PENDING)

    assert [o.status for o in outcomes] == [review.MOVED, review.REFUSED]
    assert (library / "keep.wav").is_file()
    assert unmarked.is_file(), "an unmarked clip must stay exactly where it is"
    assert "still undecided" in outcomes[1].detail


def test_with_confirmation_an_unmarked_clip_is_discarded_and_reported_as_automatic(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    library = tmp_path / "lib"
    discarded = tmp_path / "bin"
    keep = source / "keep.wav"
    unmarked = source / "unmarked.wav"
    rejected = source / "rejected.wav"
    for clip in (keep, unmarked, rejected):
        clip.write_bytes(b"x" * 16)
    entries = [_entry_for(keep), _entry_for(rejected), _entry_for(unmarked)]
    decisions = {entries[0].clip_id: review.SELECTED, entries[1].clip_id: review.REJECTED}

    outcomes = review.sort_clips(
        entries, lambda e: decisions.get(e.clip_id, review.PENDING), lambda e: "",
        {review.SELECTED: str(library), review.REJECTED: str(discarded)},
        unmarked_as=review.REJECTED)

    assert all(o.ok for o in outcomes)
    assert (library / "keep.wav").is_file()
    assert (discarded / "rejected.wav").is_file()
    assert (discarded / "unmarked.wav").is_file()
    automatic = [o for o in outcomes if o.automatic]
    assert [o.label for o in automatic] == ["unmarked.wav"]


def test_the_report_keeps_the_four_counts_distinct(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    library = tmp_path / "lib"
    discarded = tmp_path / "bin"
    for name in ("k.wav", "r.wav", "u.wav"):
        (source / name).write_bytes(b"x" * 16)
    entries = [_entry_for(source / n) for n in ("k.wav", "r.wav", "u.wav")]
    decisions = {entries[0].clip_id: review.SELECTED, entries[1].clip_id: review.REJECTED}
    outcomes = review.sort_clips(
        entries, lambda e: decisions.get(e.clip_id, review.PENDING), lambda e: "",
        {review.SELECTED: str(library), review.REJECTED: str(discarded)},
        unmarked_as=review.REJECTED)

    text = "\n".join(review.sorted_report_lines(outcomes, str(library), str(discarded)))
    assert "Kept: 1" in text
    assert "Discarded (you rejected these): 1" in text
    assert "Discarded (unmarked, swept by your confirmation): 1" in text
    assert "Failed: 0" in text


def test_nothing_is_ever_overwritten_and_the_clip_stays_put(tmp_path):
    """`os.rename` refuses an existing destination on Windows, so "never overwrite" is enforced by
    the syscall rather than by an `exists()` check a second press could race."""
    source = tmp_path / "src"
    source.mkdir()
    library = tmp_path / "lib"
    library.mkdir()
    clip = source / "a.wav"
    clip.write_bytes(b"new" * 8)
    (library / "a.wav").write_bytes(b"ORIGINAL")

    entry = _entry_for(clip)
    outcomes = review.sort_clips([entry], lambda e: review.SELECTED, lambda e: "",
                                 {review.SELECTED: str(library)})
    assert not outcomes[0].ok
    assert (library / "a.wav").read_bytes() == b"ORIGINAL", "the existing file was overwritten"
    assert clip.is_file(), "the source must stay put when the destination is taken"
    assert "already there" in outcomes[0].detail


def test_discarding_is_a_move_and_nothing_in_this_module_deletes(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    clip = source / "a.wav"
    clip.write_bytes(b"x" * 16)
    discarded = tmp_path / "bin"
    outcomes = review.sort_clips([_entry_for(clip)], lambda e: review.REJECTED, lambda e: "",
                                 {review.REJECTED: str(discarded)})
    assert outcomes[0].ok
    assert (discarded / "a.wav").read_bytes() == b"x" * 16, "the bytes must survive a discard"

    import inspect
    body = inspect.getsource(review)
    for destructive in ("os.remove", "os.unlink", "os.rmdir", "shutil.rmtree", "os.replace"):
        assert destructive not in body, f"audio_review reaches {destructive}"


def test_an_edited_name_is_used_and_keeps_the_real_extension(tmp_path):
    """Renaming a `.wav` to `voice.mp3` must still produce a `.wav`: a wrong extension would hand
    Audio Layers a file it cannot open."""
    source = tmp_path / "src"
    source.mkdir()
    clip = source / "a_part_001.wav"
    clip.write_bytes(b"x" * 16)
    library = tmp_path / "lib"
    outcomes = review.sort_clips([_entry_for(clip)], lambda e: review.SELECTED,
                                 lambda e: "01 greeting.mp3",
                                 {review.SELECTED: str(library)})
    assert outcomes[0].ok
    assert (library / "01 greeting.wav").is_file()


def test_a_clip_that_changed_after_the_scan_is_skipped_not_sorted(tmp_path):
    """A decision describes the audio the operator heard. If the bytes changed, it may not describe
    this file any more -- and this one moves the file, so the check matters more than for a
    read-only preview."""
    source = tmp_path / "src"
    source.mkdir()
    clip = source / "a.wav"
    clip.write_bytes(b"original")
    entry = _entry_for(clip)
    clip.write_bytes(b"re-recorded, different length")

    library = tmp_path / "lib"
    outcomes = review.sort_clips([entry], lambda e: review.SELECTED, lambda e: "",
                                 {review.SELECTED: str(library)})
    assert not outcomes[0].ok
    assert "changed after it was scanned" in outcomes[0].detail
    assert clip.is_file()
    assert not library.exists() or not list(library.iterdir())


def test_a_missing_clip_is_reported_rather_than_raising(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    clip = source / "a.wav"
    clip.write_bytes(b"x" * 16)
    entry = _entry_for(clip)
    clip.unlink()
    outcomes = review.sort_clips([entry], lambda e: review.SELECTED, lambda e: "",
                                 {review.SELECTED: str(tmp_path / 'lib')})
    assert not outcomes[0].ok
    assert "no longer" in outcomes[0].detail


def test_one_failure_does_not_stop_the_others_or_undo_what_moved(tmp_path):
    """Each rename is independently complete the moment it returns, so a partial batch is a real
    partial result and is reported as one."""
    source = tmp_path / "src"
    source.mkdir()
    library = tmp_path / "lib"
    library.mkdir()
    (library / "blocked.wav").write_bytes(b"ORIGINAL")
    good = source / "good.wav"
    blocked = source / "blocked.wav"
    later = source / "later.wav"
    for clip in (good, blocked, later):
        clip.write_bytes(b"x" * 16)

    entries = [_entry_for(good), _entry_for(blocked), _entry_for(later)]
    outcomes = review.sort_clips(entries, lambda e: review.SELECTED, lambda e: "",
                                 {review.SELECTED: str(library)})
    assert [o.ok for o in outcomes] == [True, False, True]
    assert blocked.is_file(), "the failure stays available for another attempt"
    text = "\n".join(review.sorted_report_lines(outcomes, str(library), ""))
    assert "Failed: 1" in text
    assert "still on screen" in text
    assert "press Confirm again" in text


def test_a_decision_with_no_destination_is_refused_rather_than_guessed(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    clip = source / "a.wav"
    clip.write_bytes(b"x" * 16)
    outcomes = review.sort_clips([_entry_for(clip)], lambda e: review.SELECTED, lambda e: "",
                                 {review.SELECTED: ""})
    assert not outcomes[0].ok
    assert "no destination folder" in outcomes[0].detail
    assert clip.is_file()


# =============================================================================================
# Phase 4: destinations
# =============================================================================================

def _dirs(tmp_path, *names):
    made = []
    for name in names:
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        made.append(str(target))
    return made


def test_identical_destinations_are_refused(tmp_path):
    """The owner's reported defect, exactly: both fields set to the same folder, and sorting
    proceeded anyway. Kept and discarded clips would end up mixed together."""
    (root,) = _dirs(tmp_path, "review")
    (same,) = _dirs(tmp_path, "Discart")
    verdict = review.validate_destinations(root, same, same)
    assert not verdict.ok
    assert verdict.code == "destinations_identical"
    assert "SAME folder" in review.refusal_text(verdict)


def test_identical_destinations_are_refused_through_a_different_spelling(tmp_path):
    """Resolved real paths, `commonpath` and the Windows `(st_dev, st_ino)` pair -- never a string
    comparison. A trailing separator, a `.` segment and a difference of case are all the same
    folder."""
    (root,) = _dirs(tmp_path, "review")
    (library,) = _dirs(tmp_path, "Discart")
    for alias in (library + os.sep, os.path.join(library, "."),
                  os.path.join(library, "..", "Discart"), library.upper(), library.lower()):
        verdict = review.validate_destinations(root, library, alias)
        assert not verdict.ok, alias
        assert verdict.code == "destinations_identical", (alias, verdict.code)


def test_nested_destinations_are_refused_both_ways_round(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    outer, inner = _dirs(tmp_path, "lib", "lib/bin")
    assert review.validate_destinations(root, outer, inner).code == "destinations_nested"
    assert review.validate_destinations(root, inner, outer).code == "destinations_nested"


def test_a_sibling_whose_name_merely_starts_the_same_is_fine(tmp_path):
    """`commonpath`, not a string prefix: `lib-evil` is not inside `lib`."""
    (root,) = _dirs(tmp_path, "review")
    a, b = _dirs(tmp_path, "lib", "lib-evil")
    assert review.validate_destinations(root, a, b).ok


def test_a_destination_inside_the_review_folder_is_refused(tmp_path):
    """A clip filed there would be found again by the next scan."""
    root, inside = _dirs(tmp_path, "review", "review/kept")
    (other,) = _dirs(tmp_path, "bin")
    assert review.validate_destinations(root, inside, other).code == "destination_inside_input"


def test_the_review_folder_inside_a_destination_is_refused(tmp_path):
    outer, root = _dirs(tmp_path, "lib", "lib/review")
    (other,) = _dirs(tmp_path, "bin")
    assert review.validate_destinations(root, outer, other).code == "input_inside_destination"


def test_a_destination_equal_to_the_review_folder_is_refused(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    (other,) = _dirs(tmp_path, "bin")
    assert review.validate_destinations(root, root, other).code == "library_is_input"
    assert review.validate_destinations(root, other, root).code == "rejected_is_input"


def test_a_protected_runtime_folder_is_refused_as_a_destination(tmp_path):
    """`cleanup_on_startup` clears most of `input/`, so a library there would be destroyed by the
    next app start."""
    (root,) = _dirs(tmp_path, "review")
    app = tmp_path / "app"
    protected = app / "input" / "gradio_uploads"
    protected.mkdir(parents=True)
    (other,) = _dirs(tmp_path, "bin")
    verdict = review.validate_destinations(root, str(protected), other, app_root=str(app))
    assert verdict.code == "protected_runtime_destination"
    assert "BeatSync manages itself" in review.refusal_text(verdict)


def test_two_separate_folders_are_accepted_and_nothing_is_created(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    library = str(tmp_path / "does-not-exist-yet")
    (discarded,) = _dirs(tmp_path, "bin")
    assert review.validate_destinations(root, library, discarded).ok
    assert not os.path.exists(library), "validation must create nothing, not even a destination"


def test_only_the_destination_a_sort_will_write_has_to_be_set(tmp_path):
    """An operator who keeps everything is never asked to name a quarantine folder."""
    (root,) = _dirs(tmp_path, "review")
    (library,) = _dirs(tmp_path, "lib")
    assert review.validate_destinations(root, library, "", need_discarded=False).ok
    # ...but a sort that WILL discard must have one.
    missing = review.validate_destinations(root, library, "")
    assert not missing.ok and missing.code == "empty_path"


def test_a_single_destination_still_gets_every_rule_that_applies_to_one(tmp_path):
    """The narrow path must not become a hole: containment, equality and the protected list all
    still apply when only one destination is being written."""
    root, inside = _dirs(tmp_path, "review", "review/kept")
    assert review.validate_destinations(
        root, inside, "", need_discarded=False).code == "destination_inside_input"
    assert review.validate_destinations(
        root, root, "", need_discarded=False).code == "library_is_input"


def test_validating_nothing_is_not_a_refusal(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    assert review.validate_destinations(root, "", "", need_library=False,
                                        need_discarded=False).ok


def test_every_refusal_code_has_operator_prose():
    """A stable machine token is for the log; the operator needs a sentence naming what to fix."""
    for code in ("destinations_identical", "destinations_nested", "destination_inside_input",
                 "input_inside_destination", "library_is_input", "rejected_is_input",
                 "protected_runtime_destination", "unresolved_reparse_point",
                 "input_not_a_directory", "empty_path"):
        assert code in review.REFUSAL_PROSE, code
        assert len(review.REFUSAL_PROSE[code]) > 20, code


def test_an_unknown_refusal_code_names_itself_rather_than_inventing_a_reason():
    verdict = core.Verdict(False, "some_future_code", "detail")
    assert "some_future_code" in review.refusal_text(verdict)


# --- the live read-out ------------------------------------------------------------------------

def test_the_live_readout_guides_rather_than_complains_while_a_form_is_half_typed(tmp_path):
    """It runs on every keystroke, so an unset folder is the normal state of a form, not an
    error. The gallery stays usable throughout -- only sorting is withheld."""
    (root,) = _dirs(tmp_path, "review")
    ok, message = review.destination_feedback(root, "", "")
    assert ok is False
    assert "Set a Media Library folder" in message
    assert "❌" not in message, "an unset field is not a failure"

    ok, message = review.destination_feedback(root, str(tmp_path / "lib"), "")
    assert ok is False and "Discarded folder" in message


def test_the_live_readout_names_the_identical_folder_problem_immediately(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    (same,) = _dirs(tmp_path, "Discart")
    ok, message = review.destination_feedback(root, same, same)
    assert ok is False
    assert "❌" in message
    assert "SAME folder" in message


def test_the_live_readout_confirms_a_good_pair_and_promises_nothing_was_created(tmp_path):
    (root,) = _dirs(tmp_path, "review")
    library, discarded = _dirs(tmp_path, "lib", "bin")
    ok, message = review.destination_feedback(root, library, discarded)
    assert ok is True
    assert "usable and separate" in message
    assert "Nothing has been created or moved" in message


def test_the_live_readout_checks_the_pair_even_before_a_folder_is_scanned(tmp_path):
    """Phase 4 wants the feedback immediate, and "these two are the same folder" is answerable
    with no review folder at all.

    This caught a real gap on its first run: the full three-root check identifies the input folder
    FIRST and returns on the first failure, so with nothing scanned yet it refused with
    `empty_path` and never reached the comparison the operator needed to see. The pair is now
    compared on its own, before the folder is known.
    """
    (same,) = _dirs(tmp_path, "Discart")
    ok, message = review.destination_feedback("", same, same)
    assert ok is False
    assert "SAME folder" in message

    nested_outer, nested_inner = _dirs(tmp_path, "lib", "lib/bin")
    ok, message = review.destination_feedback("", nested_outer, nested_inner)
    assert ok is False
    assert "inside the other" in message


def test_a_good_pair_before_a_scan_is_accepted_without_claiming_more_than_it_knows(tmp_path):
    """It cannot yet say the destinations are clear of the review folder, so it must not."""
    library, discarded = _dirs(tmp_path, "lib", "bin")
    ok, message = review.destination_feedback("", library, discarded)
    assert ok is True
    assert "different" in message
    assert "checked against the review folder as well when you scan" in message


def test_the_pair_comparison_is_the_same_helper_the_full_check_uses(tmp_path):
    """One definition of "the same folder". Two would eventually disagree."""
    (root,) = _dirs(tmp_path, "review")
    (same,) = _dirs(tmp_path, "Discart")
    standalone = core.compare_destinations(same, same)
    full = review.validate_destinations(root, same, same)
    assert standalone.code == full.code == "destinations_identical"


# =============================================================================================
# The fork package's hard rule
# =============================================================================================

def test_the_module_imports_no_runtime_and_holds_no_state():
    """Stdlib only, so the whole review model is testable on a bare interpreter -- and no
    module-level mutable record of one operator's clips exists."""
    import inspect

    source = inspect.getsource(review)
    for forbidden in ("import gradio", "import numpy", "import cv2", "import librosa",
                      "import cupy", "from logger", "from paths", "import paths"):
        assert forbidden not in source, forbidden

    # The only module-level containers permitted are fixed lookup tables, named individually so a
    # future per-operator record cannot join them by being spelled similarly. None of them is
    # keyed by a clip, a decision, a session or a path.
    tables = {"REFUSAL_PROSE", "_CONTENT_TYPES", "_CLASSES", "_BADGES"}
    mutable = {name for name, value in vars(review).items()
               if not name.startswith("__") and isinstance(value, (dict, list, set))
               and name not in tables}
    assert mutable == set(), f"module-level mutable state: {sorted(mutable)}"

    # And the permitted tables really are static vocabularies, not stores.
    assert set(review._CLASSES) == set(review.DECISIONS)
    assert set(review._BADGES) == set(review.DECISIONS)
    assert set(review._CONTENT_TYPES) == set(review.SERVABLE_SUFFIXES)


def test_the_timecode_formatter_is_total_over_junk():
    assert review.format_timecode(None) == "0:00.0"
    assert review.format_timecode(-5) == "0:00.0"
    assert review.format_timecode(float("nan")) == "—"
    assert review.format_timecode("nonsense") == "—"
    assert review.format_timecode(2.5) == "0:02.5"
    assert review.format_timecode(3725.0) == "1:02:05.0"


def test_the_plan_and_the_outcome_records_are_json_serialisable_shapes():
    """They ride on `gr.State`, so nothing live may enter them."""
    plan = review.plan_sort([_tile("A")], {"A": review.SELECTED}, "L", "D")
    assert json.loads(json.dumps({
        "keep": list(plan.keep), "rejected": list(plan.rejected),
        "unmarked": list(plan.unmarked), "signature": plan.signature}))
