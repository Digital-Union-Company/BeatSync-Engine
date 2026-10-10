"""Freeze the H2 source manifest using production enumeration semantics.

Enumeration goes through the real `beatsync_fork.input_manager.scan_folder` with
`detect_duplicates=False` -- the same call and the same flag the production render gate uses in
`check_declaration`, so no file content is read and the ordering is the production total order.
Read-only: nothing under the library is created, modified or deleted.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import d0_common as h2  # noqa: E402

LIBRARY = r"J:\New folder\Cuts"

dropped = h2.bootstrap_syspath()

from beatsync_fork import input_manager  # noqa: E402

h2.assert_provenance(input_manager, require_cache_redirect=False)

started = time.perf_counter()
input_set = input_manager.scan_folder(LIBRARY, recursive=True, detect_duplicates=False)
scan_seconds = time.perf_counter() - started

ready = [os.path.abspath(path) for path in input_set.paths]

entries = []
for path in ready:
    stat = os.stat(path)
    entries.append({"path": path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})

sizes = [item["size"] for item in entries]
FINGERPRINT_LIMIT = 3 * (1 << 20)
bounded_bytes = sum(min(size, FINGERPRINT_LIMIT) for size in sizes)

manifest = {
    "library_path": LIBRARY,
    "recursive": True,
    "extensions": sorted(input_manager.SUPPORTED_VIDEO_EXTENSIONS),
    "enumeration": "beatsync_fork.input_manager.scan_folder(recursive=True, detect_duplicates=False)",
    "enumeration_order": "production ready_paths() order (case-insensitive total order)",
    "source_count": len(entries),
    "discovered_count": input_set.discovered_count,
    "rejected_count": len(input_set.rejected),
    "scan_seconds": round(scan_seconds, 4),
    "total_file_bytes": sum(sizes),
    "min_file_bytes": min(sizes) if sizes else 0,
    "max_file_bytes": max(sizes) if sizes else 0,
    "median_file_bytes": int(statistics.median(sizes)) if sizes else 0,
    "bounded_fingerprint_bytes_approx": bounded_bytes,
    "entries": entries,
}
manifest["manifest_sha256"] = h2.manifest_digest(entries)

out = os.path.join(h2.D0_ROOT, "evidence", "source_manifest.json")
h2.write_json(out, manifest)
with open(os.path.join(h2.D0_ROOT, "evidence", "source_manifest.sha256"), "w", encoding="utf-8") as handle:
    handle.write(manifest["manifest_sha256"] + "  source_manifest.json(entries)\n")

rejected_reasons = {
    getattr(reason, "value", str(reason)): count
    for reason, count in input_set.rejected_by_reason().items()
    if count
}

print(json.dumps({
    "dropped_syspath_entries": dropped,
    "input_manager_file": input_manager.__file__,
    "source_count": manifest["source_count"],
    "discovered_count": manifest["discovered_count"],
    "rejected_reasons": rejected_reasons,
    "total_file_bytes": manifest["total_file_bytes"],
    "total_file_gib": round(manifest["total_file_bytes"] / (1 << 30), 3),
    "min_file_bytes": manifest["min_file_bytes"],
    "max_file_bytes": manifest["max_file_bytes"],
    "median_file_bytes": manifest["median_file_bytes"],
    "bounded_fingerprint_bytes_approx": bounded_bytes,
    "bounded_fingerprint_gib_approx": round(bounded_bytes / (1 << 30), 3),
    "scan_seconds": manifest["scan_seconds"],
    "manifest_sha256": manifest["manifest_sha256"],
}, indent=2))
