"""Two truth contracts the event model claims but did not yet keep.

**A — deep immutability.** ``ProgressEvent`` is documented as immutable, and R2 made ``data`` a
``MappingProxyType``. That is only shallow: the pipeline really does emit nested mutable values
(``section_types=[...]`` from Stage 3, ``first_failures=[...]`` from the Stage 6 refusal), so
``event.data["section_types"].append(...)`` still worked, and ``as_dict()``'s shallow copy handed the
*same* nested objects to every consumer.

**B — honest throughput.** The Stage 5 source counter advances for cache hits as well as for real
analysis, and ``rate = current / elapsed`` therefore billed instant cache hits as analysis throughput:
420 cache hits plus one slow real analysis reported hundreds of sources per second. The count was
right; the rate was not.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace as dc_replace

import pytest

from beatsync_fork.progress import (
    StageCounter,
    end,
    error,
    metric,
    progress,
    start,
)
from beatsync_fork.progress_view import ProgressView


# ---------------------------------------------------------------------------
# A — deep immutability of event.data
# ---------------------------------------------------------------------------


def test_nested_list_in_data_cannot_be_mutated():
    """Stage 3's real payload shape: section_types=[...]."""
    event = end(3, "5 sections", section_types=["intro", "chorus", "outro"])
    with pytest.raises((TypeError, AttributeError)):
        event.data["section_types"].append("INJECTED")
    assert list(event.data["section_types"]) == ["intro", "chorus", "outro"]


def test_nested_list_from_the_stage6_refusal_cannot_be_mutated():
    """Stage 6's real payload shape: first_failures=[...]."""
    event = error(6, "3 of 6 clip(s) failed", first_failures=["clip 1: boom", "clip 2: boom"])
    with pytest.raises((TypeError, AttributeError)):
        event.data["first_failures"].append("INJECTED")
    assert len(event.data["first_failures"]) == 2


def test_nested_mapping_in_data_cannot_be_mutated_or_extended():
    event = error(6, "refused", diagnostic={"failures": ["a", "b"], "count": 2})
    with pytest.raises(TypeError):
        event.data["diagnostic"]["count"] = 99
    with pytest.raises(TypeError):
        event.data["diagnostic"]["new_key"] = "INJECTED"
    with pytest.raises((TypeError, AttributeError)):
        event.data["diagnostic"]["failures"].append("INJECTED")
    assert list(event.data["diagnostic"]["failures"]) == ["a", "b"]
    assert event.data["diagnostic"]["count"] == 2


def test_deeply_nested_structures_are_frozen_all_the_way_down():
    event = metric(5, "m", tree={"level1": {"level2": [{"level3": ["leaf"]}]}})
    level2 = event.data["tree"]["level1"]["level2"]
    with pytest.raises((TypeError, AttributeError)):
        level2.append("INJECTED")
    leaf_holder = level2[0]
    with pytest.raises(TypeError):
        leaf_holder["level3"] = "INJECTED"
    with pytest.raises((TypeError, AttributeError)):
        leaf_holder["level3"].append("INJECTED")
    assert list(leaf_holder["level3"]) == ["leaf"]


def test_data_is_isolated_from_the_caller_nested_object():
    """Mutating the list you passed in must not change the event afterwards."""
    sections = ["intro", "chorus"]
    event = end(3, "sections", section_types=sections)
    sections.append("LATE")
    assert list(event.data["section_types"]) == ["intro", "chorus"]


def test_scalars_are_left_alone():
    """Only containers are converted; ordinary values keep their type."""
    event = progress(5, 1, 2, "x", cache_hits=420, ratio=0.375, ok=True, name="s", nothing=None)
    assert event.data["cache_hits"] == 420 and isinstance(event.data["cache_hits"], int)
    assert event.data["ratio"] == 0.375 and isinstance(event.data["ratio"], float)
    assert event.data["ok"] is True
    assert event.data["name"] == "s" and isinstance(event.data["name"], str)
    assert event.data["nothing"] is None


def test_strings_are_not_treated_as_sequences():
    event = metric(1, "m", label="abc")
    assert event.data["label"] == "abc"


# ---------------------------------------------------------------------------
# A — as_dict() isolation
# ---------------------------------------------------------------------------


def test_as_dict_shares_no_mutable_nested_container_with_the_event():
    event = end(3, "sections", section_types=["intro", "chorus"],
                diagnostic={"failures": ["a"]})
    payload = event.as_dict()

    payload["data"]["section_types"].append("VIA_AS_DICT")
    payload["data"]["diagnostic"]["failures"].append("VIA_AS_DICT")
    payload["data"]["diagnostic"]["injected"] = True
    payload["data"]["new_top_level"] = True

    assert list(event.data["section_types"]) == ["intro", "chorus"]
    assert list(event.data["diagnostic"]["failures"]) == ["a"]
    assert "injected" not in event.data["diagnostic"]
    assert "new_top_level" not in event.data


def test_as_dict_returns_plain_json_types():
    event = end(3, "sections", section_types=["intro"], diagnostic={"failures": ["a"]})
    payload = event.as_dict()
    assert isinstance(payload["data"], dict)
    assert isinstance(payload["data"]["section_types"], list)
    assert isinstance(payload["data"]["diagnostic"], dict)
    assert isinstance(payload["data"]["diagnostic"]["failures"], list)


def test_as_dict_is_still_json_serialisable():
    event = error(6, "refused", first_failures=["a", "b"],
                  diagnostic={"nested": {"deep": [1, 2, 3]}}, failed_clips=3)
    decoded = json.loads(json.dumps(event.as_dict()))
    assert decoded["data"]["first_failures"] == ["a", "b"]
    assert decoded["data"]["diagnostic"]["nested"]["deep"] == [1, 2, 3]
    assert decoded["data"]["failed_clips"] == 3


def test_two_as_dict_calls_do_not_share_nested_containers():
    event = end(3, "sections", section_types=["intro"])
    first, second = event.as_dict(), event.as_dict()
    first["data"]["section_types"].append("X")
    assert second["data"]["section_types"] == ["intro"]


def test_replace_and_with_elapsed_still_work_with_frozen_data():
    event = progress(6, 1, 2, "x", first_failures=["a"], unit="clips")
    updated = dc_replace(event, current=2)
    assert updated.current == 2
    assert list(updated.data["first_failures"]) == ["a"]
    with pytest.raises((TypeError, AttributeError)):
        updated.data["first_failures"].append("INJECTED")

    timed = event.with_elapsed(3.0)
    assert timed.elapsed_seconds == 3.0
    assert list(timed.data["first_failures"]) == ["a"]


def test_frozen_data_survives_a_view_round_trip():
    """ProgressView reads data; it must not need to mutate it."""
    view = ProgressView()
    view.apply(end(3, "5 sections", section_types=["intro", "chorus"]))
    assert "5 sections" in view.render()


# ---------------------------------------------------------------------------
# B — Stage 5 rate must not bill cache hits as analysis throughput
# ---------------------------------------------------------------------------


def test_cache_hits_do_not_contribute_to_the_measured_rate():
    """420 instant cache hits + 1 slow analysis must not read as hundreds of sources/s."""
    counter = StageCounter(5, 758, min_interval=0.0)
    for _ in range(420):
        counter.advance(1, "cached", counts_toward_rate=False)

    assert counter.current == 420
    assert counter.rate is None, "cache hits alone must not produce a throughput figure"

    counter.begin_rate_window()
    time.sleep(0.3)
    event = counter.advance(1, "analyzed")

    assert event.current == 421, "the completion count still includes cache hits"
    assert event.total == 758
    assert event.rate is not None
    assert event.rate < 20, f"rate {event.rate} still looks like cache hits were billed"
    assert event.rate == pytest.approx(1 / 0.3, rel=0.7), event.rate


def test_rate_window_measures_only_post_window_completions():
    counter = StageCounter(5, 100, min_interval=0.0)
    for _ in range(50):
        counter.advance(1, "cached", counts_toward_rate=False)
    counter.begin_rate_window()
    time.sleep(0.2)
    for _ in range(2):
        counter.advance(1, "analyzed")

    # 2 analysed completions over ~0.2 s, not 52 over the whole run.
    assert counter.current == 52
    assert counter.rate is not None and counter.rate < 40, counter.rate


def test_stage6_rate_behaviour_is_unchanged():
    """No rate window is opened on the render path, so clip throughput is measured as before."""
    counter = StageCounter(6, 10, min_interval=0.0)
    time.sleep(0.2)
    event = counter.advance(1, "clips rendered")
    assert event.rate is not None
    assert event.rate == pytest.approx(1 / 0.2, rel=0.8), event.rate


def test_rate_is_none_before_any_rate_bearing_completion():
    counter = StageCounter(5, 5, min_interval=0.0)
    assert counter.rate is None
    counter.advance(1, "cached", counts_toward_rate=False)
    assert counter.rate is None


def test_begin_rate_window_resets_the_basis_not_the_count():
    counter = StageCounter(5, 10, min_interval=0.0, initial=4)
    assert counter.current == 4
    counter.begin_rate_window()
    assert counter.current == 4, "the visible completion count must not be reset"
    assert counter.rate is None


# ---------------------------------------------------------------------------
# B — what the panel actually displays
# ---------------------------------------------------------------------------


def _stage5_stream(cache_hits: int, analyzed: int, total: int, analysis_rate: float):
    events = [start(5, f"Analyzing {total} source video(s)", current=0, total=total,
                    unit="sources")]
    for i in range(1, cache_hits + 1):
        events.append(progress(5, i, total, "cached", cache_hits=cache_hits, unit="sources"))
    events.append(metric(5, f"{cache_hits} cached, {total - cache_hits} to analyze, 14 worker(s)",
                         cache_hits=cache_hits, workers=14))
    for i in range(1, analyzed + 1):
        events.append(progress(5, cache_hits + i, total, "analyzed", cache_hits=cache_hits,
                               unit="sources", rate=analysis_rate, rate_unit="analyzed sources",
                               elapsed_seconds=10.0))
    return events


def test_panel_does_not_claim_42_sources_per_second():
    """The reported red case: 758 total, 420 cached, 1 analysed in 10 s."""
    view = ProgressView()
    for event in _stage5_stream(cache_hits=420, analyzed=1, total=758, analysis_rate=0.1):
        view.apply(event)

    line = view.stage_line(5)
    assert "421 / 758" in line, line
    assert "42.1 sources/s" not in line, line
    assert "0.1 analyzed sources/s" in line, line
    assert "420 cached" in view.render(), "cache hits must stay visible"


def test_panel_shows_no_rate_while_only_cache_hits_have_arrived():
    view = ProgressView()
    for event in _stage5_stream(cache_hits=420, analyzed=0, total=758, analysis_rate=0.0):
        view.apply(event)
    line = view.stage_line(5)
    assert "420 / 758" in line
    assert "/s" not in line, f"no throughput should be claimed yet: {line}"


def test_a_stale_straggler_does_not_wipe_the_measured_rate():
    """Regression: monotonicity protected `current` but not `rate`/`elapsed`.

    Stage 6 collects clips out of order via `as_completed`, and a late event carries no rate. Accepting
    its None wiped a good measurement, so `612 / 1216 · 4.8 clips/s` silently degraded to
    `612 / 1216`. A straggler ignored for the count must be ignored for the rate too.
    """
    view = ProgressView()
    view.apply(start(6, "", current=0, total=1216, unit="clips"))
    view.apply(progress(6, 612, 1216, "clips rendered", rate=4.83, elapsed_seconds=127.0,
                        unit="clips"))
    view.apply(progress(6, 300, 1216, "clips rendered", unit="clips"))   # late, no rate

    line = view.stage_line(6)
    assert "612 / 1216" in line, line
    assert "4.8 clips/s" in line, f"the straggler wiped the rate: {line}"
    assert "elapsed 2m 07s" in line, f"the straggler wiped elapsed: {line}"


def test_a_newer_event_without_a_rate_keeps_the_last_known_rate():
    """Never overwrite a measurement with None; that makes the figure flicker."""
    view = ProgressView()
    view.apply(start(6, "", current=0, total=10, unit="clips"))
    view.apply(progress(6, 5, 10, "clips rendered", rate=2.0, unit="clips"))
    view.apply(progress(6, 6, 10, "clips rendered", unit="clips"))
    line = view.stage_line(6)
    assert "6 / 10" in line
    assert "2.0 clips/s" in line, line


def test_a_newer_event_does_update_the_rate():
    view = ProgressView()
    view.apply(start(6, "", current=0, total=10, unit="clips"))
    view.apply(progress(6, 5, 10, "clips rendered", rate=2.0, unit="clips"))
    view.apply(progress(6, 8, 10, "clips rendered", rate=9.0, unit="clips"))
    assert "9.0 clips/s" in view.stage_line(6)


def test_panel_still_shows_stage6_clip_rate_normally():
    view = ProgressView()
    view.apply(start(6, "Rendering 1216 frame-locked cuts", current=0, total=1216, unit="clips"))
    view.apply(progress(6, 612, 1216, "clips rendered", rate=4.83, elapsed_seconds=127.0,
                        unit="clips"))
    line = view.stage_line(6)
    assert "612 / 1216 (50.3%)" in line
    assert "4.8 clips/s" in line, line


def test_rate_unit_defaults_to_the_counting_unit():
    view = ProgressView()
    view.apply(start(6, "", current=0, total=10, unit="clips"))
    view.apply(progress(6, 5, 10, "clips rendered", rate=2.0, unit="clips"))
    assert "2.0 clips/s" in view.stage_line(6)


# ---------------------------------------------------------------------------
# B — the pipeline must actually use the new semantics
# ---------------------------------------------------------------------------


def _video_analysis_source() -> str:
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "video_analysis.py",
    )
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_video_analysis_excludes_cache_hits_from_the_rate():
    source = _video_analysis_source()
    assert "counts_toward_rate=False" in source, (
        "the cache-hit advance must opt out of the rate basis"
    )


def test_video_analysis_opens_a_rate_window_before_the_analysis_pass():
    source = _video_analysis_source()
    assert "begin_rate_window()" in source, (
        "the rate clock must be re-based after the cache scan"
    )


def test_video_analysis_labels_the_rate_unit():
    source = _video_analysis_source()
    assert "analyzed sources" in source, (
        "the displayed rate should say what it measures"
    )
