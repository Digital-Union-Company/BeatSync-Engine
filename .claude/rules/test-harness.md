---
paths:
  - "tests/**/*.py"
  - "pytest.ini"
  - "requirements-dev.txt"
---
> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`.

## Tests

```bat
python -m pip install -r requirements-dev.txt    :: pytest only; NOT installed into bin/ by install.ps1
python -m pytest                                 :: whole suite
python -m pytest tests/test_input_manager_large.py -v
```

The whole suite runs on **any** recent CPython — no portable runtime, no CUDA, no FFmpeg, no models.
That is possible for two different reasons, and they are worth keeping apart:

1. **Directly importable, bare-CPython core** — `src/beatsync_fork/*` is imported normally and tested
   as ordinary code, because fork modules are stdlib-only by rule.
2. **Upstream seams verified without importing the runtime** — `video_analysis.py`, `gui.py`,
   `stage5_qwen_scene_worker.py`, `stage6_av_planner.py` and `stage4_select.py` are reached by `ast`
   inspection, by path-based `importlib` loading, and by AST-extracting real bodies and executing
   them against a synthesised parent package (all three techniques are described below). These are
   real assertions about real upstream source — seam and structure assertions, not integration runs.
3. **Real portable-runtime / end-to-end verification is out of scope for the suite** — run the CLI on
   a short audio file plus one source video instead. One opt-in tier sits in between:
   `tests/test_audio_mixdown.py` exercises real FFmpeg when `BEATSYNC_TEST_FFMPEG` points at the
   portable build and skips otherwise, which is where the suite's skips normally come from.

So the upstream pipeline modules **cannot be imported** on a bare interpreter and are nevertheless
**not untested**. Do not restate either half of that as the whole truth.

`tests/test_qwen_stream.py` is the one suite that spawns **real child processes**: it writes a tiny fake
Qwen worker into `tmp_path` and runs it with `sys.executable`. That is deliberate — the defect Phase 2B
fixed is a property of the process boundary, and a mocked `Popen` would "stream" happily under the old
`capture_output=True` code too. It still needs nothing but CPython, and it is a few seconds slower than
the rest of the suite because several cases wait on real child timing.

Two suites verify code that **cannot be imported** on a bare interpreter (`video_analysis.py` needs the
whole runtime; the Qwen worker needs cv2/PIL) by inspecting it with `ast` instead:
`tests/test_qwen_worker_protocol.py` and `tests/test_gui_guard_seam.py`. Prefer AST assertions over grep
there — they match call sites by function and keyword, so e.g. the `llama-mtmd-cli --version` probe is
distinguished from the streaming launches by its argv rather than by a line number.

Two other loading tricks recur, and both are deliberate rather than convenient:

- **Load a stage module by path** when it has no relative imports. `stage6_av_planner.py` imports only
  numpy and fork modules, so `tests/test_creative_seed.py`, `test_stage6_score_precompute.py` and
  `test_creative_scoring.py` load it with `importlib` and get the *real* planner without dragging in
  `auto_mode/__init__` (librosa, cupy, logger).
- **Load a stage module onto a synthesised parent package** when it does have relative imports.
  `stage4_select.py` needs `AutoWaveConfig` and three numeric helpers from `auto_mode/__init__`, so
  `tests/test_cut_density.py` AST-extracts exactly those, execs them into a stub package and loads the
  real stage file as its child. Use `ast.unparse`, **not** `ast.get_source_segment`: a `ClassDef`'s
  `lineno` points at the `class` keyword, so the source segment silently drops
  `@dataclass(frozen=True)` and `AutoWaveConfig` comes back as a fieldless plain class.

Beyond the seams listed above, the upstream pipeline modules have **no direct behavioural coverage**
and cannot be imported without the portable runtime; verification of their *runtime* behaviour is
still end-to-end (run the CLI on a short audio file plus one source video and check the console stage
timings and the output file). There is no linter or formatter config. The
installer's own smoke checks (`scripts/install.ps1`, bottom) are the closest thing to a runtime health
check — they import gradio/librosa/cv2/cupy/numba, run a CuPy kernel, assert PyTorch is *absent*,
verify the llama.cpp binaries and GGUF files, and run `pip check`.
