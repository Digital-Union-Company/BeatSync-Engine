# Stage-5 cache: historical state (not authoritative)

Reference material, read on demand. **Current behaviour is
`.claude/rules/stage5-cache-identity.md` (identity, the D2 contract) and
`.claude/rules/stage5-cache-durability.md` (the D1 durability invariants, which are still live).**
This file holds only the part of the D1 record that the D2 identity transition superseded: the
D1-era loader's figures and the pre-D2 `int(st_mtime)` identity. It is kept because the numbers are
real audit evidence of a real cache, and evidence is not deleted merely because a milestone
completed — but it must not be cited as current behaviour.

## The D1 merge boundary

- **Historical, at the D1 merge boundary:** D1 deliberately did **not** change `_video_signature`,
  `_path_signature_token`, `_qwen_backend_signature_token`, `_cache_path` or `ANALYSIS_VERSION`.
  `_video_signature` still used `int(stat.st_mtime)` at that point, so all pre-existing entries
  remained addressable, and 2192 of the 2196 real records remained reusable once the selective legacy
  guard landed, with 4 intentionally rejected as self-contradictory (see the stored-consistency rule
  above). Source/backend identity hardening was intentionally deferred *from* D1, because closing
  `int(st_mtime)`'s same-second collision re-keys the whole cache.

  **Current state:** the D2 identity contract above supersedes all of that. Identity now uses
  `st_mtime_ns` plus a bounded content fingerprint under `CACHE_CONTRACT_VERSION = "stage5_cache_v3"`,
  so pre-D2 records are naturally orphaned and are never reachable by a current lookup — including the
  two records whose completeness D1 could not prove, which that transition retires without a judgement
  call. P2's `v2 → v3` bump orphans the D2 generation the same way, by the same mechanism. The D1
  figures above describe the D1-era loader and cache, not current behaviour.
