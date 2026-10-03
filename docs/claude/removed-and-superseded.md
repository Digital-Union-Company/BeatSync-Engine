# Removed and superseded instruction text

A record of what the instruction modularization dropped rather than relocated, and why it is no longer
authoritative. Nothing with a live rule in it is listed here — live rules were moved verbatim into
`.claude/rules/`, and historical-but-real evidence was moved to the other files in `docs/claude/`.

## Removed: the `### Analysis cache` summary paragraph

Dropped as **removable duplication**, not as a policy change. It was a summary of the D2 identity
contract that sat immediately above the contract itself and declared, in its own text, that it was not
the authority. Every fact in it — the key components, the GGUF/llama invalidation consequence, and
"nothing about the music is in there" — is stated with full precision in
`.claude/rules/stage5-cache-identity.md` (identity and the `cache_contract` marker) and in the
media-neutral section of `.claude/rules/stage5-worker.md`.

The second paragraph of that section, the `ANALYSIS_VERSION` bump rule, **was** a live rule and was
preserved in `.claude/rules/stage5-cache-identity.md`.

Verbatim, for the record:

> `input/video_analysis_cache/*.json` is keyed by `CACHE_CONTRACT_VERSION` + `ANALYSIS_VERSION` + source
> identity (absolute path, size, `st_mtime_ns`, bounded content fingerprint) + a backend token (or
> `no_ai`) + an effective Qwen config token covering `MAX_WINDOWS`, `FRAME_WIDTH` and `MAX_NEW_TOKENS`.
> **The exact contract — including the fingerprint windows, backend identity, fail-closed behaviour and
> the persisted `cache_contract` marker — is the D2 section immediately below; read that rather than
> this summary before changing anything.** Swapping the GGUF model or llama.cpp build still invalidates
> automatically. Nothing about the *music* is in there — see the media-neutral contract (P2) below.

## Relocated, not removed

| Text | Where it went | Why |
|---|---|---|
| D1 merge-boundary historical state, the pre-D2 `int(st_mtime)` identity, the D1-era reusable-record figures | `docs/claude/stage5-cache-history.md` | Superseded by the D2 identity transition. Real audit evidence, so retained — but it describes the D1-era loader, not current behaviour. |

## Superseded experiments still named in the live rules

These stay *mentioned* in their rule file because the mention is itself the instruction — a future
change must not resurrect them — but they are not evidence for anything:

- **The hockey human-scoring experiment** is superseded by the P2 real-material A/B and must not be
  cited as implementation evidence. (`.claude/rules/stage5-worker.md`, media-neutral section.)
- **"Earliest diagnostic line wins"** was the first FFmpeg stderr selector and was wrong; the selector
  anchors on FFmpeg's consequence lines instead. (`.claude/rules/pipeline-core.md`.)
- **`volume=eval=frame`** measured as an audible staircase and was replaced by `aevalsrc` +
  `amultiply`; **`amix normalize=1`** was rejected because it made the music level depend on the voice
  count. (`.claude/rules/audio-mixdown.md`.)
- **`-hwaccel cuda` on the NVENC clip path** (and `-hwaccel cuda -threads 8`) were rejected on
  measurement, not on feasibility. (`.claude/rules/pipeline-core.md`.)
- **Three weaker Stage-5 completion definitions** (emptiness of `semantics`, envelope membership
  alone, `tag_count == frame_count`) are each recorded as a defect not to repeat.
  (`.claude/rules/stage5-cache-durability.md`.)
