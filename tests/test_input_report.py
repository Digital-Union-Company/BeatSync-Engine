"""Report formatting and serialisation."""

from __future__ import annotations

import os

from conftest import write_file

from beatsync_fork.input_manager import scan_folder
from beatsync_fork.input_report import InputReport, format_bytes, render_input_set


def test_format_bytes_scales():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1024**3) == "1.0 GB"
    assert format_bytes(443 * 1024**3) == "443.0 GB"


def test_format_bytes_handles_negative():
    assert format_bytes(-5) == "0 B"


def test_report_round_trips_scan_counts(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mp4"), b"1234")
    write_file(os.path.join(root, "b.mkv"), b"12345")
    write_file(os.path.join(root, "empty.mp4"), b"")
    write_file(os.path.join(root, "notes.txt"), b"x")

    result = scan_folder(root)
    report = InputReport.from_input_set(result)

    assert report.discovered == 4
    assert report.supported == 3
    assert report.ready == 2
    assert report.rejected == 2
    assert report.total_ready_bytes == 9
    assert report.rejected_by_reason == {"unsupported_extension": 1, "empty_file": 1}
    assert report.is_ready is True
    assert report.root == result.root


def test_as_dict_is_json_serialisable(tmp_path):
    import json

    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mp4"), b"x")

    payload = InputReport.from_input_set(scan_folder(root)).as_dict()
    decoded = json.loads(json.dumps(payload))

    assert decoded["ready"] == 1
    assert decoded["is_ready"] is True
    assert "scan_seconds" in decoded


def test_zero_counts_are_omitted_from_reason_map(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mp4"), b"x")

    report = InputReport.from_input_set(scan_folder(root))

    assert report.rejected_by_reason == {}
    assert "(" not in report.render_text().split("Rejected:")[1].splitlines()[0]


def test_render_text_reports_not_ready_for_empty_folder(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root, exist_ok=True)

    rendered = render_input_set(scan_folder(root))

    assert "NO USABLE SOURCE FILES" in rendered
    assert "INPUT READY" not in rendered


def test_render_text_shows_duplicates_and_scope(tmp_path):
    root = str(tmp_path / "src")
    same = b"identical"
    write_file(os.path.join(root, "a.mp4"), same)
    write_file(os.path.join(root, "b.mp4"), same)

    rendered = render_input_set(scan_folder(root, recursive=False))

    assert "top level only" in rendered
    assert "1 group(s)" in rendered
    assert "reported, not removed" in rendered
    assert "INPUT READY" in rendered


def test_render_text_states_when_duplicates_were_skipped(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mp4"), b"x")

    rendered = render_input_set(scan_folder(root, detect_duplicates=False))

    assert "Duplicates:  not checked" in rendered
