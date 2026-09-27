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

    A worker that dies and a worker that finished are different outcomes, and the pre-D1 code could
    not tell them apart. A finished worker is still not proof of completed AI work - see
    ``_qwen_job_completed`` and the behavioural suite.
    """
    deferred = _func(tree, "_complete_deferred_qwen")
    body = ast.unparse(deferred)

    assert "_QWEN_COMPLETED_KEY" in body, "an explicit completion signal must be consulted"
    assert "ai_enabled'] = qwen_completed" in body or "ai_enabled'] = bool(qwen_completed)" in body, (
        "ai_enabled must be the completion signal, never an unconditional True")
    assert "qwen_tag_count" not in body.split("qwen_completed")[0], (
        "tag count must not be the success predicate")


def test_the_qwen_facade_reports_completion_on_every_return_path(tree):
    """Every return path must carry a verdict.

    Skipped-by-config and an empty worker response are incomplete. A *submitted* job must satisfy the
    full requested-set contract in `_qwen_job_completed` — zero tags from a worker that merely
    finished is **not** success. The no-candidate path is the separate completed no-op.
    """
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


def test_inline_analysis_consumes_the_same_completion_signal(tree):
    """R2: the serial/inline path must not restate the request as a result.

    Behavioural coverage lives in ``test_stage5_cache_completion.py``; this pins the wiring so the
    signal cannot be quietly unhooked again.
    """
    analyse = _func(tree, "_analyze_single_video")
    body = ast.unparse(analyse)

    assert "_QWEN_COMPLETED_KEY" in body, "the inline path must consult the completion signal"
    assert ".pop(_QWEN_COMPLETED_KEY" in body, (
        "pop before timings.update(), or the private flag lands in the cached payload")

    returns = [node for node in ast.walk(analyse) if isinstance(node, ast.Return) and node.value]
    assert returns, "the analysis must return a record"
    rendered = ast.unparse(returns[-1])
    assert "inline_qwen_completed" in rendered, (
        "ai_enabled must depend on real completion, not only on enable_ai/defer_ai")
    assert "'ai_enabled': bool(enable_ai and not defer_ai)" not in rendered, (
        "the pre-R2 formula restated the request instead of the outcome")


def test_the_pop_happens_before_timings_are_updated(tree):
    """Order matters: updating first would copy the private flag into the stored timings.

    Compared by source line number of the individual ``Call`` nodes. Comparing the position of
    walked nodes does not work: ``ast.walk`` also yields the enclosing function, whose text contains
    both calls, so everything collapses to index 0.
    """
    for name in ("_analyze_single_video", "_complete_deferred_qwen"):
        func = _func(tree, name)
        pops = [node.lineno for node in ast.walk(func) if isinstance(node, ast.Call)
                and ".pop" in ast.unparse(node.func)
                and "_QWEN_COMPLETED_KEY" in ast.unparse(node)]
        updates = [node.lineno for node in _calls(func, "update")
                   if ast.unparse(node.func).startswith("timings.")]
        assert pops, f"{name}: expected a pop of the private completion key"
        assert updates, f"{name}: expected a timings.update()"
        assert min(pops) < min(updates), (
            f"{name}: pop at line {min(pops)} must precede timings.update() at {min(updates)}")


def test_single_job_completion_delegates_to_the_shared_per_job_rule(tree):
    """R4: the response envelope *locates* the job; `_qwen_job_completed` decides completion.

    R2 asserted that completion must be envelope membership alone, and that tag count must never
    appear in the verdict. Both were wrong against the worker contract: the worker records a job's
    timings whenever its loop returns, even when every candidate's semantic failed. Tag count *is*
    part of completion truth — together with the envelope and the real frame count, never alone.
    Behavioural coverage lives in ``test_stage5_cache_completion.py``.
    """
    facade = _func(tree, "_annotate_candidates_with_qwen")
    body = ast.unparse(facade)

    assert "_QWEN_SINGLE_JOB_ID" in body, "the per-job envelope must still be located by job id"
    membership = [
        node for node in ast.walk(facade)
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops)
        and "job_timings" in ast.unparse(node.comparators[0])
    ]
    assert membership, "the envelope lookup must remain a membership test on timings_by_job"

    completed_assigns = [node for node in ast.walk(facade) if isinstance(node, ast.Assign)
                         and ast.unparse(node.targets[0]) == "completed"]
    assert completed_assigns, "an explicit `completed` verdict is required"
    verdict = ast.unparse(completed_assigns[0].value)
    assert "_qwen_job_completed(" in verdict, (
        f"completion must delegate to the shared rule, not re-derive it; found {verdict}")
    assert "envelope_present" in verdict, verdict


def test_both_qwen_paths_share_one_completion_rule(tree):
    """The arithmetic must exist once, so single and batch cannot drift apart."""
    rule = _func(tree, "_qwen_job_completed")
    body = ast.unparse(rule)
    args = [arg.arg for arg in rule.args.args + rule.args.kwonlyargs]

    assert "frame_count" in body and "tag_count" in body
    assert "envelope_present" in args, "the envelope is an input to the rule"
    assert "requested_ids" in args and "returned_ids" in args, (
        f"R5: the requested and returned candidate sets are inputs; got {args}")

    for caller in ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch"):
        calls = _calls(_func(tree, caller), "_qwen_job_completed")
        assert calls, f"{caller} must use the shared rule"

    # R5: frame_count is compared against the REQUESTED set size, not merely against zero
    assert any(isinstance(node, ast.Compare) and "frame_count" in ast.unparse(node)
               and "len(expected)" in ast.unparse(node)
               for node in ast.walk(rule)), (
        "every requested candidate must have been decoded, not just 'more than none'")
    # an empty request can never be complete (that is the candidate-less case, handled elsewhere)
    assert any(isinstance(node, ast.If) and "not expected" in ast.unparse(node.test)
               for node in ast.walk(rule)), "an empty requested set must be rejected here"
    assert any(isinstance(node, ast.Compare) and any(isinstance(op, ast.NotEq) for op in node.ops)
               and "tag_count" in ast.unparse(node) and "frame_count" in ast.unparse(node)
               for node in ast.walk(rule)), "every decoded frame must have produced a semantic"
    # and the ids must be ours, exactly
    assert any(isinstance(node, ast.Compare) and any(isinstance(op, ast.Eq) for op in node.ops)
               and "returned" in ast.unparse(node) and "expected" in ast.unparse(node)
               for node in ast.walk(rule)), "the returned semantic ids must equal the requested ids"
    assert _calls(rule, "_is_count"), "bools must be rejected as counts"


def test_batch_completion_also_delegates_to_the_shared_rule(tree):
    """R4: membership in `semantics_by_job` locates the job; it does not prove completion."""
    batch = _func(tree, "_complete_deferred_qwen_batch")
    body = ast.unparse(batch)

    assert "envelope_present" in body, "membership is now only the envelope lookup"
    assigns = [node for node in ast.walk(batch)
               if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "job_completed"]
    assert assigns, "an explicit per-job verdict is required"
    verdict = ast.unparse(assigns[0].value)
    assert "_qwen_job_completed(" in verdict, verdict


def test_worker_counts_are_read_defensively(tree):
    """A malformed subprocess payload must not crash the analysis with ValueError."""
    _func(tree, "_coerce_count")
    _func(tree, "_reported_count")
    for name in ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch"):
        func = _func(tree, name)
        assert _calls(func, "_reported_count") or _calls(func, "_coerce_count"), (
            f"{name} must read worker counts through a coercing helper")
        bare_int_on_counts = [
            node for node in ast.walk(func)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "int"
            and ("frame_count" in ast.unparse(node) or "tag_count" in ast.unparse(node))
        ]
        assert not bare_int_on_counts, (
            f"{name} still calls int() directly on a worker count: "
            f"{[ast.unparse(n) for n in bare_int_on_counts]}")


def test_a_reported_zero_count_is_never_replaced_by_a_fallback(tree):
    """R5: `timing.get(key) or default` rewrote a genuine 0 into the requested candidate count."""
    reporter = _func(tree, "_reported_count")
    assert any(isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops)
               for node in ast.walk(reporter)), (
        "presence must decide, not truthiness")

    for name in ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch"):
        func = _func(tree, name)
        truthy_fallbacks = [
            ast.unparse(node) for node in ast.walk(func)
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
            and ("frame_count" in ast.unparse(node) or "tag_count" in ast.unparse(node))
            and "get(" in ast.unparse(node)
        ]
        assert not truthy_fallbacks, (
            f"{name} still uses an `or`-fallback on a worker count: {truthy_fallbacks}")


def test_the_loader_applies_a_stored_consistency_check_to_ai_records(tree):
    """R6: `ai_enabled` alone is not enough to reuse a persisted AI record.

    A read-only audit found 4 real cache records claiming `ai_enabled=True` with
    `qwen_frame_count=10` but `qwen_tag_count=9`; the pre-R6 loader accepted all four.
    """
    rule = _func(tree, "_cache_entry_is_complete")
    assert _calls(rule, "_stored_ai_cache_is_consistent"), (
        "the completion rule must check stored consistency, not just the ai_enabled flag")

    # and the check must come after ai_enabled, i.e. it strengthens rather than replaces it
    statements = [ast.unparse(node) for node in rule.body
                  if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    joined = "\n".join(statements)
    assert joined.index("ai_enabled") < joined.index("_stored_ai_cache_is_consistent"), joined

    # the loader must NOT try to replay the live rule against a stored record
    assert not _calls(rule, "_qwen_job_completed"), (
        "legacy records do not store requested_ids; replaying the live rule would invent evidence")
    assert not _calls(_func(tree, "_load_cache"), "_qwen_job_completed")


def test_the_stored_consistency_rule_checks_only_persisted_evidence(tree):
    """It must not require `frame_count == len(candidates)`: MAX_WINDOWS may have limited the set."""
    checker = _func(tree, "_stored_ai_cache_is_consistent")
    # executable statements only: the docstring legitimately *discusses* requested_ids to explain why
    # the live rule cannot be replayed here.
    code = "\n".join(ast.unparse(node) for node in checker.body
                     if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))

    assert "qwen_frame_count" in code and "qwen_tag_count" in code
    assert "ai_analyzed" in code, "the per-candidate marker is part of the stored evidence"
    assert _calls(checker, "_is_count"), "bools must be rejected as counts"

    # frame_count is bounded ABOVE by the candidate count, never required to equal it
    comparisons = [ast.unparse(node) for node in ast.walk(checker)
                   if isinstance(node, ast.Compare) and "frame_count" in ast.unparse(node)]
    assert any(">" in c or "<" in c for c in comparisons), comparisons
    assert not any("frame_count == len(candidates)" in c for c in comparisons), (
        f"a decoded subset is legitimate under MAX_WINDOWS; found {comparisons}")
    assert "requested_ids" not in code, "legacy payloads never stored the requested set"


def test_the_deterministic_scoring_evidence_discriminator_exists(tree):
    """R2: an empty candidate list alone must not count as a completed analysis."""
    _func(tree, "_deterministic_analysis_completed")
    rule = _func(tree, "_cache_entry_is_complete")
    assert _calls(rule, "_deterministic_analysis_completed"), (
        "the completion rule must distinguish a real candidate-less result from an open failure")

    evidence = ast.unparse(_func(tree, "_deterministic_analysis_completed"))
    assert "_DETERMINISTIC_SCORING_KEY" in evidence

    # and the discriminator must be written only inside the capture-opened branch
    analyse = _func(tree, "_analyze_single_video")
    writes = [node for node in ast.walk(analyse) if isinstance(node, ast.Assign)
              and "candidate_scoring_seconds" in ast.unparse(node.targets[0])]
    assert len(writes) == 1, (
        f"scoring evidence must be written exactly once; found {len(writes)}")
    guarded = [node for node in ast.walk(analyse)
               if isinstance(node, ast.If) and "isOpened" in ast.unparse(node.test)
               and any("candidate_scoring_seconds" in ast.unparse(inner)
                       for inner in ast.walk(node) if isinstance(inner, ast.Assign))]
    assert guarded, "the evidence must be produced only when the capture actually opened"


def test_the_empty_candidate_shortcut_is_gone(tree):
    """The D1 rule returned True for any empty candidate list; that accepted decode failures."""
    rule = _func(tree, "_cache_entry_is_complete")
    for node in ast.walk(rule):
        if isinstance(node, ast.If) and "not candidates" in ast.unparse(node.test):
            rendered = ast.unparse(node)
            assert "_deterministic_analysis_completed" in rendered, rendered
            assert rendered.count("return True") == 0, (
                f"an empty candidate list must not be an unconditional success: {rendered}")


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


def test_the_cache_key_carries_the_d2_identity(tree):
    """D1 deliberately left identity alone; D2 hardened it. Detailed coverage lives in
    ``test_stage5_cache_identity.py`` — this pins that the durability work still sees the D2 key."""
    signature = ast.unparse(_func(tree, "_video_signature"))

    assert "st_mtime_ns" in signature, "integer-second truncation was fixed in D2"
    assert "int(stat.st_mtime)" not in signature
    assert "ANALYSIS_VERSION" in signature
    assert "CACHE_CONTRACT_VERSION" in signature


def test_the_backend_signature_is_content_backed_and_fails_closed(tree):
    backend = _func(tree, "_qwen_backend_signature_token")
    rendered = ast.unparse(backend)
    assert "_llama_version_token" in rendered, "the version string remains as extra evidence"
    assert _calls(backend, "_backend_component_token"), "components must be content-fingerprinted"

    code = "\n".join(ast.unparse(node) for node in backend.body
                     if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))
    assert "ai_missing" not in code, "a stable token for an unprovable backend is what D2 removed"


def test_analysis_version_is_unchanged(module_source):
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in module_source


def test_the_cache_path_layout_is_unchanged(tree):
    """D2 changed the signature *inputs*, not the filename shape."""
    path = _func(tree, "_cache_path")
    body = ast.unparse(path)
    assert "_hash_text(name, 8)" in body
    assert _calls(path, "_video_signature"), "the filename still embeds the signature"
    # D2: the path may now decline to exist at all, which is the fail-closed seam
    assert any(isinstance(node, ast.Return) and ast.unparse(node.value) == "None"
               for node in ast.walk(path)), "an unprovable identity must yield no cache path"
