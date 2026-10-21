"""Which audio devices the system considers current: a fresh answer, not a snapshot from
startup.

PortAudio counts devices once, at first initialization. A second `PyAudio()` only adds one
to the reference count and returns the same picture: while a live capture holds the
library open (and it holds it for the whole session), a microphone plugged in later does
not appear in the list, and the "listening to the wrong device" complaint never fires,
because the state at startup was compared with itself.

Here the same question is asked of the system directly, through the MMDevice API: its
answer is always fresh. The names are the same (`PKEY_Device_FriendlyName`, which PortAudio
returns too), so the result is compared with the names of the devices we opened.

ctypes only, no comtypes: on import it brings up COM in the thread, while we are asked from
worker threads where we choose the apartment ourselves. And nothing is opened for
recording: this is a list poll, and no audio flows during it.
"""

from __future__ import annotations

import ctypes as ct
import logging
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_E_RENDER, _E_CAPTURE = 0, 1
# The "multimedia" role is the one PortAudio calls the default device. It always matches the
# "console" role, except for settings that cannot be set in the ordinary sound window.
_ROLE_MULTIMEDIA = 1
_STATE_ACTIVE = 1
_STGM_READ = 0
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0
_VT_LPWSTR = 31

_ole32_dll = None


class _GUID(ct.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
        ("Data4", ct.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> _GUID:
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ct.c_ubyte * 8)(*u.bytes[8:]))


class _PropertyKey(ct.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PropVariant(ct.Structure):
    # The real PROPVARIANT is 24 bytes on x64; here with a margin: the system fills the
    # structure.
    _fields_ = [
        ("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
        ("value", ct.c_void_p * 4),
    ]


_CLSID_ENUMERATOR = _GUID.of("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_ENUMERATOR = _GUID.of("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_FRIENDLY_NAME = _PropertyKey(_GUID.of("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One device: the identifier does not change; the name we show and compare."""

    id: str
    name: str


@dataclass(slots=True)
class Snapshot:
    """What the system knows about the devices right now."""

    default_in: Endpoint | None = None      # None means there is no default microphone at all
    default_out: Endpoint | None = None     # None means no speakers or headphones
    inputs: list[Endpoint] = field(default_factory=list)   # enabled microphones


def _ole():
    global _ole32_dll
    if _ole32_dll is None:
        # A library instance of our own: the `argtypes` below must not get into the shared
        # `windll.ole32`, which others use too.
        dll = ct.WinDLL("ole32")
        dll.CoInitializeEx.argtypes = [ct.c_void_p, wintypes.DWORD]
        dll.CoCreateInstance.argtypes = [
            ct.POINTER(_GUID), ct.c_void_p, wintypes.DWORD, ct.POINTER(_GUID),
            ct.POINTER(ct.c_void_p),
        ]
        dll.CoCreateInstance.restype = ct.HRESULT          # failure is an OSError
        dll.CoTaskMemFree.argtypes = [ct.c_void_p]
        dll.PropVariantClear.argtypes = [ct.POINTER(_PropVariant)]
        _ole32_dll = dll
    return _ole32_dll


def _method(obj: ct.c_void_p, index: int, restype, *argtypes):
    """A COM interface method by its number in the virtual function table."""
    table = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p)))[0]
    return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(table[index])


def _release(obj: ct.c_void_p) -> None:
    if obj.value:
        _method(obj, 2, ct.c_ulong)(obj)


def _device_id(device: ct.c_void_p) -> str:
    buf = ct.c_void_p()
    _method(device, 5, ct.HRESULT, ct.POINTER(ct.c_void_p))(device, ct.byref(buf))
    try:
        return ct.wstring_at(buf.value) if buf.value else ""
    finally:
        if buf.value:
            _ole().CoTaskMemFree(buf)


def _friendly_name(device: ct.c_void_p) -> str:
    store = ct.c_void_p()
    _method(device, 4, ct.HRESULT, wintypes.DWORD, ct.POINTER(ct.c_void_p))(
        device, _STGM_READ, ct.byref(store))
    try:
        value = _PropVariant()
        _method(store, 5, ct.HRESULT, ct.POINTER(_PropertyKey), ct.POINTER(_PropVariant))(
            store, ct.byref(_FRIENDLY_NAME), ct.byref(value))
        try:
            if value.vt != _VT_LPWSTR or not value.value[0]:
                return ""
            return ct.wstring_at(value.value[0])
        finally:
            _ole().PropVariantClear(ct.byref(value))
    finally:
        _release(store)


def _endpoint(device: ct.c_void_p) -> Endpoint:
    return Endpoint(_device_id(device), _friendly_name(device))


def _default(enumerator: ct.c_void_p, flow: int) -> Endpoint | None:
    device = ct.c_void_p()
    try:
        _method(enumerator, 4, ct.HRESULT, ct.c_int, ct.c_int, ct.POINTER(ct.c_void_p))(
            enumerator, flow, _ROLE_MULTIMEDIA, ct.byref(device))
    except OSError:
        return None             # there are no devices of this kind in the system at all; this is not a polling failure
    try:
        return _endpoint(device)
    finally:
        _release(device)


def _active(enumerator: ct.c_void_p, flow: int) -> list[Endpoint]:
    found = ct.c_void_p()
    _method(enumerator, 3, ct.HRESULT, ct.c_int, wintypes.DWORD, ct.POINTER(ct.c_void_p))(
        enumerator, flow, _STATE_ACTIVE, ct.byref(found))
    try:
        count = ct.c_uint()
        _method(found, 3, ct.HRESULT, ct.POINTER(ct.c_uint))(found, ct.byref(count))
        out: list[Endpoint] = []
        for i in range(count.value):
            device = ct.c_void_p()
            try:
                _method(found, 4, ct.HRESULT, ct.c_uint, ct.POINTER(ct.c_void_p))(
                    found, i, ct.byref(device))
                try:
                    endpoint = _endpoint(device)
                finally:
                    _release(device)
            except OSError:
                # The device disappeared right in the middle of the poll: a list without it is better than
                # no list.
                log.debug("device #%d: disappeared during the query", i, exc_info=True)
                continue
            if endpoint.name:
                out.append(endpoint)
        return out
    finally:
        _release(found)


def snapshot() -> Snapshot:
    """Ask the system which audio devices exist and which of them are the defaults.

    Called from any thread: COM is brought up and torn down inside the call. If asking failed,
    `OSError`; "no such device" is not an error but an empty spot in the answer.
    """
    if sys.platform != "win32":
        raise OSError("querying devices through the system is available only on Windows")

    ole = _ole()
    hr = ole.CoInitializeEx(None, _COINIT_MULTITHREADED)
    try:
        enumerator = ct.c_void_p()
        ole.CoCreateInstance(
            ct.byref(_CLSID_ENUMERATOR), None, _CLSCTX_ALL, ct.byref(_IID_ENUMERATOR),
            ct.byref(enumerator))
        try:
            return Snapshot(
                default_in=_default(enumerator, _E_CAPTURE),
                default_out=_default(enumerator, _E_RENDER),
                inputs=_active(enumerator, _E_CAPTURE),
            )
        finally:
            _release(enumerator)
    finally:
        if hr in (0, 1):        # S_OK and S_FALSE require CoUninitialize; "another apartment" does not
            ole.CoUninitialize()
