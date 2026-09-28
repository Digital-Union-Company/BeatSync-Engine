#!/usr/bin/env python3
"""Video source analysis for Auto Mode.

This module builds a reusable library of source moments. FFmpeg/OpenCV provide
fast deterministic signals for every candidate; Qwen3-VL optionally adds
semantic editing tags for the best candidates.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Sequence

import cv2
import numpy as np

from gpu_cpu_utils import GPU_AVAILABLE, cp
from ffmpeg_processing import (
    detect_video_scene_changes,
    get_video_duration,
    get_video_fps,
    get_video_resolution,
)
from logger import ROOT_DIR, setup_environment

# [FORK] Digital-Union: structured progress events (stdlib-only fork module).
from beatsync_fork import progress as fork_progress
# [FORK] Digital-Union (Phase 2B): streaming Qwen worker runner + stdout progress protocol.
from beatsync_fork import qwen_progress as fork_qwen


setup_environment()

ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"
DEFAULT_QWEN_MODEL_DIR = os.path.join(ROOT_DIR, "bin", "models")
DEFAULT_QWEN_GGUF_MODEL = os.path.join(DEFAULT_QWEN_MODEL_DIR, "Qwen3VL-2B-Instruct-Q8_0.gguf")
DEFAULT_QWEN_MMPROJ_MODEL = os.path.join(DEFAULT_QWEN_MODEL_DIR, "mmproj-Qwen3VL-2B-Instruct-F16.gguf")
DEFAULT_LLAMA_CPP_DIR = os.path.join(ROOT_DIR, "bin", "llama-bin-win-vulkan-x64")
VIDEO_ANALYSIS_CACHE_DIR = os.path.join(ROOT_DIR, "input", "video_analysis_cache")
_LLAMA_VERSION_TOKENS: Dict[str, str] = {}

# [FORK] Digital-Union (D1): private completion signal from the Qwen facade to its caller. Every
# caller pops it before `timings.update(...)`, so it never reaches a cache payload - the cached
# record carries the *consequence* (ai_enabled) rather than this bookkeeping flag.
_QWEN_COMPLETED_KEY = "_qwen_completed"


def _new_run_stats() -> Dict[str, Any]:
    """[FORK] Digital-Union (R1): per-invocation accounting of what THIS run actually executed.

    Stage 5 returns two different kinds of truth and used to conflate them. The `qwen_*` aggregates
    near the end of `analyze_video_sources` sum over `videos`, which includes every cache hit, so a
    fully warm run reported the cached library's historical tag counts, model id, batch size and
    inference seconds as though this invocation had produced them. Production proved it: 845/845
    cache hits, zero sources analysed, zero Qwen workers launched - and a console summary reading
    `Qwen tags: 8704/8704 in 3031.9s`.

    This dict is the *execution* half. It is created once per `analyze_video_sources` call, threaded
    only into the paths that analyse an uncached source, and never derived from a cached record, so
    a cache hit cannot inflate it. It is returned as top-level metadata and is **never** written into
    a source-cache payload: it is a separate object from `video_data`, so no path exists by which it
    could reach `_checkpoint_cache`.

    The two places that increment it do so inline rather than through a shared helper. That is
    deliberate - `_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` are both
    AST-extracted and executed by `tests/test_stage5_cache_completion.py`, so they must stay
    self-contained with respect to helpers outside that suite's extraction list.
    """
    return {
        "qwen_jobs": 0,
        "qwen_completed_jobs": 0,
        "qwen_incomplete_jobs": 0,
        # [FORK] Digital-Union (R2): requested, decoded and tagged are three different facts, and the
        # UI needs the first as its denominator. `qwen_requested_count` is what was SUBMITTED,
        # `qwen_frame_count` is what the worker PROVED it decoded, `qwen_tag_count` is what was
        # actually merged. Only "requested" is knowable without a usable worker response, which is
        # what makes a failed attempt reportable at all.
        "qwen_requested_count": 0,
        "qwen_frame_count": 0,
        "qwen_tag_count": 0,
        "qwen_seconds": 0.0,
        "qwen_inference_seconds": 0.0,
    }

# [FORK] Digital-Union (D1 R2): the worker always publishes a per-job timings entry once a job has
# finished (`timings_by_job["single"]` for the legacy single-job request), while every
# `_run_qwen_worker` failure path returns `{}`. Membership is therefore the completion evidence -
# the same rule the batch path uses - and it is what makes an empty `semantics` dict from a
# *finished* worker distinguishable from a worker that never produced a response at all.
_QWEN_SINGLE_JOB_ID = "single"

# [FORK] Digital-Union (D2): the ONE constant owning cache identity *and* the persisted completion
# contract. It is both the first component of every cache signature and the value stored as
# ``cache_contract`` in every record, so a key and its payload can never disagree about which
# generation they belong to.
#
# **Bump this whenever a change alters what a cached result means**, even if the candidate schema is
# untouched and `ANALYSIS_VERSION` therefore stays put: the identity algorithm, the Qwen prompt, the
# semantic normalisation/output contract, or any result-affecting Qwen configuration not already
# represented in the signature. Do NOT hash the worker source into the key - a progress or
# performance-only worker edit must not invalidate semantic cache.
CACHE_CONTRACT_VERSION = "stage5_cache_v2"

# Bounded content fingerprint geometry. 1 MiB chunks; files at or below three chunks are read whole.
_FINGERPRINT_CHUNK = 1 << 20
_FINGERPRINT_WHOLE_FILE_LIMIT = 3 * _FINGERPRINT_CHUNK
_FINGERPRINT_DIGEST_SIZE = 16

# Canonical Qwen-config identity for a deterministic-only (no_ai) run: there is no Qwen
# configuration to represent, and keying one in would make unrelated env changes re-key a
# deterministic cache.
_NO_AI_CONFIG_TOKEN = "cfg_no_ai"

# [FORK] Digital-Union (D1 R2): `timings["candidate_scoring_seconds"]` is written immediately after
# `_measure_windows` inside the `cap.isOpened()` branch of `_analyze_single_video`, and nowhere else.
# Its presence therefore proves the deterministic candidate pass really ran, which is how a genuine
# "no usable moments" result is told apart from an OpenCV-open failure - both of which end up with
# an empty candidate list.
_DETERMINISTIC_SCORING_KEY = "candidate_scoring_seconds"


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        v = float(value)
    except Exception:
        v = default
    if not math.isfinite(v):
        v = default
    return max(lo, min(hi, v))


def _safe_name(path: str) -> str:
    return os.path.basename(path) or path


def _hash_text(text: str, length: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def _resolve_qwen_backend_paths(qwen_model_path: str | None) -> Dict[str, str]:
    llama_dir = os.environ.get("BEATSYNC_QWEN_LLAMA_DIR", DEFAULT_LLAMA_CPP_DIR)
    model_override = os.environ.get("BEATSYNC_QWEN_LLAMA_MODEL")
    mmproj_override = os.environ.get("BEATSYNC_QWEN_LLAMA_MMPROJ")

    requested = os.path.abspath(qwen_model_path) if qwen_model_path else ""
    if model_override:
        model_path = os.path.abspath(model_override)
    elif requested and os.path.isfile(requested) and requested.lower().endswith(".gguf"):
        model_path = requested
    else:
        candidates = []
        if requested:
            candidates.extend([
                os.path.join(requested, os.path.basename(DEFAULT_QWEN_GGUF_MODEL)),
                os.path.join(os.path.dirname(requested), os.path.basename(DEFAULT_QWEN_GGUF_MODEL)),
            ])
        candidates.append(DEFAULT_QWEN_GGUF_MODEL)
        model_path = next((path for path in candidates if os.path.exists(path)), DEFAULT_QWEN_GGUF_MODEL)

    if mmproj_override:
        mmproj_path = os.path.abspath(mmproj_override)
    else:
        candidates = [
            os.path.join(os.path.dirname(model_path), os.path.basename(DEFAULT_QWEN_MMPROJ_MODEL)),
            DEFAULT_QWEN_MMPROJ_MODEL,
        ]
        mmproj_path = next((path for path in candidates if os.path.exists(path)), DEFAULT_QWEN_MMPROJ_MODEL)

    return {
        "llama_dir": os.path.abspath(llama_dir),
        "server": os.path.abspath(os.path.join(llama_dir, "llama-server.exe")),
        "mtmd": os.path.abspath(os.path.join(llama_dir, "llama-mtmd-cli.exe")),
        "model": os.path.abspath(model_path),
        "mmproj": os.path.abspath(mmproj_path),
    }


def _qwen_backend_available(qwen_model_path: str | None) -> bool:
    paths = _resolve_qwen_backend_paths(qwen_model_path)
    return all(os.path.exists(paths[key]) for key in ["server", "mtmd", "model", "mmproj"])


def _qwen_backend_model_path(qwen_model_path: str | None) -> str:
    return _resolve_qwen_backend_paths(qwen_model_path)["model"]


def _path_signature_token(path: str) -> str:
    try:
        stat = os.stat(path)
        return f"{os.path.basename(path)}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        return f"{os.path.basename(path)}:missing"


def _bounded_fingerprint(path: str, size: int) -> str | None:
    """[FORK] Digital-Union (D2): deterministic bounded content fingerprint, or None if unreadable.

    BLAKE2b, 16-byte digest. The file size is hashed first, then content samples:

    * ``size <= 3 MiB`` - the whole file, read once;
    * ``size > 3 MiB``  - exactly three non-overlapping 1 MiB windows:
        - ``[0, 1 MiB)``
        - ``[mid, mid + 1 MiB)`` where
          ``mid = max(1 MiB, min(size // 2 - 512 KiB, size - 2 MiB))``
        - ``[size - 1 MiB, size)``
      The clamp guarantees the middle window never overlaps the head or the tail, so no byte is
      hashed twice and the bytes read are exactly ``min(size, 3 MiB)``.

    BLAKE2b rather than SHA-256 because it measured faster and this is **accidental** stale-cache
    prevention, not an adversarial problem: 128 bits is ample to stop a same-size/same-timestamp
    rewrite from being mistaken for the original. No cryptographic claim is made.

    Returns ``None`` on any read failure - the caller must then treat the source as having no strong
    identity at all rather than inventing a weak placeholder.
    """
    chunk = _FINGERPRINT_CHUNK
    digest = hashlib.blake2b(digest_size=_FINGERPRINT_DIGEST_SIZE)
    digest.update(str(size).encode("ascii"))
    try:
        with open(path, "rb") as handle:
            if size <= _FINGERPRINT_WHOLE_FILE_LIMIT:
                digest.update(handle.read())
            else:
                digest.update(handle.read(chunk))
                middle = max(chunk, min(size // 2 - chunk // 2, size - 2 * chunk))
                handle.seek(middle)
                digest.update(handle.read(chunk))
                handle.seek(-chunk, os.SEEK_END)
                digest.update(handle.read(chunk))
    except OSError:
        return None
    return digest.hexdigest()


def _full_fingerprint(path: str) -> str | None:
    """[FORK] Digital-Union (D2): whole-file BLAKE2b, for files small enough that sampling is silly.

    Used for ``llama-server.exe`` and ``llama-mtmd-cli.exe`` - measured at 9,216 and 82,944 bytes,
    hashed in ~13 ms combined - so those two components get exact identity for free.
    """
    digest = hashlib.blake2b(digest_size=_FINGERPRINT_DIGEST_SIZE)
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_FINGERPRINT_CHUNK), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _backend_component_token(path: str, *, full_hash: bool) -> str | None:
    """[FORK] Digital-Union (D2): strong identity for one backend file, or None if unreadable.

    Absolute path, not basename: the D1 study proved a basename-keyed token is identical for
    same-name/same-size/same-second files in different directories, so pointing
    ``BEATSYNC_QWEN_LLAMA_MODEL`` at another copy did not re-key the cache.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    fingerprint = (_full_fingerprint(path) if full_hash
                   else _bounded_fingerprint(path, stat.st_size))
    if fingerprint is None:
        return None
    return f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}:{fingerprint}"


def _llama_version_token(llama_dir: str) -> str:
    llama_dir = os.path.abspath(llama_dir)
    cached = _LLAMA_VERSION_TOKENS.get(llama_dir)
    if cached:
        return cached

    mtmd = os.path.join(llama_dir, "llama-mtmd-cli.exe")
    token = _path_signature_token(mtmd)
    if os.path.exists(mtmd):
        env = os.environ.copy()
        env["PATH"] = llama_dir + os.pathsep + env.get("PATH", "")
        try:
            result = subprocess.run(
                [mtmd, "--version"],
                cwd=llama_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = ((result.stdout or "") + (result.stderr or "")).strip()
            if version:
                token = version.splitlines()[0][:120]
        except Exception:
            pass
    _LLAMA_VERSION_TOKENS[llama_dir] = token
    return token


def _qwen_backend_signature_token(qwen_model_path: str | None) -> str | None:
    """[FORK] Digital-Union (D2): strong backend identity, or None when it cannot be proven.

    **Compute this ONCE per `analyze_video_sources` invocation and thread the result** - see the
    ``backend_token`` parameter on `_video_signature`/`_cache_path`. It used to be called from
    `_video_signature`, i.e. once per source; with content fingerprints that is 702 calls, measured at
    **61.7 minutes** if the two GGUFs are full-hashed. Memoised to one call it is ~20 ms.

    There is deliberately no module-level cache of the result, so a second invocation in the same
    process re-reads the backend and can observe a swapped model or llama build.

    Returns ``None`` if any component is missing or unreadable. There is no weak "ai_missing"-style
    placeholder any more: a stable token for an unprovable backend is exactly what lets a stale entry
    be reused, so the caller must fall back to *no cache* instead.
    """
    paths = _resolve_qwen_backend_paths(qwen_model_path)
    components = [
        ("model", False),      # ~1.83 GB  -> bounded fingerprint
        ("mmproj", False),     # ~0.82 GB  -> bounded fingerprint
        ("server", True),      # ~9 KB     -> full hash
        ("mtmd", True),        # ~83 KB    -> full hash
    ]
    tokens = []
    for key, full_hash in components:
        token = _backend_component_token(paths[key], full_hash=full_hash)
        if token is None:
            return None
        tokens.append(token)
    # The llama --version string stays as additional evidence, but is no longer load-bearing on its
    # own: if it cannot be probed it degrades to a stat token while the file fingerprints above remain
    # strong, so a version-probe failure is non-fatal by design.
    raw = "|".join(["llama_vulkan", *tokens, _llama_version_token(paths["llama_dir"])])
    return "ai_" + _hash_text(raw, length=20)


def _qwen_prompt_style_hint(audio_profile: Dict | None) -> str:
    """[FORK] Digital-Union (D2 R2): the style hint the worker will actually put in the prompt.

    Mirrors ``stage5_qwen_scene_worker._build_prompt`` exactly:
    ``audio_profile.get("smart_preset", "rhythmic_gmv_amv")``. Kept as its own function so the
    mirrored default lives in one place next to the reason it exists.
    """
    if not isinstance(audio_profile, dict):
        return "rhythmic_gmv_amv"
    return str(audio_profile.get("smart_preset", "rhythmic_gmv_amv"))


def _qwen_config_token(audio_profile: Dict | None = None) -> str:
    """[FORK] Digital-Union (D2): identity for the Qwen inputs that change what gets persisted.

    Keyed on **effective** values, mirroring the runtime's own parsing and clamping, so behaviourally
    identical configurations produce identical identity: an unset variable and its explicit default
    agree, and a malformed value agrees with the default the worker actually falls back to.

    * ``BEATSYNC_QWEN_MAX_WINDOWS``   - default 120, malformed -> 120, then ``max(0, value)``.
      D1 proved this changes *how many* candidates receive semantics while being absent from identity,
      so a 60-window cache was silently reused by a run asking for 120.
    * ``BEATSYNC_QWEN_FRAME_WIDTH``   - default 512, clamped 224..768 (the worker's own `_env_int`).
      Changes the image the VLM sees, so it changes the semantics.
    * ``BEATSYNC_QWEN_MAX_NEW_TOKENS`` - default 128, clamped 32..256. Can truncate the semantic JSON.
    * ``audio_profile["smart_preset"]`` (D2 R2) - **prompt context**. `analyze_video_sources` forwards
      the audio profile into the worker request, and the worker's `_build_prompt` interpolates this
      value straight into the Qwen prompt ("The music edit style is {style_hint}."). Two runs differing
      only in preset therefore get different semantics, yet shared one cache key before R2.

    Only fields *proven* to reach the persisted result are included - the whole ``audio_profile`` is
    deliberately **not** hashed, since almost all of it drives beat/render decisions rather than the
    prompt. Runtime/performance knobs are excluded too: slots, device, timeouts, batching. Resolved
    model/mmproj/llama paths are covered by the backend token, and ``BEATSYNC_DISABLE_QWEN`` is
    represented indirectly - it produces ``enable_ai=False`` and therefore the separate no-AI identity,
    which carries no Qwen configuration at all.
    """
    return "cfg_" + _hash_text("|".join([
        f"max_windows={_qwen_max_windows()}",
        f"frame_width={_env_int('BEATSYNC_QWEN_FRAME_WIDTH', 512, lo=224, hi=768)}",
        f"max_new_tokens={_env_int('BEATSYNC_QWEN_MAX_NEW_TOKENS', 128, lo=32, hi=256)}",
        f"smart_preset={_qwen_prompt_style_hint(audio_profile)}",
    ]), length=16)


def _video_signature(video_file: str, enable_ai: bool, qwen_model_path: str | None,
                     backend_token: str | None = None,
                     config_token: str | None = None,
                     audio_profile: Dict | None = None) -> str | None:
    """[FORK] Digital-Union (D2): the cache signature, or None when identity cannot be proven.

    Inputs: `CACHE_CONTRACT_VERSION`, `ANALYSIS_VERSION`, the absolute source path, ``st_size``,
    ``st_mtime_ns``, the bounded source fingerprint, the backend token (or ``no_ai``) and the Qwen
    config token. ``st_mtime_ns`` alone would not be enough: it closes the integer-second truncation
    but an exact-mtime restore still collides, which the fingerprint is what actually catches.

    The absolute path stays in identity on purpose. Identity is *location + content*, so a moved file
    re-keys, preserving the pre-D2 contract. Content-only identity would deduplicate copies - a real
    gain, but a semantic change, so D2 does not make it.

    Pass ``backend_token``/``config_token`` to reuse one invocation's values instead of recomputing
    per source.
    """
    try:
        stat = os.stat(video_file)
    except OSError:
        return None
    fingerprint = _bounded_fingerprint(video_file, stat.st_size)
    if fingerprint is None:
        return None

    if enable_ai:
        if backend_token is None:
            backend_token = _qwen_backend_signature_token(qwen_model_path)
        if backend_token is None:
            return None
        if config_token is None:
            config_token = _qwen_config_token(audio_profile)
    else:
        backend_token = "no_ai"
        config_token = _NO_AI_CONFIG_TOKEN

    # `model_token` keeps the pre-D2 local name deliberately: a Phase 2B test asserts the key mixes
    # it and carries no progress-related field, and that guarantee is still exactly what we want.
    model_token = backend_token
    raw = "|".join([
        CACHE_CONTRACT_VERSION,
        ANALYSIS_VERSION,
        os.path.abspath(video_file),
        str(stat.st_size),
        str(stat.st_mtime_ns),
        fingerprint,
        model_token,
        config_token,
    ])
    return _hash_text(raw, length=24)


def _cache_path(video_file: str, enable_ai: bool, qwen_model_path: str | None,
                backend_token: str | None = None,
                config_token: str | None = None,
                audio_profile: Dict | None = None) -> str | None:
    """[FORK] Digital-Union (D2): the cache filename, or None when identity cannot be proven.

    A ``None`` return is the fail-closed path: the caller performs no lookup and no write for that
    source, so nothing is ever stored under an unprovable identity. `_checkpoint_cache` already
    treats a ``None`` cache file as a no-op, which is the seam this uses.
    """
    signature = _video_signature(video_file, enable_ai, qwen_model_path,
                                 backend_token=backend_token, config_token=config_token,
                                 audio_profile=audio_profile)
    if signature is None:
        return None
    os.makedirs(VIDEO_ANALYSIS_CACHE_DIR, exist_ok=True)
    name = os.path.splitext(_safe_name(video_file))[0]
    return os.path.join(VIDEO_ANALYSIS_CACHE_DIR, f"{_hash_text(name, 8)}_{signature}.json")


def _same_source(cached_video_file: Any, expected_video_file: str) -> bool:
    """[FORK] Digital-Union (D1): does a cache payload describe the source we asked about?"""
    if not isinstance(cached_video_file, str) or not cached_video_file:
        return False
    try:
        a = os.path.normcase(os.path.abspath(cached_video_file))
        b = os.path.normcase(os.path.abspath(expected_video_file))
    except Exception:
        return False
    return a == b


def _coerce_count(value: Any, default: int = 0) -> int:
    """[FORK] Digital-Union (D1 R4): read a count out of a worker response without trusting it.

    The worker writes plain ints, but the response is parsed JSON from a subprocess, so a malformed
    or truncated-then-repaired payload can carry anything. `int(timing.get(...) or default)` raised
    ``ValueError`` on a non-numeric value and would have taken the whole analysis down with it -
    latent in the batch path before R4, and newly reachable in the single path once genuine counts
    started being reported for incomplete jobs too.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _is_count(value: Any) -> bool:
    """[FORK] Digital-Union (D1 R5): a real integer count, not a bool.

    ``bool`` subclasses ``int``, so ``isinstance(True, int)`` is ``True`` and a response carrying
    ``frame_count: true`` would otherwise have passed the R4 type check and then compared equal to 1.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _reported_count(timing: Any, key: str, default: int) -> int:
    """[FORK] Digital-Union (D1 R5): report the worker's own count, including a genuine zero.

    ``timing.get(key) or default`` silently rewrote a real ``frame_count = 0`` into the requested
    candidate count, so the stored timings claimed frames that were never decoded. Presence decides:
    a present value is used (coerced to 0 if unreadable, because "present but malformed" is not
    evidence of anything), and the caller's default applies only when the field is genuinely absent.
    """
    if isinstance(timing, dict) and key in timing:
        return _coerce_count(timing.get(key), 0)
    return int(default)


def _qwen_job_completed(timing: Any, envelope_present: bool,
                        requested_ids: Any, returned_ids: Any) -> bool:
    """[FORK] Digital-Union (D1 R5): did the AI work for one *submitted* Qwen job actually complete?

    Shared by the single and batch paths so the arithmetic exists once.

    Three earlier definitions of this were too weak, in order:

    1. *emptiness of ``semantics``* - conflated a finished worker with a dead one (fixed in R2);
    2. *response-envelope membership alone* - ``_run_semantics_for_video`` always returns a timings
       dict and ``main`` always records it under the job id, so the envelope only proves **the job
       loop returned**. Inside that loop ``_normalize_semantic`` yields ``{}`` for any candidate whose
       semantic content is invalid, ``_run_inference_wave`` classifies ``{}`` as failed and retries it
       (server retry, reduced-slot restart, serial fallback), and a candidate still failing is simply
       **absent** from the returned semantics (fixed in R4);
    3. *``tag_count == frame_count``* - proves every *decoded* frame was tagged, but
       ``_prefetch_candidate_frames`` returns only the frames it could actually read
       (``ready = [p for p in plans if p["image"] is not None]``), so ``frame_count`` may be **smaller
       than the requested candidate set**. A job that asked for 10 candidates, decoded 8 and tagged 8
       therefore looked complete while 2 candidates silently had no semantics at all (this, R5).

    Completion now requires the requested set to be covered end to end:

    * the expected per-job envelope exists and ``timing`` is a dict;
    * ``frame_count`` and ``tag_count`` are real integer counts (not bools);
    * the requested candidate set is non-empty;
    * ``frame_count == len(requested_ids)`` - every requested candidate was decoded;
    * ``tag_count == frame_count`` - every decoded frame produced a valid semantic;
    * ``returned_ids == requested_ids`` - and those semantics are for *our* candidates. A foreign id
      is not evidence that one of ours completed, and an extra id means the response does not match
      the request; the worker keys semantics by our own candidate ids, so an exact match is the
      contract.

    Neither the envelope nor a count is sufficient alone. ``requested_ids`` is the set selected by
    ``_select_ai_candidates`` - the candidates actually submitted - not every deterministic candidate.

    A source with no candidates never submits a job at all: that is the separate no-Qwen-work case
    handled by ``_cache_entry_is_complete``/``_deterministic_analysis_completed``, not here.
    """
    if not envelope_present or not isinstance(timing, dict):
        return False
    if not _is_count(timing.get("frame_count")) or not _is_count(timing.get("tag_count")):
        return False
    try:
        expected = {str(item) for item in requested_ids}
        returned = {str(item) for item in returned_ids}
    except TypeError:
        return False
    if not expected:
        return False
    frame_count = int(timing["frame_count"])
    tag_count = int(timing["tag_count"])
    if frame_count != len(expected) or tag_count != frame_count:
        return False
    return returned == expected


def _stored_ai_cache_is_consistent(data: Any) -> bool:
    """[FORK] Digital-Union (D1 R6): is a *persisted* AI-complete record self-consistent?

    This is deliberately **not** the R5 live-worker rule. `_qwen_job_completed` needs
    ``requested_ids``, and legacy payloads never stored them (nor the ``BEATSYNC_QWEN_MAX_WINDOWS``
    value in force), so R5 cannot be replayed against an old record. Calling it from the loader would
    mean inventing evidence.

    Instead this asks only what the stored fields can actually prove: *does this record contradict
    itself?* A read-only audit of the real 2196-entry runtime cache found 4 records claiming
    ``ai_enabled=True`` with ``qwen_frame_count=10`` but ``qwen_tag_count=9`` and 9 candidates marked
    ``ai_analyzed`` — precisely the false-complete shape D1 exists to prevent, and the pre-R6 loader
    accepted all four because it only checked ``bool(data["ai_enabled"])``.

    Required, all from fields already persisted:

    * ``candidates`` is a non-empty list of dicts with usable string ids;
    * ``timings`` is a dict carrying real integer ``qwen_frame_count``/``qwen_tag_count`` (not bools);
    * ``frame_count > 0`` and ``tag_count == frame_count``;
    * ``frame_count <= len(candidates)`` — it cannot have decoded more than existed;
    * the ``ai_analyzed`` ids are unique, are a subset of the candidate ids, and number exactly
      ``tag_count``.

    Deliberately **not** required: ``frame_count == len(candidates)``. A smaller value is the normal
    result of ``_select_ai_candidates`` limiting the submitted set, and the audit's two
    398-candidate/114-tagged and 525-candidate/119-tagged records are internally coherent. They may
    well be decoded subsets of a 120-candidate request, but nothing stored proves it, so they stay
    reusable and belong to the D2 completion-contract decision.
    """
    if not isinstance(data, dict):
        return False
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    timings = data.get("timings")
    if not isinstance(timings, dict):
        return False
    frame_count = timings.get("qwen_frame_count")
    tag_count = timings.get("qwen_tag_count")
    if not _is_count(frame_count) or not _is_count(tag_count):
        return False
    if frame_count <= 0 or tag_count < 0 or tag_count != frame_count:
        return False
    if frame_count > len(candidates):
        return False

    candidate_ids: List[str] = []
    analysed_ids: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not candidate_id:
            return False
        candidate_ids.append(candidate_id)
        if candidate.get("ai_analyzed") is True:
            analysed_ids.append(candidate_id)

    if len(set(analysed_ids)) != len(analysed_ids):
        return False
    if not set(analysed_ids) <= set(candidate_ids):
        return False
    return len(analysed_ids) == tag_count


def _deterministic_analysis_completed(data: Any) -> bool:
    """[FORK] Digital-Union (D1 R2): did the deterministic candidate pass actually run?

    Needed because two very different outcomes both end with ``candidates == []``: a source whose
    windows simply yielded no usable moments (a real, finished analysis) and a source OpenCV could
    not open at all (``"Warning: OpenCV could not open ...; candidate analysis skipped."``). Only the
    first may be cached; treating the second as complete would retire a readable source permanently
    on one transient decode failure.

    The discriminator is existing durable evidence, not a new schema field:
    ``timings["candidate_scoring_seconds"]`` is written once, immediately after ``_measure_windows``
    inside the ``cap.isOpened()`` branch, and is absent from every other path.
    """
    if not isinstance(data, dict):
        return False
    timings = data.get("timings")
    return isinstance(timings, dict) and _DETERMINISTIC_SCORING_KEY in timings


def _cache_entry_is_complete(data: Any, require_ai: bool) -> bool:
    """[FORK] Digital-Union (D1): the ONE rule for "is this payload complete enough to reuse?".

    Deliberately centralised: the pre-D1 code scattered the question across `_load_cache`'s
    ``ai_enabled`` check and several ``ai_enabled = True`` assignments, which is how a failed Qwen
    run could be written as AI-complete and then reused forever.

    * ``require_ai=False`` - a deterministic-complete result is reusable.
    * ``require_ai=True`` with candidates - reusable only if AI work genuinely completed.
    * no candidates - reusable only if the deterministic pass actually ran (see
      ``_deterministic_analysis_completed``). There is then nothing for Qwen to annotate, so such a
      source is reusable under ``require_ai`` too, expressed here rather than by falsifying
      ``ai_enabled``. An *empty candidate list alone* is not evidence of success: an OpenCV-open
      failure produces exactly the same shape, and caching that would retire the source forever.
    * ``ai_deferred`` truthy - never reusable, whatever else the payload says.
    """
    if not isinstance(data, dict):
        return False
    if data.get("analysis_version") != ANALYSIS_VERSION:
        return False
    # [FORK] Digital-Union (D2): the persisted contract must match this generation. Pre-D2 records
    # carry no marker and are rejected here - which is belt-and-braces, because the D2 signature also
    # re-keys every entry, so a pre-D2 file is never even looked up. There is deliberately no
    # D1-to-D2 compatibility branch: legacy records are orphaned and rebuilt once.
    if data.get("cache_contract") != CACHE_CONTRACT_VERSION:
        return False
    if not isinstance(data.get("video_file"), str) or not data.get("video_file"):
        return False
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return False
    if data.get("ai_deferred"):
        return False
    if not candidates:
        # [FORK] Digital-Union (D1 R2): checked for both require_ai modes - a failed deterministic
        # pass must be retried whether or not the run wants semantic tags.
        return _deterministic_analysis_completed(data)
    if not require_ai:
        return True
    if not data.get("ai_enabled"):
        return False
    # [FORK] Digital-Union (D1 R6): `ai_enabled` was written by code this branch has repeatedly proven
    # could set it wrongly, so an AI-complete claim must also survive a stored-consistency check.
    return _stored_ai_cache_is_consistent(data)


def _load_cache(path: str, require_ai: bool = False,
                expected_video_file: str | None = None) -> Dict | None:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # [FORK] Digital-Union (D1): one completion rule, plus a source-identity check when the
            # caller knows which source it asked for. Unexpected extra fields stay allowed.
            if not _cache_entry_is_complete(data, require_ai):
                return None
            if expected_video_file is not None and not _same_source(
                    data.get("video_file"), expected_video_file):
                return None
            return data
    except Exception as e:
        print(f"   Warning: could not read video analysis cache: {e}")
    return None


def _save_cache(path: str, data: Dict) -> None:
    """[FORK] Digital-Union (D1): atomic publish through a UNIQUE same-directory temp file.

    The pre-D1 writer used a single shared ``path + ".tmp"``. Two processes writing the same cache
    key could then have one truncate the other's temp file, so the first writer's ``os.replace``
    published the *other* payload while reporting success, and the second failed with
    ``FileNotFoundError`` into a warning the GUI's QuietConsole discards.

    ``os.replace`` still gives process-crash atomicity: a reader sees the old entry or the new one,
    never a partial final file. Power-loss durability is NOT claimed - the temp file is fsynced, but
    the containing directory is not.
    """
    tmp_path = ""
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    except Exception as e:
        print(f"   Warning: could not write video analysis cache: {e}")
    finally:
        # best effort: never leave our own unique temp behind, and never fail the render for it
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _checkpoint_cache(cache_file: str | None, video_data: Dict, require_ai: bool) -> bool:
    """[FORK] Digital-Union (D1): persist a source the moment it is genuinely complete.

    Before D1 the only save site was the terminal loop at the end of `analyze_video_sources`, so any
    interruption - including one after every deterministic analysis and every Qwen tag had finished -
    left zero durable new entries. This is the guard that makes an early save safe: it writes only
    what the central completion rule already accepts, so checkpointing can never publish a deferred
    or failed-AI record as a reusable one.
    """
    if not cache_file or not _cache_entry_is_complete(video_data, require_ai):
        return False
    _save_cache(cache_file, video_data)
    return True


def _fmt_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _env_int(name: str, default: int, lo: int = 1, hi: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if hi is None:
        hi = max(lo, value)
    return max(lo, min(hi, value))


def _video_analysis_workers(video_count: int) -> int:
    if video_count <= 1:
        return 1
    cpu = os.cpu_count() or 4
    # Decode + OpenCV scoring are native workloads, so multiple source videos can
    # safely use otherwise idle CPU cores. Keep the default conservative enough
    # to avoid crushing slow disks, and expose an env override for Ryzen-class CPUs.
    default_workers = min(video_count, max(1, cpu // 4))
    return _env_int("BEATSYNC_VIDEO_ANALYSIS_WORKERS", default_workers, lo=1, hi=video_count)


def _candidate_metric_workers(window_count: int, use_gpu: bool) -> int:
    if use_gpu or window_count < 80:
        return 1
    cpu = os.cpu_count() or 4
    default_workers = min(4, max(1, cpu // 4), window_count)
    return _env_int("BEATSYNC_CANDIDATE_METRIC_WORKERS", default_workers, lo=1, hi=max(1, window_count))


def _opencv_decode_threads() -> int:
    cpu = os.cpu_count() or 4
    default_threads = min(8, max(2, cpu // 2))
    return _env_int("BEATSYNC_OPENCV_FFMPEG_THREADS", default_threads, lo=1, hi=max(1, cpu))


def _open_video_capture(video_file: str) -> cv2.VideoCapture:
    """Open video with FFmpeg decoder threads configured for OpenCV.

    This keeps the same sampled frames and scoring formulas, but lets FFmpeg use
    more CPU cores while OpenCV decodes/seeks source frames.
    """
    threads = _opencv_decode_threads()
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"threads;{threads}")
    try:
        cv2.setNumThreads(threads)
    except Exception:
        pass
    return cv2.VideoCapture(video_file)


def analyze_video_sources(
    video_files: Sequence[str],
    audio_profile: Dict | None = None,
    use_gpu: bool = False,
    enable_ai: bool = True,
    qwen_model_path: str | None = None,
    event_callback=None,
) -> Dict:
    """Analyze all source videos and return candidate moments for Auto Mode."""
    total_started = time.perf_counter()
    requested_qwen_model_path = qwen_model_path or DEFAULT_QWEN_MODEL_DIR
    qwen_model_path = _qwen_backend_model_path(requested_qwen_model_path)
    existing = [os.path.abspath(p) for p in video_files if p and os.path.exists(p)]
    if not existing:
        fork_progress.emit(event_callback, fork_progress.warning(
            5, "No source videos available for analysis."))
        fork_progress.emit(event_callback, fork_progress.end(5, "No source videos"))
        return {
            "analysis_version": ANALYSIS_VERSION,
            "videos": [],
            "candidates": [],
            "ai_enabled": False,
            "summary": "No source videos available for analysis.",
        }

    ai_available = bool(enable_ai and _qwen_backend_available(requested_qwen_model_path))
    if ai_available:
        qwen_model_path = _qwen_backend_model_path(requested_qwen_model_path)
    print("\n   VIDEO ANALYSIS - Auto Mode visual library")
    print(f"   Source videos: {len(existing)}")
    print(f"   Qwen semantic tags: {'enabled' if ai_available else 'disabled/fallback'}")

    # [FORK] Digital-Union: structured progress. `total` is the real source count, and cache hits
    # count as already-completed deterministic analysis, so the counter reflects work actually done
    # rather than restarting from zero on a cached run.
    # [FORK] Digital-Union (R1): this fires BEFORE the cache scan, so it cannot yet know how many
    # sources need analysing - on a fully warm library the answer is none. "Checking" is what the
    # system actually knows here; the post-scan metric below stays the authority on real work.
    fork_progress.emit(event_callback, fork_progress.start(
        5, f"Checking {len(existing)} source video(s)",
        current=0, total=len(existing), unit="sources",
    ))
    source_counter = fork_progress.StageCounter(5, len(existing), min_interval=0.5)

    results_by_index: Dict[int, Dict] = {}
    cache_paths: Dict[int, str] = {}
    jobs: List[Dict] = []
    cache_hits = 0

    # [FORK] Digital-Union (D2): compute the backend and Qwen-config identity ONCE for this whole
    # invocation, then thread them into every source signature. `_qwen_backend_signature_token` reads
    # and fingerprints the model/mmproj/exes; per source that is 702 calls, measured at 61.7 minutes
    # if the GGUFs are full-hashed. Invocation-scoped rather than module-cached, so a later call in the
    # same process still sees a swapped model or llama build.
    invocation_backend_token = _qwen_backend_signature_token(qwen_model_path) if ai_available else None
    invocation_config_token = _qwen_config_token(audio_profile) if ai_available else None
    # [FORK] Digital-Union (D2 R2): an explicit state, because `None` alone is ambiguous. Down in
    # `_video_signature` a `None` backend_token means "not supplied, compute it now", so passing the
    # failed `None` straight through made every source retry the fingerprinting - measured at 1 + N
    # calls - and let a transient later success re-enable caching *inside* a run whose identity had
    # already failed. Once the invocation-level computation fails, AI caching is off for the whole
    # invocation and `_cache_path` is not called at all.
    ai_cache_disabled = ai_available and invocation_backend_token is None
    if ai_cache_disabled:
        print("   Warning: Qwen backend identity could not be verified; "
              "this run will not read or write AI analysis cache.")
        fork_progress.emit(event_callback, fork_progress.warning(
            5, "Qwen backend identity unverifiable; AI analysis cache disabled for this run."))

    # [FORK] Digital-Union (R1): execution truth for THIS invocation, kept strictly separate from
    # the library aggregates computed later over `videos` (which include every cache hit).
    run_stats = _new_run_stats()

    for idx, video_file in enumerate(existing, 1):
        cache_file = None if ai_cache_disabled else _cache_path(
            video_file, ai_available, qwen_model_path,
            backend_token=invocation_backend_token,
            config_token=invocation_config_token,
            audio_profile=audio_profile)
        cache_paths[idx] = cache_file
        cached = (_load_cache(cache_file, require_ai=ai_available, expected_video_file=video_file)
                  if cache_file else None)
        if cached:
            cache_hits += 1
            print(f"   Reusing cached visual analysis {idx}/{len(existing)}: {_safe_name(video_file)}")
            results_by_index[idx] = cached
            # Counted as a completed source (it is), but excluded from the throughput measurement:
            # cache hits arrive instantly and would otherwise inflate the reported sources/s.
            event = source_counter.advance(
                1, "cached", counts_toward_rate=False,
                cache_hits=cache_hits, unit="sources")
            fork_progress.emit(event_callback, event)
        else:
            jobs.append({"index": idx, "video_file": video_file, "cache_file": cache_file})

    # [FORK] Digital-Union (R1): `_video_analysis_workers` keeps its one-job contract untouched
    # (`video_count <= 1 -> 1`), which the genuine single-source case relies on. What was untrue was
    # reporting one analysis worker when there are no jobs at all - the analysis block below is
    # guarded by `if jobs:`, so nothing is ever submitted. Report the workers actually used.
    workers = _video_analysis_workers(len(jobs)) if jobs else 0
    fork_progress.emit(event_callback, fork_progress.metric(
        5,
        f"{cache_hits} cached, {len(jobs)} to analyze, {workers} worker(s)",
        cache_hits=cache_hits, to_analyze=len(jobs), workers=int(workers),
    ))
    # Always publish the post-cache count, even when nothing needs analyzing.
    fork_progress.emit(event_callback, source_counter.snapshot(
        "cache scan complete", cache_hits=cache_hits, unit="sources"))
    # Re-base the rate clock: from here on, throughput describes the analysis pass only.
    source_counter.begin_rate_window()
    if jobs:
        if workers > 1:
            print(
                f"   CPU visual analysis workers: {workers} "
                f"(parallel videos; Qwen stays sequential to protect VRAM)"
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _analyze_single_video,
                        job["video_file"],
                        use_gpu,
                        ai_available,
                        qwen_model_path,
                        audio_profile or {},
                        True,
                        job["index"],
                        len(existing),
                        event_callback,
                    ): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        results_by_index[job["index"]] = future.result()
                        # [FORK] Digital-Union (D1): checkpoint as soon as this source is complete.
                        # In parallel mode the record is still ai_deferred, so the central rule
                        # declines it here and the Qwen stage checkpoints it instead.
                        _checkpoint_cache(job["cache_file"], results_by_index[job["index"]],
                                          require_ai=ai_available)
                    except Exception as exc:
                        print(
                            f"   ⚠️  Parallel analysis failed for "
                            f"{_safe_name(job['video_file'])}: {exc}"
                        )
                        print("   ↪ Retrying that video safely in serial mode...")
                        fork_progress.emit(event_callback, fork_progress.warning(
                            5,
                            f"{_safe_name(job['video_file'])} failed in parallel mode; retrying "
                            f"serially ({exc})",
                        ))
                        results_by_index[job["index"]] = _analyze_single_video(
                            job["video_file"],
                            use_gpu,
                            ai_available,
                            qwen_model_path,
                            audio_profile or {},
                            True,
                            job["index"],
                            len(existing),
                            event_callback,
                        )
                        _checkpoint_cache(job["cache_file"], results_by_index[job["index"]],
                                          require_ai=ai_available)
                    # One advance per video, after either the parallel result or the serial retry,
                    # so a retried video is never double-counted.
                    fork_progress.emit(event_callback, source_counter.advance(
                        1, "analyzed", cache_hits=cache_hits, unit="sources",
                        rate_unit="analyzed sources"))
        else:
            print("   CPU visual analysis workers: 1 (serial)")
            for job in jobs:
                # [FORK] Digital-Union (R1): only this call passes `defer_ai=False`, so it is the
                # one that runs Qwen inline and must feed the current-run accounting. The parallel
                # submissions above defer Qwen and never record, which also keeps `run_stats`
                # mutated solely from this single thread.
                results_by_index[job["index"]] = _analyze_single_video(
                    job["video_file"],
                    use_gpu,
                    ai_available,
                    qwen_model_path,
                    audio_profile or {},
                    False,
                    job["index"],
                    len(existing),
                    event_callback,
                    run_stats,
                )
                # [FORK] Digital-Union (D1): serial mode runs Qwen inline (defer_ai=False), so this
                # source is genuinely finished here - make it durable before starting the next one.
                _checkpoint_cache(job["cache_file"], results_by_index[job["index"]],
                                  require_ai=ai_available)
                fork_progress.emit(event_callback, source_counter.advance(
                    1, "analyzed", cache_hits=cache_hits, unit="sources",
                    rate_unit="analyzed sources"))

    # When the deterministic CPU-heavy pass ran in parallel, run Qwen after it in
    # original video order. A single multi-video Qwen worker is used by default so
    # the llama.cpp model loads once, not once per source video.
    deferred_jobs = [
        job for job in jobs
        if results_by_index.get(job["index"], {}).get("ai_deferred") and ai_available
    ]
    batch_qwen = os.environ.get("BEATSYNC_QWEN_BATCH_VIDEOS", "1") != "0"
    if deferred_jobs:
        # [FORK] Digital-Union (Phase 2B): the worker's stdout is now streamed, so the per-job
        # "N / T candidates" states that follow are the worker's own live numbers. This opening state
        # deliberately claims no global candidate denominator: only the worker knows how many frames
        # prefetch actually decoded for the job it is currently on.
        fork_progress.emit(event_callback, fork_progress.state(
            5,
            f"Qwen semantic tagging started ({len(deferred_jobs)} video(s))",
            phase="qwen",
            qwen_videos=len(deferred_jobs),
            qwen_live_progress_available=True,
        ))
    qwen_started = time.perf_counter()
    if len(deferred_jobs) > 1 and batch_qwen:
        _complete_deferred_qwen_batch(
            video_items=[(job, results_by_index[job["index"]]) for job in deferred_jobs],
            use_gpu=use_gpu,
            qwen_model_path=qwen_model_path,
            audio_profile=audio_profile or {},
            total_video_count=len(existing),
            event_callback=event_callback,
            run_stats=run_stats,
        )
    else:
        for job in deferred_jobs:
            idx = job["index"]
            video_data = results_by_index.get(idx)
            if not video_data:
                continue
            _complete_deferred_qwen(
                video_data=video_data,
                use_gpu=use_gpu,
                qwen_model_path=qwen_model_path,
                audio_profile=audio_profile or {},
                label=f"{idx}/{len(existing)}",
                event_callback=event_callback,
                cache_file=job["cache_file"],
                run_stats=run_stats,
            )

    if deferred_jobs:
        tagged = sum(int((results_by_index.get(j["index"], {}).get("timings") or {}).get("qwen_tag_count", 0))
                     for j in deferred_jobs)
        frames = sum(int((results_by_index.get(j["index"], {}).get("timings") or {}).get("qwen_frame_count", 0))
                     for j in deferred_jobs)
        qwen_elapsed = time.perf_counter() - qwen_started
        if tagged:
            fork_progress.emit(event_callback, fork_progress.metric(
                5, f"Qwen tags {tagged}/{frames} in {_fmt_seconds(qwen_elapsed)}",
                qwen_tag_count=tagged, qwen_frame_count=frames,
                qwen_seconds=float(qwen_elapsed),
            ))
        else:
            fork_progress.emit(event_callback, fork_progress.warning(
                5, "Qwen produced no semantic tags; deterministic visual tags retained.",
                qwen_seconds=float(qwen_elapsed),
            ))
        fork_progress.emit(event_callback, fork_progress.state(
            5, "Qwen semantic tagging finished", phase="qwen",
            qwen_seconds=float(qwen_elapsed)))

    # [FORK] Digital-Union (D1): defensive backstop only - every completed source has already been
    # checkpointed above. It goes through the same completion rule, so it can never promote an
    # incomplete or failed-AI record into an accepted cache entry (the pre-D1 loop saved
    # unconditionally, which is how a failed Qwen run became a permanent AI-complete hit).
    for job in jobs:
        video_data = results_by_index.get(job["index"])
        if video_data:
            _checkpoint_cache(job["cache_file"], video_data, require_ai=ai_available)

    videos: List[Dict] = [results_by_index[i] for i in range(1, len(existing) + 1) if i in results_by_index]
    all_candidates: List[Dict] = []
    for video_data in videos:
        all_candidates.extend(video_data.get("candidates", []))

    if not all_candidates:
        summary = "No usable candidate moments were found; renderer will use fallback sampling."
    else:
        action_avg = float(np.mean([c.get("action_score", 0.0) for c in all_candidates]))
        beauty_avg = float(np.mean([c.get("beauty_score", 0.0) for c in all_candidates]))
        quality_avg = float(np.mean([c.get("quality_score", 0.0) for c in all_candidates]))
        summary = (
            f"{len(all_candidates)} visual moments, "
            f"action={action_avg:.2f}, beauty={beauty_avg:.2f}, quality={quality_avg:.2f}"
        )

    total_elapsed = time.perf_counter() - total_started
    qwen_tag_count = sum(int((v.get("timings") or {}).get("qwen_tag_count", 0)) for v in videos)
    qwen_frame_count = sum(int((v.get("timings") or {}).get("qwen_frame_count", 0)) for v in videos)
    qwen_seconds = sum(float((v.get("timings") or {}).get("qwen_seconds", 0.0)) for v in videos)
    qwen_inference_seconds = sum(float((v.get("timings") or {}).get("qwen_inference_seconds", 0.0)) for v in videos)
    qwen_model_id = next(
        (
            str((v.get("timings") or {}).get("qwen_model_id"))
            for v in videos
            if (v.get("timings") or {}).get("qwen_model_id")
        ),
        "",
    )
    qwen_concurrency = next(
        (
            int((v.get("timings") or {}).get("qwen_concurrency"))
            for v in videos
            if (v.get("timings") or {}).get("qwen_concurrency")
        ),
        0,
    )
    qwen_peak_vram_gb = max(
        [float((v.get("timings") or {}).get("qwen_peak_vram_gb") or 0.0) for v in videos] or [0.0]
    )
    print(
        f"   Visual library ready: {summary} "
        f"[total {_fmt_seconds(total_elapsed)}, cache hits {cache_hits}/{len(existing)}]"
    )
    if not all_candidates:
        fork_progress.emit(event_callback, fork_progress.warning(
            5, "No usable candidate moments found; renderer will use fallback sampling."))
    fork_progress.emit(event_callback, fork_progress.end(
        5, summary,
        current=len(existing), total=len(existing),
        elapsed_seconds=float(total_elapsed),
        sources=len(existing), cache_hits=int(cache_hits), workers=int(workers),
        candidates=len(all_candidates), ai_enabled=bool(ai_available),
        # [FORK] Digital-Union (R1): the structured channel carries the two truths separately, so a
        # consumer never has to guess which kind of number it is holding.
        sources_analyzed_this_run=int(len(jobs)),
        qwen_jobs_this_run=int(run_stats["qwen_jobs"]),
        qwen_requested_count_this_run=int(run_stats["qwen_requested_count"]),
        qwen_tag_count_this_run=int(run_stats["qwen_tag_count"]),
        qwen_frame_count_this_run=int(run_stats["qwen_frame_count"]),
        qwen_tag_count=int(qwen_tag_count), qwen_frame_count=int(qwen_frame_count),
        unit="sources",
    ))
    return {
        "analysis_version": ANALYSIS_VERSION,
        "videos": videos,
        "candidates": all_candidates,
        "ai_enabled": ai_available,
        "qwen_model_path": qwen_model_path if ai_available else None,
        "summary": summary,
        "analysis_seconds": total_elapsed,
        "cache_hits": cache_hits,
        "source_count": len(existing),
        "worker_count": workers,
        # [FORK] Digital-Union (R1): CURRENT-RUN execution facts. Computed from the uncached `jobs`
        # set and the Qwen requests those jobs actually issued - never from `videos`, which contains
        # every cache hit. On a fully warm run they are all zero while the library aggregates below
        # stay truthfully historical. Ephemeral top-level metadata; never enters a cache payload.
        "sources_analyzed_this_run": len(jobs),
        "analysis_workers_used": workers,
        "qwen_jobs_this_run": int(run_stats["qwen_jobs"]),
        "qwen_completed_jobs_this_run": int(run_stats["qwen_completed_jobs"]),
        "qwen_incomplete_jobs_this_run": int(run_stats["qwen_incomplete_jobs"]),
        "qwen_requested_count_this_run": int(run_stats["qwen_requested_count"]),
        "qwen_frame_count_this_run": int(run_stats["qwen_frame_count"]),
        "qwen_tag_count_this_run": int(run_stats["qwen_tag_count"]),
        "qwen_seconds_this_run": float(run_stats["qwen_seconds"]),
        "qwen_inference_seconds_this_run": float(run_stats["qwen_inference_seconds"]),
        # [FORK] Digital-Union: LIBRARY AGGREGATES over the returned records, cache hits included.
        # Historical by nature; preserved unchanged for compatibility. The UI must not present these
        # as work performed by the current run - that was the production reporting defect.
        "qwen_tag_count": qwen_tag_count,
        "qwen_frame_count": qwen_frame_count,
        "qwen_seconds": qwen_seconds,
        "qwen_inference_seconds": qwen_inference_seconds,
        "qwen_model_id": qwen_model_id,
        "qwen_concurrency": qwen_concurrency,
        "qwen_peak_vram_gb": qwen_peak_vram_gb,
    }


def _analyze_single_video(
    video_file: str,
    use_gpu: bool,
    enable_ai: bool,
    qwen_model_path: str,
    audio_profile: Dict,
    defer_ai: bool = False,
    index: int | None = None,
    total: int | None = None,
    event_callback=None,
    run_stats: Dict[str, Any] | None = None,
) -> Dict:
    started = time.perf_counter()
    timings: Dict[str, float] = {}
    name = _safe_name(video_file)
    prefix = f"{index}/{total} " if index and total else ""
    print(f"   Analyzing video {prefix}{name}")

    step_started = time.perf_counter()
    duration = max(0.0, float(get_video_duration(video_file)))
    fps = max(1.0, float(get_video_fps(video_file)))
    width, height = get_video_resolution(video_file)
    timings["metadata_seconds"] = time.perf_counter() - step_started
    print(
        f"      Metadata: {duration:.1f}s, {fps:.3g} fps, "
        f"{int(width)}x{int(height)} [{_fmt_seconds(timings['metadata_seconds'])}]"
    )

    scene_use_gpu = _use_gpu_scene_detection(use_gpu)
    if use_gpu and not scene_use_gpu:
        print("      Scene detection mode: CPU FFmpeg scene filter (CUDA path is optional; default off)")

    step_started = time.perf_counter()
    scene_changes = detect_video_scene_changes(
        video_file,
        threshold=0.27,
        use_gpu=scene_use_gpu,
        analysis_fps=6.0,
        analysis_width=384,
    )
    timings["scene_detection_seconds"] = time.perf_counter() - step_started
    print(
        f"      ⏱ Scene detection total: {_fmt_seconds(timings['scene_detection_seconds'])} "
        f"({len(scene_changes)} scene cuts)"
    )

    step_started = time.perf_counter()
    boundaries = _build_boundaries(scene_changes, duration)
    windows = _make_candidate_windows(boundaries, duration)
    timings["window_build_seconds"] = time.perf_counter() - step_started
    print(
        f"      Candidate windows: {len(windows)} from {len(boundaries)} boundaries "
        f"[{_fmt_seconds(timings['window_build_seconds'])}]"
    )

    cap = _open_video_capture(video_file)
    candidates: List[Dict] = []
    if cap.isOpened():
        gpu_candidate_metrics = _use_gpu_candidate_metrics(use_gpu)
        if gpu_candidate_metrics:
            print("      Candidate scoring: GPU CuPy metrics + CPU frame decode")
        else:
            print("      Candidate scoring: CPU metrics")
        step_started = time.perf_counter()
        window_metrics = _measure_windows(cap, fps, windows, use_gpu=gpu_candidate_metrics)
        timings["candidate_scoring_seconds"] = time.perf_counter() - step_started
        valid_metrics = 0
        for i, (window, metrics) in enumerate(zip(windows, window_metrics)):
            if not metrics:
                continue
            valid_metrics += 1
            candidate = _build_candidate(video_file, name, duration, i, window, metrics)
            candidates.append(candidate)
        cap.release()
        print(
            f"      ⏱ Candidate scoring total: {_fmt_seconds(timings['candidate_scoring_seconds'])} "
            f"({valid_metrics}/{len(windows)} windows usable)"
        )
    else:
        print(f"      Warning: OpenCV could not open {name}; candidate analysis skipped.")

    qwen_seconds = 0.0
    # [FORK] Digital-Union (D1 R2): the inline (serial, defer_ai=False) path must consume the same
    # explicit completion signal the deferred path uses. Before R2 it ignored the signal entirely and
    # reported `ai_enabled = enable_ai and not defer_ai`, so a failed, timed-out or
    # deliberately-skipped Qwen run was still cached as AI-complete and never retried.
    inline_qwen_completed = False
    if enable_ai and candidates and not defer_ai:
        try:
            step_started = time.perf_counter()
            qwen_info = _annotate_candidates_with_qwen(
                video_file=video_file,
                fps=fps,
                candidates=candidates,
                qwen_model_path=qwen_model_path,
                use_gpu=use_gpu,
                audio_profile=audio_profile,
                event_callback=event_callback,
                run_stats=run_stats,
            )
            qwen_seconds = time.perf_counter() - step_started
            # Pop before the update: the private flag must never reach timings or the cache payload.
            inline_qwen_completed = bool((qwen_info or {}).pop(_QWEN_COMPLETED_KEY, False))
            timings.update(qwen_info or {})
            timings["qwen_seconds"] = qwen_seconds
            print(f"      ⏱ Qwen semantic analysis total: {_fmt_seconds(qwen_seconds)}")
            if not inline_qwen_completed:
                print(f"      Qwen did not complete for {name}; deterministic visual tags retained "
                      f"and AI analysis will retry on the next run.")
        except Exception as e:
            print(f"      Warning: Qwen semantic analysis failed for {name}: {e}")
            inline_qwen_completed = False
    elif enable_ai and candidates and defer_ai:
        timings["qwen_seconds"] = 0.0
        print("      Qwen semantic analysis: deferred until parallel CPU pass completes")

    sort_started = time.perf_counter()
    if not defer_ai:
        candidates = sorted(candidates, key=lambda c: c.get("editorial_score", 0.0), reverse=True)
    timings["sort_seconds"] = time.perf_counter() - sort_started

    elapsed = time.perf_counter() - started
    timings["total_seconds"] = elapsed
    print(
        f"   Found {len(candidates)} usable visual moments in {_fmt_seconds(elapsed)} "
        f"(scene {_fmt_seconds(timings.get('scene_detection_seconds', 0))}, "
        f"scoring {_fmt_seconds(timings.get('candidate_scoring_seconds', 0))}, "
        f"qwen {_fmt_seconds(timings.get('qwen_seconds', 0))})"
    )

    return {
        "analysis_version": ANALYSIS_VERSION,
        # [FORK] Digital-Union (D2): stamped here, at the one place source records are built, so every
        # record - AI, deterministic/no_ai and candidate-less alike - carries the contract before the
        # completion rule ever inspects it. `_save_cache` stays a pure transport primitive and never
        # injects semantic truth into a payload.
        "cache_contract": CACHE_CONTRACT_VERSION,
        "video_file": os.path.abspath(video_file),
        "source_name": name,
        "duration": duration,
        "fps": fps,
        "width": int(width),
        "height": int(height),
        "scene_changes": scene_changes,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "analysis_seconds": elapsed,
        "timings": timings,
        # [FORK] Digital-Union (D1 R2): a fact about this run, not a restatement of the request.
        # Inline AI claims completion only when the Qwen facade reported it.
        "ai_enabled": bool(enable_ai and not defer_ai and inline_qwen_completed),
        "ai_deferred": bool(enable_ai and defer_ai),
    }


def _complete_deferred_qwen(
    video_data: Dict,
    use_gpu: bool,
    qwen_model_path: str,
    audio_profile: Dict,
    label: str = "",
    event_callback=None,
    cache_file: str | None = None,
    run_stats: Dict[str, Any] | None = None,
) -> Dict:
    candidates = video_data.get("candidates") or []
    if not candidates:
        video_data["ai_deferred"] = False
        video_data["ai_enabled"] = False
        # [FORK] Digital-Union (D1): nothing for Qwen to annotate, so this source IS finished.
        # `ai_enabled` stays honestly False; `_cache_entry_is_complete` is what makes it reusable,
        # and checkpointing it here is what stops it being re-analysed on every future run.
        _checkpoint_cache(cache_file, video_data, require_ai=True)
        return

    name = _safe_name(video_data.get("video_file", video_data.get("source_name", "video")))
    print(f"   Running deferred Qwen semantic analysis {label}: {name}")
    started = time.perf_counter()
    qwen_info = {}
    qwen_completed = False
    try:
        qwen_info = _annotate_candidates_with_qwen(
            video_file=video_data["video_file"],
            fps=float(video_data.get("fps") or 24.0),
            candidates=candidates,
            qwen_model_path=qwen_model_path,
            use_gpu=use_gpu,
            audio_profile=audio_profile,
            event_callback=event_callback,
            run_stats=run_stats,
        )
        # [FORK] Digital-Union (D1): completion comes from the facade's explicit signal, never from
        # "no exception was raised". The facade applies `_qwen_job_completed`, so a worker process
        # that merely *finished* is not the same as one whose submitted candidates were all tagged.
        qwen_completed = bool((qwen_info or {}).pop(_QWEN_COMPLETED_KEY, False))
    except Exception as e:
        print(f"      Warning: Qwen semantic analysis failed for {name}: {e}")
        qwen_completed = False

    qwen_seconds = time.perf_counter() - started
    candidates.sort(key=lambda c: c.get("editorial_score", 0.0), reverse=True)
    timings = video_data.setdefault("timings", {})
    timings.update(qwen_info or {})
    timings["qwen_seconds"] = qwen_seconds
    timings["total_seconds"] = float(timings.get("total_seconds", video_data.get("analysis_seconds", 0.0))) + qwen_seconds
    video_data["analysis_seconds"] = timings["total_seconds"]
    video_data["ai_deferred"] = False
    # [FORK] Digital-Union (D1): only genuine completion may claim AI-complete. On failure the
    # deterministic candidates and visual tags are kept exactly as they are, but the record is not
    # reusable under require_ai, so the next run retries Qwen instead of inheriting a silent gap.
    video_data["ai_enabled"] = qwen_completed
    video_data["candidate_count"] = len(candidates)
    if not qwen_completed:
        print(f"      Qwen did not complete for {name}; deterministic visual tags retained and "
              f"AI analysis will retry on the next run.")
    print(
        f"      ⏱ Deferred Qwen total: {_fmt_seconds(qwen_seconds)}; "
        f"video total now {_fmt_seconds(video_data['analysis_seconds'])}"
    )
    # Checkpoint immediately: this source is finished, and waiting for the rest of Stage 5 is
    # exactly what used to lose it.
    _checkpoint_cache(cache_file, video_data, require_ai=True)




def _qwen_max_windows() -> int:
    try:
        value = int(os.environ.get("BEATSYNC_QWEN_MAX_WINDOWS", "120"))
    except ValueError:
        value = 120
    return max(0, value)


def _complete_deferred_qwen_batch(
    video_items: Sequence[tuple[Dict, Dict]],
    use_gpu: bool,
    qwen_model_path: str,
    audio_profile: Dict,
    total_video_count: int,
    event_callback=None,
    run_stats: Dict[str, Any] | None = None,
) -> None:
    # [FORK] Digital-Union (R1): `request_jobs` below is the authoritative set of sources that reach
    # the shared worker. Candidate-less sources `continue` before it and a configured skip returns
    # earlier still, so counting from the per-job merge loop - not from `video_items` or
    # `deferred_jobs` - is what makes "Qwen jobs this run" mean a request was genuinely issued.
    max_windows = _qwen_max_windows()
    if max_windows == 0:
        for _, video_data in video_items:
            video_data["ai_deferred"] = False
            video_data["ai_enabled"] = False
        print("   Qwen semantic analysis skipped (BEATSYNC_QWEN_MAX_WINDOWS=0).")
        return {"qwen_frame_count": 0, "qwen_tag_count": 0}

    # [FORK] Digital-Union (D1): remember each job's cache file so a completed job can be
    # checkpointed the moment its own merge finishes, instead of waiting for its siblings.
    job_to_cache: Dict[str, str] = {}
    request_jobs: List[Dict] = []
    job_to_video: Dict[str, Dict] = {}
    selected_by_job: Dict[str, List[Dict]] = {}
    for job, video_data in video_items:
        candidates = video_data.get("candidates") or []
        ai_candidates = _select_ai_candidates(candidates, max_windows)
        idx = int(job.get("index") or 0)
        job_id = str(idx or len(request_jobs) + 1)
        name = _safe_name(video_data.get("video_file", video_data.get("source_name", "video")))
        print(
            f"   Qwen semantic analysis batch input {idx}/{total_video_count}: "
            f"{name} - {len(ai_candidates)} candidate moments"
        )
        if not ai_candidates:
            video_data["ai_deferred"] = False
            video_data["ai_enabled"] = False
            # [FORK] Digital-Union (D1): nothing for Qwen to annotate -> finished, and durable now.
            _checkpoint_cache(job.get("cache_file"), video_data, require_ai=True)
            continue
        selected_by_job[job_id] = ai_candidates
        job_to_video[job_id] = video_data
        job_to_cache[job_id] = job.get("cache_file") or ""
        request_jobs.append({
            "job_id": job_id,
            "video_file": video_data["video_file"],
            "fps": float(video_data.get("fps") or 24.0),
            "candidates": [
                {
                    "id": c.get("id"),
                    "start": c.get("start"),
                    "end": c.get("end"),
                }
                for c in ai_candidates
            ],
        })

    if not request_jobs:
        return

    print(f"   Running one shared Qwen worker for {len(request_jobs)} video(s) (model loads once)")
    batch_started = time.perf_counter()
    response = _run_qwen_worker_batch(
        jobs=request_jobs,
        qwen_model_path=qwen_model_path,
        use_gpu=use_gpu,
        audio_profile=audio_profile,
        event_callback=event_callback,
    )
    batch_seconds = time.perf_counter() - batch_started
    # [FORK] Digital-Union (R2): SUBMISSION truth, recorded the moment the shared worker returns and
    # therefore BEFORE the empty-response branch below. A worker that timed out, exited non-zero or
    # produced an unreadable response still consumed a real attempt on real sources; R1 returned
    # early and reported `0 jobs`, which the UI rendered as "no inference this run" - false.
    # Every submitted job starts incomplete and is promoted only by its own returned evidence, so an
    # empty response correctly leaves all of them incomplete with no further bookkeeping.
    # `batch_seconds` is the one measured wall time of the one shared worker invocation: added
    # exactly once here, never per source, and never from the amortized per-source figures below.
    if run_stats is not None:
        run_stats["qwen_jobs"] += len(request_jobs)
        run_stats["qwen_incomplete_jobs"] += len(request_jobs)
        run_stats["qwen_requested_count"] += sum(
            len(selected_by_job.get(str(job.get("job_id")), ())) for job in request_jobs)
        run_stats["qwen_seconds"] += float(batch_seconds)
    semantics_by_job = response.get("semantics_by_job") or {}
    timings_by_job = response.get("timings_by_job") or {}
    if not semantics_by_job:
        for video_data in job_to_video.values():
            video_data["ai_deferred"] = False
            video_data["ai_enabled"] = False
        print("      Qwen llama.cpp returned no semantic response; AI analysis will retry on the next run.")
        print(f"      ⏱ Shared Qwen batch total: {_fmt_seconds(batch_seconds)}")
        return
    model_load_seconds = float(response.get("model_load_seconds") or 0.0)
    amortized_model = model_load_seconds / max(1, len(request_jobs))
    qwen_model_id = str(response.get("model_id") or "")
    qwen_concurrency = int(response.get("batch_size") or 0)
    qwen_peak_vram_gb = float(response.get("peak_vram_gb") or 0.0)

    for job_id, video_data in job_to_video.items():
        ai_candidates = selected_by_job.get(job_id, [])
        # [FORK] Digital-Union (D1 R4): per-job evidence in two parts. Membership in
        # `semantics_by_job` locates this job's result - a globally non-empty response is no proof
        # that *this* job ran, and before D1 an absent job became ai_enabled=True with 0 tags and was
        # reused as AI-complete forever. But membership alone is still not completion: the worker
        # records a job's timings whenever its loop returns, even when every candidate's semantic
        # failed. `_qwen_job_completed` applies the shared rule. One failing job never fails its
        # siblings.
        envelope_present = str(job_id) in (semantics_by_job if isinstance(semantics_by_job, dict)
                                           else {})
        semantics = semantics_by_job.get(str(job_id), {})
        semantic_by_id = {str(k): v for k, v in semantics.items()} if isinstance(semantics, dict) else {}
        merged_count = 0
        for candidate in ai_candidates:
            semantic = semantic_by_id.get(str(candidate.get("id")))
            if semantic:
                _merge_semantic(candidate, semantic)
                merged_count += 1
        candidates = video_data.get("candidates") or []
        candidates.sort(key=lambda c: c.get("editorial_score", 0.0), reverse=True)
        timing = timings_by_job.get(str(job_id), {}) if isinstance(timings_by_job, dict) else {}
        if not isinstance(timing, dict):
            timing = {}
        # [FORK] Digital-Union (D1 R5): same rule, using this job's own submitted candidate set.
        job_completed = _qwen_job_completed(
            timing, envelope_present,
            {str(candidate.get("id")) for candidate in ai_candidates}, set(semantic_by_id))
        qwen_seconds = (
            float(timing.get("prefetch_seconds") or 0.0)
            + float(timing.get("inference_seconds") or 0.0)
            + amortized_model
        )
        timings = video_data.setdefault("timings", {})
        timings["qwen_seconds"] = qwen_seconds
        timings["qwen_model_load_seconds_amortized"] = amortized_model
        timings["qwen_prefetch_seconds"] = float(timing.get("prefetch_seconds") or 0.0)
        timings["qwen_inference_seconds"] = float(timing.get("inference_seconds") or 0.0)
        timings["qwen_frame_count"] = _reported_count(timing, "frame_count", len(ai_candidates))
        timings["qwen_tag_count"] = _reported_count(timing, "tag_count", merged_count)
        timings["qwen_model_id"] = qwen_model_id
        timings["qwen_concurrency"] = qwen_concurrency
        timings["qwen_peak_vram_gb"] = qwen_peak_vram_gb
        timings["total_seconds"] = float(timings.get("total_seconds", video_data.get("analysis_seconds", 0.0))) + qwen_seconds
        video_data["analysis_seconds"] = timings["total_seconds"]
        video_data["ai_deferred"] = False
        video_data["ai_enabled"] = job_completed
        video_data["candidate_count"] = len(candidates)
        if not job_completed:
            print(
                f"      Qwen semantic pass incomplete for "
                f"{_safe_name(video_data.get('video_file', 'video'))} "
                f"({_reported_count(timing, 'tag_count', merged_count)} of "
                f"{_reported_count(timing, 'frame_count', 0)} decoded frames tagged for "
                f"{len(ai_candidates)} requested candidate(s)); deterministic visual tags retained "
                f"and AI analysis will retry on the next run."
            )
        print(
            f"      Qwen semantic tags merged: {merged_count}/{len(ai_candidates)} "
            f"for {_safe_name(video_data.get('video_file', 'video'))}"
        )
        print(
            f"      ⏱ Shared-Qwen video cost: {_fmt_seconds(qwen_seconds)} "
            f"(prefetch {_fmt_seconds(timings['qwen_prefetch_seconds'])}, "
            f"inference {_fmt_seconds(timings['qwen_inference_seconds'])}, "
            f"model share {_fmt_seconds(amortized_model)})"
        )
        # [FORK] Digital-Union (D1, wording corrected in R5): checkpoint this job now, so a later
        # job's failure - or a parent interruption during this post-response merge loop - cannot cost
        # a job that is already finished. Work still inside an in-flight worker is NOT covered: the
        # batch response only exists once the worker's whole job loop has returned.
        # [FORK] Digital-Union (R2): RESPONSE truth only. The job was already counted at submission,
        # so nothing here touches `qwen_jobs` - doing so would double-count every successful source.
        # Completion promotes one job out of the incomplete tally; decoded/tagged/inference numbers
        # are added only where the worker actually proved them.
        if run_stats is not None:
            if job_completed:
                run_stats["qwen_completed_jobs"] += 1
                run_stats["qwen_incomplete_jobs"] -= 1
            # Decoded frames count only when the worker reported a real integer. The persisted
            # `timings["qwen_frame_count"]` falls back to the requested count for source-record
            # compatibility; that fallback is not evidence of decoding and must not leak into
            # current-run truth.
            reported_frames = timing.get("frame_count") if isinstance(timing, dict) else None
            if _is_count(reported_frames):
                run_stats["qwen_frame_count"] += reported_frames
            # Tags are the semantics actually merged into candidates - directly observed.
            run_stats["qwen_tag_count"] += merged_count
            reported_inference = timing.get("inference_seconds") if isinstance(timing, dict) else None
            if isinstance(reported_inference, (int, float)) and not isinstance(reported_inference, bool):
                run_stats["qwen_inference_seconds"] += float(reported_inference)
        _checkpoint_cache(job_to_cache.get(job_id), video_data, require_ai=True)

    print(f"      ⏱ Shared Qwen batch total: {_fmt_seconds(batch_seconds)}")


def _qwen_worker_stdout_printer(indent: str = "      "):
    """Print a human worker line as it arrives, with the indent the old post-mortem dump used."""
    def printer(line: str) -> None:
        print(f"{indent}{line}")
    return printer


def _short_qwen_error(text: str, limit: int = 200) -> str:
    """One short line for the status panel. The full bounded tail still goes to the console."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[-limit:]


def _run_qwen_worker_batch(
    jobs: Sequence[Dict],
    qwen_model_path: str,
    use_gpu: bool,
    audio_profile: Dict,
    event_callback=None,
) -> Dict:
    os.makedirs(VIDEO_ANALYSIS_CACHE_DIR, exist_ok=True)
    token = _hash_text(f"batch|{time.time()}|{len(jobs)}", 12)
    request_path = os.path.join(VIDEO_ANALYSIS_CACHE_DIR, f"qwen_batch_request_{token}.json")
    response_path = os.path.join(VIDEO_ANALYSIS_CACHE_DIR, f"qwen_batch_response_{token}.json")
    worker_path = os.path.join(ROOT_DIR, "src", "auto_mode", "stage5_qwen_scene_worker.py")
    request = {
        "jobs": list(jobs),
        "qwen_model_path": qwen_model_path,
        "use_gpu": bool(use_gpu),
        "audio_profile": audio_profile,
    }
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(request, f)

    env = _qwen_worker_environment()
    total_candidates = sum(len(job.get("candidates") or []) for job in jobs)
    timeout = max(1800, int(total_candidates * 75))
    try:
        # [FORK] Digital-Union (Phase 2B): streamed instead of captured. Identical argv, env, UTF-8
        # decoding, timeout and return-code contract; the difference is that the worker's progress
        # lines now reach the UI while it runs instead of after it exits. The worker's own
        # llama-server Popen is unrelated and untouched.
        result = fork_qwen.run_qwen_worker(
            [sys.executable, worker_path, "--request", request_path, "--response", response_path],
            response_path,
            env=env,
            timeout=timeout,
            event_callback=event_callback,
            on_human_line=_qwen_worker_stdout_printer(),
            stderr_char_limit=2400,
        )
        if result.outcome.timed_out:
            raise subprocess.TimeoutExpired(worker_path, timeout)
        if result.outcome.launch_error:
            raise RuntimeError(result.outcome.launch_error)
        if result.outcome.returncode != 0:
            error_tail = result.outcome.stderr_tail
            print(f"      Qwen batch worker failed: {error_tail}")
            fork_progress.emit(event_callback, fork_progress.warning(
                5,
                f"Qwen batch worker failed (exit {result.outcome.returncode}): "
                f"{_short_qwen_error(error_tail)}",
                phase="qwen", qwen_returncode=result.outcome.returncode,
            ))
            return {}
        if result.response_error:
            raise RuntimeError(result.response_error)
        return result.response
    except subprocess.TimeoutExpired:
        print("      Qwen batch worker timed out; deterministic visual tags remain active.")
        fork_progress.emit(event_callback, fork_progress.warning(
            5, "Qwen batch worker timed out; deterministic visual tags remain active.",
            phase="qwen", qwen_timed_out=True,
        ))
        return {}
    except Exception as e:
        print(f"      Qwen batch worker error: {e}")
        fork_progress.emit(event_callback, fork_progress.warning(
            5, f"Qwen batch worker error: {_short_qwen_error(str(e))}", phase="qwen"))
        return {}
    finally:
        # Keep Qwen worker request/response files in video_analysis_cache for
        # reproducibility and debugging. The user explicitly wants this cache
        # folder to be preserved.
        pass

def _build_boundaries(scene_changes: Iterable[float], duration: float) -> List[float]:
    if duration <= 0:
        return [0.0]
    raw = [0.0] + [float(t) for t in scene_changes if 0.0 < float(t) < duration] + [duration]
    raw = sorted(set(round(t, 3) for t in raw))
    cleaned: List[float] = []
    for t in raw:
        if not cleaned or t - cleaned[-1] >= 0.35 or t in (0.0, duration):
            cleaned.append(t)
    if cleaned[-1] != duration:
        cleaned.append(duration)
    return cleaned


def _make_candidate_windows(boundaries: Sequence[float], duration: float) -> List[Dict]:
    windows: List[Dict] = []
    if duration <= 0:
        return windows

    max_window = 5.2
    min_window = 0.55
    fallback_step = 3.5

    if len(boundaries) < 2:
        starts = np.arange(0.0, max(duration - min_window, 0.0), fallback_step)
        return [
            {"start": float(s), "end": float(min(duration, s + max_window)), "kind": "fallback"}
            for s in starts
        ]

    for scene_idx in range(len(boundaries) - 1):
        start = float(boundaries[scene_idx])
        end = float(boundaries[scene_idx + 1])
        scene_duration = end - start
        if scene_duration < min_window:
            continue

        if scene_duration <= max_window:
            windows.append({"start": start, "end": end, "kind": "scene", "scene_index": scene_idx})
            continue

        chunks = max(1, int(math.ceil(scene_duration / max_window)))
        step = scene_duration / chunks
        for chunk_idx in range(chunks):
            chunk_start = start + chunk_idx * step
            chunk_end = min(end, chunk_start + max_window)
            if chunk_end - chunk_start >= min_window:
                windows.append({
                    "start": float(chunk_start),
                    "end": float(chunk_end),
                    "kind": "scene_chunk",
                    "scene_index": scene_idx,
                })

    return windows


def _measure_windows(cap: cv2.VideoCapture, fps: float, windows: Sequence[Dict],
                     use_gpu: bool = False) -> List[Dict]:
    plans = [_window_sample_plan(fps, float(w["start"]), float(w["end"])) for w in windows]
    targets = sorted({idx for plan in plans for idx in plan["frame_indices"]})
    if not targets:
        return [{} for _ in windows]

    frame_span = max(1, targets[-1] - targets[0] + 1)
    seek_ratio = frame_span / max(1, len(targets))
    sequential_limit = float(os.environ.get("BEATSYNC_SEQUENTIAL_SAMPLE_RATIO", "80"))
    use_sequential = (
        os.environ.get("BEATSYNC_SEQUENTIAL_WINDOW_SAMPLING", "1") != "0"
        and seek_ratio <= sequential_limit
    )

    def build_metric(plan: Dict, frame_map: Dict[int, np.ndarray]) -> Dict:
        frames = []
        sample_times = []
        for frame_idx, sample_time in zip(plan["frame_indices"], plan["sample_times"]):
            frame = frame_map.get(frame_idx)
            if frame is not None:
                frames.append(frame)
                sample_times.append(sample_time)
        return _measure_frame_samples(
            frames,
            np.asarray(sample_times, dtype=float),
            plan["start"],
            plan["duration"],
            use_gpu,
        )

    if use_sequential:
        print(
            f"         Ordered frame sampling: {len(targets)} target frames "
            f"(span/target={seek_ratio:.1f}, limit={sequential_limit:g})"
        )
        read_started = time.perf_counter()
        frame_map = _read_ordered_frames(cap, targets)
        read_elapsed = time.perf_counter() - read_started
        approx_ram_mb = sum(getattr(frame, "nbytes", 0) for frame in frame_map.values()) / (1024 * 1024)
        print(
            f"         Frame decode/cache: {len(frame_map)}/{len(targets)} frames "
            f"in RAM, ~{approx_ram_mb:.0f} MB [{_fmt_seconds(read_elapsed)}]"
        )

        metric_workers = _candidate_metric_workers(len(plans), use_gpu)
        metrics_started = time.perf_counter()
        if metric_workers > 1:
            print(f"         Metric CPU workers: {metric_workers}")
            with ThreadPoolExecutor(max_workers=metric_workers) as executor:
                metrics = list(executor.map(lambda plan: build_metric(plan, frame_map), plans))
        else:
            metrics = [build_metric(plan, frame_map) for plan in plans]
        metric_elapsed = time.perf_counter() - metrics_started
        usable = sum(1 for metric in metrics if metric)
        print(
            f"         Metric math: {usable}/{len(plans)} windows "
            f"[{_fmt_seconds(metric_elapsed)}, {usable / max(metric_elapsed, 1e-6):.1f} windows/s]"
        )
        return metrics

    print(
        f"         Random-seek sampling fallback: {len(targets)} target frames "
        f"(span/target={seek_ratio:.1f} > limit {sequential_limit:g})"
    )
    fallback_started = time.perf_counter()
    metrics = [
        _measure_window(cap, fps, float(w["start"]), float(w["end"]), use_gpu=use_gpu)
        for w in windows
    ]
    fallback_elapsed = time.perf_counter() - fallback_started
    usable = sum(1 for metric in metrics if metric)
    print(
        f"         Random-seek scoring: {usable}/{len(windows)} windows "
        f"[{_fmt_seconds(fallback_elapsed)}]"
    )
    return metrics


def _window_sample_plan(fps: float, start: float, end: float) -> Dict:
    duration = max(0.0, end - start)
    if duration <= 0.05:
        return {"start": start, "duration": duration, "sample_times": [], "frame_indices": []}

    sample_count = int(np.clip(math.ceil(duration * 1.4), 3, 8))
    sample_times = np.linspace(start + duration * 0.12, end - duration * 0.12, sample_count)
    frame_indices = [max(0, int(round(float(t) * fps))) for t in sample_times]
    return {
        "start": start,
        "duration": duration,
        "sample_times": sample_times,
        "frame_indices": frame_indices,
    }


def _read_ordered_frames(cap: cv2.VideoCapture, frame_indices: Sequence[int]) -> Dict[int, np.ndarray]:
    frames: Dict[int, np.ndarray] = {}
    if not frame_indices:
        return frames

    first = int(frame_indices[0])
    cap.set(cv2.CAP_PROP_POS_FRAMES, first)
    current = first

    for frame_idx in frame_indices:
        frame_idx = int(frame_idx)
        if frame_idx < current:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            current = frame_idx
        while current < frame_idx:
            if not cap.grab():
                break
            current += 1
        if current != frame_idx:
            continue
        ok, frame = cap.read()
        current += 1
        if not ok or frame is None:
            continue
        frames[frame_idx] = _resize_for_analysis(frame, max_width=360)
    return frames


def _measure_window(cap: cv2.VideoCapture, fps: float, start: float, end: float,
                    use_gpu: bool = False) -> Dict:
    plan = _window_sample_plan(fps, start, end)
    if plan["duration"] <= 0.05:
        return {}

    frames: List[np.ndarray] = []
    used_times: List[float] = []
    for frame_idx, sample_time in zip(plan["frame_indices"], plan["sample_times"]):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frame = _resize_for_analysis(frame, max_width=360)
        frames.append(frame)
        used_times.append(float(sample_time))

    return _measure_frame_samples(frames, np.asarray(used_times, dtype=float), start, plan["duration"], use_gpu)


def _measure_frame_samples(frames: Sequence[np.ndarray], sample_times: np.ndarray,
                           start: float, duration: float, use_gpu: bool = False) -> Dict:
    if not frames:
        return {}

    if use_gpu and GPU_AVAILABLE and cp is not None:
        try:
            return _measure_frames_gpu(frames, sample_times, start, duration)
        except Exception:
            pass

    gray_frames = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    hsv_frames = [cv2.cvtColor(f, cv2.COLOR_BGR2HSV) for f in frames]

    brightness = float(np.mean([np.mean(g) / 255.0 for g in gray_frames]))
    contrast = float(np.mean([np.std(g) / 80.0 for g in gray_frames]))
    saturation = float(np.mean([np.mean(h[:, :, 1]) / 255.0 for h in hsv_frames]))
    blur_values = [cv2.Laplacian(g, cv2.CV_64F).var() for g in gray_frames]
    sharpness = _clamp(np.mean(blur_values) / 520.0)

    motion_values = []
    peak_offset = duration * 0.5
    for i in range(1, len(gray_frames)):
        diff = cv2.absdiff(gray_frames[i], gray_frames[i - 1])
        motion = float(np.mean(diff) / 42.0)
        motion_values.append(motion)
    if motion_values:
        motion = _clamp(np.mean(motion_values))
        peak_i = int(np.argmax(motion_values)) + 1
        peak_offset = float(sample_times[min(peak_i, len(sample_times) - 1)] - start)
    else:
        motion = 0.0

    colorfulness = _colorfulness(frames)
    darkness_penalty = _clamp((0.25 - brightness) / 0.25)
    blown_penalty = _clamp((brightness - 0.86) / 0.14)
    quality = _clamp(
        0.36 * sharpness
        + 0.22 * _clamp(contrast)
        + 0.18 * _clamp(saturation)
        + 0.14 * (1.0 - darkness_penalty)
        + 0.10 * (1.0 - blown_penalty)
    )

    return {
        "duration": duration,
        "brightness": _clamp(brightness),
        "contrast": _clamp(contrast),
        "saturation": _clamp(saturation),
        "sharpness": _clamp(sharpness),
        "motion": _clamp(motion),
        "colorfulness": _clamp(colorfulness),
        "quality_score": quality,
        "peak_offset": _clamp(peak_offset, 0.0, duration, default=duration * 0.5),
    }


def _use_gpu_candidate_metrics(use_gpu: bool) -> bool:
    if not (use_gpu and GPU_AVAILABLE and cp is not None):
        return False
    return os.environ.get("BEATSYNC_GPU_CANDIDATE_METRICS", "0") == "1"


def _use_gpu_scene_detection(use_gpu: bool) -> bool:
    if not use_gpu:
        return False
    return os.environ.get("BEATSYNC_GPU_SCENE_DETECTION", "0") == "1"


def _measure_frames_gpu(frames: Sequence[np.ndarray], sample_times: np.ndarray,
                        start: float, duration: float) -> Dict:
    frame_stack = cp.asarray(np.stack(frames, axis=0), dtype=cp.float32)
    b = frame_stack[:, :, :, 0]
    g = frame_stack[:, :, :, 1]
    r = frame_stack[:, :, :, 2]

    gray = 0.114 * b + 0.587 * g + 0.299 * r
    brightness = float(cp.asnumpy(cp.mean(gray) / 255.0))
    contrast = float(cp.asnumpy(cp.mean(cp.std(gray, axis=(1, 2))) / 80.0))

    max_rgb = cp.max(frame_stack, axis=3)
    min_rgb = cp.min(frame_stack, axis=3)
    saturation_map = cp.where(max_rgb > 1e-6, (max_rgb - min_rgb) / max_rgb, 0.0)
    saturation = float(cp.asnumpy(cp.mean(saturation_map)))

    lap = _gpu_laplacian(gray)
    sharpness = _clamp(float(cp.asnumpy(cp.mean(cp.var(lap, axis=(1, 2)))) / 520.0))

    if gray.shape[0] > 1:
        diff = cp.abs(gray[1:] - gray[:-1])
        motion_values = cp.mean(diff, axis=(1, 2)) / 42.0
        motion = _clamp(float(cp.asnumpy(cp.mean(motion_values))))
        peak_i = int(cp.asnumpy(cp.argmax(motion_values))) + 1
        peak_offset = float(sample_times[min(peak_i, len(sample_times) - 1)] - start)
    else:
        motion = 0.0
        peak_offset = duration * 0.5

    rg = cp.abs(r - g)
    yb = cp.abs(0.5 * (r + g) - b)
    colorfulness = _clamp(float(cp.asnumpy(cp.mean(cp.std(rg, axis=(1, 2)) + cp.std(yb, axis=(1, 2)))) / 95.0))

    darkness_penalty = _clamp((0.25 - brightness) / 0.25)
    blown_penalty = _clamp((brightness - 0.86) / 0.14)
    quality = _clamp(
        0.36 * sharpness
        + 0.22 * _clamp(contrast)
        + 0.18 * _clamp(saturation)
        + 0.14 * (1.0 - darkness_penalty)
        + 0.10 * (1.0 - blown_penalty)
    )

    return {
        "duration": duration,
        "brightness": _clamp(brightness),
        "contrast": _clamp(contrast),
        "saturation": _clamp(saturation),
        "sharpness": _clamp(sharpness),
        "motion": _clamp(motion),
        "colorfulness": _clamp(colorfulness),
        "quality_score": quality,
        "peak_offset": _clamp(peak_offset, 0.0, duration, default=duration * 0.5),
    }


def _gpu_laplacian(gray_stack):
    padded = cp.pad(gray_stack, ((0, 0), (1, 1), (1, 1)), mode="reflect")
    center = padded[:, 1:-1, 1:-1]
    return (
        padded[:, :-2, 1:-1]
        + padded[:, 2:, 1:-1]
        + padded[:, 1:-1, :-2]
        + padded[:, 1:-1, 2:]
        - 4.0 * center
    )


def _resize_for_analysis(frame: np.ndarray, max_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / float(w)
    return cv2.resize(frame, (max_width, max(2, int(h * scale))), interpolation=cv2.INTER_AREA)


def _colorfulness(frames: Sequence[np.ndarray]) -> float:
    values = []
    for frame in frames:
        b, g, r = cv2.split(frame.astype("float"))
        rg = np.abs(r - g)
        yb = np.abs(0.5 * (r + g) - b)
        values.append((np.std(rg) + np.std(yb)) / 95.0)
    return _clamp(float(np.mean(values)) if values else 0.0)


def _build_candidate(
    video_file: str,
    source_name: str,
    video_duration: float,
    index: int,
    window: Dict,
    metrics: Dict,
) -> Dict:
    start = float(window["start"])
    end = float(window["end"])
    duration = max(0.01, end - start)
    motion = metrics["motion"]
    quality = metrics["quality_score"]
    brightness = metrics["brightness"]
    saturation = metrics["saturation"]
    contrast = metrics["contrast"]
    sharpness = metrics["sharpness"]
    colorfulness = metrics["colorfulness"]

    balanced_light = 1.0 - min(abs(brightness - 0.52) / 0.52, 1.0)
    action = _clamp(0.58 * motion + 0.17 * contrast + 0.12 * saturation + 0.13 * quality)
    beauty = _clamp(0.34 * quality + 0.21 * colorfulness + 0.18 * saturation + 0.17 * balanced_light + 0.10 * sharpness)
    tension = _clamp(0.42 * motion + 0.22 * contrast + 0.18 * (1.0 - balanced_light) + 0.18 * saturation)
    soft = _clamp(0.55 * beauty + 0.25 * balanced_light + 0.20 * (1.0 - motion))
    editorial = _clamp(0.35 * max(action, beauty, tension) + 0.35 * quality + 0.16 * saturation + 0.14 * contrast)

    tags = _fallback_tags(action, beauty, tension, soft, quality)
    semantic = {
        "action_intensity": action,
        "beauty_score": beauty,
        "emotion": "hype" if action > 0.68 else "soft" if soft > 0.64 else "tension" if tension > 0.62 else "neutral",
        "combat": 0.0,
        "chase": _clamp(motion * 0.6),
        "explosion": 0.0,
        "character_focus": _clamp(0.35 * quality + 0.20 * sharpness + 0.10 * balanced_light),
        "camera_motion": motion,
        "visual_quality": quality,
        "recommended_use": "drop" if action > 0.68 else "soft" if soft > 0.64 else "build" if tension > 0.62 else "flow",
        "description": "",
    }

    candidate_id = f"{_hash_text(os.path.abspath(video_file), 10)}_{index:05d}_{int(start * 1000):08d}"
    return {
        "id": candidate_id,
        "video_file": os.path.abspath(video_file),
        "source_name": source_name,
        "video_duration": video_duration,
        "start": start,
        "end": end,
        "duration": duration,
        "center": start + duration * 0.5,
        "peak_time": start + float(metrics.get("peak_offset", duration * 0.5)),
        "scene_index": int(window.get("scene_index", index)),
        "kind": window.get("kind", "scene"),
        "motion": motion,
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "sharpness": sharpness,
        "colorfulness": colorfulness,
        "quality_score": quality,
        "action_score": action,
        "beauty_score": beauty,
        "tension_score": tension,
        "soft_score": soft,
        "editorial_score": editorial,
        "tags": tags,
        "semantic": semantic,
        "ai_analyzed": False,
    }


def _fallback_tags(action: float, beauty: float, tension: float, soft: float, quality: float) -> List[str]:
    tags: List[str] = []
    if action >= 0.68:
        tags.append("action")
    if beauty >= 0.62:
        tags.append("beauty")
    if tension >= 0.62:
        tags.append("tension")
    if soft >= 0.64:
        tags.append("soft")
    if quality >= 0.68:
        tags.append("clean")
    if not tags:
        tags.append("flow")
    return tags


def _select_ai_candidates(candidates: List[Dict], max_windows: int) -> List[Dict]:
    if len(candidates) <= max_windows:
        return candidates

    selected: Dict[str, Dict] = {}

    def add_many(items: Sequence[Dict], count: int) -> None:
        for item in items[:count]:
            selected[item["id"]] = item

    ranked_action = sorted(candidates, key=lambda c: c.get("action_score", 0.0), reverse=True)
    ranked_beauty = sorted(candidates, key=lambda c: c.get("beauty_score", 0.0), reverse=True)
    ranked_quality = sorted(candidates, key=lambda c: c.get("quality_score", 0.0), reverse=True)

    add_many(ranked_action, max(1, max_windows // 3))
    add_many(ranked_beauty, max(1, max_windows // 4))
    add_many(ranked_quality, max(1, max_windows // 5))

    if len(selected) < max_windows:
        step = max(1, len(candidates) // max(1, max_windows - len(selected)))
        for item in candidates[::step]:
            selected[item["id"]] = item
            if len(selected) >= max_windows:
                break

    return list(selected.values())[:max_windows]


def _annotate_candidates_with_qwen(
    video_file: str,
    fps: float,
    candidates: List[Dict],
    qwen_model_path: str,
    use_gpu: bool,
    audio_profile: Dict,
    event_callback=None,
    run_stats: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    # [FORK] Digital-Union (R1): `run_stats` is the optional current-run accounting sink. It stays
    # `None` for every pre-existing caller, and a job is recorded only on the branch below that
    # actually calls `_run_qwen_worker` - so a configured skip and a candidate-less source, neither
    # of which issues a request, are correctly not counted as Qwen work performed this run.
    # [FORK] Digital-Union (D1 R3): annotation corrected from `-> None`. Every normal branch returns
    # a timings dict, and since D1 it also carries the load-bearing private `_QWEN_COMPLETED_KEY`
    # that the caller pops to decide `ai_enabled`. Annotation only - no behaviour or shape change.
    max_windows = int(os.environ.get("BEATSYNC_QWEN_MAX_WINDOWS", "120"))
    max_windows = max(0, max_windows)
    if max_windows == 0:
        print("   Qwen semantic analysis skipped (BEATSYNC_QWEN_MAX_WINDOWS=0).")
        # [FORK] Digital-Union (D1): skipped-by-configuration is not completed AI work. The batch
        # path already treats BEATSYNC_QWEN_MAX_WINDOWS=0 this way; this makes the two agree.
        return {_QWEN_COMPLETED_KEY: False}

    ai_candidates = _select_ai_candidates(candidates, max_windows)
    if not ai_candidates:
        # Nothing to annotate (no candidates) - genuinely finished, not a failure.
        return {"qwen_frame_count": 0, "qwen_tag_count": 0, _QWEN_COMPLETED_KEY: True}

    print(f"   Qwen semantic analysis: {len(ai_candidates)} candidate moments")
    _qwen_request_started = time.perf_counter()
    response = _run_qwen_worker(
        video_file=video_file,
        fps=fps,
        candidates=ai_candidates,
        qwen_model_path=qwen_model_path,
        use_gpu=use_gpu,
        audio_profile=audio_profile,
        event_callback=event_callback,
    )
    # [FORK] Digital-Union (D1 R4): the response envelope locates this job's timings; whether the
    # job's AI work actually *completed* is then decided by `_qwen_job_completed`, which requires
    # every decoded frame item to have produced a valid semantic. Envelope membership alone only
    # proves the worker's job loop returned - see that helper for the worker-contract reasoning.
    job_timings = response.get("timings_by_job") if isinstance(response, dict) else None
    envelope_present = isinstance(job_timings, dict) and _QWEN_SINGLE_JOB_ID in job_timings
    timing = job_timings.get(_QWEN_SINGLE_JOB_ID) if envelope_present else {}
    if not isinstance(timing, dict):
        timing = {}
    semantics = response.get("semantics") if isinstance(response, dict) else {}
    semantic_by_id = {str(k): v for k, v in semantics.items()} if isinstance(semantics, dict) else {}
    # [FORK] Digital-Union (D1 R5): the requested set is what `_select_ai_candidates` submitted.
    requested_ids = {str(candidate.get("id")) for candidate in ai_candidates}
    completed = _qwen_job_completed(timing, envelope_present, requested_ids, set(semantic_by_id))

    merged_count = 0
    for candidate in ai_candidates:
        semantic = semantic_by_id.get(str(candidate.get("id")))
        if semantic:
            _merge_semantic(candidate, semantic)
            merged_count += 1

    if not envelope_present:
        print("      Qwen returned no usable response; deterministic visual tags remain active.")
    elif not completed:
        print(
            f"      Qwen tagged {_reported_count(timing, 'tag_count', merged_count)} of "
            f"{_reported_count(timing, 'frame_count', 0)} decoded frames for "
            f"{len(requested_ids)} requested candidate(s); the semantic pass did not complete, so "
            f"deterministic visual tags remain active and AI analysis will retry."
        )
    else:
        print(f"      Qwen semantic tags merged: {merged_count}/{len(ai_candidates)}")

    # [FORK] Digital-Union (R1): a request was genuinely issued for this source, so it counts as
    # one current-run Qwen job whether or not it completed. This is the seam that makes the
    # serial/inline path visible - it never appears in `deferred_jobs`. Inline rather than via a
    # helper - see `_new_run_stats`.
    if run_stats is not None:
        # Submission truth: one attempt over `len(ai_candidates)` submitted candidates, plus the one
        # measured wall time of this worker invocation. All true even when the response is unusable.
        run_stats["qwen_jobs"] += 1
        run_stats["qwen_requested_count"] += len(ai_candidates)
        run_stats["qwen_seconds"] += float(time.perf_counter() - _qwen_request_started)
        # Response truth: completion, then only what the worker actually proved.
        if completed:
            run_stats["qwen_completed_jobs"] += 1
        else:
            run_stats["qwen_incomplete_jobs"] += 1
        # Decoded frames count ONLY when reported as a real integer. `_reported_count` falls back to
        # the requested count for source-record compatibility; that fallback stays for the persisted
        # field but is not evidence of decoding, so it must not reach current-run truth.
        reported_frames = timing.get("frame_count") if isinstance(timing, dict) else None
        if _is_count(reported_frames):
            run_stats["qwen_frame_count"] += reported_frames
        # Tags are the semantics actually merged into candidates - directly observed.
        run_stats["qwen_tag_count"] += merged_count
        reported_inference = timing.get("inference_seconds") if isinstance(timing, dict) else None
        if isinstance(reported_inference, (int, float)) and not isinstance(reported_inference, bool):
            run_stats["qwen_inference_seconds"] += float(reported_inference)
    return {
        _QWEN_COMPLETED_KEY: completed,
        # [FORK] Digital-Union (D1 R5): a worker-reported 0 stays 0; the requested/merged fallback
        # applies only when the field is genuinely absent.
        "qwen_frame_count": _reported_count(timing, "frame_count", len(ai_candidates)),
        "qwen_tag_count": _reported_count(timing, "tag_count", merged_count),
        "qwen_model_id": str(response.get("model_id") or "") if isinstance(response, dict) else "",
        "qwen_concurrency": int(response.get("batch_size") or 0) if isinstance(response, dict) else 0,
        "qwen_peak_vram_gb": float(response.get("peak_vram_gb") or 0.0) if isinstance(response, dict) else 0.0,
    }


def _run_qwen_worker(
    video_file: str,
    fps: float,
    candidates: Sequence[Dict],
    qwen_model_path: str,
    use_gpu: bool,
    audio_profile: Dict,
    event_callback=None,
) -> Dict:
    os.makedirs(VIDEO_ANALYSIS_CACHE_DIR, exist_ok=True)
    token = _hash_text(f"{video_file}|{time.time()}", 12)
    request_path = os.path.join(VIDEO_ANALYSIS_CACHE_DIR, f"qwen_request_{token}.json")
    response_path = os.path.join(VIDEO_ANALYSIS_CACHE_DIR, f"qwen_response_{token}.json")
    worker_path = os.path.join(ROOT_DIR, "src", "auto_mode", "stage5_qwen_scene_worker.py")
    request = {
        "video_file": video_file,
        "fps": fps,
        "qwen_model_path": qwen_model_path,
        "use_gpu": bool(use_gpu),
        "audio_profile": audio_profile,
        "candidates": [
            {
                "id": c.get("id"),
                "start": c.get("start"),
                "end": c.get("end"),
            }
            for c in candidates
        ],
    }
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(request, f)

    env = _qwen_worker_environment()
    timeout = max(1800, int(len(candidates) * 75))
    try:
        # [FORK] Digital-Union (Phase 2B): same streaming boundary as the batch path, so live Qwen
        # progress is not silently exclusive to batched runs.
        result = fork_qwen.run_qwen_worker(
            [sys.executable, worker_path, "--request", request_path, "--response", response_path],
            response_path,
            env=env,
            timeout=timeout,
            event_callback=event_callback,
            on_human_line=_qwen_worker_stdout_printer(),
            stderr_char_limit=1800,
        )
        if result.outcome.timed_out:
            raise subprocess.TimeoutExpired(worker_path, timeout)
        if result.outcome.launch_error:
            raise RuntimeError(result.outcome.launch_error)
        if result.outcome.returncode != 0:
            error_tail = result.outcome.stderr_tail
            print(f"      Qwen worker failed: {error_tail}")
            fork_progress.emit(event_callback, fork_progress.warning(
                5,
                f"Qwen worker failed (exit {result.outcome.returncode}): "
                f"{_short_qwen_error(error_tail)}",
                phase="qwen", qwen_returncode=result.outcome.returncode,
            ))
            return {}
        if result.response_error:
            raise RuntimeError(result.response_error)
        return result.response
    except subprocess.TimeoutExpired:
        print("      Qwen worker timed out; deterministic visual tags remain active.")
        fork_progress.emit(event_callback, fork_progress.warning(
            5, "Qwen worker timed out; deterministic visual tags remain active.",
            phase="qwen", qwen_timed_out=True,
        ))
        return {}
    except Exception as e:
        print(f"      Qwen worker error: {e}")
        fork_progress.emit(event_callback, fork_progress.warning(
            5, f"Qwen worker error: {_short_qwen_error(str(e))}", phase="qwen"))
        return {}
    finally:
        # Keep Qwen worker request/response files in video_analysis_cache for
        # reproducibility and debugging. The user explicitly wants this cache
        # folder to be preserved.
        pass


def _qwen_worker_environment() -> Dict[str, str]:
    env = os.environ.copy()
    llama_dir = os.environ.get("BEATSYNC_QWEN_LLAMA_DIR", DEFAULT_LLAMA_CPP_DIR)
    llama_root = os.path.normcase(os.path.abspath(llama_dir))
    path_parts = []
    for part in env.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        norm = os.path.normcase(os.path.abspath(part))
        if norm == llama_root:
            continue
        path_parts.append(part)
    env["PATH"] = os.pathsep.join([llama_dir] + path_parts)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _merge_semantic(candidate: Dict, semantic: Dict) -> None:
    if not semantic:
        return
    current = candidate.get("semantic", {})
    merged = dict(current)
    numeric_keys = [
        "action_intensity",
        "beauty_score",
        "combat",
        "chase",
        "explosion",
        "character_focus",
        "camera_motion",
        "visual_quality",
    ]
    for key in numeric_keys:
        if key in semantic:
            merged[key] = _clamp(semantic[key])
    for key in ["emotion", "recommended_use", "description"]:
        if semantic.get(key):
            merged[key] = str(semantic[key])[:160]

    candidate["semantic"] = merged
    candidate["ai_analyzed"] = True

    deterministic_action = _clamp(candidate.get("action_score", 0.0))
    deterministic_motion = _clamp(candidate.get("motion", 0.0))
    semantic_action = _clamp(merged.get("action_intensity", 0.0))
    motion_gate = _clamp(0.35 + 0.65 * deterministic_motion)

    # Qwen is used as semantic guidance, not as the sole truth. Motion/quality
    # metrics stay dominant so a beautiful static frame is not hallucinated into
    # an action scene.
    action = _clamp(0.72 * deterministic_action + 0.28 * semantic_action * motion_gate)
    beauty = _clamp(0.65 * candidate.get("beauty_score", 0.0) + 0.35 * merged.get("beauty_score", 0.0))
    quality = _clamp(0.70 * candidate.get("quality_score", 0.0) + 0.30 * merged.get("visual_quality", 0.0))
    camera_motion = _clamp(0.75 * deterministic_motion + 0.25 * merged.get("camera_motion", deterministic_motion))
    tension = _clamp(0.48 * candidate.get("tension_score", 0.0) + 0.22 * camera_motion + 0.18 * action + 0.12 * merged.get("character_focus", 0.0))
    soft = _clamp(0.48 * beauty + 0.24 * merged.get("character_focus", 0.0) + 0.18 * (1.0 - action) + 0.10 * quality)

    candidate["action_score"] = action
    candidate["beauty_score"] = beauty
    candidate["quality_score"] = quality
    candidate["tension_score"] = tension
    candidate["soft_score"] = soft
    candidate["editorial_score"] = _clamp(0.30 * max(action, beauty, tension) + 0.42 * quality + 0.16 * candidate.get("saturation", 0.0) + 0.12 * candidate.get("contrast", 0.0))

    tags = set(_fallback_tags(action, beauty, tension, soft, quality))
    rec = str(merged.get("recommended_use", "")).lower()
    emotion = str(merged.get("emotion", "")).lower()
    if rec and (rec != "drop" or action >= 0.45):
        tags.add(rec)
    if emotion and (emotion != "hype" or action >= 0.45):
        tags.add(emotion)
    if merged.get("combat", 0.0) > 0.50 and action >= 0.38:
        tags.add("combat")
    if merged.get("chase", 0.0) > 0.50 and action >= 0.38:
        tags.add("chase")
    if merged.get("explosion", 0.0) > 0.55 and action >= 0.38:
        tags.add("explosion")
    candidate["tags"] = sorted(tags)
