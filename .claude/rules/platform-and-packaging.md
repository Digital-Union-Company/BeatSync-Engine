---
paths:
  - "install.bat"
  - "run.bat"
  - "scripts/install.ps1"
  - "src/logger.py"
  - "src/paths.py"
  - "README.md"
  - "CHANGELOG-FORK.md"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Platform notes

Windows-only by construction: `.exe` paths, `CREATE_NO_WINDOW`, `chcp 65001`, PowerShell installer,
embedded-Python `._pth` patched to include `..\..\src`. `gui.py` also patches asyncio's Proactor
transport to swallow benign `WinError 10054` pipe resets — that filter is intentional, not dead code.

Runtime folders are created inside the repo root, so `.gitignore` covers `bin/`, `input/`, `output/`
and Python caches. Ignoring is not deleting: `output/` and `input/video_analysis_cache/` are still
retained on disk by the housekeeping policy in `.claude/rules/operating-policies.md`.

## Installer tooling: pinned, verified, reused

Every bootstrap tool `scripts/install.ps1` fetches is pinned. The pin is the single source of truth —
URLs and the llama version-match pattern derive from it rather than restating the version.

One recorded literal survives on purpose: `$LlamaVersionRecorded = "version:\s+9842"` is a
**tripwire**, compared against the pattern derived from `$LlamaBuild` and throwing if the two
disagree. It is pinned by name in `tests/test_gui_guard_seam.py`, and as a tripwire it is the
inverse of the original defect — moving the pin fails loudly on the next run instead of quietly
matching nothing and replacing a good install.

| tool | pin | variable |
|---|---|---|
| Python (embedded) | `3.13.14` | `$PythonVersion` |
| FFmpeg | `8.1.2` | `$FfmpegVersion` |
| llama.cpp Vulkan x64 | `b9842` | `$LlamaBuild` |
| UV | `0.13.0` | `$UvVersion` |

**The idempotence rule.** An already-valid pinned tool is *reused*; a missing, wrong or
**unprovable** one is *replaced*. "Unprovable" is deliberately grouped with "wrong": the installer
either gets a successful exit code and the exact pinned version string, or it reinstalls. File
existence is never sufficient on its own.

Download caches stay transient — `bin\downloads\` is removed wholesale on every successful run, so
idempotence can never come to rest on a cached archive. Retained *executable* tooling may remain
under `bin/`: `bin\uv\uv.exe` is kept precisely so the next run can validate and reuse it, and the
installer re-proves it **after** cleanup, before reporting success.

**`llama-cli.exe --version` writes to stderr, not stdout** (measured: exit 0, stdout empty,
`version: 9842 (…)` on stderr). This is a trap rather than a detail, because `install.bat` launches
**Windows PowerShell 5.1** under `$ErrorActionPreference = "Stop"`, where *both* obvious probes
throw rather than return text: `2>$null` raises a `RemoteException`, and `2>&1` raises one whose
message is the version line itself. So native version probing goes through `Invoke-NativeProbe`,
which redirects both streams to temp files outside the repository, preserves the native exit code,
and reports a failed launch as exit `-1` so callers treat it exactly like a wrong version.

Two assets are verified by **exact size and SHA256** before use, not by a size floor: the Director
GGUF (its measured bytes *are* the behavioural contract) and the UV archive (it carries the tool
that installs everything else). Both go through `Install-VerifiedModel`, and a failed verification
deletes the bad file so a later existence check cannot pass it off as valid.

What this does **not** claim: offline installation. Package installation still resolves and installs
from the network on every run. The narrow guarantee is **tool-asset reuse** — a valid llama.cpp
`b9842` and a valid UV `0.13.0` are not re-downloaded. The contract is asserted from the installer
source by `tests/test_installer_contract.py`, which needs no PowerShell, Windows, network or
runtime.

Licensed AGPL-3.0. This repository is a **modified fork** — see `CHANGELOG-FORK.md` for the
modification record required by AGPL-3.0 §5(a), and the README licence section for the §13
(network-use) consequence of ever exposing the Gradio UI beyond localhost.
