"""D0 shared bootstrap: force every BeatSync module to resolve from the frozen D0 source export.

Identical in substance to the H2 harness (`post-v0.1.0-h2-cold-cache-identity\\harness\\h2_common.py`),
retargeted at the D0 export of main `eabf342e`. Guards the documented portable-Python `._pth`
provenance hazard: the embedded interpreter's `python313._pth` carries `..\\..\\src`, which wins over
PYTHONPATH, so a naive `import video_analysis` under a borrowed runtime loads that runtime's checkout
and points VIDEO_ANALYSIS_CACHE_DIR at a real runtime cache.

Nothing here modifies production code. It only (a) fixes sys.path, (b) redirects the module-level
cache directory into D0 scratch, and (c) refuses to run if either guard cannot be proven.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

D0_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D0_SRC = os.path.join(D0_ROOT, "source", "src")
D0_CACHE = os.path.join(D0_ROOT, "cache")

EXPECTED_SOURCE_SHA = "eabf342e22bbf6fe876d2006a0fd86bf8cbbd75f"
EXPECTED_SOURCE_TREE = "548d302ca0f6e60034e62f4486380b901d4a9ac3"
# src/ is byte-identical to the H2 export basis dd26c4fe (that merge was docs-only), so D0 timings are
# directly comparable to H2's. video_analysis.py git blob: 67f54b9d84ca93422538139011bec5495f871925
EXPECTED_VIDEO_ANALYSIS_SHA256 = "c88e19d619df84343820fb9d70d8819d09925b93a92e88f6dfd6c6183d23c863"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def bootstrap_syspath() -> list[str]:
    """Strip every foreign `src` entry (the `._pth` hazard) and put the D0 export first."""
    wanted = _norm(D0_SRC)
    dropped = []
    kept = []
    for entry in sys.path:
        if not entry:
            kept.append(entry)
            continue
        normalised = _norm(entry)
        if os.path.basename(normalised) == "src" and normalised != wanted:
            dropped.append(entry)
            continue
        kept.append(entry)
    sys.path[:] = [D0_SRC] + [p for p in kept if _norm(p) != wanted]
    return dropped


def assert_source_bytes() -> str:
    """Refuse to run unless the exported video_analysis.py is the exact reviewed bytes."""
    path = os.path.join(D0_SRC, "video_analysis.py")
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != EXPECTED_VIDEO_ANALYSIS_SHA256:
        raise SystemExit(
            f"D0 PROVENANCE FAILURE: video_analysis.py sha256 {digest}, "
            f"expected {EXPECTED_VIDEO_ANALYSIS_SHA256}"
        )
    return digest


def assert_provenance(module, *, require_cache_redirect: bool = True) -> dict:
    """Refuse to continue unless the loaded module and its cache directory are the D0 ones."""
    loaded = _norm(module.__file__)
    expected_prefix = _norm(D0_SRC)
    if not loaded.startswith(expected_prefix):
        raise SystemExit(
            f"D0 PROVENANCE FAILURE: {module.__name__} loaded from {module.__file__!r}, "
            f"expected under {D0_SRC!r}"
        )
    info = {"module": module.__name__, "file": module.__file__}
    if require_cache_redirect:
        cache_dir = _norm(module.VIDEO_ANALYSIS_CACHE_DIR)
        if not cache_dir.startswith(_norm(D0_ROOT)):
            raise SystemExit(
                f"D0 PROVENANCE FAILURE: cache dir {module.VIDEO_ANALYSIS_CACHE_DIR!r} "
                f"is outside D0 scratch {D0_ROOT!r}"
            )
        info["cache_dir"] = module.VIDEO_ANALYSIS_CACHE_DIR
    return info


def redirect_cache_dir(module) -> str:
    """Point `_cache_path`'s destination at D0 scratch. No payload is ever read or written."""
    os.makedirs(D0_CACHE, exist_ok=True)
    module.VIDEO_ANALYSIS_CACHE_DIR = D0_CACHE
    return D0_CACHE


def manifest_digest(entries: list[dict]) -> str:
    """Collision-safe digest over the ordered (path, size, mtime_ns) manifest.

    Byte-for-byte the H2 formula, so D0's manifest hash is directly comparable to
    96bc14cd9014a5b9fd3fa8445494ab0fd88c26ef6909927f21c1ca2f0ab47c6c.
    """
    digest = hashlib.sha256()
    for item in entries:
        digest.update(
            f"{item['path']}\x00{item['size']}\x00{item['mtime_ns']}\x1e".encode("utf-8")
        )
    return digest.hexdigest()


def ordered_digest(values) -> str:
    """Collision-safe digest over an ordered result list, position-sensitive, None-aware."""
    digest = hashlib.sha256()
    for index, value in enumerate(values):
        token = "\x00NONE\x00" if value is None else str(value)
        digest.update(f"{index}\x00{token}\x1e".encode("utf-8"))
    return digest.hexdigest()


def write_json(path: str, payload) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
