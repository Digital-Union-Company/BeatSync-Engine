#!/usr/bin/env python3
"""Stage-5 per-job partial artifacts: the filename contract, the payload and the atomic writer.

[FORK] Digital-Union (A1).

**What problem this exists for.** ``stage5_qwen_scene_worker.main()`` accumulates every job's
semantics in memory and writes its response JSON **once**, after the whole job loop has returned.
So while the shared worker is in flight, no completed job is durable anywhere: a worker that dies on
job 100 of 100 discards 99 jobs of finished GPU inference, and the parent's existing per-job
checkpointing (D1) cannot help because it only runs *after* a usable response arrives. Bounding the
submission (``library_prep.DEFAULT_ANALYZE_BATCH_SIZE``) bounds that exposure; it does not close it.

This module lets the worker publish one small artifact per job, the moment that job's loop returns,
next to the response file it would eventually have written.

**A1 wrote these artifacts; A2-S1 reads them, and only on one narrow path.** The worker's final
response JSON is still the **primary** semantic authority: when it is usable, nothing here is opened,
globbed or validated at all. Only when that response is unusable — a dead, killed, timed-out or
unparseable worker — may ``video_analysis._recover_jobs_from_partials`` admit **same-invocation**
partials as fallback evidence for the jobs that did finish.

Three limits are permanent, not transitional:

* **Same invocation only.** :func:`load_partial` refuses a partial whose ``invocation`` is not the
  one the caller names, and :func:`partial_glob` is anchored on that invocation's own stem. There is
  no cross-invocation recovery and no cross-run resumability — a *later* run still re-analyses a
  source whose record was never checkpointed.
* **Evidence, never a verdict.** A recovered partial supplies one job's ``semantics``/``timings`` and
  nothing else. ``_qwen_job_completed`` and ``_cache_entry_is_complete`` in ``video_analysis.py``
  remain the only completion rules and still decide, unchanged, whether the job may be checkpointed.
* **No durability upgrade.** ``POWER_LOSS_DURABILITY`` is still not claimed, and a partial is still
  not a cache record: it is read, used once to rebuild one response envelope, and left on disk.

**The worker emits evidence; the parent judges it.** A partial carries the job's ``semantics`` and
``timings`` verbatim and **no verdict** — no ``ai_enabled``, no completion flag, no requested-candidate
set. ``_qwen_job_completed`` and ``_cache_entry_is_complete`` live in ``video_analysis.py`` and stay the
only rules; evaluating either here would be the second completion rule
``.claude/rules/stage5-cache-durability.md`` forbids outright, and a second rule is exactly how a
failed Qwen run once became a permanent AI-complete cache hit.

**This is a transport artifact, not a cache record.** :data:`PARTIAL_CONTRACT_VERSION` versions the
artifact's own shape and is deliberately **not** derived from ``CACHE_CONTRACT_VERSION``: it reaches no
cache key, no signature and no persisted record, and bumping it must never invalidate semantic cache.

**Stdlib only**, like every module in this package — the worker imports it under the same guarded
pattern it already uses for ``beatsync_fork.qwen_progress``, so a partial or broken checkout degrades
to "no partials" rather than to a broken Qwen run.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Callable, Dict, Mapping

PARTIAL_CONTRACT_VERSION = "stage5_job_partial_v1"
"""Version of the *artifact shape* below, and nothing else.

Bump it when the payload's meaning changes. A reader must refuse a value it does not recognise rather
than guess. This constant reaches no cache key, no cache signature and no persisted cache record, so
it is **not** a generation marker in the sense ``CACHE_CONTRACT_VERSION`` is — do not couple them.
"""

PARTIAL_SUFFIX = ".partial.json"
"""Terminal suffix of a published partial.

Load-bearing: :func:`write_partial` publishes through ``tempfile.mkstemp(prefix=<name>., suffix=.tmp)``,
so a killed write leaves ``<name>.partial.json.<random>.tmp`` behind. Because this suffix is terminal
and :func:`partial_glob` anchors on it, such a leftover can never be discovered or read as a partial.
"""

_JOB_TOKEN = "_job_"
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_TOKEN_LIMIT = 80


def safe_job_id(value: str) -> str:
    """Sanitise a job id for use inside a filename.

    Deliberately the **same algorithm** as the worker's own ``_safe_file_token``, which already names
    its ``llama-server`` log files. It cannot be imported from there (that module needs ``cv2``/``PIL``),
    so ``tests/test_stage5_job_partials.py`` executes the real worker function and asserts the two
    agree character for character rather than trusting this copy.

    It is **lossy** — distinct ids can sanitise to the same token — which is why a filename is only
    ever a hint here and :func:`build_payload` records the exact ``job_id`` as the authority.
    """
    return _UNSAFE_FILENAME_CHARS.sub("_", str(value))[:_SAFE_TOKEN_LIMIT] or "item"


def response_stem(response_path: str) -> str:
    """The response path with its final suffix removed — the sibling-artifact prefix.

    Mirrors the worker's ``_response_prefix`` (``Path(response_path).with_suffix("")``), which is how
    it already names ``*_llama_server_ctx*_stdout.log``, ``*_llama_cli_stdout.log`` and
    ``*_llama_frames``. Reusing that convention means a partial needs no new directory, no new identity
    formula and no knowledge of ``VIDEO_ANALYSIS_CACHE_DIR``: the parent chose the response path, so
    the parent already owns where partials land.

    ``os.path.splitext`` rather than ``pathlib`` on purpose, so the caller's separator spelling is
    preserved rather than rewritten. The two are **path**-equivalent, not string-identical: on Windows
    ``Path("/c/x").with_suffix("")`` returns ``\\c\\x``. They always agree on the basename — which is
    the part that becomes :func:`invocation_id` — and in production the question cannot arise, because
    the parent builds the response path with ``os.path.join``.
    """
    return os.path.splitext(str(response_path))[0]


def invocation_id(response_path: str) -> str:
    """The per-invocation identity a partial declares: the response file's stem basename.

    The parent mints the response path (and therefore this token) once per worker launch and passes it
    on the command line; the worker only **echoes** it. So no identity is minted inside the worker, no
    clock is read for identity here, and a future reader can scope discovery to the one invocation it
    is recovering instead of trusting a directory listing.
    """
    return os.path.basename(response_stem(response_path))


def partial_path(response_path: str, job_index: int, job_id: str) -> str:
    """Absolute sibling path for one job's partial.

    ``{response_stem}_job_{job_index:05d}_{safe_job_id}.partial.json``

    ``job_index`` is the worker's own 1-based loop position. It is here for **filename
    disambiguation and loop-position attribution** only: two jobs that happen to share a ``job_id``
    get two distinct files instead of one silently replacing the other, and a leftover partial names
    the position that produced it.

    **It does not resolve duplicate job-ID collisions in the parent.** If two request jobs ever
    carried the same id, ``_complete_deferred_qwen_batch``'s ``selected_by_job`` / ``job_to_video`` /
    ``job_to_cache`` mappings would already have collapsed *before the worker was launched*, so no
    filename scheme could mitigate it. (Today they cannot: ``analyze_video_sources`` numbers jobs with
    ``enumerate(existing, 1)``, so every ``index`` — and therefore every ``job_id`` — is unique.)
    """
    stem = response_stem(response_path)
    return f"{stem}{_JOB_TOKEN}{int(job_index):05d}_{safe_job_id(job_id)}{PARTIAL_SUFFIX}"


def path_matches(path: str, response_path: str, *, job_index: Any, job_id: Any) -> bool:
    """Is ``path`` the **one** legal filename for this invocation + index + job id? Never raises.

    [FORK] Digital-Union (A2-H1): the filename/payload consistency predicate.

    A1's writer derives the filename from the same two values it records in the payload, so for any
    genuine artifact ``path == partial_path(response_path, payload["job_index"], payload["job_id"])``.
    A file where those disagree is therefore **self-contradictory about its own identity** — it was
    not produced by this worker — and a reader that silently accepted it would be trusting a filename
    and a payload that do not describe the same job.

    Implemented by **recomputing** :func:`partial_path` from the payload rather than by parsing the
    filename, which is deliberate: parsing would be a second, drift-prone decoder of A1's naming
    formula, and `safe_job_id` is lossy so a parsed token cannot be inverted anyway. Recomputation
    applies the identical (lossy) sanitisation to the payload's exact ``job_id``, so a valid artifact
    always matches and no new naming rule is introduced.

    ``normpath``/``normcase`` only — the two spellings can legitimately differ (``find_partials``
    joins a directory, so a bare relative response path yields ``.\\name`` where
    :func:`partial_path` yields ``name``), and they name the same file. Nothing here touches the
    filesystem: it is pure string comparison, so it cannot be raced and cannot raise on a missing
    file.
    """
    try:
        expected = partial_path(str(response_path), int(job_index), job_id)
        return (os.path.normcase(os.path.normpath(str(path)))
                == os.path.normcase(os.path.normpath(expected)))
    except Exception:
        return False


def partial_glob(response_path: str) -> str:
    """Discovery pattern for every partial belonging to **one** invocation.

    Anchored on both ends: the stem scopes it to this invocation, and :data:`PARTIAL_SUFFIX` excludes
    ``mkstemp`` leftovers. A1 itself calls this nowhere — it is part of the filename contract, owned
    here so a future reader cannot re-derive the pattern wrongly and start reading half-written temps.
    """
    return f"{response_stem(response_path)}{_JOB_TOKEN}*{PARTIAL_SUFFIX}"


def build_payload(
    *,
    response_path: str,
    job_index: int,
    job_total: int,
    job_id: str,
    video_file: str,
    semantics: Mapping[str, Any],
    timings: Mapping[str, Any],
    backend: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """One job's evidence, and nothing beyond it.

    ``semantics`` and ``timings`` are stored **verbatim** — the same two objects
    ``_run_semantics_for_video`` returned and the same two the final response would have carried under
    ``semantics_by_job[job_id]`` / ``timings_by_job[job_id]``. Nothing is reshaped, filtered, rounded,
    summarised or re-keyed, which is what lets a reader apply the parent's existing count and telemetry
    handling unchanged.

    Deliberately **absent**, and not to be added without a contract review:

    * any completion verdict, ``ai_enabled`` or "complete" flag — the parent decides;
    * the requested-candidate set — that would be the worker vouching for its own request, and the
      retained request JSON already records it under the same ``job_id``;
    * a cache signature — only a *cross-invocation* reader would need one, and binding it now would
      invite a reader to trust the partial for identity instead of re-deriving it;
    * any creative or musical state. Persisted Stage-5 semantics are media-neutral (P2): nothing about
      the music, the edit style, a seed, a preset or a director request may cross this boundary.

    ``job_total`` is **diagnostic only** — it says how far a batch got without listing the directory —
    and must never be read by validation.

    ``backend`` is **diagnostic only** and must never influence a completion decision. It records what
    ``client`` reported at the time this job ran, which is not constant across a batch: ``load_seconds``
    *accumulates* and ``batch_size`` is *reassigned* whenever ``restart_server_with_slots`` succeeds
    mid-run. A reader that wants the values a successful response would have reported must therefore
    take them from the **last** valid partial, never the first.
    """
    payload: Dict[str, Any] = {
        "partial_contract": PARTIAL_CONTRACT_VERSION,
        "invocation": invocation_id(response_path),
        "job_index": int(job_index),
        "job_total": int(job_total),
        "job_id": str(job_id),
        "video_file": str(video_file),
        "semantics": semantics,
        "timings": timings,
    }
    if backend is not None:
        payload["backend"] = backend
    return payload


def write_partial(
    target_path: str,
    payload: Mapping[str, Any],
    *,
    on_error: Callable[[str], None] | None = None,
) -> str | None:
    """Publish one partial atomically. Returns the published path, or ``None`` on failure.

    Mirrors ``video_analysis._save_cache``'s transport design, including the detail that cost D1 a
    measured defect: the temp file is **unique** (``tempfile.mkstemp`` in the *target's own* directory),
    never a shared ``path + ".tmp"``. With a shared name two writers could have one truncate the
    other's temp, so the first writer's ``os.replace`` published the *other* payload while reporting
    success.

    ```
    PROCESS_CRASH_ATOMICITY = YES   - the target is only ever created by os.replace of a fully
                                      written, flushed and fsynced temp, so a reader sees a complete
                                      partial or no partial. Never a half-written one: this function
                                      never opens `target_path` for writing.
    POWER_LOSS_DURABILITY   = NOT CLAIMED - the temp is fsynced, the containing directory is not.
    ```

    **A failed write must never cost a Qwen job**, which has already spent real GPU time, so every
    ordinary failure is contained here and reported through ``on_error``. ``Exception`` only —
    ``KeyboardInterrupt`` and ``SystemExit`` still propagate, because a cancelled or exiting process
    must not be held up writing diagnostics.

    ``json.dump``'s ``allow_nan`` default is left alone on purpose. This is a transport primitive, not
    the semantic-policy layer — the same reason ``_save_cache`` does not sanitise its payload (T1).

    There is deliberately **no deletion path here and nowhere else in this module**: published partials
    live in ``input/video_analysis_cache/``, which project policy preserves deliberately — including
    the retained ``qwen_batch_request_*`` / ``qwen_batch_response_*`` worker files. A retention policy
    is a separate product-owner decision. The only thing ever removed is this function's own temp.
    """
    target = str(target_path)
    tmp_path = ""
    try:
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(target) + ".", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = ""
        return target
    except Exception as exc:
        if on_error is not None:
            try:
                on_error(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        return None
    finally:
        # Best effort, and never allowed to raise: a leftover temp is untidy, a raised exception here
        # would cost the job this whole function exists not to cost.
        #
        # [FORK] Digital-Union (A2-H1): the removal is attempted directly, with no `os.path.exists`
        # probe in front of it. The probe was never a guard - `os.remove` on an absent file raises
        # `FileNotFoundError`, which *is* an `OSError` and was already swallowed below - so it only
        # added a syscall, a TOCTOU window, and one statement outside the `try` that could in
        # principle raise and defeat the never-raise contract this block exists to honour.
        # `tmp_path` is `""` after a successful `os.replace`, so a PUBLISHED partial is never a
        # removal candidate: the only file this can ever delete is the temp this call created.
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def find_partials(response_path: str) -> list[str]:
    """Every published partial of **one** invocation, in loop-position order. Never raises.

    [FORK] Digital-Union (A2-S1): the production discovery call, and the reason it lives here rather
    than in the parent is twofold.

    **Ownership.** :func:`partial_glob` already declares the pattern, and the comment there says why
    it is owned in one place — a reader that re-derived it wrongly would start reading half-written
    temps. Enumerating here keeps the pattern and its application together instead of splitting them
    across the boundary.

    **The parent must not enumerate its own cache directory.** ``video_analysis.py`` is guarded
    against exactly that (``glob.glob``, ``os.scandir``, ``os.unlink``, ``shutil.rmtree`` are all
    banned there, by test) so that "Stage 5 has no cache sweeper and no migration" stays a mechanical
    fact rather than a promise. Recovery needs to *read* two or three files out of that directory, not
    to acquire a licence to walk it, so the listing happens behind this contract.

    Implemented with ``os.listdir`` and the two anchors :func:`partial_glob` expresses as a pattern —
    deliberately **no** ``glob``/``fnmatch`` import, because neither is on this package's curated
    stdlib allowlist and a published artifact contract is not worth widening it for. ``os.listdir``
    rather than ``os.scandir`` for the same reason the parent is banned from both: a plain name list
    cannot grow into a directory walk. :func:`partial_glob` stays the published pattern and a test
    pins the two to agree.

    Both anchors are load-bearing:

    * the **prefix** is this invocation's own stem basename plus ``_job_``, so another run's artifacts
      in the same directory are never enumerated — which is what makes recovery same-invocation by
      construction rather than by a later check;
    * the **suffix** is the terminal :data:`PARTIAL_SUFFIX`, so a killed write's
      ``<name>.partial.json.<random>.tmp`` can never be returned.

    Sorted, so the caller's iteration order is the zero-padded ``job_index`` order and never the
    filesystem's. An unreadable or missing directory yields ``[]`` — absence of evidence, never an
    exception into the caller.
    """
    try:
        stem = response_stem(str(response_path))
        directory = os.path.dirname(stem) or "."
        prefix = os.path.basename(stem) + _JOB_TOKEN
        if not os.path.basename(stem):
            return []
        return sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith(prefix) and name.endswith(PARTIAL_SUFFIX)
        )
    except Exception:
        return []


def load_partial(path: str, *, expected_invocation: str) -> Dict[str, Any] | None:
    """Read one published partial, or ``None``. Never raises an ordinary exception.

    [FORK] Digital-Union (A2-S1): the read side of the artifact contract, owned here next to the
    writer so a reader cannot re-derive it wrongly. It answers exactly one question — *is this file a
    well-formed partial belonging to the invocation the caller names?* — and deliberately stops there.

    Three checks, and no fourth:

    * the bytes parse as a **JSON object** (a list, a number or a truncated file is not a partial);
    * ``partial_contract`` is a value this code knows. An unrecognised version is **refused, never
      guessed** — a future shape may mean something different, and the whole point of versioning the
      artifact is that a reader can tell;
    * ``invocation`` equals ``expected_invocation``, which the parent derives from the response path
      it *itself* chose for the worker it *itself* launched. This is what keeps recovery
      same-invocation: a partial from any other run is refused here even if it is otherwise perfect.

    Deliberately **not** done here, and not to be added:

    * **no completion verdict.** ``_qwen_job_completed`` and ``_cache_entry_is_complete`` live in
      ``video_analysis.py`` and stay the only rules — evaluating one here is the second completion
      rule ``.claude/rules/stage5-cache-durability.md`` forbids;
    * **no job/source authority.** Whether ``job_id`` belongs to the live request and whether
      ``video_file`` is the source the parent asked about are the *parent's* questions, answered with
      the parent's own ``selected_by_job`` and ``_same_source``. A filename is never identity;
    * **no write, no rename, no delete.** This function only ever opens a file for reading;
    * **no cache knowledge** — no signature, no key, no record, no ``VIDEO_ANALYSIS_CACHE_DIR``.

    ``Exception`` only, like :func:`write_partial`: ``KeyboardInterrupt``/``SystemExit`` still
    propagate. A missing file, a permission error, a directory handed in place of a file, a
    non-``str`` path and malformed JSON all return ``None`` — one unreadable partial must cost its
    siblings nothing.
    """
    if not isinstance(expected_invocation, str) or not expected_invocation:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("partial_contract") != PARTIAL_CONTRACT_VERSION:
        return None
    if payload.get("invocation") != expected_invocation:
        return None
    return payload


__all__ = [
    "PARTIAL_CONTRACT_VERSION",
    "PARTIAL_SUFFIX",
    "build_payload",
    "find_partials",
    "invocation_id",
    "load_partial",
    "partial_glob",
    "partial_path",
    "path_matches",
    "response_stem",
    "safe_job_id",
    "write_partial",
]
