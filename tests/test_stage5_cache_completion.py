"""Stage 5 cache completion + writer behaviour (D1).

``video_analysis.py`` cannot be imported on a bare interpreter (it needs cv2/numpy/librosa and
mutates PATH), but the cache primitives D1 introduced are stdlib-only. So instead of asserting
*about* them with ``ast``, this suite lifts the real function definitions out of the production file
by their ``ast`` source ranges and executes them in a stdlib-only namespace. The bodies under test
are therefore provably the production bodies, and the assertions are behavioural rather than textual.

The orchestration wiring that *calls* these primitives needs the whole runtime and is pinned
structurally in ``test_stage5_cache_durability.py``.

Nothing here touches the real runtime cache: every path is inside pytest's ``tmp_path``.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from typing import Any, Dict

import pytest

_VIDEO_ANALYSIS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "video_analysis.py")

# The exact set of cache primitives D1 owns. All stdlib-only by construction.
_FUNCS = ("_safe_name", "_hash_text", "_same_source", "_cache_entry_is_complete",
          "_load_cache", "_save_cache", "_checkpoint_cache")
_CONSTS = ("ANALYSIS_VERSION", "_QWEN_COMPLETED_KEY")


def _load_primitives():
    """Exec the production definitions verbatim in an isolated stdlib-only namespace."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile,
        "Any": Any, "Dict": Dict, "__builtins__": __builtins__,
    }
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _CONSTS:
                    namespace[target.id] = ast.literal_eval(node.value)

    missing = [name for name in _FUNCS if name not in found]
    assert not missing, f"cache primitives missing from video_analysis.py: {missing}"
    for name in _CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"

    for name in _FUNCS:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    return namespace


@pytest.fixture(scope="module")
def cache():
    return _load_primitives()


def _entry(av, *, video_file, candidates=None, ai_enabled=True, ai_deferred=False, **extra):
    entry = {
        "analysis_version": av,
        "video_file": video_file,
        "source_name": os.path.basename(video_file),
        "duration": 30.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [1.0, 5.0],
        "candidates": [{"id": "c0", "start": 0.0, "end": 2.0, "action_score": 0.5,
                        "editorial_score": 0.5}] if candidates is None else candidates,
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0},
        "ai_enabled": ai_enabled,
        "ai_deferred": ai_deferred,
    }
    entry["candidate_count"] = len(entry["candidates"])
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# the single completion rule
# ---------------------------------------------------------------------------


def test_deterministic_result_is_reusable_when_ai_is_not_required(cache):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4", ai_enabled=False)
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is True


def test_ai_run_requires_genuine_ai_completion_when_candidates_exist(cache):
    av = cache["ANALYSIS_VERSION"]
    complete = _entry(av, video_file=r"C:\src\a.mp4", ai_enabled=True)
    deterministic_only = _entry(av, video_file=r"C:\src\a.mp4", ai_enabled=False)

    assert cache["_cache_entry_is_complete"](complete, require_ai=True) is True
    assert cache["_cache_entry_is_complete"](deterministic_only, require_ai=True) is False


def test_candidate_less_result_is_complete_without_faking_ai_enabled(cache):
    """There is nothing for Qwen to annotate, so the source is finished.

    The pre-D1 behaviour re-analysed such a source on every single run, because the only way to be
    accepted under ``require_ai`` was ``ai_enabled=True`` - which would have been a lie. The
    completion rule carries that knowledge instead.
    """
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\empty.mp4",
                   candidates=[], ai_enabled=False)

    assert entry["ai_enabled"] is False, "the flag must stay honest"
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_a_deferred_entry_is_never_complete_whatever_else_it_claims(cache):
    """The trap any checkpointing change falls into: ai_deferred outranks ai_enabled."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4",
                   ai_enabled=True, ai_deferred=True)

    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is False


@pytest.mark.parametrize("mutate, reason", [
    (lambda e: e.update(analysis_version="auto_av_analysis_v7_old"), "stale version"),
    (lambda e: e.pop("analysis_version"), "missing version"),
    (lambda e: e.pop("video_file"), "missing video_file"),
    (lambda e: e.update(video_file=None), "non-string video_file"),
    (lambda e: e.update(video_file=""), "empty video_file"),
    (lambda e: e.pop("candidates"), "missing candidates"),
    (lambda e: e.update(candidates="not-a-list"), "candidates is a str"),
    (lambda e: e.update(candidates={"a": 1}), "candidates is a dict"),
])
def test_malformed_entries_are_rejected(cache, mutate, reason):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4")
    mutate(entry)
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is False, reason


def test_non_dict_payload_is_rejected_without_raising(cache):
    for payload in ([], "text", 3, None, 4.5):
        assert cache["_cache_entry_is_complete"](payload, require_ai=False) is False


def test_unexpected_extra_fields_stay_allowed(cache):
    """Forward compatibility: D1 adds a completion rule, not a closed schema."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4",
                   something_added_later={"nested": [1, 2]}, another=5)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_a_representative_pre_d1_entry_is_still_accepted(cache):
    """Existing cache entries must remain reusable: D1 changes no key and no required field."""
    legacy = {
        "analysis_version": cache["ANALYSIS_VERSION"],
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "duration": 41.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [2.0], "candidate_count": 2,
        "candidates": [{"id": "a", "action_score": 0.4}, {"id": "b", "action_score": 0.6}],
        "analysis_seconds": 4.4,
        "timings": {"total_seconds": 4.4, "qwen_tag_count": 2, "qwen_seconds": 2.3},
        "ai_enabled": True, "ai_deferred": False,
    }
    assert cache["_cache_entry_is_complete"](legacy, require_ai=True) is True
    assert cache["_cache_entry_is_complete"](legacy, require_ai=False) is True


# ---------------------------------------------------------------------------
# source identity of a payload
# ---------------------------------------------------------------------------


def test_same_source_normalises_case_and_relative_paths(cache, tmp_path):
    target = tmp_path / "Clip.mp4"
    target.write_bytes(b"x")
    assert cache["_same_source"](str(target), str(target)) is True
    assert cache["_same_source"](str(target).upper(), str(target)) is True
    assert cache["_same_source"](str(target), str(tmp_path / "other.mp4")) is False


def test_same_source_rejects_missing_or_non_string_values(cache):
    assert cache["_same_source"](None, r"C:\a.mp4") is False
    assert cache["_same_source"]("", r"C:\a.mp4") is False
    assert cache["_same_source"](123, r"C:\a.mp4") is False


def test_loader_rejects_a_payload_describing_a_different_source(cache, tmp_path):
    """A version-correct entry for a foreign video must not be reused for this one."""
    path = str(tmp_path / "entry.json")
    cache["_save_cache"](path, _entry(cache["ANALYSIS_VERSION"],
                                      video_file=r"C:\elsewhere\FOREIGN.mp4"))

    assert cache["_load_cache"](path, require_ai=True) is not None, "no expectation given"
    assert cache["_load_cache"](path, require_ai=True,
                               expected_video_file=r"C:\lib\ours.mp4") is None


def test_loader_round_trips_a_valid_entry(cache, tmp_path):
    source = str(tmp_path / "ours.mp4")
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=source)
    cache["_save_cache"](path, entry)

    loaded = cache["_load_cache"](path, require_ai=True, expected_video_file=source)
    assert loaded is not None
    assert loaded["video_file"] == source
    assert loaded["candidates"] == entry["candidates"]


def test_loader_rejects_deferred_and_malformed_entries_from_disk(cache, tmp_path):
    av = cache["ANALYSIS_VERSION"]
    source = str(tmp_path / "ours.mp4")

    deferred = str(tmp_path / "deferred.json")
    cache["_save_cache"](deferred, _entry(av, video_file=source, ai_enabled=True, ai_deferred=True))
    assert cache["_load_cache"](deferred, require_ai=True, expected_video_file=source) is None

    wrong_type = str(tmp_path / "wrong.json")
    cache["_save_cache"](wrong_type, _entry(av, video_file=source, candidates="not-a-list"))
    assert cache["_load_cache"](wrong_type, require_ai=True, expected_video_file=source) is None


def test_loader_survives_a_corrupt_file_without_raising(cache, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"analysis_version": "au', encoding="utf-8")
    assert cache["_load_cache"](str(path), require_ai=False) is None


def test_loader_returns_none_for_a_missing_file(cache, tmp_path):
    assert cache["_load_cache"](str(tmp_path / "absent.json"), require_ai=False) is None


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_save_uses_a_unique_temp_in_the_target_directory_and_leaves_none_behind(cache, tmp_path):
    """The pre-D1 writer used one shared ``path + '.tmp'``; two writers could then publish each
    other's payload. Unique names remove the shared resource entirely."""
    path = str(tmp_path / "entry.json")
    observed = []

    for index in range(5):
        cache["_save_cache"](path, _entry(cache["ANALYSIS_VERSION"],
                                          video_file=rf"C:\src\{index}.mp4"))
        observed.append(sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")))

    assert observed == [[]] * 5, f"temp files leaked: {observed}"
    assert not (tmp_path / "entry.json.tmp").exists(), "the fixed shared temp name must be gone"
    assert json.loads(open(path, encoding="utf-8").read())["video_file"] == r"C:\src\4.mp4"


def test_save_publishes_atomically_so_a_reader_never_sees_a_partial_final_file(cache, tmp_path):
    """Regression guard for the property os.replace already provided before D1."""
    av = cache["ANALYSIS_VERSION"]
    path = str(tmp_path / "entry.json")
    source = str(tmp_path / "a.mp4")
    cache["_save_cache"](path, _entry(av, video_file=source, writer="OLD"))

    # a temp that was fully written but never replaced (the crash-before-replace boundary)
    blob = json.dumps(_entry(av, video_file=source, writer="NEW"), indent=2)
    fd, tmp = tempfile.mkstemp(prefix="entry.json.", suffix=".tmp", dir=str(tmp_path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(blob)

    still_old = cache["_load_cache"](path, require_ai=False)
    assert still_old is not None and still_old["writer"] == "OLD"

    os.replace(tmp, path)
    now_new = cache["_load_cache"](path, require_ai=False)
    assert now_new is not None and now_new["writer"] == "NEW"


def test_save_failure_is_reported_but_never_raises(cache, tmp_path, capsys):
    """A cache write must not be able to abort a render."""
    unwritable = str(tmp_path / "nope" / "deep")
    os.makedirs(unwritable, exist_ok=True)
    # a directory where the final file should be: os.replace will fail
    os.makedirs(os.path.join(unwritable, "entry.json"), exist_ok=True)

    cache["_save_cache"](os.path.join(unwritable, "entry.json"),
                         _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4"))

    assert "could not write video analysis cache" in capsys.readouterr().out
    leftovers = [n for n in os.listdir(unwritable) if n.endswith(".tmp")]
    assert leftovers == [], f"failed write left a temp behind: {leftovers}"


def test_save_writes_valid_utf8_json_with_non_ascii_content(cache, tmp_path):
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\clip.mp4",
                   source_name="ünïcödé – 素材.mp4")
    cache["_save_cache"](path, entry)

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["source_name"] == "ünïcödé – 素材.mp4"


# ---------------------------------------------------------------------------
# the checkpoint guard
# ---------------------------------------------------------------------------


def test_checkpoint_writes_a_complete_source_immediately(cache, tmp_path):
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"))

    assert cache["_checkpoint_cache"](path, entry, require_ai=True) is True
    assert os.path.exists(path)


@pytest.mark.parametrize("entry_kwargs, why", [
    ({"ai_enabled": False}, "AI required but Qwen did not complete"),
    ({"ai_enabled": True, "ai_deferred": True}, "still deferred"),
    ({"candidates": "not-a-list"}, "malformed payload"),
])
def test_checkpoint_refuses_to_publish_an_incomplete_source(cache, tmp_path, entry_kwargs, why):
    """This is what makes early saving safe: the checkpoint cannot create a record that the
    loader would then accept as AI-complete."""
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"), **entry_kwargs)

    assert cache["_checkpoint_cache"](path, entry, require_ai=True) is False, why
    assert not os.path.exists(path), why


def test_checkpoint_without_a_cache_file_is_a_no_op(cache, tmp_path):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"))
    assert cache["_checkpoint_cache"](None, entry, require_ai=True) is False
    assert cache["_checkpoint_cache"]("", entry, require_ai=True) is False


def test_checkpointed_sources_survive_while_a_later_source_is_abandoned(cache, tmp_path):
    """D1-A/B in behavioural form: the records a run has already finished are on disk, and a
    record abandoned mid-run is not - which is exactly what the pre-D1 terminal-only save lost."""
    av = cache["ANALYSIS_VERSION"]
    finished = []
    for index in (1, 2):
        source = str(tmp_path / f"s{index}.mp4")
        path = str(tmp_path / f"s{index}.json")
        assert cache["_checkpoint_cache"](path, _entry(av, video_file=source), require_ai=True)
        finished.append((source, path))

    # third source never completes -> nothing is written for it
    third = str(tmp_path / "s3.json")
    assert cache["_checkpoint_cache"](
        third, _entry(av, video_file=str(tmp_path / "s3.mp4"), ai_deferred=True),
        require_ai=True) is False

    for source, path in finished:
        assert cache["_load_cache"](path, require_ai=True, expected_video_file=source) is not None
    assert not os.path.exists(third)
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
