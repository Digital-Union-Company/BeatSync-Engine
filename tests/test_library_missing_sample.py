"""R14-B S2 — the bounded sample of confirmed-missing source paths.

S1 made A5's source axis answerable. The owner's first real run then showed why a count alone is
not yet usable: on a 5,741-file library it reported *"at least 50 records with confirmed-missing
source paths"* — believable, and impossible to check. S2 prints up to twenty of those paths so a
person can look.

What this suite has to hold down is that a **display** stayed a display:

* the population is S1's already-finalised verdict tuple, admitted by A1's own
  `confirmed_missing` (which reads `LeafVerdict.published`), so UNKNOWN, provisional, access-denied,
  unreachable and uninspected are excluded by that function and not by a second rule in `gui.py`;
* out-of-scope paths cannot appear, because only `SCOPE_IN` records were ever probed — excluded
  upstream, which is why no containment test exists in the renderer to go wrong;
* it counts **paths**, A5 counts **records**, and the two numbers are different on purpose;
* A5's block is **byte-identical** with and without the sample, because nothing in it is derived
  from the display;
* no filesystem call, no second record read, no new widget, no raised ceiling, nothing written.

The harness is `test_library_inventory_panel`'s, and `_f3_missing_sample` is registered in its
`F3_FUNCTIONS` tuple, so the structural bans in that suite cover it too.
"""

from __future__ import annotations

import ast
import json
import os

import pytest

from test_library_inventory_panel import (  # noqa: E402  (harness reuse is the point)
    F3_FUNCTIONS,
    HANDLER,
    SAMPLE_FN,
    SOURCES_FN,
    _code,
    _func,
    _tree,
    cfg,
    contract_module,  # noqa: F401  (imported so pytest collects it as a fixture here too)
    load_f3,
)

OFFLINE_SOURCE = r"Z:\disconnected-drive\clip.mp4"


def _record(folder, name, source):
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        json.dump({"cache_contract": "stage5_cache_v3", "video_file": str(source)}, fh)


def _name(i: int) -> str:
    """A filename of A3's record shape: 8 hex, underscore, 24 hex."""
    return f"{i:08x}_{i:024x}.json"


@pytest.fixture
def lib(tmp_path):
    """A library with two present sources, three missing ones, two aliases of one missing path,
    and one record on a volume this machine does not have."""
    media = tmp_path / "Library"
    local = tmp_path / "input" / "video_analysis_cache"
    shared = tmp_path / "SharedCache" / "VideoAnalysis"
    for p in (media, local, shared):
        p.mkdir(parents=True)
    (media / "present_a.mp4").write_bytes(b"\0" * 2048)
    (media / "present_b.mkv").write_bytes(b"\0" * 4096)

    missing = [str(media / "gone_1.mp4"), str(media / "gone_2.mkv"), str(media / "gone_3.webm")]
    _record(str(local), _name(1), media / "present_a.mp4")
    _record(str(local), _name(2), media / "present_b.mkv")
    _record(str(local), _name(3), missing[0])
    _record(str(local), _name(4), missing[1])
    _record(str(local), _name(5), missing[2])
    # two further records naming the SAME missing source as the first -- three records, one path
    _record(str(local), _name(6), missing[0])
    _record(str(local), _name(7), missing[0].upper())
    _record(str(local), _name(8), OFFLINE_SOURCE)
    return {"media": str(media), "input": str(tmp_path / "input"), "local": str(local),
            "shared": str(shared), "root": tmp_path, "missing": missing}


def ns_for(lib, **kw):
    return load_f3(shared_config=cfg(lib["shared"], True), local_default=lib["input"], **kw)


def build(lib, *, records=True, sources=True, ns=None):
    ns = ns or ns_for(lib)
    return ns[HANDLER](lib["media"], records, sources), ns


def _sample_block(text, ns):
    """The sample section only, bounded at the next blank-line-separated heading."""
    start = text.find(ns["F3_SAMPLE_HEADING"])
    if start < 0:
        return ""
    end = text.find("Analysis cache report", start)
    return text[start:end if end > 0 else len(text)]


def _a5_block(text):
    start = text.find("Analysis cache report")
    return text[start:] if start >= 0 else ""


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
# GROUP T1 — it shows the paths, and counts PATHS
# ===========================================================================

def test_t1_the_confirmed_missing_paths_are_listed(lib, contract_module):
    text, ns = build(lib)
    block = _sample_block(text, ns)
    assert block, text[-2500:]
    for path in lib["missing"]:
        assert path in block, (path, block)


def test_t2_several_records_naming_one_source_are_one_path(lib, contract_module):
    """Three records name `gone_1.mp4` (one of them in upper case). The sample is a list of
    PATHS, so it must appear once — and the two counts must be reported as the different things
    they are."""
    text, ns = build(lib)
    block = _sample_block(text, ns)
    target = lib["missing"][0]
    assert block.count(target) == 1, block
    # 3 distinct paths out of 5 confirmed-missing verdicts
    assert "3 DISTINCT source paths" in block, block
    assert "from 5 confirmed-missing verdicts" in block, block
    assert "is NOT the record figure A5 reports below" in block


def test_t3_the_path_count_is_not_presented_as_a_record_count(lib, contract_module):
    """A5 still owns the record figure, and it is still a floor here."""
    text, _ns = build(lib)
    a5 = _a5_block(text)
    axis = [l for l in a5.splitlines() if "source confirmed missing:" in l]
    assert axis and "records" in axis[0], axis
    assert "3 records" not in axis[0], (
        "the record axis adopted the DISTINCT PATH count; they are different populations")


# ===========================================================================
# GROUP T2 — bounded, deduplicated, deterministic
# ===========================================================================

def test_t4_at_most_twenty_paths_are_shown_and_the_rest_are_named(lib, contract_module):
    for i in range(10, 35):                       # 25 further missing sources
        _record(lib["local"], _name(i), os.path.join(lib["media"], f"absent_{i:02d}.mp4"))
    text, ns = build(lib)
    block = _sample_block(text, ns)
    listed = [l for l in block.splitlines() if l.strip().startswith(lib["media"])]
    assert len(listed) == ns["F3_SAMPLE_MAX_PATHS"] == 20, len(listed)
    assert "28 DISTINCT source paths" in block, block
    assert "BOUNDED SAMPLE: 8 further distinct paths are not listed" in block, block
    assert "never a complete list" in block


def test_t5_the_order_is_deterministic_and_independent_of_verdict_order(lib):
    """Same pass, same twenty, whatever order the records happened to be read in."""
    from beatsync_fork import library_inventory as fi

    ns = ns_for(lib)
    verdicts = [
        fi.LeafVerdict(path=p, verdict=fi.MISSING_CONFIRMED, is_final=True,
                       clauses=fi.Clauses(parent_listed=True, name_absent=True,
                                          leaf_absent_error=True,
                                          anchor_reachable_at_listing=True,
                                          anchor_reachable_at_end=True))
        for p in (r"C:\lib\c.mp4", r"C:\lib\a.mp4", r"C:\lib\b.mp4")
    ]
    forward, _ = ns[SAMPLE_FN](tuple(verdicts))
    backward, _ = ns[SAMPLE_FN](tuple(reversed(verdicts)))
    assert forward == backward
    paths = [l.strip() for l in forward if l.strip().lower().startswith("c:\\lib")]
    assert paths == [r"C:\lib\a.mp4", r"C:\lib\b.mp4", r"C:\lib\c.mp4"], paths


def test_t6_two_identical_clicks_show_the_same_sample(lib, contract_module):
    first, ns = build(lib)
    second, _ = build(lib, ns=ns)
    assert _sample_block(first, ns) == _sample_block(second, ns)


# ===========================================================================
# GROUP T3 — what may never appear
# ===========================================================================

def test_t7_an_out_of_scope_record_is_never_listed(lib, contract_module):
    text, ns = build(lib)
    assert OFFLINE_SOURCE not in text
    assert "disconnected-drive" not in text
    assert "Z:\\" not in text
    touched = " ".join(str(c) for c in ns["_fs"].calls)
    assert "disconnected-drive" not in touched, touched[:600]


@pytest.mark.parametrize("verdict,final", [
    ("MISSING_PROVISIONAL", False),               # not finalised: published is UNKNOWN
    ("MISSING_CONFIRMED", False),                 # enum right, not final: published is UNKNOWN
    ("UNKNOWN", True),
    ("ACCESS_DENIED", True),                      # inaccessible storage
    ("ROOT_UNREACHABLE", True),                   # disconnected storage
    ("NOT_INSPECTED", True),
    ("PRESENT", True),
])
def test_t8_only_a_finalised_confirmed_missing_verdict_is_listed(lib, verdict, final):
    from beatsync_fork import library_inventory as fi

    ns = ns_for(lib)
    item = fi.LeafVerdict(path=r"C:\lib\never-show.mp4", verdict=getattr(fi, verdict),
                          is_final=final,
                          clauses=fi.Clauses(parent_listed=True, name_absent=True,
                                             leaf_absent_error=True,
                                             anchor_reachable_at_listing=True,
                                             anchor_reachable_at_end=True))
    lines, rewritten = ns[SAMPLE_FN]((item,))
    assert rewritten == 0
    assert "never-show.mp4" not in "\n".join(lines), (verdict, final, lines)
    assert lines == [ns["F3_SAMPLE_NONE"]], lines


def test_t9_a_root_lost_during_the_pass_shows_no_paths(lib, contract_module, monkeypatch):
    """The S1 end-of-pass check retracts the findings, so the sample has nothing to print —
    the display must follow the verdicts rather than the earlier evidence."""
    from beatsync_fork import library_inventory as fi

    honest, ns = build(lib)
    assert lib["missing"][0] in _sample_block(honest, ns), "the fixture shows no path to lose"

    real_probe, real_list = fi.probe_root, fi.list_directory
    seen = {"listings": 0}

    def spy_list(directory, **kw):
        seen["listings"] += 1
        return real_list(directory, **kw)

    def spy_probe(path, **kw):
        if seen["listings"]:
            return fi.RootState(typed=str(path), real=os.path.realpath(str(path)),
                                verdict=fi.ROOT_UNREACHABLE, anchor_reachable=False, exists=True)
        return real_probe(path, **kw)

    monkeypatch.setattr(fi, "list_directory", spy_list)
    monkeypatch.setattr(fi, "probe_root", spy_probe)

    after, ns2 = build(lib)
    block = _sample_block(after, ns2)
    for path in lib["missing"]:
        assert path not in block, (path, block)
    assert ns2["F3_SAMPLE_NONE"].split(":")[0] in block, block


# ===========================================================================
# GROUP T4 — both gates, and nothing on disk
# ===========================================================================

@pytest.mark.parametrize("records,sources", [(False, False), (False, True), (True, False)])
def test_t10_no_path_list_while_either_box_is_unticked(lib, records, sources, contract_module):
    text, ns = build(lib, records=records, sources=sources)
    assert ns["F3_SAMPLE_HEADING"] not in text, (records, sources)
    for path in lib["missing"]:
        assert path not in text, (records, sources, path)


@pytest.mark.parametrize("records", [False, True])
@pytest.mark.parametrize("sources", [False, True])
def test_t11_nothing_is_written_on_any_combination(lib, records, sources):
    before = manifest(lib["root"])
    build(lib, records=records, sources=sources)
    assert manifest(lib["root"]) == before


def test_t12_the_renderer_touches_no_filesystem_and_reads_no_record():
    body = _code(_func(SAMPLE_FN))
    for forbidden in ("open(", "json", "listdir", "scandir", "stat(", "lstat", "isdir", "exists",
                      "walk", "read_records", "list_directory", "classify_leaf", "probe_root",
                      "shutil", "subprocess", "makedirs", "remove", "unlink", "rename"):
        assert forbidden not in body, forbidden


def test_t13_the_ceilings_are_module_constants_and_unchanged():
    source = ast.unparse(_tree())
    assert "F3_SAMPLE_MAX_PATHS = 20" in source
    assert "F3_SAMPLE_PATH_MAX_CHARS = 400" in source
    # S1's resource ceilings must not have moved
    assert "F3_SOURCE_MAX_RECORDS = 2000" in source or "F3_SOURCE_MAX_RECORDS = 2_000" in source
    assert "F3_SOURCE_MAX_DIRECTORIES = 500" in source


# ===========================================================================
# GROUP T5 — untrusted record text cannot corrupt the read-out
# ===========================================================================

def test_t14_control_characters_in_a_recorded_path_are_neutralised(lib):
    from beatsync_fork import library_inventory as fi

    ns = ns_for(lib)
    hostile = "C:\\lib\\evil\r\n  Analysis cache report\x1b[31m\x00.mp4"
    item = fi.LeafVerdict(path=hostile, verdict=fi.MISSING_CONFIRMED, is_final=True,
                          clauses=fi.Clauses(parent_listed=True, name_absent=True,
                                             leaf_absent_error=True,
                                             anchor_reachable_at_listing=True,
                                             anchor_reachable_at_end=True))
    lines, rewritten = ns[SAMPLE_FN]((item,))
    assert rewritten == 1
    # Per LINE, because the renderer returns lines: a control character surviving inside one is
    # what would let a record's contents forge a heading or move the cursor.
    for line in lines:
        for bad in ("\r", "\n", "\t", "\x00", "\x1b", "\x07", "\x08"):
            assert bad not in line, (repr(bad), line)
    rendered = "\n".join(lines)
    assert ns["F3_SAMPLE_REWRITTEN_MARK"].strip() in rendered
    assert "NOT safe to copy as paths" in rendered
    # The residual, stated rather than overclaimed: with the control characters gone the injected
    # text is inert, but it is still TEXT, and it can still read like a heading. It cannot start a
    # LINE -- every sample path is indented and no newline survives -- so the structure of the
    # read-out is what it protects, not the absence of look-alike words.
    assert "Analysis cache report" in rendered
    assert not any(line.startswith("Analysis cache report") for line in lines)


def test_t15_an_absurdly_long_recorded_path_is_truncated(lib):
    from beatsync_fork import library_inventory as fi

    ns = ns_for(lib)
    long_path = "C:\\lib\\" + ("x" * 5000) + ".mp4"
    item = fi.LeafVerdict(path=long_path, verdict=fi.MISSING_CONFIRMED, is_final=True,
                          clauses=fi.Clauses(parent_listed=True, name_absent=True,
                                             leaf_absent_error=True,
                                             anchor_reachable_at_listing=True,
                                             anchor_reachable_at_end=True))
    lines, rewritten = ns[SAMPLE_FN]((item,))
    assert rewritten == 1
    for line in lines:
        assert len(line) < ns["F3_SAMPLE_PATH_MAX_CHARS"] + len(
            ns["F3_SAMPLE_REWRITTEN_MARK"]) + 8, len(line)


# ===========================================================================
# GROUP T6 — A5 is untouched
# ===========================================================================

def _drop_the_sample(name, node):
    """DEFECT/control: every statement that renders the sample is removed from
    `_f3_record_lines`, and **only** those.

    Targeted by name rather than by "the first body that changes length": the sample lives in an
    ``else`` branch inside a ``try``, so a loose walk removes the wrong statement and the control
    then proves something other than what it claims.
    """
    if name != "_f3_record_lines":
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    markers = (SAMPLE_FN, "F3_SAMPLE_HEADING", "F3_SAMPLE_FAILED", "sample_lines")
    removed = []

    def mentions(node) -> bool:
        text = ast.unparse(node)
        return any(m in text for m in markers)

    def removable(stmt) -> bool:
        """Only the three statements that ARE the sample.

        An ``if``/``else`` that merely *contains* them mentions them too — removing it was the
        first version of this control, and it silently deleted the whole source pass, so A5
        reverted to "not assessed" and `test_t16` failed for a reason that had nothing to do
        with the sample.
        """
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.Try)):
            return mentions(stmt)
        if isinstance(stmt, ast.For):
            return mentions(stmt.iter)
        return False

    def prune(stmts):
        kept = []
        for stmt in stmts:
            if removable(stmt):
                removed.append(ast.unparse(stmt).splitlines()[0])
                continue
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    setattr(stmt, field, prune(inner))
            for handler in getattr(stmt, "handlers", []) or []:
                handler.body = prune(handler.body)
            kept.append(stmt)
        return kept

    clone.body = prune(clone.body)
    assert len(removed) == 3, removed          # the heading append, the guarded call, the loop
    return ast.fix_missing_locations(clone)


def test_t16_a5s_report_block_is_byte_identical_with_and_without_the_sample(lib, contract_module):
    """The display is downstream of everything A5 says. If removing it changed one character of
    A5's block, the sample would be feeding the engine rather than reading it."""
    honest, ns = build(lib)
    stripped, _ = build(lib, ns=ns_for(lib, mutations=(_drop_the_sample,)))
    assert _a5_block(honest) == _a5_block(stripped), (
        "A5's block changed when the sample was removed")
    assert ns["F3_SAMPLE_HEADING"] in honest
    assert ns["F3_SAMPLE_HEADING"] not in stripped


def test_t17_removing_the_sample_makes_the_path_assertions_fail(lib, contract_module):
    """Negative control for GROUP T1/T2: without the renderer there are no paths to find."""
    stripped, ns = build(lib, ns=ns_for(lib, mutations=(_drop_the_sample,)))
    for path in lib["missing"]:
        assert path not in stripped, (
            "a path survived the removal of the sample, so test_t1 proves nothing")


def _admit_everything(name, node):
    """DEFECT: the admission test stops reading `published` and lists every supplied verdict."""
    if name != SAMPLE_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    for stmt in ast.walk(clone):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and getattr(stmt.targets[0], "id", None) == "found"):
            stmt.value = ast.parse(
                "tuple(getattr(v, 'path', '') for v in source_verdicts)").body[0].value
            return ast.fix_missing_locations(clone)
    raise AssertionError("no `found` assignment to mutate")


def test_t18_admitting_every_verdict_would_list_an_unconfirmed_path(lib):
    """Negative control for GROUP T3: `confirmed_missing` is what keeps a provisional, unknown,
    access-denied or unreachable verdict out. With it bypassed, one appears."""
    from beatsync_fork import library_inventory as fi

    item = fi.LeafVerdict(path=r"C:\lib\never-show.mp4", verdict=fi.MISSING_PROVISIONAL,
                          is_final=False)
    honest = ns_for(lib)
    broken = ns_for(lib, mutations=(_admit_everything,))
    assert "never-show.mp4" not in "\n".join(honest[SAMPLE_FN]((item,))[0])
    assert "never-show.mp4" in "\n".join(broken[SAMPLE_FN]((item,))[0]), (
        "the admission test is not load-bearing; test_t8 would pass without it")


def test_t19_the_renderer_is_inside_the_structural_ban_tuple():
    assert SAMPLE_FN in F3_FUNCTIONS
    declared = {n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)}
    assert {n for n in declared if n.startswith("_f3_")} == set(F3_FUNCTIONS) - {HANDLER}


def test_t20_no_widget_and_no_new_event_was_added():
    from test_library_inventory_panel import _panel_with

    panel = ast.unparse(_panel_with())
    for banned in ("gr.Number", "gr.Slider", "gr.Dataframe", "gr.Dropdown", "gr.Radio",
                   "gr.File", "gr.Dataset"):
        assert banned not in panel, banned
    boxes = [n for n in ast.walk(_panel_with()) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "gr.Checkbox"]
    assert len(boxes) == 2, [ast.unparse(b) for b in boxes]
    buttons = [n for n in ast.walk(_panel_with()) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "gr.Button"]
    assert len(buttons) == 1, [ast.unparse(b) for b in buttons]


def test_t21_the_sample_says_a_missing_path_is_not_permission_to_delete(lib, contract_module):
    text, ns = build(lib)
    block = _sample_block(text, ns)
    assert "does NOT authorize deletion of its cache record" in block
    low = block.lower()
    for claim in ("safe to delete", "can be deleted", "should be deleted", "may be removed",
                  "reclaimable space:"):
        assert claim not in low, claim
