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

Licensed AGPL-3.0. This repository is a **modified fork** — see `CHANGELOG-FORK.md` for the
modification record required by AGPL-3.0 §5(a), and the README licence section for the §13
(network-use) consequence of ever exposing the Gradio UI beyond localhost.
