"""R7.2 Audio review: **one** hover gallery and **one** sorting policy, shared by two tabs.

Two surfaces review exported audio clips -- the Raw Audio Cutter's own review half, and the
standalone Audio Preview tab -- and before R7.2 only the first existed. Rather than grow a second
gallery beside it, everything that is not Gradio and not HTTP lives here and both tabs call it:
enumeration, paging, the tile markup, the decision vocabulary, the sort plan and the destination
rules. A behaviour that is wrong is wrong in one place.

Stdlib only (`hashlib`, `html`, `os`), per ``beatsync_fork``'s hard rule, so all of it is testable
on a bare interpreter with no Gradio, no FFmpeg and no browser.

Why the audio is not in the page any more
-----------------------------------------
R7.1 embedded each clip as a base64 data URI under a cumulative byte budget. That worked for a page
of short voice clips and failed exactly as a budget must: an operator's perfectly valid clip became
unplayable because *earlier* clips had spent the page's allowance, and the tile said so in words
("not loaded for preview on this page") that describe a page limit rather than anything about that
clip. Reported from real use, and the wrong trade for a review tool.

So a tile now carries a **URL** and no bytes, and the player is created on hover. The URL is an
opaque ``(session, page, clip)`` triple minted by :class:`beatsync_fork.clips_review.ClipRegistry`
-- the same registry, the same six authorization gates and the same descriptor-identity
revalidation the R5 Clips Preview already uses, reached through ``ClipRegistry.publish_page``'s
``servable`` allowlist. **No filesystem path travels through the browser**, Gradio's
``allowed_paths`` is untouched, and page weight is now independent of clip size: a folder of
hour-long WAVs costs the same markup as a folder of one-second ones.

What that changes about honesty in the UI
-----------------------------------------
The R7.1 message conflated two different facts. They are now separate, because an operator can act
on one and not the other:

* **a loading limit** -- "this page would not inline it" -- no longer exists at all;
* **a real decode or playback failure** -- the browser fetched the bytes and could not play them --
  is reported by the client from the ``<audio>`` element's own error, and the clip stays
  classifiable, because an unplayable clip is still a clip the operator may want to keep or bin.
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
from dataclasses import dataclass
from typing import Any, Iterable

from beatsync_fork import audio_library
from beatsync_fork import clips_review as core
from beatsync_fork import collision

# ---------------------------------------------------------------------------------------------
# Vocabulary -- deliberately the SAME three words the Clips Preview uses
# ---------------------------------------------------------------------------------------------

PENDING = core.PENDING
SELECTED = core.SELECTED
REJECTED = core.REJECTED
DECISIONS = core.DECISIONS

#: Audio containers this gallery will enumerate and serve. Broader than what the Raw Audio Cutter
#: writes (WAV/FLAC/MP3) because the Audio Preview tab browses folders it did not create, and
#: narrower than "any audio file" because every entry here is a container an ordinary Chromium can
#: actually decode -- enumerating something the browser cannot play would reintroduce the confusion
#: this module exists to remove. It is an **allowlist**, passed to ``publish_page``, so a manifest
#: or a sidecar beside the clips can never enter a page.
SERVABLE_SUFFIXES = (".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus")

#: Deliberately not `clips_review._CONTENT_TYPES`: that map answers for video and a wrong
#: `Content-Type` on an audio response is exactly the kind of thing a browser silently refuses to
#: decode. `.opus` is served as `audio/ogg` because that is the container it arrives in.
_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}


def content_type_for(suffix: Any) -> str:
    """The audio media type for a suffix, or a type no browser will guess at."""
    return _CONTENT_TYPES.get(str(suffix or "").casefold(), "application/octet-stream")


#: Tiles per page. A page is a *review* unit, not a capacity limit: decisions are kept by file
#: identity rather than by page, so paging through a thousand clips preserves every one of them.
DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = core.MAX_CLIPS_PER_PAGE


# ---------------------------------------------------------------------------------------------
# Discovery -- the same ordering authority, never a second tree walk
# ---------------------------------------------------------------------------------------------

def discover_audio(folder: Any, recursive: Any) -> core.Discovery:
    """Enumerate reviewable audio under ``folder``, in the project's one documented order.

    This does **not** walk the tree itself, for the same reason
    :func:`beatsync_fork.clips_review.discover_clips` does not: ``input_manager.scan_folder`` is
    the single ordering policy in this repository, and a second traversal would be a second policy
    that eventually disagrees with the first about "the files, in order".

    Every failure is a :class:`~beatsync_fork.clips_review.Discovery` carrying ``error`` prose --
    never an exception -- because every caller is a Gradio event handler.
    """
    from beatsync_fork import input_manager

    if not isinstance(folder, str) or not folder.strip():
        return core.Discovery(recursive=bool(recursive),
                              error="Enter the folder that holds the audio clips.")
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
    if not os.path.exists(root):
        return core.Discovery(root=root, recursive=bool(recursive),
                              error=f"Folder not found:\n{root}")
    if not os.path.isdir(root):
        return core.Discovery(root=root, recursive=bool(recursive), error=f"Not a folder:\n{root}")
    try:
        found = input_manager.scan_folder(root, recursive=bool(recursive),
                                          extensions=SERVABLE_SUFFIXES,
                                          detect_duplicates=False)
    except input_manager.InputScanError as exc:
        return core.Discovery(root=root, recursive=bool(recursive), error=str(exc))
    except OSError as exc:
        return core.Discovery(root=root, recursive=bool(recursive),
                              error=f"Could not read the folder: {exc}")
    return core.Discovery(root=found.root, recursive=bool(recursive), files=found.files,
                          rejected=found.rejected, discovered_count=found.discovered_count,
                          scan_seconds=found.scan_seconds)


def resolve_page_size(value: Any) -> int:
    """Total over anything a Number widget can hand over, including `None` and `''`."""
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(size, MAX_PAGE_SIZE))


def page_count(total: Any, size: Any) -> int:
    """How many pages ``total`` files make at ``size`` per page. Never zero, so "page 1 of 1" is
    what an empty folder reports rather than "page 1 of 0"."""
    try:
        count = max(0, int(total))
    except (TypeError, ValueError):
        count = 0
    per = resolve_page_size(size)
    return max(1, (count + per - 1) // per)


def resolve_page(value: Any, total: Any, size: Any) -> int:
    """Clamp a requested 1-based page number into range.

    Clamping rather than refusing is deliberate: the page the operator is on becomes out of range
    whenever a batch is sorted and the folder shrinks, and an out-of-range page must show the last
    real one instead of an error.
    """
    pages = page_count(total, size)
    try:
        page = int(float(value))
    except (TypeError, ValueError):
        page = 1
    return max(1, min(page, pages))


def page_slice(page: Any, total: Any, size: Any) -> tuple[int, int]:
    """``(start_index, count)`` for a 1-based page, both already clamped to ``total``."""
    per = resolve_page_size(size)
    current = resolve_page(page, total, per)
    try:
        count = max(0, int(total))
    except (TypeError, ValueError):
        count = 0
    start = min((current - 1) * per, max(0, count))
    return start, min(per, max(0, count - start))


def freeze_paths(paths: Iterable[Any], start: int, count: int
                 ) -> list[tuple[int, str, int, int, int, int]]:
    """Turn a slice of an ordered path list into the tuples ``publish_page`` wants.

    ``st_dev``/``st_ino`` are captured **here**, so the serve path can later compare the *opened
    handle* against them rather than re-reading the pathname. A file that cannot be stat'ed right
    now is skipped rather than published with a guessed identity -- publishing one would mean the
    first fetch answered 409 for a file that was simply busy at scan time.

    Takes plain paths rather than discovery records because both callers have a different source
    for the list: the cutter knows exactly what it just wrote, and the Audio Preview tab has an
    ``InputSet``. The *order* is the caller's, already frozen, and nothing here sorts or re-reads
    a directory.
    """
    out: list[tuple[int, str, int, int, int, int]] = []
    rows = list(paths)
    begin = max(0, int(start))
    for offset, path in enumerate(rows[begin:begin + max(0, int(count))]):
        text = str(path)
        try:
            info = os.stat(text)
        except OSError:
            continue
        out.append((begin + offset + 1, text, info.st_size, info.st_mtime_ns,
                    info.st_dev, info.st_ino))
    return out


# ---------------------------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------------------------

def format_timecode(seconds: Any) -> str:
    """``M:SS.s`` under an hour, ``H:MM:SS.s`` beyond it. Mirrors the cutter's own formatter so the
    two surfaces never show the same length two ways."""
    try:
        value = float(seconds or 0.0)
    except (TypeError, ValueError):
        return "—"
    # NaN is tested BEFORE the clamp, and that order is the whole of this guard. `max(0.0, nan)`
    # returns `0.0` on CPython -- every comparison with NaN is false, so `max` keeps its first
    # argument -- which would have turned an unknown length into a confident "0:00.0" and made the
    # check below unreachable. Measured, not reasoned about.
    if value != value:
        return "—"
    total = max(0.0, value)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    rest = total - hours * 3600 - minutes * 60
    if hours:
        return f"{hours}:{minutes:02d}:{rest:04.1f}"
    return f"{minutes}:{rest:04.1f}"


@dataclass(frozen=True)
class AudioTile:
    """One tile's presentation, already decided by the server.

    ``name`` is the **editable destination name**, which is the one place an audio review
    legitimately differs from the video one: the operator is naming files, so the name has to be on
    screen and in an input they can type into. It is escaped on the way into the markup like
    everything else, so a filename still cannot carry script into the page.
    """

    clip_id: str
    number: int
    name: str
    duration: float | None = None
    span: str = ""


_CLASSES = {PENDING: "r7-pending", SELECTED: "r7-selected", REJECTED: "r7-rejected"}
_BADGES = {PENDING: "?", SELECTED: "✓", REJECTED: "✗"}

#: The Raw Audio Cutter's review half serves through this prefix...
MEDIA_PREFIX_CUTTER = "/r7audio"
#: ...and the standalone Audio Preview tab through this one.
#:
#: **Two prefixes because there are two registries, and two registries because
#: ``ClipRegistry.new_session`` revokes every previous session that owner holds.** Sharing one
#: registry between the two surfaces would mean that scanning a folder in Audio Preview silently
#: revoked the cutter's just-cut gallery -- every tile in it answering 403 with nothing on screen
#: to say why. The two features are independent reviews and now have independent authorization.
MEDIA_PREFIX_PREVIEW = "/r7preview"

#: Back-compatible alias for the cutter's prefix, which is the one that existed first.
MEDIA_PREFIX = MEDIA_PREFIX_CUTTER

MEDIA_PREFIXES = (MEDIA_PREFIX_CUTTER, MEDIA_PREFIX_PREVIEW)


def media_url(session_id: Any, page_token: Any, clip_id: Any,
              prefix: str = MEDIA_PREFIX_CUTTER) -> str:
    """The opaque triple a tile fetches its bytes from. No path, no filename, no index."""
    return f"{prefix}/{session_id}/{page_token}/{clip_id}"


def _escape(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def tile_html(tile: AudioTile, decision: str, session_id: str, page_token: str,
              media_prefix: str = MEDIA_PREFIX_CUTTER) -> str:
    """One tile: a name, a length, a decision badge, and a URL to fetch audio from on hover.

    There is no ``<audio>`` element here, and that absence is the whole of the R7.2 fix. A player
    is created by the client when the pointer arrives and torn down when it leaves, so the page
    holds at most one decoded clip however many tiles it shows.
    """
    state = decision if decision in _CLASSES else PENDING
    url = media_url(session_id, page_token, tile.clip_id, media_prefix)
    length = format_timecode(tile.duration) if tile.duration is not None else "—"
    span = (f'<span class="r7-src">{_escape(tile.span)}</span>') if tile.span else ""
    return (
        f'<div class="r7-tile {_CLASSES[state]}" tabindex="0" role="button"'
        f' data-idx="{_escape(tile.clip_id)}"'
        f' data-audio="{_escape(url)}"'
        f' data-decision="{_escape(state)}"'
        f' data-dur="{float(tile.duration or 0.0):.3f}"'
        f' data-name="{_escape(tile.name)}">'
        f'<div class="r7-head">'
        f'<span class="r7-num">{int(tile.number)}</span>'
        f'<span class="r7-badge">{_BADGES[state]}</span>'
        f'</div>'
        f'<div class="r7-time">{_escape(length)} {span}</div>'
        f'<div class="r7-wave"><span class="r7-state">hover to play</span></div>'
        f'<input class="r7-name" type="text" value="{_escape(tile.name)}"'
        f' aria-label="file name" spellcheck="false">'
        f'</div>')


#: The Raw Audio Cutter's review half. Kept as the historical id so its CSS and the existing
#: `#r7-grid` selectors are unchanged.
GRID_CUTTER = "r7-grid"
#: The standalone Audio Preview tab's grid.
GRID_PREVIEW = "r7-grid-preview"


def gallery_html(tiles: Iterable[AudioTile], decisions: Any, session_id: Any, page_token: Any,
                 grid_id: str = GRID_CUTTER, store_id: str = "audio-decisions",
                 counts_id: str = "audio-counts",
                 media_prefix: str = MEDIA_PREFIX_CUTTER,
                 empty_message: str = "No clips to review. Cut a recording first.") -> str:
    """The whole grid.

    **Every grid identifies its own machinery, and that is what lets two of them coexist.** R7.2
    puts a second gallery on the screen, and the R7.1 client found its hidden decisions field and
    its counts box by hard-coded document-wide id. Two grids under that scheme would both write the
    Audio Cutter's field, so the Audio Preview tab's decisions would land on the cutter's clips --
    silently, and only when both tabs had been used. The grid therefore carries ``data-store`` and
    ``data-counts``, the client reads them off the grid the tile belongs to, and a third gallery
    would need no client change at all.

    Carries a **nonce**, for the measured reason the Clips Preview grid does: a ``gr.HTML`` update
    whose markup is byte-identical to the previous value is not delivered by the pinned Gradio, so
    without one "repaint from what the server holds" can silently do nothing -- which is exactly
    what a Cancel, a re-scan onto an unchanged page, or a partial sort needs to be able to do.
    """
    rows = list(tiles)
    nonce = secrets.token_hex(4)
    if not rows:
        # The empty state carries the nonce too: "this folder is now empty" is a repaint that must
        # be delivered, and it is precisely the one a fully-sorted batch produces.
        return (f'<div class="r7-empty" data-nonce="{nonce}">{_escape(empty_message)}</div>')
    current = dict(decisions or {})
    body = "".join(tile_html(tile, current.get(tile.clip_id, PENDING),
                             str(session_id), str(page_token), media_prefix)
                   for tile in rows)
    return (f'<div class="r7-grid" id="{_escape(grid_id)}" data-count="{len(rows)}"'
            f' data-session="{_escape(session_id)}" data-page="{_escape(page_token)}"'
            f' data-store="{_escape(store_id)}" data-counts="{_escape(counts_id)}"'
            f' data-nonce="{nonce}">{body}</div>')


def decision_counts(tiles: Iterable[AudioTile], decisions: Any) -> dict:
    """``{SELECTED, REJECTED, PENDING, TOTAL}`` for a page, counted from the SERVER's tile list.

    Counting from the tiles rather than from the decision map is deliberate and load-bearing: a
    decision for a clip that is not on this page must not be able to inflate a total, and a clip
    with no decision must count as pending rather than vanish.
    """
    current = dict(decisions or {})
    counts = {SELECTED: 0, REJECTED: 0, PENDING: 0}
    rows = list(tiles)
    for tile in rows:
        decision = current.get(tile.clip_id, PENDING)
        counts[decision if decision in counts else PENDING] += 1
    counts["TOTAL"] = len(rows)
    return counts


def counts_line(counts: dict) -> str:
    return (f"Keep {counts.get(SELECTED, 0)}   ·   Discard {counts.get(REJECTED, 0)}"
            f"   ·   Undecided {counts.get(PENDING, 0)}"
            f"   ·   Total {counts.get('TOTAL', 0)}")


# ---------------------------------------------------------------------------------------------
# The sort plan  [R7.2 Phase 3]
#
# The operator's actual request: mark the GOOD clips and press Confirm. Clicking Reject on every
# unwanted clip is work the tool should not demand, and R7.1 demanded it -- Confirm refused while
# anything was undecided.
#
# So Confirm becomes available on one kept clip, and the unmarked remainder is treated as
# discarded. That is a destructive-looking default, so it is gated rather than assumed: the counts
# are shown BEFORE anything moves, the operator confirms that specific batch once, and a batch that
# changed afterwards needs confirming again. `signature` is what makes "that batch" mean something.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SortPlan:
    """What a Confirm would do, decided before anything moves.

    Three disjoint groups, deliberately kept apart all the way into the report rather than merged
    into one "discarded" number: an operator who is told "14 discarded" cannot tell whether they
    rejected 14 clips or forgot to look at 12 of them.
    """

    keep: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    unmarked: tuple[str, ...] = ()
    signature: str = ""

    @property
    def total(self) -> int:
        return len(self.keep) + len(self.rejected) + len(self.unmarked)

    @property
    def discarded(self) -> tuple[str, ...]:
        """Everything that would move to the Discarded folder, explicit and unmarked alike."""
        return tuple(self.rejected) + tuple(self.unmarked)

    @property
    def has_unmarked(self) -> bool:
        return bool(self.unmarked)

    @property
    def needs_library(self) -> bool:
        return bool(self.keep)

    @property
    def needs_discarded(self) -> bool:
        return bool(self.rejected) or bool(self.unmarked)

    @property
    def can_sort(self) -> bool:
        """At least one clip is kept. This is the whole admission rule -- see the module note."""
        return bool(self.keep)


def plan_sort(tiles: Iterable[AudioTile], decisions: Any,
              library: Any = "", discarded: Any = "") -> SortPlan:
    """Classify a page into keep / explicitly rejected / unmarked, and sign the result.

    The signature covers every clip id, its decision, and **both destinations**, so a one-time
    confirmation authorises exactly the batch the operator was shown. Change a decision, edit a
    destination, re-scan onto a different page: the signature moves and the confirmation is void.
    It is a change detector, not a credential -- nothing is authorised by presenting it, and the
    caller still re-derives the plan and re-validates the folders before moving a byte.
    """
    current = dict(decisions or {})
    keep, rejected, unmarked = [], [], []
    for tile in tiles:
        decision = current.get(tile.clip_id, PENDING)
        if decision == SELECTED:
            keep.append(tile.clip_id)
        elif decision == REJECTED:
            rejected.append(tile.clip_id)
        else:
            unmarked.append(tile.clip_id)

    material = "\n".join([
        f"library={os.path.normcase(str(library or ''))}",
        f"discarded={os.path.normcase(str(discarded or ''))}",
        *(f"{SELECTED}:{clip}" for clip in sorted(keep)),
        *(f"{REJECTED}:{clip}" for clip in sorted(rejected)),
        *(f"{PENDING}:{clip}" for clip in sorted(unmarked)),
    ])
    return SortPlan(keep=tuple(keep), rejected=tuple(rejected), unmarked=tuple(unmarked),
                    signature=hashlib.sha256(material.encode("utf-8")).hexdigest())


def plan_preview_lines(plan: SortPlan, library: Any = "", discarded: Any = "") -> list[str]:
    """What is about to happen, in the operator's words, before it happens.

    The last line is not reassurance padding: the whole reason an unmarked clip may be swept into
    the Discarded folder is that doing so destroys nothing, and the operator has to be able to see
    that stated at the moment they are asked to agree to it.
    """
    lines = [f"{len(plan.keep)} clip(s) selected to KEEP."]
    if library:
        lines.append(f"    → {library}")
    lines.append(f"{len(plan.rejected)} clip(s) explicitly rejected.")
    if plan.unmarked:
        lines.append(f"{len(plan.unmarked)} unmarked clip(s) will ALSO be moved to Discarded.")
    if plan.needs_discarded and discarded:
        lines.append(f"    → {discarded}")
    lines.append("Nothing will be permanently deleted.")
    return lines


# ---------------------------------------------------------------------------------------------
# Destinations  [R7.2 Phase 4]
# ---------------------------------------------------------------------------------------------

#: A verdict code is a stable machine token; this is what it means to a person. The operator's
#: reported problem -- both destination boxes set to the same folder, and sorting proceeding anyway
#: -- is the first row, and it now cannot happen: the check runs on change AND again at Confirm.
REFUSAL_PROSE = {
    "destinations_identical": (
        "The Media Library and the Discarded folder are the SAME folder. Kept and discarded "
        "clips would end up mixed together, so nothing will be sorted until they differ."),
    "destinations_nested": (
        "One destination folder is inside the other. Clips filed into the inner one would appear "
        "to be in both, so nothing will be sorted until they are separate folders."),
    "destination_inside_input": (
        "A destination folder is inside the folder being reviewed. A clip filed there would be "
        "found again by the next scan, so nothing will be sorted."),
    "input_inside_destination": (
        "The folder being reviewed is inside a destination folder. Sorting would move clips "
        "around inside their own source folder, so nothing will be sorted."),
    "library_is_input": (
        "The Media Library is the folder being reviewed, so a kept clip would have nowhere to go. "
        "Choose a different folder."),
    "rejected_is_input": (
        "The Discarded folder is the folder being reviewed, so a discarded clip would have "
        "nowhere to go. Choose a different folder."),
    "protected_runtime_destination": (
        "That folder is one BeatSync manages itself, and files placed there can be cleared "
        "automatically. Choose a folder of your own."),
    "unresolved_reparse_point": (
        "Windows could not resolve that folder's shortcut or junction to a real location, so it "
        "cannot be checked against the other folders. Enter the real path instead."),
    "input_not_a_directory": "The folder being reviewed does not exist.",
    "empty_path": "That folder has not been set.",
    "unresolvable_path": "That path could not be resolved.",
    "identity_unprovable": "Windows would not report that folder's identity, so it cannot be checked.",
}


def refusal_text(verdict: core.Verdict) -> str:
    """Operator prose for a verdict, falling back to the code rather than inventing a reason."""
    prose = REFUSAL_PROSE.get(verdict.code)
    if prose:
        return prose
    return f"Those folders were refused ({verdict.code})."


def validate_destinations(review_root: Any, library: Any, discarded: Any, app_root: str = "",
                          need_library: bool = True,
                          need_discarded: bool = True) -> core.Verdict:
    """Read-only check of the destinations a sort would actually write. Creates nothing.

    **Which check applies depends on how many destinations will be written, and this is the one
    place that decides.** When both will be, the full pairwise
    :func:`~beatsync_fork.clips_review.validate_roots` runs -- that is what catches the identical
    and nested cases the operator reported. When only one will be,
    :func:`~beatsync_fork.clips_review.validate_one_destination` applies every rule that can be
    stated about a single folder, so an operator who keeps everything is still never asked to name
    a quarantine folder they will not use.

    Both paths resolve real paths, compare with ``commonpath`` rather than string prefixes, and
    cross-check the Windows ``(st_dev, st_ino)`` pair, so a junction, a ``subst`` drive, a hard
    link and a difference of letter case are all the *same* folder here.
    """
    if not need_library and not need_discarded:
        return core.Verdict(True, "nothing_to_validate")

    typed_library = str(library or "").strip().strip('"')
    typed_discarded = str(discarded or "").strip().strip('"')

    # Both set: check the pair, even when only one will be written. Catching "these two are the
    # same folder" while the operator is still typing is the point of Phase 4, and it costs
    # nothing to answer a question whose inputs are both present.
    if typed_library and typed_discarded:
        verdict, _ = core.validate_roots(review_root, typed_library, typed_discarded,
                                         app_root=app_root)
        return verdict
    if need_library and need_discarded:
        # One of them is missing and both are needed; report the missing one specifically.
        missing = "Media Library" if not typed_library else "Discarded folder"
        return core.Verdict(False, "empty_path", missing)
    if need_library:
        verdict, _ = core.validate_one_destination(review_root, typed_library,
                                                   core.ROLE_LIBRARY, app_root=app_root)
        return verdict
    verdict, _ = core.validate_one_destination(review_root, typed_discarded,
                                               core.ROLE_REJECTED, app_root=app_root)
    return verdict


def destination_feedback(review_root: Any, library: Any, discarded: Any,
                         app_root: str = "") -> tuple[bool, str]:
    """``(ok, message)`` for the live read-out beside the two destination boxes.

    This runs on every edit, so it must be *informative* about an unset folder rather than
    complaining about one: a half-typed configuration is the normal state of a form, not an error.
    The gallery stays usable throughout -- only sorting is withheld.
    """
    typed_library = str(library or "").strip().strip('"')
    typed_discarded = str(discarded or "").strip().strip('"')
    if not typed_library and not typed_discarded:
        return False, "Set a Media Library folder and a Discarded folder before sorting."
    if not typed_library:
        return False, "Set the Media Library folder (where KEPT clips go) before sorting."
    if not typed_discarded:
        return False, "Set the Discarded folder (quarantine for everything else) before sorting."

    # The destination PAIR is checked first and on its own, because that question is answerable
    # before anything has been scanned -- and "these two are the same folder" is precisely the
    # problem Phase 4 exists to surface while the operator is still typing. Running the full
    # three-root check first would refuse with `empty_path` for the missing review folder and never
    # reach the comparison the operator needs to see.
    pair = core.compare_destinations(typed_library, typed_discarded)
    if not pair.ok:
        detail = f"\n{pair.detail}" if pair.detail else ""
        return False, f"❌ {refusal_text(pair)}{detail}"

    if not str(review_root or "").strip():
        return True, ("The two folders are different. They will be checked against the review "
                      "folder as well when you scan one.")

    verdict = validate_destinations(review_root, typed_library, typed_discarded,
                                    app_root=app_root, need_library=True, need_discarded=True)
    if not verdict.ok:
        detail = f"\n{verdict.detail}" if verdict.detail else ""
        return False, f"❌ {refusal_text(verdict)}{detail}"
    return True, "✅ Both folders are usable and separate. Nothing has been created or moved."


# ---------------------------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------------------------

MOVED = audio_library.MOVED
REFUSED = audio_library.REFUSED


@dataclass
class FilingOutcome:
    """One clip's result. Every status but :data:`MOVED` left the file exactly where it was."""

    clip_id: str
    label: str
    decision: str
    status: str
    destination: str = ""
    detail: str = ""
    automatic: bool = False

    @property
    def ok(self) -> bool:
        return self.status == MOVED


def file_entry(entry: core.ClipEntry, destination_folder: str,
               preferred_name: str = "",
               policy: str = collision.DEFAULT_POLICY) -> FilingOutcome:
    """Move one enumerated clip, revalidating its identity first.

    Identity is re-checked against what the scan recorded immediately before the move, so a file
    that was replaced or re-recorded while the operator was reviewing is **skipped** rather than
    filed on a decision that describes different audio. That matters more here than it does for a
    read-only preview: this one moves the file.

    The move itself is :func:`beatsync_fork.audio_library.file_clip`, unchanged, so all three of
    its guarantees still hold and are stated in one place: ``os.rename`` refuses an existing
    destination rather than overwriting, raises across volumes rather than degrading into
    copy-then-delete, and nothing on any path through it deletes anything.
    """
    outcome = FilingOutcome(clip_id=entry.clip_id, label=os.path.basename(entry.path),
                            decision="", status=REFUSED)
    try:
        live = os.stat(entry.path)
    except FileNotFoundError:
        outcome.detail = "it is no longer at its scanned location. Nothing was changed."
        return outcome
    except OSError as exc:
        outcome.detail = f"{core.io_detail(exc)}. Nothing was changed."
        return outcome

    if (live.st_size != entry.size or live.st_mtime_ns != entry.mtime_ns
            or (entry.ino not in (-1, 0) and live.st_ino != entry.ino)):
        outcome.detail = ("it changed after it was scanned, so the decision may not describe it. "
                          "Nothing was changed.")
        return outcome

    requested = audio_library.target_name(preferred_name, entry.path)
    try:
        destination = audio_library.file_clip(entry.path, destination_folder, preferred_name,
                                              policy)
    except audio_library.FilingError as failure:
        outcome.detail = str(failure)
        return outcome
    outcome.status = MOVED
    outcome.destination = destination
    # [FORK] R8: say so when the written name is not the requested one. Derived by comparing the
    # two names rather than by trusting the policy, so the note describes what actually happened
    # on disk -- under SUFFIX most clips still keep their own name and must not be annotated.
    if os.path.basename(destination) != requested:
        outcome.detail = (f"renamed to {os.path.basename(destination)} — "
                          f"{requested} was already there")
    return outcome


def sort_clips(entries: Iterable[core.ClipEntry], decision_of, name_of,
               destinations: dict, unmarked_as: str = REJECTED,
               policy: str = collision.DEFAULT_POLICY) -> list[FilingOutcome]:
    """File every clip the plan accounts for. Returns one outcome per clip, in order.

    ``decision_of(entry) -> str`` and ``name_of(entry) -> str`` read the **server's** state;
    nothing here looks at markup, a tile class or a path the browser sent.

    ``unmarked_as`` is how a `PENDING` clip is treated, and it is a parameter precisely so the
    sweep is never implicit: a caller that has not obtained the operator's one-time confirmation
    passes :data:`PENDING`, and every unmarked clip is then left alone rather than quietly binned.

    One clip failing does not stop the others and does not undo the ones already moved -- each
    rename is independently complete the moment it returns, so a partial batch is a real partial
    result and is reported as one.
    """
    outcomes: list[FilingOutcome] = []
    made: set[str] = set()
    for entry in entries:
        decision = decision_of(entry)
        automatic = False
        if decision not in (SELECTED, REJECTED):
            if unmarked_as not in (SELECTED, REJECTED):
                outcomes.append(FilingOutcome(
                    entry.clip_id, os.path.basename(entry.path), PENDING, REFUSED,
                    detail="it is still undecided, so it was left exactly where it is."))
                continue
            decision = unmarked_as
            automatic = True

        folder = str(destinations.get(decision) or "")
        if not folder:
            outcomes.append(FilingOutcome(
                entry.clip_id, os.path.basename(entry.path), decision, REFUSED,
                detail="no destination folder is set for that decision.", automatic=automatic))
            continue
        if folder not in made:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                outcomes.append(FilingOutcome(
                    entry.clip_id, os.path.basename(entry.path), decision, REFUSED,
                    detail=f"{core.io_detail(exc)}. Nothing was changed.", automatic=automatic))
                continue
            made.add(folder)
        outcome = file_entry(entry, folder, name_of(entry), policy)
        outcome.decision = decision
        outcome.automatic = automatic
        outcomes.append(outcome)
    return outcomes


def sorted_report_lines(outcomes: Iterable[FilingOutcome], library: Any = "",
                        discarded: Any = "") -> list[str]:
    """The four counts the owner asked to see, kept distinct, plus whatever did not move.

    "Automatic" discards are reported **separately** from explicit ones for the whole length of the
    report. Collapsing them would hide the one number an operator needs to sanity-check after
    agreeing to a sweep.
    """
    rows = list(outcomes)
    moved = [o for o in rows if o.ok]
    kept = [o for o in moved if o.decision == SELECTED]
    binned = [o for o in moved if o.decision == REJECTED and not o.automatic]
    swept = [o for o in moved if o.decision == REJECTED and o.automatic]
    stayed = [o for o in rows if not o.ok]

    lines = []
    if moved and not stayed:
        lines.append(f"✅ Sorted all {len(moved)} clip(s).")
    elif moved:
        lines.append(f"⚠️ Sorted {len(moved)} of {len(rows)} clip(s). "
                     f"{len(stayed)} stayed where they were.")
    else:
        lines.append("❌ Nothing was moved.")
    lines.append("")
    lines.append(f"Kept: {len(kept)}" + (f"   → {library}" if library and kept else ""))
    lines.append(f"Discarded (you rejected these): {len(binned)}")
    lines.append(f"Discarded (unmarked, swept by your confirmation): {len(swept)}")
    if discarded and (binned or swept):
        lines.append(f"   → {discarded}")
    lines.append(f"Failed: {len(stayed)}")
    if stayed:
        lines.append("")
        lines.append("Left in place:")
        for outcome in stayed:
            lines.append(f"  • {outcome.label} — {outcome.detail}")
        lines.append("")
        lines.append("Nothing was overwritten, renamed or deleted. The clips that did not move "
                     "are still on screen, so you can fix the problem and press Confirm again.")
    return lines


__all__ = [
    "PENDING", "SELECTED", "REJECTED", "DECISIONS",
    "SERVABLE_SUFFIXES", "content_type_for",
    "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
    "discover_audio", "resolve_page_size", "page_count", "resolve_page", "page_slice",
    "freeze_paths",
    "format_timecode", "AudioTile", "media_url", "tile_html", "gallery_html",
    "MEDIA_PREFIX", "MEDIA_PREFIX_CUTTER", "MEDIA_PREFIX_PREVIEW", "MEDIA_PREFIXES",
    "GRID_CUTTER", "GRID_PREVIEW",
    "decision_counts", "counts_line",
    "SortPlan", "plan_sort", "plan_preview_lines",
    "REFUSAL_PROSE", "refusal_text", "validate_destinations", "destination_feedback",
    "MOVED", "REFUSED", "FilingOutcome", "file_entry", "sort_clips", "sorted_report_lines",
]
