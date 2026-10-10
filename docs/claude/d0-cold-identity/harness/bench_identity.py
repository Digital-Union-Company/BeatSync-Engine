"""H2: time the exact production cache-identity phase at a requested worker count.

Benchmark boundary (deliberately narrow):

  * ONLY `video_analysis._compute_cache_paths_parallel(...)` is timed -- one `perf_counter()`
    immediately before and one subtraction immediately after, which is exactly what production
    records as `cache_identity_seconds`.
  * Backend-token resolution, config-token resolution, source enumeration and manifest verification
    all happen OUTSIDE the timed region.
  * `analyze_video_sources` is never called. No cache payload is loaded or written, no Qwen
    inference runs, no deterministic video analysis runs, nothing is rendered.
  * `_cache_path`'s destination is redirected into H2 scratch; the files need not and do not exist.

The 1-worker measurement goes through the SAME production helper (which takes its own serial branch
at `workers <= 1`), never a hand-written loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import d0_common as h2  # noqa: E402

RC1 = r"C:\tmp\BeatSync-Engine-DigitalUnion\tasks\release-v0.1.0-rc1\source"
os.environ.setdefault("BEATSYNC_QWEN_LLAMA_DIR", os.path.join(RC1, "bin", "llama-bin-win-vulkan-x64"))
os.environ.setdefault("BEATSYNC_QWEN_LLAMA_MODEL",
                      os.path.join(RC1, "bin", "models", "Qwen3VL-2B-Instruct-Q8_0.gguf"))
os.environ.setdefault("BEATSYNC_QWEN_LLAMA_MMPROJ",
                      os.path.join(RC1, "bin", "models", "mmproj-Qwen3VL-2B-Instruct-F16.gguf"))

parser = argparse.ArgumentParser()
parser.add_argument("--workers", type=int, required=True)
parser.add_argument("--slot", required=True, help="run slot label, e.g. cold_w1_a")
parser.add_argument("--warm-repeat", action="store_true",
                    help="after the timed cold run, repeat it immediately without any reset")
parser.add_argument("--limit", type=int, default=0, help="subset size for cold-method validation only")
args = parser.parse_args()

source_sha256 = h2.assert_source_bytes()
dropped = h2.bootstrap_syspath()

# Production prologue order: logger.setup_environment() before cupy/cv2 land via video_analysis.
from logger import setup_environment  # noqa: E402
setup_environment()

import video_analysis as va  # noqa: E402

h2.assert_provenance(va, require_cache_redirect=False)
cache_dir = h2.redirect_cache_dir(va)
h2.assert_provenance(va, require_cache_redirect=True)

manifest_path = os.path.join(h2.D0_ROOT, "evidence", "source_manifest.json")
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)

entries = manifest["entries"]
if args.limit:
    entries = entries[:args.limit]
sources = [item["path"] for item in entries]
source_count = len(sources)

# ---- invocation-level tokens, resolved exactly as classify_library_sources does, ONCE, untimed ----
requested_qwen_model_path = va.DEFAULT_QWEN_MODEL_DIR
qwen_model_path = va._qwen_backend_model_path(requested_qwen_model_path)
enable_ai = True
ai_available = bool(enable_ai and va._qwen_backend_available(requested_qwen_model_path))
if not ai_available:
    raise SystemExit("H2 STOP: Qwen backend assets are not all present; "
                     "backend identity cannot be proven and the None path must not be benchmarked.")
backend_token = va._qwen_backend_signature_token(qwen_model_path)
if backend_token is None:
    raise SystemExit("H2 STOP: _qwen_backend_signature_token returned None; "
                     "refusing to benchmark the fail-disabled None identity path.")
config_token = va._qwen_config_token()
if not config_token:
    raise SystemExit("H2 STOP: _qwen_config_token did not resolve.")

# ---- worker policy: env-driven, verified through the production resolver ----
requested = os.environ.get("BEATSYNC_CACHE_IDENTITY_WORKERS", "")
effective_workers = va._cache_identity_workers(source_count)
if requested != str(args.workers):
    raise SystemExit(f"H2 STOP: BEATSYNC_CACHE_IDENTITY_WORKERS={requested!r} "
                     f"does not match --workers {args.workers}")
if effective_workers != args.workers:
    raise SystemExit(f"H2 STOP: production _cache_identity_workers({source_count}) "
                     f"= {effective_workers}, expected {args.workers}")


def one_pass() -> tuple[float, list, str | None]:
    error = None
    started = time.perf_counter()
    try:
        keys = va._compute_cache_paths_parallel(
            sources, enable_ai, qwen_model_path,
            backend_token=backend_token, config_token=config_token,
        )
    except BaseException as exc:  # surfaced, never laundered into a result
        elapsed = time.perf_counter() - started
        return elapsed, [], f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    return elapsed, keys, error


cold_seconds, keys, cold_error = one_pass()

warm_seconds = None
warm_digest = None
if args.warm_repeat and cold_error is None:
    warm_seconds, warm_keys, warm_error = one_pass()
    warm_digest = h2.ordered_digest(warm_keys) if warm_error is None else None

# ---- post-run accounting (untimed) ----
none_count = sum(1 for key in keys if key is None)
non_none = [key for key in keys if key is not None]
duplicate_positions = len(non_none) - len(set(non_none))

# Did the library change under measurement? Pure stat, after the timed region.
drift = []
for item in entries:
    try:
        stat = os.stat(item["path"])
    except OSError as exc:
        drift.append({"path": item["path"], "problem": f"stat_failed: {exc.__class__.__name__}"})
        continue
    if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
        drift.append({"path": item["path"], "problem": "size_or_mtime_changed",
                      "was": [item["size"], item["mtime_ns"]],
                      "now": [stat.st_size, stat.st_mtime_ns]})

record = {
    "slot": args.slot,
    "worker_request": args.workers,
    "effective_workers": effective_workers,
    "source_count": source_count,
    "subset_limit": args.limit or None,
    "cache_identity_seconds": cold_seconds,
    "warm_repeat_seconds": warm_seconds,
    "returned_key_count": len(keys),
    "none_count": none_count,
    "duplicate_position_count": duplicate_positions,
    "exception": cold_error,
    "exception_count": 0 if cold_error is None else 1,
    "ordered_keys_sha256": h2.ordered_digest(keys),
    "warm_repeat_ordered_keys_sha256": warm_digest,
    "manifest_sha256": manifest["manifest_sha256"],
    "manifest_drift_count": len(drift),
    "manifest_drift": drift[:20],
    "backend_token": backend_token,
    "config_token": config_token,
    "cache_contract_version": va.CACHE_CONTRACT_VERSION,
    "analysis_version": va.ANALYSIS_VERSION,
    "cache_dir": cache_dir,
    "video_analysis_file": va.__file__,
    "dropped_syspath_entries": dropped,
    "video_analysis_sha256": source_sha256,
    "python_version": sys.version,
    "cache_files_present_after": len(os.listdir(cache_dir)) if os.path.isdir(cache_dir) else 0,
}

runs_dir = os.path.join(h2.D0_ROOT, "evidence", "runs")
h2.write_json(os.path.join(runs_dir, f"{args.slot}.json"), record)

keys_dir = os.path.join(h2.D0_ROOT, "evidence", "keys")
os.makedirs(keys_dir, exist_ok=True)
with open(os.path.join(keys_dir, f"{args.slot}.keys.txt"), "w", encoding="utf-8") as handle:
    for index, key in enumerate(keys):
        handle.write(f"{index}\t{'' if key is None else os.path.basename(key)}\n")

print(json.dumps({k: v for k, v in record.items()
                  if k not in ("manifest_drift", "dropped_syspath_entries")}, indent=2))
