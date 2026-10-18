# Clips Preview Selector V1 — specification

**Status:** PLANNED · **NOT IMPLEMENTED** · design only.
No code exists. Every performance figure below is a **target to be measured**, not a result.

A fifth top-level GUI tab for reviewing clips **after** the Raw Cutter has produced them: keep the
good ones, quarantine the rest, in bulk, quickly.

---

## 1. What this is, and what it deliberately is not

| | |
|---|---|
| **Is** | post-cut media triage, in its own tab, run when the operator chooses |
| **Is NOT** | per-cut confirmation inside the Raw Cutter |

That distinction is the whole reason this is acceptable. A **Review Assistant Cutter** — approval
inside the cut loop — was considered and **rejected by the product owner**, and `ROADMAP.md`
records it. Folder Batch exists precisely so that cutting needs no interaction. This tab reviews
*output*, after the fact, and the Raw Cutter never waits for it.

---

## 2. Inputs

| Control | Default | Notes |
|---|---|---|
| **Input folder** | — | where the clips to review are |
| **Include subfolders** | **ON** | matches the Raw Cutter's own default |
| **Media Library folder** | — | destination for SELECTED clips |
| **Rejected Media folder** | — | **quarantine** for REJECTED clips |

Enumeration is **deterministic** — the same folder yields the same order every time — using the
same ordering rule the Raw Cutter's batch discovery already uses, so the two features cannot
disagree about what "the files, in order" means.

### Path relationships that must be refused, before anything is read

1. Either destination **inside** the input folder (the tool would re-ingest its own output).
2. Input **inside** either destination.
3. The two destinations identical, or one inside the other.
4. Any of the three resolving to the same location **through a junction, symlink or `subst` drive**
   — compare resolved real paths, never the typed strings.
5. A destination that is a protected runtime directory (`bin/`, `input/video_analysis_cache/`).

**Source files are not touched until an explicit batch confirmation.** Browsing, playing and
classifying are read-only.

---

## 3. Gallery UX

### Page size

**100 clips per logical page** is the product target. See §5 — it must be benchmarked before it is
promised.

Responsive grid, **3–4 clips per desktop row**.

### States

| State | Visual | Meaning |
|---|---|---|
| **PENDING** | yellow indicator, `?` | not yet decided — the starting state for every clip |
| **SELECTED** | green background/border, `✓` | goes to the Media Library |
| **REJECTED** | red background/border, `✗` | goes to quarantine |

### Interactions

| Action | Result |
|---|---|
| Hover | muted inline playback starts |
| Mouse leave | playback pauses |
| **Left click** | SELECTED |
| **Right click** | REJECTED (context menu suppressed on the tile only) |
| Explicit control | back to PENDING — a middle click or a small ↺ on the tile |
| Wheel over a hovered clip *(optional)* | scrub within the clip, bounded to its duration |
| Keyboard | **required** — arrows move focus, `S`/`Enter` selects, `R`/`Delete` rejects, `P` resets, `Space` plays/pauses |

Wheel scrubbing must **not** capture page scrolling outside the hovered tile, and must not prevent
normal scrolling when the pointer is between tiles.

> ### Colour is never the decision record
> A tile's class is a *rendering* of state. The authority is the **server-side item ID and its
> durable decision record**. A reload, a re-render, a second browser tab or a CSS change must not
> be able to alter what will be moved. The confirm step reads the server's record, never the DOM.

---

## 4. Classification and confirm

Always visible: **Selected / Rejected / Pending / Total**.

**`Confirm and load next batch` stays disabled until every item in the current page is SELECTED or
REJECTED.** Pending is not a decision, and no undecided clip is ever moved.

On confirm:

1. Selected → Media Library folder.
2. Rejected → Rejected Media folder.
3. Undecided → **impossible** (the button was disabled).
4. **No existing destination file is overwritten**, ever.
5. Outcomes are **durably recorded before** the next page loads.

**Rejected Media is quarantine, not deletion.** V1 has **no** retention expiry and **no** permanent
delete. Emptying it is the operator's decision, made outside this tool.

---

## 5. Performance

The target is 100 logical items **without 100 decoded video streams in memory**. Techniques to
evaluate:

| Technique | Purpose |
|---|---|
| **Poster thumbnails** | one cheap frame per clip, extracted once and cached; the grid renders images, not video |
| **Lazy `<video>` creation** | no media element until hover |
| **One active player at a time** | starting a new hover tears down the previous element |
| **Viewport virtualization** | only visible rows mounted |
| **Bounded metadata/proxy cache** | LRU, size-capped, keyed by clip identity (path + size + mtime) |
| **Format fallback** | some containers will not play in Chromium; fall back to the poster plus a clear "preview unavailable" marker rather than an empty box — **an unplayable clip must still be classifiable** |

### Benchmark before promising

Measure **25, 50 and 100** items in the pinned Windows + Gradio 6.19.0 environment:

- time to first interactive render
- scroll smoothness at the bottom of the page
- hover-to-first-frame latency
- seek latency when scrubbing
- peak browser memory

**100 items is a product target, not a verified claim.** If it does not hold, the page size is what
gives — not the safety rules.

---

## 6. Filesystem integrity

### Transfer contract

| Case | Method |
|---|---|
| **Same volume** | atomic **no-replace** move (`MoveFileEx` without `REPLACE_EXISTING`). Fails loudly if the target exists. |
| **Cross volume** | **stage → verify → remove**: copy to a temporary name in the destination, verify size **and** hash, atomically rename into place, and only then remove the source. |

A cross-volume move is never a single irreversible step.

### Durable intent

Before moving anything, write a per-file **intent record** (source identity, destination, decision).
After each file, write its **outcome**. A crash mid-batch is then recoverable: on restart the tool
reconciles intents against the filesystem and reports what actually happened.

### Rules that do not bend

- **Revalidate source identity immediately before moving** — size, mtime, and the existence of the
  exact path. A clip that changed since it was classified is skipped and reported, not moved.
- **Never trust a browser-supplied absolute path** as transfer authority. The browser sends an
  opaque item ID; the server resolves it against its own enumeration.
- **No silent collision resolution.** No `_1` suffixes, no overwrite. A collision is reported and
  that file stays put.
- **If any move fails, the current batch is preserved** and exact partial results are shown. The
  tool does **not** advance to the next page automatically.

---

## 7. Local file serving security

Gradio's `allowed_paths` is a blunt instrument: pointing it at a media root exposes that whole tree
to any request the browser can make.

- Prefer **bounded per-file authorization**: the server mints a **stable opaque clip ID** per
  enumerated file and serves bytes only for IDs in the current page's set.
- An ID must not be a path, and must not be guessable into one.
- **Localhost-only** unless a separately reviewed network-exposure design exists. This app already
  binds `127.0.0.1` and that stays.
- Nothing outside the enumerated set is reachable, so unrelated private media cannot be fetched by
  crafting a URL.

---

## 8. Completion contract

When every clip on the page is decided and the operator confirms:

1. Move verified **selected** clips to the Media Library.
2. Move verified **rejected** clips to quarantine.
3. Record **exact** successes and failures, per file.
4. Retain recoverable state.
5. Load the next ordered page **only once the current one is safely committed**.

When no clips remain, show a final summary: totals selected, rejected, moved, failed, skipped, and
where everything went.

---

## 9. Open questions

1. Poster extraction cost for a large folder — one FFmpeg call per clip is simple but may dominate
   first render. Worth measuring against a one-pass batch extraction.
2. Whether page size should adapt to measured performance rather than being fixed at 100.
3. Whether a "skip this page" escape is wanted, given Confirm requires a full decision set.
4. Undo after a confirmed batch: quarantine makes rejection reversible by hand, but there is no
   in-tool undo in V1, and whether that is acceptable is a product decision.
5. Whether the Raw Cutter should optionally hand its output folder straight to this tab.

---

## 10. Explicitly out of scope for V1

- Permanent deletion, retention expiry, or any automatic emptying of quarantine.
- Editing, re-cutting, trimming or re-encoding from this tab.
- Any Qwen or semantic assistance — this is manual triage. Smart Passages (R3) is a separate,
  opt-in feature and must not be mixed with Stage-5 cache identity or the existing Qwen worker.
- Network exposure of the media server.
