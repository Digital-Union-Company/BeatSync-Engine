"""Phase 3B: a failed Stage 6 clip must carry the reason FFmpeg already printed.

The regression these tests exist for is expensive and real. A 701-source render lost every clip to an
NVENC driver/API mismatch; FFmpeg said exactly what was wrong, ``extract_clip_segment_ffmpeg`` printed
it and returned ``False``, and the GUI — which redirects stdout into ``QuietConsole`` — showed
``283 clip(s) failed``. Diagnosing it took a full forensic task.

The fixture below is the genuine stderr captured during that incident, trimmed to the lines that
matter. Two properties are load-bearing:

* the summary must contain the **driver/API mismatch**, not the ``Error while opening encoder`` that
  FFmpeg printed three lines later as a consequence;
* it must not report the ``cuvidCreateDecoder`` hwaccel warning printed four lines *earlier*, which
  FFmpeg recovered from by falling back to software decode. An "earliest diagnostic line wins" rule
  picks that one — it was the first implementation here and this fixture rejected it.
"""

from __future__ import annotations

import pytest

from beatsync_fork import ffmpeg_diagnostics as fd


# Real capture: nvenc-forensics/logs/case_a_stderr.txt, driver 595.79 + FFmpeg 8.1.2.
REAL_PRE_DRIVER_STDERR = """ffmpeg version 8.1.2-essentials_build-www.gyan.dev Copyright (c) 2000-2026
  built with gcc 16.1.0 (Rev2, Built by MSYS2 project)
  configuration: --enable-gpl --enable-version3 --enable-nvenc --enable-nvdec
  libavutil      60. 26.102 / 60. 26.102
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:/videos/726.mp4':
  Metadata:
    major_brand     : mp42
    creation_time   : 2026-09-26T13:19:49.000000Z
  Duration: 00:00:48.80, start: 0.000000, bitrate: 7725 kb/s
  Stream #0:0[0x1](eng): Video: h264 (Main) (avc1 / 0x31637661), yuv420p(progressive), 1280x720
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> h264 (h264_nvenc))
Press [q] to stop, [?] for help
[h264 @ 000001c31c1824c0] decoder->cvdl->cuvidCreateDecoder(&decoder->decoder, params) failed -> \
CUDA_ERROR_INVALID_VALUE: invalid argument
[h264 @ 000001c31c1824c0] Using more than 32 (33) decode surfaces might cause nvdec to fail.
[h264 @ 000001c31c1824c0] Try lowering the amount of threads. Using 16 right now.
[h264 @ 000001c31c1824c0] Failed setup for format cuda: hwaccel initialisation returned error.
[h264_nvenc @ 000001c31bb96680] Driver does not support the required nvenc API version. \
Required: 13.1 Found: 13.0
[h264_nvenc @ 000001c31bb96680] The minimum required Nvidia driver for nvenc is 610.00 or newer
[vost#0:0/h264_nvenc @ 000001c3198dedc0] [enc:h264_nvenc @ 000001c31bb77d40] Error while opening \
encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
[vf#0:0 @ 000001c31bb96a00] Error sending frames to consumers: Function not implemented
[vf#0:0 @ 000001c31bb96a00] Task finished with error code: -40 (Function not implemented)
[vost#0:0/h264_nvenc @ 000001c3198dedc0] Could not open encoder before EOF
[out#0/mp4 @ 000001c319890640] Nothing was written into output file, because at least one of its \
streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=N/A bitrate=N/A speed=N/A elapsed=0:00:00.03
Conversion failed!
"""

# The known non-fatal case on driver 617.14: nvdec init fails, FFmpeg falls back, the clip is valid.
NON_FATAL_STDERR = """[h264 @ 0000024d6f4ad9c0] decoder->cvdl->cuvidCreateDecoder(&decoder->decoder, \
params) failed -> CUDA_ERROR_INVALID_VALUE: invalid argument
[h264 @ 0000024d6f4ad9c0] Using more than 32 (33) decode surfaces might cause nvdec to fail.
[h264 @ 0000024d6f4ad9c0] Failed setup for format cuda: hwaccel initialisation returned error.
frame=   60 fps=0.0 q=25.0 Lsize=    3104KiB time=00:00:02.00 bitrate=12713.7kbits/s speed=4.1x
"""


# ---------------------------------------------------------------------------
# the incident fixture
# ---------------------------------------------------------------------------


def test_the_real_incident_reports_the_driver_api_mismatch():
    summary = fd.summarize_ffmpeg_failure(REAL_PRE_DRIVER_STDERR)

    assert "Driver does not support the required nvenc API version" in summary
    assert "Required: 13.1 Found: 13.0" in summary


def test_the_real_incident_does_not_collapse_to_the_generic_consequence():
    """``Error while opening encoder`` is what FFmpeg says *because* of the mismatch, not why."""
    summary = fd.summarize_ffmpeg_failure(REAL_PRE_DRIVER_STDERR)

    assert "Error while opening encoder" not in summary
    assert "Conversion failed" not in summary
    assert "Task finished with error code" not in summary


def test_the_real_incident_does_not_report_the_recovered_hwaccel_warning():
    """The nvdec failure four lines earlier was recovered from; reporting it would mislead.

    This is the assertion that rejected the first implementation of the selector.
    """
    summary = fd.summarize_ffmpeg_failure(REAL_PRE_DRIVER_STDERR)

    assert "cuvidCreateDecoder" not in summary
    assert "decode surfaces" not in summary


def test_the_real_incident_summary_is_bounded_and_single_line():
    summary = fd.summarize_ffmpeg_failure(REAL_PRE_DRIVER_STDERR)

    assert 0 < len(summary) <= fd.MAX_REASON_CHARS
    assert "\n" not in summary and "\r" not in summary


def test_heap_addresses_are_stripped_so_the_reason_is_stable():
    """Two runs of the same failure must produce the same string, or it cannot be compared."""
    summary = fd.summarize_ffmpeg_failure(REAL_PRE_DRIVER_STDERR)

    assert "[h264_nvenc]" in summary
    assert "000001c31bb96680" not in summary
    other_run = REAL_PRE_DRIVER_STDERR.replace("000001c31bb96680", "00007ffdeadbeef0")
    assert fd.summarize_ffmpeg_failure(other_run) == summary


# ---------------------------------------------------------------------------
# generic, not NVIDIA-special-cased
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stderr, expected", [
    ("[in#0 @ 0x1] Error opening input: No such file or directory\nConversion failed!\n",
     "No such file or directory"),
    ("[h264_nvenc @ 0x1] Cannot load nvcuda.dll\n"
     "[vost#0:0] Error while opening encoder\nConversion failed!\n",
     "Cannot load nvcuda.dll"),
    ("[vf#0:0 @ 0x1] Impossible to convert between the formats\n"
     "[vf#0:0 @ 0x1] Task finished with error code: -38\n",
     "Impossible to convert between the formats"),
    ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory (10)\n"
     "[vost#0:0] Error while opening encoder\n",
     "out of memory"),
    ("[mp4 @ 0x1] Permission denied\nConversion failed!\n", "Permission denied"),
    ("[h264 @ 0x1] Invalid data found when processing input\n", "Invalid data found"),
])
def test_other_failure_families_are_summarised_without_special_casing(stderr, expected):
    """Nothing in the module mentions NVIDIA; these all work through the same generic ranking."""
    assert expected in fd.summarize_ffmpeg_failure(stderr)


def test_the_ranking_data_contains_no_vendor_special_case():
    """A guard against "fix the incident" drift.

    Asserted against the classification tuples themselves rather than the source text, because the
    docstrings legitimately quote the real NVENC capture as the worked example. What must stay
    vendor-neutral is the data that decides which line wins.
    """
    vendor_tokens = ("nvenc", "nvidia", "cuda", "nvcuda", "13.1", "610.00", "geforce", "vulkan")
    marker_groups = {
        "_NOISE_PREFIXES": fd._NOISE_PREFIXES,
        "_NOISE_CONTAINS": fd._NOISE_CONTAINS,
        "_GENERIC_MARKERS": fd._GENERIC_MARKERS,
        "_SPECIFIC_MARKERS": fd._SPECIFIC_MARKERS,
    }
    for name, markers in marker_groups.items():
        for marker in markers:
            for token in vendor_tokens:
                assert token not in marker.lower(), f"{name} mentions {token!r}: {marker!r}"


# ---------------------------------------------------------------------------
# boundedness / hostile input
# ---------------------------------------------------------------------------


def test_megabytes_of_stderr_produce_a_bounded_reason():
    huge = (
        "ffmpeg version 8.1.2-essentials_build\n"
        + "  configuration: " + ("--enable-x " * 5000) + "\n"
        + "".join(f"[noise @ {i:08x}] ggml_vulkan spew line {i} padding padding padding\n"
                 for i in range(20000))
        + "[h264_nvenc @ 0xabc] Cannot load nvcuda.dll\n"
        + "[vost#0:0] Error while opening encoder\nConversion failed!\n"
    )
    assert len(huge) > 1_000_000

    summary = fd.summarize_ffmpeg_failure(huge)

    assert len(summary) <= fd.MAX_REASON_CHARS
    assert "Cannot load nvcuda.dll" in summary, "the real cause must survive the noise"


def test_one_absurdly_long_line_is_truncated_with_an_ellipsis():
    stderr = "[h264_nvenc @ 0x1] Cannot allocate " + ("x" * 10000) + " frames\n"

    summary = fd.summarize_ffmpeg_failure(stderr)

    assert len(summary) == fd.MAX_REASON_CHARS
    assert summary.endswith("…")


def test_unicode_and_control_characters_stay_ui_safe():
    stderr = (
        "[h264_nvenc @ 0x1] Cannot open device \u2014 \u00fcnic\u00f6de \u2713\r\n"
        "\x00\x07[vost#0:0] Error while opening encoder\n"
    )

    summary = fd.summarize_ffmpeg_failure(stderr)

    assert "\u00fcnic\u00f6de" in summary
    assert "\r" not in summary and "\n" not in summary
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in summary)


@pytest.mark.parametrize("stderr", ["", None, "\n\n\n", "   ", "frame=  10 fps=0.0 q=25.0\n"])
def test_useless_stderr_yields_an_empty_reason_not_an_exception(stderr):
    """Empty is honest. The caller substitutes the exit code rather than inventing a cause."""
    assert fd.summarize_ffmpeg_failure(stderr) == ""


def test_banner_only_stderr_is_not_reported_as_a_cause():
    banner = (
        "ffmpeg version 8.1.2-essentials_build-www.gyan.dev\n"
        "  built with gcc 16.1.0\n"
        "  configuration: --enable-gpl\n"
        "  libavutil      60. 26.102 / 60. 26.102\n"
    )
    assert fd.summarize_ffmpeg_failure(banner) == ""


def test_bound_never_exceeds_its_limit():
    for limit in (1, 2, 10, 80, 240, 1000):
        result = fd.bound("x" * 5000, limit)
        assert len(result) <= limit, limit


def test_max_lines_is_respected():
    stderr = "\n".join(f"[enc @ 0x1] Cannot do thing {i}" for i in range(10)) + \
             "\n[vost#0:0] Error while opening encoder\n"

    one = fd.summarize_ffmpeg_failure(stderr, max_lines=1)
    two = fd.summarize_ffmpeg_failure(stderr, max_lines=2)

    assert one.count("Cannot do thing") == 1
    assert two.count("Cannot do thing") == 2
    # Nearest the failure boundary wins, so the last emitted causes are the ones kept.
    assert "thing 9" in one


# ---------------------------------------------------------------------------
# the non-failure cases the summariser must not be used for
# ---------------------------------------------------------------------------


def test_missing_and_empty_output_have_distinct_honest_reasons():
    assert "missing" in fd.describe_output_problem(exists=False, size=0)
    assert "empty" in fd.describe_output_problem(exists=True, size=0)
    assert fd.describe_output_problem(exists=True, size=1024) == ""


def test_output_problem_reasons_do_not_invent_stderr():
    """There is usually no stderr for this case; fabricating one would be worse than saying so."""
    for reason in (fd.describe_output_problem(False, 0), fd.describe_output_problem(True, 0)):
        assert "FFmpeg reported success" in reason
        assert len(reason) <= fd.MAX_REASON_CHARS


def test_timeout_is_described_by_its_numbers_not_the_whole_argv():
    import subprocess

    exc = subprocess.TimeoutExpired(cmd=["ffmpeg", "-i", "x" * 4000, "out.mp4"], timeout=120)

    reason = fd.describe_exception(exc)

    assert reason == "FFmpeg timed out after 120s"
    assert "x" * 100 not in reason


def test_other_exceptions_keep_their_type_and_message_bounded():
    reason = fd.describe_exception(OSError("handle is invalid"))

    assert "OSError" in reason and "handle is invalid" in reason
    assert len(reason) <= fd.MAX_REASON_CHARS
    assert len(fd.describe_exception(RuntimeError("y" * 5000))) <= fd.MAX_REASON_CHARS


def test_exception_without_a_message_still_names_its_type():
    assert fd.describe_exception(KeyboardInterrupt()) == "KeyboardInterrupt"


# ---------------------------------------------------------------------------
# a successful run must never be turned into a failure by its stderr
# ---------------------------------------------------------------------------


def test_the_non_fatal_hwaccel_warning_is_never_consulted_on_success():
    """rc=0 means success. This stderr exists on every successful clip on driver 617.14.

    The summariser would happily describe it as a failure — which is exactly why the caller must only
    invoke it when ``returncode != 0``. Asserted at the seam in ``test_stage6_diagnostic_seam.py``.
    """
    would_have_said = fd.summarize_ffmpeg_failure(NON_FATAL_STDERR)

    assert would_have_said, "sanity: this text does look like a failure in isolation"
    assert "cuvidCreateDecoder" in would_have_said
