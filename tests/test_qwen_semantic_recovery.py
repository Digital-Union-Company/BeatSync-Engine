"""Targeted semantic recovery (R1): the fallback exists, is narrow, and is genuinely last.

Two measured production candidates fail `_normalize_semantic` on every retry tier because greedy
decoding deterministically truncates their JSON at the 128-token primary budget. The fix asks once
more with a 160-token budget and a `description.maxLength = 96` schema. Both halves are required:
a bigger budget alone was measured failing through 256 tokens against a degenerate repetition loop,
and the bound alone leaves the other candidate one token short of closing.

`stage5_qwen_scene_worker.py` needs cv2/numpy/PIL, so it cannot be imported on the bare interpreter
the suite runs on. Two techniques are used instead, both stronger than grep:

* the pure, stdlib-only pieces (the constants, the schemas, `_is_semantic_rejection`) are extracted
  by `ast` and executed in an isolated namespace, so they are tested as real code;
* the wiring is asserted structurally - call sites matched by function name, keyword arguments by
  name, ordering by line number - so a refactor that quietly moves recovery ahead of the existing
  primary retry tiers, or adds a second attempt, fails the suite.

The measured candidate ids and filenames deliberately appear only here and in the evidence, never
in production code, which must recover the *shape* rather than special-case the instances.
"""

from __future__ import annotations

import ast
import copy
import os
import typing

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage5_qwen_scene_worker.py")
_PARENT = os.path.join(_REPO_ROOT, "src", "video_analysis.py")

# The two measured failures this work exists for. Evidence only - see the module docstring.
MEASURED_FAILURES = (
    ("190.mp4", "68b2762072_00000_00000000"),
    ("828.mp4", "66d0116873_00000_00000000"),
)


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {tree}")


def _method(tree: ast.Module, class_name: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == name:
                    return sub
    raise AssertionError(f"{class_name}.{name} not found")


def _calls(node: ast.AST, func_name: str) -> list[ast.Call]:
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = sub.func.id if isinstance(sub.func, ast.Name) else (
                sub.func.attr if isinstance(sub.func, ast.Attribute) else ""
            )
            if name == func_name:
                found.append(sub)
    return found


def _forwarded_values(call: ast.Call) -> set[str]:
    """Local names passed through as argument *values*.

    Deliberately reads the values, not the keyword names: `max_tokens=None` names the keyword while
    dropping the override, which is precisely the regression these tests exist to catch.
    """
    names = {a.id for a in call.args if isinstance(a, ast.Name)}
    names |= {kw.value.id for kw in call.keywords if isinstance(kw.value, ast.Name)}
    return names


def _generate_calls(node: ast.AST, dotted: str) -> list[ast.Call]:
    """Calls whose unparsed callee is exactly `dotted` (e.g. "self.generate")."""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if ast.unparse(sub.func) == dotted:
                found.append(sub)
    return found


def _enclosing_ifs(root: ast.AST, target: ast.AST) -> list[ast.If]:
    """Every `if` statement that contains `target`, outermost first."""
    chain = []
    for node in ast.walk(root):
        if isinstance(node, ast.If) and any(sub is target for sub in ast.walk(node)):
            chain.append(node)
    return sorted(chain, key=lambda n: n.lineno)


def _code(node: ast.AST) -> str:
    """Executable statements only - never the docstring.

    Asserting on `ast.unparse(fn)` reads the prose, so a docstring mentioning a name would satisfy a
    test about the code. This repo has been bitten by that repeatedly.
    """
    body = getattr(node, "body", [])
    return "\n".join(
        ast.unparse(stmt) for stmt in body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    )


@pytest.fixture(scope="module")
def worker_tree() -> ast.Module:
    return _tree(_WORKER)


@pytest.fixture(scope="module")
def pure():
    """Execute the stdlib-only recovery pieces from the real worker source.

    Pulls the schema/enum definitions, the two recovery constants, `_recovery_semantic_schema` and
    `_is_semantic_rejection` out of the real file and runs them. Nothing here touches cv2, numpy or
    PIL, so this is the production code under test rather than a copy of it.
    """
    tree = _tree(_WORKER)
    wanted_names = {
        "NUMERIC_KEYS", "ALLOWED_EMOTIONS", "ALLOWED_USES", "SEMANTIC_SCHEMA",
        "_SEMANTIC_RECOVERY_MAX_TOKENS", "_SEMANTIC_RECOVERY_DESCRIPTION_MAX_LENGTH",
    }
    wanted_funcs = {"_recovery_semantic_schema", "_is_semantic_rejection"}
    picked: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & wanted_names:
                picked.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # SEMANTIC_SCHEMA["properties"].update({...})
            if "SEMANTIC_SCHEMA" in ast.unparse(node):
                picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            picked.append(node)
    module = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"copy": copy, "Dict": typing.Dict}
    exec(compile(module, _WORKER, "exec"), namespace)  # noqa: S102 - real source, isolated namespace
    for name in wanted_names | wanted_funcs:
        assert name in namespace, f"failed to extract {name}"
    return namespace


# ---------------------------------------------------------------- RECOVERY-1 / 15 / 2
def test_recovery_1_primary_schema_has_no_max_length(pure):
    """The ordinary request must stay exactly as it was: an unbounded description string."""
    assert pure["SEMANTIC_SCHEMA"]["properties"]["description"] == {"type": "string"}


def test_recovery_15_primary_numeric_and_enum_contract_unchanged(pure):
    schema = pure["SEMANTIC_SCHEMA"]
    assert pure["NUMERIC_KEYS"] == [
        "action_intensity", "beauty_score", "combat", "chase", "explosion",
        "character_focus", "camera_motion", "visual_quality",
    ]
    assert pure["ALLOWED_EMOTIONS"] == {"soft", "tension", "hype", "sad", "neutral"}
    assert pure["ALLOWED_USES"] == {"drop", "soft", "build", "transition", "flow", "filler"}
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == pure["NUMERIC_KEYS"] + [
        "emotion", "recommended_use", "description"]
    for key in pure["NUMERIC_KEYS"]:
        assert schema["properties"][key] == {"type": "number", "minimum": 0, "maximum": 1}
    assert schema["properties"]["emotion"] == {
        "type": "string", "enum": sorted(pure["ALLOWED_EMOTIONS"])}
    assert schema["properties"]["recommended_use"] == {
        "type": "string", "enum": sorted(pure["ALLOWED_USES"])}


def test_recovery_2_recovery_schema_differs_only_by_description_max_length(pure):
    primary = pure["SEMANTIC_SCHEMA"]
    before = copy.deepcopy(primary)
    recovery = pure["_recovery_semantic_schema"]()

    assert recovery["properties"]["description"] == {"type": "string", "maxLength": 96}
    # the ONLY difference, proven by normalising that one key and comparing wholesale
    patched = copy.deepcopy(recovery)
    patched["properties"]["description"] = {"type": "string"}
    assert patched == primary
    # and the primary object must not have been mutated in place
    assert primary == before
    assert primary["properties"]["description"] == {"type": "string"}
    assert recovery is not primary
    assert recovery["properties"] is not primary["properties"]


# ---------------------------------------------------------------- RECOVERY-3 / 4 / 5
def test_recovery_3_primary_budget_still_comes_from_max_new_tokens(worker_tree):
    """Both backends must fall back to `_max_new_tokens()` when no override is supplied."""
    server = _code(_method(worker_tree, "LlamaServerClient", "generate"))
    assert "_max_new_tokens() if max_tokens is None else int(max_tokens)" in server
    cli = _code(_method(worker_tree, "LlamaMtmdClient", "generate"))
    assert "_max_new_tokens() if max_tokens is None else int(max_tokens)" in cli

    for cls in ("LlamaServerClient", "LlamaMtmdClient", "QwenLlamaClient"):
        sig = _method(worker_tree, cls, "generate")
        defaults = {
            arg.arg: default for arg, default in zip(
                sig.args.args[-len(sig.args.defaults):] if sig.args.defaults else [],
                sig.args.defaults,
            )
        }
        assert "max_tokens" in defaults, f"{cls}.generate missing max_tokens override"
        assert "semantic_schema" in defaults, f"{cls}.generate missing semantic_schema override"
        for name in ("max_tokens", "semantic_schema"):
            node = defaults[name]
            assert isinstance(node, ast.Constant) and node.value is None, (
                f"{cls}.generate {name} must default to None so existing callers are unchanged")


def test_recovery_4_recovery_budget_is_exactly_160_and_bound_is_96(pure):
    assert pure["_SEMANTIC_RECOVERY_MAX_TOKENS"] == 160
    assert pure["_SEMANTIC_RECOVERY_DESCRIPTION_MAX_LENGTH"] == 96


def test_recovery_5_recovery_constants_are_hard_coded_not_env(worker_tree):
    """A tunable recovery knob would be result-affecting config absent from the D2 cache key."""
    source = _source(_WORKER)
    assert "BEATSYNC_QWEN_RECOVERY" not in source
    assert "RECOVERY_MAX_TOKENS" in source

    seen = 0
    for node in worker_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if targets & {"_SEMANTIC_RECOVERY_MAX_TOKENS",
                      "_SEMANTIC_RECOVERY_DESCRIPTION_MAX_LENGTH"}:
            seen += 1
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, int), (
                "recovery constants must be plain int literals, not computed or env-derived")
    assert seen == 2

    # and the recovery path itself reads no environment
    recover = _code(_func(worker_tree, "_recover_semantic"))
    assert "environ" not in recover and "_env_int" not in recover
    schema_fn = _code(_func(worker_tree, "_recovery_semantic_schema"))
    assert "environ" not in schema_fn and "_env_int" not in schema_fn


# ------------------------------------------- override forwarding: every backend path (R2)
def test_cli_ctx_fallback_retry_forwards_both_overrides(worker_tree):
    """The CLI client's context-error self-retry must carry the recovery overrides with it.

    This is load-bearing rather than tidiness. Recovery starts at 160 tokens with the bounded schema;
    if a context error then triggers `LlamaMtmdClient.generate`'s recursive retry and the overrides
    are dropped, the retry silently becomes an ordinary 128-token unbounded request and truncates
    again - so recovery would report failure while appearing to have executed, and the source would
    stay permanently uncacheable for a reason no log explains.
    """
    fn = _method(worker_tree, "LlamaMtmdClient", "generate")
    recursive = _generate_calls(fn, "self.generate")
    assert len(recursive) == 1, (
        "expected exactly one recursive self.generate (the ctx fallback); "
        f"found {[ast.unparse(c) for c in recursive]}")
    call = recursive[0]

    forwarded = _forwarded_values(call)
    for name in ("image", "prompt", "item_id", "max_tokens", "semantic_schema"):
        assert name in forwarded, (
            f"ctx-fallback retry drops {name!r}: {ast.unparse(call)}")

    # It must sit under the non-zero-exit -> context/memory-error fallback, not somewhere unrelated.
    chain = _enclosing_ifs(fn, call)
    assert chain, "the recursive retry is no longer guarded by any condition"
    tests = [ast.unparse(node.test) for node in chain]
    assert any("returncode" in t for t in tests), (
        f"recursive retry is not under the non-zero-exit branch: {tests}")
    assert any("_is_context_or_memory_error" in t for t in tests), (
        f"recursive retry is not under the context/memory-error branch: {tests}")

    # and it really is a retry: the fallback ctx is adopted before re-entering
    guard = [node for node in chain if "_is_context_or_memory_error" in ast.unparse(node.test)][-1]
    body = "\n".join(ast.unparse(stmt) for stmt in guard.body)
    assert "self.ctx_size = fallback_ctx" in body


def test_qwen_client_forwards_overrides_on_every_backend_path(worker_tree):
    """No backend route may silently drop either override: server, ctx-retry server, or CLI."""
    fn = _method(worker_tree, "QwenLlamaClient", "generate")

    server_calls = _generate_calls(fn, "self.server.generate")
    assert len(server_calls) == 2, (
        "expected the ordinary server call and the fallback-context server retry; "
        f"found {[ast.unparse(c) for c in server_calls]}")
    for call in server_calls:
        forwarded = _forwarded_values(call)
        assert {"image", "prompt", "max_tokens", "semantic_schema"} <= forwarded, (
            f"server path drops an override: {ast.unparse(call)}")

    cli_calls = _generate_calls(fn, "self.cli.generate")
    assert len(cli_calls) == 1, f"expected one CLI handoff; found {len(cli_calls)}"
    forwarded = _forwarded_values(cli_calls[0])
    assert {"image", "prompt", "item_id", "max_tokens", "semantic_schema"} <= forwarded, (
        f"CLI handoff drops an override: {ast.unparse(cli_calls[0])}")

    # one of the two server calls is the ctx retry, and it must be inside the exception path
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert handlers, "the server exception handling disappeared"
    in_handler = [c for c in server_calls
                  if any(sub is c for h in handlers for sub in ast.walk(h))]
    assert len(in_handler) == 1, (
        "expected exactly one server retry inside the exception handler")
    retry_chain = [ast.unparse(n.test) for n in _enclosing_ifs(fn, in_handler[0])]
    assert any("_is_context_or_memory_error" in t for t in retry_chain), (
        f"the server ctx retry is not guarded by the context/memory check: {retry_chain}")


def test_recovery_reaches_the_backend_through_the_forwarding_client(worker_tree):
    """`_recover_semantic` must go through QwenLlamaClient.generate, which forwards to any backend.

    Calling `client.server.generate` directly would work only while a server happens to be up and
    would skip the CLI path entirely.
    """
    recover = _func(worker_tree, "_recover_semantic")
    direct = _generate_calls(recover, "client.server.generate")
    assert not direct, "recovery must not bypass the forwarding client"
    via_client = _generate_calls(recover, "client.generate")
    assert len(via_client) == 1, "recovery must issue exactly one client.generate"
    kwargs = {kw.arg for kw in via_client[0].keywords if kw.arg}
    assert {"max_tokens", "semantic_schema"} <= kwargs


# ---------------------------------------------------------------- RECOVERY-6 / 7 / 8
@pytest.mark.parametrize("semantic,text,expected", [
    ({}, '{"action_intensity": 0.8, "description": "truncated', True),   # the measured shape
    ({}, "   \n  ", False),                                              # whitespace only
    ({}, "", False),                                                     # transport / empty
    ({}, None, False),                                                   # defensive
    ({"emotion": "hype"}, '{"emotion": "hype"}', False),                 # accepted, not a rejection
    ({"emotion": "hype"}, "", False),                                    # accepted despite no text
])
def test_recovery_6_7_8_rejection_classification(pure, semantic, text, expected):
    """Recovery is for *non-empty text that normalization rejected* - nothing else."""
    assert pure["_is_semantic_rejection"](semantic, text) is expected


def test_recovery_7_transport_failures_classify_as_not_rejected(worker_tree):
    """Every exception path must report `False`, so a broken backend never triggers recovery."""
    for name in ("_generate_with_server", "_generate_serial"):
        fn = _func(worker_tree, name)
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        assert handlers, f"{name} lost its exception handling"
        for handler in handlers:
            returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
            assert returns, f"{name} exception handler must return"
            for ret in returns:
                assert isinstance(ret.value, ast.Tuple) and len(ret.value.elts) == 4, (
                    f"{name} must return the 4-tuple including the rejection flag")
                last = ret.value.elts[-1]
                assert isinstance(last, ast.Constant) and last.value is False, (
                    f"{name} transport failure must classify as not-a-semantic-rejection")

    # the "server is not running" early return is a transport condition too
    fn = _func(worker_tree, "_generate_with_server")
    early = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
             and isinstance(n.value, ast.Tuple) and len(n.value.elts) == 4]
    assert len(early) >= 3, "expected the not-running, exception and success returns"


def test_recovery_6_loop_guards_on_the_rejection_flag(worker_tree):
    wave = _code(_func(worker_tree, "_run_inference_wave"))
    assert "rejected.get(item_id)" in wave, "recovery must be gated on the rejection classification"
    assert "_recover_semantic" in wave


# ---------------------------------------------------------------- RECOVERY-9 / 10 / 11 / 12
def test_recovery_9_recovery_runs_after_every_existing_primary_tier(worker_tree):
    """Ordering is the whole point: recovery is a last resort, not a replacement for the tiers."""
    fn = _func(worker_tree, "_run_inference_wave")
    recover = _calls(fn, "_recover_semantic")
    assert len(recover) == 1
    recover_line = recover[0].lineno

    primary_lines = [c.lineno for c in _calls(fn, "_generate_with_server")]
    primary_lines += [c.lineno for c in _calls(fn, "_generate_serial")]
    # The parallel first pass passes `_generate_with_server` to `executor.submit` as a *reference*,
    # so it is not an ast.Call to that name - count it separately rather than miscounting the tiers.
    submits = _calls(fn, "submit")
    assert submits, "the parallel first pass disappeared"
    submitted = {ast.unparse(a) for call in submits for a in call.args}
    assert "_generate_with_server" in submitted, "the parallel pass no longer submits the primary"
    assert len(primary_lines) >= 3, (
        "expected the serial first pass, the server retry and the serial fallback to survive")
    assert recover_line > max(primary_lines + [c.lineno for c in submits]), (
        "targeted recovery must come after all primary generation attempts")

    # the reduced-slot tier still restarts and recurses, and still returns before recovery
    recursive = _calls(fn, "_run_inference_wave")
    assert len(recursive) == 1, "reduced-slot retry tier disappeared"
    assert recursive[0].lineno < recover_line
    assert _calls(fn, "restart_server_with_slots"), "reduced-slot restart tier disappeared"


def test_recovery_10_exactly_one_attempt_and_no_recursion(worker_tree):
    fn = _func(worker_tree, "_run_inference_wave")
    assert len(_calls(fn, "_recover_semantic")) == 1

    recover = _func(worker_tree, "_recover_semantic")
    generate_calls = _calls(recover, "generate")
    assert len(generate_calls) == 1, "recovery must issue exactly one generation"
    assert not _calls(recover, "_recover_semantic"), "recovery must not recurse"
    assert not _calls(recover, "_generate_with_server")
    assert not _calls(recover, "_generate_serial")

    kwargs = {kw.arg for kw in generate_calls[0].keywords if kw.arg}
    assert {"max_tokens", "semantic_schema"} <= kwargs
    body = _code(recover)
    assert "_SEMANTIC_RECOVERY_MAX_TOKENS" in body
    assert "_recovery_semantic_schema()" in body


def test_recovery_11_successful_primary_never_enters_recovery(worker_tree):
    wave = _code(_func(worker_tree, "_run_inference_wave"))
    assert "if item_id in semantics:" in wave, (
        "recovery must skip candidates that already have semantics")
    # eligibility is recomputed from `semantics`, not from the stale `failed` list
    assert "for offset, item in enumerate(wave_items, 1)" in wave


def test_recovery_12_failed_recovery_leaves_the_candidate_absent(worker_tree):
    """A failed recovery must add nothing, so the job stays incomplete and nothing checkpoints."""
    fn = _func(worker_tree, "_run_inference_wave")
    call = _calls(fn, "_recover_semantic")[0]

    # find the enclosing statement, then the guarded assignment after it
    owner = None
    for node in ast.walk(fn):
        if isinstance(node, ast.For):
            if any(sub is call for sub in ast.walk(node)):
                owner = node
    assert owner is not None
    guarded = [n for n in ast.walk(owner)
               if isinstance(n, ast.If) and "semantic" == ast.unparse(n.test).strip()]
    assert guarded, "the recovery result must only be stored under `if semantic:`"
    stored = [n for n in ast.walk(guarded[0]) if isinstance(n, ast.Subscript)]
    assert any("semantics" in ast.unparse(n) for n in stored)

    recover = _func(worker_tree, "_recover_semantic")
    returns = [ast.unparse(n.value) for n in ast.walk(recover)
               if isinstance(n, ast.Return) and n.value is not None]
    assert "{}" in returns, "recovery must return an empty dict on failure"


# ---------------------------------------------------------------- RECOVERY-13 / 14
def test_recovery_13_no_cache_or_completion_change_and_no_rekey(worker_tree):
    """Pins the compatibility argument for why this needs no re-key and no contract bump.

    A source current main caches has every requested candidate tagged on the primary path, so
    recovery never runs and the persisted semantics are identical. A source that misses a candidate
    fails `_qwen_job_completed`'s `returned_ids == requested_ids`, so current main writes no complete
    record at all - recovery can only turn an absence into a record, never contradict a stored one.
    """
    parent = _source(_PARENT)
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in parent
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in parent

    # recovery must be invisible to cache identity and to the completion rules
    for token in ("_SEMANTIC_RECOVERY_MAX_TOKENS", "_SEMANTIC_RECOVERY_DESCRIPTION_MAX_LENGTH",
                  "_recovery_semantic_schema", "_recover_semantic", "BEATSYNC_QWEN_RECOVERY"):
        assert token not in parent, f"{token} must not reach video_analysis.py"

    parent_tree = _tree(_PARENT)
    config = _code(_func(parent_tree, "_qwen_config_token"))
    assert "RECOVERY" not in config.upper()
    # MAX_WINDOWS reaches the token through `_qwen_max_windows()`; the other two are read inline.
    # P2 retired the fourth component (`smart_preset`) along with style-conditioned prompting; these
    # three are the whole of media-semantic Qwen configuration now, and recovery touches none of them.
    for key in ("_qwen_max_windows()", "BEATSYNC_QWEN_FRAME_WIDTH",
                "BEATSYNC_QWEN_MAX_NEW_TOKENS"):
        assert key in config, f"{key} must remain in the cache config token"
    assert "smart_preset" not in config

    # the completion rules still exist and still demand exact requested-set coverage
    completed = _code(_func(parent_tree, "_qwen_job_completed"))
    assert "returned_ids" in completed and "requested_ids" in completed
    assert "frame_count" in completed and "tag_count" in completed
    for name in ("_cache_entry_is_complete", "_stored_ai_cache_is_consistent",
                 "_deterministic_analysis_completed", "_checkpoint_cache"):
        assert _func(parent_tree, name) is not None


def test_recovery_14_worker_prompt_is_untouched_by_recovery(worker_tree):
    """Recovery changes the token budget and bounds `description`; it never touches the prompt.

    The prompt's own conditioning is P2's business (see `tests/test_media_neutral_semantics.py`);
    what this pins is that the semantic key list, the enums and the description contract - the parts
    recovery's bounded schema has to agree with - are still exactly what they were.
    """
    fn = _func(worker_tree, "_build_prompt")
    # `ast.unparse` normalises quoting, so compare the literal string pieces themselves.
    prompt = "".join(
        node.value for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    for fragment in (
        "You are tagging one source-video moment for professional AMV/GMV editing. ",
        "Return JSON only. Keys: action_intensity, beauty_score, combat, chase, explosion, ",
        "character_focus, camera_motion, visual_quality as numbers 0..1; ",
        "emotion as one of soft,tension,hype,sad,neutral; ",
        "recommended_use as one of drop,soft,build,transition,flow,filler; ",
        "description under 12 words. Do not include markdown.",
    ):
        assert fragment in prompt, f"prompt drifted: missing {fragment!r}"
    assert "maxLength" not in prompt
    assert "160" not in prompt and "96" not in prompt


# ---------------------------------------------------------------- no instance special-casing
def test_production_code_does_not_special_case_the_measured_failures():
    """The fix must recover the failure *shape*, not these two files."""
    worker = _source(_WORKER)
    parent = _source(_PARENT)
    for filename, candidate_id in MEASURED_FAILURES:
        for token in (filename, candidate_id, candidate_id.split("_")[0]):
            assert token not in worker, f"{token} must not appear in the worker"
            assert token not in parent, f"{token} must not appear in video_analysis.py"
