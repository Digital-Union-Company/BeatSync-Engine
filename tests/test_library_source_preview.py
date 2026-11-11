"""R14-B S1 — the read-only source-state preview.

What this increment actually added is a HAND-OFF, not an engine. A5
(`library_cache_report`) has always accepted `source_verdicts` and `library_roots` and has always
joined on `library_inventory.LeafVerdict.published == MISSING_CONFIRMED`; F-3 supplied neither, so
that axis answered `not assessed` on every run and no preview of possible cleanup existed. This
suite pins the hand-off and, more importantly, the four things that keep it honest:

* the second tick is **default-off** and **fail-closed**, like the first;
* it is **subordinate** — with no records read there is nothing for a source state to be about,
  and ticking it alone reaches no filesystem at all;
* only `SCOPE_IN` records are looked for, so a record whose source lives on a disconnected drive
  is never probed and is never called an orphan — the case R14-B names explicitly;
* a missing finding cannot escape unfinalised, because `classify_leaf` answers
  `MISSING_PROVISIONAL` and only `retract_subtree` settles clause (d2) — the same call that takes
  every such finding BACK if the root disappeared during the pass.

The harness is imported from `test_library_inventory_panel` deliberately. That module already
executes the real `gui.py` constants and F-3 function bodies against a controlled namespace, and
`_f3_source_verdicts` is registered in its `F3_FUNCTIONS` tuple — so these tests drive the shipped
code, and a second copy of a 300-line loader could only drift from it.

Stdlib-only, like every suite here: `gui.py` cannot be imported on a bare interpreter.
"""

import ast
import json
import os

import pytest

from test_library_inventory_panel import (  # noqa: E402  (harness reuse is the point)
    F3_FUNCTIONS,
    HANDLER,
    LINES_FN,
    SOURCES_FN,
    _code,
    _func,
    _panel_with,
    _tree,
    cfg,
    contract_module,  # noqa: F401  (imported so pytest collects it as a fixture here too)
    load_f3,
)

RECORD_A = "aabbccdd_0123456789abcdef01234567.json"
RECORD_MISSING = "bbccddee_123456789abcdef012345678.json"
RECORD_OFFLINE = "ccddeeff_23456789abcdef0123456789.json"

#: A path on a volume this machine does not have. The point of the offline record is that it is
#: never probed at all, so the test does not depend on how an absent volume answers.
OFFLINE_SOURCE = r"Z:\disconnected-drive\clip.mp4"


def _json_path(p) -> str:
    return json.dumps(str(p))


@pytest.fixture
def lib(tmp_path):
    """A library, a local cache, and three records: present, missing-in-library, offline."""
    media = tmp_path / "Library"
    local = tmp_path / "input" / "video_analysis_cache"
    shared = tmp_path / "SharedCache" / "VideoAnalysis"
    for p in (media, local, shared):
        p.mkdir(parents=True)
    (media / "a.mp4").write_bytes(b"\0" * 2048)
    (media / "b.mkv").write_bytes(b"\0" * 4096)

    def record(name, source):
        (local / name).write_text(
            '{"cache_contract": "stage5_cache_v3", "video_file": ' + _json_path(source) + "}",
            encoding="utf-8")

    record(RECORD_A, media / "a.mp4")                       # source present, in scope
    record(RECORD_MISSING, media / "gone.mp4")              # source MISSING, in scope
    record(RECORD_OFFLINE, OFFLINE_SOURCE)                  # out of scope, never probed
    return {"media": str(media), "input": str(tmp_path / "input"), "local": str(local),
            "shared": str(shared), "root": tmp_path}


def ns_for(lib, **kw):
    return load_f3(shared_config=cfg(lib["shared"], True), local_default=lib["input"], **kw)


def build(lib, *, records=True, sources=True, ns=None):
    ns = ns or ns_for(lib)
    return ns[HANDLER](lib["media"], records, sources), ns


def manifest(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in list(dirs) + list(files):
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
                out[p] = (os.path.isdir(p), st.st_size, st.st_mtime_ns)
            except OSError:
                out[p] = None
    return out


# ===========================================================================
# GROUP S1 — default-off, and fail-closed
# ===========================================================================

def test_s1_the_source_checkbox_defaults_to_false():
    """Read off the PARSED keyword: a conditional there is how a box ships ticked while the
    file still reads `value=False`."""
    panel = _panel_with()
    boxes = [n for n in ast.walk(panel) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "gr.Checkbox"]
    assert len(boxes) == 2, [ast.unparse(b) for b in boxes]
    for box in boxes:
        value = next(kw for kw in box.keywords if kw.arg == "value")
        assert isinstance(value.value, ast.Constant), ast.unparse(value.value)
        assert value.value.value is False


def test_s2_the_handler_parameter_defaults_to_false():
    node = _func(HANDLER)
    defaults = dict(zip([a.arg for a in node.args.args][-len(node.args.defaults):],
                        node.args.defaults))
    assert isinstance(defaults["inspect_sources"], ast.Constant)
    assert defaults["inspect_sources"].value is False


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "True", "false", "", [], object()])
def test_s3_only_the_identity_true_enables_the_source_pass(lib, value):
    """`is not True`, not a truthy test: a stray `1` or `'true'` arriving from anywhere must not
    start a filesystem pass the operator did not ask for."""
    text, ns = build(lib, records=True, sources=value)
    if value is True:                                       # not reachable, guards the parametrise
        pytest.skip("covered elsewhere")
    assert ns["F3_SOURCES_OFF"] in text, text[-1500:]
    assert ns["F3_SOURCES_NOTE"] not in text


def test_s4_unticked_the_pass_looks_for_no_source_file(lib):
    """The strongest form: the missing source's own path never reaches a filesystem call."""
    text, ns = build(lib, records=True, sources=False)
    touched = " ".join(str(c) for c in ns["_fs"].calls)
    assert "gone.mp4" not in touched, touched[:600]
    assert ns["F3_SOURCES_OFF"] in text


# ===========================================================================
# GROUP S2 — subordinate to the record tick
# ===========================================================================

def test_s5_sources_without_records_reports_rather_than_looks(lib):
    """Source state is a question ABOUT records. With none read there is nothing to answer, and
    the honest move is to say so rather than go looking anyway."""
    text, ns = build(lib, records=False, sources=True)
    assert ns["F3_SOURCES_NEEDS_RECORDS"] in text
    assert ns["F3_RECORDS_OFF"] in text
    touched = " ".join(str(c) for c in ns["_fs"].calls)
    for name in ("gone.mp4", "a.mp4", "video_analysis_cache"):
        assert name not in touched, (name, touched[:600])


def test_s6_the_source_section_is_always_present(lib):
    """A section that vanishes when unticked reads as "nothing to report" rather than "the
    question was not put". Every combination renders the heading."""
    for records in (False, True):
        for sources in (False, True):
            text, ns = build(lib, records=records, sources=sources)
            assert ns["F3_SOURCES_HEADING"] in text, (records, sources)


# ===========================================================================
# GROUP S3 — the hand-off actually answers, and answers conservatively
# ===========================================================================

def test_s7_with_both_ticked_a5_answers_the_source_axis(lib, contract_module):
    off, _ = build(lib, records=True, sources=False)
    on, ns = build(lib, records=True, sources=True)
    unassessed = "no finalised source verdict was supplied"
    assert unassessed in off, "the pre-R14-B wording is gone; this test would prove nothing"
    assert unassessed not in on, on[-2500:]
    assert "source confirmed missing" in on
    # A5 owns the number and its exactness. The panel must not restate either.
    assert "lower_bound" in on or "at least" in on, on[-2500:]


def test_s8_the_offline_record_is_never_probed_and_never_called_missing(lib):
    on, ns = build(lib, records=True, sources=True)
    touched = " ".join(str(c) for c in ns["_fs"].calls)
    assert "disconnected-drive" not in touched, touched[:800]
    assert "Z:\\" not in touched
    # It is reported as what it is: outside the folders inspected.
    assert "outside the folders inspected" in on, on[-2500:]


def test_s9_no_byte_total_and_no_removal_recommendation(lib, contract_module):
    on, ns = build(lib, records=True, sources=True)
    assert "No reclaimable-space figure is offered" in on
    assert ns["F3_SOURCES_NOT_A_RECOMMENDATION"] in on
    low = on.lower()
    for claim in ("safe to delete", "can be deleted", "should be deleted", "may be removed",
                  "reclaimable space:"):
        assert claim not in low, claim
    # Every mention of "obsolete" is a negation, never an assertion.
    for line in on.splitlines():
        if "obsolete" in line.lower():
            assert "not obsolete" in line.lower() or "not evidence of an obsolete" in line.lower()


# ===========================================================================
# GROUP S4 — a missing finding cannot escape unfinalised
# ===========================================================================

def test_s10_retract_subtree_is_called_and_classify_leaf_is_not_published_directly():
    """`classify_leaf` answers MISSING_PROVISIONAL. Publishing its output without
    `retract_subtree` would ship a provisional deletion claim and would also lose the
    mid-pass-disconnect guard. Pinned structurally, because the behavioural case needs a disk
    that disappears."""
    body = _code(_func(SOURCES_FN))
    assert "classify_leaf" in body
    assert "retract_subtree" in body
    # the finalised tuple, never `pending`, is what leaves the function
    returns = [ast.unparse(n) for n in ast.walk(ast.parse(body))
               if isinstance(n, ast.Return)]
    assert not any("pending" in r for r in returns), returns


def test_s11_an_unobserved_root_yields_no_verdicts_at_all(lib):
    """`positively_observed` is the gate. A root nobody managed to read cannot anchor a missing
    claim, and the pass says so rather than treating every file under it as gone."""
    ns = ns_for(lib)

    class _Root:
        positively_observed = False
        real = lib["media"]

    verdicts, notes = ns[SOURCES_FN]([], (_Root(),))
    assert verdicts == ()
    assert notes == [ns["F3_SOURCES_NO_ROOT"]]


def test_s12_no_in_scope_record_is_not_a_finding(lib):
    ns = ns_for(lib)

    class _Root:
        positively_observed = True
        real = lib["media"]

    verdicts, notes = ns[SOURCES_FN]([], (_Root(),))
    assert verdicts == ()
    assert notes == [ns["F3_SOURCES_NONE_IN_SCOPE"]]


# ===========================================================================
# GROUP S5 — bounded, and the bound is named
# ===========================================================================

def test_s13_the_ceilings_are_module_constants_not_widget_values():
    """A resource ceiling belongs in a constant, not in the operator's hands — the same rule the
    record bounds already follow. Neither constant may be reachable from a widget."""
    source = ast.unparse(_tree())
    for name in ("F3_SOURCE_MAX_RECORDS", "F3_SOURCE_MAX_DIRECTORIES"):
        assert f"{name} = " in source, name
    panel = ast.unparse(_panel_with())
    for name in ("F3_SOURCE_MAX_RECORDS", "F3_SOURCE_MAX_DIRECTORIES"):
        assert name not in panel, f"{name} is reachable from the panel"
    for banned in ("gr.Number", "gr.Slider"):
        assert banned not in panel


def test_s14_a_reached_bound_is_named_in_the_read_out(lib, contract_module):
    """With the record ceiling lowered to one, the pass must SAY it stopped rather than let the
    unexamined records read as present."""
    ns = ns_for(lib)
    ns["F3_SOURCE_MAX_RECORDS"] = 1
    text = ns[HANDLER](lib["media"], True, True)
    assert "BOUND REACHED" in text, text[-2000:]
    assert str(1) in text


def test_s14b_the_directory_ceiling_is_effective_and_named_separately(lib, contract_module):
    """The second ceiling, which `test_s14` does not reach: with the folder budget at zero the
    pass cannot list anything, and must say which bound stopped it rather than reporting the
    unexamined records as present.

    Pinned separately because the two limits fail differently -- one stops after N records, the
    other after N directory listings -- and a single test could only ever exercise whichever
    triggered first.
    """
    ns = ns_for(lib)
    ns["F3_SOURCE_MAX_DIRECTORIES"] = 0
    text = ns[HANDLER](lib["media"], True, True)
    assert ns["F3_SOURCES_BOUND_DIRECTORIES"].format(limit=0) in text, text[-2500:]
    assert ns["F3_SOURCES_BOUND_RECORDS"].format(limit=ns["F3_SOURCE_MAX_RECORDS"]) not in text, (
        "the folder bound was reported as the record bound")
    # Nothing was examined, so nothing may be published as missing or present.
    axis = [ln for ln in text.splitlines() if "source confirmed missing:" in ln]
    assert axis and "not assessed" in axis[0], axis


# ===========================================================================
# GROUP S6 — nothing is modified, on any path
# ===========================================================================

@pytest.mark.parametrize("records", [False, True])
@pytest.mark.parametrize("sources", [False, True])
def test_s15_no_filesystem_modification_on_any_combination(lib, records, sources):
    before = manifest(lib["root"])
    build(lib, records=records, sources=sources)
    after = manifest(lib["root"])
    assert before == after, {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
    }


def test_s16_the_source_pass_opens_no_file(lib):
    """It reads directory listings and file STATUS. Opening a media file would be a different and
    much more expensive promise, and this panel does not make it."""
    body = _code(_func(SOURCES_FN))
    for forbidden in ("open(", "read_text", "read_bytes", "shutil", "subprocess", "makedirs",
                      "remove", "unlink", "rename", "replace("):
        assert forbidden not in body, forbidden


def test_s17_the_helper_is_inside_the_structural_ban_tuple():
    """The bans in `test_library_inventory_panel` are only ever as wide as `F3_FUNCTIONS`."""
    assert SOURCES_FN in F3_FUNCTIONS
    declared = {n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)}
    assert {n for n in declared if n.startswith("_f3_")} == set(F3_FUNCTIONS) - {HANDLER}


# ===========================================================================
# GROUP S7 — negative controls, so the group above cannot be vacuous
# ===========================================================================

def test_s18_forcing_the_gate_on_changes_the_read_out(lib):
    """Mutation control for `test_s3`/`test_s4`: with the gate forced, the OFF text disappears
    and the source note arrives. A gate that was never load-bearing would show no difference."""
    def mutate(name, node):
        if name != "_f3_record_lines":
            return node
        clone = ast.parse(ast.unparse(node)).body[0]
        for stmt in ast.walk(clone):
            if isinstance(stmt, ast.If) and "inspect_sources" in ast.unparse(stmt.test):
                stmt.test = ast.Constant(False)             # always take the `else`
                break
        else:
            raise AssertionError("no `inspect_sources` gate found to mutate")
        return ast.fix_missing_locations(clone)

    honest = ns_for(lib)
    broken = ns_for(lib, mutations=(mutate,))
    text_honest = honest[HANDLER](lib["media"], True, False)
    text_broken = broken[HANDLER](lib["media"], True, False)
    assert honest["F3_SOURCES_OFF"] in text_honest
    assert honest["F3_SOURCES_OFF"] not in text_broken, (
        "the gate is not load-bearing; test_s3 and test_s4 would pass with it removed")
    assert honest["F3_SOURCES_NOTE"] in text_broken


def test_s19_both_gates_are_identity_tests_never_truthy_ones():
    """A truthy test is what lets a stray `1` or `'false'` start a filesystem pass.

    The two gates spell it in opposite directions and both are correct: `_f3_record_lines`
    refuses with `is not True`, while `_f3_inventory_lines` reports the subordinate case with
    `is True`. What must never appear is a bare `if inspect_sources:`.
    """
    seen = 0
    for fn in (LINES_FN, "_f3_record_lines"):
        body = _code(_func(fn))
        if "inspect_sources" not in body:
            continue
        seen += 1
        assert ("inspect_sources is not True" in body
                or "inspect_sources is True" in body), fn
        for truthy in ("if inspect_sources:", "if inspect_sources and",
                       "if not inspect_sources:", "bool(inspect_sources)"):
            assert truthy not in body, (fn, truthy)
    assert seen == 2, f"only {seen} gate(s) carried the parameter; the pin would be weak"


# ===========================================================================
# GROUP S8 — clause (d2) is settled on an END-OF-PASS observation (finding F-7)
# ===========================================================================
#
# The independent review of this increment found that `test_s10` above pinned the *call* to
# `retract_subtree` and stopped there -- and that the call alone is not the mid-pass-disconnect
# guard. `retract_subtree` reads `RootState.positively_observed`, which is a frozen field on a
# snapshot, so settling clause (d2) against the root `inspect_library` probed BEFORE any record
# was read says only that the folder was there when the read-out STARTED. `F3_SOURCES_NOTE`
# promised the operator the stronger thing in words. The tests below are what make it true.

def _reobservation_spy(monkeypatch, *, verdict):
    """Patch A1 so a probe taken AFTER the first listing answers ``verdict``.

    The real `probe_root` still answers the inventory's own opening probe, so the fixture library
    is observed honestly; only a *later* observation is substituted. That is exactly the window
    F-7 is about, and it is the only way to reach it without a disk to unplug.
    """
    from beatsync_fork import library_inventory as fork_inventory

    real_probe = fork_inventory.probe_root
    real_list = fork_inventory.list_directory
    seen = {"listings": 0, "late_probes": 0}

    def spy_list(directory, **kw):
        seen["listings"] += 1
        return real_list(directory, **kw)

    def spy_probe(path, **kw):
        if seen["listings"]:
            seen["late_probes"] += 1
            return fork_inventory.RootState(
                typed=str(path), real=os.path.realpath(str(path)), anchor="", verdict=verdict,
                anchor_reachable=False, exists=True,
                detail="injected: this root was re-observed after the leaf evidence")
        return real_probe(path, **kw)

    monkeypatch.setattr(fork_inventory, "list_directory", spy_list)
    monkeypatch.setattr(fork_inventory, "probe_root", spy_probe)
    return seen


def test_s20_the_root_is_re_observed_after_the_leaf_evidence(lib, monkeypatch):
    """The ordering F-7 is about: an observation of the root must be taken after the listings.

    A snapshot taken before the records were even read cannot answer "was it still there when the
    pass ended", however many times it is consulted.
    """
    from beatsync_fork import library_inventory as fork_inventory

    seen = _reobservation_spy(monkeypatch, verdict=fork_inventory.ROOT_UNREACHABLE)
    ns = ns_for(lib)
    ns[HANDLER](lib["media"], True, True)
    assert seen["listings"] >= 1, "the source pass listed nothing; the fixture is wrong"
    assert seen["late_probes"] >= 1, (
        "the root was never observed again after the leaf evidence was gathered, so clause (d2) "
        "was settled on a stale snapshot and F3_SOURCES_NOTE's end-of-pass promise is words only")
    # And ONCE for the one root, not once per record: the re-observation is a listing of the root
    # folder, so a per-record probe would turn a bounded pass into a quadratic one.
    assert seen["late_probes"] == 1, (
        f"the root was re-observed {seen['late_probes']} times for one root; the end-of-pass "
        f"check must be per root, not per record")


@pytest.mark.parametrize("verdict_name", ["ROOT_UNREACHABLE", "UNKNOWN"])
def test_s21_a_root_lost_during_the_pass_takes_its_findings_back(lib, contract_module,
                                                                 monkeypatch, verdict_name):
    """And the behavioural half: with the end-of-pass observation answering "not positively
    observed", the confirmed-missing finding must be HELD, not published.

    Both a reachability failure and a plain hold are tried, because
    `RootState.positively_observed` requires a positive verdict and not merely a reachable
    anchor -- A1's own finding F-2.
    """
    from beatsync_fork import library_inventory as fork_inventory

    honest = ns_for(lib)
    before = honest[HANDLER](lib["media"], True, True)
    assert any("1 record" in ln for ln in before.splitlines()
               if "source confirmed missing:" in ln), (
        "the fixture does not produce a confirmed-missing finding in the first place, so this "
        "test could not tell a retraction from a no-op:\n" + before[-2500:])

    _reobservation_spy(monkeypatch, verdict=getattr(fork_inventory, verdict_name))
    ns = ns_for(lib)
    after = ns[HANDLER](lib["media"], True, True)

    axis = [ln for ln in after.splitlines() if "source confirmed missing:" in ln]
    assert axis, after[-2500:]
    assert not any("1 record" in ln for ln in axis), (
        "a confirmed-missing finding survived a root that was not positively observed when the "
        "pass ended: " + " | ".join(axis))
