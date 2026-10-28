r"""Narrow, handle-bound Win32 filesystem primitive. **ISOLATED — reachable from nothing.**

Nothing in the product imports this module. It is not wired into ``clips_review_transfer``'s
``execute_transfer``, ``_same_volume`` or ``_cross_volume``, not into ``gui.py``'s
``_on_clips_confirm``, and not into any HTTP route. Wiring it up is a separate phase needing its own
authorization after independent review.

Why it exists
-------------
Every pathname-addressed filesystem operation re-resolves its pathname at the instant it runs. R5-P63
and R5-P65 measured the consequence in this repository: a decoy substituted between a containment
check and the operation is acted upon, because the *name* was checked and the *object* was not. The
fix is to stop naming the source at all — acquire a handle first, verify identity *on that handle*,
and commit through it.

``SetFileInformationByHandle(FileRenameInfo)`` is the one Win32 primitive that renames **the object
behind a handle**. The destination is a name; the source is not. That asymmetry is the whole point.

The ordering IS the security property
-------------------------------------
Measured (R5-P66, independently reproduced by R5-P67): a handle-bound rename on a handle opened
*after* a containment check binds the wrong object and binds it perfectly. The only safe order is

    ACQUIRE -> verify identity on the handle -> qualify the volume -> evaluate containment on the
    handle's OWN name -> hash from the handle -> durable intent -> commit through the handle

This module supplies those steps as separate, testable primitives. **It deliberately does not
sequence them for you**, because the sequence is product policy and this module is not where product
policy lives.

What this module deliberately does NOT decide
---------------------------------------------
* **No Contract O/L selection.** It exposes the object's identity and the handle's own final path;
  whether "still inside the input root at commit time" is required is unsettled (decision A).
* **No hardlink policy.** ``SourceHandle.identity().link_count`` is *observed and reported*. This
  module never admits or refuses a multiply-linked object — H1 vs H2 is unsettled (decision B).
* **No approval semantics.** A handle existing means an open succeeded and nothing more. Identity is
  compared only against an expectation the caller supplies.
* **No deletion, ever.** ``FileDispositionInfo``/``FileDispositionInfoEx`` are not bound, no delete
  API is bound, and the cross-volume ``SOURCE_REMOVAL_BLOCKED`` contract is untouched.
* **No replacement.** The rename is hard-wired to ``ReplaceIfExists = 0``; there is no parameter.
* **No preview pinning, no destination rollback.** Decisions C and F are unsettled; F5/DA-7 are open.

Per CLAUDE.md's hard rule this module imports only the standard library, and no upstream runtime
(``logger``, ``paths``, ``gradio``, ``numpy``, ``cupy``, ``cv2``, ``librosa``). It additionally
carries the **one** module-scoped ``ctypes`` exception to the package's stdlib allowlist; every other
module in the package is still forbidden from importing it, and
``tests/test_no_runtime_dependency.py`` enforces that statically.

Import safety
-------------
Importing this module on a non-Windows interpreter, or anywhere ``kernel32`` is unavailable, is
**safe and silent**: the DLL is bound lazily on first use, so import causes no DLL load, no handle,
no filesystem probe and no directory creation. Actual operations then fail closed with
``Win32Unavailable``.

Authorized Win32 surface
------------------------
Exactly eight documented ``kernel32`` entry points, enumerated in :data:`AUTHORIZED_ENTRY_POINTS`
and bound through :func:`_bind` so an unlisted name raises rather than resolving. No undocumented NT
API. ``GetFileInformationByHandleEx`` is used with two documented classes only: ``FileIdInfo`` (the
128-bit file id) and ``FileRemoteProtocolInfo`` (remote classification).

:func:`_bind` is the **only** place the library is resolved. There is deliberately no module-level
accessor returning the live ``WinDLL``: R5-P70 F70-4 measured that such an accessor re-exports
unguarded FFI to every other module in the package (``from ... import _library`` reaches kernel32
with no ``ctypes`` import and no ``_bind``). The loader now lives inside :func:`_bind`, after the
authorization check, so no importable handle to the library exists at all. ``restype`` and
``argtypes`` for all eight entry points are declared there too, at that one controlled point
(R5-P70 F70-9), rather than at each call site.

Destination names
-----------------
``SourceHandle.rename_no_replace`` validates its destination **before** building the rename buffer
and **before** any kernel call, because R5-P70 measured two routes by which a commit succeeded at a
name the caller never requested: a UTF-16 length computed from ``len(str)`` (which undercounts
supplementary-plane characters by 2 bytes each) and an unvalidated embedded NUL (which the *kernel*
truncates). Both reported unqualified success. See :func:`_encoded_destination_name`.

Handle ownership (R5-P70 F70-8, deliberately deferred — see below)
------------------------------------------------------------------
Every handle-returning function transfers ownership to the caller:

* :func:`open_source` returns a :class:`SourceHandle`; :func:`open_destination_directory` returns a
  :class:`DestinationDirectoryHandle`. **The caller owns it and the caller must close it.**
* Use ``with``. ``__exit__`` closes on the normal path and on an exception alike, and
  :meth:`_Handle.close` is idempotent and never raises, so a double close is safe.
* A function that *refuses* never returns a handle, and leaks none: acquisition failures raise
  before a wrapper exists, and every refusal after acquisition propagates out of the caller's
  ``with``.
* **A leaked handle is a user-visible defect, not a resource nicety.** While a ``SourceHandle`` is
  open, the object denies ``FILE_SHARE_DELETE``: an ordinary ``open()`` of it is refused (errno 13),
  and renaming or deleting its leaf is refused (winerror 32) — measured R5-P70. A caller that
  neither closes nor uses ``with`` therefore makes the object unreadable and unmovable for the rest
  of the process lifetime.

There is deliberately **no finalizer**. ``weakref.finalize`` would be the natural safety net, but
``weakref`` is not on the fork package's stdlib allowlist and adding it would widen the very import
scope this module is meant to keep narrow. F70-8 is therefore retained as an **open** item with a
precise acceptance condition, stated in ``R5_P71_ABI_AND_HANDLE_ROBUSTNESS.md``: a finalizer is
required before this primitive is wired to any caller that does not hold the handle inside a single
``with`` block in the same function that opened it.
"""
from __future__ import annotations

import ctypes
import hashlib
from dataclasses import dataclass
from typing import Callable

#: The complete authorized kernel32 surface. A ninth name is a scope violation, not a convenience.
#: `tests/test_win32_transfer_security.py` reads this tuple AND the module AST, so adding a binding
#: without adding it here fails, and adding it here without owner authorization is visible in review.
AUTHORIZED_ENTRY_POINTS = (
    "CreateFileW",
    "GetFileInformationByHandle",
    "GetFileInformationByHandleEx",
    "GetFinalPathNameByHandleW",
    "GetVolumeInformationByHandleW",
    "ReadFile",
    "SetFileInformationByHandle",
    "CloseHandle",
)

#: [R5-P71 / F70-4] The approved primitive interface — the only names another fork module may import
#: from this one, once an integration phase is authorized.
#:
#: This exists to draw a line the earlier guard did not: *importing the approved primitive* and
#: *reaching unrestricted DLL access* are different things, and only the second is forbidden. The
#: earlier design exposed a module-level accessor to the live ``WinDLL``, so any fork module could
#: reach the whole of kernel32 by importing one private name (R5-P70 F70-4, reproduced as M16). That
#: accessor is now gone, and this tuple states positively what remains importable.
#:
#: **This is a repository regression contract enforced by static analysis, not an access-control
#: boundary.** Python has no such boundary: a leading underscore prevents nothing at runtime, and
#: ``tests/test_no_runtime_dependency.py`` asserting this is a review tripwire that makes an
#: unauthorized widening visible in a diff — it does not and cannot prevent one from executing.
PUBLIC_API = (
    "AUTHORIZED_ENTRY_POINTS",
    "COMMIT_UNVERIFIED_CODES",
    "DestinationDirectoryHandle",
    "HandleIdentity",
    "PUBLIC_API",
    "REFUSAL_CODES",
    "SourceHandle",
    "VolumeQualification",
    "Win32CommitUnverified",
    "Win32Refused",
    "Win32Unavailable",
    "open_destination_directory",
    "open_source",
    "require_same_volume",
)

# ---- access rights -------------------------------------------------------------------------------
#: Least privilege for the source, as measured in R5-P66 B1: `FILE_READ_DATA | DELETE` is sufficient
#: for identity, hashing and the handle-bound rename. `GENERIC_READ` is deliberately not requested.
FILE_READ_DATA = 0x00000001
FILE_LIST_DIRECTORY = 0x00000001          # same bit, directory spelling
DELETE = 0x00010000

# ---- share modes ---------------------------------------------------------------------------------
#: Denying FILE_SHARE_DELETE is what prevents the source leaf being renamed or deleted out from
#: under the handle (R5-P66 C0, reproduced by R5-P67 A4: winerror 32 when denied).
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002

SOURCE_SHARE_MODE = FILE_SHARE_READ
#: The destination directory must deny DELETE (so it cannot be relocated) but PERMIT WRITE, or the
#: kernel cannot add the directory entry and the commit itself is refused with winerror 32.
#: Measured R5-P67 E12: only 6 of 18 access/share combinations satisfy both requirements.
DESTINATION_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE

OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

# ---- FILE_INFO_BY_HANDLE_CLASS (only these two are used) -----------------------------------------
_FileIdInfo = 18
_FileRemoteProtocolInfo = 13
#: Hard-wired: the ONLY class this module ever passes to SetFileInformationByHandle.
_FileRenameInfo = 3

# ---- GetFinalPathNameByHandleW flags -------------------------------------------------------------
VOLUME_NAME_DOS = 0x0
VOLUME_NAME_GUID = 0x1
VOLUME_NAME_NT = 0x2

# ---- volume capability flags the local-NTFS contract requires ------------------------------------
FILE_SUPPORTS_HARD_LINKS = 0x00400000
FILE_SUPPORTS_OPEN_BY_FILE_ID = 0x01000000
FILE_PERSISTENT_ACLS = 0x00000008
#: 0x80, NOT 0x100. R5-P68's human-readable flag *decode* used 0x100 here (and several other
#: wrong values); the hex flag words it recorded are correct but some of its flag NAMES were
#: mislabelled. Measured against the real word 0x03e72eff on a local NTFS volume: 0x80 is set and
#: 0x100 is not, so the wrong constant made a correct volume fail the capability gate.
FILE_SUPPORTS_REPARSE_POINTS = 0x00000080
#: Required together. `SUPPORTS_HARD_LINKS` is load-bearing rather than trivia: without it a
#: link-count observation is trivially 1 and tells a later policy layer nothing.
REQUIRED_VOLUME_FLAGS = (
    FILE_SUPPORTS_HARD_LINKS
    | FILE_SUPPORTS_OPEN_BY_FILE_ID
    | FILE_PERSISTENT_ACLS
    | FILE_SUPPORTS_REPARSE_POINTS
)

REQUIRED_FILESYSTEM = "NTFS"

_ERROR_SHARING_VIOLATION = 32
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_NOT_SAME_DEVICE = 17
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class Win32Unavailable(RuntimeError):
    """Raised when the Win32 surface is unavailable — a non-Windows platform, or no ``kernel32``.

    Importing this module never raises it; only an attempted operation does. That is what keeps the
    package importable on a bare non-Windows interpreter.
    """


class Win32Refused(Exception):
    """A fail-closed refusal.

    Carries a stable ``code`` and an optional ``winerror``, and **never a filesystem path**. Paths in
    an exception message leak into logs, HTTP bodies and journals; ``str(OSError)`` additionally
    reprs its own filenames, which is how a path reaches a caller that deliberately avoided
    formatting one. Nothing here formats a path, so there is nothing to leak.
    """

    def __init__(self, code: str, winerror: int | None = None) -> None:
        self.code = code
        self.winerror = winerror
        super().__init__(code if winerror is None else f"{code} (winerror={winerror})")


class Win32CommitUnverified(Exception):
    """The rename **COMMITTED** but the published name could not be confirmed as the requested one.

    This is deliberately **not** a :class:`Win32Refused` and deliberately not a subclass of it. A
    refusal means *nothing happened*; this means *something happened and its result is not
    confirmed*. R5-P70 measured the exact failure this guards against: a commit that succeeded at a
    truncated name while the module returned unqualified success, leaving a journal or rollback
    keyed on the requested name unable to find the object and entitled to conclude the transfer
    never occurred.

    :attr:`published` is always ``True``. The object **may be, and most likely is, live at some
    name** — possibly the requested one, with only the confirming query having failed. The caller
    must treat the publication as having occurred and reconcile from the filesystem.

    This module does **not** attempt rollback. Un-publishing would mean a second rename on a
    half-understood state, and inventing recovery inside a primitive that deliberately leaves
    sequencing to a product layer is how a bad state becomes two bad states. Decision F
    (destination authority and rollback) is unsettled and is not resolved here.
    """

    def __init__(self, code: str, winerror: int | None = None) -> None:
        self.code = code
        self.winerror = winerror
        #: Always True. The commit happened; only its confirmation did not.
        self.published = True
        super().__init__(
            f"{code} (PUBLICATION MAY HAVE OCCURRED"
            + (")" if winerror is None else f", winerror={winerror})")
        )


#: Every code :class:`Win32CommitUnverified` can carry. Disjoint from :data:`REFUSAL_CODES` by
#: construction, and asserted so in the suite: a caller that confuses the two would report a
#: publication as "nothing happened".
COMMIT_UNVERIFIED_CODES = frozenset({
    "published_name_mismatch",
    "published_name_unverifiable",
})


#: Every refusal code this module can raise. Enumerated so a caller can branch exhaustively and a
#: test can assert no undeclared code escapes. Every one of these means **nothing was committed**.
REFUSAL_CODES = frozenset({
    "source_unavailable",
    "source_shared_violation",
    "destination_directory_unavailable",
    "identity_mismatch",
    "identity_unavailable",
    "final_path_unavailable",
    "volume_query_failed",
    "volume_not_ntfs",
    "volume_missing_required_capability",
    "volume_remote",
    "volume_path_not_local_dos",
    "volume_guid_unavailable",
    "volume_device_not_local",
    "cross_volume_not_supported",
    "already_streamed",
    "stream_failed",
    #: [R5-P73 / F72-1] ``chunk_size`` is not a usable read size: not exactly an ``int``, or outside
    #: ``1 .. _MAX_STREAM_CHUNK``. Raised BEFORE the one-shot flag is burned, before any allocation
    #: and before any kernel call, so nothing was read and the handle remains usable.
    "chunk_size_invalid",
    #: [R5-P73 / F72-1] The forward pass ended having hashed a number of bytes other than the size
    #: this handle's own identity reports. A digest that does not cover the whole object is refused
    #: rather than returned, because a partial digest is indistinguishable from a complete one.
    "stream_incomplete",
    "destination_exists",
    "destination_access_denied",
    #: [R5-P71 / F70-2] The destination is not a usable Win32 name: not a `str`, empty, carrying an
    #: embedded NUL, or not encodable as strict UTF-16. Raised BEFORE the buffer is built and
    #: before any kernel call, so it is unambiguously pre-commit.
    "destination_malformed",
    "source_missing",
    "rename_failed",
    "handle_closed",
})


#: [R5-P73 / F72-1] The largest ``chunk_size`` :meth:`SourceHandle.sha256_stream` accepts, and
#: therefore the largest allocation it can be made to attempt. Also its default, so there is exactly
#: one literal and the two cannot drift apart (pinned by a test).
#:
#: 1 MiB is deliberately **not** generous. The bound exists for two measured reasons, not as style:
#:
#: * **Unbounded allocation.** R5-P72 F72-2 measured ``create_string_buffer(1 << 40)`` really being
#:   attempted on an unvalidated argument, raising ``MemoryError`` — a 1 TiB allocation request
#:   reachable from a caller-supplied number.
#: * **DWORD truncation.** ``ReadFile``'s ``nNumberOfBytesToRead`` is ``c_uint32`` and ``ctypes``
#:   truncates rather than range-checks (measured: ``c_uint32(1 << 32).value == 0``). So any
#:   positive multiple of 2**32 marshalled to a **zero-byte read request**, which on a non-empty
#:   file returns success-with-zero-bytes and is byte-for-byte indistinguishable from EOF. Capping
#:   below 2**32 makes that class unreachable by construction rather than by a second check.
#:
#: A caller needing a different trade-off passes a smaller value; nothing needs a larger one, since
#: the read loop already streams to EOF.
_MAX_STREAM_CHUNK = 1 << 20


# ---- lazy, gated binding -------------------------------------------------------------------------

_k32 = None

#: [R5-P71 / F70-9] ``restype`` and ``argtypes`` for all eight entry points, declared ONCE and
#: applied inside :func:`_bind`.
#:
#: Two reasons this is a single table rather than per-call-site assignments. First, ctypes caches
#: one ``_FuncPtr`` per name per library, so ``getattr(lib, "CloseHandle")`` returns the *same*
#: object every time and assigning ``.restype`` at a call site mutates shared state — benign while
#: every site assigns the same value, and a silent hazard the moment one does not. Second, without
#: ``argtypes`` a bare Python ``int`` passed where a pointer-width value is required marshals as a
#: 32-bit C ``int`` and truncates, with no signature to catch it.
#:
#: Signatures are taken from the documented Win32 API. ``LPVOID``/``LPDWORD`` out-parameters are
#: declared ``c_void_p``; measured on this interpreter (``p71_abi_probe.py``), a ``c_void_p``
#: argtype accepts every form the call sites below pass — ``byref(struct)``, ``byref(c_uint32)``,
#: ``create_string_buffer``, ``create_unicode_buffer``, ``None`` and ``c_void_p`` — so declaring
#: them hardens the calls without changing any of them.
_SIGNATURES = {
    # HANDLE CreateFileW(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE)
    "CreateFileW": (ctypes.c_void_p, (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)),
    # BOOL GetFileInformationByHandle(HANDLE, LPBY_HANDLE_FILE_INFORMATION)
    "GetFileInformationByHandle": (ctypes.c_int, (ctypes.c_void_p, ctypes.c_void_p)),
    # BOOL GetFileInformationByHandleEx(HANDLE, FILE_INFO_BY_HANDLE_CLASS, LPVOID, DWORD)
    "GetFileInformationByHandleEx": (ctypes.c_int, (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)),
    # DWORD GetFinalPathNameByHandleW(HANDLE, LPWSTR, DWORD, DWORD)
    "GetFinalPathNameByHandleW": (ctypes.c_uint32, (
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)),
    # BOOL GetVolumeInformationByHandleW(HANDLE, LPWSTR, DWORD, LPDWORD, LPDWORD, LPDWORD,
    #                                    LPWSTR, DWORD)
    "GetVolumeInformationByHandleW": (ctypes.c_int, (
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32)),
    # BOOL ReadFile(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED)
    "ReadFile": (ctypes.c_int, (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)),
    # BOOL SetFileInformationByHandle(HANDLE, FILE_INFO_BY_HANDLE_CLASS, LPVOID, DWORD)
    "SetFileInformationByHandle": (ctypes.c_int, (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)),
    # BOOL CloseHandle(HANDLE)
    "CloseHandle": (ctypes.c_int, (ctypes.c_void_p,)),
}


def _bind(name: str) -> Callable:
    """Resolve one authorized entry point. **The only route to ``kernel32`` in this module.**

    An unlisted name raises :class:`AssertionError` *before* the library is touched, so the
    authorized surface is enforced at the one place every call must pass through.

    The library is loaded here, lazily, and is never exposed. There is no module-level accessor
    returning the live ``WinDLL``: R5-P70 F70-4 measured that such an accessor hands unrestricted
    kernel32 to every other fork module through a single private import, with no ``ctypes`` import
    and no ``_bind``. Keeping the only reference in a module global that nothing returns is what
    makes "``_bind`` is the one chokepoint" a property of the code rather than a claim about it.

    Import time stays free of DLL loads, which is what keeps this module importable on a
    non-Windows interpreter.
    """
    if name not in AUTHORIZED_ENTRY_POINTS:
        raise AssertionError(
            f"unauthorized kernel32 entry point {name!r}; "
            f"authorized surface is {AUTHORIZED_ENTRY_POINTS!r}"
        )
    global _k32
    if _k32 is None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise Win32Unavailable("ctypes.WinDLL is unavailable on this platform")
        try:
            _k32 = loader("kernel32", use_last_error=True)
        except OSError as exc:                                   # pragma: no cover - non-Windows
            raise Win32Unavailable("kernel32 could not be loaded") from exc
    function = getattr(_k32, name)
    restype, argtypes = _SIGNATURES[name]
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


def _last_error() -> int:
    """``GetLastError`` is read only on a failure branch — it is stale after a successful call."""
    return ctypes.get_last_error()


# ---- structures ----------------------------------------------------------------------------------

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _FILE_REMOTE_PROTOCOL_INFO(ctypes.Structure):
    _fields_ = [
        ("StructureVersion", ctypes.c_uint16),
        ("StructureSize", ctypes.c_uint16),
        ("Protocol", ctypes.c_uint32),
        ("ProtocolMajorVersion", ctypes.c_uint16),
        ("ProtocolMinorVersion", ctypes.c_uint16),
        ("ProtocolRevision", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("Flags", ctypes.c_uint32),
        ("GenericReserved", ctypes.c_uint32 * 8),
        ("ProtocolSpecificReserved", ctypes.c_uint32 * 16),
    ]


@dataclass(frozen=True)
class HandleIdentity:
    """Identity read FROM a handle, never from a pathname.

    ``link_count`` is **observed and reported**, never acted upon: admitting or refusing a
    multiply-linked object is decision B and is not this module's to make. It is live — measured
    R5-P67 B1, the value changes on an already-open handle when a link is added.
    """

    volume_serial: int
    file_index: int
    file_id_128: str | None
    link_count: int
    size: int
    attributes: int

    def matches(self, other: "HandleIdentity") -> bool:
        """Same object? Compares the 128-bit id with its volume serial when both are available.

        Never compares content: R5-P66's D2-C decoy was chosen byte-identical to the approved object
        precisely so that a digest comparison could not distinguish them.
        """
        if self.file_id_128 is not None and other.file_id_128 is not None:
            return (self.file_id_128 == other.file_id_128
                    and self.volume_serial == other.volume_serial)
        return (self.file_index == other.file_index
                and self.volume_serial == other.volume_serial)


@dataclass(frozen=True)
class VolumeQualification:
    """The affirmative result of the local-NTFS contract. Only ever produced on success."""

    filesystem: str
    volume_serial: int
    volume_flags: int
    dos_path: str
    guid_path: str
    nt_path: str


# ---- handles -------------------------------------------------------------------------------------

class _Handle:
    """Base for an owned handle. Closure is deterministic and idempotent."""

    __slots__ = ("_handle", "_closed")

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._closed = False

    # -- lifecycle --
    def close(self) -> None:
        """Release the handle. Safe to call repeatedly; never raises."""
        if not self._closed:
            self._closed = True
            handle, self._handle = self._handle, None
            if handle is not None:
                _bind("CloseHandle")(ctypes.c_void_p(handle))

    def __enter__(self) -> "_Handle":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _raw(self) -> ctypes.c_void_p:
        if self._closed:
            raise Win32Refused("handle_closed")
        return ctypes.c_void_p(self._handle)

    # -- queries available on any handle --
    def identity(self) -> HandleIdentity:
        """Identity and link count, from this handle."""
        info = _BY_HANDLE_FILE_INFORMATION()
        fn = _bind("GetFileInformationByHandle")
        if not fn(self._raw(), ctypes.byref(info)):
            raise Win32Refused("identity_unavailable", _last_error())
        file_id = None
        idi = _FILE_ID_INFO()
        ex = _bind("GetFileInformationByHandleEx")
        if ex(self._raw(), _FileIdInfo, ctypes.byref(idi), ctypes.sizeof(idi)):
            file_id = bytes(idi.FileId).hex()
        return HandleIdentity(
            volume_serial=info.dwVolumeSerialNumber,
            file_index=(info.nFileIndexHigh << 32) | info.nFileIndexLow,
            file_id_128=file_id,
            link_count=info.nNumberOfLinks,
            size=(info.nFileSizeHigh << 32) | info.nFileSizeLow,
            attributes=info.dwFileAttributes,
        )

    def verify_identity(self, expected: HandleIdentity) -> HandleIdentity:
        """Compare this handle's object against an expectation **the caller supplies**.

        A handle existing is not approval. There is no state in this module that could make an
        object "approved"; the expectation always comes from outside.
        """
        actual = self.identity()
        if not actual.matches(expected):
            raise Win32Refused("identity_mismatch")
        return actual

    def final_path(self, flags: int = VOLUME_NAME_DOS) -> str:
        """This handle's OWN current name.

        Containment must be evaluated on this, never by re-resolving the caller's input pathname:
        R5-P67 C1 measured an ancestor junction being re-pointed while a handle was held, after
        which the input pathname and the handle named different objects.
        """
        fn = _bind("GetFinalPathNameByHandleW")
        needed = fn(self._raw(), None, 0, flags)
        if needed == 0:
            raise Win32Refused("final_path_unavailable", _last_error())
        buf = ctypes.create_unicode_buffer(needed)
        if fn(self._raw(), buf, needed, flags) == 0:
            raise Win32Refused("final_path_unavailable", _last_error())
        return buf.value

    def _volume_information(self) -> tuple[str, int, int]:
        fsname = ctypes.create_unicode_buffer(261)
        label = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        fn = _bind("GetVolumeInformationByHandleW")
        ok = fn(self._raw(), label, 261, ctypes.byref(serial), ctypes.byref(max_component),
                ctypes.byref(flags), fsname, 261)
        if not ok:
            raise Win32Refused("volume_query_failed", _last_error())
        return fsname.value, serial.value, flags.value

    def _is_remote(self) -> bool:
        """``FileRemoteProtocolInfo`` succeeds only for a remote handle.

        Success is positive evidence of remoteness. Its *failure* is deliberately NOT treated as
        proof of locality — see :meth:`qualify_local_ntfs`.
        """
        info = _FILE_REMOTE_PROTOCOL_INFO()
        info.StructureVersion = 2
        info.StructureSize = ctypes.sizeof(info)
        ex = _bind("GetFileInformationByHandleEx")
        return bool(ex(self._raw(), _FileRemoteProtocolInfo,
                       ctypes.byref(info), ctypes.sizeof(info)))

    def qualify_local_ntfs(self) -> VolumeQualification:
        r"""Prove this handle sits on a **local NTFS** volume, or refuse.

        R5-P68 measured that an SMB loopback reports filesystem name ``NTFS`` *and the identical
        volume serial* as the local volume it re-exports. So neither the name nor the serial proves
        locality, and a gate built on either would have admitted SMB as local.

        Five conditions, all required. Four are **affirmative** — they assert what a local handle
        looks like — and only one is a negative:

        1. ``VOLUME_NAME_DOS`` is a drive-letter form ``\\?\X:\`` and is **not** ``\\?\UNC\``
           (measured: the SMB handle yields ``\\?\UNC\127.0.0.1\E$\...``);
        2. ``VOLUME_NAME_GUID`` **resolves** to ``\\?\Volume{...}`` (measured: it fails with
           winerror 3 for the SMB handle — a remote path has no local volume GUID);
        3. ``VOLUME_NAME_NT`` names a local device and is **not** ``\Device\Mup\`` (measured: the
           SMB handle yields ``\Device\Mup\...``, the Multiple UNC Provider redirector);
        4. the filesystem name is exactly ``NTFS`` and every flag in
           :data:`REQUIRED_VOLUME_FLAGS` is present (measured: the SMB handle reports a *different*
           flag word, ``0x00c700fe`` vs ``0x03e72eff``, despite the same name and serial);
        5. ``FileRemoteProtocolInfo`` does **not** succeed.

        Condition 5 alone would rest on winerror 87 meaning "local", which it does not — it means
        "this class does not apply to this handle", and an unrelated future cause of failure would
        read as locality. It is therefore a veto, never the proof. Conditions 1–4 carry the proof.
        """
        if self._is_remote():
            raise Win32Refused("volume_remote")

        # Filesystem identity before path shape, so an unsupported filesystem reports the reason a
        # reader expects. The order does not affect safety - every condition must hold - but a
        # FAT32 volume refused as "volume_device_not_local" would send a reader hunting the wrong
        # problem.
        filesystem, serial, flags = self._volume_information()
        if filesystem.upper() != REQUIRED_FILESYSTEM:
            raise Win32Refused("volume_not_ntfs")
        if (flags & REQUIRED_VOLUME_FLAGS) != REQUIRED_VOLUME_FLAGS:
            raise Win32Refused("volume_missing_required_capability")

        dos = self.final_path(VOLUME_NAME_DOS)
        if dos.upper().startswith("\\\\?\\UNC\\") or not _is_dos_drive_path(dos):
            raise Win32Refused("volume_path_not_local_dos")

        try:
            guid = self.final_path(VOLUME_NAME_GUID)
        except Win32Refused as exc:
            raise Win32Refused("volume_guid_unavailable", exc.winerror) from None
        if not guid.upper().startswith("\\\\?\\VOLUME{"):
            raise Win32Refused("volume_guid_unavailable")

        nt = self.final_path(VOLUME_NAME_NT)
        if not _is_local_device_path(nt):
            raise Win32Refused("volume_device_not_local")

        return VolumeQualification(filesystem=filesystem, volume_serial=serial,
                                   volume_flags=flags, dos_path=dos, guid_path=guid, nt_path=nt)


def _encoded_destination_name(destination_path: str) -> bytes:
    r"""Validate a destination name and return its exact UTF-16LE bytes, or refuse.

    This is the pre-commit gate for both R5-P70 MAJORs. It runs **before** the rename buffer is
    built and before any kernel call, so ``destination_malformed`` is unambiguously a refusal in
    which nothing happened.

    Four rejections, each for a measured reason:

    * **not a ``str``.** Refused rather than coerced. ``str(obj)`` on an arbitrary object would
      invent a pathname from a repr and then publish to it — a silent, attacker-influenceable
      name. There is no safe default here, so there is no default.
    * **empty.** No name to publish to. R5-P69 left this to the kernel, which refused it with
      winerror 24; refusing it here keeps the whole class of malformed input on one code path.
    * **not strictly encodable as UTF-16.** A lone surrogate (``"tail_\ud83d"``) is a valid Python
      ``str`` and not a valid Win32 name. R5-P70 measured the module *creating* such a file, whose
      name ordinary tooling then could not round-trip — this review's own directory lister raised
      ``UnicodeEncodeError`` on it. ``errors="strict"`` is the whole point.
    * **an embedded NUL.** The serialized buffer is byte-correct and the **kernel** truncates at the
      NUL: R5-P70 measured ``cl\x00ip.bin`` committing as ``cl`` and returning success.

    ``str.encode`` is called **unbound** deliberately. A ``str`` subclass overriding ``encode`` (or
    ``__contains__``) could otherwise pass validation as one string and be serialized as another,
    which would reintroduce exactly the defect this function exists to close. For the same reason
    the NUL check runs on the resulting **code units** rather than on the input object.
    """
    if not isinstance(destination_path, str):
        raise Win32Refused("destination_malformed")
    try:
        encoded = str.encode(destination_path, "utf-16-le", "strict")
    except UnicodeEncodeError:
        raise Win32Refused("destination_malformed") from None
    if not encoded:
        raise Win32Refused("destination_malformed")
    if any(encoded[index] == 0 and encoded[index + 1] == 0
           for index in range(0, len(encoded), 2)):
        raise Win32Refused("destination_malformed")
    return encoded


def _leaf_of(name: str) -> str:
    r"""The final path component, for comparing a requested name against a published one.

    Only the leaf is compared. The directory the object lands in is already established by the
    caller committing to a destination *handle's own resolved name* (R5-P67 E13), while the leaf is
    precisely what both measured defects corrupted. Comparing whole strings instead would fail
    spuriously whenever the caller passes a ``C:\dir\leaf`` form and the handle reports the
    equivalent ``\\?\C:\dir\leaf``.
    """
    cut = name.rfind("\\")
    return name if cut < 0 else name[cut + 1:]


def _is_dos_drive_path(path: str) -> bool:
    r"""True for ``\\?\X:\...`` or ``X:\...`` with an ASCII drive letter."""
    body = path[4:] if path.startswith("\\\\?\\") else path
    return (len(body) >= 3 and body[0].isascii() and body[0].isalpha()
            and body[1] == ":" and body[2] == "\\")


#: Local volume device namespaces, as an ALLOWLIST rather than a ``Mup`` denylist: a device class
#: nobody anticipated is then refused instead of admitted. Both forms were measured on this
#: machine -- a fixed NTFS volume reports ``\Device\HarddiskVolume8\`` and a removable volume
#: reports ``\Device\Volume{...}\``, while the SMB redirector reports ``\Device\Mup\``.
_LOCAL_DEVICE_PREFIXES = ("\\DEVICE\\HARDDISKVOLUME", "\\DEVICE\\VOLUME{")


def _is_local_device_path(nt_path: str) -> bool:
    r"""True only for a recognised local volume device namespace.

    ``\Device\Mup\`` is the Multiple UNC Provider -- the redirector every remote path resolves
    through -- and is excluded explicitly as well as by the allowlist, so the intent survives an
    edit to the prefix tuple.
    """
    upper = nt_path.upper()
    if upper.startswith("\\DEVICE\\MUP"):
        return False
    return any(upper.startswith(prefix) for prefix in _LOCAL_DEVICE_PREFIXES)


class SourceHandle(_Handle):
    """A source object, opened with least privilege and protective sharing.

    Opened ``FILE_READ_DATA | DELETE`` with share ``FILE_SHARE_READ``. The ``DELETE`` access right
    is what the rename needs; denying ``FILE_SHARE_DELETE`` is what stops the leaf being renamed or
    deleted away while held.

    One consequence the caller must know: while this handle is open, an ordinary ``open()`` on the
    same object **is refused** (errno 13, and ``winerror`` is ``None`` because CPython's ``open()``
    goes through the CRT). Windows' sharing check is bidirectional — a new opener's share mask must
    permit an existing handle's granted access, and ``open()`` never grants ``FILE_SHARE_DELETE``.
    That is why a preview pin and this handle cannot be the same handle, and it is also why the
    object must be hashed from *this* handle rather than by pathname.
    """

    __slots__ = ("_streamed",)

    def __init__(self, handle: int) -> None:
        super().__init__(handle)
        self._streamed = False

    def sha256_stream(self, chunk_size: int = _MAX_STREAM_CHUNK) -> tuple[str, int]:
        """SHA-256 the object behind this handle, in one forward pass from offset 0.

        Returns ``(hexdigest, byte_count)`` **only** for a digest covering the whole object.

        ``SetFilePointerEx`` is **not** in the authorized surface, so there is no seek: the file
        pointer starts at 0 after the open and this reads forward to EOF exactly once. A second call
        would silently hash nothing and return the digest of an empty stream, so it is refused
        rather than permitted to produce a wrong answer. Re-hashing requires a fresh handle, and a
        fresh handle must be re-verified.

        **[R5-P73 / F72-1] Two properties this function now owns, both of which it previously
        lacked, and both of which were measured absent on real media.**

        *First, the argument is validated before anything is spent.* R5-P72 measured
        ``sha256_stream(chunk_size=0)`` on a real non-empty 55,500-byte NTFS file returning the
        SHA-256 of the **empty string** with size 0 as an unqualified success — a false integrity
        record that would re-verify consistently forever. The mechanism:
        ``ReadFile(h, buf, 0, &got, NULL)`` on a non-empty file returns ``rv=1, got=0``, which is
        indistinguishable from EOF, so the loop terminated on its first iteration. Three routes
        reached it — ``0``; ``False`` (``bool`` is an ``int`` subclass and ``False == 0``); and any
        positive multiple of 2**32, via the ``c_uint32`` truncation described at
        :data:`_MAX_STREAM_CHUNK`.

        The check is ``type(chunk_size) is not int`` rather than ``isinstance``, and the difference
        is load-bearing for two separately measured reasons.

        ``isinstance`` admits ``bool``, which is route two above, so an ``isinstance`` form needs a
        second explicit ``not isinstance(chunk_size, bool)`` clause to be equivalent.

        More importantly, ``isinstance`` admits a hostile ``int`` subclass that overrides the **rich
        comparisons**. In ``1 <= chunk_size <= _MAX_STREAM_CHUNK`` Python gives the subclass's
        reflected operation priority, so ``chunk_size.__ge__(1)`` and ``chunk_size.__le__`` are
        consulted — and an override can return ``True`` for both while the object's real integer
        value is anything at all. Measured on this interpreter: a subclass returning ``True`` from
        ``__ge__``/``__le__`` passes the ``isinstance`` form with a true value of ``0`` (which then
        marshals to a zero-byte read and reproduces this very defect) and with a true value of
        ``1 << 40`` (which then reaches ``create_string_buffer`` and attempts a 1 TiB allocation,
        reproducing F72-2). The exact-type form refuses all of them.

        What is deliberately **not** claimed, because it was measured and found false: ``ctypes``
        does *not* consult ``__index__`` here. ``create_string_buffer`` and ``c_uint32`` both use
        the underlying integer value, so an ``__index__`` override does not make a value marshal
        differently from how it compared. The divergence this check closes is in the **comparison**,
        not in the marshalling.

        This is the same class of defect as the ``str`` subclass
        :func:`_encoded_destination_name` refuses to coerce, and it is refused here for the same
        stated reason: a value must not be able to validate as one thing and be used as another.

        Everything fallible happens **before** ``self._streamed = True``, which is the last
        statement before the first read and has no fallible operation between it and the loop. So a
        refused or failed *pre-read* call leaves the handle's one shot intact and a subsequent valid
        call still works. R5-P72 F72-2 measured the opposite: the flag was burned first, so one bad
        argument permanently destroyed that handle's ability to hash, costing a full re-acquire and
        re-verification in a design where acquisition is the security-critical step.

        *Second, a returned digest is complete.* The expected length is read from **this handle's
        own identity** (:meth:`identity`, i.e. ``GetFileInformationByHandle`` — already authorized;
        no new entry point, and no seek), never from a pathname, and the forward pass must account
        for exactly that many bytes or the result is refused ``stream_incomplete``. This is what
        makes a short read fail closed instead of returning a plausible digest of a prefix: a
        partial digest is not distinguishable from a complete one by looking at it.

        The comparison is well defined rather than racy because of the share mode this handle was
        opened with: ``FILE_SHARE_READ`` alone denies ``FILE_SHARE_WRITE``, so while it is held no
        other opener may write to the object and its length cannot change under the loop. If the
        count ever disagrees — a short read, a premature success-with-zero-bytes, a length that
        moved anyway, or a count larger than the read that was requested — that is refused, not
        reported.

        That fourth mode is :data:`stream_failed`, not :data:`stream_incomplete`, and it is
        checked inside the loop rather than inferred from the total afterwards. R5-P74 F74-1
        measured why the distinction matters: an over-reported count inflates a separately
        accumulated total by more than the content it represents, so it can be made to land
        **exactly** on the expected size while only a prefix was hashed, which the final
        comparison then accepts. Two independent properties now prevent it — the impossible
        count is refused, and progress counts only bytes actually handed to the digest — and
        either alone is sufficient, which is deliberate defence in depth rather than
        redundancy to be optimised away.

        Refusals, all of them meaning **nothing was read or nothing is returned**, never that a
        digest is partially trustworthy: ``chunk_size_invalid``, ``already_streamed``,
        ``handle_closed``, ``identity_unavailable``, ``stream_failed``, ``stream_incomplete``. An
        unexpected exception is deliberately **not** converted: hashing commits nothing, so there is
        no published state to report as unverified, and swallowing it would hide a fault while
        returning nothing anyway. It propagates, and no digest is produced on that path either.
        """
        # (1) Argument validation. Depends on no state, allocates nothing, calls no kernel entry
        #     point, and mutates nothing -- so it cannot be reached after anything is spent.
        if type(chunk_size) is not int or not 1 <= chunk_size <= _MAX_STREAM_CHUNK:
            raise Win32Refused("chunk_size_invalid")
        # (2) The one-shot contract, and (3) the expected length from this handle's own identity.
        #     Both can refuse; neither consumes the stream.
        if self._streamed:
            raise Win32Refused("already_streamed")
        expected_size = self.identity().size
        # (4) Everything that can still fail, done while the handle's one shot is still intact.
        fn = _bind("ReadFile")
        digest = hashlib.sha256()
        buffer = ctypes.create_string_buffer(chunk_size)
        read = ctypes.c_uint32(0)
        total = 0
        # (5) The irreversible transition. Nothing fallible may be inserted between this line and
        #     the first read, or a pre-read failure would silently spend the handle again.
        self._streamed = True
        while True:
            if not fn(self._raw(), buffer, chunk_size, ctypes.byref(read), None):
                raise Win32Refused("stream_failed", _last_error())
            # [R5-P75 / F74-1] The count is only meaningful once the call has SUCCEEDED,
            #     and a count above the request is impossible: the buffer is exactly
            #     `chunk_size` bytes and the marshalled request equals it (the 1 MiB cap
            #     makes `c_uint32` truncation unreachable). Refuse rather than clamp --
            #     a clamped impossible count would be reported as a verified stream.
            if read.value > chunk_size:
                raise Win32Refused("stream_failed")
            if read.value == 0:
                break
            # [R5-P75 / F74-1] Progress is the length of the byte sequence actually
            #     hashed, never a separately reported number. `buffer.raw[:n]` is clamped
            #     by Python to the buffer length while `read.value` is not, so the two
            #     could diverge and `total` could reach `expected_size` having covered
            #     only a prefix. Same rule, and same reason, as `name_bytes` in
            #     :meth:`rename_no_replace` -- R5-P70 F70-1.
            chunk = buffer.raw[:read.value]
            digest.update(chunk)
            total += len(chunk)
        # (6) Completeness. A zero-byte read ends the loop whether it is EOF or a premature
        #     success, so EOF alone is not evidence that the whole object was covered.
        if total != expected_size:
            raise Win32Refused("stream_incomplete")
        return digest.hexdigest(), total

    def _confirm_published_name(self, destination_path: str) -> None:
        """After a successful commit, confirm the object actually landed at the requested leaf.

        Defence in depth, and deliberately *post*-commit. The validation in
        :func:`_encoded_destination_name` closes the two routes R5-P70 measured; this closes the
        class. Any future route by which the kernel publishes under a name other than the one
        requested — a mangling this module does not anticipate — becomes a loud, explicit
        :class:`Win32CommitUnverified` instead of a silent success, which is the property the
        MAJORs were really about.

        Uses the handle's own name, which is the only trustworthy source: re-resolving the
        destination pathname would re-introduce the pathname race the module exists to avoid. It
        binds no new entry point — ``GetFinalPathNameByHandleW`` is already authorized.

        **Never raises a refusal here.** A refusal means nothing happened, and by this point the
        rename has committed.
        """
        try:
            published = self.final_path(VOLUME_NAME_DOS)
        except Win32Refused as exc:
            raise Win32CommitUnverified("published_name_unverifiable", exc.winerror) from None
        if _leaf_of(published) != _leaf_of(destination_path):
            raise Win32CommitUnverified("published_name_mismatch")

    def rename_no_replace(self, destination_path: str) -> None:
        r"""Atomically rename **the object behind this handle** to ``destination_path``.

        ``ReplaceIfExists`` is hard-wired to 0 and ``RootDirectory`` to NULL — neither is a
        parameter, so no caller and no future edit can turn replacement on through this API. An
        occupied destination is refused with ``destination_exists`` (winerror 183) and the occupant
        is left byte-identical with its file id intact.

        ``destination_path`` must be fully qualified. Pass
        ``directory.final_path() + "\\" + leaf`` — the destination directory handle's own resolved
        name — rather than a configured pathname: R5-P67 E8 redirected a publication by re-pointing
        a junction in an unresolved destination path, and E13 defeated the same attack by committing
        to the resolved name.

        ``RootDirectory`` is NULL because it does not work: R5-P67 measured 36 of 36 variants
        refused with winerror 87 through ``SetFileInformationByHandle``, while the identical buffer
        with a NULL ``RootDirectory`` commits. Reaching it would need an undocumented NT API.

        **The destination is validated before anything is committed.** A name that is not a ``str``,
        is empty, carries an embedded NUL, or is not strictly encodable as UTF-16 raises
        ``destination_malformed`` with nothing touched — see :func:`_encoded_destination_name`.

        **Two distinct, non-interchangeable outcomes on failure.**

        * :class:`Win32Refused` — **nothing was committed.** Every code in :data:`REFUSAL_CODES`
          means the object is still exactly where it was.
        * :class:`Win32CommitUnverified` — **the rename committed** and the published name could not
          be confirmed equal to the request. ``published`` is ``True``. This module does not roll
          back; the caller must reconcile from the filesystem.

        A caller that catches only :class:`Win32Refused` will therefore not silently treat a
        publication as a no-op, which is the failure mode R5-P70 F70-1 and F70-2 both produced.
        """
        # Validate and encode FIRST. Both R5-P70 MAJORs were commits that succeeded at a name the
        # caller never asked for, so nothing may touch the kernel until the name is known good.
        encoded = _encoded_destination_name(destination_path)
        name_bytes = len(encoded)
        # x64 FILE_RENAME_INFO: 0 BOOLEAN ReplaceIfExists (union, 4 bytes + pad);
        # 8 HANDLE RootDirectory; 16 DWORD FileNameLength (BYTES, excluding any terminator);
        # 20 WCHAR FileName[] (need not be NUL-terminated).
        # Offsets independently confirmed against a ctypes-computed SDK reference structure
        # (0 / 8 / 16 / 20, sizeof 24) rather than against this arithmetic -- R5-P71.
        size = 20 + name_bytes + 2
        buffer = ctypes.create_string_buffer(size)                # zero-filled: NUL terminator free
        buffer[0] = b"\x00"                                       # ReplaceIfExists = FALSE
        ctypes.memmove(ctypes.addressof(buffer) + 8,
                       ctypes.byref(ctypes.c_void_p(0)), 8)       # RootDirectory = NULL
        ctypes.memmove(ctypes.addressof(buffer) + 16,
                       ctypes.byref(ctypes.c_uint32(name_bytes)), 4)
        # The ENCODED BYTES are copied, and the length is that byte sequence's own length. Deriving
        # either from `len(destination_path)` undercounts every supplementary-plane character by 2
        # bytes (one code point, two UTF-16 code units) -- R5-P70 F70-1.
        ctypes.memmove(ctypes.addressof(buffer) + 20, encoded, name_bytes)
        fn = _bind("SetFileInformationByHandle")
        if fn(self._raw(), _FileRenameInfo, buffer, size):
            self._confirm_published_name(destination_path)
            return
        code = _last_error()
        if code == _ERROR_ALREADY_EXISTS:
            raise Win32Refused("destination_exists", code)
        if code == _ERROR_ACCESS_DENIED:
            raise Win32Refused("destination_access_denied", code)
        if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            raise Win32Refused("source_missing", code)
        if code == _ERROR_NOT_SAME_DEVICE:
            raise Win32Refused("cross_volume_not_supported", code)
        raise Win32Refused("rename_failed", code)


class DestinationDirectoryHandle(_Handle):
    """A destination directory, pinned against relocation but still writable by the kernel.

    Opened ``FILE_LIST_DIRECTORY`` with share ``FILE_SHARE_READ | FILE_SHARE_WRITE``. Denying
    ``FILE_SHARE_DELETE`` freezes the directory's own name; permitting ``FILE_SHARE_WRITE`` is
    mandatory, because the rename must add a directory entry and a WRITE-denying pin refuses the
    engine's own commit (R5-P67 E12). An attributes-only open would be exempt from the sharing check
    altogether and would pin nothing (R5-P67 E9), which is why ``FILE_LIST_DIRECTORY`` is requested.
    """

    __slots__ = ()


# ---- acquisition ---------------------------------------------------------------------------------

def _create_file(path: str, access: int, share: int, flags: int) -> int:
    fn = _bind("CreateFileW")
    handle = fn(ctypes.c_wchar_p(path), ctypes.c_uint32(access), ctypes.c_uint32(share),
                None, ctypes.c_uint32(OPEN_EXISTING), ctypes.c_uint32(flags), None)
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return -_last_error() or -1
    return handle


def open_source(path: str) -> SourceHandle:
    """Acquire a source handle with least privilege and protective sharing.

    **Acquire before evaluating anything.** R5-P67 A7-E2 measured that validating containment first
    and opening afterwards lets an adversary swap the pathname, after which the handle binds the
    decoy and the commit publishes an object that was never inside the root.

    A sharing conflict is a refusal (``source_shared_violation``) and never a retry by another
    route: an antivirus, indexer, backup or cloud-sync agent holding the file makes acquisition fail
    closed, which is correct and is a real availability cost.

    **Ownership transfers to the caller, who must close it** — use ``with``. While this handle is
    open the object denies ``FILE_SHARE_DELETE``, so an ordinary ``open()`` of it is refused
    (errno 13) and its leaf cannot be renamed or deleted (winerror 32). Leaking the handle leaves
    the object unreadable and unmovable for the process lifetime, and there is no finalizer to
    recover it (R5-P70 F70-8, retained open — see the module docstring).
    """
    handle = _create_file(path, FILE_READ_DATA | DELETE, SOURCE_SHARE_MODE, 0)
    if handle < 0:
        code = -handle
        if code == _ERROR_SHARING_VIOLATION:
            raise Win32Refused("source_shared_violation", code)
        raise Win32Refused("source_unavailable", code)
    return SourceHandle(handle)


def open_destination_directory(path: str) -> DestinationDirectoryHandle:
    """Acquire a destination-directory handle that is pinned yet writable.

    **Ownership transfers to the caller, who must close it** — use ``with``. While it is open the
    directory cannot be relocated (winerror 32), so a leaked handle blocks the operator from moving
    or removing that directory for the process lifetime (R5-P70 F70-8, retained open).
    """
    handle = _create_file(path, FILE_LIST_DIRECTORY, DESTINATION_SHARE_MODE,
                          FILE_FLAG_BACKUP_SEMANTICS)
    if handle < 0:
        raise Win32Refused("destination_directory_unavailable", -handle)
    return DestinationDirectoryHandle(handle)


def require_same_volume(source: VolumeQualification,
                        destination: VolumeQualification) -> None:
    """Refuse unless both qualifications name the same volume.

    A handle-bound rename is a single-volume operation — cross-volume refuses with winerror 17 and
    creates nothing (measured, R5-P67 E5). Serial equality is used **only** as a same-volume test
    and never as proof of locality: an SMB loopback reports the identical serial as the local volume
    it re-exports, so locality is established by :meth:`_Handle.qualify_local_ntfs` on each handle
    independently, before this comparison is reached.
    """
    if source.volume_serial != destination.volume_serial:
        raise Win32Refused("cross_volume_not_supported")
