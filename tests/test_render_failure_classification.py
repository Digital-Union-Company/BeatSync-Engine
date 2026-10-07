#!/usr/bin/env python3
"""[FORK] Digital-Union (C3-R1B-a): the producer -> RenderOutcomeKind matrix, pinned mechanically.

C3-R1A shipped the five-member ``RenderOutcomeKind`` vocabulary with producers that were not
truthful: ``SHARED_FATAL`` had none at all, eleven of sixteen reachable producers left
``RENDER_OUTCOME_KEY`` unset, the batch loop preserved only ``CANCELLED`` and discarded every other
class, and the one specific class it did produce -- ``CANDIDATE_LOCAL`` for any ``AudioMixError`` --
was wrong for both causes that actually reach that handler. R1B-a fixes the foundation and changes
**no** continuation policy: the batch still stops after every non-success candidate.

So this file has two jobs, and the second is as important as the first:

1. every producer names the class the evidence supports (the matrix below);
2. R1B-b has **not** arrived early -- ``RENDER_SELECTION_SIZE`` is still 2, there is no
   continue-after-``CANDIDATE_LOCAL``, and no class is ever recovered by reading prose.

===============================================================================
How this file reaches code that cannot be imported
===============================================================================

`gui.py` imports gradio, cupy and cv2, so it is never imported here. Three techniques, all already
established in this suite (`.claude/rules/test-harness.md`):

* `audio_mixdown.py` bodies are AST-extracted and executed against a stubbed subprocess -- reusing
  `test_audio_mixdown.load_mixdown` rather than building a second loader;
* `_process_video_impl` and `_promote_output_no_replace` are AST-extracted from the real `gui.py`
  and executed against a synthesised namespace -- reusing `test_gui_guard_seam`'s `_impl_namespace`
  for the same reason;
* `render_selected_variants_guarded`'s real body is executed over stub runtime globals so the batch
  outcome is **measured**, not inferred from structure.

`RenderOutcomeKind`, `RenderCancelled` and `render_batch` are the REAL stdlib-only fork modules
everywhere below. A stub enum would let the thing under test drift without this file noticing.
"""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import types

import pytest

import beatsync_fork.audio_mix as fork_audio_mix
import beatsync_fork.presets as fork_presets
import beatsync_fork.render_batch as fork_render_batch
import beatsync_fork.render_worker as fork_render_worker
import beatsync_fork.smart_mix as fork_smart_mix
import beatsync_fork.variant_batch as fork_variant_batch
import beatsync_fork.variant_lab as fork_lab
from test_audio_mixdown import FakeCompleted, FakeSubprocess, load_mixdown
from test_gui_guard_seam import (
    _OsShim,
    _gui_body,
    _gui_func,
    _gui_source,
    _gui_tree,
    _impl_namespace,
    _load_promotion_helper,
)

KIND = fork_render_worker.RenderOutcomeKind
RenderCancelled = fork_render_worker.RenderCancelled

_GUI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "gui.py")
_MIXDOWN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "audio_mixdown.py")


# ===========================================================================
# The audio cause hierarchy
# ===========================================================================


def test_the_four_cause_types_sit_under_the_retained_base():
    """`AudioMixError` is retained as the base, so every pre-existing catch still catches."""
    mix = load_mixdown(FakeSubprocess())
    for name in ("AudioProbeError", "AudioMixInputError", "AudioMixPlanError",
                 "AudioMixExecutionError"):
        cause = getattr(mix, name)
        assert issubclass(cause, mix.AudioMixError), f"{name} must subclass AudioMixError"
        assert cause is not mix.AudioMixError, f"{name} must be a distinct type"
    # four DISTINCT types -- aliasing two of them would silently merge two mappings
    types_ = {mix.AudioProbeError, mix.AudioMixInputError, mix.AudioMixPlanError,
              mix.AudioMixExecutionError}
    assert len(types_) == 4


def test_render_cancelled_is_not_an_audio_error():
    """**The load-bearing negative.** If a Stop could be caught as an audio failure, every
    `except audio_mixdown.AudioMixError` in `gui.py` would re-type it into a render failure class
    and tell a user who pressed Cancel that their mix broke."""
    mix = load_mixdown(FakeSubprocess())
    assert not issubclass(RenderCancelled, mix.AudioMixError)
    assert not isinstance(RenderCancelled("x"), mix.AudioMixError)
    # and the reverse: no audio cause may be mistaken for a cancellation
    for name in ("AudioMixError", "AudioProbeError", "AudioMixInputError",
                 "AudioMixPlanError", "AudioMixExecutionError"):
        assert not issubclass(getattr(mix, name), RenderCancelled)


def test_the_audio_module_never_learns_about_batch_classification():
    """`audio_mixdown.py` names what failed locally and must not name a RenderOutcomeKind.

    The separation is the whole correction: the same `probe_duration` failure is SHARED_FATAL from
    the voice preflight, CANDIDATE_LOCAL from the SFX preflight and UNKNOWN_FATAL from the
    generated-master verification. No exception type raised inside this module could carry that, so
    the module must not try.
    """
    with open(_MIXDOWN, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    # Checked against the EXECUTABLE module, not the text: the docstrings legitimately *explain*
    # the separation (and naming the classes there is how a future reader learns why the split
    # exists), while what must never appear is code that reads or produces one.
    forbidden = {"RenderOutcomeKind", "SHARED_FATAL", "CANDIDATE_LOCAL", "UNKNOWN_FATAL",
                 "RENDER_OUTCOME_KEY", "session_state", "render_batch", "gui"}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.name.split(".")[0])
            used.add((node.asname or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            used.add(node.module.split(".")[-1])
    leaked = forbidden & used
    assert not leaked, f"audio_mixdown.py's CODE references {sorted(leaked)}"


# ===========================================================================
# probe_duration: the context-neutral cause
# ===========================================================================


@pytest.mark.parametrize("result", [
    FakeCompleted(1, "", "No such file"),      # non-zero ffprobe
    FakeCompleted(0, "not-a-number", ""),      # unparseable
    FakeCompleted(0, "0", ""),                 # unusable: zero
    FakeCompleted(0, "-3", ""),                # unusable: negative
    FakeCompleted(0, "nan", ""),               # unusable: NaN
    FakeCompleted(0, "inf", ""),               # unusable: inf
])
def test_every_direct_probe_failure_is_an_audio_probe_error(result):
    mix = load_mixdown(FakeSubprocess([result]))
    with pytest.raises(mix.AudioProbeError):
        mix.probe_duration("C:/voices/x.wav")


def test_a_probe_timeout_is_an_audio_probe_error():
    class Timeout(FakeSubprocess):
        def run(self, command, **kwargs):
            raise subprocess.TimeoutExpired(command, 30)

    mix = load_mixdown(Timeout())
    with pytest.raises(mix.AudioProbeError, match="Timed out"):
        mix.probe_duration("C:/voices/x.wav")


def test_probe_duration_lets_a_cancellation_escape_unchanged():
    """A cancellation must not come back as a probe failure, from either boundary.

    `probe_duration` has two places a `RenderCancelled` originates: its own
    `lifecycle.raise_if_cancelled()` before it starts, and `_run`'s poll loop mid-command. Its one
    `except` clause is the narrow `subprocess.TimeoutExpired`, which a cancellation is not.
    """
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "12.0", "")]))
    lifecycle = fork_render_worker.RenderLifecycle(invocation_id="probe")
    lifecycle.request_cancel()
    with pytest.raises(RenderCancelled):
        mix.probe_duration("C:/voices/x.wav", lifecycle=lifecycle)
    # and the module's own `except` surface is exactly as narrow as that claim requires
    fn = next(n for n in ast.walk(ast.parse(open(_MIXDOWN, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "probe_duration")
    handlers = [ast.unparse(h.type) if h.type else "BARE"
                for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers == ["subprocess.TimeoutExpired", "(TypeError, ValueError)"], handlers


# ===========================================================================
# Voice preflight -> AudioMixInputError -> SHARED_FATAL
# ===========================================================================


@pytest.mark.parametrize("selection,fragment", [
    ([None], "not a usable file path"),
    (["   "], "not a usable file path"),
    ([7], "not a usable file path"),
    (["C:/v/clip.txt"], "not a supported type"),
])
def test_direct_voice_validation_failures_are_input_errors(selection, fragment):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixInputError, match=fragment):
        mix.prepare_voice_inputs(selection)


def test_a_missing_voice_file_is_an_input_error(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixInputError, match="missing or unreadable"):
        mix.prepare_voice_inputs([str(tmp_path / "gone.wav")])


def test_a_voice_probe_failure_is_wrapped_as_an_input_error(tmp_path):
    """The probe cause is *re-typed*, because here the thing that failed IS a user input.

    The original `AudioProbeError` is chained rather than discarded, so nothing downstream has to
    read a message to learn what happened.
    """
    clip = tmp_path / "01_intro.wav"
    clip.write_bytes(b"x")
    mix = load_mixdown(FakeSubprocess([FakeCompleted(1, "", "moov atom not found")]))

    with pytest.raises(mix.AudioMixInputError) as excinfo:
        mix.prepare_voice_inputs([str(clip)])
    assert isinstance(excinfo.value.__cause__, mix.AudioProbeError), \
        "the probe cause must be chained, not thrown away"
    assert not isinstance(excinfo.value, mix.AudioProbeError), \
        "it must be re-typed, so the GUI cannot confuse it with the shared-music fallback"


def test_the_voice_preflight_wrap_is_narrow():
    """`except AudioProbeError`, never `except AudioMixError` and never `except Exception`.

    A wide catch would swallow a `RenderCancelled` from inside `probe_duration` and report the
    user's Stop as a broken voice clip.
    """
    fn = next(n for n in ast.walk(ast.parse(open(_MIXDOWN, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "prepare_voice_inputs")
    handlers = [ast.unparse(h.type) if h.type else "BARE"
                for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers == ["AudioProbeError"], handlers


def test_the_gui_maps_the_voice_preflight_to_shared_fatal():
    """Proven shared: the gate is a bare `if voice_files:` with no candidate condition, the
    selection is frozen for the whole batch, and the call takes that selection and nothing else."""
    handler = _audio_preflight_handler("prepare_voice_inputs")
    assert "RenderOutcomeKind.SHARED_FATAL" in handler, handler
    assert "CANDIDATE_LOCAL" not in handler, handler


# ===========================================================================
# SFX preflight -> AudioMixInputError -> CANDIDATE_LOCAL
# ===========================================================================


@pytest.mark.parametrize("root,fragment", [
    (None, "no SFX library folder"),
    ("   ", "no SFX library folder"),
    (7, "no SFX library folder"),
])
def test_an_unusable_sfx_root_is_an_input_error(root, fragment):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixInputError, match=fragment):
        mix.prepare_sfx_inputs(root, fork_smart_mix.ROLE_ORDER)


def test_a_missing_sfx_root_is_an_input_error(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixInputError, match="does not exist"):
        mix.prepare_sfx_inputs(str(tmp_path / "nope"), fork_smart_mix.ROLE_ORDER)


def test_an_empty_sfx_library_is_an_input_error(tmp_path):
    (tmp_path / "Impacts").mkdir()
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixInputError, match="no usable audio"):
        mix.prepare_sfx_inputs(str(tmp_path), fork_smart_mix.ROLE_ORDER)


def test_a_corrupt_sfx_asset_probe_failure_is_wrapped_as_an_input_error(tmp_path):
    role = tmp_path / "Impacts"
    role.mkdir()
    (role / "hit.wav").write_bytes(b"x")
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "0", "")]))

    with pytest.raises(mix.AudioMixInputError) as excinfo:
        mix.prepare_sfx_inputs(str(tmp_path), fork_smart_mix.ROLE_ORDER)
    assert isinstance(excinfo.value.__cause__, mix.AudioProbeError)


def test_the_sfx_preflight_wrap_is_narrow():
    fn = next(n for n in ast.walk(ast.parse(open(_MIXDOWN, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "prepare_sfx_inputs")
    handlers = [ast.unparse(h.type) if h.type else "BARE"
                for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers == ["OSError", "AudioProbeError"], handlers


def test_the_gui_maps_the_sfx_preflight_to_candidate_local():
    """The row that forced the local-cause / batch-outcome split.

    Root and roles are batch-frozen, so the *inputs* look shared -- but reachability is not. See
    the reachability proof below for why that difference is decided by candidate state.
    """
    handler = _audio_preflight_handler("prepare_sfx_inputs")
    assert "RenderOutcomeKind.CANDIDATE_LOCAL" in handler, handler
    assert "SHARED_FATAL" not in handler, handler


# ---------------------------------------------------------------------------
# The reachability proof, through the REAL resolvers and the REAL gate
# ---------------------------------------------------------------------------
#
# Deterministic fixture rather than a search: root master 92 at spread 100 over the default full
# range, with `sfx_amount` ticked under Audio variation, resolves a four-candidate batch whose
# amounts are 87 / 87 / 0 / 79. Candidate 3's Smart Mix is OFF, so with a broken SFX library
# candidates 1, 2 and 4 fail the preflight and candidate 3 renders fine. That is the whole evidence
# for CANDIDATE_LOCAL, and it is executed here rather than asserted in prose.

_ROOT_92_AMOUNTS = (87, 87, 0, 79)


def _root_92_batch(count=4):
    visual_base = {name: 50 for name in fork_presets.CREATIVE_CONTROL_FIELDS}
    config = fork_lab.VariantLabConfig(
        master_seed=92, spread=100,
        randomized=frozenset(fork_presets.CREATIVE_CONTROL_FIELDS))
    audio_config = fork_lab.AudioVariantConfig(randomized=frozenset({"sfx_amount"}))
    declaration = fork_variant_batch.declaration_from(
        config, visual_base, audio_config,
        {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50},
        count)
    return fork_variant_batch.resolve_batch(declaration)


def _smart_mix_active(sfx_root, amount, roles=fork_smart_mix.ROLE_ORDER):
    """The REAL production gate expression from `_process_video_impl`, not a paraphrase."""
    config = fork_smart_mix.SmartMixConfig(enabled_roles=roles, amount=amount,
                                           sfx_level_percent=50)
    return bool(sfx_root and str(sfx_root).strip()) and config.plans_anything


def test_root_92_resolves_a_batch_that_straddles_the_smart_mix_gate():
    batch = _root_92_batch()
    amounts = tuple(c.audio_recipe.sfx_amount for c in batch.candidates)
    assert amounts == _ROOT_92_AMOUNTS, amounts

    gates = [_smart_mix_active(r"C:\library", a) for a in amounts]
    assert gates == [True, True, False, True], gates
    assert any(gates) and not all(gates), \
        "the fixture must contain BOTH a candidate that reaches the preflight and one that does not"


def test_a_candidate_resolving_zero_amount_never_calls_the_sfx_preflight():
    """`sfx_amount == 0` is a hard off-branch, so the failing operation is never performed."""
    assert fork_smart_mix.SmartMixConfig(
        enabled_roles=fork_smart_mix.ROLE_ORDER, amount=0, sfx_level_percent=50
    ).plans_anything is False
    assert _smart_mix_active(r"C:\library", 0) is False
    assert _smart_mix_active(r"C:\library", 1) is True
    # 0 is a VALID AudioRecipe value, not an out-of-range accident
    assert fork_lab.AudioRecipe(music_under_voice_percent=35, sfx_amount=0,
                                sfx_level_percent=50).sfx_amount == 0


def test_the_preflight_and_structure_calls_sit_inside_the_candidate_gated_branch():
    """Structural half of the same proof: both producers are *inside* `if smart_mix_active:`."""
    impl = _gui_func("_process_video_impl")
    gated = [n for n in ast.walk(impl)
             if isinstance(n, ast.If) and ast.unparse(n.test) == "smart_mix_active"]
    rendered = "\n".join(ast.unparse(n) for n in gated)
    assert "prepare_sfx_inputs" in rendered
    assert "project_structure" in rendered
    # and the voice preflight is NOT -- its gate carries no candidate state
    voice_gate = next(n for n in ast.walk(impl)
                      if isinstance(n, ast.If) and ast.unparse(n.test) == "voice_files")
    assert "prepare_voice_inputs" in ast.unparse(voice_gate)
    assert "smart_mix_active" not in ast.unparse(voice_gate)


# ===========================================================================
# SmartMixStructureError -> CANDIDATE_LOCAL
# ===========================================================================


def test_the_gui_maps_a_smart_mix_structure_error_to_candidate_local():
    """Shared `beat_info`, candidate-gated operation. The gate decides, not the data."""
    impl = _gui_func("_process_video_impl")
    handler = next(
        h for n in ast.walk(impl) if isinstance(n, ast.Try) for h in n.handlers
        if h.type is not None
        and "SmartMixStructureError" in ast.unparse(h.type))
    rendered = ast.unparse(handler)
    assert "RenderOutcomeKind.CANDIDATE_LOCAL" in rendered, rendered
    assert "SHARED_FATAL" not in rendered


def test_smart_mix_itself_was_not_modified_for_classification():
    """`smart_mix.py` is a pure planner and stays one -- only the GUI mapping is new."""
    source = os.path.join(os.path.dirname(_MIXDOWN), "beatsync_fork", "smart_mix.py")
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("RenderOutcomeKind", "CANDIDATE_LOCAL", "SHARED_FATAL", "UNKNOWN_FATAL",
                      "RENDER_OUTCOME_KEY"):
        assert forbidden not in text, f"smart_mix.py mentions {forbidden}"


# ===========================================================================
# Voice placement -> AudioMixPlanError -> SHARED_FATAL
# ===========================================================================


def test_a_placement_failure_becomes_a_plan_error(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    voices = (fork_audio_mix.VoiceInput(0, "v.wav", 50.0),)
    with pytest.raises(mix.AudioMixPlanError, match="no legal placement"):
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=200.0,
            beat_times=[i * 0.5 for i in range(400)],
            sections=(fork_audio_mix.MusicSection(0.0, 200.0, "drop"),),
            voices=voices, config=fork_audio_mix.AudioMixConfig(),
            session_dir=str(tmp_path))


def test_placement_feasibility_reads_only_batch_frozen_configuration():
    """The source proof for SHARED_FATAL, re-derived from `audio_mix.py` on every run.

    `plan_voice_placements` may read only the three frozen placement fields. The one field a C3
    candidate varies, `music_under_voice_percent`, must NOT appear -- if it ever did, a different
    candidate could rescue a placement failure and the class would have to become CANDIDATE_LOCAL.
    """
    path = os.path.join(os.path.dirname(_MIXDOWN), "beatsync_fork", "audio_mix.py")
    fn = next(n for n in ast.walk(ast.parse(open(path, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "plan_voice_placements")
    read = {node.attr for node in ast.walk(fn)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "config"}
    assert read == {"avoid_drops", "start_delay_seconds", "min_gap_seconds"}, read
    assert "music_under_voice_percent" not in read
    assert "music_floor" not in read, \
        "the duck floor must stay out of placement; it is the one candidate-varied value"


def test_a_placement_failure_cannot_fire_with_an_empty_voice_selection(tmp_path):
    """Second half of the proof: `PlacementFailure` is returned only from inside the voice loop.

    So it is reachable only when the batch-frozen voice selection is non-empty -- which is what
    makes SHARED_FATAL unconditional rather than context-dependent.
    """
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "200.0", "")]))
    plan = fork_audio_mix.plan_voice_placements(
        music_duration=200.0, beat_times=(0.0, 1.0, 2.0),
        sections=(fork_audio_mix.MusicSection(0.0, 200.0, "drop"),),
        voices=(), config=fork_audio_mix.AudioMixConfig())
    assert not isinstance(plan, fork_audio_mix.PlacementFailure)
    assert isinstance(plan, fork_audio_mix.AudioMixPlan)


def test_the_gui_maps_a_plan_error_to_shared_fatal_before_the_generic_clause():
    """Order matters: a wider clause placed first would shadow the specific one entirely."""
    clauses = _mixdown_handler_clauses()
    assert clauses[0][0] == "audio_mixdown.AudioMixPlanError", clauses
    assert "RenderOutcomeKind.SHARED_FATAL" in clauses[0][1], clauses
    assert clauses[1][0] == "audio_mixdown.AudioMixError", clauses
    assert "RenderOutcomeKind.UNKNOWN_FATAL" in clauses[1][1], clauses
    assert len(clauses) == 2, clauses


# ===========================================================================
# Mix execution -> AudioMixExecutionError -> UNKNOWN_FATAL
# ===========================================================================


def _mix_plan():
    config = fork_audio_mix.AudioMixConfig()
    return fork_audio_mix.AudioMixPlan(5.0, (), (), config)


def test_a_mix_timeout_is_an_execution_error(tmp_path):
    class Timeout(FakeSubprocess):
        def run(self, command, **kwargs):
            raise subprocess.TimeoutExpired(command, 600)

    mix = load_mixdown(Timeout())
    with pytest.raises(mix.AudioMixExecutionError, match="timed out"):
        mix.render_mixed_master("m.mp3", _mix_plan(), str(tmp_path / "out.wav"))


def test_a_non_zero_mix_return_is_an_execution_error(tmp_path):
    mix = load_mixdown(FakeSubprocess([FakeCompleted(1, "", "filter graph exploded")]))
    with pytest.raises(mix.AudioMixExecutionError, match="mixdown failed"):
        mix.render_mixed_master("m.mp3", _mix_plan(), str(tmp_path / "out.wav"))


def test_a_missing_master_is_an_execution_error(tmp_path):
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "", "")]))
    with pytest.raises(mix.AudioMixExecutionError, match="no output file"):
        mix.render_mixed_master("m.mp3", _mix_plan(), str(tmp_path / "out.wav"))


def test_an_empty_master_is_an_execution_error(tmp_path):
    out = tmp_path / "out.wav"
    out.write_bytes(b"")
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "", "")]))
    with pytest.raises(mix.AudioMixExecutionError, match="empty output file"):
        mix.render_mixed_master("m.mp3", _mix_plan(), str(out))


def test_a_generated_master_probe_failure_is_an_execution_error_not_an_input_error(tmp_path):
    """**Explicitly not `AudioMixInputError`.** The artifact is this render's own output, so a
    failure reading it is never a statement about something the user supplied.

    [C3-R1B-a / R2] And the **displayed reason is byte-equivalent to the cause**. R1B-a is
    classification-only: the type changes, the text the user reads must not. R1 prefixed this with
    "Could not verify the mixed master: " while the milestone claimed no user-facing message
    changed; R2 restores the probe's own wording. The cause stays chained for diagnostics.
    """
    out = tmp_path / "out.wav"
    out.write_bytes(b"RIFF")
    mix = load_mixdown(FakeSubprocess([
        FakeCompleted(0, "", ""),                      # the mix command
        FakeCompleted(1, "", "Invalid data found"),    # the verification probe
    ]))
    with pytest.raises(mix.AudioMixExecutionError) as excinfo:
        mix.render_mixed_master("m.mp3", _mix_plan(), str(out))
    wrapper = excinfo.value
    cause = wrapper.__cause__
    assert isinstance(cause, mix.AudioProbeError)
    assert not isinstance(wrapper, mix.AudioMixInputError)
    # the whole of §9: same reason, different type
    assert str(wrapper) == str(cause), \
        f"the displayed reason changed: {str(wrapper)!r} != {str(cause)!r}"
    assert "Could not read" in str(wrapper), str(wrapper)
    assert "verify the mixed master" not in str(wrapper), \
        "R1's added prefix is back -- that is a user-facing change in a classification-only milestone"


def test_no_audio_wrapper_alters_the_reason_the_user_reads():
    """All three narrow wraps must preserve their cause's text exactly.

    A wrap exists to change the *type* so `gui.py` can classify. The moment one also rewrites the
    message, R1B-a stops being classification-only — and a reader comparing the panel against the
    base would see a difference this milestone promised not to introduce.
    """
    fn_source = open(_MIXDOWN, encoding="utf-8").read()
    tree = ast.parse(fn_source)
    wrapped = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or "AudioProbeError" not in ast.unparse(node.type):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Raise) and inner.exc is not None:
                wrapped.append(ast.unparse(inner))
    assert len(wrapped) == 3, wrapped
    for rendered in wrapped:
        assert "str(exc)" in rendered, \
            f"a wrap rewrites its cause's message instead of forwarding it: {rendered}"
        assert "from exc" in rendered, f"the cause is not chained: {rendered}"


def test_a_duration_drift_is_an_execution_error(tmp_path):
    out = tmp_path / "out.wav"
    out.write_bytes(b"RIFF")
    mix = load_mixdown(FakeSubprocess([
        FakeCompleted(0, "", ""),
        FakeCompleted(0, "9.5", ""),                   # the plan wants 5.0
    ]))
    with pytest.raises(mix.AudioMixExecutionError, match="drift"):
        mix.render_mixed_master("m.mp3", _mix_plan(), str(out))


def test_the_generated_master_probe_wrap_is_narrow():
    """`render_mixed_master` hands the lifecycle to `probe_duration`, so a wide catch here would
    convert a user's Stop into an execution failure."""
    fn = next(n for n in ast.walk(ast.parse(open(_MIXDOWN, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "render_mixed_master")
    handlers = [ast.unparse(h.type) if h.type else "BARE"
                for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers == ["subprocess.TimeoutExpired", "AudioProbeError"], handlers


def test_build_mixed_master_still_discards_a_partial_master_on_both_paths():
    """R1A's cleanup guarantee, unchanged: an audio failure and a cancellation both discard the
    partial WAV, and the cancellation still re-raises as itself."""
    fn = next(n for n in ast.walk(ast.parse(open(_MIXDOWN, encoding="utf-8").read()))
              if isinstance(n, ast.FunctionDef) and n.name == "build_mixed_master")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.handlers]
    clauses = {ast.unparse(h.type): ast.unparse(h) for t in tries for h in t.handlers}
    assert set(clauses) == {"AudioMixError", "RenderCancelled"}, clauses
    for name, rendered in clauses.items():
        assert "discard_master(output_path)" in rendered, name
        assert "raise" in rendered, name
    # the cancellation clause must NOT re-type into any audio cause
    for cause in ("AudioMixInputError", "AudioMixPlanError", "AudioMixExecutionError",
                  "AudioProbeError"):
        assert cause not in clauses["RenderCancelled"], \
            f"a cancellation was re-typed as {cause}"


# ===========================================================================
# The defensive original-music fallback -> UNKNOWN_FATAL
# ===========================================================================


def test_the_music_fallback_probe_is_classified_unknown_fatal_unconditionally():
    """Frozen product decision. The path is unreachable today (`analyze_beats_auto` guards
    `y.size == 0` and then sets `audio_duration = len(y) / sr`, strictly positive), so both
    candidate choices behave identically in R1B-a. UNKNOWN_FATAL is the forward-safe one: a future
    change that made this reachable gets a fresh review rather than inheriting a conditional."""
    impl = _gui_func("_process_video_impl")
    handler = next(
        h for n in ast.walk(impl) if isinstance(n, ast.Try) for h in n.handlers
        if h.type is not None and "AudioProbeError" in ast.unparse(h.type))
    rendered = ast.unparse(handler)
    assert "RenderOutcomeKind.UNKNOWN_FATAL" in rendered, rendered
    assert "SHARED_FATAL" not in rendered, rendered
    assert "CANDIDATE_LOCAL" not in rendered, rendered
    # no `prepared_voices`-conditional classification anywhere near it
    assert "prepared_voices" not in rendered, \
        "the rejected conditional mapping was implemented"


def test_the_fallback_hoist_preserves_the_short_circuit_and_adds_no_lifecycle():
    """Semantically identical to the `float(...) or probe_duration(...)` it replaces."""
    body = _gui_body("_process_video_impl")
    assert "music_duration = float(beat_info.get('audio_duration') or 0.0)" in body
    assert "if not music_duration:" in body
    assert "music_duration=music_duration" in body
    # the old inline form is gone
    assert "or audio_mixdown.probe_duration(local_audio_path)" not in body
    # and cancellation behaviour is untouched: this call never took a lifecycle and still does not
    calls = [ast.unparse(n) for n in ast.walk(_gui_func("_process_video_impl"))
             if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "audio_mixdown.probe_duration"]
    assert calls == ["audio_mixdown.probe_duration(local_audio_path)"], calls


# ===========================================================================
# Durable promotion: three branches, three classes
# ===========================================================================


def test_a_successful_promotion_contributes_no_failure_class(tmp_path):
    def rename(src, dst):
        os.rename(src, dst)

    promote = _load_promotion_helper(rename)
    temp = tmp_path / "s" / "a.mp4"
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"NEW")
    dest = tmp_path / "o" / "a.mp4"
    dest.parent.mkdir(parents=True)

    assert promote(str(temp), str(dest)) == ("", None)


@pytest.mark.parametrize("rename,expected,fragment", [
    (lambda s, d: (_ for _ in ()).throw(FileExistsError(errno.EEXIST, "File exists")),
     KIND.CANDIDATE_LOCAL, "preserved"),
    (lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
     KIND.SHARED_FATAL, "volume"),
    (lambda s, d: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")),
     KIND.UNKNOWN_FATAL, "promotion failed"),
])
def test_each_promotion_branch_carries_its_own_class(tmp_path, rename, expected, fragment):
    promote = _load_promotion_helper(rename)
    message, kind = promote(str(tmp_path / "temp.mp4"), str(tmp_path / "out.mp4"))
    assert kind is expected
    assert fragment in message.lower()


def test_the_promotion_helper_still_removes_nothing_and_never_copies():
    """The typed return must not have bought a behaviour change."""
    body = _gui_body("_promote_output_no_replace")
    for forbidden in ("os.remove(", "os.unlink(", "shutil.rmtree(", "shutil.move(",
                      "shutil.copy", "os.replace(", "open(", "subprocess"):
        assert forbidden not in body, f"the promotion helper uses {forbidden}"
    calls = [ast.unparse(n.func) for n in ast.walk(_gui_func("_promote_output_no_replace"))
             if isinstance(n, ast.Call)]
    assert "os.rename" in calls
    assert "os.path.exists" not in calls, \
        "a pre-check would re-open the race the atomic rename closes"


def test_the_promotion_call_site_fails_closed_on_an_unnamed_kind():
    """If a future branch forgot to name a class, the caller must still record a fatal."""
    body = _gui_body("_process_video_impl")
    assert "promotion_kind or RenderOutcomeKind.UNKNOWN_FATAL" in body


# ===========================================================================
# Primary shared inputs and the source gate -> SHARED_FATAL
# ===========================================================================


_SHARED_INPUT_MESSAGES = (
    "❌ Error: Could not access audio file",
    "❌ Error: No audio file selected",
    "❌ Error: Could not access video files",
    "❌ Error: No video files selected",
    "❌ Error: Audio file is missing or inaccessible.",
    "❌ Error: Video files are missing or inaccessible.",
)


def test_all_six_primary_input_failures_are_shared_fatal():
    """Each is a statement about `audio_file` / `video_files`, both frozen for the whole batch.

    Walked structurally so a new early return cannot be added without a class: every `return` in
    `_process_video_impl` carrying one of these messages must be immediately preceded by the
    SHARED_FATAL assignment.
    """
    impl = _gui_func("_process_video_impl")
    found = {}
    for node in ast.walk(impl):
        if not isinstance(node, (ast.FunctionDef, ast.If, ast.Try, ast.For, ast.While)):
            continue
        body_lists = [node.body] + ([node.orelse] if hasattr(node, "orelse") else [])
        for statements in body_lists:
            for previous, current in zip(statements, statements[1:]):
                if not isinstance(current, ast.Return):
                    continue
                rendered = ast.unparse(current)
                for message in _SHARED_INPUT_MESSAGES:
                    if message in rendered:
                        found[message] = ast.unparse(previous)
    assert set(found) == set(_SHARED_INPUT_MESSAGES), \
        f"unmatched: {sorted(set(_SHARED_INPUT_MESSAGES) - set(found))}"
    for message, preceding in found.items():
        assert preceding == \
            "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL", \
            f"{message!r} is preceded by {preceding!r}"


def test_the_source_gate_refusal_is_shared_fatal():
    """The gate's only inputs are `source_state` and the four live source controls -- every one an
    argument the batch handler froze before its candidate loop."""
    gate = _gui_func("_process_video_guarded_unlocked")
    refusal = next(n for n in ast.walk(gate)
                   if isinstance(n, ast.If) and ast.unparse(n.test) == "not decision.allowed")
    rendered = ast.unparse(refusal)
    assert "RenderOutcomeKind.SHARED_FATAL" in rendered, rendered
    assert "decision.message" in rendered, "the user-facing message must be unchanged"


def test_the_early_output_collision_is_candidate_local():
    impl = _gui_func("_process_video_impl")
    collision = next(
        n for n in ast.walk(impl) if isinstance(n, ast.If)
        and ast.unparse(n.test) == "os.path.exists(output_path)")
    rendered = ast.unparse(collision)
    assert "RenderOutcomeKind.CANDIDATE_LOCAL" in rendered, rendered
    assert "SHARED_FATAL" not in rendered


# ===========================================================================
# `_process_video_impl` executed for real: the collision row end to end
# ===========================================================================


_FROZEN_STAMP = "20261007_120000"


def _impl_with_frozen_clock(tmp_path, calls):
    """`_impl_namespace`, plus the two things a deterministic filename needs.

    The output name embeds `datetime.now()` to the second, so a test that *guesses* it would be a
    race across a second boundary. The clock is pinned instead, which makes the destination this
    render will compute exactly predictable -- and `GRADIO_TEMP_DIR` is created, because
    `_process_video_impl` calls `tempfile.mkdtemp(dir=...)` into it.
    """
    import datetime as _dt

    namespace = _impl_namespace(tmp_path, calls, rename=os.rename,
                                dest_appears_during_render=False)
    os.makedirs(namespace["GRADIO_TEMP_DIR"], exist_ok=True)

    class _FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 7, 12, 0, 0)

    namespace["datetime"] = types.SimpleNamespace(datetime=_FrozenDatetime)
    # The REAL source-path resolvers, extracted rather than reimplemented: these cases hand
    # `_process_video_impl` a fresh selection, so it genuinely walks the resolution branch that
    # the shared-input rows are about.
    bodies = [_gui_func(name) for name in
              ("_as_existing_source_path", "_as_existing_source_paths",
               "_promote_output_no_replace", "_process_video_impl")]
    exec(compile(ast.Module(body=bodies, type_ignores=[]), "<gui>", "exec"), namespace)
    return namespace


def test_the_real_impl_records_candidate_local_for_an_occupied_destination(tmp_path):
    """Not a structural claim: the real function, real bytes, real class on the key.

    Also proves the refusal still happens BEFORE any analysis -- a measured call count, so
    "no expensive work began" is not an inference.
    """
    calls = []
    namespace = _impl_with_frozen_clock(tmp_path, calls)

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"ID3")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"ftyp")
    out_dir = namespace["get_output_dir"]()

    # occupy exactly the destination this render will compute (neutral creative adds no suffix)
    stem = "music_video_batchT_c01_m99"
    existing = os.path.join(out_dir, f"{stem}_{_FROZEN_STAMP}.mp4")
    with open(existing, "wb") as handle:
        handle.write(b"OLD")

    state = {}
    result = namespace["_process_video_impl"](
        audio_file=str(audio), video_files=[str(video)],
        output_filename=stem, processing_mode="h264",
        custom_fps=30.0, creative=None, session_state=state)

    assert result[0] is None, result
    assert "already exists and was preserved" in result[1], result[1]
    assert state["render_outcome_kind"] is KIND.CANDIDATE_LOCAL
    assert state["last_output_path"] == ""
    assert "analyze_beats_auto" not in calls, \
        "the collision must be refused before any expensive work"
    assert open(existing, "rb").read() == b"OLD", "the user's existing output was replaced"


def test_the_real_impl_records_shared_fatal_for_a_missing_audio_selection(tmp_path):
    """The other end of the matrix, executed: a shared primary input really does record
    SHARED_FATAL rather than leaving the key `None` as R1A did."""
    calls = []
    namespace = _impl_with_frozen_clock(tmp_path, calls)

    state = {}
    result = namespace["_process_video_impl"](
        audio_file="", video_files=[str(tmp_path / "clip.mp4")],
        output_filename="out", processing_mode="h264",
        custom_fps=30.0, creative=None, session_state=state)

    assert result[0] is None
    assert "No audio file selected" in result[1]
    assert state["render_outcome_kind"] is KIND.SHARED_FATAL
    assert calls == [], "a missing selection must cost no analysis and no render"


def test_the_real_impl_records_success_only_after_a_clean_promotion(tmp_path):
    """The positive control for the two rows above: an unobstructed render still reaches SUCCESS,
    so the new classification has not broken the happy path."""
    calls = []
    namespace = _impl_with_frozen_clock(tmp_path, calls)

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"ID3")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"ftyp")

    state = {}
    result = namespace["_process_video_impl"](
        audio_file=str(audio), video_files=[str(video)],
        output_filename="clean", processing_mode="h264",
        custom_fps=30.0, creative=None, session_state=state)

    assert result[0] is not None, result
    assert state["render_outcome_kind"] is KIND.SUCCESS
    assert state["last_output_path"].endswith(f"clean_{_FROZEN_STAMP}.mp4")
    assert os.path.exists(state["last_output_path"])
    assert calls == ["analyze_beats_auto", "create_music_video"], calls


# ===========================================================================
# The batch loop: the FULL class survives, and the batch still stops
# ===========================================================================


class _BatchRun:
    """What one real batch execution actually produced.

    `captured` holds the genuine `RenderCandidateOutcome` objects the batch body constructed -- not
    reconstructions. That distinction is the whole point of this harness: a test that builds its own
    outcome afterwards and asserts on *that* passes even when the loop discarded the class, which is
    exactly the gap R2 closes.
    """

    def __init__(self):
        self.captured = []
        self.attempted = 0
        self.final = None

    @property
    def kinds(self):
        return [o.outcome_kind for o in self.captured]


def _run_batch(kinds, durables):
    """Execute the REAL `render_selected_variants_guarded`, capturing every outcome it constructs.

    Each entry of `kinds` is the class the candidate's render writes onto `RENDER_OUTCOME_KEY`, and
    `durables` whether it produced a durable file. The namespace receives a thin PROXY around
    `fork_render_batch` whose `RenderCandidateOutcome` records each constructed instance and then
    delegates to the real class unchanged -- so the model's own `__post_init__` validation still
    runs, and a contradiction still raises out of this call.
    """
    import threading
    import uuid as _uuid

    run = _BatchRun()

    class _Skip:
        def __repr__(self):
            return "<skip>"

    def capturing_outcome(*args, **kwargs):
        # The REAL class, constructed with the REAL arguments the batch body passed. If
        # `__post_init__` raises, it raises here and propagates -- nothing is swallowed.
        outcome = fork_render_batch.RenderCandidateOutcome(*args, **kwargs)
        run.captured.append(outcome)
        return outcome

    proxy = types.SimpleNamespace(
        RenderCandidateOutcome=capturing_outcome,
        RenderBatchOutcome=fork_render_batch.RenderBatchOutcome,
        build_request=fork_render_batch.build_request,
    )

    def guarded_unlocked(*args, **kwargs):
        index = run.attempted
        run.attempted += 1
        session_state = args[24]

        def stream():
            session_state["render_outcome_kind"] = kinds[index]
            session_state["last_output_path"] = (
                f"C:/output/candidate{index}.mp4" if durables[index] else "")
            session_state["audio_layers_report"] = ""
            session_state["smart_mix_report"] = ""
            yield (None, f"status {index}", session_state, "", "", "inv")
        return stream()

    batch = _root_92_batch(count=2)
    namespace = {
        "gr": types.SimpleNamespace(skip=_Skip),
        "uuid": _uuid,
        "threading": threading,
        "fork_render_batch": proxy,
        "RenderLifecycle": fork_render_worker.RenderLifecycle,
        "RenderLifecycleState": fork_render_worker.RenderLifecycleState,
        "RenderOutcomeKind": KIND,
        "RenderCancelled": RenderCancelled,
        "RENDER_OUTCOME_KEY": "render_outcome_kind",
        "LAST_OUTPUT_PATH_KEY": "last_output_path",
        "AUDIO_LAYERS_REPORT_KEY": "audio_layers_report",
        "SMART_MIX_REPORT_KEY": "smart_mix_report",
        "RENDER_BUSY_MESSAGE": "busy",
        "_RENDER_LOCK": threading.Lock(),
        "_install_active_render": lambda lifecycle: None,
        "_clear_active_render": lambda invocation_id: None,
        "_render_batch_request_tag": lambda: "T",
        "_process_video_guarded_unlocked": guarded_unlocked,
        "Iterator": None, "Tuple": None, "VideoFilesInput": None,
    }
    exec(compile(ast.Module(body=[_gui_func("render_selected_variants_guarded")],
                            type_ignores=[]), "<gui>", "exec"), namespace)

    session_state = {}
    stream = namespace["render_selected_variants_guarded"](
        batch, [0, 1], "track.mp3", None, 2.0, 1.0, True, "", fork_smart_mix.ROLE_ORDER,
        "folder", "C:/src", False, None, "music_video", "h264", 30.0, session_state, None)
    for run.final in stream:
        pass
    return run


def test_the_capture_harness_really_observes_the_batch_body():
    """Self-check. If the proxy were never reached, every assertion below would be vacuous."""
    run = _run_batch([KIND.SUCCESS, KIND.SUCCESS], [True, True])
    assert run.attempted == 2, "both candidates must have been rendered"
    assert len(run.captured) == 2, "the batch body must have constructed two outcomes"
    assert all(isinstance(o, fork_render_batch.RenderCandidateOutcome) for o in run.captured)
    assert "2 / 2 succeeded" in run.final[1], run.final[1]


@pytest.mark.parametrize("kind", [
    KIND.CANDIDATE_LOCAL,
    KIND.SHARED_FATAL,
    KIND.UNKNOWN_FATAL,
])
def test_the_batch_preserves_every_failure_class_on_the_candidate_outcome(kind):
    """The producer's class must arrive on the ACTUAL outcome the batch body built.

    R1A recorded `CANCELLED if cancelled else None`, so these three were thrown away; R1 then
    re-introduced a disagreement filter that could substitute `None` again. This asserts the
    captured object, so neither shape can pass.
    """
    run = _run_batch([kind, None], [False, False])
    assert run.attempted == 1, "the batch must stop after the first failed candidate"
    assert len(run.captured) == 1
    assert run.captured[0].outcome_kind is kind, \
        f"the batch published {run.captured[0].outcome_kind} instead of the producer's {kind}"
    assert run.captured[0].success is False
    assert "not attempted" in run.final[3], run.final[3]


def test_the_batch_preserves_cancelled_on_the_candidate_outcome():
    run = _run_batch([KIND.CANCELLED, None], [False, False])
    assert run.attempted == 1
    assert run.captured[0].outcome_kind is KIND.CANCELLED
    assert run.captured[0].cancelled is True
    assert "CANCELLED" in run.final[3], run.final[3]


def test_the_batch_preserves_success_on_the_candidate_outcome():
    run = _run_batch([KIND.SUCCESS, KIND.SUCCESS], [True, True])
    assert run.attempted == 2
    assert run.kinds == [KIND.SUCCESS, KIND.SUCCESS]
    assert all(o.success for o in run.captured)
    assert [o.durable_output_path for o in run.captured] == \
        ["C:/output/candidate0.mp4", "C:/output/candidate1.mp4"]


# ---------------------------------------------------------------------------
# An explicit contradiction must be LOUD, not laundered
# ---------------------------------------------------------------------------
#
# None of these can occur on a normal production path: the durable promotion is the sole success
# authority and every producer is required to write a matching class. If one DOES occur, a producer
# has violated its contract -- and silently replacing its class with a derived one would hide that
# and could make C3-R1B-b continue (or stop) on a class nobody verified.
#
#     explicit contradiction          ->  ValueError, out of the batch
#     no explicit classification      ->  conservative derivation by the model
#
# That asymmetry is the contract, and these cases are what hold it.


def test_an_explicit_success_without_a_durable_path_raises():
    """CASE 1. SUCCESS is written only after the promotion, so this means a broken producer."""
    with pytest.raises(ValueError, match="disagrees with outcome_kind"):
        _run_batch([KIND.SUCCESS, None], [False, False])


@pytest.mark.parametrize("kind", [
    KIND.CANDIDATE_LOCAL,
    KIND.SHARED_FATAL,
    KIND.UNKNOWN_FATAL,
    KIND.CANCELLED,
])
def test_an_explicit_failure_class_with_a_durable_path_raises(kind):
    """CASE 2 and its siblings. A durable file under a failure class is a contradiction."""
    with pytest.raises(ValueError, match="disagrees with outcome_kind"):
        _run_batch([kind, None], [True, False])


def test_the_contradiction_is_not_swallowed_into_a_derived_class():
    """The negative that matters: the batch must NOT answer with a plausible outcome instead.

    R1's filter made case 1 publish `UNKNOWN_FATAL` and case 2 publish `SUCCESS` -- both of them
    classes the producer never named.
    """
    for kinds, durables in (([KIND.SUCCESS, None], [False, False]),
                            ([KIND.CANDIDATE_LOCAL, None], [True, False])):
        try:
            run = _run_batch(kinds, durables)
        except ValueError:
            continue
        raise AssertionError(
            f"a contradiction was laundered into {run.kinds!r} instead of raising")


def test_no_explicit_classification_falls_back_to_the_conservative_derivation():
    """The one case that genuinely proves nothing: the producer never classified itself.

    Here -- and only here -- the model's derivation is correct, and it is the model's, not the
    batch's: SUCCESS from a durable path, UNKNOWN_FATAL otherwise.
    """
    run = _run_batch([None, None], [False, False])
    assert run.attempted == 1
    assert run.captured[0].outcome_kind is KIND.UNKNOWN_FATAL
    assert run.captured[0].success is False

    run = _run_batch([None, None], [True, True])
    assert run.attempted == 2
    assert run.kinds == [KIND.SUCCESS, KIND.SUCCESS]
    assert all(o.success for o in run.captured)


@pytest.mark.parametrize("kind", [
    KIND.CANDIDATE_LOCAL,
    KIND.SHARED_FATAL,
    KIND.UNKNOWN_FATAL,
    KIND.CANCELLED,
])
def test_every_failure_class_still_stops_the_batch(kind):
    """**The R1B-a negative guard, measured.** Classification changed; policy did not.

    A CANDIDATE_LOCAL candidate must NOT be followed by another render. If this ever passes with
    `attempted == 2`, continue-after-failure arrived without authorization.
    """
    run = _run_batch([kind, None], [False, False])
    assert run.attempted == 1, f"{kind} did not stop the batch -- R1B-b arrived early"
    assert len(run.captured) == 1, "a second candidate outcome was recorded"


def test_the_batch_loop_does_not_branch_on_the_candidate_class():
    """Structural half of the same guard: the stop condition must be class-blind.

    The loop reads the class to RECORD it. The moment it reads the class to DECIDE whether to carry
    on, that is R1B-b.
    """
    node = _gui_func("render_selected_variants_guarded")
    loop = next(n for n in ast.walk(node) if isinstance(n, ast.For))
    stop = next(n for n in ast.walk(loop) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "candidate_cancelled or not durable")
    assert "break" in ast.unparse(stop)
    assert "stopped = True" in ast.unparse(stop)
    # no `continue` anywhere in the candidate loop, and no test against a specific failure class
    rendered = ast.unparse(loop)
    assert "continue" not in rendered, "a continue policy was added to the candidate loop"
    for forbidden in ("is RenderOutcomeKind.CANDIDATE_LOCAL",
                      "is RenderOutcomeKind.SHARED_FATAL",
                      "is RenderOutcomeKind.UNKNOWN_FATAL"):
        assert forbidden not in rendered, \
            f"the loop branches on {forbidden} -- that is R1B-b"


def test_render_selection_size_is_still_exactly_two():
    """R1B-a must not move the count, introduce a range, or add min/max."""
    assert fork_render_batch.RENDER_SELECTION_SIZE == 2
    source = open(os.path.join(os.path.dirname(_MIXDOWN), "beatsync_fork", "render_batch.py"),
                  encoding="utf-8").read()
    for forbidden in ("RENDER_SELECTION_MIN", "RENDER_SELECTION_MAX", "RENDER_SELECTION_DEFAULT"):
        assert forbidden not in source, f"render_batch.py declares {forbidden} -- that is R1B-b"
    # the selection contract still refuses any count other than two
    assert fork_render_batch.normalize_selection([0, 1], 4) == (0, 1)
    for rejected in ([0], [0, 1, 2], [0, 1, 2, 3], []):
        assert fork_render_batch.normalize_selection(rejected, 4) == ()


# ===========================================================================
# No prose parsing, anywhere
# ===========================================================================


def test_no_outcome_class_is_derived_from_a_status_or_error_string():
    """Every class comes from an exception TYPE or a batch-frozen value -- never from text."""
    source = _gui_source()
    for forbidden in (
        "in last_status", "in status_msg", "in error_msg", "in promotion_error",
        "last_status.startswith", "status.startswith", "str(exc) ==",
        "'Cancelled' in", "'❌' in", ".lower() ==",
    ):
        assert forbidden not in source, f"gui.py appears to parse prose: {forbidden}"
    # and every assignment to the key names an enum member or None explicitly
    assigns = [ast.unparse(n.value) for n in ast.walk(_gui_tree())
               if isinstance(n, ast.Assign) and n.targets
               and ast.unparse(n.targets[0]) == "session_state[RENDER_OUTCOME_KEY]"]
    assert assigns, "no assignment to RENDER_OUTCOME_KEY was found at all"
    for rendered in assigns:
        assert (rendered == "None"
                or rendered.startswith("RenderOutcomeKind.")
                or rendered == "promotion_kind or RenderOutcomeKind.UNKNOWN_FATAL"
                or rendered == "candidate_kind"), rendered


def test_shared_fatal_now_has_real_producers_in_the_module():
    """The statement this milestone exists to make false: "SHARED_FATAL has no producer"."""
    source = _gui_source()
    assert source.count("RenderOutcomeKind.SHARED_FATAL") >= 5, \
        "SHARED_FATAL must be produced by the gate refusal, the six shared inputs, the voice " \
        "preflight, a plan error and an EXDEV promotion"
    assert "RenderOutcomeKind.CANDIDATE_LOCAL" in source
    assert "RenderOutcomeKind.UNKNOWN_FATAL" in source


# ===========================================================================
# Helpers that read the real `gui.py` handlers
# ===========================================================================


def _innermost_try_around(call_name):
    """The SMALLEST `ast.Try` whose *body* contains `call_name`.

    `ast.walk` yields the whole-function `try:` first, and that one contains every call in the
    function -- so picking the first match would always answer the generic handler and these tests
    would assert nothing. Size-ordering is what makes each assertion land on the clause that
    actually guards the call.
    """
    impl = _gui_func("_process_video_impl")
    matches = [
        node for node in ast.walk(impl)
        if isinstance(node, ast.Try)
        and call_name in ast.unparse(ast.Module(body=node.body, type_ignores=[]))
    ]
    assert matches, f"no try/except found around {call_name}"
    return min(matches, key=lambda n: len(ast.unparse(ast.Module(body=n.body,
                                                                 type_ignores=[]))))


def _audio_preflight_handler(call_name):
    """The `except` clause guarding one preflight call, rendered."""
    node = _innermost_try_around(call_name)
    return "\n".join(ast.unparse(h) for h in node.handlers)


def _mixdown_handler_clauses():
    """`[(clause type, rendered handler)]` for the try around `build_mixed_master`, in order."""
    node = _innermost_try_around("audio_mixdown.build_mixed_master")
    return [(ast.unparse(h.type) if h.type else "BARE", ast.unparse(h)) for h in node.handlers]
