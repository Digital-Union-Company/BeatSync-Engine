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
$UvZipUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
$FfmpegZipUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/ffmpeg-$FfmpegVersion-essentials_build.zip"
$LlamaZipUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild/llama-$LlamaBuild-bin-win-vulkan-x64.zip"
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
        A Director-specific ensure/verify wrapper around the shared `Download-File`.

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
    Step "Preparing UV"
    $UvExe = Join-Path $UvDir "uv.exe"
    if (Test-Path $UvExe) {
        Write-Host "UV ready: $UvExe"
        return $UvExe
    }

    Ensure-Dir $UvDir
    $Archive = Join-Path $DownloadsDir "uv-x86_64-pc-windows-msvc.zip"
    Download-File $UvZipUrl $Archive 1048576
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
        try {
            $CurrentVersion = (& $CliExe --version 2>$null | Select-Object -First 1)
            if ($CurrentVersion -match "version:\s+9842\b") {
                Write-Host "llama.cpp Vulkan ready: $LlamaDir"
                return
            }
            Write-Host "Existing llama.cpp build is not $LlamaBuild; replacing Vulkan binaries."
        } catch {
            Write-Host "Existing llama.cpp version check failed; replacing Vulkan binaries."
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
    Step "Cleaning installer cache"
    Remove-InstallerFolder $DownloadsDir "downloads"
    Remove-InstallerFolder $UvDir "UV"
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

Write-Host ""
Write-Host "Portable install is ready: llama.cpp Vulkan + CuPy CTK, no PyTorch." -ForegroundColor Green
