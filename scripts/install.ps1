$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$BinDir = Join-Path $Root "bin"
$DownloadsDir = Join-Path $BinDir "downloads"
$UvDir = Join-Path $BinDir "uv"
$PythonVersion = "3.13.14"
$FfmpegVersion = "8.1.2"
$LlamaBuild = "b9842"
$LlamaDir = Join-Path $BinDir "llama-bin-win-vulkan-x64"
$PythonDir = Join-Path $BinDir "python-$PythonVersion-embed-amd64"
$PythonExe = Join-Path $PythonDir "python.exe"
$FfmpegDir = Join-Path $BinDir "ffmpeg"
$ModelsDir = Join-Path $BinDir "models"

$PythonZipUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
# UV IS PINNED, HASH-VERIFIED AND RETAINED.
# This used to be `/releases/latest/download/...`, which has two costs. The obvious one is that a
# floating URL silently installs a different UV whenever upstream publishes -- an unaudited build of
# the tool that resolves and installs every other dependency. The less obvious one is that
# successful cleanup then deleted `bin\uv` outright, so the next run had to fetch UV again: a
# repeated download of a tool that was already present and working. Pinning the version makes the
# archive's exact bytes checkable, which in turn is what makes retaining and reusing it safe.
$UvVersion = "0.13.0"
$UvZipUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvArchiveExpectedBytes = 15722003
$UvArchiveSha256 = "088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c"

# Appended to every pinned-version regex below. A plain `\b` is NOT enough: it matches between `0`
# and `-`, so `uv 0.13.0-rc1` would satisfy a pin of `0.13.0`, and `version: 9842-dirty` would
# satisfy `b9842`. Requiring the next character to be neither a word character, a dot nor a hyphen
# means the pinned token has to end exactly where the pin ends. Measured against `uv 0.13.00`,
# `uv 0.13.01`, `uv 0.13.0-rc1` and `uv 1.0.13.0`, none of which may pass.
$VersionTokenEnd = '(?![\w.\-])'
$FfmpegZipUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/ffmpeg-$FfmpegVersion-essentials_build.zip"
$LlamaZipUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild/llama-$LlamaBuild-bin-win-vulkan-x64.zip"

# The version probe matches a build number DERIVED from the pin. The old check restated it as a bare
# `9842`, a literal that could drift away from $LlamaBuild with nothing to notice.
#
# `$LlamaVersionRecorded` is NOT that duplicate coming back: it is a tripwire. The derived pattern
# and this recorded contract text (also pinned by `tests/test_gui_guard_seam.py`) must agree, so
# moving $LlamaBuild fails loudly here on the very next run rather than quietly matching nothing and
# replacing a perfectly good install -- which is the failure mode H3 exists to remove.
$LlamaBuildMatch = [regex]::Match($LlamaBuild, '^b(\d+)$')
if (-not $LlamaBuildMatch.Success) {
    throw "Unexpected llama.cpp build pin '$LlamaBuild'; expected the form b<number>."
}
$LlamaVersionPattern = "version:\s+$($LlamaBuildMatch.Groups[1].Value)"
$LlamaVersionRecorded = "version:\s+9842"
if ($LlamaVersionPattern -ne $LlamaVersionRecorded) {
    throw "llama.cpp pin '$LlamaBuild' no longer agrees with the recorded version pattern '$LlamaVersionRecorded'. Update the pin and the recorded pattern together, deliberately."
}
# Stage 5 semantic analysis: the VISION model plus its multimodal projector. Unchanged.
$QwenModelUrl = "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/main/Qwen3VL-2B-Instruct-Q8_0.gguf?download=true"
$QwenMmprojUrl = "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-2B-Instruct-F16.gguf?download=true"
# AI Director V2: a SEPARATE text-only intent model. It does not replace Stage 5 and has no mmproj,
# because the Director never looks at a frame. The Stage-5 2B model was measured against the V2
# intent contract and failed it, so this is a third asset rather than a reuse of the first.
#
# PINNED TO AN IMMUTABLE REVISION, AND HASH-VERIFIED, FOR A SPECIFIC REASON.
# The Director's whole behavioural evidence -- 100% strict-schema validity, 14/14 concepts, 6/6 on
# the energy cluster, 12/12 on an unseen holdout -- was measured against EXACTLY these bytes. That
# evidence does not transfer to arbitrary future bytes published under the same filename, so
# `/resolve/main/` is the wrong reference: upstream may replace or requantise the file at any time
# and the product would keep claiming validated behaviour it no longer has. The revision below is
# the repository commit containing the measured artifact.
$DirectorModelRevision = "e6f794d44f9395d0184a966c27b5ae99ea356fcb"
$DirectorModelUrl = "https://huggingface.co/ggml-org/Qwen3-4B-Instruct-2507-Q8_0-GGUF/resolve/$DirectorModelRevision/qwen3-4b-instruct-2507-q8_0.gguf"
$DirectorModelFile = "qwen3-4b-instruct-2507-q8_0.gguf"
$DirectorModelExpectedBytes = 4280403520
$DirectorModelSha256 = "ae916ede1c010a26955ee8ae2e908bf8815a3f135ec860439ab924701c69d5f1"

function Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Ensure-Dir($Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Assert-InDirectory($Path, $Parent, $Label) {
    $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $ResolvedParent = (Resolve-Path -LiteralPath $Parent).Path
    $IsInside = (
        $ResolvedPath.Equals($ResolvedParent, [System.StringComparison]::OrdinalIgnoreCase) -or
        $ResolvedPath.StartsWith($ResolvedParent + [IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
    )
    if (-not $IsInside) {
        throw "Refusing to operate on $Label outside expected folder: $ResolvedPath"
    }
}

function Invoke-NativeProbe($Exe, [string[]]$ProbeArgs) {
    <#
        Run a native executable and return its exit code with BOTH output streams captured.

        This exists because asking a tool for its version is deceptively hostile in Windows
        PowerShell. `llama-cli.exe --version` writes the version to STDERR and leaves stdout empty,
        and this script runs under `$ErrorActionPreference = "Stop"` in the Windows PowerShell 5.1
        that `install.bat` launches. Measured on 5.1.26100:

          & $exe --version 2>$null    -> THROWS (RemoteException). The old probe did this, so a
                                         perfectly good pinned build landed in the catch branch and
                                         was re-downloaded and replaced on every run.
          & $exe --version 2>&1       -> ALSO THROWS. Merging stderr into the success stream turns
                                         each stderr line into an ErrorRecord, and `Stop` makes the
                                         first one terminating -- the exception message is literally
                                         the version text we were trying to read.

        Redirecting to files sidesteps PowerShell's stream semantics entirely: the OS writes both
        streams, nothing passes through the error pipeline, and there is no reader deadlock. A
        launch failure (missing or unrunnable image) is reported as exit code -1 rather than
        propagating, so every caller can treat "could not prove it" exactly like "wrong version".

        Scratch goes to the system temp directory, never into the repository, and is removed in
        `finally`.
    #>
    $OutFile = [IO.Path]::GetTempFileName()
    $ErrFile = [IO.Path]::GetTempFileName()
    try {
        try {
            $Proc = Start-Process -FilePath $Exe -ArgumentList $ProbeArgs -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
        } catch {
            return [pscustomobject]@{
                ExitCode = -1
                StdOut = ""
                StdErr = $_.Exception.Message
                Combined = $_.Exception.Message
            }
        }
        $StdOut = [string](Get-Content -LiteralPath $OutFile -Raw -ErrorAction SilentlyContinue)
        $StdErr = [string](Get-Content -LiteralPath $ErrFile -Raw -ErrorAction SilentlyContinue)
        return [pscustomobject]@{
            ExitCode = $Proc.ExitCode
            StdOut = $StdOut
            StdErr = $StdErr
            Combined = ($StdOut + "`n" + $StdErr)
        }
    } finally {
        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ErrFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-ProbeFirstLine($Probe) {
    return ($Probe.Combined -split "`r?`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1)
}

function Get-CurlExe {
    $SystemCurl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($SystemCurl) {
        return $SystemCurl.Source
    }
    throw "curl.exe was not found. Windows 10 1803+ includes curl.exe; install curl or update Windows."
}

$script:CurlHelpText = $null
function Test-CurlOption($CurlExe, $Option) {
    if ($null -eq $script:CurlHelpText) {
        try {
            $script:CurlHelpText = (& $CurlExe --help all 2>$null) -join "`n"
        } catch {
            $script:CurlHelpText = ""
        }
    }
    return ($script:CurlHelpText -match [regex]::Escape($Option))
}

function Invoke-CurlDownload($CurlExe, $Url, $OutFile, [bool]$Resume) {
    $FailOption = "--fail"
    if (Test-CurlOption $CurlExe "--fail-with-body") {
        $FailOption = "--fail-with-body"
    }

    $CurlArgs = @(
        "--location",
        $FailOption,
        "--show-error",
        "--retry", "12",
        "--retry-delay", "2",
        "--retry-max-time", "0",
        "--connect-timeout", "30",
        "--speed-time", "60",
        "--speed-limit", "1024",
        "--user-agent", "BeatSync-Installer/1.0",
        "--output", $OutFile
    )

    if (Test-CurlOption $CurlExe "--retry-all-errors") {
        $CurlArgs += "--retry-all-errors"
    }
    if (Test-CurlOption $CurlExe "--retry-connrefused") {
        $CurlArgs += "--retry-connrefused"
    }
    if (Test-CurlOption $CurlExe "--tcp-nodelay") {
        $CurlArgs += "--tcp-nodelay"
    }
    if ($Resume) {
        $CurlArgs += @("--continue-at", "-")
    }

    $CurlArgs += $Url
    & $CurlExe @CurlArgs
    return $LASTEXITCODE
}

function Download-File($Url, $Path, [long]$MinimumBytes = 1) {
    Ensure-Dir (Split-Path -Parent $Path)

    if (Test-Path $Path) {
        $Existing = Get-Item -LiteralPath $Path
        if ($Existing.Length -ge $MinimumBytes) {
            Write-Host "Using cached file: $Path"
            return
        }
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }

    $TempPath = "$Path.partial"
    $CurlExe = Get-CurlExe
    Write-Host "Downloading: $Url"
    Write-Host "Using curl: $CurlExe"

    $Resume = $false
    if (Test-Path $TempPath) {
        $Partial = Get-Item -LiteralPath $TempPath
        if ($Partial.Length -gt 0) {
            $Resume = $true
            Write-Host "Resuming partial file: $TempPath"
        } else {
            Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
        }
    }

    $ExitCode = Invoke-CurlDownload $CurlExe $Url $TempPath $Resume
    if (($ExitCode -ne 0) -and $Resume) {
        Write-Host "Resume failed; retrying once from scratch." -ForegroundColor Yellow
        Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
        $ExitCode = Invoke-CurlDownload $CurlExe $Url $TempPath $false
    }

    if ($ExitCode -ne 0) {
        throw "curl download failed with exit code $ExitCode`: $Url"
    }
    if (-not (Test-Path $TempPath)) {
        throw "curl reported success, but output file was not created: $TempPath"
    }
    if ((Get-Item -LiteralPath $TempPath).Length -lt $MinimumBytes) {
        Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
        throw "downloaded file is too small: $Url"
    }

    Move-Item -LiteralPath $TempPath -Destination $Path -Force
}

function Test-FileSha256($Path, $ExpectedSha256) {
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    return $Actual.Equals($ExpectedSha256, [System.StringComparison]::OrdinalIgnoreCase)
}

function Install-VerifiedModel($Url, $Path, [long]$ExpectedBytes, $ExpectedSha256, $Label) {
    <#
        An exact-size-AND-SHA256 ensure/verify wrapper around the shared `Download-File`.

        The name is historical: the Director model was the only caller, and is still the strictest
        consumer. The pinned UV archive now uses this same path, because the requirement is
        identical and one verified code path beats two. The seam is pinned by name in
        `tests/test_gui_guard_seam.py`, so it keeps that name.

        The generic downloader treats any existing file of at least `MinimumBytes` as reusable,
        which is the right policy for an archive that is about to be expanded and validated by its
        own contents -- and the wrong policy for a 4.28 GB model whose exact bytes ARE the contract.
        A locally corrupted, truncated-then-resumed, hand-swapped or differently-quantised file over
        the size floor would be silently accepted and then reported as a valid Director model.

        So this path is EXACT rather than "big enough", at both ends:

          A. existing file, exact size AND matching hash  -> reuse, no download
          B. existing file, wrong size                    -> remove, download pinned artifact
          C. existing file, exact size but wrong hash      -> remove, download pinned artifact
          D. downloaded artifact                           -> exact size AND hash, or FAIL loudly
          E. any failed verification                       -> the bad file is REMOVED first, so a
                                                              later `Test-RequiredFile` can never
                                                              report it as a valid model

        Deliberately narrow: it changes no other asset's policy, adds no dependency, and reuses
        `Download-File` for the transfer itself (curl, resume, retries) rather than reimplementing it.
    #>
    Ensure-Dir (Split-Path -Parent $Path)

    if (Test-Path $Path) {
        $Existing = Get-Item -LiteralPath $Path
        if ($Existing.Length -ne $ExpectedBytes) {
            Write-Host "$Label has the wrong size ($($Existing.Length) bytes, expected $ExpectedBytes); replacing it." -ForegroundColor Yellow
            Remove-Item -LiteralPath $Path -Force
        } elseif (-not (Test-FileSha256 $Path $ExpectedSha256)) {
            Write-Host "$Label has the expected size but the wrong SHA256; replacing it." -ForegroundColor Yellow
            Remove-Item -LiteralPath $Path -Force
        } else {
            Write-Host "$Label verified (exact size and SHA256): $Path"
            return
        }
    }

    # `MinimumBytes` is the exact size here: a short transfer must not even reach verification.
    Download-File $Url $Path $ExpectedBytes

    if (-not (Test-Path $Path)) {
        throw "$Label download reported success but produced no file: $Path"
    }
    $Downloaded = Get-Item -LiteralPath $Path
    if ($Downloaded.Length -ne $ExpectedBytes) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        throw "$Label has the wrong size: got $($Downloaded.Length) bytes, expected $ExpectedBytes."
    }
    if (-not (Test-FileSha256 $Path $ExpectedSha256)) {
        $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        throw "$Label failed SHA256 verification: got $Actual, expected $ExpectedSha256."
    }
    Write-Host "$Label verified (exact size and SHA256): $Path"
}

function Expand-Zip($ZipPath, $Destination) {
    Ensure-Dir $Destination
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $Destination -Force
}

function Remove-SafeFolder($Path, $Parent, $Label) {
    if (-not (Test-Path $Path)) {
        return
    }
    Assert-InDirectory $Path $Parent $Label
    Write-Host "Removing legacy $Label`: $Path"
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Install-Uv {
    <#
        Reuse a retained UV only when it can PROVE it is the pinned version.

        "The file exists" was the old test, which was both too weak and -- because cleanup deleted
        the folder anyway -- never actually exercised. Existence says nothing about which UV it is,
        and a `latest`-era executable left behind by an older install is exactly the thing that must
        not be trusted. So: exit code 0 AND an exact pinned version string, or it gets replaced from
        the hash-verified archive.
    #>
    Step "Preparing UV $UvVersion"
    $UvExe = Join-Path $UvDir "uv.exe"

    if (Test-Path $UvExe) {
        $Probe = Invoke-NativeProbe $UvExe @("--version")
        if (($Probe.ExitCode -eq 0) -and ($Probe.Combined -match "uv\s+$([regex]::Escape($UvVersion))$VersionTokenEnd")) {
            Write-Host "UV $UvVersion ready (reusing retained executable): $UvExe"
            return $UvExe
        }
        if ($Probe.ExitCode -ne 0) {
            Write-Host "Retained uv.exe could not report its version (exit $($Probe.ExitCode)); replacing it." -ForegroundColor Yellow
        } else {
            Write-Host "Retained uv.exe is not $UvVersion (got: $(Get-ProbeFirstLine $Probe)); replacing it." -ForegroundColor Yellow
        }
        Assert-InDirectory $UvDir $BinDir "UV folder"
        Remove-Item -LiteralPath $UvDir -Recurse -Force
    }

    Ensure-Dir $UvDir
    $Archive = Join-Path $DownloadsDir "uv-$UvVersion-x86_64-pc-windows-msvc.zip"
    # Exact size AND SHA256 BEFORE extraction -- this archive carries the tool that installs
    # everything else, so "big enough" is not a sufficient check for it either.
    Install-VerifiedModel $UvZipUrl $Archive $UvArchiveExpectedBytes $UvArchiveSha256 "UV $UvVersion archive"
    Expand-Zip $Archive $UvDir

    $Found = Get-ChildItem -Path $UvDir -Recurse -Filter "uv.exe" | Select-Object -First 1
    if (-not $Found) {
        throw "UV archive extracted, but uv.exe was not found."
    }
    if ($Found.FullName -ne $UvExe) {
        Copy-Item -LiteralPath $Found.FullName -Destination $UvExe -Force
    }
    return $UvExe
}

function Install-Python {
    Step "Installing portable Python $PythonVersion"
    if (-not (Test-Path $PythonExe)) {
        $Archive = Join-Path $DownloadsDir "python-$PythonVersion-embed-amd64.zip"
        Download-File $PythonZipUrl $Archive 1048576
        if (Test-Path $PythonDir) {
            Remove-Item -LiteralPath $PythonDir -Recurse -Force
        }
        Expand-Zip $Archive $PythonDir
    }

    $PthFile = Join-Path $PythonDir "python313._pth"
    @(
        "python313.zip",
        ".",
        "Lib\site-packages",
        "..\..\src",
        "import site"
    ) | Set-Content -LiteralPath $PthFile -Encoding ASCII

    Ensure-Dir (Join-Path $PythonDir "Lib\site-packages")
    Write-Host "Python ready: $PythonExe"
}

function Install-FFmpeg {
    Step "Installing FFmpeg $FfmpegVersion release zip"
    $FfmpegExe = Join-Path $FfmpegDir "ffmpeg.exe"
    $FfprobeExe = Join-Path $FfmpegDir "ffprobe.exe"
    if ((Test-Path $FfmpegExe) -and (Test-Path $FfprobeExe)) {
        try {
            $CurrentVersion = (& $FfmpegExe -version 2>$null | Select-Object -First 1)
            if ($CurrentVersion -match [regex]::Escape($FfmpegVersion)) {
                Write-Host "FFmpeg ready: $FfmpegDir"
                return
            }
            Write-Host "Existing FFmpeg is not $FfmpegVersion; replacing portable binaries."
        } catch {
            Write-Host "Existing FFmpeg version check failed; replacing portable binaries."
        }
    }

    Ensure-Dir $FfmpegDir
    $Archive = Join-Path $DownloadsDir "ffmpeg-$FfmpegVersion-essentials_build.zip"
    Download-File $FfmpegZipUrl $Archive 1048576
    $ExtractDir = Join-Path $DownloadsDir "ffmpeg-extract"
    if (Test-Path $ExtractDir) {
        Remove-Item -LiteralPath $ExtractDir -Recurse -Force
    }
    Expand-Zip $Archive $ExtractDir

    $ExtractedFfmpeg = Get-ChildItem -Path $ExtractDir -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $ExtractedFfmpeg) {
        throw "FFmpeg archive extracted, but ffmpeg.exe was not found."
    }
    $ExtractedBin = Split-Path -Parent $ExtractedFfmpeg.FullName
    Copy-Item -LiteralPath (Join-Path $ExtractedBin "ffmpeg.exe") -Destination $FfmpegDir -Force
    Copy-Item -LiteralPath (Join-Path $ExtractedBin "ffprobe.exe") -Destination $FfmpegDir -Force
}

function Install-LlamaCppVulkan {
    Step "Installing llama.cpp Vulkan $LlamaBuild"
    $ServerExe = Join-Path $LlamaDir "llama-server.exe"
    $MtmdExe = Join-Path $LlamaDir "llama-mtmd-cli.exe"
    $CliExe = Join-Path $LlamaDir "llama-cli.exe"
    # AI Director V2 runs one-shot through llama-completion.exe, not llama-cli.exe -- see
    # .claude/rules/director.md for why that choice is measured rather than stylistic. It ships in
    # the same archive, but it was not previously named here, so an otherwise "ready" install could
    # satisfy this check and still have no Director runtime.
    $CompletionExe = Join-Path $LlamaDir "llama-completion.exe"
    if ((Test-Path $ServerExe) -and (Test-Path $MtmdExe) -and (Test-Path $CliExe) -and (Test-Path $CompletionExe)) {
        # `llama-cli --version` prints to STDERR with an empty stdout and exit code 0. See
        # `Invoke-NativeProbe` for why reading that through the PowerShell pipeline throws instead
        # of returning text, and why a valid pinned build was consequently replaced on every run.
        $Probe = Invoke-NativeProbe $CliExe @("--version")
        if (($Probe.ExitCode -eq 0) -and ($Probe.Combined -match "$LlamaVersionPattern$VersionTokenEnd")) {
            Write-Host "llama.cpp Vulkan $LlamaBuild ready (reusing existing binaries): $LlamaDir"
            return
        }
        if ($Probe.ExitCode -ne 0) {
            Write-Host "Existing llama-cli.exe could not report its version (exit $($Probe.ExitCode)); replacing Vulkan binaries." -ForegroundColor Yellow
        } else {
            Write-Host "Existing llama.cpp build is not $LlamaBuild (got: $(Get-ProbeFirstLine $Probe)); replacing Vulkan binaries." -ForegroundColor Yellow
        }
    }

    $Archive = Join-Path $DownloadsDir "llama-$LlamaBuild-bin-win-vulkan-x64.zip"
    Download-File $LlamaZipUrl $Archive 1048576
    $ExtractDir = Join-Path $DownloadsDir "llama-vulkan-extract"
    if (Test-Path $ExtractDir) {
        Remove-Item -LiteralPath $ExtractDir -Recurse -Force
    }
    Expand-Zip $Archive $ExtractDir

    $ExtractedServer = Get-ChildItem -Path $ExtractDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
    if (-not $ExtractedServer) {
        throw "llama.cpp archive extracted, but llama-server.exe was not found."
    }

    if (Test-Path $LlamaDir) {
        Remove-Item -LiteralPath $LlamaDir -Recurse -Force
    }
    Ensure-Dir $LlamaDir
    $ExtractedBin = Split-Path -Parent $ExtractedServer.FullName
    Copy-Item -Path (Join-Path $ExtractedBin "*") -Destination $LlamaDir -Recurse -Force
}

function Install-PythonPackages($UvExe) {
    Step "Installing Python packages with UV"
    $Env:UV_LINK_MODE = "copy"
    foreach ($Name in @("CUDA_PATH", "CUDA_HOME", "CUDA_ROOT")) {
        Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
    }
    $Env:PATH = @(
        $FfmpegDir,
        $PythonDir,
        (Join-Path $PythonDir "Scripts"),
        $Env:PATH
    ) -join [IO.Path]::PathSeparator

    & $UvExe pip install --python $PythonExe --system --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "UV failed while installing pip/setuptools/wheel." }

    & $UvExe pip install --python $PythonExe --system --upgrade -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "UV failed while installing app requirements." }
}

function Remove-LegacyPythonPackages($UvExe) {
    Step "Removing legacy PyTorch/Transformers packages"
    $Packages = @("torch", "torchvision", "torchaudio", "accelerate", "transformers", "safetensors")
    & $UvExe pip uninstall --python $PythonExe --system -y @Packages
    if ($LASTEXITCODE -ne 0) {
        throw "UV failed while removing legacy PyTorch/Transformers packages."
    }
}

function Install-QwenGgufModels {
    Step "Installing Qwen GGUF models (Stage 5 vision + Director intent)"
    Ensure-Dir $ModelsDir
    # Stage 5 semantic analysis.
    Download-File $QwenModelUrl (Join-Path $ModelsDir "Qwen3VL-2B-Instruct-Q8_0.gguf") 104857600
    Download-File $QwenMmprojUrl (Join-Path $ModelsDir "mmproj-Qwen3VL-2B-Instruct-F16.gguf") 104857600
    # AI Director V2. Exact size AND SHA256, from a pinned immutable revision -- see the constants
    # at the top of this file for why "big enough" is not a sufficient check for this one asset.
    Install-VerifiedModel $DirectorModelUrl (Join-Path $ModelsDir $DirectorModelFile) `
        $DirectorModelExpectedBytes $DirectorModelSha256 "AI Director GGUF model"
}

function Ensure-AppFolders {
    Step "Creating app folders"
    foreach ($Path in @(
        "input",
        "input\audio",
        "input\video",
        "input\processing",
        "input\gradio_uploads",
        "input\video_analysis_cache",
        "output"
    )) {
        Ensure-Dir (Join-Path $Root $Path)
    }
}

function Remove-LegacyFiles {
    Step "Removing legacy runtime files"
    Remove-SafeFolder (Join-Path $BinDir "PortableGit") $BinDir "PortableGit folder"
    Remove-SafeFolder (Join-Path $BinDir "CUDA\v13.3") $BinDir "portable CUDA Toolkit"
    Remove-SafeFolder (Join-Path $BinDir "models\Qwen3-VL-2B-Instruct") $BinDir "Transformers Qwen model"

    $LegacyMinGitZip = Join-Path $DownloadsDir "MinGit-2.54.0-64-bit.zip"
    if (Test-Path $LegacyMinGitZip) {
        Remove-Item -LiteralPath $LegacyMinGitZip -Force
    }

    $CudaRoot = Join-Path $BinDir "CUDA"
    if ((Test-Path $CudaRoot) -and (-not (Get-ChildItem -LiteralPath $CudaRoot -Force -ErrorAction SilentlyContinue))) {
        Remove-Item -LiteralPath $CudaRoot -Force
    }
}

function Remove-InstallerFolder($Path, $Label) {
    if (-not (Test-Path $Path)) {
        return
    }
    Assert-InDirectory $Path $BinDir $Label
    Write-Host "Cleaning $Label`: $Path"
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Cleanup-InstallerFiles {
    <#
        Remove the transient download cache and NOTHING ELSE.

        `bin\uv\uv.exe` used to be deleted here, which is what made the next run download UV again.
        It is now deliberately retained: it is a pinned, hash-verified executable, `Install-Uv`
        re-proves its version on every run, and the verification block below confirms it survived
        this cleanup. Archives are still not kept -- the durable artifact is the verified
        executable, not the ZIP it came out of, and `$DownloadsDir` holds every archive the
        installer fetches.
    #>
    Step "Cleaning transient installer downloads"
    Remove-InstallerFolder $DownloadsDir "download cache"
}

function Test-RequiredFile($Path, $Label) {
    if (-not (Test-Path $Path)) {
        throw "Missing $Label`: $Path"
    }
}

Ensure-Dir $BinDir
Ensure-Dir $DownloadsDir
Ensure-AppFolders
Remove-LegacyFiles

Install-Python
$UvExe = Install-Uv
Install-FFmpeg
Install-LlamaCppVulkan
Install-PythonPackages $UvExe
Remove-LegacyPythonPackages $UvExe
Install-QwenGgufModels

Step "Verifying portable install"
& $PythonExe -X utf8 -c "import sys, gradio, librosa, cv2, numpy, cupy, numba; print('Python', sys.version.split()[0]); print('gradio', gradio.__version__); print('librosa', librosa.__version__); print('cupy', cupy.__version__); print('numba', numba.__version__); x = cupy.arange(10, dtype=cupy.int32); print('CUDA runtime', cupy.cuda.runtime.runtimeGetVersion()); print('GPU sum', int(cupy.sum(x).get()))"
if ($LASTEXITCODE -ne 0) {
    throw "Portable app import/CuPy CTK verification failed."
}

& $PythonExe -X utf8 -c "import importlib.util, sys; missing = [name for name in ('torch', 'torchvision', 'torchaudio', 'accelerate', 'transformers', 'safetensors') if importlib.util.find_spec(name) is not None]; print('legacy packages', missing); sys.exit(1 if missing else 0)"
if ($LASTEXITCODE -ne 0) {
    throw "Legacy PyTorch/Transformers packages are still installed."
}

Test-RequiredFile (Join-Path $LlamaDir "llama-server.exe") "llama-server.exe"
Test-RequiredFile (Join-Path $LlamaDir "llama-mtmd-cli.exe") "llama-mtmd-cli.exe"
Test-RequiredFile (Join-Path $LlamaDir "llama-cli.exe") "llama-cli.exe"
Test-RequiredFile (Join-Path $LlamaDir "llama-completion.exe") "llama-completion.exe"
Test-RequiredFile (Join-Path $ModelsDir "Qwen3VL-2B-Instruct-Q8_0.gguf") "Stage 5 Qwen GGUF model"
Test-RequiredFile (Join-Path $ModelsDir "mmproj-Qwen3VL-2B-Instruct-F16.gguf") "Stage 5 Qwen mmproj model"
Test-RequiredFile (Join-Path $ModelsDir $DirectorModelFile) "AI Director GGUF model"

& (Join-Path $LlamaDir "llama-cli.exe") --version
if ($LASTEXITCODE -ne 0) {
    throw "llama-cli.exe --version failed."
}

& $PythonExe -X utf8 -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "pip check failed."
}

Cleanup-InstallerFiles

# Deliberately AFTER cleanup: what needs proving is that the retained tooling survived it, so the
# next run can validate and reuse it instead of downloading the archive again.
Step "Verifying retained installer tooling"
Test-RequiredFile $UvExe "retained uv.exe"
$UvVerification = Invoke-NativeProbe $UvExe @("--version")
if ($UvVerification.ExitCode -ne 0) {
    throw "Retained uv.exe --version failed with exit code $($UvVerification.ExitCode)."
}
if ($UvVerification.Combined -notmatch "uv\s+$([regex]::Escape($UvVersion))$VersionTokenEnd") {
    throw "Retained uv.exe did not report $UvVersion`: $(Get-ProbeFirstLine $UvVerification)"
}
Write-Host "UV $UvVersion retained and verified: $UvExe"

Write-Host ""
Write-Host "Portable install is ready: llama.cpp Vulkan + CuPy CTK, no PyTorch." -ForegroundColor Green
Write-Host "Pinned tooling is reused on later runs: llama.cpp $LlamaBuild and UV $UvVersion are not re-downloaded while they verify." -ForegroundColor Green
