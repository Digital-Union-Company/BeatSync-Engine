"""L2 V1 — the process-local post-Stage-3 cache module.

``beatsync_fork.stage_cache`` is stdlib-only by rule, so it is imported and exercised directly: no
AST tricks, no synthesised parent, no numpy. The orchestration half — that ``analyze_beats_auto``
actually skips the front end and Stages 1-3 on a hit — lives in
``tests/test_l2_stage3_cache_identity.py``, which drives the real pipeline body.

Every file written here lives in pytest's ``tmp_path``. Nothing reads real media, and nothing in this
suite may create a cache directory or a cache file: the whole point of V1 is that there is no
persistence to create.
"""

from __future__ import annotations

import ast
import copy
import os
import threading

import pytest

from beatsync_fork import stage_cache as sc

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "stage_cache.py")

_CHUNK = 1 << 20
_SECOND = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _Cfg:
    """A duck-typed stand-in for ``AutoWaveConfig``.

    Deliberately not the real dataclass: ``analysis_config_identity`` reads by name through
    ``getattr``, so a test config proves the helper depends on the *named fields* rather than on the
    production class. ``tests/test_l2_stage3_cache_identity.py`` runs the same helper against the
    real ``AutoWaveConfig``.
    """

    #: Every Stage-4-only / unrelated field, so a test can change one and require the key not to move.
    _STAGE4_ONLY = {
        "low_energy_min_interval": 0.90,
        "medium_energy_min_interval": 0.58,
        "high_energy_min_interval": 0.38,
        "peak_energy_min_interval": 0.30,
        "low_energy_max_hold": 3.80,
        "medium_energy_max_hold": 2.80,
        "high_energy_max_hold": 1.85,
        "peak_energy_max_hold": 1.25,
        "enable_rare_micro_cuts": True,
        "max_micro_cut_ratio": 0.025,
        "micro_min_gap": 0.34,
        "micro_percentile": 96.5,
        "target_cut_ratio_min": 0.22,
        "target_cut_ratio_max": 0.46,
        "enable_video_analysis": True,
        "enable_qwen_semantics": True,
        "qwen_model_path": "",
        "anchor_bonus": 0.32,
        "phrase_bonus": 0.48,
    }

    def __init__(self, **over):
        values = {
            "sr": 22050,
            "hop_length": 512,
            "n_fft": 2048,
            "wave_smooth_beats": 16,
            "phrase_beats": 8,
            "bar_beats": 4,
            "section_min_seconds": 10.0,
        }
        values.update(self._STAGE4_ONLY)
        values.update(over)
        for name, value in values.items():
            setattr(self, name, value)


def _write(path, payload: bytes, mtime_ns: int = _SECOND) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return os.path.abspath(path)


def _small(tmp_path, name="track.mp3", filler=b"a", size=4096, mtime_ns=_SECOND) -> str:
    return _write(str(tmp_path / name), filler * size, mtime_ns)


def _large(tmp_path, name="big.wav", mtime_ns=_SECOND, *, head=b"H", gap1=b"g",
           middle=b"M", gap2=b"G", tail=b"T") -> str:
    """A 5 MiB file whose five 1 MiB regions are individually addressable.

    At 5 MiB the sampled windows are ``[0, 1 MiB)``, ``[2 MiB, 3 MiB)`` and ``[4 MiB, 5 MiB)``, so
    ``gap1`` (``[1, 2)``) and ``gap2`` (``[3, 4)``) are the two regions the bounded fingerprint
    deliberately does **not** read.
    """
    payload = (head * _CHUNK + gap1 * _CHUNK + middle * _CHUNK + gap2 * _CHUNK + tail * _CHUNK)
    assert len(payload) == 5 * _CHUNK
    return _write(str(tmp_path / name), payload, mtime_ns)


def _key(cfg=None, path="/lib/track.mp3", window=(0.0, None), use_gpu=False,
         fingerprint="ff", size=4096, mtime_ns=_SECOND):
    """A key built directly, so a test can vary exactly one component."""
    return sc.Stage3CacheKey(
        version=sc.L2_CACHE_VERSION,
        track_identity=sc.TrackIdentity(
            path=path, size=size, mtime_ns=mtime_ns, fingerprint=fingerprint),
        audio_window=window,
        analysis_config_identity=sc.analysis_config_identity(cfg or _Cfg()),
        use_gpu_requested=use_gpu,
    )


@pytest.fixture
def cache():
    return sc.Stage3ProcessCache()


# ---------------------------------------------------------------------------
# the version contract
# ---------------------------------------------------------------------------


def test_one_l2_version_constant_owns_the_artifact():
    assert sc.L2_CACHE_VERSION == "l2_stage3_v1"
    assert _key().version == sc.L2_CACHE_VERSION


def test_the_l2_version_is_independent_of_the_stage5_contract():
    """L2 and Stage 5 have separate contracts, and an L2 change must not touch Stage-5 identity.

    Executable code only: the module's own prose legitimately *names* the Stage-5 constants to
    explain that it has nothing to do with them, exactly as the Stage-5 rules name retired fields.
    """
    with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MODULE_PATH)
    code = _code_only(tree)
    for foreign in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "_video_signature",
                    "_cache_path", "video_analysis", "_qwen_config_token"):
        assert foreign not in code, f"stage_cache must not know about {foreign}"

    # and the Stage-5 constants are untouched in the real production file
    with open(os.path.join(_REPO_ROOT, "src", "video_analysis.py"), "r", encoding="utf-8") as handle:
        stage5 = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in stage5
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in stage5


def test_the_bundle_contract_is_exactly_the_five_post_stage3_facts():
    assert sc.STAGE3_BUNDLE_FIELDS == (
        "audio_duration", "beat_times", "tempo", "features", "sections")
    # the expensive raw front-end arrays are deliberately absent: nothing after Stage 3 reads them
    for excluded in ("y", "y_harmonic", "y_percussive", "beat_frames", "onset_env",
                     "selected_beats", "selection_info", "audio_visual_profile",
                     "video_analysis", "plan"):
        assert excluded not in sc.STAGE3_BUNDLE_FIELDS


def test_bundle_completeness_is_all_or_nothing():
    full = sc.stage3_bundle(audio_duration=1.0, beat_times=[0.0], tempo=120.0,
                            features={}, sections=[])
    assert sorted(full) == sorted(sc.STAGE3_BUNDLE_FIELDS)
    assert sc.bundle_is_complete(full) is True
    for drop in sc.STAGE3_BUNDLE_FIELDS:
        partial = {k: v for k, v in full.items() if k != drop}
        assert sc.bundle_is_complete(partial) is False, drop
    for junk in (None, [], "bundle", 7, object()):
        assert sc.bundle_is_complete(junk) is False


# ---------------------------------------------------------------------------
# §26  ONE entry, replaced atomically
# ---------------------------------------------------------------------------


def test_single_entry_replacement_semantics(cache):
    """put A -> get A; put B -> get A misses, get B succeeds. Never two retained entries."""
    a, b = _key(path="/lib/a.mp3"), _key(path="/lib/b.mp3")

    cache.put(a, {"v": "A"})
    assert cache.get(a) == {"v": "A"}

    cache.put(b, {"v": "B"})
    assert cache.get(a) is None, "the previous entry must be evicted, not retained"
    assert cache.get(b) == {"v": "B"}

    cache.put(a, {"v": "A2"})
    assert cache.get(b) is None
    assert cache.get(a) == {"v": "A2"}


def test_there_is_no_hidden_dictionary_of_historical_keys(cache):
    """Capacity is 1 by construction: the instance holds one key slot and one value slot."""
    assert set(sc.Stage3ProcessCache.__slots__) == {"_lock", "_key", "_value"}
    for index in range(25):
        cache.put(_key(path=f"/lib/{index}.mp3"), {"i": index})
    # only the last one survives
    assert cache.get(_key(path="/lib/24.mp3")) == {"i": 24}
    for index in range(24):
        assert cache.get(_key(path=f"/lib/{index}.mp3")) is None, index
    assert cache._value == {"i": 24}


def test_an_empty_cache_and_a_none_key_are_plain_misses(cache):
    assert cache.get(_key()) is None
    assert cache.get(None) is None
    assert cache.has_entry() is False
    cache.put(None, {"v": 1})
    assert cache.has_entry() is False, "a keyless bundle must never be stored"
    cache.put(_key(), {"v": 1})
    assert cache.has_entry() is True
    cache.clear()
    assert cache.has_entry() is False


def test_a_module_level_single_instance_exists():
    assert isinstance(sc.STAGE3_CACHE, sc.Stage3ProcessCache)


# ---------------------------------------------------------------------------
# §27  defensive copying, on BOTH sides
# ---------------------------------------------------------------------------


class _Mutable:
    """A mutable stand-in for a NumPy array: deep-copyable, comparable, and nothing numpy-specific.

    The module never imports numpy, so its copying contract has to hold for any mutable graph.
    """

    def __init__(self, values):
        self.values = list(values)

    def __eq__(self, other):
        return isinstance(other, _Mutable) and self.values == other.values

    def __repr__(self):
        return f"_Mutable({self.values!r})"


def _nested_bundle():
    return sc.stage3_bundle(
        audio_duration=42.0,
        beat_times=_Mutable([0.0, 0.5, 1.0]),
        tempo=123.0,
        features={"wave": _Mutable([0.1, 0.2]), "nested": {"deep": [1, 2, 3]}},
        sections=[{"index": 0, "type": "intro", "tags": ["a"]},
                  {"index": 1, "type": "drop", "tags": ["b"]}],
    )


def test_a_cache_hit_can_be_mutated_without_touching_the_stored_artifact(cache):
    """Hit #1 is mutated deeply; hit #2 must still equal the original stored value.

    This is load-bearing rather than defensive style: downstream code legitimately mutates
    `beat_info`, and handing two renders the same object graph would let the first corrupt the
    second's "cached" facts silently.
    """
    key = _key()
    original = _nested_bundle()
    reference = copy.deepcopy(original)

    cache.put(key, original)

    hit1 = cache.get(key)
    assert hit1 == reference

    hit1["beat_times"].values.append(99.0)
    hit1["features"]["wave"].values[0] = -1.0
    hit1["features"]["nested"]["deep"].append(4)
    hit1["sections"][0]["type"] = "vandalised"
    hit1["sections"].append({"index": 2, "type": "appended"})
    hit1["tempo"] = 0.0

    hit2 = cache.get(key)
    assert hit2 == reference, "GET must hand out an isolated copy"


def test_two_hits_are_distinct_objects_all_the_way_down(cache):
    key = _key()
    cache.put(key, _nested_bundle())
    hit1, hit2 = cache.get(key), cache.get(key)

    assert hit1 == hit2
    assert hit1 is not hit2
    assert hit1["beat_times"] is not hit2["beat_times"]
    assert hit1["features"] is not hit2["features"]
    assert hit1["features"]["wave"] is not hit2["features"]["wave"]
    assert hit1["features"]["nested"] is not hit2["features"]["nested"]
    assert hit1["sections"] is not hit2["sections"]
    assert hit1["sections"][0] is not hit2["sections"][0]


def test_mutating_the_caller_graph_after_put_cannot_reach_the_stored_artifact(cache):
    """PUT-side isolation. Without it the *producer* keeps a live handle into the cache."""
    key = _key()
    producer_bundle = _nested_bundle()
    reference = copy.deepcopy(producer_bundle)

    cache.put(key, producer_bundle)

    producer_bundle["beat_times"].values.clear()
    producer_bundle["features"]["nested"]["deep"] = ["wrecked"]
    producer_bundle["sections"][1]["type"] = "wrecked"
    producer_bundle["sections"].pop()

    assert cache.get(key) == reference, "PUT must store an isolated copy"


def test_the_stored_graph_is_never_handed_out(cache):
    key = _key()
    bundle = _nested_bundle()
    cache.put(key, bundle)
    stored = cache._value
    assert stored is not bundle
    assert cache.get(key) is not stored
    assert cache.get(key)["sections"] is not stored["sections"]


# ---------------------------------------------------------------------------
# thread safety
# ---------------------------------------------------------------------------


def test_a_lookup_never_observes_a_half_updated_pair(cache):
    """The (key, value) pair is read and written together, so no reader can pair key A with value B.

    Writers churn between two keys while readers hammer `get`; any observation of a key paired with
    the other key's value would be a torn read.
    """
    key_a, key_b = _key(path="/lib/a.mp3"), _key(path="/lib/b.mp3")
    expected = {key_a: "A", key_b: "B"}
    stop = threading.Event()
    torn = []

    def writer():
        index = 0
        while not stop.is_set():
            k = key_a if index % 2 == 0 else key_b
            cache.put(k, {"v": expected[k], "payload": list(range(64))})
            index += 1

    def reader():
        while not stop.is_set():
            for k in (key_a, key_b):
                hit = cache.get(k)
                if hit is not None and hit["v"] != expected[k]:
                    torn.append((k, hit["v"]))

    threads = [threading.Thread(target=writer) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(4000):
        cache.get(key_a)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert torn == [], f"torn (key, value) observations: {torn[:5]}"


def test_concurrent_hits_are_isolated_from_each_other(cache):
    """Two threads mutating their own hits must not see each other's damage."""
    key = _key()
    cache.put(key, _nested_bundle())
    reference = copy.deepcopy(cache.get(key))
    failures = []

    def worker(tag):
        for _ in range(40):
            hit = cache.get(key)
            hit["sections"][0]["type"] = tag
            hit["beat_times"].values.append(1.0)
            fresh = cache.get(key)
            if fresh != reference:
                failures.append(tag)
                return

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert failures == []
    assert cache.get(key) == reference


def test_the_lock_is_not_held_across_the_copy(cache):
    """`put`/`get` must not hold the lock while deep-copying, so a slow copy cannot serialise the
    whole process. Proven by copying under the lock being observable as re-entrant progress: a
    second thread's `has_entry` completes while a deepcopy of a deliberately slow object runs."""
    key = _key()
    entered = threading.Event()
    release = threading.Event()
    observed = []

    class _SlowCopy:
        def __deepcopy__(self, memo):
            entered.set()
            release.wait(timeout=5)
            return _SlowCopy()

    def putter():
        cache.put(key, {"slow": _SlowCopy(), **sc.stage3_bundle(1.0, [], 1.0, {}, [])})

    thread = threading.Thread(target=putter)
    thread.start()
    assert entered.wait(timeout=5), "the copy never started"
    observed.append(cache.has_entry())   # would block forever if the lock were held across the copy
    release.set()
    thread.join(timeout=10)

    assert observed == [False], "the entry must only appear after the copy completes"
    assert cache.has_entry() is True


# ---------------------------------------------------------------------------
# §28  track identity
# ---------------------------------------------------------------------------


def test_identity_1_an_unchanged_file_keeps_a_stable_identity(tmp_path):
    track = _small(tmp_path)
    first, second = sc.track_identity(track), sc.track_identity(track)
    assert first is not None and first == second
    assert first.path == os.path.abspath(track)
    assert first.size == 4096
    assert first.mtime_ns == _SECOND


def test_identity_2_changed_bytes_behind_an_exact_restored_mtime_re_key(tmp_path):
    """The reason the content fingerprint exists: same path, same size, same `st_mtime_ns` to the
    nanosecond, different bytes. A metadata-only identity would reuse a stale artifact here."""
    track = _small(tmp_path, filler=b"a")
    before = sc.track_identity(track)

    _write(track, b"b" * 4096, mtime_ns=_SECOND)      # byte-for-byte restored timestamp
    after = sc.track_identity(track)

    assert after is not None
    assert (after.path, after.size, after.mtime_ns) == (before.path, before.size, before.mtime_ns)
    assert after.fingerprint != before.fingerprint
    assert after != before


def test_identity_3_the_same_bytes_at_a_different_path_re_key(tmp_path):
    """Identity is location + content, exactly as in Stage 5. Moving the file re-keys; content-only
    identity would deduplicate copies, which is a semantic change V1 does not make."""
    here = _small(tmp_path / "one", filler=b"z")
    there = _small(tmp_path / "two", filler=b"z")
    a, b = sc.track_identity(here), sc.track_identity(there)

    assert a.fingerprint == b.fingerprint, "same bytes, same content fingerprint"
    assert a.path != b.path
    assert a != b


@pytest.mark.parametrize("offset", [0, 1, 2047, 4095])
def test_identity_4_a_small_file_is_hashed_whole_so_any_byte_matters(tmp_path, offset):
    payload = bytearray(b"a" * 4096)
    track = _write(str(tmp_path / "small.mp3"), bytes(payload))
    before = sc.bounded_fingerprint(track, 4096)

    payload[offset] = ord("b")
    _write(track, bytes(payload))
    assert sc.bounded_fingerprint(track, 4096) != before, offset


@pytest.mark.parametrize("region", ["head", "middle", "tail"])
def test_identity_5_each_sampled_window_of_a_large_file_matters(tmp_path, region):
    base = _large(tmp_path, name=f"{region}_base.wav")
    changed = _large(tmp_path, name=f"{region}_changed.wav", **{region: b"X"})
    size = 5 * _CHUNK
    assert os.path.getsize(base) == os.path.getsize(changed) == size
    assert sc.bounded_fingerprint(base, size) != sc.bounded_fingerprint(changed, size), region


def test_identity_5b_the_bounded_geometry_reads_exactly_three_mib(tmp_path):
    """Bounded, not whole-file: the fingerprint is accidental-staleness protection, so it makes no
    claim about bytes outside the sampled windows and must not be described as adversarial."""
    size = 5 * _CHUNK
    base = _large(tmp_path, name="gap_base.wav")
    gap_only = _large(tmp_path, name="gap_changed.wav", gap1=b"X", gap2=b"Y")
    assert os.path.getsize(gap_only) == size
    # Documented, not aspirational: these two differ in bytes the geometry deliberately never reads.
    assert sc.bounded_fingerprint(base, size) == sc.bounded_fingerprint(gap_only, size)


def test_identity_6_size_is_hashed_so_a_truncation_between_windows_is_caught(tmp_path):
    track = _write(str(tmp_path / "t.wav"), b"a" * (4 * _CHUNK))
    before = sc.bounded_fingerprint(track, 4 * _CHUNK)
    _write(track, b"a" * (4 * _CHUNK - 1))
    assert sc.bounded_fingerprint(track, 4 * _CHUNK - 1) != before


def test_identity_7_a_missing_or_unreadable_source_has_no_identity(tmp_path):
    """No weak fallback: a stable token for an unprovable input is exactly what would let a stale
    artifact be reused, so the answer is None and the cache is simply unavailable."""
    assert sc.track_identity(str(tmp_path / "absent.mp3")) is None
    assert sc.bounded_fingerprint(str(tmp_path / "absent.mp3"), 10) is None
    for junk in (None, "", 7, object()):
        assert sc.track_identity(junk) is None, junk


def test_identity_8_a_directory_yields_no_identity(tmp_path):
    folder = str(tmp_path / "folder")
    os.makedirs(folder, exist_ok=True)
    assert sc.track_identity(folder) is None


def test_no_identity_means_no_key_and_therefore_no_cache(tmp_path):
    assert sc.stage3_cache_key(str(tmp_path / "gone.mp3"), 0.0, None, _Cfg(), False) is None
    real = _small(tmp_path)
    assert sc.stage3_cache_key(real, 0.0, None, _Cfg(), False) is not None


# ---------------------------------------------------------------------------
# §29  the effective audio window
# ---------------------------------------------------------------------------


def test_window_1_identical_effective_semantics_reuse():
    assert _key(window=sc.audio_window_identity(0.0, None)) == \
           _key(window=sc.audio_window_identity(0, None))
    assert _key(window=sc.audio_window_identity(10, 30)) == \
           _key(window=sc.audio_window_identity(10.0, 30.0))


def test_window_2_a_different_start_is_a_different_key():
    assert _key(window=sc.audio_window_identity(0.0, 30.0)) != \
           _key(window=sc.audio_window_identity(5.0, 30.0))


def test_window_3_a_different_effective_duration_is_a_different_key():
    assert _key(window=sc.audio_window_identity(0.0, 30.0)) != \
           _key(window=sc.audio_window_identity(0.0, 45.0))
    assert _key(window=sc.audio_window_identity(0.0, None)) != \
           _key(window=sc.audio_window_identity(0.0, 30.0))


def test_window_4_an_unbounded_duration_is_its_own_value_not_a_number():
    """`None` means "no bounded duration" and must stay distinguishable from any float, including
    0.0 — otherwise a full-track render and a zero-length trim would share one artifact."""
    assert sc.audio_window_identity(0.0, None) == (0.0, None)
    assert sc.audio_window_identity(0.0, 0.0) == (0.0, 0.0)
    assert sc.audio_window_identity(0.0, None) != sc.audio_window_identity(0.0, 0.0)


def test_window_5_a_falsy_start_normalises_without_changing_meaning():
    assert sc.audio_window_identity(None, 30.0) == (0.0, 30.0)
    assert sc.audio_window_identity(0, 30.0) == (0.0, 30.0)
    assert sc.audio_window_identity(0.0, 30.0) == (0.0, 30.0)


# ---------------------------------------------------------------------------
# §30  Stage 1-3 analysis config identity
# ---------------------------------------------------------------------------


def test_config_identity_is_exactly_the_seven_documented_fields():
    assert sc.STAGE3_ANALYSIS_CONFIG_FIELDS == (
        "sr", "hop_length", "n_fft", "wave_smooth_beats",
        "phrase_beats", "bar_beats", "section_min_seconds")
    identity = sc.analysis_config_identity(_Cfg())
    assert [name for name, _ in identity] == list(sc.STAGE3_ANALYSIS_CONFIG_FIELDS)
    assert dict(identity)["sr"] == 22050


@pytest.mark.parametrize("field,changed", [
    ("sr", 44100),
    ("hop_length", 256),
    ("n_fft", 4096),
    ("wave_smooth_beats", 8),
    ("phrase_beats", 16),
    ("bar_beats", 3),
    ("section_min_seconds", 5.0),
])
def test_every_stage1to3_config_field_changes_the_key(field, changed):
    base = _key(cfg=_Cfg())
    assert base != _key(cfg=_Cfg(**{field: changed})), field


@pytest.mark.parametrize("field", sorted(_Cfg._STAGE4_ONLY))
def test_no_stage4_or_qwen_config_field_changes_the_stage3_key(field):
    """Stage-4 safety floors, max holds, cut-ratio caps, micro-cut policy, anchor bonuses and the
    video-analysis/Qwen settings cannot change a beat grid, a feature curve or a section boundary,
    so including one would over-invalidate the artifact."""
    current = _Cfg._STAGE4_ONLY[field]
    changed = (not current) if isinstance(current, bool) else (
        "other" if isinstance(current, str) else current + 1)
    assert _key(cfg=_Cfg()) == _key(cfg=_Cfg(**{field: changed})), field


def test_use_gpu_requested_separates_identities():
    """Stage 2 has a CPU/CuPy branch and this repository has no byte-exact parity contract, so V1
    keeps the two *requested* modes apart. No claim is made that their outputs differ."""
    assert _key(use_gpu=False) != _key(use_gpu=True)
    # coerced to a real bool, so truthiness variants of one request share one identity
    assert _key(use_gpu=bool(0)) == _key(use_gpu=False)
    assert _key(use_gpu=bool(1)) == _key(use_gpu=True)


def test_the_key_is_frozen_hashable_and_structured():
    key = _key()
    assert hash(key) == hash(_key())
    with pytest.raises(Exception):
        key.version = "tampered"
    assert {field.name for field in __import__("dataclasses").fields(sc.Stage3CacheKey)} == {
        "version", "track_identity", "audio_window", "analysis_config_identity",
        "use_gpu_requested"}
    assert {field.name for field in __import__("dataclasses").fields(sc.TrackIdentity)} == {
        "path", "size", "mtime_ns", "fingerprint"}


def test_no_creative_or_stage5_state_can_appear_in_the_key():
    """Structural: the whole module may not mention a creative control, a producer of one, or any
    Stage-5/render concept. A Stage-3 artifact that missed because the seed changed would be an
    over-invalidation, not a correctness win."""
    with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MODULE_PATH)
    code = _code_only(tree)
    for banned in ("seed", "cut_density", "micro_cuts", "semantic_emphasis", "energy_response",
                   "motion_bias", "source_diversity", "preset", "variant", "director",
                   "proposal", "freestyle", "qwen", "encoder", "fps", "video_files",
                   "smart_mix", "voice", "sfx", "output_path"):
        assert banned not in code.lower(), f"{banned} must not participate in the Stage-3 key"


# ---------------------------------------------------------------------------
# §42 / §43  stdlib-only, and no persistence whatsoever
# ---------------------------------------------------------------------------


def _code_only(tree: ast.AST) -> str:
    """Unparsed source with every string literal blanked, so prose cannot satisfy or trip a guard."""
    class _Blank(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str):
                return ast.copy_location(ast.Constant(value=""), node)
            return node

    return ast.unparse(_Blank().visit(copy.deepcopy(tree)))


def test_the_module_imports_only_stdlib():
    """`tests/test_no_runtime_dependency.py` discovers every fork module and enforces this for the
    package as a whole; this is the module-specific, explicitly-named half."""
    with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MODULE_PATH)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    for forbidden in ("numpy", "cupy", "librosa", "gradio", "cv2", "torch",
                      "auto_mode", "video_analysis", "gui", "paths", "logger"):
        assert forbidden not in imported, f"stage_cache must not import {forbidden}"
    assert imported <= {"__future__", "copy", "dataclasses", "hashlib", "os", "threading", "typing"}


def test_the_module_creates_no_persistent_side_effect():
    """V1 has no cache directory, no cache JSON and no pickle artifact. Reading the audio file for
    identity is the only filesystem access allowed."""
    with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MODULE_PATH)
    code = _code_only(tree)
    for banned in ("os.makedirs", "json.dump", "pickle", "np.save", "tempfile",
                   "shutil", "os.replace", "mkstemp", "os.remove", "os.unlink"):
        assert banned not in code, f"stage_cache must not use {banned}"

    # the one permitted `open` is read-binary, for the fingerprint
    opens = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open"]
    assert len(opens) == 1, f"exactly one open(); found {len(opens)}"
    assert [ast.unparse(a) for a in opens[0].args][1:] == ["'rb'"], ast.unparse(opens[0])


def test_using_the_cache_writes_nothing_to_disk(tmp_path, cache):
    """Behavioural half of the guard: a full put/get cycle leaves the filesystem untouched."""
    track = _small(tmp_path, name="t.mp3")
    before = sorted(os.walk(str(tmp_path)))

    key = sc.stage3_cache_key(track, 0.0, None, _Cfg(), False)
    assert key is not None
    cache.put(key, _nested_bundle())
    assert cache.get(key) is not None

    assert sorted(os.walk(str(tmp_path))) == before, "no file or directory may be created"
    for leaked in ("input/stage_cache", ".stage_cache", "stage_cache.json", "stage_cache.pkl"):
        assert not os.path.exists(os.path.join(_REPO_ROOT, leaked)), leaked


def test_nothing_survives_a_fresh_instance():
    """Process-local: a new instance starts cold, exactly as a process restart does."""
    first = sc.Stage3ProcessCache()
    first.put(_key(), {"v": 1})
    assert sc.Stage3ProcessCache().get(_key()) is None
