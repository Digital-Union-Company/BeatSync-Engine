"""A1: the Stage-5 worker publishes one atomic partial artifact per finished job.

The defect A1 addresses is a property of the worker's *write ordering*, not of a parsing function:
``stage5_qwen_scene_worker.main()`` accumulates every job's semantics in memory and writes its response
JSON once, after the whole loop, so a worker that dies on its last job discards every earlier job's
completed inference.

**A1 is inert**, and several tests below pin that: nothing in the parent reads a partial, the final
response is unchanged, and library preparation is still not resumable. Recovery is A2.

Three techniques, each chosen for what it can actually prove — see `.claude/rules/test-harness.md`:

* ``beatsync_fork.qwen_partials`` is stdlib-only, so it is **imported normally** and tested as ordinary
  code (tier 1).
* ``stage5_qwen_scene_worker.py`` needs ``cv2``/``PIL`` and cannot be imported here, so its real
  ``_publish_job_partial`` / ``_safe_file_token`` / ``_response_prefix`` bodies are **AST-extracted and
  executed** against a synthesised namespace, and ``main``'s loop structure is asserted with ``ast``.
  These are real assertions about real worker source, not mocks.
* Process death is driven with a **real child process** (the ``test_qwen_stream.py`` pattern): a tiny
  fake worker that calls the real ``qwen_partials`` functions and then dies. A mocked write would
  "publish" happily under a non-atomic implementation too.
"""

from __future__ import annotations

import ast
import copy
import glob as globmod
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from beatsync_fork import qwen_partials as qp

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
_WORKER = os.path.join(_SRC, "auto_mode", "stage5_qwen_scene_worker.py")
_PARENT = os.path.join(_SRC, "video_analysis.py")
_MODULE = os.path.join(_SRC, "beatsync_fork", "qwen_partials.py")

_PRE_EXISTING_PROTOCOL_KINDS = {"job_start", "job_progress", "job_end", "worker_state"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _tree(path: str) -> ast.Module:
    return ast.parse(_read(path), filename=path)


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _exec_worker_functions(names, namespace: dict) -> dict:
    """Execute real worker function bodies in a synthesised namespace.

    ``ast.unparse`` rather than ``ast.get_source_segment`` for the reason `test-harness.md` records:
    a decorated or class definition's ``lineno`` points at the keyword, so the source segment can
    silently drop a decorator. ``from __future__ import annotations`` is prepended so the string
    annotations (``"QwenLlamaClient"``, ``Dict``) are never evaluated at definition time.
    """
    tree = _tree(_WORKER)
    sources = ["from __future__ import annotations"]
    for name in names:
        sources.append(ast.unparse(_function(tree, name)))
    exec(compile("\n\n".join(sources), "<worker-extract>", "exec"), namespace)  # noqa: S102
    return namespace


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Drop every docstring, at every nesting level, on a deep copy.

    Load-bearing for every ban below: A1's own docstrings legitimately name ``_qwen_job_completed``,
    ``_emit_progress``, ``CACHE_CONTRACT_VERSION`` and ``<random>.tmp`` precisely in order to say that
    none of them may be *used* here. Prose that states a rule must not be able to satisfy or fail it.

    Two kinds are removed, and the second is why this is not simply
    ``tests/test_media_neutral_semantics.py``'s helper:

    * the leading docstring of every module, class and function — a nested function's survives
      ``ast.unparse`` of its parent, so the walk must be deep;
    * every **other** bare string-expression statement, i.e. the PEP 258 attribute docstrings this
      package writes under module-level constants (``PARTIAL_CONTRACT_VERSION = …`` followed by its
      own ``\"\"\"…\"\"\"``). Those are not the first body element, so a leading-only strip keeps them.

    A bare string expression statement is a no-op in Python, so nothing executable can be lost.
    """
    node = copy.deepcopy(node)
    for inner in ast.walk(node):
        body = getattr(inner, "body", None)
        if not isinstance(body, list) or not body:
            continue
        kept = [
            item for item in body
            if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str))
        ]
        body[:] = kept or [ast.Pass()]
    return ast.fix_missing_locations(node)


def _executable_source(path: str) -> str:
    """A whole module with every docstring removed."""
    return ast.unparse(_strip_docstrings(ast.parse(_read(path))))


def _body_code(node: ast.FunctionDef) -> str:
    """Executable body only — docstrings stripped and the signature excluded, so neither prose nor a
    parameter name can satisfy or fail a check."""
    return "\n".join(ast.unparse(item) for item in _strip_docstrings(node).body)


def _words(text: str) -> set[str]:
    """Identifier words, so ``os.makedirs(directory)`` is never read as a Director reference."""
    return set(re.findall(r"[a-z]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()))


# ---------------------------------------------------------------------------
# A. THE FILENAME CONTRACT
# ---------------------------------------------------------------------------


def test_a_partial_filename_is_the_documented_sibling_shape():
    target = qp.partial_path(r"C:\cache\qwen_batch_response_3f2a9c1b84d7.json", 7, "7")

    assert os.path.basename(target) == "qwen_batch_response_3f2a9c1b84d7_job_00007_7.partial.json"
    # Same directory as the response: the parent chose where artifacts land, and the worker needs no
    # knowledge of VIDEO_ANALYSIS_CACHE_DIR.
    assert os.path.dirname(target) == r"C:\cache"
    assert target.endswith(qp.PARTIAL_SUFFIX)


def test_a_job_index_is_zero_padded_to_five_digits_and_sorts():
    names = [os.path.basename(qp.partial_path("/c/resp.json", index, "j"))
             for index in (1, 2, 10, 100, 12345)]

    assert names[0] == "resp_job_00001_j.partial.json"
    assert names[-1] == "resp_job_12345_j.partial.json"
    # Zero padding exists so a directory listing orders by loop position rather than lexically by digit.
    assert names == sorted(names)


def test_a_duplicate_job_id_with_different_indices_yields_distinct_filenames():
    """The one thing the index in the filename genuinely buys."""
    first = qp.partial_path("/c/resp.json", 1, "7")
    second = qp.partial_path("/c/resp.json", 2, "7")

    assert first != second
    assert os.path.basename(first) == "resp_job_00001_7.partial.json"
    assert os.path.basename(second) == "resp_job_00002_7.partial.json"


def test_a_a_lossy_sanitisation_collision_is_resolved_by_the_payload_not_the_filename():
    """Two different ids CAN sanitise to one token. The filename is a hint; the payload is authority."""
    assert qp.safe_job_id("a/b") == qp.safe_job_id("a:b") == "a_b"
    assert qp.partial_path("/c/resp.json", 1, "a/b") == qp.partial_path("/c/resp.json", 1, "a:b")

    first = qp.build_payload(
        response_path="/c/resp.json", job_index=1, job_total=1, job_id="a/b",
        video_file="/v/a.mp4", semantics={}, timings={})
    second = qp.build_payload(
        response_path="/c/resp.json", job_index=1, job_total=1, job_id="a:b",
        video_file="/v/a.mp4", semantics={}, timings={})

    assert first["job_id"] == "a/b"
    assert second["job_id"] == "a:b"
    assert first["job_id"] != second["job_id"]


@pytest.mark.parametrize("raw, expected", [
    ("7", "7"),
    ("single", "single"),
    ("a b", "a_b"),
    ("a//b", "a_b"),
    ("keep.dots-and_dashes", "keep.dots-and_dashes"),
    ("", "item"),
    ("///", "_"),
])
def test_a_sanitisation_cases(raw, expected):
    assert qp.safe_job_id(raw) == expected


def test_a_sanitisation_matches_the_real_worker_character_for_character():
    """Duplicated algorithm, pinned against the real source rather than trusted.

    ``_safe_file_token`` cannot be imported (the worker needs cv2/PIL), so the real function is
    extracted and executed here. If either copy drifts, a leftover partial stops matching the name a
    future reader would compute for it.
    """
    namespace = _exec_worker_functions(["_safe_file_token"], {"re": re})
    worker_token = namespace["_safe_file_token"]

    for raw in ["7", "single", "", "a b", "a/b", "a:b", "///", "x" * 200,
                "keep.dots-and_dashes", "ünïcødé", "tab\there", "nl\nhere", "123"]:
        assert qp.safe_job_id(raw) == worker_token(raw), raw


def test_a_response_stem_is_path_equivalent_to_the_real_worker_prefix_helper():
    """Pinned against the real ``_response_prefix``, which is how the worker already names its
    ``*_llama_server_ctx*_stdout.log`` siblings.

    The assertion is **path** equivalence, not string equality, and the difference is deliberate:
    ``pathlib`` rewrites separators on Windows (``/c/x`` becomes ``\\c\\x``) while ``os.path.splitext``
    preserves the caller's spelling. Both name the same file, and the invocation id — the stem's
    basename, which is the part that actually becomes identity — is identical either way. In
    production the question never arises: the parent builds the response path with ``os.path.join``.
    """
    namespace = _exec_worker_functions(["_response_prefix"], {"Path": Path})
    worker_prefix = namespace["_response_prefix"]

    for path in [r"C:\cache\qwen_batch_response_abc.json", "/c/resp.json", "/c/resp",
                 "/c/a.b.json", r"C:\cache\resp.JSON"]:
        ours = qp.response_stem(path)
        theirs = str(worker_prefix(path))
        assert os.path.normpath(ours) == os.path.normpath(theirs), path
        assert os.path.basename(ours) == os.path.basename(theirs), path
        assert qp.invocation_id(path) == os.path.basename(theirs), path


def test_a_invocation_id_is_the_response_stem_basename():
    assert qp.invocation_id(r"C:\cache\qwen_batch_response_abc.json") == \
        "qwen_batch_response_abc"
    assert qp.invocation_id("/c/qwen_response_9c4e.json") == "qwen_response_9c4e"


def test_a_discovery_glob_is_anchored_and_excludes_mkstemp_leftovers(tmp_path):
    """The hazard: a worker killed mid-write leaves `<name>.partial.json.<random>.tmp` behind.

    A prefix-only pattern would discover it and a reader would try to parse a half-written file. The
    suffix is terminal and the pattern anchors on it, so it cannot match.
    """
    response = str(tmp_path / "qwen_batch_response_tok.json")
    published = qp.partial_path(response, 1, "1")
    Path(published).write_text("{}", encoding="utf-8")
    # Exactly the shape mkstemp(prefix=basename + ".", suffix=".tmp") leaves behind.
    Path(published + ".q7x1.tmp").write_text("{partial", encoding="utf-8")
    # A different invocation in the same directory must stay invisible.
    other = qp.partial_path(str(tmp_path / "qwen_batch_response_OTHER.json"), 1, "1")
    Path(other).write_text("{}", encoding="utf-8")

    found = globmod.glob(qp.partial_glob(response))

    assert found == [published]


# ---------------------------------------------------------------------------
# B. THE PAYLOAD SCHEMA
# ---------------------------------------------------------------------------


_SEMANTICS = {
    "cand_a": {"action_intensity": 0.5, "emotion": "neutral", "description": "two people talking"},
    "cand_b": {"action_intensity": 0.9, "emotion": "hype", "description": "car chase"},
}
_TIMINGS = {"prefetch_seconds": 1.21, "inference_seconds": 8.44, "frame_count": 2, "tag_count": 2}
_BACKEND = {"model_id": "Qwen3VL-2B-Instruct-Q8_0", "batch_size": 4, "model_load_seconds": 8.07}


def _payload(**overrides):
    kwargs = dict(
        response_path="/c/qwen_batch_response_tok.json",
        job_index=7,
        job_total=100,
        job_id="7",
        video_file=r"J:\New folder\Cuts\clip.mp4",
        semantics=_SEMANTICS,
        timings=_TIMINGS,
        backend=_BACKEND,
    )
    kwargs.update(overrides)
    return qp.build_payload(**kwargs)


def test_b_payload_key_set_is_exactly_the_contract():
    assert set(_payload()) == {
        "partial_contract", "invocation", "job_index", "job_total", "job_id",
        "video_file", "semantics", "timings", "backend",
    }


def test_b_backend_is_optional_and_omitted_rather_than_faked():
    """`None` means unknown. An absent group must not become a fabricated one (T1's rule)."""
    assert "backend" not in _payload(backend=None)


def test_b_payload_values_are_the_declared_ones():
    payload = _payload()

    assert payload["partial_contract"] == "stage5_job_partial_v1" == qp.PARTIAL_CONTRACT_VERSION
    assert payload["invocation"] == "qwen_batch_response_tok"
    assert payload["job_index"] == 7
    assert payload["job_total"] == 100
    assert payload["job_id"] == "7"
    assert payload["video_file"] == r"J:\New folder\Cuts\clip.mp4"


def test_b_semantics_and_timings_are_stored_verbatim():
    """Verbatim is what lets a future reader apply the parent's existing count/telemetry handling."""
    payload = _payload()

    assert payload["semantics"] == _SEMANTICS
    assert payload["timings"] == _TIMINGS
    # Not reshaped, filtered, rounded, summarised or re-keyed.
    assert list(payload["semantics"]) == list(_SEMANTICS)
    assert payload["timings"]["inference_seconds"] == 8.44
    assert payload["timings"]["frame_count"] == 2


def test_b_payload_carries_no_verdict_and_no_requested_candidate_set():
    """The worker emits evidence; the parent judges. A second completion rule is forbidden."""
    serialised = json.dumps(_payload())

    for banned in ("ai_enabled", "ai_deferred", "complete", "completed", "verdict",
                   "requested", "cache_contract", "signature", "analysis_version"):
        assert banned not in serialised, banned


def test_b_an_incomplete_or_zero_tag_job_still_produces_a_well_formed_payload():
    """Filtering by "looks complete" would need the forbidden second rule."""
    payload = _payload(
        semantics={},
        timings={"prefetch_seconds": 0.4, "inference_seconds": 0.0,
                 "frame_count": 0, "tag_count": 0},
    )

    assert payload["semantics"] == {}
    assert payload["timings"]["tag_count"] == 0
    assert set(payload) == set(_payload())


def test_b_payload_round_trips_through_json_unchanged():
    payload = _payload()
    assert json.loads(json.dumps(payload)) == payload


def test_b_job_index_and_total_are_coerced_to_int():
    payload = _payload(job_index="7", job_total="100")
    assert payload["job_index"] == 7 and payload["job_total"] == 100
    assert isinstance(payload["job_index"], int) and isinstance(payload["job_total"], int)


# ---------------------------------------------------------------------------
# C. ATOMIC PUBLICATION
#
# PROCESS_CRASH_ATOMICITY is tested for THIS writer rather than inherited from `_save_cache`'s
# measured D1 evidence. POWER_LOSS_DURABILITY is not claimed and not tested: the temp is fsynced,
# the containing directory is not.
# ---------------------------------------------------------------------------


def test_c_write_publishes_valid_json_and_returns_the_path(tmp_path):
    target = qp.partial_path(str(tmp_path / "resp.json"), 3, "3")

    written = qp.write_partial(target, _payload())

    assert written == target
    assert json.loads(Path(target).read_text(encoding="utf-8")) == _payload()


def test_c_no_temporary_file_survives_a_successful_write(tmp_path):
    target = qp.partial_path(str(tmp_path / "resp.json"), 3, "3")

    qp.write_partial(target, _payload())

    assert [p.name for p in tmp_path.iterdir()] == [os.path.basename(target)]


def test_c_the_target_is_never_opened_for_writing(tmp_path):
    """The structural half of the atomicity guarantee.

    A reader can only ever see the old entry or the new one because the target is created exclusively
    by ``os.replace`` of a fully written temp — this function never opens it for writing at all.
    """
    writer = _function(_tree(_MODULE), "write_partial")
    dumped = ast.dump(writer)

    assert "mkstemp" in dumped and "fdopen" in dumped and "fsync" in dumped and "replace" in dumped
    opens = [node for node in ast.walk(writer)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "open"]
    assert not opens, "write_partial must not open() anything - only os.fdopen on the mkstemp fd"
    # And the temp is unique, never a predictable `path + ".tmp"`.
    mkstemp = [node for node in ast.walk(writer)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "mkstemp"]
    assert len(mkstemp) == 1
    assert {kw.arg for kw in mkstemp[0].keywords} == {"prefix", "suffix", "dir"}


def test_c_mkstemp_failure_is_non_fatal_and_publishes_nothing(tmp_path, monkeypatch):
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")
    reasons = []

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(qp.tempfile, "mkstemp", boom)

    assert qp.write_partial(target, _payload(), on_error=reasons.append) is None
    assert not os.path.exists(target)
    assert list(tmp_path.iterdir()) == []
    assert reasons and "No space left on device" in reasons[0]


def test_c_os_replace_failure_is_non_fatal_and_leaves_no_temp(tmp_path, monkeypatch):
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")
    reasons = []

    def boom(*_a, **_k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(qp.os, "replace", boom)

    assert qp.write_partial(target, _payload(), on_error=reasons.append) is None
    assert not os.path.exists(target)
    assert list(tmp_path.iterdir()) == [], "the unique temp must be cleaned up"
    assert reasons and "Permission denied" in reasons[0]


def test_c_a_failure_mid_serialisation_leaves_no_published_partial(tmp_path, monkeypatch):
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")

    def half_written(payload, handle, **_k):
        handle.write('{"partial_contract": "stage5_job')
        raise ValueError("circular reference")

    monkeypatch.setattr(qp.json, "dump", half_written)

    assert qp.write_partial(target, _payload()) is None
    assert not os.path.exists(target)
    assert list(tmp_path.iterdir()) == []


def test_c_a_failed_rewrite_cannot_corrupt_an_already_published_partial(tmp_path, monkeypatch):
    """The reader-visible invariant: the old entry survives intact, never a truncated target."""
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")
    qp.write_partial(target, _payload())
    original = Path(target).read_text(encoding="utf-8")

    monkeypatch.setattr(qp.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("nope")))

    assert qp.write_partial(target, _payload(job_total=999)) is None
    assert Path(target).read_text(encoding="utf-8") == original


def test_c_the_writer_creates_a_missing_directory(tmp_path):
    target = qp.partial_path(str(tmp_path / "deeper" / "resp.json"), 1, "1")

    assert qp.write_partial(target, _payload()) == target
    assert os.path.exists(target)


def test_c_base_exception_is_not_caught(tmp_path, monkeypatch):
    """`Exception` only. A cancelled or exiting process must not be held up writing diagnostics."""
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(qp.json, "dump", interrupted)

    with pytest.raises(KeyboardInterrupt):
        qp.write_partial(target, _payload())


def test_c_a_hostile_error_reporter_cannot_break_the_writer(tmp_path, monkeypatch):
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")
    monkeypatch.setattr(qp.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("nope")))

    def hostile(_reason):
        raise RuntimeError("the reporter itself is broken")

    assert qp.write_partial(target, _payload(), on_error=hostile) is None


def test_c_allow_nan_is_left_at_its_default(tmp_path):
    """This is a transport primitive, not the semantic-policy layer - the same rule `_save_cache` keeps.

    Demonstrated rather than asserted: a non-finite value survives the round trip, exactly as it does
    through the existing response and cache files. Sanitising belongs at ingestion, in the parent.
    """
    target = qp.partial_path(str(tmp_path / "resp.json"), 1, "1")
    payload = _payload(timings={"inference_seconds": float("inf"), "frame_count": 1, "tag_count": 1})

    assert qp.write_partial(target, payload) == target
    loaded = json.loads(Path(target).read_text(encoding="utf-8"))
    assert loaded["timings"]["inference_seconds"] == float("inf")


# ---------------------------------------------------------------------------
# D. THE REAL WORKER'S PUBLISHER
#
# `_publish_job_partial` cannot be imported (the worker needs cv2/PIL), so the real body is extracted
# and executed against a stub client and the real qwen_partials module.
# ---------------------------------------------------------------------------


class _StubClient:
    """Only what `_publish_job_partial` actually reads off the real QwenLlamaClient."""

    def __init__(self, model_id="Qwen3VL-2B", batch_size=4, load_seconds=8.07):
        self.model_id = model_id
        self.batch_size = batch_size
        self.load_seconds = load_seconds


def _publisher(partials=qp):
    return _exec_worker_functions(
        ["_publish_job_partial"],
        {"os": os, "_qwen_partials": partials, "Dict": dict},
    )["_publish_job_partial"]


def _publish(tmp_path, partials=qp, client=None, **overrides):
    kwargs = dict(
        response_path=str(tmp_path / "qwen_batch_response_tok.json"),
        job_index=2,
        job_total=4,
        job_id="2",
        video_file=r"J:\Cuts\clip.mp4",
        semantics=_SEMANTICS,
        timings=_TIMINGS,
        client=client or _StubClient(),
    )
    kwargs.update(overrides)
    _publisher(partials)(**kwargs)
    return kwargs


def test_d_the_real_publisher_writes_the_expected_artifact(tmp_path, capsys):
    kwargs = _publish(tmp_path)

    target = qp.partial_path(kwargs["response_path"], 2, "2")
    payload = json.loads(Path(target).read_text(encoding="utf-8"))

    assert payload["partial_contract"] == qp.PARTIAL_CONTRACT_VERSION
    assert payload["invocation"] == "qwen_batch_response_tok"
    assert payload["job_index"] == 2 and payload["job_total"] == 4
    assert payload["job_id"] == "2"
    assert payload["video_file"] == r"J:\Cuts\clip.mp4"
    assert payload["semantics"] == _SEMANTICS
    assert payload["timings"] == _TIMINGS
    assert "Qwen per-job partial published for job 2" in capsys.readouterr().out


def test_d_backend_telemetry_comes_from_the_live_client(tmp_path):
    kwargs = _publish(tmp_path, client=_StubClient("m-id", 8, 12.5))

    payload = json.loads(
        Path(qp.partial_path(kwargs["response_path"], 2, "2")).read_text(encoding="utf-8"))

    assert payload["backend"] == {
        "model_id": "m-id", "batch_size": 8, "model_load_seconds": 12.5}


def test_d_backend_telemetry_tracks_a_mid_batch_restart(tmp_path):
    """Why a reader must take backend values from the LAST valid partial, not the first.

    ``restart_server_with_slots`` reassigns ``batch_size`` and accumulates ``load_seconds``, so the
    first partial of a batch can legitimately disagree with what a successful response would report.
    """
    response = str(tmp_path / "qwen_batch_response_tok.json")
    client = _StubClient("m", 8, 8.0)
    _publish(tmp_path, response_path=response, job_index=1, job_id="1", client=client)
    client.batch_size = 4           # the restart halved the slots
    client.load_seconds += 7.5      # and its load time accumulated
    _publish(tmp_path, response_path=response, job_index=2, job_id="2", client=client)

    first = json.loads(Path(qp.partial_path(response, 1, "1")).read_text(encoding="utf-8"))
    last = json.loads(Path(qp.partial_path(response, 2, "2")).read_text(encoding="utf-8"))

    assert first["backend"] == {"model_id": "m", "batch_size": 8, "model_load_seconds": 8.0}
    assert last["backend"] == {"model_id": "m", "batch_size": 4, "model_load_seconds": 15.5}


def test_d_a_write_failure_is_reported_and_swallowed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(qp.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))

    _publish(tmp_path)  # must not raise

    out = capsys.readouterr().out
    assert "Qwen per-job partial not written for job 2" in out
    assert "denied" in out
    assert not globmod.glob(str(tmp_path / "*.partial.json"))


def test_d_an_unexpected_module_error_is_also_swallowed(tmp_path, capsys):
    class Broken:
        PARTIAL_CONTRACT_VERSION = "x"

        @staticmethod
        def partial_path(*_a, **_k):
            raise RuntimeError("module is broken")

    _publish(tmp_path, partials=Broken())  # must not raise

    assert "Qwen per-job partial not written for job 2" in capsys.readouterr().out


def test_d_a_missing_fork_package_degrades_to_no_partials(tmp_path, capsys):
    """A broken/partial checkout must mean "no partials", never a broken Qwen run."""
    _publish(tmp_path, partials=None)

    assert not globmod.glob(str(tmp_path / "*.partial.json"))
    assert capsys.readouterr().out == ""


def test_d_the_publisher_swallows_exception_not_base_exception():
    handlers = [
        node for node in ast.walk(_function(_tree(_WORKER), "_publish_job_partial"))
        if isinstance(node, ast.ExceptHandler)
    ]
    assert handlers, "_publish_job_partial must contain its own failures"
    for handler in handlers:
        assert isinstance(handler.type, ast.Name) and handler.type.id == "Exception", \
            "never BaseException - KeyboardInterrupt/SystemExit must still propagate"


def test_d_the_publisher_evaluates_no_completion_rule():
    """Executable body only: the docstring names these rules in order to say it must not use them."""
    body = _body_code(_function(_tree(_WORKER), "_publish_job_partial")).lower()

    for banned in ("qwen_job_completed", "cache_entry_is_complete", "ai_enabled", "ai_deferred",
                   "checkpoint", "require_ai"):
        assert banned not in body, banned


# ---------------------------------------------------------------------------
# E. main()'s LOOP STRUCTURE — the integration, asserted on real source
# ---------------------------------------------------------------------------


def _main_job_loop() -> ast.For:
    main = _function(_tree(_WORKER), "main")
    loops = [node for node in ast.walk(main) if isinstance(node, ast.For)]
    assert len(loops) == 1, "main must still have exactly one job loop"
    return loops[0]


def test_e_the_publisher_is_called_inside_the_job_loop_before_the_dict_assignments():
    loop = _main_job_loop()
    statements = loop.body

    publish_at = [
        index for index, node in enumerate(statements)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_publish_job_partial"
    ]
    assign_at = [
        index for index, node in enumerate(statements)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
            and target.value.id in {"semantics_by_job", "timings_by_job"}
            for target in node.targets)
    ]

    assert len(publish_at) == 1, "exactly one publish call, in the loop body"
    assert len(assign_at) == 2
    assert publish_at[0] < min(assign_at), "evidence is published before it is accumulated"


def test_e_the_publish_call_forwards_the_real_job_identity():
    loop = _main_job_loop()
    call = next(
        node.value for node in loop.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_publish_job_partial"
    )
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}

    assert set(keywords) == {"response_path", "job_index", "job_total", "job_id",
                             "video_file", "semantics", "timings", "client"}
    # The response path the parent supplied, never a path the worker invents.
    assert isinstance(keywords["response_path"], ast.Attribute)
    assert keywords["response_path"].attr == "response"
    # The worker's own loop position and the parent's own job id.
    assert isinstance(keywords["job_index"], ast.Name) and keywords["job_index"].id == "job_index"
    assert isinstance(keywords["job_id"], ast.Name) and keywords["job_id"].id == "job_id"
    # job_total is the real job count, not a request field.
    total = keywords["job_total"]
    assert isinstance(total, ast.Call) and total.func.id == "len" and total.args[0].id == "jobs"
    # Verbatim objects straight from _run_semantics_for_video.
    assert keywords["semantics"].id == "semantics"
    assert keywords["timings"].id == "timings"


def test_e_the_job_loop_keeps_its_fail_fast_behaviour():
    """A1 does not add per-job exception isolation. An exception still ends the invocation."""
    loop = _main_job_loop()

    assert not [node for node in ast.walk(loop) if isinstance(node, (ast.Try, ast.ExceptHandler))], \
        "the job loop must not gain a try/except - that is a separate, unauthorized change"


def test_e_the_final_response_is_still_written_exactly_once_after_the_loop():
    main = _function(_tree(_WORKER), "main")
    loop = _main_job_loop()
    loop_nodes = {id(node) for node in ast.walk(loop)}

    writes = [
        node for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
        and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "w"
    ]

    assert len(writes) == 1, "the response must still be written exactly once"
    assert id(writes[0]) not in loop_nodes, "and still after the loop, never incrementally"
    assert isinstance(writes[0].args[0], ast.Attribute) and writes[0].args[0].attr == "response"


def test_e_the_response_payload_shape_is_unchanged():
    """Including the legacy single-job branch: A1 adds no response key and removes none."""
    source = _read(_WORKER)

    for key in ('"model_load_seconds"', '"model_id"', '"batch_size"', '"peak_vram_gb"',
                '"total_seconds"', '"timings_by_job"'):
        assert key in source, key
    assert 'response["semantics"] = semantics_by_job.get("single", {})' in source
    assert 'response["semantics_by_job"] = semantics_by_job' in source
    assert "if legacy_single:" in source


def test_e_the_fork_import_is_guarded_like_the_progress_one():
    tree = _tree(_WORKER)
    guarded_modules = {
        child.module
        for node in ast.walk(tree) if isinstance(node, ast.Try)
        for child in node.body if isinstance(child, (ast.Import, ast.ImportFrom))
        for _ in [0]
        if getattr(child, "module", None)
    }
    guarded_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for child in node.body:
            if isinstance(child, ast.ImportFrom) and child.module:
                guarded_names.add(child.module)
                guarded_names.update(alias.name for alias in child.names)

    assert "beatsync_fork.qwen_progress" in guarded_modules
    assert "beatsync_fork" in guarded_names and "qwen_partials" in guarded_names


# ---------------------------------------------------------------------------
# F. REAL CHILD PROCESSES — death before, during and after publication
#
# The fake worker calls the REAL qwen_partials functions, so the atomic write and the filename
# contract are exercised across a real process boundary and a real kill. The real worker's
# `_publish_job_partial` wrapper is covered by section D.
# ---------------------------------------------------------------------------


_FAKE_WORKER = """
    import json, os, sys
    sys.path.insert(0, SRC)
    from beatsync_fork import qwen_partials as qp

    RESPONSE = sys.argv[sys.argv.index('--response') + 1]
    jobs = json.load(open(sys.argv[sys.argv.index('--request') + 1], encoding='utf-8'))['jobs']
    die_after = int(os.environ.get('DIE_AFTER', '-1'))

    semantics_by_job, timings_by_job = {}, {}
    for job_index, job in enumerate(jobs, 1):
        if die_after >= 0 and job_index > die_after:
            os._exit(3)
        semantics = {c['id']: {'action_intensity': 0.5, 'emotion': 'neutral'}
                     for c in job['candidates']}
        timings = {'prefetch_seconds': 0.1, 'inference_seconds': 0.2,
                   'frame_count': len(job['candidates']), 'tag_count': len(semantics)}
        qp.write_partial(
            qp.partial_path(RESPONSE, job_index, job['job_id']),
            qp.build_payload(
                response_path=RESPONSE, job_index=job_index, job_total=len(jobs),
                job_id=job['job_id'], video_file=job['video_file'],
                semantics=semantics, timings=timings,
                backend={'model_id': 'fake', 'batch_size': 4, 'model_load_seconds': 1.0}),
        )
        semantics_by_job[job['job_id']] = semantics
        timings_by_job[job['job_id']] = timings

    json.dump({'model_id': 'fake', 'batch_size': 4, 'peak_vram_gb': 0.0,
               'model_load_seconds': 1.0, 'total_seconds': 1.0,
               'timings_by_job': timings_by_job, 'semantics_by_job': semantics_by_job},
              open(RESPONSE, 'w', encoding='utf-8'), indent=2)
"""


def _run_fake(tmp_path, job_count=4, die_after=-1, job_ids=None):
    worker = tmp_path / "fake_worker.py"
    worker.write_text(f"SRC = {_SRC!r}\n" + textwrap.dedent(_FAKE_WORKER), encoding="utf-8")

    ids = job_ids or [str(index) for index in range(1, job_count + 1)]
    jobs = [
        {"job_id": job_id, "video_file": f"/v/clip_{job_id}.mp4", "fps": 24.0,
         "candidates": [{"id": f"cand_{job_id}_{n}", "start": n, "end": n + 1} for n in range(2)]}
        for job_id in ids
    ]
    request = tmp_path / "qwen_batch_request_tok.json"
    response = tmp_path / "qwen_batch_response_tok.json"
    request.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    env = dict(os.environ, DIE_AFTER=str(die_after), PYTHONUNBUFFERED="1")
    completed = subprocess.run(
        [sys.executable, str(worker), "--request", str(request), "--response", str(response)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    published = sorted(globmod.glob(qp.partial_glob(str(response))))
    return completed, str(response), published


def test_f_death_before_the_first_job_leaves_no_partial(tmp_path):
    completed, response, published = _run_fake(tmp_path, job_count=4, die_after=0)

    assert completed.returncode == 3
    assert published == []
    assert not os.path.exists(response), "and no response either - nothing is durable"


def test_f_death_after_two_of_four_jobs_preserves_exactly_two_valid_partials(tmp_path):
    """The primary A1 scenario. Before A1 this left nothing at all."""
    completed, response, published = _run_fake(tmp_path, job_count=4, die_after=2)

    assert completed.returncode == 3
    assert not os.path.exists(response), "the worker died before its single response write"
    assert [os.path.basename(path) for path in published] == [
        "qwen_batch_response_tok_job_00001_1.partial.json",
        "qwen_batch_response_tok_job_00002_2.partial.json",
    ]

    for index, path in enumerate(published, 1):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["partial_contract"] == qp.PARTIAL_CONTRACT_VERSION
        assert payload["invocation"] == "qwen_batch_response_tok"
        assert payload["job_index"] == index
        assert payload["job_id"] == str(index)
        assert payload["job_total"] == 4
        assert payload["video_file"] == f"/v/clip_{index}.mp4"
        assert set(payload["semantics"]) == {f"cand_{index}_0", f"cand_{index}_1"}
        assert payload["timings"]["frame_count"] == 2
        assert payload["timings"]["tag_count"] == 2


def test_f_no_half_written_artifact_survives_a_kill(tmp_path):
    """Every surviving file is either a complete partial or nothing - never a truncated target."""
    _completed, response, published = _run_fake(tmp_path, job_count=4, die_after=2)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert not [name for name in names if name.endswith(".tmp")]
    for path in published:
        json.loads(Path(path).read_text(encoding="utf-8"))  # parses, or this raises
    assert os.path.basename(response) not in names


def test_f_a_successful_run_leaves_every_partial_and_the_unchanged_final_response(tmp_path):
    completed, response, published = _run_fake(tmp_path, job_count=4)

    assert completed.returncode == 0
    assert len(published) == 4
    assert os.path.exists(response)

    final = json.loads(Path(response).read_text(encoding="utf-8"))
    assert set(final["semantics_by_job"]) == {"1", "2", "3", "4"}
    assert set(final["timings_by_job"]) == {"1", "2", "3", "4"}
    # Every partial's semantics are byte-equal to the final response's for the same job.
    for path in published:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["semantics"] == final["semantics_by_job"][payload["job_id"]]
        assert payload["timings"] == final["timings_by_job"][payload["job_id"]]


def test_f_the_legacy_single_job_shape_also_publishes(tmp_path):
    _completed, response, published = _run_fake(tmp_path, job_ids=["single"])

    assert [os.path.basename(path) for path in published] == [
        "qwen_batch_response_tok_job_00001_single.partial.json"]
    payload = json.loads(Path(published[0]).read_text(encoding="utf-8"))
    assert payload["job_id"] == "single"
    assert payload["job_index"] == 1


def test_f_partials_from_two_invocations_never_mix(tmp_path):
    """Discovery is scoped to one invocation's stem, so an earlier run's artifacts stay invisible."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _c1, response_a, published_a = _run_fake(first, job_count=2)
    _c2, _response_b, published_b = _run_fake(second, job_count=2)

    assert len(published_a) == 2 and len(published_b) == 2
    # Same filenames, different directories - and the glob is anchored on the full stem path.
    assert globmod.glob(qp.partial_glob(response_a)) == published_a


# ---------------------------------------------------------------------------
# G. BOUNDARIES A1 MUST NOT CROSS
# ---------------------------------------------------------------------------


def test_g_the_module_is_stdlib_only():
    imported = set()
    for node in ast.walk(_tree(_MODULE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {"__future__", "json", "os", "re", "tempfile", "typing"}


def test_g_no_cache_identity_or_completion_function_learns_about_partials():
    """A2-S1 lets the parent READ partials. No identity or completion function may learn of them.

    A1's version of this test banned every partial name from ``video_analysis.py`` outright, which
    was right while A1 was inert and is wrong now: the ban would forbid the feature. What actually
    needed protecting was narrower and is asserted directly here instead — **the parent may consume
    a partial, but no function that decides identity, completion or reusability may mention one.**
    That is the boundary A1's blanket ban was approximating, and naming it makes the test stronger:
    it now fails if a partial name ever reaches `_qwen_job_completed`, `_cache_entry_is_complete`,
    `_video_signature`, `_cache_path` or `_qwen_config_token`, which a string search over the whole
    file could never detect once any mention became legal.
    """
    tree = ast.parse(_read(_PARENT))

    # The parent never publishes, and never re-implements the artifact contract.
    whole = ast.unparse(tree)
    for never in ("write_partial", "build_payload", "partial_path", "PARTIAL_SUFFIX",
                  "PARTIAL_CONTRACT_VERSION"):
        assert never not in whole, f"video_analysis.py must not use {never}"

    # No identity, completion or reuse rule may know a partial exists.
    sealed = ("_qwen_job_completed", "_cache_entry_is_complete", "_stored_ai_cache_is_consistent",
              "_deterministic_analysis_completed", "_video_signature", "_cache_path",
              "_qwen_config_token", "_checkpoint_cache", "_save_cache", "_load_cache")
    for name in sealed:
        body = _body_code(_function(tree, name)).lower()
        for banned in ("partial", "qwen_partials", "invocation", "recover"):
            assert banned not in body, f"{name} must not mention {banned!r}"

    # And the one consumer is the recovery helper, which writes nothing.
    helper = _body_code(_function(tree, "_recover_jobs_from_partials"))
    for banned in ("_save_cache", "_checkpoint_cache", "_qwen_job_completed",
                   "_cache_entry_is_complete", "os.remove", "os.unlink", "shutil"):
        assert banned not in helper, f"recovery must not reference {banned}"


def test_g_the_contract_constants_are_untouched():
    assigned = {}
    for node in ast.walk(_tree(_PARENT)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value.value

    assert assigned.get("CACHE_CONTRACT_VERSION") == "stage5_cache_v3"
    assert assigned.get("ANALYSIS_VERSION") == "auto_av_analysis_v8_llama_vulkan_batched"
    assert assigned.get("_CACHE_IDENTITY_WORKER_CAP") == 16


def test_g_the_partial_contract_version_is_not_coupled_to_the_cache_contract():
    """A partial is a transport artifact. Bumping it must never invalidate semantic cache.

    Executable source only — the module docstring names ``CACHE_CONTRACT_VERSION`` in order to say
    the two constants are deliberately *not* coupled.
    """
    assert qp.PARTIAL_CONTRACT_VERSION == "stage5_job_partial_v1"
    source = _executable_source(_MODULE)

    assert "PARTIAL_CONTRACT_VERSION = 'stage5_job_partial_v1'" in source
    assert "CACHE_CONTRACT_VERSION" not in source
    assert "ANALYSIS_VERSION" not in source


def test_g_no_new_progress_protocol_kind_was_added():
    """Partials are files. The progress stream gains no authority and no new kind."""
    kinds = set()
    for node in ast.walk(_tree(_WORKER)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_emit_progress" and node.args
                and isinstance(node.args[0], ast.Constant)):
            kinds.add(node.args[0].value)

    assert kinds == _PRE_EXISTING_PROTOCOL_KINDS

    publisher = _body_code(_function(_tree(_WORKER), "_publish_job_partial"))
    assert "_emit_progress" not in publisher, "a partial is a file, not a status line"
    assert "qwen_progress" not in _executable_source(_MODULE)


def test_g_nothing_deletes_a_published_partial():
    """The cache directory is deliberately preserved. Retention is a separate owner decision."""
    tree = _tree(_MODULE)
    removals = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"remove", "unlink", "rmtree", "rmdir"}
    ]

    assert len(removals) == 1, "exactly one removal: write_partial's own temp"
    assert isinstance(removals[0].args[0], ast.Name) and removals[0].args[0].id == "tmp_path"
    writer = _function(tree, "write_partial")
    assert any(node is removals[0] for node in ast.walk(writer))

    assert "shutil" not in _executable_source(_MODULE)
    publisher_body = _body_code(_function(_tree(_WORKER), "_publish_job_partial"))
    for banned in ("remove", "unlink", "rmtree", "shutil", "glob"):
        assert banned not in publisher_body, banned


def test_g_no_creative_or_musical_state_crosses_this_boundary():
    """Persisted Stage-5 semantics are media-neutral (P2), and so is their evidence."""
    banned = ("seed", "creative", "variation", "master", "director", "proposal", "freestyle",
              "preset", "audio", "tempo", "beat", "section", "shortlist", "style", "music")
    words = _words(_executable_source(_MODULE)) | _words(
        _body_code(_function(_tree(_WORKER), "_publish_job_partial")))

    for word in banned:
        assert word not in words, f"qwen_partials must not know about {word!r}"


def test_g_the_module_mints_no_identity_and_reads_no_clock():
    """Invocation identity is the parent's response stem, echoed. Nothing here invents one."""
    source = _executable_source(_MODULE)

    for banned in ("time.", "datetime", "random", "secrets", "uuid", "getpid", "blake2b",
                   "sha1", "hashlib"):
        assert banned not in source, f"{banned} must not appear - the parent owns invocation identity"


def test_g_the_module_declares_the_atomicity_boundary_it_claims():
    source = _read(_MODULE)

    assert "PROCESS_CRASH_ATOMICITY = YES" in source
    assert "POWER_LOSS_DURABILITY   = NOT CLAIMED" in source
    assert "the containing directory is not" in source
