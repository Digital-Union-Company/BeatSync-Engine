"""Stage 5 durable-checkpoint wiring (D1).

`video_analysis.analyze_video_sources` cannot be imported on a bare interpreter (cv2/numpy/librosa),
so the *decisions* it delegates are executed for real in ``test_stage5_cache_completion.py`` and the
*wiring* is pinned here with ``ast`` - the same technique ``test_qwen_worker_protocol.py`` uses.
Matching is by function and keyword rather than by line number or grep, so a reorder cannot
accidentally satisfy a test and a rename cannot silently unhook a checkpoint.

What this suite guarantees: a checkpoint call exists at every point where a source becomes complete,
every save goes through the completion guard, the deferred paths receive the cache file they need,
per-job Qwen completion is decided per job, and the D2-deferred cache key is untouched.
"""

from __future__ import annotations

import ast
import os

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_VIDEO_ANALYSIS = os.path.join(_SRC, "video_analysis.py")


@pytest.fixture(scope="module")
def module_source() -> str:
    return open(_VIDEO_ANALYSIS, encoding="utf-8").read()


@pytest.fixture(scope="module")
def tree(module_source) -> ast.Module:
    return ast.parse(module_source)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in video_analysis.py")


def _calls(node: ast.AST, callee: str) -> list[ast.Call]:
    out = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            target = inner.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if name == callee:
                out.append(inner)
    return out


def _kwargs(call: ast.Call) -> dict[str, str]:
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}


# ---------------------------------------------------------------------------
# the primitives exist and are wired to each other
# ---------------------------------------------------------------------------


def test_the_cache_primitives_d1_relies_on_exist(tree):
    for name in ("_same_source", "_cache_entry_is_complete", "_load_cache", "_save_cache",
                 "_checkpoint_cache"):
        _func(tree, name)


def test_checkpoint_guards_every_write_through_the_completion_rule(tree):
    """`_checkpoint_cache` is the only thing allowed to decide that a save is safe."""
    checkpoint = _func(tree, "_checkpoint_cache")
    body = ast.unparse(checkpoint)

    assert _calls(checkpoint, "_cache_entry_is_complete"), "checkpoint must consult the rule"
    assert _calls(checkpoint, "_save_cache"), "checkpoint must delegate the write"
    assert "not cache_file" in body, "a missing cache path must be a no-op, not a crash"


def test_the_loader_uses_the_central_rule_and_the_source_identity(tree):
    loader = _func(tree, "_load_cache")
    assert _calls(loader, "_cache_entry_is_complete"), "no ad-hoc completion logic in the loader"
    assert _calls(loader, "_same_source"), "the loader must be able to reject a foreign payload"
    assert "expected_video_file" in {a.arg for a in loader.args.args + loader.args.kwonlyargs}


def test_the_completion_rule_outranks_ai_enabled_with_ai_deferred(tree):
    """A deferred entry must be rejected before ``ai_enabled`` can grant completion.

    Asserted over the executable statements, not the rendered text: the docstring legitimately
    discusses ``ai_enabled`` first, so a substring-order check would read the prose, not the logic.
    """
    rule = _func(tree, "_cache_entry_is_complete")
    statements = [ast.unparse(node) for node in rule.body
                  if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    joined = "\n".join(statements)

    assert joined.index("ai_deferred") < joined.index("ai_enabled"), joined
    # and rejecting a deferred entry must be unconditional, not gated on require_ai
    deferred_guard = next(s for s in statements if "ai_deferred" in s)
    assert "require_ai" not in deferred_guard, deferred_guard


def test_analysis_version_is_still_part_of_the_completion_rule(tree):
    assert "ANALYSIS_VERSION" in ast.unparse(_func(tree, "_cache_entry_is_complete"))


# ---------------------------------------------------------------------------
# checkpoints at every completion point
# ---------------------------------------------------------------------------


def test_every_source_analysis_branch_checkpoints(tree):
    """Parallel result, parallel serial-retry and serial branch each get a checkpoint.

    Three call sites, because `analyze_video_sources` has three places a deterministic result
    arrives. Losing any one of them silently restores the pre-D1 behaviour for that path.
    """
    orchestrator = _func(tree, "analyze_video_sources")
    calls = _calls(orchestrator, "_checkpoint_cache")
    assert len(calls) >= 4, (
        f"expected checkpoints for parallel, serial-retry, serial and the terminal backstop; "
        f"found {len(calls)}")

    rendered = [ast.unparse(call) for call in calls]
    assert all("require_ai" in text for text in rendered), rendered
    assert all("ai_available" in text for text in rendered), (
        "the checkpoint must use the run's real AI requirement, not a constant")


def test_the_terminal_loop_is_a_guarded_backstop_not_an_unconditional_save(tree):
    """The pre-D1 terminal loop called `_save_cache` directly, which is how a failed-AI record
    became a permanent AI-complete cache hit."""
    orchestrator = _func(tree, "analyze_video_sources")

    assert not _calls(orchestrator, "_save_cache"), (
        "analyze_video_sources must never call _save_cache directly; checkpoint instead")
    assert _calls(orchestrator, "_checkpoint_cache")


def test_the_cache_lookup_passes_the_expected_source(tree):
    orchestrator = _func(tree, "analyze_video_sources")
    loads = _calls(orchestrator, "_load_cache")
    assert loads, "the cache lookup must still happen"
    for call in loads:
        assert "expected_video_file" in _kwargs(call), ast.unparse(call)


def test_the_deferred_single_path_receives_and_uses_a_cache_file(tree):
    deferred = _func(tree, "_complete_deferred_qwen")
    arg_names = {a.arg for a in deferred.args.args + deferred.args.kwonlyargs}
    assert "cache_file" in arg_names, "the single deferred path cannot checkpoint without it"

    checkpoints = _calls(deferred, "_checkpoint_cache")
    assert len(checkpoints) >= 2, (
        "checkpoint both the candidate-less early return and the finished-Qwen path")

    orchestrator = _func(tree, "analyze_video_sources")
    single_calls = _calls(orchestrator, "_complete_deferred_qwen")
    assert single_calls, "the single deferred path must still be reachable"
    for call in single_calls:
        assert "cache_file" in _kwargs(call), ast.unparse(call)


def test_the_deferred_batch_path_checkpoints_each_job_independently(tree):
    batch = _func(tree, "_complete_deferred_qwen_batch")
    checkpoints = _calls(batch, "_checkpoint_cache")
    assert len(checkpoints) >= 2, (
        "checkpoint the candidate-less job and each merged job, not one summary write")

    body = ast.unparse(batch)
    assert "job_to_cache" in body, "each job must carry its own cache file"
    assert "cache_file" in body


# ---------------------------------------------------------------------------
# AI completion truth
# ---------------------------------------------------------------------------


def test_single_qwen_completion_comes_from_an_explicit_signal(tree, module_source):
    """Not from "no exception raised", and not from a non-zero tag count.

    A worker that dies and a worker that legitimately returns zero tags are different outcomes; the
    pre-D1 code could not tell them apart and marked both AI-complete.
    """
    deferred = _func(tree, "_complete_deferred_qwen")
    body = ast.unparse(deferred)

    assert "_QWEN_COMPLETED_KEY" in body, "an explicit completion signal must be consulted"
    assert "ai_enabled'] = qwen_completed" in body or "ai_enabled'] = bool(qwen_completed)" in body, (
        "ai_enabled must be the completion signal, never an unconditional True")
    assert "qwen_tag_count" not in body.split("qwen_completed")[0], (
        "tag count must not be the success predicate")


def test_the_qwen_facade_reports_completion_on_every_return_path(tree):
    """Skipped-by-config and an empty worker response are failures; zero tags with a real response
    and 'nothing to annotate' are successes."""
    facade = _func(tree, "_annotate_candidates_with_qwen")
    returns = [node for node in ast.walk(facade) if isinstance(node, ast.Return) and node.value]
    assert returns, "the facade must return completion information"
    for node in returns:
        assert "_QWEN_COMPLETED_KEY" in ast.unparse(node), ast.unparse(node)


def test_the_completion_signal_never_reaches_a_cached_payload(tree):
    """It is popped, so the stored record carries the consequence (ai_enabled), not bookkeeping."""
    body = ast.unparse(_func(tree, "_complete_deferred_qwen"))
    assert ".pop(_QWEN_COMPLETED_KEY" in body, "the private signal must be popped before storage"


def test_batch_completion_is_decided_per_job_by_response_membership(tree):
    """A globally non-empty `semantics_by_job` is not proof that *this* job completed.

    The membership test is located as an ``ast.Compare`` using ``ast.In`` whose right-hand side
    mentions the worker response, rather than by a rendered substring - the real expression carries
    an ``isinstance`` guard, so `"in semantics_by_job"` never appears literally.
    """
    batch = _func(tree, "_complete_deferred_qwen_batch")
    body = ast.unparse(batch)

    membership = [
        node for node in ast.walk(batch)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.In) for op in node.ops)
        and "semantics_by_job" in ast.unparse(node.comparators[0])
    ]
    assert membership, "per-job completion must be a membership test on the worker response"
    assert any("job_id" in ast.unparse(node.left) for node in membership), (
        "the membership test must be keyed on this job's id")

    assigns = [node for node in ast.walk(batch)
               if isinstance(node, ast.Assign) and "ai_enabled" in ast.unparse(node.targets[0])]
    rendered = {ast.unparse(node.value) for node in assigns}
    assert "job_completed" in rendered, (
        f"ai_enabled must come from the per-job verdict; found {rendered}")
    assert "True" not in rendered, (
        f"no unconditional AI-complete assignment may remain; found {rendered}")
    assert "job_completed" in body


def test_total_batch_failure_behaviour_is_preserved(tree):
    """Pre-existing and correct: an empty response marks every job not-AI-complete and retries."""
    body = ast.unparse(_func(tree, "_complete_deferred_qwen_batch"))
    assert "if not semantics_by_job" in body
    assert "will retry on the next run" in body


def test_the_candidate_less_source_is_finished_without_faking_ai_enabled(tree):
    """MINOR 11: it must be checkpointed and reusable, but ai_enabled must stay honest."""
    deferred = ast.unparse(_func(tree, "_complete_deferred_qwen"))
    head = deferred.split("name = _safe_name")[0]
    assert "ai_enabled'] = False" in head, "no candidates means no AI work was done - say so"
    assert "_checkpoint_cache" in head, "and it must still become durable"


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_the_writer_uses_a_unique_same_directory_temp_and_fsyncs_it(tree):
    save = _func(tree, "_save_cache")
    body = ast.unparse(save)

    mkstemp = _calls(save, "mkstemp")
    assert mkstemp, "a unique temp name is required; the shared '.tmp' caused cross-writer publishes"
    assert any("dir=" in ast.unparse(call) for call in mkstemp), (
        "the temp must live beside the target so os.replace stays on one filesystem")
    assert _calls(save, "fsync"), "the temp must be fsynced before it is published"
    assert _calls(save, "replace"), "publication must stay atomic"
    assert "finally" in body and _calls(save, "remove"), "clean up our own temp, best effort"


def test_the_writer_no_longer_derives_its_temp_from_the_final_path(tree):
    """`path + '.tmp'` is a shared resource between processes - it must be gone from the code."""
    save = _func(tree, "_save_cache")
    for node in ast.walk(save):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            rendered = ast.unparse(node)
            assert not (rendered.startswith("path +") and ".tmp" in rendered), rendered


def test_a_cache_write_failure_cannot_abort_the_render(tree):
    save = _func(tree, "_save_cache")
    handlers = [node for node in ast.walk(save) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "_save_cache must remain best-effort"
    assert not any(isinstance(node, ast.Raise) for node in ast.walk(save))


# ---------------------------------------------------------------------------
# D2 freeze: the cache key and creative surfaces are untouched by D1
# ---------------------------------------------------------------------------


def test_the_cache_key_is_unchanged_because_identity_hardening_is_d2(tree):
    signature = ast.unparse(_func(tree, "_video_signature"))

    assert "int(stat.st_mtime)" in signature, "D1 must not re-key the existing cache"
    assert "st_mtime_ns" not in signature, "source-identity hardening is deferred to D2"
    assert "ANALYSIS_VERSION" in signature


def test_the_path_and_backend_signature_tokens_are_unchanged(tree):
    token = ast.unparse(_func(tree, "_path_signature_token"))
    assert "int(stat.st_mtime)" in token and "st_mtime_ns" not in token

    backend = ast.unparse(_func(tree, "_qwen_backend_signature_token"))
    assert "_llama_version_token" in backend
    assert "ai_missing" in backend


def test_analysis_version_is_unchanged(module_source):
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in module_source


def test_the_cache_path_layout_is_unchanged(tree):
    body = ast.unparse(_func(tree, "_cache_path"))
    assert "_hash_text(name, 8)" in body
    assert "_video_signature(video_file, enable_ai, qwen_model_path)" in body
