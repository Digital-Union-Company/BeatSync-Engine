"""R16 — Music Playlist Mode V1.

The owner asked for one thing: let several music files be **one ordered, continuous soundtrack** for
Create Music Video, keep Single Track as the default, and make the existing repetition control
repeat the **whole playlist** rather than each song.

So this suite is organised around the claims that make that true rather than around the files that
implement it:

1.  the Music Source control normalises like every other control in this repository;
2.  the ordered soundtrack is a model in which **order is identity** and a repeated track is kept;
3.  the seven editing operations the owner named are exact, total and deterministic — including
    that Shuffle is an explicit, seeded, one-time reorder with no randomness on the render path;
4.  the render-admission **freeze** crosses into the pipeline as plain primitives and is `()` for
    anything that is not a playlist declaration;
5.  the sample-count verification rests on a real RIFF parse, including the shape FFmpeg actually
    writes, and an **unmeasured** header is a third answer rather than a refusal;
6.  the runtime builder validates the WHOLE selection, never returns a subset, owns one `Popen`,
    cleans up every intermediate on every path, and never re-types a cancellation;
7.  **Single Track is structurally the legacy render** — every statement the feature adds to
    `gui.py`'s render body sits behind a playlist guard, asserted over the real AST;
8.  **Repeat Playlist repeats the whole ordered playlist**, and that falls out of R10 unchanged
    because the playlist master simply *is* the music file by the time R10 runs;
9.  the playlist reaches no Stage-5 cache identity, no Qwen request, no creative recipe, no video
    source identity and no preparation state;
10. the wiring contract — positional alignment, the writer matrix, the Variant Lab freeze.

Loading techniques are the established ones: the pure fork module imports normally;
`music_playlist_builder` — which imports `ffmpeg_processing` and therefore the portable runtime — is
reached by AST inspection plus execution of its dependency-free parts; and `gui.py` is reached by
AST inspection throughout.
"""

from __future__ import annotations

import ast
import os
import struct

import pytest

# [FORK] R17-A: one shared definition of "Gradio injects this parameter" (see `tests/conftest.py`).
from conftest import submitted_parameters

from beatsync_fork import desktop_folders as fork_folders
from beatsync_fork import music_playlist as P
from beatsync_fork import music_repeat as fork_music_repeat

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
_GUI = os.path.join(_SRC, "gui.py")
_BUILDER = os.path.join(_SRC, "music_playlist_builder.py")
_REPEATER = os.path.join(_SRC, "music_repeater.py")
_PURE = os.path.join(_SRC, "beatsync_fork", "music_playlist.py")
_PROCESSOR = os.path.join(_SRC, "video_processor.py")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(_tree(path))
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _body(path: str, name: str) -> str:
    return "\n".join(ast.unparse(statement) for statement in _func(path, name).body)


def _code(path: str, name: str) -> str:
    """One function's body with its docstring removed.

    Needed for the same reason `_executable_source` is: these functions deliberately *name* in
    prose the thing they must never *do* — `probe_duration`'s docstring explains why it does not use
    `get_video_duration`, and `_on_repeat_music_change`'s explains what it probes in which mode — so
    a substring check over the whole body would be satisfied by the explanation itself.
    """
    statements = _func(path, name).body
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]
    return "\n".join(ast.unparse(statement) for statement in statements)


def _outputs_names(path: str, kwargs: dict) -> set:
    """Every component name in an `outputs=` value, resolving one level of list indirection.

    `playlist_outputs` is a named list, so a plain walk sees the *name* and never the widgets in it
    — which is how every "is this widget ever written?" assertion in a suite goes vacuous the moment
    a widget moves behind an indirection. This is the local form of the repository's
    `expanded_names_in`.
    """
    outputs = kwargs.get("outputs")
    if outputs is None:
        return set()
    names = {getattr(node, "id", None) for node in ast.walk(outputs)
             if isinstance(node, ast.Name)}
    tree = _tree(path)
    for listname in list(names):
        if not listname:
            continue
        assignment = next((node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                           and getattr(node.targets[0], "id", None) == listname
                           and isinstance(node.value, (ast.List, ast.Tuple))), None)
        if assignment is not None:
            names |= {getattr(e, "id", None) for e in assignment.value.elts}
    return names


def _executable_source(path: str) -> str:
    """The module's code with every docstring and comment removed.

    Needed because these modules deliberately *name* in prose the things they must never *do* — the
    builder's docstring explains why the master is not in `get_processing_dir()` and why it does not
    import a sibling's runner — so a substring check over the raw file would be satisfied by the
    explanation itself.
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _click_kwargs(path: str, button: str) -> dict:
    call = next(node for node in ast.walk(_tree(path))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "click"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == button)
    return {kw.arg: kw.value for kw in call.keywords}


def _click_inputs(path: str, button: str) -> list:
    return [getattr(node, "id", None) for node in _click_kwargs(path, button)["inputs"].elts]


def _entry(path: str, size: int = 10, mtime: int = 1, duration: float = 60.0) -> tuple:
    return (path, size, mtime, duration)


#: Every global name this feature adds to `gui.py`'s render body. Used by the structural guard that
#: makes "Single Track is the legacy render" a property of the code's shape.
PLAYLIST_GLOBALS = {"fork_playlist", "music_playlist_builder"}


# ===========================================================================
# 1. THE MUSIC SOURCE CONTROL
# ===========================================================================


def test_the_two_modes_and_the_default():
    assert (P.MODE_SINGLE, P.MODE_PLAYLIST) == ("single", "playlist")
    assert P.DEFAULT_MODE == P.MODE_SINGLE
    assert P.MODE_VALUES == (P.MODE_SINGLE, P.MODE_PLAYLIST)


@pytest.mark.parametrize("value,expected", [
    ("single", "single"), ("playlist", "playlist"),
    ("PLAYLIST", "playlist"), ("  Playlist  ", "playlist"),
    # Unrecognised falls back to the DEFAULT, never to playlist: an operator who never touched the
    # control must get exactly the render they got before R16.
    ("", "single"), ("nonsense", "single"), (None, "single"), (0, "single"), (1, "single"),
    ([], "single"), ({}, "single"), (object(), "single"),
    # `bool` is rejected before anything else, because `True` is not a mode.
    (True, "single"), (False, "single"),
])
def test_normalize_mode_is_total(value, expected):
    assert P.normalize_mode(value) == expected


@pytest.mark.parametrize("value,playlist", [
    ("playlist", True), ("PLAYLIST", True),
    ("single", False), (None, False), ("", False), (True, False), (7, False),
])
def test_is_playlist_only_answers_true_for_a_real_declaration(value, playlist):
    assert P.is_playlist(value) is playlist


def test_the_gui_pre_check_literal_cannot_drift_from_the_constant():
    """`_process_video_guarded_unlocked` pre-checks `music_source != 'single'` with a LITERAL, for
    the same def-time reason `repeat_music`'s default is the literal `1`. Pinned so it cannot drift."""
    body = _body(_GUI, "_process_video_guarded_unlocked")
    assert "if music_source != 'single':" in body
    assert P.MODE_SINGLE == "single"


# ===========================================================================
# 2. THE ORDERED SOUNDTRACK
# ===========================================================================


def test_order_is_identity():
    """Two playlists holding the same files in a different order are different soundtracks: the
    beats, the sections and therefore the whole edit differ."""
    a = P.playlist_from_entries([_entry("C:/m/a.mp3"), _entry("C:/m/b.mp3")])
    b = P.playlist_from_entries([_entry("C:/m/b.mp3"), _entry("C:/m/a.mp3")])
    assert a.identity != b.identity
    assert a.count == b.count == 2
    assert a.total_seconds == b.total_seconds


def test_identity_notices_a_replaced_file_not_merely_a_renamed_one():
    same = P.playlist_from_entries([_entry("C:/m/a.mp3", size=10, mtime=1)])
    swapped = P.playlist_from_entries([_entry("C:/m/a.mp3", size=99, mtime=1)])
    assert same.identity != swapped.identity


def test_a_repeated_track_is_kept_never_deduplicated():
    """A track listed twice is an intentional repeat inside the soundtrack. Dropping it would
    render a shorter video than the operator asked for."""
    playlist = P.playlist_from_entries([
        _entry("C:/m/a.mp3", duration=30.0),
        _entry("C:/m/b.mp3", duration=30.0),
        _entry("C:/m/a.mp3", duration=30.0),
    ])
    assert playlist.count == 3
    assert [t.name for t in playlist.tracks] == ["a.mp3", "b.mp3", "a.mp3"]
    assert playlist.total_seconds == pytest.approx(90.0)


@pytest.mark.parametrize("entries", [
    None, "", "a string", 17, object(), b"bytes",
    [None], [17], [()], [("",)], [{"path": ""}], [{"nothing": 1}],
])
def test_playlist_from_entries_is_total(entries):
    """It reads `gr.State`, which is untrusted input: a malformed row costs that row, not the tab."""
    assert isinstance(P.playlist_from_entries(entries), P.Playlist)


def test_a_malformed_row_is_skipped_and_the_rest_survive():
    playlist = P.playlist_from_entries([
        _entry("C:/m/a.mp3"), None, 17, _entry("C:/m/b.mp3"), {"path": ""},
    ])
    assert [t.name for t in playlist.tracks] == ["a.mp3", "b.mp3"]


def test_entries_are_accepted_as_tuples_mappings_and_bare_paths():
    tuple_form = P.track_from_entry(("C:/m/a.mp3", 5, 9, 1.5))
    mapping_form = P.track_from_entry(
        {"path": "C:/m/a.mp3", "size": 5, "mtime_ns": 9, "duration_seconds": 1.5})
    bare = P.track_from_entry("C:/m/a.mp3")
    assert tuple_form == mapping_form
    assert bare == P.PlaylistTrack(path="C:/m/a.mp3")
    # A bare path has NO recorded identity, which is "never probed" rather than "probed as empty".
    assert (bare.size, bare.mtime_ns) == (0, 0)


def test_the_queue_is_bounded():
    """One mis-click in a multi-select dialog must not ask for a decode of ten thousand files."""
    playlist = P.playlist_from_entries([_entry(f"C:/m/{i}.mp3") for i in range(P.MAX_TRACKS + 50)])
    assert playlist.count == P.MAX_TRACKS


def test_a_track_whose_duration_was_never_measured_does_not_poison_the_total():
    playlist = P.playlist_from_entries([
        _entry("C:/m/a.mp3", duration=30.0),
        ("C:/m/b.mp3", 1, 1, 0.0),
        ("C:/m/c.mp3", 1, 1, float("nan")),
    ])
    assert playlist.total_seconds == pytest.approx(30.0)


@pytest.mark.parametrize("path,supported", [
    ("a.mp3", True), ("a.WAV", True), ("a.flac", True), ("a.m4a", True), ("a.opus", True),
    ("a.mp4", False), ("a.txt", False), ("a", False), ("", False), (None, False), (7, False),
])
def test_supported_extensions(path, supported):
    assert P.has_supported_extension(path) is supported


# ===========================================================================
# 3. THE SEVEN EDITING OPERATIONS
# ===========================================================================


def _names(playlist) -> list:
    return [track.name for track in playlist.tracks]


def test_add_appends_in_filename_order_and_never_reorders_what_is_already_queued():
    """A native multi-select dialog returns whatever order the OS supplies, not the click order —
    the measured defect `audio_mix.voice_order_key` exists for. The ADDITION is ordered; the
    existing queue is not touched."""
    current = P.playlist_from_entries([_entry("C:/m/zz_first.mp3")])
    updated = P.add_tracks(current, [
        _entry("C:/other/03_c.mp3"), _entry("C:/m/01_a.mp3"), _entry("C:/x/02_b.mp3"),
    ])
    assert _names(updated) == ["zz_first.mp3", "01_a.mp3", "02_b.mp3", "03_c.mp3"]


def test_add_orders_by_FILENAME_not_by_whole_path():
    """The parents differ, so a whole-path sort would give a different (and wrong) answer. This is
    the assertion that keeps the reuse of `voice_order_key` from going vacuous."""
    updated = P.add_tracks(P.EMPTY, [
        _entry("C:/zzz/01_a.mp3"), _entry("C:/aaa/02_b.mp3"),
    ])
    assert _names(updated) == ["01_a.mp3", "02_b.mp3"]


def test_add_respects_the_bound():
    full = P.playlist_from_entries([_entry(f"C:/m/{i}.mp3") for i in range(P.MAX_TRACKS)])
    assert P.add_tracks(full, [_entry("C:/m/extra.mp3")]).count == P.MAX_TRACKS


def test_remove_takes_several_and_clears_the_selection():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abcd"])
    updated, selection = P.remove_tracks(playlist, [0, 2])
    assert _names(updated) == ["b.mp3", "d.mp3"]
    assert selection == ()


@pytest.mark.parametrize("indices", [None, (), [], "abc", {}, object(), True, False,
                                     [99], [-1], ["x"]])
def test_remove_with_nothing_usable_selected_changes_nothing(indices):
    playlist = P.playlist_from_entries([_entry("C:/m/a.mp3"), _entry("C:/m/b.mp3")])
    updated, selection = P.remove_tracks(playlist, indices)
    assert updated.as_entries() == playlist.as_entries()
    assert selection == ()


def test_move_up_moves_one_track_and_the_selection_follows_it():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abc"])
    updated, selection = P.move_up(playlist, [2])
    assert _names(updated) == ["a.mp3", "c.mp3", "b.mp3"]
    assert selection == (1,)


def test_move_up_shifts_a_contiguous_block_as_a_whole():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abcd"])
    updated, selection = P.move_up(playlist, [1, 2])
    assert _names(updated) == ["b.mp3", "c.mp3", "a.mp3", "d.mp3"]
    assert selection == (0, 1)


def test_move_up_against_the_top_moves_nothing_and_does_not_tear_the_block():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abc"])
    updated, selection = P.move_up(playlist, [0, 1])
    assert _names(updated) == ["a.mp3", "b.mp3", "c.mp3"]
    assert selection == (0, 1)


def test_move_down_moves_one_track_and_the_selection_follows_it():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abc"])
    updated, selection = P.move_down(playlist, [0])
    assert _names(updated) == ["b.mp3", "a.mp3", "c.mp3"]
    assert selection == (1,)


def test_move_down_shifts_a_contiguous_block_as_a_whole():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abcd"])
    updated, selection = P.move_down(playlist, [0, 1])
    assert _names(updated) == ["c.mp3", "a.mp3", "b.mp3", "d.mp3"]
    assert selection == (1, 2)


def test_move_down_against_the_bottom_moves_nothing():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abc"])
    updated, selection = P.move_down(playlist, [1, 2])
    assert _names(updated) == ["a.mp3", "b.mp3", "c.mp3"]
    assert selection == (1, 2)


def test_repeated_move_up_walks_a_track_to_the_top_and_stops():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abcd"])
    selection = [3]
    for _ in range(10):
        playlist, selection = P.move_up(playlist, selection)
    assert _names(playlist) == ["d.mp3", "a.mp3", "b.mp3", "c.mp3"]
    assert selection == (0,)


def test_shuffle_is_deterministic_for_a_seed_and_keeps_every_track():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{i:02d}.mp3") for i in range(12)])
    first, _ = P.shuffle(playlist, 4242)
    again, _ = P.shuffle(playlist, 4242)
    other, _ = P.shuffle(playlist, 4243)
    assert first.as_entries() == again.as_entries()
    assert sorted(_names(first)) == sorted(_names(playlist))
    assert first.as_entries() != playlist.as_entries()
    assert other.as_entries() != first.as_entries()


@pytest.mark.parametrize("seed", [None, "", "abc", object(), -1, True])
def test_shuffle_with_an_unusable_seed_stays_total_and_reproducible(seed):
    playlist = P.playlist_from_entries([_entry(f"C:/m/{i:02d}.mp3") for i in range(8)])
    once, _ = P.shuffle(playlist, seed)
    twice, _ = P.shuffle(playlist, seed)
    assert once.as_entries() == twice.as_entries()
    assert sorted(_names(once)) == sorted(_names(playlist))


def test_shuffle_of_fewer_than_two_tracks_is_a_no_op():
    one = P.playlist_from_entries([_entry("C:/m/a.mp3")])
    assert P.shuffle(one, 5)[0].as_entries() == one.as_entries()
    assert P.shuffle(P.EMPTY, 5)[0].as_entries() == ()


def test_sort_by_name_is_case_insensitive_and_stable():
    playlist = P.playlist_from_entries([
        _entry("C:/m/Beta.mp3"), _entry("C:/m/alpha.mp3"), _entry("C:/m/GAMMA.mp3"),
    ])
    updated, selection = P.sort_by_name(playlist)
    assert _names(updated) == ["alpha.mp3", "Beta.mp3", "GAMMA.mp3"]
    assert selection == ()


def test_sort_after_add_is_a_no_op_on_the_added_block():
    """Both use the same key, so pressing Sort by Name after an Add is not a second, differently
    ordered answer."""
    added = P.add_tracks(P.EMPTY, [
        _entry("C:/x/03_c.mp3"), _entry("C:/y/01_a.mp3"), _entry("C:/z/02_b.mp3")])
    assert P.sort_by_name(added)[0].as_entries() == added.as_entries()


def test_clear_empties_the_queue_and_the_selection():
    assert P.clear() == (P.EMPTY, ())
    assert P.EMPTY.count == 0


def test_every_operation_returns_a_new_playlist_and_mutates_nothing():
    playlist = P.playlist_from_entries([_entry(f"C:/m/{n}.mp3") for n in "abc"])
    before = playlist.as_entries()
    P.add_tracks(playlist, [_entry("C:/m/d.mp3")])
    P.remove_tracks(playlist, [0])
    P.move_up(playlist, [1])
    P.move_down(playlist, [1])
    P.shuffle(playlist, 9)
    P.sort_by_name(playlist)
    assert playlist.as_entries() == before


def test_shuffle_is_the_only_place_the_feature_uses_randomness():
    """Shuffle is an explicit one-time reorder performed when the button is pressed. There is no
    randomisation anywhere on the render path, and this is asserted rather than reasoned about."""
    pure = _executable_source(_PURE)
    assert pure.count("random.") == 1
    assert "random.Random(" in _body(_PURE, "shuffle")
    assert "random" not in _executable_source(_BUILDER)
    impl = ast.unparse(_func(_GUI, "_process_video_impl"))
    for forbidden in ("random", "shuffle", "Shuffle"):
        assert forbidden not in impl, forbidden


# ===========================================================================
# 4. THE RENDER-ADMISSION FREEZE
# ===========================================================================


def test_the_freeze_is_empty_for_anything_that_is_not_a_playlist_declaration():
    entries = [_entry("C:/m/a.mp3")]
    assert P.freeze_entries("single", entries) == ()
    assert P.freeze_entries(None, entries) == ()
    assert P.freeze_entries("nonsense", entries) == ()
    assert P.freeze_entries(True, entries) == ()
    assert len(P.freeze_entries("playlist", entries)) == 1


@pytest.mark.parametrize("entries", [None, "", 17, object(), [None], [17], []])
def test_the_freeze_is_total_and_never_raises(entries):
    assert isinstance(P.freeze_entries("playlist", entries), tuple)


def test_the_frozen_value_is_plain_primitives_only():
    """It crosses into `gr.State` (which deep-copies) and into `_process_video_impl` (which several
    suites execute against a namespace holding no fork module), so nothing but str/int/float may be
    in it."""
    frozen = P.freeze_entries("playlist", [_entry("C:/m/a.mp3"), _entry("C:/m/b.mp3")])
    assert isinstance(frozen, tuple)
    for row in frozen:
        assert isinstance(row, tuple) and len(row) == 4
        assert isinstance(row[0], str)
        assert isinstance(row[1], int) and isinstance(row[2], int)
        assert isinstance(row[3], float)
    import copy
    assert copy.deepcopy(frozen) == frozen


def test_the_freeze_preserves_order_and_duplicates_exactly():
    rows = [_entry("C:/m/b.mp3"), _entry("C:/m/a.mp3"), _entry("C:/m/b.mp3")]
    assert [r[0] for r in P.freeze_entries("playlist", rows)] == [r[0] for r in rows]


def test_an_empty_playlist_in_playlist_mode_is_refused_before_the_render_body():
    """A refusal, never a silent fall back to the Single Track picker -- which would render music
    the operator did not ask for, or nothing at all, without saying which."""
    body = _body(_GUI, "_process_video_guarded_unlocked")
    assert "fork_playlist.EMPTY_PLAYLIST_MESSAGE" in body
    assert "playlist = fork_playlist.freeze_entries(music_source, playlist_state)" in body
    # ...and it happens BEFORE the render body is marked as started, so a refused click raises no
    # desktop notification, exactly as a source-gate refusal does not.
    assert body.index("EMPTY_PLAYLIST_MESSAGE") < body.index("RENDER_STARTED_KEY] = True")
    # exactly one freeze, so there is no second interpretation of what Music Source means
    assert body.count("freeze_entries(") == 1
    with open(_GUI, "r", encoding="utf-8") as handle:
        assert handle.read().count("freeze_entries(") == 1


def test_the_refusal_is_shared_fatal():
    """The gate's inputs here are this handler's own submitted arguments, frozen and identical for
    every candidate of a C3 batch, and no candidate recipe can put a track in an empty playlist."""
    core = _func(_GUI, "_process_video_guarded_unlocked")
    for node in ast.walk(core):
        if (isinstance(node, ast.If)
                and ast.unparse(node.test) == "not playlist"):
            rendered = ast.unparse(node)
            assert "RenderOutcomeKind.SHARED_FATAL" in rendered
            assert "EMPTY_PLAYLIST_MESSAGE" in rendered
            break
    else:
        raise AssertionError("the empty-playlist refusal was not found")


# ===========================================================================
# 5. RIFF GEOMETRY — THE SAMPLE-COUNT VERIFICATION'S FOUNDATION
# ===========================================================================


def _wav_header(frames: int, channels: int = 2, rate: int = 48_000, bits: int = 24,
                extra_chunks: bytes = b"") -> bytes:
    """A canonical RIFF/WAVE header, optionally with chunks before `data` as FFmpeg writes them."""
    frame_bytes = channels * (bits // 8)
    data_size = frames * frame_bytes
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * frame_bytes, frame_bytes, bits)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra_chunks
    body += b"data" + struct.pack("<I", data_size)
    return b"RIFF" + struct.pack("<I", 4 + len(body) + data_size) + b"WAVE" + body


def test_a_canonical_header_is_parsed_exactly():
    geometry = P.parse_wav_header(_wav_header(frames=48_000))
    assert geometry == P.WavGeometry(channels=2, sample_rate=48_000,
                                    bits_per_sample=24, frames=48_000)
    assert geometry.seconds == pytest.approx(1.0)


def test_a_LIST_chunk_before_data_is_walked_past():
    """FFmpeg's wav muxer writes a `LIST INFO` chunk naming the encoder. A parser that assumed
    `data` came straight after `fmt ` would answer `None` for every real file."""
    listing = b"LIST" + struct.pack("<I", 26) + b"INFOISFT" + struct.pack("<I", 14) \
        + b"Lavf61.7.100\x00\x00"
    geometry = P.parse_wav_header(_wav_header(frames=1000, extra_chunks=listing))
    assert geometry is not None and geometry.frames == 1000


def test_an_odd_sized_chunk_is_word_aligned():
    odd = b"junk" + struct.pack("<I", 3) + b"abc" + b"\x00"
    geometry = P.parse_wav_header(_wav_header(frames=7, extra_chunks=odd))
    assert geometry is not None and geometry.frames == 7


@pytest.mark.parametrize("header", [
    None, "", 17, object(), b"", b"RIFF", b"RIFFxxxxWAVE",
    b"XXXX" + struct.pack("<I", 4) + b"WAVE",           # not RIFF
    b"RIFF" + struct.pack("<I", 4) + b"XXXX",           # not WAVE
])
def test_an_unparseable_header_is_unmeasured_not_zero(header):
    """`None` is a genuine third answer, never read as zero frames -- the rule
    `frame_lock.FrameCountVerdict` records, for the same reason: an unmeasured count must not become
    a refusal."""
    assert P.parse_wav_header(header) is None


def test_a_streamed_wav_whose_data_size_is_a_placeholder_is_unmeasured():
    header = _wav_header(frames=0)
    assert P.parse_wav_header(header) is None
    sentinel = _wav_header(frames=1).replace(
        b"data" + struct.pack("<I", 6), b"data" + struct.pack("<I", 0xFFFFFFFF))
    assert P.parse_wav_header(sentinel) is None


def test_a_data_chunk_beyond_the_bounded_read_is_unmeasured():
    """The runtime side reads a bounded prefix. If `data` has not been reached the answer is
    'unmeasured', never 'zero frames'."""
    filler = b"junk" + struct.pack("<I", 20_000) + b"\x00" * 20_000
    whole = _wav_header(frames=100, extra_chunks=filler)
    assert P.parse_wav_header(whole[:P.WAV_HEADER_PROBE_BYTES]) is None
    # ...and with the whole header available it parses, so the bound is the only reason it did not.
    assert P.parse_wav_header(whole) is not None


def test_a_fmt_chunk_with_impossible_geometry_is_unmeasured():
    broken = P.parse_wav_header(_wav_header(frames=10, channels=0))
    assert broken is None


# ===========================================================================
# 6. THE RUNTIME BUILDER
# ===========================================================================


def _exec_builder_functions(names, classes=()) -> dict:
    """Execute dependency-free builder functions against a tiny synthesised namespace.

    `music_playlist_builder` imports `ffmpeg_processing` and therefore the whole portable runtime,
    so it cannot be imported on a bare interpreter. The argv builders and the arithmetic are
    extracted from the REAL source and executed here, so these are assertions about shipped code
    rather than about a reimplementation.

    `classes` names dataclasses the extracted functions construct. They are carried as whole
    `ClassDef` nodes and re-`unparse`d — never `ast.get_source_segment`, whose `lineno` points at
    the `class` keyword and would silently drop `@dataclass(frozen=True)`, giving back a fieldless
    plain class (the trap `.claude/rules/test-harness.md` records).
    """
    from dataclasses import dataclass
    namespace = {
        "FFMPEG_PATH": "ffmpeg.exe", "FFPROBE_PATH": "ffprobe.exe",
        "TRACK_CODEC": "pcm_s24le", "TRACK_SAMPLE_RATE": 48_000, "TRACK_CHANNELS": 2,
        "fork_playlist": P, "os": os, "dataclass": dataclass,
        "PlaylistBuildError": type("PlaylistBuildError", (Exception,), {}),
    }
    tree = _tree(_BUILDER)
    wanted_classes = [node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name in set(classes)]
    assert len(wanted_classes) == len(set(classes)), [n.name for n in wanted_classes]
    wanted = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in set(names)]
    assert len(wanted) == len(set(names)), [n.name for n in wanted]
    module = ast.Module(body=wanted_classes + wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), _BUILDER, "exec"), namespace)
    return namespace


def test_the_decode_argv_takes_the_first_audio_stream_and_nothing_else():
    """`-vn` and `-map 0:a:0` together are load-bearing: an MP3 with cover art presents a video
    stream, and an M4A or MKV may carry several audio tracks."""
    namespace = _exec_builder_functions(["build_decode_command"])
    argv = namespace["build_decode_command"]("C:/m/a.mp3", "C:/tmp/01.wav")
    assert argv[0] == "ffmpeg.exe"
    assert "-nostdin" in argv and "-y" in argv
    assert "-vn" in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert argv[argv.index("-c:a") + 1] == "pcm_s24le"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "2"
    assert argv[-1] == "C:/tmp/01.wav"


def test_the_join_argv_stream_copies_and_never_re_encodes():
    """`-c copy` is the whole point: the tracks are already canonical, so there is no requantisation
    and nothing between two songs for a gap to live in."""
    namespace = _exec_builder_functions(["build_join_command"])
    argv = namespace["build_join_command"]("C:/tmp/list.txt", "C:/tmp/master.wav")
    assert argv[argv.index("-f") + 1] == "concat"
    assert argv[argv.index("-safe") + 1] == "0"
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[-1] == "C:/tmp/master.wav"


def test_the_join_list_is_in_playlist_order_and_escapes_a_quote():
    namespace = _exec_builder_functions(["build_join_list"])
    text = namespace["build_join_list"]([
        r"C:\tmp\01.wav", r"C:\Users\O'Brien\02.wav"])
    assert text.splitlines() == ["file 'C:/tmp/01.wav'",
                                 "file 'C:/Users/O'\\''Brien/02.wav'"]


def test_the_scratch_estimate_is_two_copies_of_the_soundtrack():
    """Every decoded track has to exist while the join reads them, so the peak is TWO copies.
    Stated rather than discovered."""
    namespace = _exec_builder_functions(["estimate_scratch_bytes"])
    assert namespace["estimate_scratch_bytes"](100.0) == 2 * P.master_bytes(100.0)
    assert namespace["estimate_scratch_bytes"](None) == 0


def test_measured_track_placement_is_a_cumulative_SAMPLE_offset():
    """The whole reason the decode is per-track: 'track 4 starts at 11:02' is a measurement, not a
    sum of container metadata."""
    namespace = _exec_builder_functions(["_place_tracks"], classes=["PreparedTrack"])
    ordered = [("C:/m/a.mp3", 10.0), ("C:/m/b.mp3", 20.0), ("C:/m/c.mp3", 5.0)]
    frames = [480_000, 960_123, 240_000]
    placed = namespace["_place_tracks"](ordered, frames, 48_000, 35.0025625, True)
    assert [t.start_frame for t in placed] == [0, 480_000, 1_440_123]
    assert [t.frames for t in placed] == frames
    assert placed[1].start_seconds == pytest.approx(10.0)
    assert placed[1].duration_seconds == pytest.approx(960_123 / 48_000)
    assert [t.name for t in placed] == ["a.mp3", "b.mp3", "c.mp3"]
    # the source containers' own durations are kept alongside, never replaced
    assert [t.source_duration_seconds for t in placed] == [10.0, 20.0, 5.0]


def test_unmeasured_placement_falls_back_to_the_containers_and_still_lands_on_the_master():
    namespace = _exec_builder_functions(["_place_tracks"], classes=["PreparedTrack"])
    ordered = [("C:/m/a.mp3", 10.0), ("C:/m/b.mp3", 30.0)]
    placed = namespace["_place_tracks"](ordered, [None, None], 48_000, 44.0, False)
    assert placed[0].start_frame == 0
    total = placed[-1].start_frame + placed[-1].frames
    assert total == pytest.approx(44.0 * 48_000, rel=1e-6)


def test_the_sample_count_check_is_exact_equality_not_a_tolerance():
    """A stream copy of consecutive PCM runs either reproduced every sample or it did not. A single
    dropped or duplicated frame would put every later track at the wrong offset."""
    namespace = _exec_builder_functions(["_verify_sample_counts"])
    verify = namespace["_verify_sample_counts"]
    good = P.WavGeometry(2, 48_000, 24, 300)
    assert verify(good, [100, 200], False, 2) == (300, True)
    off_by_one = P.WavGeometry(2, 48_000, 24, 301)
    with pytest.raises(namespace["PlaylistBuildError"]) as excinfo:
        verify(off_by_one, [100, 200], False, 2)
    assert "301" in str(excinfo.value) and "300" in str(excinfo.value)


def test_an_unmeasured_count_is_never_a_refusal():
    namespace = _exec_builder_functions(["_verify_sample_counts"])
    verify = namespace["_verify_sample_counts"]
    good = P.WavGeometry(2, 48_000, 24, 300)
    assert verify(good, [100, None], False, 2) == (None, False)
    assert verify(good, [100, 200], True, 2) == (None, False)
    assert verify(None, [100, 200], False, 2) == (None, False)


def test_the_builder_validates_the_whole_selection_and_never_returns_a_subset():
    """A four-track soundtrack for an operator who queued five is a plausible-looking render that
    silently dropped a song -- what `prepare_voice_inputs` refuses to do for voice."""
    node = _func(_BUILDER, "prepare_playlist_tracks")
    rendered = ast.unparse(node)
    # every rejection RAISES rather than skipping
    assert "continue" not in rendered
    assert rendered.count("raise PlaylistInputError") >= 6
    # and it walks the queue in playlist order
    assert "enumerate(playlist.tracks, start=1)" in rendered


def test_the_builder_checks_the_recorded_identity_so_a_replaced_file_is_reported():
    assert "describe_identity_change" in _body(_BUILDER, "prepare_playlist_tracks")
    track = P.PlaylistTrack(path="C:/m/a.mp3", size=100, mtime_ns=5)
    assert P.describe_identity_change(track, 100, 5) == ""
    assert "changed" in P.describe_identity_change(track, 200, 5)
    assert "modified" in P.describe_identity_change(track, 100, 9)
    # a bare path has no recorded identity, so it is "never probed" rather than "changed"
    assert P.describe_identity_change(P.PlaylistTrack(path="C:/m/a.mp3"), 1, 2) == ""


def test_input_failures_and_build_failures_are_separate_types():
    """`gui.py` classifies them differently and must not guess: the operator's files are frozen for
    a whole batch (SHARED_FATAL), a generated artifact's failure is not (UNKNOWN_FATAL)."""
    classes = {node.name: [ast.unparse(base) for base in node.bases]
               for node in ast.walk(_tree(_BUILDER)) if isinstance(node, ast.ClassDef)}
    assert classes["PlaylistError"] == ["Exception"]
    assert classes["PlaylistInputError"] == ["PlaylistError"]
    assert classes["PlaylistBuildError"] == ["PlaylistError"]


def test_the_playlist_error_is_its_own_type_not_a_repeat_or_audio_layers_one():
    """The existing `except music_repeater.MusicRepeatError` and `except audio_mixdown.AudioMixError`
    handlers in the render body must keep meaning exactly what they mean today."""
    code = _executable_source(_BUILDER)
    assert "audio_mixdown" not in code
    assert "AudioMixError" not in code
    assert "MusicRepeatError" not in code
    assert "music_repeater" not in code


def test_the_builder_owns_exactly_one_popen_and_borrows_no_sibling_runner():
    """Each runtime module keeps its own small cancellable runner; a handle belongs to its frame."""
    code = _executable_source(_BUILDER)
    assert code.count("subprocess.Popen") == 1
    assert "_run_media_command" not in code
    imported = {alias.name for node in ast.walk(_tree(_BUILDER))
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(_tree(_BUILDER))
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "audio_mixdown" not in imported
    assert "music_repeater" not in imported


def test_the_runner_defaults_to_the_unchanged_blocking_call():
    """`lifecycle=None` must be byte-for-byte the pre-existing blocking `subprocess.run`, as the
    FIRST statement, so a non-cancellable caller behaves exactly as before."""
    node = _func(_BUILDER, "_run")
    statements = [s for s in node.body
                  if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                          and isinstance(s.value.value, str))]
    guard = statements[0]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "lifecycle is None"
    assert not guard.orelse
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Return)
    assert "subprocess.run(" in ast.unparse(guard.body[0].value)


def test_a_cancellation_is_never_re_typed_and_the_partial_master_is_still_discarded():
    """Re-typing it would report a user's Stop as a broken music file, and in a batch would
    describe a cancelled candidate as a problem with their playlist."""
    node = _func(_BUILDER, "build_playlist_master")
    handlers = [ast.unparse(h.type) for h in ast.walk(node)
                if isinstance(h, ast.ExceptHandler) and h.type is not None]
    assert "(PlaylistError, RenderCancelled)" in handlers
    for handler in ast.walk(node):
        if (isinstance(handler, ast.ExceptHandler)
                and ast.unparse(handler.type) == "(PlaylistError, RenderCancelled)"):
            rendered = ast.unparse(handler)
            assert "discard_playlist_master(master_path)" in rendered
            raises = [n for n in handler.body if isinstance(n, ast.Raise)]
            assert len(raises) == 1 and raises[0].exc is None
            break
    else:
        raise AssertionError("the combined handler was not found")


def test_no_handler_in_the_builder_is_a_bare_except_or_catches_everything():
    """A wide catch anywhere here would swallow a cancellation. The one `except Exception` is the
    runner's own best-effort kill in a `finally`, which cannot mask a result."""
    wide = []
    for node in ast.walk(_tree(_BUILDER)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or ast.unparse(node.type) == "Exception":
            wide.append(ast.unparse(node)[:60])
    assert len(wide) == 1, wide


def test_the_builder_cleans_up_every_intermediate_on_every_path():
    node = _func(_BUILDER, "build_playlist_master")
    finals = [t for t in ast.walk(node) if isinstance(t, ast.Try) and t.finalbody]
    assert finals
    text = "\n".join(ast.unparse(s) for t in finals for s in t.finalbody)
    assert "discard_playlist_master(list_path)" in text
    assert "discard_playlist_master(path)" in text
    assert "for path in decoded" in text


def test_the_master_never_goes_into_the_processing_directory():
    """`create_music_video` clears `get_processing_dir()` at the start of every render and would
    delete the soundtrack moments after it was written."""
    code = _executable_source(_BUILDER)
    assert "get_processing_dir" not in code
    assert "PROCESSING_DIR" not in code
    assert "session_dir" in _body(_BUILDER, "master_path_for")
    assert "uuid.uuid4()" in _body(_BUILDER, "master_path_for")


def test_the_builder_never_writes_near_the_operators_own_files():
    """Every artifact goes in `session_dir`; the queued tracks are opened read-only."""
    node = _func(_BUILDER, "build_playlist_master")
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and ast.unparse(call.func) == "os.path.join":
            assert ast.unparse(call.args[0]) == "session_dir", ast.unparse(call)
    code = _executable_source(_BUILDER)
    for forbidden in ("shutil.copy", "shutil.move", "os.rename", "os.replace", "rmtree"):
        assert forbidden not in code, forbidden
    # the ONE removal path, and it removes only files this module created
    assert code.count("os.remove(") == 1


def test_the_format_ceiling_and_the_volume_check_refuse_rather_than_corrupt():
    """A WAV `data` size is 32 bits, so an over-long soundtrack would be written with a wrapped
    header and would desynchronise. And a volume that cannot hold the peak is refused up front."""
    body = _body(_BUILDER, "_refuse_impossible_sizes")
    assert "MAX_MASTER_BYTES" in body
    assert "shutil.disk_usage(session_dir)" in body
    # an unmeasurable volume is NOT a refusal
    assert "except OSError" in body and "return" in body
    assert "build_playlist_master" in _executable_source(_BUILDER)
    assert "_refuse_impossible_sizes(" in _body(_BUILDER, "build_playlist_master")


def test_the_probe_raises_rather_than_answering_a_fallback():
    """`ffmpeg_processing.get_video_duration` answers 10.0 on failure, which is right for its caller
    and catastrophic here: a guessed track length misstates the whole soundtrack."""
    body = _code(_BUILDER, "probe_duration")
    assert "get_video_duration" not in body
    assert body.count("raise PlaylistInputError") >= 4
    # and the generated master's own probe failure is a BUILD error, not an input one
    assert "raise PlaylistBuildError" in _code(_BUILDER, "probe_master_duration")


# ===========================================================================
# 7. SINGLE TRACK IS STRUCTURALLY THE LEGACY RENDER
# ===========================================================================


def test_every_playlist_statement_in_the_render_body_is_behind_a_playlist_guard():
    """**The most important test in R16.**

    "Single Track changes nothing" must be a property of the code's shape, not of the values
    flowing through it. Every reference to either new module inside `_process_video_impl` has to sit
    inside an `if` whose condition is a playlist guard -- `playlist` for the work,
    `playlist_master_path` for the cleanup and the panel line -- so a Single Track render does not
    resolve either module, build any file or execute one added statement.

    This is also what keeps the established exec-based harnesses valid: several suites run the real
    `_process_video_impl` against a synthesised namespace that contains neither module, and they
    pass precisely because a Single Track render never looks them up.
    """
    impl = _func(_GUI, "_process_video_impl")
    allowed_tests = {"playlist", "playlist_master_path"}

    guarded: set[int] = set()
    for node in ast.walk(impl):
        if isinstance(node, ast.If) and ast.unparse(node.test) in allowed_tests:
            for inner in ast.walk(node):
                guarded.add(id(inner))

    found = 0
    for node in ast.walk(impl):
        if isinstance(node, ast.Name) and node.id in PLAYLIST_GLOBALS:
            found += 1
            assert id(node) in guarded, (
                f"{node.id} is referenced outside a playlist guard at line {node.lineno}")
    assert found >= 4, "the playlist block was not found at all"


def test_the_only_legacy_statements_touched_are_two_truthiness_tests():
    """The feature's whole intrusion into the pre-existing primary-input checks, stated directly:
    one `elif playlist:` branch and one `not playlist and` prefix. Neither resolves a module -- both
    test the truthiness of a plain tuple -- so a Single Track render takes the identical statements
    with the identical messages."""
    body = _body(_GUI, "_process_video_impl")
    assert "elif playlist:" in body
    assert "if not playlist and (not local_audio_path or not os.path.exists(local_audio_path)):" \
        in body
    assert "'❌ Error: No audio file selected'" in body
    assert "Audio file is missing or inaccessible" in body


def test_the_playlist_master_is_declared_before_the_try_and_discarded_in_the_finally():
    impl = _func(_GUI, "_process_video_impl")
    body = _body(_GUI, "_process_video_impl")
    assert body.index("playlist_master_path = None") < body.index("try:")
    finals = [t for t in ast.walk(impl) if isinstance(t, ast.Try) and t.finalbody]
    text = "\n".join(ast.unparse(s) for t in finals for s in t.finalbody)
    assert "music_playlist_builder.discard_playlist_master(playlist_master_path)" in text
    assert "if playlist_master_path:" in text


def test_the_preparation_is_handed_the_session_dir_and_the_lifecycle():
    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and "build_playlist_master" in ast.unparse(node.func):
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            assert kwargs["session_dir"] == "session_dir"
            assert kwargs["tracks"] == "playlist_tracks"
            assert kwargs["lifecycle"] == "lifecycle"
            break
    else:
        raise AssertionError("build_playlist_master is not called")


def test_both_failure_classes_are_named_and_neither_is_a_generic_catch():
    """`RenderCancelled` is an ordinary `Exception`, so a generic clause here would report a user's
    Stop as a playlist failure."""
    impl = _func(_GUI, "_process_video_impl")
    found = {}
    for node in ast.walk(impl):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        name = ast.unparse(node.type)
        if "Playlist" in name:
            found[name] = ast.unparse(node)
    assert "music_playlist_builder.PlaylistInputError" in found
    assert "music_playlist_builder.PlaylistBuildError" in found
    assert "RenderOutcomeKind.SHARED_FATAL" in found[
        "music_playlist_builder.PlaylistInputError"]
    assert "RenderOutcomeKind.UNKNOWN_FATAL" in found[
        "music_playlist_builder.PlaylistBuildError"]


def test_there_is_no_fallback_to_a_shorter_soundtrack_or_to_the_single_track_picker():
    """Rendering four of five queued songs, or one unrelated track, is a plausible-looking result
    that silently ignored the request."""
    impl = ast.unparse(_func(_GUI, "_process_video_impl"))
    for handler in ast.walk(_func(_GUI, "_process_video_impl")):
        if (isinstance(handler, ast.ExceptHandler) and handler.type is not None
                and "Playlist" in ast.unparse(handler.type)):
            rendered = ast.unparse(handler)
            assert "return" in rendered
            for forbidden in ("audio_file", "playlist[0]", "continue", "pass"):
                assert forbidden not in rendered, forbidden
    assert "playlist_tracks = ()" in impl


# ===========================================================================
# 8. REPEAT PLAYLIST REPEATS THE WHOLE ORDERED PLAYLIST
# ===========================================================================


def test_the_master_becomes_local_audio_path_before_the_analysis():
    """The architecture in one assertion: everything after this line is unaware a playlist
    happened, which is why no stage, no planner, no cache and no renderer needed changing."""
    body = _body(_GUI, "_process_video_impl")
    build = body.index("music_playlist_builder.build_playlist_master(")
    rebind = body.index("local_audio_path = prepared_playlist.path")
    analyse = body.index("analyze_beats_auto(")
    assert build < rebind < analyse


def test_the_analysis_is_still_handed_local_audio_path_and_knows_nothing_about_a_playlist():
    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "analyze_beats_auto":
            rendered = ast.unparse(node)
            assert ast.unparse(node.args[0]) == "local_audio_path"
            for forbidden in ("playlist", "music_playlist", "prepared_playlist"):
                assert forbidden not in rendered, rendered
            break
    else:
        raise AssertionError("analyze_beats_auto is not called")


def test_r10_is_handed_the_playlist_master_unchanged_which_is_how_the_whole_playlist_repeats():
    """R10's cycle is whatever audio file it was handed. Because the playlist has already become
    one file by this point, `build_repeated_music` measures the COMPLETE soundtrack as its cycle --
    so Repeat Playlist repeats the whole ordered playlist rather than each song, by construction."""
    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and "build_repeated_music" in ast.unparse(node.func):
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            assert kwargs["music_path"] == "local_audio_path"
            break
    else:
        raise AssertionError("build_repeated_music is not called")
    body = _body(_GUI, "_process_video_impl")
    assert body.index("local_audio_path = prepared_playlist.path") < \
        body.index("music_repeater.build_repeated_music(")


def test_neither_r10_module_LEARNED_about_a_playlist():
    """The two R10 modules contain no playlist logic, and must not: their contract is "repeat the
    audio file you were given", which is exactly what makes Repeat Playlist fall out of them
    unchanged.

    Asserted over the **executable** source, docstrings stripped, because `music_repeat`'s own prose
    legitimately explains that its caller may label the line *Repeat Playlist* -- and an explanation
    of what a caller does must never read as the module doing it. `music_repeater` is unchanged
    outright, docstrings included.
    """
    repeat_code = _executable_source(os.path.join(_SRC, "beatsync_fork", "music_repeat.py"))
    for forbidden in ("playlist", "Playlist", "music_source", "music_playlist",
                      "fork_playlist"):
        assert forbidden not in repeat_code, f"music_repeat.py DOES {forbidden}"
    with open(_REPEATER, "r", encoding="utf-8") as handle:
        repeater = handle.read()
    for forbidden in ("playlist", "Playlist", "music_source"):
        assert forbidden not in repeater, f"music_repeater.py mentions {forbidden}"


def test_the_repeat_line_names_the_control_the_operator_actually_USED():
    """One formatter, one bus record, one arithmetic -- and a label that tells the truth about which
    control produced it. The label is R10's only R16 change and it is a plain string parameter: the
    module still cannot tell a playlist from a single track, which is the point."""
    assert fork_music_repeat.DEFAULT_SUMMARY_LABEL == "REPEAT MUSIC"
    bus = {fork_music_repeat.BUS_KEY: {"repeat": 2, "cycle_seconds": 18.0}}
    assert fork_music_repeat.summary_line(bus) == "🔁 REPEAT MUSIC: 2x - 00:18 -> 00:36"
    assert fork_music_repeat.summary_line(bus, "REPEAT PLAYLIST") == \
        "🔁 REPEAT PLAYLIST: 2x - 00:18 -> 00:36"
    # an unusable label falls back rather than printing an empty name
    for junk in (None, "", "   ", 17, object()):
        assert "REPEAT MUSIC" in fork_music_repeat.summary_line(bus, junk)
    # a single-track render's line is character for character what it was
    assert "'REPEAT PLAYLIST' if playlist_master_path else 'REPEAT MUSIC'" in \
        _body(_GUI, "_process_video_impl")


def test_the_repeat_estimate_states_that_the_WHOLE_playlist_repeats():
    """"Repeat 3x" over five songs has two readings and only one of them is this feature, so the
    read-out says which."""
    playlist = P.playlist_from_entries([
        _entry("C:/m/a.mp3", duration=120.0), _entry("C:/m/b.mp3", duration=60.0)])
    text = P.repeat_estimate_markdown(playlist, 3)
    assert "whole playlist" in text
    assert "not each song" in text
    assert "09:00" in text                         # 3 x (2:00 + 1:00)
    assert "2 tracks" in text


def test_the_repeat_normaliser_has_exactly_one_definition():
    """Repeat Playlist is the same control reading the same 1..30 range, so the normaliser is
    imported rather than restated."""
    pure = _executable_source(_PURE)
    assert "normalize_repeat(count)" in pure
    assert "def normalize_repeat" not in pure
    assert P.repeat_estimate_markdown(P.EMPTY, 99).startswith(
        f"**Repeat Playlist: {fork_music_repeat.MAX_REPEAT}x**")


# ===========================================================================
# 9. ISOLATION — IDENTITY, CACHE, CREATIVE STATE, SOURCE STATE
# ===========================================================================


def test_no_playlist_name_reaches_stage_5_or_its_cache():
    """Stage 5 keys on the VIDEO sources; the music reaches it only as the one audio file it has
    always received. Asserted as absence in the files that own identity."""
    for relative in (("video_analysis.py",), ("auto_mode", "stage5_qwen_scene_worker.py"),
                     ("auto_mode", "__init__.py"), ("auto_mode", "stage4_select.py"),
                     ("auto_mode", "stage3_sections.py"),
                     ("auto_mode", "stage6_av_planner.py"),
                     ("beatsync_fork", "stage_cache.py"),
                     ("beatsync_fork", "analysis_cache.py"),
                     ("beatsync_fork", "library_prep.py")):
        path = os.path.join(_SRC, *relative)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("music_playlist", "PlaylistMaster", "playlist_master",
                          "music_source"):
            assert forbidden not in source, f"{relative[-1]} mentions {forbidden}"


def test_the_cache_contract_constants_are_untouched():
    with open(os.path.join(_SRC, "video_analysis.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_nothing_about_a_playlist_reaches_the_variant_lab_the_director_or_the_presets():
    """Creative variation must never modify the playlist's order or length: the soundtrack is the
    operator's decision, not a value to randomise."""
    for module in ("creative.py", "creative_recipe.py", "variant_lab.py", "variant_batch.py",
                   "presets.py", "director.py", "freestyle.py", "render_batch.py",
                   "variation.py"):
        path = os.path.join(_SRC, "beatsync_fork", module)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("playlist", "Playlist", "music_source"):
            assert forbidden not in source, f"{module} mentions {forbidden}"


def test_the_renderer_itself_needed_no_playlist_awareness():
    """`create_music_video` renders whatever audio file and whatever beat array it is handed, which
    is exactly why R16 needed no second render engine and no change to the frame-lock contract."""
    with open(_PROCESSOR, "r", encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("playlist", "Playlist", "music_source"):
        assert forbidden not in source, f"video_processor.py mentions {forbidden}"


def test_the_widgets_are_absent_from_source_and_preparation_wiring():
    """Editing the soundtrack must not clear a confirmed VIDEO source set or start a scan."""
    tree = _tree(_GUI)
    for listname in ("source_outputs", "prep_outputs"):
        assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                          and getattr(node.targets[0], "id", None) == listname)
        names = {getattr(element, "id", None) for element in assignment.value.elts}
        for widget in ("music_source", "playlist_state", "playlist_summary",
                       "playlist_selected", "playlist_notice", "repeat_music_heading"):
            assert widget not in names, f"{widget} in {listname}"

    for handler in ("_on_source_mode_change", "_on_folder_path_change", "_on_recursive_change",
                    "_on_scan_click", "_on_browser_files_change", "_on_confirm_click",
                    "_on_prep_folder_change"):
        rendered = ast.unparse(_func(_GUI, handler))
        for forbidden in ("music_source", "playlist"):
            assert forbidden not in rendered, f"{handler} mentions {forbidden}"
    # ...and the pure gate itself, which lives in the fork package.
    session = os.path.join(_SRC, "beatsync_fork", "input_session.py")
    with open(session, "r", encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("music_source", "playlist", "Playlist"):
        assert forbidden not in source, f"input_session.py mentions {forbidden}"


def test_the_live_declaration_knows_nothing_about_a_playlist():
    core = _body(_GUI, "_process_video_guarded_unlocked")
    declaration = core[core.index("live_declaration("):]
    assert "playlist" not in declaration.split(")")[0]
    assert "music_source" not in declaration.split(")")[0]


def test_the_repeat_slider_still_has_no_writer_anywhere_in_the_app():
    """R16 renames the control on screen through a SEPARATE heading Markdown, precisely so the
    slider stays a zero-writer widget. Writing `gr.update(label=...)` to it would have been the easy
    way and would have broken this."""
    tree = _tree(_GUI)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("click", "change", "input", "select", "submit")):
            outputs = next((kw.value for kw in node.keywords if kw.arg == "outputs"), None)
            if outputs is None:
                continue
            names = {getattr(inner, "id", None) for inner in ast.walk(outputs)
                     if isinstance(inner, ast.Name)}
            assert "repeat_music" not in names, (
                f"{getattr(node.func.value, 'id', '?')}.{node.func.attr} writes repeat_music")


def test_the_slider_keeps_its_label_constant_for_assistive_technology():
    """`show_label=False` hides it visually; the constant is still passed, exactly as the Video
    Source radio does it, so screen readers and every test that reads it are unaffected."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("repeat_music = gr.Slider(")
    declaration = source[start:source.index(")\n", start)]
    assert "label=LABEL_REPEAT_MUSIC" in declaration
    assert "show_label=False" in declaration
    assert "elem_id='repeat-music-slider'" in declaration


def test_the_audio_input_component_is_otherwise_untouched_by_the_wrapping():
    """It was wrapped in a `gr.Group` so the two modes can be shown one at a time. Everything a
    handler depends on is unchanged, so the wrapping cannot have moved a value."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("audio_input = gr.File(")
    declaration = source[start:source.index(")\n", start)]
    assert "type='filepath'" in declaration
    assert "elem_id='audio-file-input'" in declaration
    assert "file_types=['.mp3', '.wav', '.flac']" in declaration
    assert "height=" in declaration
    assert "audio_input = gr.Textbox(" not in source
    # and it is never emptied when hidden: switching modes must not discard the operator's track
    assert "audio_input" not in _outputs_names(
        _GUI, _click_kwargs(_GUI, "playlist_add_btn"))


def test_switching_mode_discards_neither_the_track_nor_the_queue():
    change = next(node for node in ast.walk(_tree(_GUI))
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "change"
                  and getattr(node.func.value, "id", None) == "music_source")
    kwargs = {kw.arg: kw.value for kw in change.keywords}
    outputs = [getattr(e, "id", None) for e in kwargs["outputs"].elts]
    assert outputs == ["single_track_group", "playlist_group", "repeat_music_heading",
                       "repeat_music_estimate", "playlist_summary"]
    assert "audio_input" not in outputs
    assert "playlist_state" not in outputs
    assert "concurrency_id" not in kwargs


# ===========================================================================
# 10. THE WIRING CONTRACT
# ===========================================================================


@pytest.mark.parametrize("button,handler", [
    ("process_btn", "process_video_guarded"),
    ("render_selected_variants_btn", "render_selected_variants_guarded"),
])
def test_both_render_events_pass_the_playlist_at_the_same_index_as_the_signature(button, handler):
    """Gradio matches positionally, so the click list and the signature are one contract."""
    inputs = _click_inputs(_GUI, button)
    args = submitted_parameters(_func(_GUI, handler))
    assert inputs[-2:] == ["music_source", "playlist_state"], (button, inputs[-3:])
    assert args[-2:] == ["music_source", "playlist_state"], (handler, args[-3:])
    for name in ("music_source", "playlist_state"):
        assert inputs.index(name) == args.index(name), (button, handler, name)
    # appended, never inserted: R10's count keeps its own index, immediately before the pair
    assert inputs[-3] == "repeat_music"
    assert args[-3] == "repeat_music"
    all_args = [arg.arg for arg in _func(_GUI, handler).args.args]
    assert all_args == args + ["request"], (handler, all_args)


@pytest.mark.parametrize("handler", [
    "_process_video_guarded_unlocked", "process_video_guarded",
    "render_selected_variants_guarded",
])
def test_the_exec_safe_defaults_are_literals_that_cannot_drift(handler):
    """All three handlers are AST-extracted and exec'd against namespaces where the fork modules
    are stubs, so a default that dereferenced `fork_playlist` raises at def-time -- the measured
    trap `repeat_music`'s integer literal records."""
    node = _func(_GUI, handler)
    args = [arg.arg for arg in node.args.args]
    for name, expected in (("music_source", P.MODE_SINGLE), ("playlist_state", ())):
        index = args.index(name)
        default = node.args.defaults[len(node.args.defaults) - (len(args) - index)]
        assert isinstance(default, (ast.Constant, ast.Tuple)), (handler, ast.unparse(default))
        value = ast.literal_eval(default)
        assert value == expected, (handler, name, value)


def test_the_core_threads_the_frozen_tuple_without_reinterpretation():
    for handler in ("process_video", "_process_video_guarded_unlocked"):
        assert "playlist=playlist" in _body(_GUI, handler), handler
    assert "playlist=playlist" in _body(_GUI, "process_video")


def test_the_batch_freezes_the_soundtrack_from_its_own_arguments_never_from_a_recipe():
    """Comparing candidates is only meaningful when the thing that differs between them is their
    creative settings -- so every selected candidate renders the same playlist in the same order."""
    body = ast.unparse(_func(_GUI, "render_selected_variants_guarded"))
    assert "music_source=music_source" in body
    assert "playlist_state=playlist_state" in body
    for forbidden in ("recipe.playlist", "audio.playlist", "candidate.playlist",
                      "recipe.music_source", "audio.music_source"):
        assert forbidden not in body, forbidden


def test_the_batch_passes_the_same_frozen_values_to_every_candidate():
    """Structurally: the two arguments are read from the handler's own parameters inside the
    candidate loop, so there is nothing per-candidate about them."""
    node = _func(_GUI, "render_selected_variants_guarded")
    loops = [n for n in ast.walk(node) if isinstance(n, ast.For)]
    assert loops
    candidate_loop = next(n for n in loops
                          if "batch_request.candidates" in ast.unparse(n.iter))
    rendered = ast.unparse(candidate_loop)
    assert "music_source=music_source" in rendered
    assert "playlist_state=playlist_state" in rendered


def test_the_read_out_handler_owns_both_read_outs_and_writes_nothing_else():
    tree = _tree(_GUI)
    registrations = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "change"
                     and any(kw.arg == "fn"
                             and ast.unparse(kw.value) == "_on_repeat_music_change"
                             for kw in node.keywords)]
    assert len(registrations) == 2, "expected the slider and the audio file to drive the read-out"
    owners = sorted(getattr(node.func.value, "id", "?") for node in registrations)
    assert owners == ["audio_input", "repeat_music"]
    for node in registrations:
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        assert [getattr(e, "id", None) for e in kwargs["inputs"].elts] == \
            ["audio_input", "repeat_music", "music_source", "playlist_state"]
        assert [getattr(e, "id", None) for e in kwargs["outputs"].elts] == \
            ["repeat_music_estimate", "playlist_summary"]
        assert "concurrency_id" not in kwargs
        assert ast.unparse(kwargs["concurrency_limit"]) == "None"


def test_the_read_out_handler_still_probes_rather_than_guessing_in_single_track_mode():
    body = _code(_GUI, "_on_repeat_music_change")
    assert "music_repeater.probe_duration(" in body
    assert "fork_music_repeat.estimate_markdown(" in body
    assert "get_video_duration" not in body
    assert "duration = None" in body
    # ...and probes NOTHING in playlist mode: every queued length was measured when it was added.
    playlist_branch = body[:body.index("duration = None")]
    assert "fork_playlist.is_playlist(music_source)" in playlist_branch
    assert "probe_duration" not in playlist_branch


@pytest.mark.parametrize("button,handler", [
    ("playlist_add_btn", "_on_playlist_add"),
    ("playlist_remove_btn", "_on_playlist_remove"),
    ("playlist_up_btn", "_on_playlist_move_up"),
    ("playlist_down_btn", "_on_playlist_move_down"),
    ("playlist_shuffle_btn", "_on_playlist_shuffle"),
    ("playlist_sort_btn", "_on_playlist_sort"),
    ("playlist_clear_btn", "_on_playlist_clear"),
])
def test_every_playlist_button_writes_exactly_the_one_projection(button, handler):
    kwargs = _click_kwargs(_GUI, button)
    assert getattr(kwargs["fn"], "id", None) == handler
    assert getattr(kwargs["outputs"], "id", None) == "playlist_outputs"
    assert "concurrency_id" not in kwargs
    assert ast.unparse(kwargs["concurrency_limit"]) == "None"
    # Positional alignment with the handler's own signature. Gradio matches by position, so this is
    # one contract; two widget/parameter names differ deliberately and are mapped rather than
    # renamed, exactly as `audio_input -> audio_file` is elsewhere in this repository.
    renames = {"audio_input": "audio_file", "repeat_music": "repeat_value",
               "playlist_selected": "selection"}
    inputs = [renames.get(getattr(e, "id", None), getattr(e, "id", None))
              for e in kwargs["inputs"].elts]
    args = [arg.arg for arg in _func(_GUI, handler).args.args]
    assert inputs == args, (button, inputs, args)


def test_the_projection_list_matches_the_frame_it_is_fed():
    """Gradio matches `outputs` by position, so `playlist_outputs` and `_playlist_frame`'s return
    tuple are one contract."""
    assignment = next(node for node in ast.walk(_tree(_GUI)) if isinstance(node, ast.Assign)
                      and getattr(node.targets[0], "id", None) == "playlist_outputs")
    names = [getattr(e, "id", None) for e in assignment.value.elts]
    assert names == ["playlist_state", "playlist_summary", "playlist_selected",
                     "playlist_notice", "repeat_music_estimate"]
    returns = [n for n in ast.walk(_func(_GUI, "_playlist_frame"))
               if isinstance(n, ast.Return)]
    assert len(returns) == 1
    assert len(returns[0].value.elts) == len(names)


def test_no_playlist_handler_writes_an_execution_widget_other_than_the_queue():
    forbidden = {"repeat_music", "audio_input", "variation_seed", "cut_density", "micro_cuts",
                 "semantic_emphasis", "energy_response", "motion_bias", "source_diversity",
                 "creative_preset", "music_under_voice", "sfx_amount", "sfx_level",
                 "voice_files", "sfx_folder", "sfx_roles", "voice_overflow",
                 "output_filename", "processing_mode", "output_profile",
                 "source_mode", "source_folder", "source_recursive", "video_input",
                 "source_state", "prep_state", "status_output", "video_output"}
    tree = _tree(_GUI)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("click", "change")):
            continue
        fn = next((kw.value for kw in node.keywords if kw.arg == "fn"), None)
        if fn is None or not ast.unparse(fn).startswith(
                ("_on_playlist_", "_on_music_source_change")):
            continue
        # `playlist_outputs` is a named list; `_outputs_names` resolves it rather than letting the
        # check go vacuous -- the exact hazard `expanded_names_in` exists for elsewhere.
        names = _outputs_names(_GUI, {kw.arg: kw.value for kw in node.keywords})
        assert "playlist_state" in names or "single_track_group" in names, ast.unparse(fn)
        assert not (names & forbidden), (ast.unparse(fn), names & forbidden)


def test_the_gui_gained_no_subprocess_call_site():
    """Two suites pin `gui.py`'s `subprocess.run` sites at two (the delivery preview and the
    Director). The playlist's native dialog is delegated to the fork package, exactly as every other
    folder picker in this app is."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("subprocess.run(") == 2
    assert "fork_folders.pick_files(" in _body(_GUI, "_on_playlist_add")


def test_the_multi_select_picker_can_only_ever_add():
    """Cancel, timeout and any failure to show the dialog all leave the queue exactly as it was."""
    status, chosen = fork_folders.pick_files(
        None, runner=lambda argv, env, timeout: (fork_folders.TIMED_OUT, ""))
    assert (status, chosen) == (fork_folders.TIMED_OUT, ())
    status, chosen = fork_folders.pick_files(
        None, runner=lambda argv, env, timeout: (fork_folders.PICKED, "CANCELLED"))
    assert (status, chosen) == (fork_folders.CANCELLED, ())
    status, chosen = fork_folders.pick_files(
        None, runner=lambda argv, env, timeout: (fork_folders.PICKED, "noise on stdout"))
    assert (status, chosen) == (fork_folders.CANCELLED, ())


def test_the_multi_select_picker_returns_every_chosen_file_in_order():
    """Unlike the single-file parser, which answers the LAST marked line, this collects all of
    them: a multi-select dialog legitimately produces several."""
    output = "PICKED:C:\\m\\b.mp3\nPICKED:C:\\m\\a.mp3\n"
    status, chosen = fork_folders.parse_multi_picker_output(output)
    assert status == fork_folders.PICKED
    assert chosen == ("C:\\m\\b.mp3", "C:\\m\\a.mp3")
    # the single-file parser is untouched and still answers one path
    assert fork_folders.parse_picker_output(output) == (fork_folders.PICKED, "C:\\m\\a.mp3")


def test_the_multi_select_argv_is_the_same_hardened_dialog():
    argv = fork_folders.build_files_picker_command()
    assert "-STA" in argv and "-NoProfile" in argv and "-NonInteractive" in argv
    script = argv[-1]
    assert "Multiselect = $true" in script
    assert "$env:BEATSYNC_PICK_START" in script       # never on the command line
    assert "$env:BEATSYNC_PICK_FILTER" in script


def test_the_dialog_filter_is_a_convenience_and_never_a_gate():
    text = fork_folders.file_filter("Music", P.SUPPORTED_TRACK_EXTENSIONS)
    assert text.endswith("All files|*.*")
    assert "*.mp3" in text and "*.flac" in text


# ===========================================================================
# 11. THE READ-OUTS AND THE RENDER REPORT
# ===========================================================================


def test_the_queue_table_states_the_count_and_the_total():
    playlist = P.playlist_from_entries([
        _entry("C:/m/a.mp3", duration=90.0), _entry("C:/m/b.mp3", duration=30.0)])
    text = P.summary_markdown(playlist)
    assert "a.mp3" in text and "b.mp3" in text
    assert "01:30" in text and "00:30" in text
    assert "2 tracks" in text
    assert "02:00 total soundtrack" in text


def test_an_empty_queue_says_so_rather_than_claiming_a_zero_length_soundtrack():
    assert "empty" in P.summary_markdown(P.EMPTY).lower()
    assert "00:00" not in P.summary_markdown(P.EMPTY)


def test_a_filename_containing_a_pipe_cannot_break_the_table():
    text = P.summary_markdown([_entry("C:/m/a|b.mp3", duration=1.0)])
    assert r"a\|b.mp3" in text


def test_a_single_track_queue_is_described_in_the_singular():
    assert "1 track ·" in P.summary_markdown([_entry("C:/m/a.mp3", duration=1.0)])
    assert "1 track" in P.repeat_estimate_markdown([_entry("C:/m/a.mp3", duration=1.0)], 1)


def test_the_selector_values_are_positions_so_a_repeated_track_stays_unambiguous():
    labels = P.choice_labels([_entry("C:/m/a.mp3", duration=5.0),
                              _entry("C:/m/a.mp3", duration=5.0)])
    assert [value for _label, value in labels] == [0, 1]
    assert labels[0][0].startswith("1. a.mp3")
    assert labels[1][0].startswith("2. a.mp3")


def test_the_long_render_warning_names_both_masters():
    """A playlist render can hold the soundtrack master AND the repeated master at once, so the
    disk warning says so rather than understating it."""
    long_playlist = [_entry(f"C:/m/{i}.mp3", duration=600.0) for i in range(3)]
    text = P.repeat_estimate_markdown(long_playlist, 4)
    assert "temporary disk space" in text
    assert text.count("GB") + text.count("MB") >= 2


def test_the_success_panel_line_reads_only_the_bus():
    """The panel must report what the render DID. Nothing is recomputed, re-probed or re-derived."""
    record = {
        "tracks": [
            {"index": 0, "name": "a.mp3", "start_seconds": 0.0, "duration_seconds": 90.0},
            {"index": 1, "name": "b.mp3", "start_seconds": 90.0, "duration_seconds": 30.0},
        ],
        "total_seconds": 120.0,
    }
    line = P.summary_line({P.BUS_KEY: record})
    assert "MUSIC PLAYLIST: 2 tracks" in line
    assert "02:00 soundtrack" in line
    assert "00:00  a.mp3" in line
    assert "01:30  b.mp3" in line


@pytest.mark.parametrize("bus", [
    None, {}, "nonsense", 17, {P.BUS_KEY: None}, {P.BUS_KEY: {}},
    {P.BUS_KEY: {"tracks": []}}, {P.BUS_KEY: {"tracks": "abc"}},
])
def test_the_success_panel_line_is_empty_for_every_shape_it_does_not_recognise(bus):
    """Including a pre-R16 bus, which is what keeps a Single Track panel character for character
    what it was."""
    assert P.summary_line(bus) == ""


def test_the_panel_line_is_behind_the_playlist_guard_and_comes_before_the_repeat_line():
    body = _body(_GUI, "_process_video_impl")
    assert "if playlist_master_path:" in body
    assert body.index("fork_playlist.summary_line(beat_info)") < \
        body.index("fork_music_repeat.summary_line(")


def test_the_bus_record_is_written_after_the_repeat_extension_so_it_survives_it():
    """`extend_beat_info` returns NEW containers, so a record written before it would be dropped."""
    body = _body(_GUI, "_process_video_impl")
    assert body.index("fork_music_repeat.extend_beat_info(") < \
        body.index("beat_info[fork_playlist.BUS_KEY]")
    assert body.index("beat_info[fork_playlist.BUS_KEY]") < \
        body.index("beat_times = beat_info.get('times', selected_beats)")


def test_the_bus_record_is_plain_primitives_and_carries_no_repeat_count():
    """One fact with two independently-formatted views is how a panel starts disagreeing with
    itself: the repeat is R10's own record."""
    rendered = _code(_BUILDER, "bus_record")
    for forbidden in ("repeat", "music_repeat"):
        assert forbidden not in rendered, forbidden
    assert "as_record()" in rendered


def test_the_pure_module_has_exactly_one_reader_of_its_bus_key():
    pure = _executable_source(_PURE)
    assert pure.count("beat_info.get(BUS_KEY)") == 1
    assert "def bus_record" in pure
    assert "bus_record(beat_info)" in _body(_PURE, "summary_line")


# ===========================================================================
# 12. THE PURE MODULE'S OWN DISCIPLINE
# ===========================================================================


def test_the_pure_module_imports_nothing_but_the_stdlib_and_two_siblings():
    """`beatsync_fork` is stdlib-only by rule. The two siblings are reused so there is a single
    clock formatter, a single ordering key and a single repeat normaliser rather than duplicates
    that can disagree. (The package-wide guard in `test_no_runtime_dependency.py` enforces the rule
    itself; this records the intent.)"""
    modules = set()
    for node in ast.walk(_tree(_PURE)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert modules == {"__future__", "os", "random", "dataclasses", "typing",
                       "beatsync_fork.audio_mix", "beatsync_fork.music_repeat"}, modules


def test_the_pure_module_touches_no_filesystem_and_reads_no_clock():
    """`os` is imported for path STRING arithmetic only -- `basename`, `splitext`, `normcase`. A
    filesystem call here would make the editing operations untestable on a bare interpreter and
    would put a disk read behind every slider nudge."""
    code = _executable_source(_PURE)
    for forbidden in ("subprocess", "open(", "os.stat", "os.path.isfile", "os.path.exists",
                      "os.listdir", "os.scandir", "os.walk", "os.makedirs", "os.remove",
                      "shutil", "numpy", "time.", "Popen", "unlink", "datetime"):
        assert forbidden not in code, forbidden
    allowed = {"os.path.basename", "os.path.splitext", "os.path.normcase"}
    for node in ast.walk(_tree(_PURE)):
        if not isinstance(node, ast.Call):
            continue
        dotted = ast.unparse(node.func)
        # Only the calls whose callee is literally an `os.…` attribute chain: a `.lower()` applied
        # to the RESULT of one is not itself an `os` call, and treating it as one would make this
        # assertion trip over ordinary string arithmetic.
        if not (dotted.startswith("os.") and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"):
            continue
        assert dotted in allowed, ast.unparse(node)


def test_every_pure_entry_point_is_total():
    """They all run on live widget values or on `gr.State`, so none of them may raise mid-render."""
    rubbish = [None, "", 0, -1, 1.5, float("nan"), float("inf"), True, False, object(),
               [], (), {}, "abc", b"bytes"]
    for junk in rubbish:
        P.normalize_mode(junk)
        P.is_playlist(junk)
        P.has_supported_extension(junk)
        P.playlist_from_entries(junk)
        P.entries_of(junk)
        P.freeze_entries(junk, junk)
        P.add_tracks(junk, junk)
        P.remove_tracks(junk, junk)
        P.move_up(junk, junk)
        P.move_down(junk, junk)
        P.shuffle(junk, junk)
        P.sort_by_name(junk)
        P.summary_markdown(junk)
        P.repeat_estimate_markdown(junk, junk)
        P.choice_labels(junk)
        P.summary_line(junk)
        P.bus_record(junk)
        P.master_bytes(junk)
        P.format_bytes(junk)
        P.parse_wav_header(junk)


def test_the_records_are_deepcopy_safe_because_gradio_deep_copies_state():
    import copy
    playlist = P.playlist_from_entries([_entry("C:/m/a.mp3")])
    assert copy.deepcopy(playlist) == playlist
    assert copy.deepcopy(playlist.tracks[0]) == playlist.tracks[0]


def test_the_canonical_format_matches_what_every_other_master_in_the_app_uses():
    """48 kHz / stereo / `pcm_s24le`, so neither the mixdown nor final assembly has any resampling
    or requantisation to do, and `music_repeater` can stream-copy this file's cycle verbatim."""
    assert P.MASTER_BYTES_PER_SECOND == 48_000 * 2 * 3
    assert P.MASTER_BYTES_PER_SECOND == fork_music_repeat.TEMP_AUDIO_BYTES_PER_SECOND
    builder = _executable_source(_BUILDER)
    assert "TRACK_SAMPLE_RATE = 48000" in builder or "TRACK_SAMPLE_RATE = 48_000" in builder
    assert "TRACK_CODEC = 'pcm_s24le'" in builder
    assert "TRACK_CHANNELS = 2" in builder


# ===========================================================================
# 13. NOTHING NEW MAY ANNOUNCE A JOB
# ===========================================================================


def test_the_playlist_adds_no_notification_producer():
    """Exactly one truthful terminal notification per top-level render job. The playlist is part of
    a render, not a job of its own, so it publishes nothing."""
    for handler in ("_on_playlist_add", "_on_playlist_remove", "_on_playlist_move_up",
                    "_on_playlist_move_down", "_on_playlist_shuffle", "_on_playlist_sort",
                    "_on_playlist_clear", "_on_music_source_change", "_playlist_frame",
                    "_playlist_probe_chosen", "_on_repeat_music_change"):
        rendered = ast.unparse(_func(_GUI, handler))
        for forbidden in ("_notify_publish", "_notify_render", "_notify_drain", "gr.Request"):
            assert forbidden not in rendered, f"{handler} mentions {forbidden}"
    impl = ast.unparse(_func(_GUI, "_process_video_impl"))
    assert impl.count("_notify_") == 0


def test_no_playlist_registration_joins_the_render_or_cancel_lane():
    """None of them is a render, and queuing one behind a render would make it look dead."""
    for node in ast.walk(_tree(_GUI)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("click", "change")):
            continue
        fn = next((kw.value for kw in node.keywords if kw.arg == "fn"), None)
        if fn is None or not ast.unparse(fn).startswith(
                ("_on_playlist_", "_on_music_source_change")):
            continue
        kwargs = {kw.arg for kw in node.keywords}
        assert "concurrency_id" not in kwargs, ast.unparse(fn)


def test_the_playlist_takes_no_render_mutex_and_starts_no_lifecycle():
    """A queue edit is not a render: it holds no lock, installs no lifecycle and clears no slot."""
    for handler in ("_on_playlist_add", "_on_playlist_remove", "_on_playlist_move_up",
                    "_on_playlist_move_down", "_on_playlist_shuffle", "_on_playlist_sort",
                    "_on_playlist_clear", "_on_music_source_change", "_playlist_frame"):
        rendered = ast.unparse(_func(_GUI, handler))
        for forbidden in ("_RENDER_LOCK", "RenderLifecycle", "_install_active_render",
                          "process_video", "analyze_beats_auto", "create_music_video",
                          "resolve_for_render"):
            assert forbidden not in rendered, f"{handler} mentions {forbidden}"
