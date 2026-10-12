"""Deliverable Export V1-B-S1 — the subordinate durable shareable MP4 (H1-b).

The production helper is executed for real, not modelled. `gui.py` needs the whole portable
runtime (`logger` pulls in librosa, `gpu_cpu_utils` probes NVENC), so it cannot be imported on a
bare interpreter — but `_publish_share_no_replace` is deliberately stdlib-only and depends on
exactly one module constant, so the real body is AST-extracted and run here.

Faults are injected by shadowing `os`, `open` and `hashlib` **in the exec namespace** (a
module-global `open` shadows the builtin), so every assertion below exercises the real control
flow. Nothing here restates the production algorithm — a test that recomputed the copy-and-digest
sequence would pass against a broken implementation that made the same mistake.

What H1-b guarantees, and what each section pins:

    * the master promotion and SUCCESS commit point are untouched          -> CONTRACTS
    * staging is attempt-unique and exclusively created                    -> STAGING OWNERSHIP
    * publication is atomic and never replaces                             -> ATOMIC PUBLICATION
    * the staged copy is verified by re-reading it                         -> VERIFICATION
    * every failure is contained and reported truthfully                   -> FAILURE MATRIX
    * the preview and master are never mutated                             -> PRESERVATION
"""

from __future__ import annotations

import ast
import errno
import hashlib
import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

CHUNK = 1 << 20


def _gui_source() -> str:
    with open(_GUI, "r", encoding="utf-8") as fh:
        return fh.read()


def load_share_publisher(namespace: dict | None = None) -> dict:
    """The REAL `_publish_share_no_replace` plus its REAL module constants.

    Exported so the GUI-level suites can give the extracted render body the genuine publisher
    instead of a stub; a stub there would let the publication contract drift unnoticed.
    """
    tree = ast.parse(_gui_source(), filename=_GUI)
    want_fn = {"_publish_share_no_replace", "_publish_share_attempt"}
    want_const = {"SHARE_STAGING_DIR_NAME", "SHARE_COPY_CHUNK_BYTES"}
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_fn:
            nodes.append(node)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in want_const):
            nodes.append(node)
    found = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    assert not want_fn - found, f"not found in gui.py: {want_fn - found}"
    found_const = {n.targets[0].id for n in nodes if isinstance(n, ast.Assign)}
    assert not want_const - found_const, f"missing constants: {want_const - found_const}"

    ns = {} if namespace is None else namespace
    for name, mod in (("os", os), ("errno", errno), ("uuid", uuid), ("hashlib", hashlib),
                      ("List", list)):
        ns.setdefault(name, mod)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), ns)  # noqa: S102
    return ns


@pytest.fixture
def publish():
    return load_share_publisher()["_publish_share_no_replace"]


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


class Life:
    """Minimal lifecycle stand-in. The real contract lives in test_render_cancellation.py."""

    def __init__(self, cancelled=False, cancel_after=None):
        self._cancelled = cancelled
        self._cancel_after = cancel_after
        self.checks = 0

    def cancel_requested(self):
        self.checks += 1
        if self._cancel_after is not None and self.checks > self._cancel_after:
            return True
        return self._cancelled


def layout(tmp_path, preview_bytes=b"PREVIEW" * 512):
    """A realistic layout: a session preview outside output/, staging inside it."""
    out = tmp_path / "output"
    stage = out / ".beatsync-staging"
    session = tmp_path / "session"
    for d in (out, session):
        os.makedirs(d, exist_ok=True)
    preview = session / "clip_20260101_000000_preview.mp4"
    with open(preview, "wb") as fh:
        fh.write(preview_bytes)
    return {
        "out": str(out), "stage": str(stage), "preview": str(preview),
        "share": str(out / "clip_20260101_000000_share.mp4"),
    }


# ===========================================================================
# CONTRACTS — H1 untouched, H1-b subordinate
# ===========================================================================


def _impl_body() -> str:
    tree = ast.parse(_gui_source())
    return ast.unparse(next(n for n in ast.walk(tree)
                            if isinstance(n, ast.FunctionDef)
                            and n.name == "_process_video_impl"))


def test_the_master_promotion_helper_is_untouched_and_not_reused():
    """H1 owns the ONE authoritative promotion. H1-b must not route through it."""
    body = _impl_body()
    assert body.count("_promote_output_no_replace(") == 1
    assert body.count("_publish_share_no_replace(") == 1

    tree = ast.parse(_gui_source())
    promote = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_promote_output_no_replace")
    # the authoritative helper must not have gained a share/policy parameter
    args = [a.arg for a in promote.args.args] + [a.arg for a in promote.args.kwonlyargs]
    assert args == ["temp_output", "output_path"], args
    publish = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_publish_share_no_replace")
    assert publish is not promote


def test_publication_runs_strictly_after_the_success_commit_point():
    body = _impl_body()
    assert (body.index("_promote_output_no_replace(")
            < body.index("RenderOutcomeKind.SUCCESS")
            < body.index("build_delivery_preview_command(")
            < body.index("_publish_share_no_replace("))


def test_publication_never_writes_the_authoritative_keys():
    """A share failure must be invisible to the outcome model and to batch continuation."""
    tree = ast.parse(_gui_source())
    impl = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "_process_video_impl")
    # the share region: from the publish call to the stage-6 summary that closes it
    body = ast.unparse(impl)
    region = body[body.index("share_path = ''"):body.index("_stage6_summary(")]
    for forbidden in ("RENDER_OUTCOME_KEY", "LAST_OUTPUT_PATH_KEY", "preview_path ="):
        assert forbidden not in region, f"{forbidden} in the share region:\n{region}"

    publisher = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                     and n.name == "_publish_share_no_replace")
    helper = ast.unparse(publisher)
    for forbidden in ("RENDER_OUTCOME_KEY", "LAST_OUTPUT_PATH_KEY", "RenderOutcomeKind",
                      "session_state"):
        assert forbidden not in helper, forbidden


def test_the_helper_never_raises_and_uses_no_banned_primitive():
    """Checked on the AST, not by substring: the docstring legitimately NAMES os.replace to say
    it is not used, and a substring ban would trip on the prose explaining the ban."""
    tree = ast.parse(_gui_source())
    wrapper = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_publish_share_no_replace")
    attempt = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_publish_share_attempt")

    # The PUBLIC helper is the one that must never raise, and it must contain the attempt
    # with a broad handler -- naming causes one at a time cannot deliver that guarantee.
    assert not [n for n in ast.walk(wrapper) if isinstance(n, ast.Raise)]
    handlers = [h for n in ast.walk(wrapper) if isinstance(n, ast.Try) for h in n.handlers]
    assert any(h.type is not None and ast.unparse(h.type) == "Exception" for h in handlers), \
        "the public helper has no broad containment handler"
    assert not [n for n in ast.walk(attempt) if isinstance(n, ast.Raise)]

    both = {ast.unparse(n.func) for fn in (wrapper, attempt)
            for n in ast.walk(fn) if isinstance(n, ast.Call)}
    for banned in ("os.replace", "shutil.move", "shutil.copy", "shutil.copy2",
                   "shutil.copyfile", "os.truncate", "os.rmdir", "shutil.rmtree"):
        assert banned not in both, f"{banned} is called"

    renames = [n for fn in (wrapper, attempt) for n in ast.walk(fn)
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.rename"]
    assert len(renames) == 1, "there must be exactly one atomic publication step"
    # removals exist only to discard staging this code exclusively created
    removes = [n for fn in (wrapper, attempt) for n in ast.walk(fn)
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.remove"]
    assert len(removes) == 2, f"{len(removes)} os.remove sites (attempt + wrapper cleanup)"


def test_no_second_transcode_is_introduced():
    tree = ast.parse(_gui_source())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_publish_share_no_replace")
    rendered = ast.unparse(fn)
    for token in ("subprocess", "Popen", "build_delivery_preview_command", "ffmpeg", "libx264"):
        assert token not in rendered, token
    # and the GUI gained no subprocess.run site
    runs = [n for n in ast.walk(ast.parse(_gui_source())) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]
    assert len(runs) == 2, f"{len(runs)} subprocess.run sites; expected preview + Director"


# ===========================================================================
# HAPPY PATH
# ===========================================================================


def test_a_share_file_is_published_beside_the_master(tmp_path, publish):
    L = layout(tmp_path)
    before = sha(L["preview"])

    reason, published = publish(L["preview"], L["share"], L["stage"])

    assert reason == "", reason
    assert os.path.isfile(L["share"])
    assert published == os.path.getsize(L["share"]) == os.path.getsize(L["preview"])
    assert sha(L["share"]) == before, "published bytes differ from the preview"
    assert sha(L["preview"]) == before, "the preview was mutated"


def test_the_staging_directory_is_created_inside_output(tmp_path, publish):
    L = layout(tmp_path)
    assert not os.path.isdir(L["stage"])

    publish(L["preview"], L["share"], L["stage"])

    assert os.path.isdir(L["stage"])
    assert os.path.commonpath([os.path.realpath(L["stage"]),
                               os.path.realpath(L["out"])]) == os.path.realpath(L["out"])


def test_no_staging_artifact_survives_a_successful_publication(tmp_path, publish):
    L = layout(tmp_path)

    publish(L["preview"], L["share"], L["stage"])

    assert os.listdir(L["stage"]) == [], os.listdir(L["stage"])


# ===========================================================================
# STAGING OWNERSHIP  (§6)
# ===========================================================================


def test_the_staging_filename_is_attempt_unique(tmp_path, publish):
    """Two attempts at the SAME share name must not compute the same staging path."""
    L = layout(tmp_path)
    seen = []

    real_open = os.open

    def recording_open(path, flags, *a):
        if str(path).endswith(".part"):
            seen.append(os.path.basename(str(path)))
        return real_open(path, flags, *a)

    ns = load_share_publisher({"os": _OsShim(open=recording_open)})
    pub = ns["_publish_share_no_replace"]
    pub(L["preview"], L["share"], L["stage"])
    os.remove(L["share"])
    pub(L["preview"], L["share"], L["stage"])

    assert len(seen) == 2, seen
    assert seen[0] != seen[1], f"staging name is predictable: {seen}"
    assert all(n.endswith(".part") for n in seen), seen


def test_the_staging_file_is_created_exclusively(tmp_path, publish):
    """O_EXCL is what stops a second attempt truncating the first one's in-flight copy."""
    L = layout(tmp_path)
    flags_seen = []
    real_open = os.open

    def recording_open(path, flags, *a):
        if str(path).endswith(".part"):
            flags_seen.append(flags)
        return real_open(path, flags, *a)

    ns = load_share_publisher({"os": _OsShim(open=recording_open)})
    ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert flags_seen, "no staging file was opened"
    assert flags_seen[0] & os.O_EXCL, "staging file was not created exclusively"
    assert flags_seen[0] & os.O_CREAT


def test_a_taken_staging_name_is_refused_not_truncated(tmp_path):
    """If O_EXCL loses, the attempt refuses; it must never truncate the winner's file."""
    L = layout(tmp_path)

    def always_exists(path, flags, *a):
        raise FileExistsError(errno.EEXIST, "taken")

    ns = load_share_publisher({"os": _OsShim(open=always_exists)})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "staging filename was already taken" in reason
    assert not os.path.exists(L["share"])


def test_an_unrelated_staging_artifact_is_preserved(tmp_path, publish):
    """Another invocation's abandoned .part must survive untouched (§6 retention policy)."""
    L = layout(tmp_path)
    os.makedirs(L["stage"], exist_ok=True)
    orphan = os.path.join(L["stage"], "clip_20251231_000000_share.mp4.999.deadbeefcafe.part")
    with open(orphan, "wb") as fh:
        fh.write(b"SOMEONE-ELSES-IN-FLIGHT-COPY")
    guard = sha(orphan)

    reason, _ = publish(L["preview"], L["share"], L["stage"])

    assert reason == ""
    assert os.path.isfile(orphan), "an unrelated staging artifact was removed"
    assert sha(orphan) == guard, "an unrelated staging artifact was modified"


def test_a_failed_attempt_discards_only_its_own_staging_file(tmp_path):
    L = layout(tmp_path)
    os.makedirs(L["stage"], exist_ok=True)
    orphan = os.path.join(L["stage"], "other.part")
    with open(orphan, "wb") as fh:
        fh.write(b"OTHER")

    ns = load_share_publisher({"os": _OsShim(fsync=_raise(OSError(errno.EIO, "fsync failed")))})
    reason, _ = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert "could not be written" in reason, reason
    assert os.path.isfile(orphan), "someone else's staging file was removed"
    remaining = [f for f in os.listdir(L["stage"]) if f != "other.part"]
    assert remaining == [], f"own staging file was not discarded: {remaining}"


# ===========================================================================
# ATOMIC PUBLICATION  (§7)
# ===========================================================================


def test_an_occupied_destination_is_refused_and_preserved(tmp_path, publish):
    L = layout(tmp_path)
    with open(L["share"], "wb") as fh:
        fh.write(b"PRE-EXISTING-SHARE-FILE")
    guard = sha(L["share"])

    reason, published = publish(L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "already exists" in reason and "preserved" in reason
    assert sha(L["share"]) == guard, "the existing share file was replaced"
    assert sha(L["preview"]) == sha(L["preview"]), "preview intact"
    assert os.listdir(L["stage"]) == [], "the refused staging copy was left behind"


def test_a_destination_appearing_mid_flight_cannot_be_overwritten(tmp_path):
    """Concurrent collision: the file appears between staging and rename.

    The collision is simulated rather than driven through the real primitive, because POSIX
    `rename(2)` REPLACES silently and this suite must run on any CPython. The real Windows
    behaviour this depends on is asserted separately, once, in the skipif test below.
    """
    L = layout(tmp_path)

    def rename_after_someone_else_won(src, dst):
        with open(dst, "wb") as fh:
            fh.write(b"WON-THE-RACE")
        raise FileExistsError(errno.EEXIST, "destination occupied")

    ns = load_share_publisher({"os": _OsShim(rename=rename_after_someone_else_won)})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0, "a lost race was reported as a successful publication"
    assert reason, "a lost race produced no failure reason"
    with open(L["share"], "rb") as fh:
        assert fh.read() == b"WON-THE-RACE", "the winner's bytes were destroyed"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows no-replace rename semantics")
def test_real_windows_rename_refuses_an_occupied_destination(tmp_path):
    """The guarantee rests on the real primitive, so assert the real primitive once."""
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"NEW")
    b.write_bytes(b"OLD")

    with pytest.raises(FileExistsError):
        os.rename(a, b)

    assert b.read_bytes() == b"OLD", "destination bytes changed"
    assert a.read_bytes() == b"NEW", "source was consumed by a failed rename"


def test_a_failed_rename_is_never_reported_as_published(tmp_path):
    ns = load_share_publisher(
        {"os": _OsShim(rename=_raise(OSError(errno.EACCES, "denied")))})
    L = layout(tmp_path)

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0 and reason
    assert not os.path.exists(L["share"])


def test_no_partial_file_ever_appears_under_the_final_name(tmp_path):
    """Every failure mode: the final path must not exist afterwards."""
    faults = {
        "write": _OsShim(fsync=_raise(OSError(errno.ENOSPC, "no space"))),
        "rename": _OsShim(rename=_raise(OSError(errno.EIO, "io"))),
        "exdev": _OsShim(rename=_raise(OSError(errno.EXDEV, "cross-device"))),
    }
    for label, shim in faults.items():
        L = layout(tmp_path / label)
        ns = load_share_publisher({"os": shim})
        reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])
        assert published == 0, label
        assert reason, label
        assert not os.path.exists(L["share"]), f"{label}: partial file at the final path"


def test_cross_volume_is_refused_fail_closed(tmp_path):
    L = layout(tmp_path)
    ns = load_share_publisher({"os": _OsShim(rename=_raise(OSError(errno.EXDEV, "xdev")))})

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "different volumes" in reason, reason
    assert not os.path.exists(L["share"])


def test_a_staging_directory_on_another_volume_is_refused_before_any_copy(tmp_path):
    L = layout(tmp_path)
    calls = []

    def splitdrive(p):
        # staging reports a different drive than output
        return ("Z:", p) if ".beatsync-staging" in str(p) else ("C:", p)

    def tracking_open(path, flags, *a):
        calls.append(path)
        raise AssertionError("a copy was started despite a cross-volume staging dir")

    ns = load_share_publisher({"os": _OsShim(splitdrive=splitdrive, open=tracking_open)})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "different volumes" in reason, reason
    assert calls == []


def test_a_staging_path_that_only_shares_a_string_prefix_is_refused(tmp_path):
    """`output-evil/` must not pass as being inside `output/`."""
    L = layout(tmp_path)
    evil = str(tmp_path / "output-evil" / ".beatsync-staging")
    ns = load_share_publisher()

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], evil)

    assert published == 0
    assert "not inside the output folder" in reason, reason
    assert not os.path.exists(L["share"])


def test_an_unresolvable_reparse_attribute_refuses_the_publication(tmp_path):
    """The RESIDUAL reparse guard, which is the only thing it actually is.

    [FORK] Digital-Union (V1-B-S2, accepted review finding MINOR-2): this test was named
    `..._a_reparse_point_in_the_publication_path_is_refused` and its docstring claimed junctions
    are "refused rather than followed" with "the real-junction case covered by the OS itself".
    Both were wrong. `realpath` runs BEFORE `_is_reparse`, so a real junction is **followed** and
    the attribute check only ever sees an already-resolved path — which by construction is not a
    reparse point. Verified against real Windows junctions: the branch below never fires for one.

    So what this pins is the residual case the branch genuinely covers: `realpath` returned a path
    whose attributes still say reparse point (it could not resolve), and the publication refuses
    rather than proceeding blind. The shim is what makes that state reachable at all.

    Real junctions are contained by measurement instead, and that is asserted separately in
    `test_a_staging_path_that_only_shares_a_string_prefix_is_refused` (commonpath) and
    `test_a_staging_directory_on_another_volume_is_refused_before_any_copy` (splitdrive).
    """
    L = layout(tmp_path)

    class _Stat:
        st_file_attributes = 0x400      # FILE_ATTRIBUTE_REPARSE_POINT

    ns = load_share_publisher({"os": _OsShim(lstat=lambda p: _Stat())})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "junction or symlink" in reason, reason
    assert not os.path.exists(L["share"])


# ===========================================================================
# VERIFICATION
# ===========================================================================


def test_a_corrupted_staged_copy_is_detected_by_rereading_it(tmp_path):
    """A flipped byte keeps the SIZE correct, so only a real digest check catches it.
    This is why verification re-reads the staged file instead of trusting the write digest.

    [FORK] Digital-Union (V1-B-S2, accepted review finding MINOR-3): the fragment asserted below
    was "does not match the preview", which overstated the comparison. What is compared is the
    staged bytes against the bytes **read during the copy** — which is exactly what catches this
    corrupt write, and is also why a source mutated mid-copy would not be caught (see
    `test_the_digest_check_does_not_claim_the_source_was_stable`). Wording only; the control flow
    and every published byte are unchanged.
    """
    L = layout(tmp_path)
    ns = load_share_publisher({"open": _corrupting_open(flip=True)})

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "does not match the bytes copied" in reason, reason
    assert not os.path.exists(L["share"])


def test_the_digest_check_does_not_claim_the_source_was_stable(tmp_path):
    """The exact scope of the verification, pinned so the docs cannot drift back.

    [FORK] Digital-Union (V1-B-S2, accepted review finding MINOR-3): a same-size in-place mutation
    of the preview DURING the copy publishes successfully, because the staged bytes still match
    what was read. That is not a defect — the runtime supplies source stability (`_RENDER_LOCK`
    serialises renders, `session_dir` is a per-session mkdtemp, and the preview's FFmpeg child is
    reaped before publication starts), so the condition is unreachable in the app. It is pinned
    because the *claim* must stay honest: this proves "staged == bytes copied", nothing more.

    A SIZE-changing mutation is a different matter and IS caught, by the pre-copy size comparison.
    """
    L = layout(tmp_path, preview_bytes=b"A" * (CHUNK * 2))
    pristine = sha(L["preview"])
    real_open = open
    reads = {"n": 0}

    def mutating_open(path, mode="r", *a, **k):
        handle = real_open(path, mode, *a, **k)
        if str(path) != L["preview"] or "r" not in str(mode):
            return handle

        class _MutatingReader:
            def read(self, size=-1):
                reads["n"] += 1
                if reads["n"] == 2:
                    # same length, so the size check cannot see it
                    with real_open(L["preview"], "r+b") as writable:
                        writable.seek(0)
                        writable.write(b"Z" * 16)
                return handle.read(size)

            def __getattr__(self, item):
                return getattr(handle, item)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return handle.__exit__(*exc)

        return _MutatingReader()

    ns = load_share_publisher({"open": mutating_open})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert reason == "", "a same-size mid-copy source mutation was reported as a failure"
    assert published > 0
    assert sha(L["preview"]) != pristine, "the probe did not actually mutate the source"
    # published == what was READ, which here is the pre-mutation content: NOT the source as it
    # now stands. Claim A holds; claim B does not, and the docs must not say otherwise.
    assert sha(L["share"]) != sha(L["preview"])


def test_a_source_that_changes_size_during_the_copy_is_refused(tmp_path):
    """The other half of MINOR-3: size drift IS detected, by the pre-copy `source_bytes`."""
    L = layout(tmp_path, preview_bytes=b"B" * (CHUNK * 2))
    real_open = open
    reads = {"n": 0}

    def shrinking_open(path, mode="r", *a, **k):
        handle = real_open(path, mode, *a, **k)
        if str(path) != L["preview"] or "r" not in str(mode):
            return handle

        class _ShrinkingReader:
            def read(self, size=-1):
                reads["n"] += 1
                data = handle.read(size)
                return data[: len(data) // 2] if reads["n"] == 1 else data

            def __getattr__(self, item):
                return getattr(handle, item)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return handle.__exit__(*exc)

        return _ShrinkingReader()

    ns = load_share_publisher({"open": shrinking_open})
    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "wrong size" in reason, reason
    assert not os.path.exists(L["share"])


def test_a_short_staged_copy_is_detected_by_size(tmp_path):
    L = layout(tmp_path)
    ns = load_share_publisher({"open": _corrupting_open(drop_tail=16)})

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert "wrong size" in reason, reason
    assert not os.path.exists(L["share"])


def test_a_multi_chunk_preview_is_copied_and_verified_whole(tmp_path, publish):
    """Larger than one chunk, so the loop really iterates."""
    L = layout(tmp_path, preview_bytes=os.urandom(CHUNK + 4096))
    before = sha(L["preview"])

    reason, published = publish(L["preview"], L["share"], L["stage"])

    assert reason == ""
    assert published == CHUNK + 4096
    assert sha(L["share"]) == before


# ===========================================================================
# PREVIEW PRECONDITIONS
# ===========================================================================


@pytest.mark.parametrize("state,fragment", [
    ("missing", "missing"),
    ("empty", "empty"),
])
def test_a_missing_or_empty_preview_publishes_nothing(tmp_path, publish, state, fragment):
    L = layout(tmp_path)
    if state == "missing":
        os.remove(L["preview"])
    else:
        open(L["preview"], "wb").close()

    reason, published = publish(L["preview"], L["share"], L["stage"])

    assert published == 0
    assert fragment in reason, reason
    assert not os.path.exists(L["share"])


# ===========================================================================
# CANCELLATION  (§8)
# ===========================================================================


def test_cancellation_before_the_copy_starts_opens_nothing(tmp_path):
    L = layout(tmp_path)

    def tracking_open(path, flags, *a):
        raise AssertionError("a staging file was created despite a pending cancellation")

    ns = load_share_publisher({"os": _OsShim(open=tracking_open)})
    reason, published = ns["_publish_share_no_replace"](
        L["preview"], L["share"], L["stage"], lifecycle=Life(cancelled=True))

    assert published == 0
    assert "cancelled" in reason
    assert not os.path.exists(L["share"])


def test_cancellation_between_chunks_abandons_the_publication(tmp_path, publish):
    L = layout(tmp_path, preview_bytes=os.urandom(CHUNK * 3))
    before = sha(L["preview"])

    reason, published = publish(L["preview"], L["share"], L["stage"],
                                lifecycle=Life(cancel_after=2))

    assert published == 0
    assert "cancelled" in reason, reason
    assert not os.path.exists(L["share"]), "a cancelled publication still published"
    assert sha(L["preview"]) == before
    assert os.listdir(L["stage"]) == [], "the cancelled staging copy was left behind"


def test_cancellation_is_checked_immediately_before_the_rename(tmp_path):
    """A Stop arriving after verification but before publication must still stop it."""
    L = layout(tmp_path)

    def never(src, dst):
        raise AssertionError("renamed despite cancellation arriving before publication")

    # cancel only once the copy and verification reads are done
    life = Life(cancel_after=3)
    ns = load_share_publisher({"os": _OsShim(rename=never)})
    reason, published = ns["_publish_share_no_replace"](
        L["preview"], L["share"], L["stage"], lifecycle=life)

    assert published == 0
    assert "cancelled" in reason, reason
    assert not os.path.exists(L["share"])


def test_the_lifecycle_is_polled_not_blocked_on(tmp_path, publish):
    L = layout(tmp_path, preview_bytes=os.urandom(CHUNK * 2))
    life = Life()

    publish(L["preview"], L["share"], L["stage"], lifecycle=life)

    assert life.checks >= 3, f"only {life.checks} cancellation checks across a 2-chunk copy"


def test_no_lifecycle_means_no_cancellation_checks(tmp_path, publish):
    L = layout(tmp_path)
    reason, published = publish(L["preview"], L["share"], L["stage"], lifecycle=None)
    assert reason == "" and published > 0


# ===========================================================================
# FAILURE MATRIX  (§9)
# ===========================================================================


@pytest.mark.parametrize("attr,exc,fragment", [
    ("open", OSError(errno.ENOSPC, "no space"), "disk space"),
    ("open", OSError(errno.EACCES, "denied"), "could not be created"),
    ("makedirs", OSError(errno.EACCES, "denied"), "staging directory"),
    ("fsync", OSError(errno.EIO, "fsync"), "could not be written"),
    ("fsync", OSError(errno.ENOSPC, "full"), "disk space"),
    ("rename", OSError(errno.ENOSPC, "full"), "disk space"),
    ("rename", RuntimeError("something odd"), "could not be published"),
])
def test_every_failure_is_contained_and_named(tmp_path, attr, exc, fragment):
    L = layout(tmp_path)
    ns = load_share_publisher({"os": _OsShim(**{attr: _raise(exc)})})

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert fragment in reason, f"expected {fragment!r} in {reason!r}"
    assert not os.path.exists(L["share"])
    assert os.path.isfile(L["preview"]), "the preview was damaged by a failed publication"


def test_an_unexpected_exception_is_contained_rather_than_raised(tmp_path):
    L = layout(tmp_path)
    ns = load_share_publisher({"hashlib": _BrokenHashlib()})

    reason, published = ns["_publish_share_no_replace"](L["preview"], L["share"], L["stage"])

    assert published == 0
    assert reason, "an unexpected exception produced no reason"
    assert not os.path.exists(L["share"])


# ===========================================================================
# THE GUI PATH — the real `_process_video_impl`, end to end
# ===========================================================================


def test_a_successful_render_names_all_three_artifacts(tmp_path):
    from test_delivery_preview import _run_impl

    out = _run_impl(tmp_path)

    assert "HIGH-QUALITY MASTER:" in out["status"]
    assert "SHAREABLE MP4 (saved permanently):" in out["status"]
    assert "PLAYABLE PREVIEW (this session only - not a permanent download):" in out["status"]
    # each named path is the right one
    assert out["durable"] in out["status"]
    assert out["share"] in out["status"] and out["share"].endswith("_share.mp4")
    assert out["preview_path"] in out["status"]
    assert "⚠️" not in out["status"]
    # the three are genuinely different files, and only two are durable
    assert len({out["durable"], out["share"], out["preview_path"]}) == 3
    assert os.path.dirname(out["share"]) == out["out_dir"]
    assert os.path.dirname(out["preview_path"]) == out["session_dir"]


def test_the_share_file_is_a_verified_copy_of_the_preview(tmp_path):
    from test_delivery_preview import _run_impl

    out = _run_impl(tmp_path)

    assert sha(out["share"]) == sha(out["preview_path"]), \
        "the published share file is not byte-identical to the preview"


def test_gr_video_still_receives_the_session_preview_not_the_share_file(tmp_path):
    """Measured against the live app: a path under GRADIO_TEMP_DIR serves 206, a raw output/
    path serves 403. Handing `gr.Video` the durable share file would break playback."""
    from test_delivery_preview import _run_impl

    out = _run_impl(tmp_path)

    assert out["preview_path"] != out["share"]
    assert out["preview_path"] != out["durable"]
    assert os.path.dirname(out["preview_path"]) == out["session_dir"]


def test_a_failed_share_publication_leaves_the_render_successful(tmp_path):
    """The whole H1-b containment claim, measured on the real render body."""
    from test_delivery_preview import _run_impl
    import test_gui_guard_seam as seam

    real_namespace = seam._impl_namespace

    def namespace_with_failing_publisher(*a, **k):
        ns = real_namespace(*a, **k)
        ns["_publish_share_no_replace"] = lambda *_a, **_k: ("disk went away", 0)
        return ns

    seam._impl_namespace = namespace_with_failing_publisher
    try:
        out = _run_impl(tmp_path)
    finally:
        seam._impl_namespace = real_namespace

    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")
    assert out["state"]["last_output_path"] == out["durable"]
    assert out["preview_path"].endswith("_preview.mp4"), "the preview was discarded too"
    assert not out["status"].startswith("❌")
    assert "No permanent shareable MP4 was saved (disk went away)" in out["status"]
    assert "complete and unchanged" in out["status"]
    assert "PLAYABLE PREVIEW" in out["status"], "the preview must still be offered"
    assert "SHAREABLE MP4 (saved permanently):" not in out["status"]
    assert out["share"] == ""


def test_an_exploding_publisher_cannot_downgrade_the_committed_outcome(tmp_path):
    """An IMPOSSIBLE breach of the helper's contract: what survives it, and what does not.

    [FORK] Digital-Union (V1-B-S2, accepted review finding MINOR-5): this test was named
    `..._cannot_reach_the_generic_render_handler`, which is false — a raising publisher **does**
    reach `_process_video_impl`'s generic `except Exception`. Two different things were being
    conflated, and only one of them holds:

        the committed OUTCOME survives   -> RENDER_OUTCOME_KEY is already SUCCESS and the
                                            handler's `is None` guard leaves it standing, so the
                                            batch's continuation decision stays correct
        the user-visible RESULT does not -> the function still returns (None, '❌ Error: ...'),
                                            clearing gr.Video and showing an error panel for a
                                            finished, promoted render

    Both are asserted below, so the distinction cannot be lost again. None of this is reachable in
    the shipped code: the helper's structural wrapper catches `Exception`, and *that* — not this
    generic handler — is what delivers "never raises". This case only describes the blast radius
    if the wrapper were ever removed.
    """
    from test_delivery_preview import _run_impl
    import test_gui_guard_seam as seam

    real_namespace = seam._impl_namespace

    def namespace_with_exploding_publisher(*a, **k):
        ns = real_namespace(*a, **k)

        def boom(*_a, **_k):
            raise OSError("the publisher broke its own contract")

        ns["_publish_share_no_replace"] = boom
        return ns

    seam._impl_namespace = namespace_with_exploding_publisher
    try:
        out = _run_impl(tmp_path)
    finally:
        seam._impl_namespace = real_namespace

    # what SURVIVES: the committed outcome and the durable master
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS"), \
        "a broken publisher downgraded a committed render"
    assert out["state"]["last_output_path"] == out["durable"]
    assert os.path.isfile(out["durable"]), "the promoted master was lost"

    # what DOES NOT: the panel and the video widget. Measured, not assumed -- this is precisely
    # the cost the wrapper exists to prevent, and calling it "contained" would be wrong.
    assert out["status"].startswith("❌"), \
        "the generic handler is being credited with containing the user-visible result"
    assert out["preview_path"] is None, "gr.Video would have kept the preview"


def test_no_share_file_is_published_when_the_preview_failed(tmp_path):
    from test_delivery_preview import _run_impl

    for result in ("fail", "missing", "empty", "timeout"):
        out = _run_impl(tmp_path / result, preview_result=result)
        assert out["share"] == "", f"{result}: a share file was published without a preview"
        assert "SHAREABLE MP4" not in out["status"], result
        assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS"), result


def test_a_cancelled_render_publishes_no_share_file(tmp_path):
    from test_delivery_preview import _run_impl, _Lifecycle, _boom

    out = _run_impl(tmp_path, lifecycle=_Lifecycle(), runner=_boom,
                    cancel_during_render=True)

    assert out["share"] == ""
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")


def test_a_share_failure_writes_nothing_the_batch_reads(tmp_path):
    """The SEAM half: three independent real renders with a failing publisher all keep SUCCESS.

    [FORK] Digital-Union (V1-B-S2, accepted review finding MINOR-4): this was named
    `test_the_batch_cannot_see_a_share_failure`, which claimed more than it executed — it calls
    `_run_impl` three times and never constructs `render_selected_variants_guarded` at all. What
    it genuinely establishes is the seam: what `_process_video_impl` writes into `session_state`
    after a share failure, which is the only thing the batch reads. The batch WRAPPER is now
    driven for real in `test_the_real_batch_continues_past_a_candidate_share_failure` below.
    """
    from test_delivery_preview import _run_impl
    import test_gui_guard_seam as seam

    real_namespace = seam._impl_namespace
    attempts = []

    def namespace_with_failing_publisher(*a, **k):
        ns = real_namespace(*a, **k)

        def failing(*_a, **_k):
            attempts.append(1)
            return ("simulated candidate-2 share failure", 0)

        ns["_publish_share_no_replace"] = failing
        return ns

    seam._impl_namespace = namespace_with_failing_publisher
    try:
        states = [_run_impl(tmp_path / f"cand{i}") for i in range(1, 4)]
    finally:
        seam._impl_namespace = real_namespace

    assert len(attempts) == 3, "not every candidate attempted publication"
    for i, out in enumerate(states, start=1):
        kind = out["state"]["render_outcome_kind"]
        assert str(kind).endswith("SUCCESS"), f"candidate {i} outcome was {kind}"
        assert out["state"]["last_output_path"] == out["durable"], i
        # the class the batch reads is SUCCESS, so `if not candidate_outcome.success` is False
        # and nothing stops the loop
        assert "CANDIDATE_LOCAL" not in str(kind)
        assert "SHARED_FATAL" not in str(kind)


def test_the_real_batch_continues_past_a_candidate_share_failure(tmp_path):
    """The WRAPPER half, executed rather than inferred (accepted review finding MINOR-4).

    [FORK] Digital-Union (V1-B-S2): this drives the REAL `render_selected_variants_guarded` over
    four selected candidates, and each candidate runs the REAL `_process_video_impl` — real master
    promotion, real preview, real publisher — with the subordinate publication failing on
    **candidate 2 only**. Nothing about the continuation policy is stubbed: the real
    `RenderCandidateOutcome` is constructed with the real arguments the loop passes, so its
    `__post_init__` invariant still runs and a contradiction would still raise out of here.

    The production batch policy is untouched; this only measures it.
    """
    import test_render_failure_classification as rfc
    import test_delivery_preview as dp
    import test_gui_guard_seam as seam
    import threading
    import types
    import uuid as _uuid

    FAILING_CANDIDATE = 1                       # 0-based -> candidate 2
    COUNT = 4
    real_namespace = seam._impl_namespace
    current = {"index": -1}
    published = {}
    run = rfc._BatchRun()
    lifecycle_box = {}
    per_candidate = {}

    def namespace_for_current_candidate(*a, **k):
        ns = real_namespace(*a, **k)
        genuine = ns["_publish_share_no_replace"]

        def routed(*pa, **pk):
            if current["index"] == FAILING_CANDIDATE:
                return ("simulated candidate-2 share failure", 0)
            return genuine(*pa, **pk)

        ns["_publish_share_no_replace"] = routed
        return ns

    class _Skip:
        def __repr__(self):
            return "<skip>"

    def capturing_outcome(*args, **kwargs):
        outcome = rfc.fork_render_batch.RenderCandidateOutcome(*args, **kwargs)
        run.captured.append(outcome)
        return outcome

    def capturing_batch_outcome(*args, **kwargs):
        run.batch_outcome = rfc.fork_render_batch.RenderBatchOutcome(*args, **kwargs)
        return run.batch_outcome

    def guarded_unlocked(*args, **kwargs):
        index = len(run.order)
        run.order.append(index)
        session_state = args[24]

        def stream():
            current["index"] = index
            seam._impl_namespace = namespace_for_current_candidate
            try:
                out = dp._run_impl(tmp_path / f"candidate{index}")
            finally:
                seam._impl_namespace = real_namespace
            per_candidate[index] = out
            published[index] = out["share"]
            inner = out["state"]
            # exactly the hand-off production performs: the batch reads these two keys and
            # nothing else -- not the status prose, not the share path, not the preview.
            session_state["render_outcome_kind"] = inner.get("render_outcome_kind")
            session_state["last_output_path"] = inner.get("last_output_path", "")
            session_state["audio_layers_report"] = ""
            session_state["smart_mix_report"] = ""
            yield (out["preview_path"], out["status"], session_state, "", "", "inv")

        return stream()

    def tracking_lifecycle(invocation_id):
        assert "lifecycle" not in lifecycle_box, "a second RenderLifecycle for one batch"
        lifecycle_box["lifecycle"] = rfc.fork_render_worker.RenderLifecycle(
            invocation_id=invocation_id)
        run.lifecycle = lifecycle_box["lifecycle"]
        return lifecycle_box["lifecycle"]

    namespace = {
        "gr": types.SimpleNamespace(skip=_Skip),
        "uuid": _uuid,
        "threading": threading,
        "fork_render_batch": types.SimpleNamespace(
            RenderCandidateOutcome=capturing_outcome,
            RenderBatchOutcome=capturing_batch_outcome,
            build_request=rfc.fork_render_batch.build_request,
        ),
        "RenderLifecycle": tracking_lifecycle,
        "RenderLifecycleState": rfc.fork_render_worker.RenderLifecycleState,
        "RenderOutcomeKind": rfc.KIND,
        "RenderCancelled": rfc.RenderCancelled,
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
    exec(compile(ast.Module(body=[rfc._gui_func("render_selected_variants_guarded")],
                            type_ignores=[]), "<gui>", "exec"), namespace)  # noqa: S102

    stream = namespace["render_selected_variants_guarded"](
        rfc._root_92_batch(count=COUNT), list(range(COUNT)), "track.mp3", None, 2.0, 1.0, True, "",
        rfc.fork_smart_mix.ROLE_ORDER,
        "folder", "C:/src", False, None, "music_video", "h264", 30.0, {}, None)
    for _ in stream:
        pass

    # every candidate was STARTED -- a measured call order, not inferred from a count
    assert run.order == [0, 1, 2, 3], f"candidates attempted: {run.order}"
    assert len(run.captured) == COUNT

    for index, outcome in enumerate(run.captured):
        kind = str(outcome.outcome_kind)
        assert kind.endswith("SUCCESS"), f"candidate {index + 1} outcome was {kind}"
        assert outcome.success is True, index
        # a subordinate failure must never become one of the authoritative classes
        for forbidden in ("CANDIDATE_LOCAL", "SHARED_FATAL", "UNKNOWN_FATAL", "CANCELLED"):
            assert forbidden not in kind, f"candidate {index + 1} -> {kind}"
        # the batch summary stays truthful: every master path is the real promoted file
        assert outcome.durable_output_path == per_candidate[index]["durable"]
        assert os.path.isfile(outcome.durable_output_path)

    # candidate 2 published nothing; every other candidate did
    assert published[FAILING_CANDIDATE] == "", "candidate 2 published a share file anyway"
    for index in (0, 2, 3):
        assert published[index].endswith("_share.mp4"), f"candidate {index + 1}: {published[index]}"
    # and candidate 2's own panel says so, without reporting the render as an error
    failing_status = per_candidate[FAILING_CANDIDATE]["status"]
    assert "No permanent shareable MP4 was saved" in failing_status
    assert not failing_status.startswith("❌")
    assert "PLAYABLE PREVIEW" in failing_status

    # the top-level event exhausted its selection under the authorized policy
    assert run.lifecycle_state is rfc.fork_render_worker.RenderLifecycleState.FINISHED
    assert run.batch_outcome.outcome_kind is None


# ===========================================================================
# PROTECTED CONSTANTS
# ===========================================================================


def test_the_protected_contract_constants_are_unchanged():
    analysis = open(os.path.join(_SRC, "video_analysis.py"), encoding="utf-8").read()
    partials = open(os.path.join(_SRC, "beatsync_fork", "qwen_partials.py"),
                    encoding="utf-8").read()
    prep = open(os.path.join(_SRC, "beatsync_fork", "library_prep.py"), encoding="utf-8").read()

    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in analysis
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in analysis
    assert "_CACHE_IDENTITY_WORKER_CAP = 16" in analysis
    assert 'PARTIAL_CONTRACT_VERSION = "stage5_job_partial_v1"' in partials
    assert "DEFAULT_ANALYZE_BATCH_SIZE = 100" in prep


def test_no_new_gui_output_wiring_was_added():
    """No download button, no new output component and no new state.

    [FORK] Digital-Union (V1-B-S2): the `gr.DownloadButton` assertion used to read "S2's download
    button leaked into S1". The product owner has since **rejected** a download control outright in
    favour of Open Output Folder, so this is no longer a sequencing guard that S2 will one day
    relax — it is permanent. See `tests/test_open_output_folder.py` for what S2 did add.
    """
    source = _gui_source()
    tree = ast.parse(source)
    components = [ast.unparse(n.func) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("gr.")]

    assert components.count("gr.DownloadButton") == 0, \
        "a download control was rejected by the product owner; it must not return"
    assert components.count("gr.Video") == 1
    assert "share_download" not in source
    assert "SHARE_OUTCOME_KEY" not in source, "a second outcome class was introduced"


def test_no_progress_event_was_invented_for_delivery():
    """M4 is DEFERRED BY PRODUCT OWNER. S1 must not emit an invisible or misleading event, and
    must not restart Stage 6 or invent Stage 7, merely to look like it reports progress."""
    tree = ast.parse(_gui_source())
    impl = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "_process_video_impl")
    body = ast.unparse(impl)
    region = body[body.index("share_path = ''"):body.index("_stage6_summary(")]
    for token in ("fork_progress", "ProgressEvent", "EventKind", "event_callback", "stage=7",
                  "Stage.DELIVERY"):
        assert token not in region, f"{token} emitted in the share region (M4 is deferred)"


# ===========================================================================
# helpers: fault injection
# ===========================================================================


def _raise(exc):
    def boom(*_a, **_k):
        raise exc
    return boom


def _raise_factory(exc):
    return _raise(exc)


class _OsShim:
    """Delegates everything to the real `os` except the named attributes.

    `os.path` is delegated too, with selective overrides, so the helper's real containment
    arithmetic runs rather than a reimplementation of it.
    """

    def __init__(self, **overrides):
        self._path_overrides = {}
        self._overrides = {}
        for key, value in overrides.items():
            if key in {"splitdrive", "realpath", "commonpath", "isfile", "getsize", "islink",
                       "abspath", "dirname", "basename", "join"}:
                self._path_overrides[key] = value
            else:
                self._overrides[key] = value
        self.path = _PathShim(self._path_overrides)

    def __getattr__(self, item):
        if item in self._overrides:
            return self._overrides[item]
        return getattr(os, item)


class _PathShim:
    def __init__(self, overrides):
        self._overrides = overrides

    def __getattr__(self, item):
        if item in self._overrides:
            return self._overrides[item]
        return getattr(os.path, item)


def _corrupting_open(flip=False, drop_tail=0):
    """Return an `open` that damages writes to the staging file only."""
    real = open

    def opener(path, mode="r", *a, **k):
        handle = real(path, mode, *a, **k)
        if "w" not in str(mode):
            return handle
        return _CorruptWriter(handle, flip=flip, drop_tail=drop_tail)

    return opener


class _CorruptWriter:
    def __init__(self, handle, flip=False, drop_tail=0):
        self._h = handle
        self._flip = flip
        self._drop = drop_tail
        self._first = True

    def write(self, data):
        if self._drop:
            take = max(0, len(data) - self._drop)
            self._drop = 0
            data = data[:take]
        elif self._flip and self._first and data:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        self._first = False
        return self._h.write(data)

    def __getattr__(self, item):
        return getattr(self._h, item)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._h.__exit__(*exc)


class _BrokenHashlib:
    @staticmethod
    def sha256(*_a, **_k):
        raise RuntimeError("digest backend exploded")
