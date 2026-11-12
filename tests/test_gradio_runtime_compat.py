"""OPT-IN runtime compatibility gate for the pinned Gradio, and a real `gr.Blocks` build.

Why this file exists
--------------------

`src/gui.py` cannot be imported on a bare interpreter, so every other assertion in this suite
about a Gradio widget is an **AST assertion about source text**. That is exactly how R14-A A6
shipped `gr.Textbox(show_copy_button=True)` to `main`: the parameter was removed in Gradio 6 in
favour of `buttons`, the pinned `gradio==6.19.0` raises

    TypeError: Textbox.__init__() got an unexpected keyword argument 'show_copy_button'

while `create_ui()` is building the interface, and **the whole application failed to start** --
with 215 green source-level tests saying nothing at all. A source assertion that pins one literal
keyword name cannot catch the next member of that class. Only the real library can.

So this file follows the repository's existing opt-in convention (`tests/test_audio_mixdown.py`
runs real FFmpeg when `BEATSYNC_TEST_FFMPEG` is set, and skips otherwise) and adds two things a
bare interpreter cannot do:

1. **A keyword/signature census.** Every syntactically identified ``gr.*`` call in `src/gui.py` is
   resolved against the installed library and each explicit keyword is checked against the real
   `inspect.signature`. Unknown keywords and unknown attributes FAIL. Anything the census could
   not check -- a `**` unpacking, a signature that will not introspect, a callee that accepts
   arbitrary keywords -- is reported by name and **never counted as a pass**.
2. **One real `gr.Blocks` construction.** `create_ui()` is built in a child process, in a
   task-local synthetic environment, and the resulting component graph is inspected.

What this is NOT
----------------

A build is **not** a live-server smoke and **not** user acceptance. Nothing here calls
`.launch()`, binds a port, starts a server, makes a network request, touches the operator's real
per-user configuration, or reads any shared analysis-cache folder. A passing build proves the
interface can be constructed on the pinned library; it proves nothing about what the interface
does once a browser is attached, and it must never be reported as if it did.

How it is gated
---------------

``BEATSYNC_TEST_GRADIO=1`` opts in. **Absent, the whole module skips.** Present, an absent or
mismatched `gradio==6.19.0` is a **failure, not a skip** -- the point of the gate is to be told
when the runtime is not the pinned one, and a gate that skips in that case would report the same
green as a bare interpreter. Nothing here is mocked: a stand-in `gradio` would make every
assertion below vacuous, so the module checks that the module it imported is the real library
with a real version string.

Running it (Windows, the portable runtime, from the repository root)::

    set BEATSYNC_TEST_GRADIO=1
    bin\\python-3.13.14-embed-amd64\\python.exe -m pytest tests/test_gradio_runtime_compat.py

The portable interpreter carries `gradio` but not `pytest`; one way to supply it without
installing anything into `bin/` is to append a pytest-bearing `site-packages` to `sys.path` for
that one run. The child process uses `sys.executable`, so whichever interpreter runs the suite is
the one that builds the interface -- the same deliberate choice `tests/test_qwen_stream.py` makes.

Side effect worth stating: importing `src/paths.py` creates `input/*` and `output/` under the tree
being built, because that is what importing it does in the application too. The child builds the
**repository's own** tree and asserts the `gui` module it imported is that tree's file, so nothing
resolves to another checkout.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_GUI = os.path.join(_SRC, "gui.py")

#: The opt-in. Exactly ``"1"``, so a stray empty string or a leftover ``0`` does not enable a
#: gate that imports a 100-MB dependency tree and builds a whole interface.
OPT_IN = "BEATSYNC_TEST_GRADIO"
#: The pinned version, from `requirements.txt`. A mismatch is a failure when opted in.
PINNED = "6.19.0"
#: The parameter Gradio 6 removed, spelled in two halves. This module names it in prose in order
#: to explain it, and `tests/test_library_inventory_panel.py` scans `gui.py`'s AST for it -- the
#: "naming a ban trips it" trap, which this repository has hit repeatedly.
REMOVED_PARAM = "show_" + "copy_button"
REPLACEMENT_PARAM = "buttons"

HANDLER = "_on_library_inventory_click"
#: Keyed on `elem_id` rather than on a label, deliberately: labels are user-facing prose and are
#: reworded under review, while an `elem_id` is a stable identity.
A6_REPORT_BOX = "library-cache-report-box"
F3_FOLDER_BOX = "library-inventory-folder"
F3_RECORDS_BOX = "library-inventory-records"
#: R14-B S1: the second default-off tick.
F3_SOURCES_BOX = "library-inventory-sources"
F3_BUTTON = "library-inventory-button"
F3_REPORT_BOX = "library-inventory-box"
F3_ELEM_IDS = (F3_FOLDER_BOX, F3_RECORDS_BOX, F3_SOURCES_BOX, F3_BUTTON, F3_REPORT_BOX)

pytestmark = pytest.mark.skipif(
    os.environ.get(OPT_IN) != "1",
    reason=f"set {OPT_IN}=1 to run the real-Gradio compatibility gate (needs gradio=={PINNED})")


# ---------------------------------------------------------------------------
# the library, demanded rather than hoped for
# ---------------------------------------------------------------------------

def _gradio():
    """The real pinned library, or a FAILURE.

    Imported lazily so that collection on a bare interpreter cannot raise: the module-level
    `pytestmark` skip is applied per test, after the module body has already run.
    """
    try:
        import gradio
    except Exception as exc:                                           # noqa: BLE001
        pytest.fail(f"{OPT_IN}=1 opts in to the real-Gradio gate, but importing gradio failed: "
                    f"{type(exc).__name__}: {exc}. That is a failure, not a skip: an opted-in "
                    "run with no library proves nothing, and skipping would report the same "
                    "green a bare interpreter does.")
    version = getattr(gradio, "__version__", None)
    assert isinstance(version, str) and version, (
        "the imported `gradio` has no version string, so it is not the real library")
    assert version == PINNED, (
        f"this gate is measured against the pinned gradio=={PINNED}, but the runtime carries "
        f"{version}. A different version is a FAILURE rather than a skip: its signatures are not "
        "the ones `src/gui.py` is pinned against.")
    for attribute in ("Blocks", "Textbox", "Checkbox", "Button", "Accordion"):
        assert hasattr(gradio, attribute), (
            f"the imported `gradio` has no {attribute}; a stand-in module would make every "
            "assertion in this file vacuous")
    return gradio


# ---------------------------------------------------------------------------
# the census
# ---------------------------------------------------------------------------

def _resolve(gradio, dotted: str):
    """Resolve ``gr.a.b`` against the real module. Returns ``(target, '')`` or ``(None, reason)``."""
    parts = dotted.split(".")
    assert parts[0] == "gr"
    target = gradio
    for part in parts[1:]:
        if not hasattr(target, part):
            return (None, f"{type(target).__name__} has no attribute {part!r}")
        target = getattr(target, part)
    return (target, "")


def _signature(target):
    """``(signature, drops_self, '')`` or ``(None, False, reason)``."""
    if inspect.isclass(target):
        try:
            return (inspect.signature(target.__init__), True, "")
        except (TypeError, ValueError) as exc:
            return (None, True, f"{type(exc).__name__}: {exc}")
    if not callable(target):
        return (None, False, "the resolved attribute is not callable")
    try:
        return (inspect.signature(target), False, "")
    except (TypeError, ValueError) as exc:
        return (None, False, f"{type(exc).__name__}: {exc}")


#: An invented keyword no Gradio callee can legitimately have. Used to MEASURE whether a callee
#: that declares ``**kwargs`` actually accepts an unknown keyword.
INVENTED = "definitely_not_a_real_gradio_parameter"


def probe_permissive(gradio, target) -> str:
    """Does this callee really accept an unknown keyword? ``'permissive'``/``'strict'``/a reason.

    **A declared ``**kwargs`` is NOT proof of permissiveness**, and that is measured rather than
    assumed: on the pinned 6.19.0 ``gr.Blocks`` declares ``**kwargs`` and still raises
    ``TypeError`` for an unknown keyword, because it forwards into ``BlockContext.__init__``. A
    census that exempted every ``**kwargs`` callee would therefore have stopped checking the one
    call that builds the whole interface. So the question is put to the library.
    """
    try:
        with gradio.Blocks():
            target(**{INVENTED: 1})
    except TypeError as exc:
        return "strict" if INVENTED in str(exc) else f"unmeasurable: TypeError: {exc}"
    except Exception as exc:                                           # noqa: BLE001
        return f"unmeasurable: {type(exc).__name__}: {exc}"
    return "permissive"


def probe_keyword(gradio, target, name: str) -> str:
    """Would the real library reject this keyword? ``'rejected'`` or ``'accepted'``.

    Asked of the library rather than inferred, because the signature alone is not the answer in
    either direction on 6.19.0: `gr.Blocks` declares ``**kwargs`` yet rejects an invented keyword,
    and it nevertheless accepts `theme` and `css`, which are not among its named parameters.

    Anything that is not an unexpected-keyword `TypeError` counts as accepted. A value-validation
    error means the keyword reached the callee, which is the only question being put.
    """
    try:
        with gradio.Blocks():
            target(**{name: None})
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" in message and name in message:
            return "rejected"
    except Exception:                                                  # noqa: BLE001, S110
        pass
    return "accepted"


def census(source: str, gradio, label: str = "<source>") -> dict:
    """Check every ``gr.*`` call's explicit keywords against the installed library.

    **Nothing is classified as a pass by omission.** A keyword absent from the callee's signature
    is not waved through and not failed on sight: the library is asked whether it would reject it,
    and the pair is recorded by name either way. Unresolvable attributes, unintrospectable
    signatures and `**` unpackings each land in their own list, and the caller asserts on all of
    them.
    """
    unknown_keyword: list = []
    unknown_attribute: list = []
    unchecked: list = []
    dynamic: list = []
    runtime_accepted: set = set()
    calls = by_signature = by_runtime = 0
    probed: dict = {}

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        dotted = ast.unparse(node.func)
        if not dotted.startswith("gr."):
            continue
        calls += 1
        where = f"{label}:{node.lineno} {dotted}"
        target, problem = _resolve(gradio, dotted)
        if problem:
            unknown_attribute.append(f"{where} -- {problem}")
            continue
        signature, drops_self, problem = _signature(target)
        if problem:
            unchecked.append(f"{where} -- {problem}")
            continue
        parameters = dict(signature.parameters)
        if drops_self:
            parameters.pop("self", None)
        for keyword in node.keywords:
            if keyword.arg is None:
                dynamic.append(f"{where} -- a ** unpacking, whose keys are not visible in the "
                               "source at all")
                continue
            if keyword.arg in parameters and parameters[keyword.arg].kind is not (
                    inspect.Parameter.VAR_KEYWORD):
                by_signature += 1
                continue
            pair = (dotted, keyword.arg)
            if pair not in probed:
                probed[pair] = probe_keyword(gradio, target, keyword.arg)
            if probed[pair] == "rejected":
                unknown_keyword.append(f"{where} -- {keyword.arg!r}")
            else:
                by_runtime += 1
                runtime_accepted.add(f"{dotted}.{keyword.arg}")

    return {"unknown_keyword": unknown_keyword, "unknown_attribute": unknown_attribute,
            "unchecked": unchecked, "dynamic": dynamic,
            "runtime_accepted": sorted(runtime_accepted), "calls": calls,
            "by_signature": by_signature, "by_runtime": by_runtime}


def test_a1_the_opt_in_demands_the_real_pinned_library():
    """The gate's own precondition, asserted first so a later failure cannot be misread."""
    gradio = _gradio()
    assert gradio.__version__ == PINNED
    assert os.path.isfile(_GUI), _GUI


def test_a2_the_removed_parameter_really_is_gone_and_really_does_raise():
    """The measured incompatibility, reproduced against the real library rather than described.

    Two halves, and the second is what a source assertion cannot do: the signature no longer
    carries the parameter, AND constructing the widget with it raises the exact `TypeError` that
    stopped `create_ui()` on pristine `main`.
    """
    gradio = _gradio()
    parameters = inspect.signature(gradio.Textbox.__init__).parameters
    assert REMOVED_PARAM not in parameters, (
        f"gradio {PINNED} unexpectedly accepts {REMOVED_PARAM!r}; this gate's premise needs "
        "re-measuring")
    assert REPLACEMENT_PARAM in parameters, (
        f"gradio {PINNED} has no {REPLACEMENT_PARAM!r} parameter either; the replacement this "
        "repository standardised on is not available")
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()), (
        "gr.Textbox accepts arbitrary keywords, so an unknown one would NOT raise and the "
        "census below would be unable to prove anything about it")
    with gradio.Blocks():
        with pytest.raises(TypeError) as caught:
            gradio.Textbox(label="x", **{REMOVED_PARAM: True})
    assert REMOVED_PARAM in str(caught.value), str(caught.value)


#: The callees measured to accept arbitrary keywords on the pinned 6.19.0. `gr.Blocks` declares
#: `**kwargs` and is deliberately NOT here, because it still rejects an invented keyword.
PERMISSIVE_CALLEES = ("gr.HTML", "gr.update")

#: Measured on 6.19.0: every ``callee.keyword`` pair in `src/gui.py` that the signature does not
#: declare and the library accepts anyway. Six pairs, covering 43 of the file's 1226 explicit
#: keywords; the other 1183 are validated against a real signature, and 1183 + 43 = 1226 is the
#: whole surface, which is why neither half can quietly stop being counted.
RUNTIME_ACCEPTED_PAIRS = ("gr.Blocks.css", "gr.Blocks.theme", "gr.update.choices",
                          "gr.update.interactive", "gr.update.value", "gr.update.variant")


def test_b1_every_gradio_keyword_in_gui_py_is_real():
    """The census over the shipped `src/gui.py`, with nothing unknown allowed to pass silently."""
    gradio = _gradio()
    with open(_GUI, "r", encoding="utf-8") as handle:
        result = census(handle.read(), gradio, label="src/gui.py")
    print(f"\ngradio {gradio.__version__}: {result['calls']} gr.* calls; "
          f"{result['by_signature']} keywords validated against a signature; "
          f"{result['by_runtime']} accepted only by the runtime, on "
          f"{result['runtime_accepted']}; {len(result['unchecked'])} unchecked; "
          f"{len(result['dynamic'])} dynamic")
    assert result["calls"] > 100, result["calls"]
    assert result["by_signature"] > 500, result["by_signature"]
    assert result["unknown_attribute"] == [], result["unknown_attribute"]
    assert result["unknown_keyword"] == [], result["unknown_keyword"]
    # Reported by name, never silently treated as a pass.
    assert result["unchecked"] == [], result["unchecked"]
    assert result["dynamic"] == [], result["dynamic"]
    # Pinned BY NAME rather than waved through by count. Every pair here is a keyword the
    # signature does not declare and the library nevertheless accepts; a new entry means a
    # keyword stopped being statically describable and somebody has to read it.
    assert result["runtime_accepted"] == list(RUNTIME_ACCEPTED_PAIRS), (
        f"{result['runtime_accepted']} vs the pinned {list(RUNTIME_ACCEPTED_PAIRS)}")


def test_b1a_a_declared_kwargs_is_not_taken_as_permission():
    """The measured subtlety this census exists to respect.

    `gr.Blocks` declares `**kwargs` and nevertheless rejects an invented keyword, so inferring
    permissiveness from the signature would have exempted the one call that builds the entire
    interface. `gr.update` and `gr.HTML` genuinely do accept one.
    """
    gradio = _gradio()
    assert probe_permissive(gradio, gradio.Blocks) == "strict", (
        "gr.Blocks now accepts arbitrary keywords; the census's probes need re-measuring, and so "
        "does this assertion")
    for name in PERMISSIVE_CALLEES:
        target, problem = _resolve(gradio, name)
        assert not problem, (name, problem)
        assert probe_permissive(gradio, target) == "permissive", name
    # And the keyword-level probe agrees with the construction that actually happens: the two
    # keywords `gr.Blocks` accepts without declaring are accepted, while an invented one is not.
    for accepted in ("theme", "css"):
        assert probe_keyword(gradio, gradio.Blocks, accepted) == "accepted", accepted
    assert probe_keyword(gradio, gradio.Blocks, INVENTED) == "rejected"


def test_b2_the_census_flags_a_planted_unsupported_keyword():
    """Positive control. Without it, `test_b1` passing would be indistinguishable from a census
    that checks nothing at all."""
    gradio = _gradio()
    planted = census(f"gr.Textbox(label='x', {REMOVED_PARAM}=True)", gradio, label="<planted>")
    assert len(planted["unknown_keyword"]) == 1, planted
    assert REMOVED_PARAM in planted["unknown_keyword"][0], planted


def test_b3_the_census_flags_an_unknown_attribute_and_a_dynamic_unpacking():
    """Two more controls, for the two classifications that must not read as a pass."""
    gradio = _gradio()
    missing = census("gr.ThisWidgetDoesNotExist(label='x')", gradio, label="<planted>")
    assert len(missing["unknown_attribute"]) == 1, missing
    assert missing["unknown_keyword"] == [], missing
    starred = census("gr.Textbox(**options)", gradio, label="<planted>")
    assert len(starred["dynamic"]) == 1, starred


# ---------------------------------------------------------------------------
# the real build, in a child process
# ---------------------------------------------------------------------------

#: The child. A separate process deliberately: `LOCALAPPDATA`, `BEATSYNC_SHARED_CONFIG` and
#: `GRADIO_TEMP_DIR` have to be redirected BEFORE `src/gui.py` is imported, `gui` import order is
#: load-bearing and `setup_environment()` mutates `PATH`/`CUDA_PATH` and reconfigures the
#: streams -- none of which belongs in the process running the rest of the suite.
_CHILD = r'''
import json, os, sys, traceback
primary, scratch = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
extra = [os.path.abspath(p) for p in sys.argv[3:]]
os.environ["LOCALAPPDATA"] = os.path.join(scratch, "localappdata")
os.environ["BEATSYNC_SHARED_CONFIG"] = os.path.join(scratch, "shared-config.json")
os.environ["GRADIO_TEMP_DIR"] = os.path.join(scratch, "gradio-temp")
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
for d in (os.environ["LOCALAPPDATA"], os.environ["GRADIO_TEMP_DIR"]):
    os.makedirs(d, exist_ok=True)
sys.path[:0] = [primary] + extra
out = {"primary": primary, "extra": extra,
       "shared_config_env": os.environ["BEATSYNC_SHARED_CONFIG"],
       "localappdata_env": os.environ["LOCALAPPDATA"]}
try:
    import gradio
    out["gradio_version"] = gradio.__version__
    import gui
    out["gui_file"] = os.path.abspath(gui.__file__)
    out["gui_is_pinned_tree"] = (os.path.normcase(os.path.abspath(gui.__file__))
                                 == os.path.normcase(os.path.join(primary, "gui.py")))
    out["render_concurrency_id"] = gui.RENDER_CONCURRENCY_ID
    blocks = gui.create_ui()
    out["built"] = True
    out["block_count"] = len(blocks.blocks)
    out["dependency_count"] = len(blocks.fns)
    components = {}
    for block in blocks.blocks.values():
        elem_id = getattr(block, "elem_id", None)
        if not elem_id:
            continue
        components[elem_id] = {
            "type": type(block).__name__,
            "label": getattr(block, "label", None),
            "value": getattr(block, "value", None) if isinstance(
                getattr(block, "value", None), (str, bool, int, float, type(None))) else "<other>",
            "interactive": getattr(block, "interactive", "ATTR-ABSENT"),
            "lines": getattr(block, "lines", "ATTR-ABSENT"),
            "buttons": list(getattr(block, "buttons", []) or []),
        }
    out["components"] = components
    lanes = {}
    deps = []
    for key, dep in blocks.fns.items():
        lane = getattr(dep, "concurrency_id", None)
        lanes[str(lane)] = lanes.get(str(lane), 0) + 1
        if getattr(getattr(dep, "fn", None), "__name__", None) == sys.argv_handler:
            deps.append({
                "inputs": [getattr(c, "elem_id", None) for c in dep.inputs],
                "outputs": [getattr(c, "elem_id", None) for c in dep.outputs],
                "concurrency_id": str(lane),
                "concurrency_limit": getattr(dep, "concurrency_limit", "ATTR-ABSENT"),
                "show_progress": str(getattr(dep, "show_progress", "ATTR-ABSENT")),
            })
    out["handler_dependencies"] = deps
    out["lane_sizes"] = lanes
    accordions = []
    for block in blocks.blocks.values():
        if type(block).__name__ == "Accordion":
            accordions.append({"label": getattr(block, "label", None),
                               "open": getattr(block, "open", None)})
    out["accordions"] = accordions
    out["handler_blank_folder"] = gui._on_library_inventory_click("", False)[:6000]
    out["root_refused_constant"] = gui.F3_ROOT_REFUSED
    out["records_off_constant"] = gui.F3_RECORDS_OFF
except BaseException as exc:
    out["built"] = False
    out["error_type"] = type(exc).__name__
    out["error"] = str(exc)
    out["traceback_tail"] = traceback.format_exc().strip().splitlines()[-4:]
sys.stdout.write("<<<JSON>>>" + json.dumps(out))
'''


def _run_child(tmp_path, primary_src, *extra_src, handler=HANDLER):
    script = tmp_path / "build_child.py"
    body = _CHILD.replace("sys.argv_handler", json.dumps(handler))
    script.write_text(body, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    done = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), str(primary_src), str(scratch),
         *[str(p) for p in extra_src]],
        capture_output=True, text=True, timeout=600)
    marker = "<<<JSON>>>"
    assert marker in done.stdout, (
        f"the child produced no result payload (rc={done.returncode})\n"
        f"--- stdout ---\n{done.stdout[-3000:]}\n--- stderr ---\n{done.stderr[-3000:]}")
    return json.loads(done.stdout.split(marker, 1)[1]), done


def test_c1_create_ui_builds_the_real_interface_and_the_panel_is_what_it_claims(tmp_path):
    """One real `gr.Blocks` construction of THIS tree, inspected afterwards.

    A build, and nothing more: no server, no port, no browser, no user acceptance.
    """
    _gradio()
    result, done = _run_child(tmp_path, _SRC)
    assert result.get("built") is True, (
        f"create_ui() did not build: {result.get('error_type')}: {result.get('error')}\n"
        f"{result.get('traceback_tail')}\n--- stderr ---\n{done.stderr[-2000:]}")
    assert result["gradio_version"] == PINNED, result["gradio_version"]
    assert result["gui_is_pinned_tree"] is True, result.get("gui_file")
    # The redirect is what keeps the operator's real per-user configuration out of this run.
    assert result["shared_config_env"].startswith(str(tmp_path)), result["shared_config_env"]
    assert result["localappdata_env"].startswith(str(tmp_path)), result["localappdata_env"]

    components = result["components"]
    # The A6 regression fix, on the real library: the box exists and offers a copy button.
    a6 = components[A6_REPORT_BOX]
    assert a6["type"] == "Textbox" and a6["interactive"] is False, a6
    assert a6["buttons"] == ["copy"], a6

    for elem_id in F3_ELEM_IDS:
        assert elem_id in components, (elem_id, sorted(components))
    folder, records = components[F3_FOLDER_BOX], components[F3_RECORDS_BOX]
    sources = components[F3_SOURCES_BOX]
    button, report = components[F3_BUTTON], components[F3_REPORT_BOX]
    assert folder["type"] == "Textbox", folder
    assert records["type"] == "Checkbox" and records["value"] is False, records
    # R14-B S1: the source-state tick is default-off on the REAL built interface, not merely in
    # the source text. Both ticks are asserted the same way and for the same reason.
    assert sources["type"] == "Checkbox" and sources["value"] is False, sources
    assert button["type"] == "Button", button
    assert report["type"] == "Textbox" and report["interactive"] is False, report
    assert report["buttons"] == ["copy"], report

    panel_ids = [i for i in components if i.startswith("library-inventory-")]
    assert sorted(panel_ids) == sorted(F3_ELEM_IDS), (
        f"the panel built {len(panel_ids)} identified widgets; five and no sixth are expected")

    deps = result["handler_dependencies"]
    assert len(deps) == 1, deps
    only = deps[0]
    # Positional contract: the R14-B input is APPENDED, so the two pre-existing indices hold.
    assert only["inputs"] == [F3_FOLDER_BOX, F3_RECORDS_BOX, F3_SOURCES_BOX], only
    assert only["outputs"] == [F3_REPORT_BOX], only
    assert only["concurrency_limit"] is None, only
    assert only["concurrency_id"] != result["render_concurrency_id"], only
    assert result["lane_sizes"][only["concurrency_id"]] == 1, (
        "the panel shares a concurrency lane with another event; a full lane leaves an event "
        "QUEUED rather than letting the server answer it (the measured S2-A R3 trap)")

    closed = [a for a in result["accordions"]
              if a["label"] == components[F3_REPORT_BOX]["label"]]
    assert closed and all(a["open"] is False for a in closed), closed

    # The handler itself, on the real build: a blank folder is refused and no record file is
    # opened. Prose-INDEPENDENT -- the child hands back the panel's own constants rather than
    # letting this file retype a sentence that review may reword.
    text = result["handler_blank_folder"]
    assert result["root_refused_constant"] in text, text[:800]
    assert result["records_off_constant"] in text, text[:800]


def test_c2_the_build_child_reproduces_the_removed_parameter_end_to_end(tmp_path):
    """The defect class, reproduced through `create_ui()` rather than asserted about.

    A copy of the shipped `gui.py` with the replacement parameter turned back into the removed one
    is built the same way. It must fail with the same `TypeError` -- which is what proves
    `test_c1` is a live measurement and not a harness that reports success regardless.
    """
    _gradio()
    injected = tmp_path / "injected"
    injected.mkdir()
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    needle = f"{REPLACEMENT_PARAM}=['copy'],"
    assert source.count(needle) == 2, source.count(needle)
    (injected / "gui.py").write_text(
        source.replace(needle, f"{REMOVED_PARAM}=True,"), encoding="utf-8")
    # Only `gui.py` is copied; everything else resolves from the real `src` appended behind it.
    result, done = _run_child(tmp_path, injected, _SRC)
    assert result.get("built") is False, (
        "a tree carrying the parameter the pinned gradio removed built successfully, so this "
        f"harness cannot detect the defect it exists for\n--- stderr ---\n{done.stderr[-2000:]}")
    assert result.get("error_type") == "TypeError", result
    assert REMOVED_PARAM in result.get("error", ""), result
    # Its own 900 KB copy, removed as soon as it has served its purpose. pytest owns `tmp_path`
    # and would clean it anyway; this only keeps the footprint brief.
    shutil.rmtree(injected, ignore_errors=True)
