"""The portable installer's pinned-tool contract, read off the real ``scripts/install.ps1``.

This is the repository's first dedicated installer test, and it exists because two installer
defects were invisible to every other kind of check we have:

  H3  ``llama-cli.exe --version`` writes its version to **stderr** and leaves stdout empty. The
      old probe was ``& $CliExe --version 2>$null``, which discarded exactly the text it needed --
      and under the Windows PowerShell 5.1 that ``install.bat`` actually launches, with
      ``$ErrorActionPreference = "Stop"``, a native command whose stderr is redirected raises a
      ``NativeCommandError``, so the probe did not merely come back empty, it *threw*. A valid
      pinned build was therefore re-downloaded and replaced on every single run.

  H4  UV came from a floating ``/releases/latest/`` URL, and then ``Cleanup-InstallerFiles``
      deleted the whole retained ``bin\\uv`` directory on every successful run, so the next run had
      to fetch UV again -- a different, unaudited UV each time upstream published a release.

Neither defect is reachable from Python at runtime, so nothing in the suite could see them. These
tests read the installer **as source** and assert the contract instead.

Deliberate constraints, matching the rest of the harness (see ``.claude/rules/test-harness.md``):
this module runs on a bare CPython on any platform. It never launches PowerShell, never touches the
network, and needs no portable runtime, FFmpeg, llama.cpp, UV or model assets. It also avoids
absolute line numbers -- assertions go through the small structural helpers below, so ordinary
edits to the installer do not produce phantom failures.
"""

from __future__ import annotations

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INSTALLER = os.path.join(_REPO_ROOT, "scripts", "install.ps1")


def _source() -> str:
    with open(_INSTALLER, encoding="utf-8") as handle:
        return handle.read()


SOURCE = _source()


# --------------------------------------------------------------------------------------
# Structural helpers
#
# A PowerShell parser is out of scope and would be its own liability. These three helpers are
# enough for a pin/flow contract: top-level scalar assignments, and brace-matched function bodies.
# --------------------------------------------------------------------------------------


def assignment(name: str) -> str:
    """Return the right-hand side of the last top-level ``$Name = ...`` assignment."""
    pattern = re.compile(r"^\$" + re.escape(name) + r"\s*=\s*(.+?)\s*$", re.MULTILINE)
    matches = pattern.findall(SOURCE)
    if not matches:
        raise AssertionError(f"installer has no top-level assignment for ${name}")
    return matches[-1]


def string_assignment(name: str) -> str:
    """Return a ``$Name = "..."`` value with the surrounding quotes stripped."""
    raw = assignment(name)
    match = re.fullmatch(r'"(.*)"', raw) or re.fullmatch(r"'(.*)'", raw)
    if not match:
        raise AssertionError(f"${name} is not a simple quoted string: {raw!r}")
    return match.group(1)


def int_assignment(name: str) -> int:
    raw = assignment(name)
    if not re.fullmatch(r"\d+", raw):
        raise AssertionError(f"${name} is not a bare integer literal: {raw!r}")
    return int(raw)


def function_body(name: str) -> str:
    """Return the brace-matched body of ``function Name { ... }``.

    Brace counting is naive about braces inside strings; the installer has none inside the
    functions this module inspects, and a mismatch would surface as an obviously wrong body rather
    than a silent pass.
    """
    header = re.search(r"^function\s+" + re.escape(name) + r"\b[^\n{]*\{", SOURCE, re.MULTILINE)
    if not header:
        raise AssertionError(f"installer has no function named {name}")
    depth = 0
    start = header.end() - 1
    for index in range(start, len(SOURCE)):
        char = SOURCE[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start + 1:index]
    raise AssertionError(f"unbalanced braces in function {name}")


def main_flow() -> str:
    """Everything after the last function definition: the installer's top-level script body."""
    last = None
    for match in re.finditer(r"^function\s+([\w-]+)", SOURCE, re.MULTILINE):
        last = match
    assert last is not None, "installer defines no functions at all"
    body = function_body(last.group(1))
    return SOURCE[SOURCE.index(body) + len(body):]


# --------------------------------------------------------------------------------------
# 1. The helpers themselves
#
# If a helper silently stops finding anything, every assertion built on it passes vacuously. These
# four tests are the floor under the rest of the module.
# --------------------------------------------------------------------------------------


def test_installer_source_is_present_and_substantial():
    assert os.path.isfile(_INSTALLER), _INSTALLER
    assert len(SOURCE) > 5000
    assert '$ErrorActionPreference = "Stop"' in SOURCE


def test_assignment_helper_finds_a_known_pin():
    assert string_assignment("PythonVersion") == "3.13.14"


def test_function_body_helper_returns_a_plausible_body():
    body = function_body("Test-FileSha256")
    assert "Get-FileHash" in body
    assert "function" not in body


def test_function_body_helper_rejects_an_unknown_function():
    with pytest.raises(AssertionError):
        function_body("No-SuchFunctionExists")


# --------------------------------------------------------------------------------------
# 2. H3 -- llama.cpp pin and reuse
# --------------------------------------------------------------------------------------


def test_llama_build_stays_pinned():
    assert string_assignment("LlamaBuild") == "b9842"


def test_llama_archive_url_derives_from_the_pin():
    url = string_assignment("LlamaZipUrl")
    assert "$LlamaBuild" in url, "the llama archive URL must derive from the pin, not restate it"
    assert "9842" not in url.replace("$LlamaBuild", ""), "no second hard-coded build number in the URL"
    assert url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/")


def test_llama_readiness_still_requires_all_four_executables():
    body = function_body("Install-LlamaCppVulkan")
    for exe in (
        "llama-server.exe",
        "llama-mtmd-cli.exe",
        "llama-cli.exe",
        "llama-completion.exe",
    ):
        assert exe in body, f"{exe} is no longer part of the readiness check"
    # All four gate the reuse decision, not just the first one that happens to exist.
    guard = re.search(r"if\s*\(\(Test-Path[^\n]*\n?[^\n]*\)\s*\{", body)
    assert guard is not None, "the four-executable readiness guard is gone"
    assert guard.group(0).count("Test-Path") == 4


def test_llama_version_probe_does_not_discard_stderr():
    """The whole of H3. ``2>$null`` on the version probe is the defect, not a style choice."""
    body = function_body("Install-LlamaCppVulkan")
    probe_lines = [line for line in body.splitlines() if "--version" in line]
    assert probe_lines, "the version probe is gone -- 'file exists' is not a sufficient check"
    for line in probe_lines:
        assert "2>$null" not in line, (
            "the llama version probe must not discard stderr: llama-cli prints its version there"
        )
        assert "2>&1" not in line, (
            "merging stderr into the success stream throws NativeCommandError under Windows "
            "PowerShell 5.1 with ErrorActionPreference=Stop -- capture the streams instead"
        )


def test_llama_version_probe_considers_combined_output_and_exit_code():
    body = function_body("Install-LlamaCppVulkan")
    assert "Invoke-NativeProbe" in body, "the probe must go through the explicit native-probe helper"
    assert ".Combined" in body, "the reuse decision must read the combined stdout+stderr text"
    assert ".ExitCode" in body, "the reuse decision must check the native exit status"


def test_expected_llama_version_is_derived_from_the_pin():
    """The matched build number comes from ``$LlamaBuild``, not from a literal in the probe."""
    body = function_body("Install-LlamaCppVulkan")
    assert not re.search(r"version:\\s\+9842", body), (
        "the probe must not restate the build number; derive it from the pin"
    )
    assert "$LlamaVersionPattern" in body, "the probe must use the derived pattern"

    assert re.search(r"\[regex\]::Match\(\$LlamaBuild, '\^b\(\\d\+\)\$'\)", SOURCE), (
        "the build number must be parsed out of $LlamaBuild"
    )
    assert 'throw "Unexpected llama.cpp build pin' in SOURCE, (
        "an unexpected pin shape must fail loudly rather than silently match nothing"
    )
    pattern = assignment("LlamaVersionPattern")
    assert "$LlamaBuild" in pattern or "LlamaBuildMatch" in pattern, pattern


def test_the_recorded_llama_pattern_is_a_tripwire_not_a_silent_duplicate():
    r"""``version:\s+9842`` still appears once, deliberately, and cannot drift silently.

    ``tests/test_gui_guard_seam.py`` pins that contract text, so it stays -- but as a recorded
    expectation that is *compared* against the derived pattern. Moving ``$LlamaBuild`` without
    updating it throws on the next installer run, which is the opposite of the original defect
    (a stale literal quietly matching nothing and replacing a good install).
    """
    assert string_assignment("LlamaVersionRecorded") == r"version:\s+9842"
    assert "if ($LlamaVersionPattern -ne $LlamaVersionRecorded) {" in SOURCE, (
        "the derived pattern and the recorded pattern must be checked against each other"
    )
    guard = SOURCE[SOURCE.index("if ($LlamaVersionPattern -ne $LlamaVersionRecorded)"):]
    assert "throw" in guard[:400], "a disagreement must throw, not warn"
    # Exactly one recorded literal, and it is the tripwire -- not a second one in the probe.
    assert len(re.findall(r"version:\\s\+9842", SOURCE)) == 1


def test_llama_version_mismatch_still_reaches_replacement():
    body = function_body("Install-LlamaCppVulkan")
    # Reuse is the only early exit, so every other outcome -- wrong build, failed probe, missing
    # executable -- falls through to download + replace. Counted as statements, not substrings.
    returns = [line.strip() for line in body.splitlines() if re.match(r"^\s*return\b", line)]
    assert returns == ["return"], f"reuse must be the single early exit; found {returns}"
    assert "Download-File $LlamaZipUrl" in body
    assert re.search(r"is not \$LlamaBuild", body), "the mismatch message/branch is gone"


def test_llama_build_is_not_silently_upgraded():
    assert "b9842" in SOURCE
    for other in ("b9843", "b9900", "b10000"):
        assert other not in SOURCE


# --------------------------------------------------------------------------------------
# 3. The native probe helper
# --------------------------------------------------------------------------------------


def test_native_probe_helper_exists_and_captures_both_streams():
    body = function_body("Invoke-NativeProbe")
    assert "RedirectStandardOutput" in body
    assert "RedirectStandardError" in body
    assert "ExitCode" in body
    assert "Combined" in body


def test_native_probe_helper_survives_a_missing_executable():
    """``Start-Process`` throws on a missing image; a probe that cannot run means "replace"."""
    body = function_body("Invoke-NativeProbe")
    assert "catch" in body, "a failed launch must not propagate out of the probe"
    assert re.search(r"ExitCode\s*=\s*-1", body), (
        "a failed launch must report a non-zero exit code so callers treat it as replace"
    )


def test_native_probe_helper_cleans_up_its_temp_files_outside_the_repo():
    body = function_body("Invoke-NativeProbe")
    assert "GetTempFileName" in body, "probe scratch belongs outside the repository"
    assert "finally" in body
    assert "Remove-Item" in body


# --------------------------------------------------------------------------------------
# 4. H4 -- UV pin, verification and reuse
# --------------------------------------------------------------------------------------

UV_VERSION = "0.13.0"
UV_ARCHIVE_BYTES = 15722003
UV_ARCHIVE_SHA256 = "088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c"


def test_uv_version_is_pinned():
    assert string_assignment("UvVersion") == UV_VERSION


def test_uv_url_uses_the_pinned_release_and_not_latest():
    url = string_assignment("UvZipUrl")
    assert "/latest/" not in url, "a floating 'latest' URL is the H4 defect"
    assert "$UvVersion" in url, "the UV URL must derive from the pin"
    assert url == (
        "https://github.com/astral-sh/uv/releases/download/$UvVersion/"
        "uv-x86_64-pc-windows-msvc.zip"
    )


def test_uv_archive_integrity_is_pinned():
    assert int_assignment("UvArchiveExpectedBytes") == UV_ARCHIVE_BYTES
    assert string_assignment("UvArchiveSha256").lower() == UV_ARCHIVE_SHA256


def test_uv_install_validates_an_existing_executable_by_version():
    body = function_body("Install-Uv")
    assert "Invoke-NativeProbe" in body, "a retained uv.exe must be proven, not merely present"
    assert ".ExitCode" in body
    assert "$UvVersion" in body
    assert re.search(r'uv\\s\+\$\(\[regex\]::Escape\(\$UvVersion\)\)', body) or re.search(
        r'uv\\s\+\$UvVersion', body
    ), "the reuse test must compare against the pinned UV version"


def test_pinned_version_matches_cannot_be_satisfied_by_a_longer_version():
    r"""A trailing ``\b`` is not enough, and this was caught by measurement, not by review.

    ``\b`` matches between ``0`` and ``-``, so a pin of ``0.13.0`` was satisfied by a probe
    reporting ``uv 0.13.0-rc1``. Every pinned-version comparison must therefore end with
    ``$VersionTokenEnd``, and none may fall back to a bare word boundary.
    """
    token_end = assignment("VersionTokenEnd")
    assert token_end in ("'(?![\\w.\\-])'", '"(?![\\w.\\-])"'), token_end

    comparisons = [
        line for line in SOURCE.splitlines()
        if re.search(r'-(not)?match\s+"(uv\\s|\$LlamaVersionPattern)', line)
    ]
    assert len(comparisons) == 3, (
        f"expected exactly the UV reuse, UV verify and llama reuse checks: {comparisons}"
    )
    for line in comparisons:
        assert "$VersionTokenEnd" in line, f"pinned-version match without the token-end guard: {line.strip()}"
        assert not re.search(r'\\b"', line), f"bare word-boundary version match: {line.strip()}"


def test_version_token_end_regex_actually_rejects_near_misses():
    """Assert the published regex's behaviour, not just its presence."""
    token_end = assignment("VersionTokenEnd").strip("'\"")
    pattern = re.compile(r"uv\s+" + re.escape("0.13.0") + token_end)
    assert pattern.search("uv 0.13.0 (4ecd0eff2 2026-10-09 x86_64-pc-windows-msvc)")
    assert pattern.search("uv 0.13.0")
    for near_miss in ("uv 0.13.0-rc1", "uv 0.13.00", "uv 0.13.01", "uv 0.13.0.1", "uv 1.0.13.0"):
        assert not pattern.search(near_miss), near_miss

    llama = re.compile(r"version:\s+" + "9842" + token_end)
    assert llama.search("version: 9842 (6f4f53f2b)")
    for near_miss in ("version: 98420", "version: 9842-dirty", "version: 9842.1"):
        assert not llama.search(near_miss), near_miss


def test_uv_install_reuses_only_a_correct_version():
    body = function_body("Install-Uv")
    reuse = re.search(r"if\s*\(\(\$Probe\.ExitCode -eq 0\).*?\{(.*?)\n\s{8}\}", body, re.DOTALL)
    assert reuse is not None, "the reuse branch must be gated on exit code AND version"
    assert "return $UvExe" in reuse.group(1)


def test_uv_install_replaces_a_wrong_or_unprovable_executable():
    body = function_body("Install-Uv")
    assert "Assert-InDirectory $UvDir $BinDir" in body, (
        "removing the UV folder must keep the existing containment guard"
    )
    assert "Remove-Item -LiteralPath $UvDir -Recurse -Force" in body
    # ... and then go on to install the pinned archive rather than keeping what was there.
    assert body.index("Remove-Item -LiteralPath $UvDir") < body.index("Install-VerifiedModel")


def test_uv_archive_is_verified_before_extraction():
    body = function_body("Install-Uv")
    assert "Install-VerifiedModel $UvZipUrl" in body, (
        "the UV archive must go through the exact-size+SHA256 path, not the size-floor downloader"
    )
    assert "$UvArchiveExpectedBytes" in body
    assert "$UvArchiveSha256" in body
    assert body.index("Install-VerifiedModel") < body.index("Expand-Zip"), (
        "an unverified archive must never be extracted"
    )


def test_uv_archive_verification_reuses_the_existing_hash_helper():
    body = function_body("Install-VerifiedModel")
    assert "Test-FileSha256" in body
    assert "Download-File" in body, "no second download implementation"


def test_verified_file_helper_still_enforces_exact_size_and_hash():
    body = function_body("Install-VerifiedModel")
    assert "$ExpectedBytes" in body
    assert "-ne $ExpectedBytes" in body
    assert "failed SHA256 verification" in body
    # A failed verification removes the bad file so a later Test-RequiredFile cannot pass it off.
    assert body.count("Remove-Item") >= 3


# --------------------------------------------------------------------------------------
# 5. H4 -- retention across cleanup
# --------------------------------------------------------------------------------------


def test_cleanup_still_removes_the_transient_download_cache():
    body = function_body("Cleanup-InstallerFiles")
    assert "$DownloadsDir" in body
    assert "Remove-InstallerFolder $DownloadsDir" in body


def test_cleanup_no_longer_deletes_the_retained_uv_directory():
    """The whole of H4: deleting ``bin\\uv`` on success forced a fresh UV download every run."""
    body = function_body("Cleanup-InstallerFiles")
    assert "$UvDir" not in body, (
        "successful cleanup must retain bin\\uv\\uv.exe so the next run can validate and reuse it"
    )
    assert "Remove-InstallerFolder $UvDir" not in SOURCE


def test_no_zip_archive_is_retained_for_idempotence():
    """The durable artifact is the verified executable, not a cached archive."""
    cleanup = function_body("Cleanup-InstallerFiles")
    # $DownloadsDir holds every archive the installer fetches, and it is still wiped wholesale,
    # so idempotence can never come to depend on a retained ZIP.
    assert "Remove-InstallerFolder $DownloadsDir" in cleanup
    assert '$DownloadsDir = Join-Path $BinDir "downloads"' in SOURCE
    assert ".zip" not in cleanup


def test_retained_uv_is_proven_before_the_installer_reports_success():
    flow = main_flow()
    assert "Invoke-NativeProbe $UvExe" in flow, (
        "a retained UV executable must be re-proven in the final verification"
    )
    assert "$UvVersion" in flow
    assert flow.index("Cleanup-InstallerFiles") < flow.index("Invoke-NativeProbe $UvExe"), (
        "verify UV *after* cleanup, so retention itself is what gets proven"
    )
    assert flow.index("Invoke-NativeProbe $UvExe") < flow.index("Portable install is ready")


def test_cleanup_wording_does_not_claim_all_tooling_is_removed():
    body = function_body("Cleanup-InstallerFiles")
    step = re.search(r'Step\s+"([^"]+)"', body)
    assert step is not None
    message = step.group(1).lower()
    assert "download" in message, f"cleanup step should name what it actually removes: {step.group(1)!r}"


# --------------------------------------------------------------------------------------
# 6. Regression guards -- no other pin may move
#
# H3/H4 touch two bootstrap tools. Everything else the installer pins is frozen, and the Director
# model's bytes are the one asset whose exact hash is the product contract.
# --------------------------------------------------------------------------------------


def test_python_and_ffmpeg_pins_are_unchanged():
    assert string_assignment("PythonVersion") == "3.13.14"
    assert string_assignment("FfmpegVersion") == "8.1.2"


def test_director_model_pin_is_unchanged():
    assert string_assignment("DirectorModelRevision") == "e6f794d44f9395d0184a966c27b5ae99ea356fcb"
    assert int_assignment("DirectorModelExpectedBytes") == 4280403520
    assert string_assignment("DirectorModelSha256") == (
        "ae916ede1c010a26955ee8ae2e908bf8815a3f135ec860439ab924701c69d5f1"
    )
    assert "$DirectorModelRevision" in string_assignment("DirectorModelUrl"), (
        "the Director model must stay on its immutable revision, never /resolve/main/"
    )
    assert "/resolve/main/" not in string_assignment("DirectorModelUrl")


def test_stage5_model_policy_is_unchanged():
    body = function_body("Install-QwenGgufModels")
    assert "Qwen3VL-2B-Instruct-Q8_0.gguf" in body
    assert "mmproj-Qwen3VL-2B-Instruct-F16.gguf" in body
    assert "Install-VerifiedModel $DirectorModelUrl" in body


def test_installer_does_not_claim_offline_installation():
    """H3/H4 make two tool *assets* reusable. Package installation still goes to the network."""
    lowered = SOURCE.lower()
    for overclaim in ("offline install", "fully offline", "no network", "zero network"):
        assert overclaim not in lowered


def test_package_installation_is_untouched():
    body = function_body("Install-PythonPackages")
    assert "pip install --python $PythonExe --system --upgrade pip setuptools wheel" in body
    assert "requirements.txt" in body
