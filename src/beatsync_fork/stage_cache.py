#!/usr/bin/env python3
"""[FORK] Digital-Union (L2 V1): the process-local post-Stage-3 audio-analysis cache.

Stdlib only, by CLAUDE.md's hard rule for ``src/beatsync_fork/`` - no numpy, no librosa, no cupy,
no cv2, no gradio, no ``auto_mode``, no ``video_analysis``. The cached bundle *contains* NumPy
arrays, but this module never imports or understands NumPy: a bundle is an opaque ``Any`` that is
deep-copied in and deep-copied out.

What this is, exactly
---------------------
**One** most-recent entry, in this process only, holding the five facts the pipeline still needs
after Stage 3::

    audio_duration   beat_times   tempo   features   sections

Measured on the real Nero track: the frontend (audio load 0.502 s, normalize 0.001 s, HPSS
12.453 s) plus Stages 1-3 (0.487 s + 0.993 s + 1.299 s) is **~15.735 s** of reusable work, and the
post-Stage-3 artifact serialises to ~344 KB. Stage 4 measured ~0.0076 s, so it is deliberately
*not* cached here - its cost does not pay for the extra key and correctness surface. Stage 6
measured ~2.930 s and is likewise uncached, because every target scenario changes something Stage 6
reads.

Why one entry and no disk
-------------------------
The measured user-value scenarios are *same-track repeated renders* and *candidate 2 of a
sequential C3 render batch*. One entry captures both without inventing a multi-track lifetime
policy, an LRU, a size knob or a persistence format. A process restart therefore starts cold, and
that is the whole contract - nothing here is durable, and nothing here is execution authority.

Full contract: ``.claude/rules/l2-stage-cache.md``.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import threading
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "L2_CACHE_VERSION",
    "STAGE3_ANALYSIS_CONFIG_FIELDS",
    "STAGE3_BUNDLE_FIELDS",
    "TrackIdentity",
    "Stage3CacheKey",
    "Stage3ProcessCache",
    "STAGE3_CACHE",
    "bounded_fingerprint",
    "track_identity",
    "audio_window_identity",
    "analysis_config_identity",
    "stage3_cache_key",
]


# ---------------------------------------------------------------------------
# the one L2 version constant
# ---------------------------------------------------------------------------

#: Owns the meaning and schema of the process-local post-Stage-3 artifact. Bump it when Stage 1-3
#: algorithm/output semantics change, or when the stored bundle contract changes.
#:
#: L2 and Stage 5 have **independent** contracts: this constant has nothing to do with
#: ``CACHE_CONTRACT_VERSION`` (``stage5_cache_v3``) or ``ANALYSIS_VERSION``, and neither of those
#: may be touched for an L2 change.
L2_CACHE_VERSION = "l2_stage3_v1"


#: Every ``AutoWaveConfig`` field that can affect the audio front-end or Stages 1-3, and therefore
#: the whole of what the Stage-3 analysis-config identity covers. Derived from the real current
#: source: ``cfg.sr`` (the ``librosa.load`` call), ``cfg.hop_length`` (Stages 1, 2 and 3),
#: ``cfg.n_fft`` (Stages 2 and 3), ``cfg.wave_smooth_beats`` / ``cfg.phrase_beats`` /
#: ``cfg.bar_beats`` (Stage 2) and ``cfg.section_min_seconds`` (Stage 3).
#:
#: ``tests/test_l2_stage3_cache_identity.py`` re-derives that set from the real Stage 1-3 source on
#: every run and fails if a field is read that is not represented here, so a future Stage 1-3 edit
#: cannot silently widen what affects the artifact while leaving the key behind.
STAGE3_ANALYSIS_CONFIG_FIELDS = (
    "sr",
    "hop_length",
    "n_fft",
    "wave_smooth_beats",
    "phrase_beats",
    "bar_beats",
    "section_min_seconds",
)

#: The post-Stage-3 facts the bundle carries, and nothing else. The expensive raw front-end arrays
#: (``y``, ``y_harmonic``, ``y_percussive``, ``beat_frames``, ``onset_env``) are deliberately absent:
#: nothing after Stage 3 reads them, so retaining them merely because they were expensive to compute
#: would hold megabytes of audio per entry for no reuse at all.
STAGE3_BUNDLE_FIELDS = (
    "audio_duration",
    "beat_times",
    "tempo",
    "features",
    "sections",
)


# ---------------------------------------------------------------------------
# bounded content identity
# ---------------------------------------------------------------------------

_FINGERPRINT_CHUNK = 1 << 20
_FINGERPRINT_WHOLE_FILE_LIMIT = 3 * _FINGERPRINT_CHUNK
_FINGERPRINT_DIGEST_SIZE = 16


def bounded_fingerprint(path: str, size: int) -> Optional[str]:
    """Deterministic bounded content fingerprint of an audio file, or ``None`` if unreadable.

    Structurally the accepted D2 geometry, re-expressed here rather than imported: BLAKE2b with a
    16-byte digest, the file size hashed first, then content samples::

        size <= 3 MiB  -> the whole file, read once
        size >  3 MiB  -> exactly three non-overlapping 1 MiB windows
                          [0, 1 MiB)
                          [mid, mid + 1 MiB)  with
                              mid = max(1 MiB, min(size // 2 - 512 KiB, size - 2 MiB))
                          [size - 1 MiB, size)

    The clamp guarantees the middle window never overlaps the head or the tail, so no byte is hashed
    twice and the bytes read are exactly ``min(size, 3 MiB)``.

    This is **accidental** stale-result prevention, exactly the same class of protection Stage 5's
    source identity provides - not an adversarial or cryptographic media identity. No guarantee is
    made about bytes outside the sampled windows of a large file, and none is needed: the failure it
    exists to stop is a same-size, same-timestamp rewrite being mistaken for the original.

    ``None`` on any read failure, so the caller treats the track as having no strong identity rather
    than inventing a weak placeholder.
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


@dataclasses.dataclass(frozen=True)
class TrackIdentity:
    """Strong identity of one audio file: location **plus** content.

    The absolute path stays in identity on purpose, exactly as it does in Stage 5: moving the same
    audio file to a different path re-keys. Content-only identity would deduplicate copies, which is
    a semantic change this does not make.
    """

    path: str
    size: int
    mtime_ns: int
    fingerprint: str


def track_identity(audio_file: str) -> Optional[TrackIdentity]:
    """Absolute path + ``st_size`` + ``st_mtime_ns`` + bounded fingerprint, or ``None``.

    ``st_mtime_ns`` closes integer-second truncation but not an exact-mtime restore; the fingerprint
    is what catches that, which is why the timestamp alone is not enough.

    An unstatable or unreadable source yields ``None``, which means *the cache is unavailable for
    this invocation* - the render then follows the existing uncached path. There is deliberately no
    weak fallback identity: a stable token for an unprovable input is exactly what would let a stale
    bundle be reused.
    """
    try:
        absolute = os.path.abspath(audio_file)
        stat = os.stat(absolute)
    except (OSError, TypeError, ValueError):
        return None
    fingerprint = bounded_fingerprint(absolute, stat.st_size)
    if fingerprint is None:
        return None
    return TrackIdentity(
        path=absolute,
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# the rest of the key
# ---------------------------------------------------------------------------


def audio_window_identity(start_time: Any, effective_duration: Any) -> Tuple[float, Optional[float]]:
    """The *effective* load window, never the raw UI syntax.

    Takes the offset and the already-resolved effective duration - i.e. the two values the pipeline
    actually hands ``librosa.load`` - so two different UI inputs that produce identical current load
    semantics key identically, and different effective trims cannot reuse. The end-time-to-duration
    rule itself stays where it already lives, in ``analyze_beats_auto``; this normalises what came
    out of it rather than restating it, so there is no second trim contract to drift.
    """
    start = float(start_time or 0.0)
    if effective_duration is None:
        return (start, None)
    return (start, float(effective_duration))


def analysis_config_identity(cfg: Any) -> Tuple[Tuple[str, Any], ...]:
    """Only the configuration that can affect the front-end or Stages 1-3.

    Read by name off ``STAGE3_ANALYSIS_CONFIG_FIELDS`` through ``getattr``, so there is exactly one
    list of participating fields and the drift test can compare it against the real source. Field
    names travel with the values, so reordering the tuple is a loud change rather than a silent
    re-keying.

    Every Stage-4-only field is absent by construction: the energy min-intervals and max-holds,
    ``target_cut_ratio_*``, the micro-cut policy, the anchor bonuses, and every video-analysis or
    Qwen setting. None of them can change a beat grid, a feature curve or a section boundary.
    """
    return tuple((name, getattr(cfg, name)) for name in STAGE3_ANALYSIS_CONFIG_FIELDS)


@dataclasses.dataclass(frozen=True)
class Stage3CacheKey:
    """Everything that can change the post-Stage-3 artifact, and nothing else.

    Frozen and hashable, with structured fields rather than a concatenated string, so a malformed
    component cannot silently collide with a well-formed one.

    **What is deliberately absent, and why.** No creative state: not the Variation Seed, Cut
    Density, Micro Cuts, Semantic Emphasis, Energy Response, Motion Bias or Source Diversity; not a
    preset name, Variant Lab state, a Director prompt or proposal, a Freestyle checkbox, declaration
    or resolved section rule. None of those exist before Stage 4, so including one would
    over-invalidate the artifact with pure UI producer state. No Stage-5 state either: no video
    source list, no Qwen model or config, no cache contract - a source-library change must not
    invalidate cached audio analysis, and an audio change must not invalidate Stage-5 media records.
    And nothing about rendering: no encoder, FPS, voice/SFX setting or output path.
    """

    version: str
    track_identity: TrackIdentity
    audio_window: Tuple[float, Optional[float]]
    analysis_config_identity: Tuple[Tuple[str, Any], ...]
    use_gpu_requested: bool


def stage3_cache_key(audio_file: str, start_time: Any, effective_duration: Any,
                     cfg: Any, use_gpu: Any) -> Optional[Stage3CacheKey]:
    """The whole key, or ``None`` when the track has no provable identity.

    ``use_gpu_requested`` participates because Stage 2 has a CPU/CuPy execution branch and this
    repository has no byte-exact CPU/GPU parity contract. V1 therefore keeps the two *requested*
    modes in separate cache identities. **No claim is made that their outputs differ** - this is
    conservative cache correctness, and sharing one artifact between them would be asserting a
    parity nobody has measured.
    """
    identity = track_identity(audio_file)
    if identity is None:
        return None
    return Stage3CacheKey(
        version=L2_CACHE_VERSION,
        track_identity=identity,
        audio_window=audio_window_identity(start_time, effective_duration),
        analysis_config_identity=analysis_config_identity(cfg),
        use_gpu_requested=bool(use_gpu),
    )


# ---------------------------------------------------------------------------
# the one-entry process-local cache
# ---------------------------------------------------------------------------


class Stage3ProcessCache:
    """Exactly one most-recent (key, bundle) pair, in this process, behind one lock.

    No LRU, no configurable capacity, no dictionary of historical keys, no directory and no
    persistence: a new key replaces the previous pair atomically, and a process restart starts cold.

    **Defensive copying is load-bearing, on both sides.** The bundle holds mutable values - NumPy
    arrays, dicts, a list of section dicts - so handing the same object graph to two renders would
    let one render's downstream mutation corrupt the next render's "cached" facts. ``put`` stores a
    deep copy of what it was given and ``get`` returns a deep copy of what it holds, so the stored
    graph is never reachable from any caller. This does not rely on downstream code being careful.
    """

    __slots__ = ("_lock", "_key", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Optional[Stage3CacheKey] = None
        self._value: Any = None

    def get(self, key: Optional[Stage3CacheKey]) -> Optional[Any]:
        """The bundle for ``key`` as a fresh deep copy, or ``None`` on a miss."""
        if key is None:
            return None
        with self._lock:
            stored_key = self._key
            stored_value = self._value
        # The (key, value) pair is read together under the lock, so a lookup observes either the
        # complete old entry or the complete new entry - never a half-updated pair. The copy itself
        # happens outside the lock: `put` replaces the stored reference rather than mutating the old
        # graph, and the stored graph is never handed out, so what we captured cannot change under us.
        if stored_key is None or stored_key != key:
            return None
        return copy.deepcopy(stored_value)

    def put(self, key: Optional[Stage3CacheKey], value: Any) -> None:
        """Replace the single entry. The caller's object graph is copied, not adopted."""
        if key is None:
            return
        stored = copy.deepcopy(value)
        with self._lock:
            self._key = key
            self._value = stored

    def clear(self) -> None:
        """Drop the entry. Used by tests; the production path never needs it."""
        with self._lock:
            self._key = None
            self._value = None

    def has_entry(self) -> bool:
        with self._lock:
            return self._key is not None


#: The one process-local instance. Module-level rather than passed around, because its whole purpose
#: is to outlive a single ``analyze_beats_auto`` call within one process.
STAGE3_CACHE = Stage3ProcessCache()


def stage3_bundle(audio_duration: Any, beat_times: Any, tempo: Any,
                  features: Any, sections: Any) -> Dict[str, Any]:
    """Assemble the post-Stage-3 bundle. Keyword order follows ``STAGE3_BUNDLE_FIELDS``."""
    return {
        "audio_duration": audio_duration,
        "beat_times": beat_times,
        "tempo": tempo,
        "features": features,
        "sections": sections,
    }


def bundle_is_complete(bundle: Any) -> bool:
    """Is this a bundle carrying every required field?

    Used by the reader before it trusts a hit, so a malformed or partially-built mapping degrades to
    an ordinary miss instead of entering the pipeline as half a result.
    """
    if not isinstance(bundle, dict):
        return False
    return all(name in bundle for name in STAGE3_BUNDLE_FIELDS)
