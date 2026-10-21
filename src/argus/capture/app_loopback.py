"""Audio of a single application: WASAPI process loopback.

Ordinary loopback takes everything that plays on the device: the other party's speech
mixed with music, notifications, and a video in the next tab. Windows can do it
differently: hand over the audio of **one process and its children**. Then "system sound"
stops being a dumping ground and becomes what was asked for: the sound of Zoom, Chrome,
or Discord.

Works from Windows 10 build 20348 and on Windows 11. Below that it silently does not
work, so there is `available()`: ask before, not find out after.

Three things, each of which cost a separate attempt.

**The handler must be `IAgileObject`.** Without this marker `ActivateAudioInterfaceAsync`
answers `E_ILLEGAL_METHOD_CALL` ("method called at an unexpected time"), and that is all
it says: nothing about the thread or the parameters, because it never gets that far. In
C++ examples the marker comes by itself, with the base class, so it is not visible in
them.

**The thread must be in the MTA**, and this is decided before `comtypes` is imported: it
brings up an STA by itself, and the apartment cannot be switched afterward. Hence
`sys.coinit_flags` at the start of the file, before the imports; it looks odd, and the
order is mandatory.

**The format is not asked for but set.** This device has no format of its own, and
`GetMixFormat` does not work for it. We ask for 48 kHz, two channels, 16 bits; then the
resampler converts it to our 16 kHz mono, as for ordinary capture.
"""

from __future__ import annotations

import sys
from typing import ClassVar

# Must come before the `comtypes` import: see the docstring above.
if not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = 0

import ctypes as ct
import logging
import threading
from ctypes import wintypes

import numpy as np
from comtypes import COMMETHOD, GUID, COMObject, IUnknown

log = logging.getLogger(__name__)

DEVICE_ID = "VAD" + chr(92) + "Process_Loopback"

RATE = 48_000
CHANNELS = 2
BITS = 16

_SHARED = 0
_STREAM_LOOPBACK = 0x00020000
_BUFFER_SILENT = 0x2
_ACTIVATION_PROCESS_LOOPBACK = 1
_VT_BLOB = 65

INCLUDE_TREE = 0
EXCLUDE_TREE = 1

IID_AUDIO_CLIENT = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_CAPTURE_CLIENT = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")


class ProcessAudioUnavailable(RuntimeError):
    """The app's audio could not be obtained: with an explanation, not silently."""


# --------------------------------------------------------------------------
# Definitions of structures and interfaces
# --------------------------------------------------------------------------


class WAVEFORMATEX(ct.Structure):
    _fields_: ClassVar[list] = [
        ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _LoopbackParams(ct.Structure):
    _fields_: ClassVar[list] = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ct.c_int)]


class _ActivationParams(ct.Structure):
    _fields_: ClassVar[list] = [("ActivationType", ct.c_int), ("Loopback", _LoopbackParams)]


class _Blob(ct.Structure):
    _fields_: ClassVar[list] = [("cbSize", wintypes.ULONG), ("pBlobData", ct.c_void_p)]


class _PropVariant(ct.Structure):
    _fields_: ClassVar[list] = [
        ("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
        ("r3", wintypes.WORD), ("blob", _Blob), ("pad", ct.c_void_p * 2),
    ]


class IAudioClient(IUnknown):
    _iid_ = IID_AUDIO_CLIENT
    _methods_: ClassVar[list] = [
        COMMETHOD([], ct.HRESULT, "Initialize",
                  (["in"], ct.c_int, "mode"), (["in"], wintypes.DWORD, "flags"),
                  (["in"], ct.c_longlong, "buffer"), (["in"], ct.c_longlong, "period"),
                  (["in"], ct.POINTER(WAVEFORMATEX), "format"),
                  (["in"], ct.c_void_p, "session")),
        COMMETHOD([], ct.HRESULT, "GetBufferSize", (["out"], ct.POINTER(ct.c_uint32), "frames")),
        COMMETHOD([], ct.HRESULT, "GetStreamLatency", (["out"], ct.POINTER(ct.c_longlong), "ns")),
        COMMETHOD([], ct.HRESULT, "GetCurrentPadding",
                  (["out"], ct.POINTER(ct.c_uint32), "frames")),
        COMMETHOD([], ct.HRESULT, "IsFormatSupported",
                  (["in"], ct.c_int, "mode"), (["in"], ct.POINTER(WAVEFORMATEX), "format"),
                  (["out"], ct.POINTER(ct.POINTER(WAVEFORMATEX)), "closest")),
        COMMETHOD([], ct.HRESULT, "GetMixFormat",
                  (["out"], ct.POINTER(ct.POINTER(WAVEFORMATEX)), "format")),
        COMMETHOD([], ct.HRESULT, "GetDevicePeriod",
                  (["out"], ct.POINTER(ct.c_longlong), "default"),
                  (["out"], ct.POINTER(ct.c_longlong), "minimum")),
        COMMETHOD([], ct.HRESULT, "Start"),
        COMMETHOD([], ct.HRESULT, "Stop"),
        COMMETHOD([], ct.HRESULT, "Reset"),
        COMMETHOD([], ct.HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "handle")),
        COMMETHOD([], ct.HRESULT, "GetService",
                  (["in"], ct.POINTER(GUID), "iid"), (["out"], ct.POINTER(ct.c_void_p), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_CAPTURE_CLIENT
    _methods_: ClassVar[list] = [
        COMMETHOD([], ct.HRESULT, "GetBuffer",
                  (["out"], ct.POINTER(ct.POINTER(ct.c_byte)), "data"),
                  (["out"], ct.POINTER(ct.c_uint32), "frames"),
                  (["out"], ct.POINTER(wintypes.DWORD), "flags"),
                  (["out"], ct.POINTER(ct.c_ulonglong), "position"),
                  (["out"], ct.POINTER(ct.c_ulonglong), "qpc")),
        COMMETHOD([], ct.HRESULT, "ReleaseBuffer", (["in"], ct.c_uint32, "frames")),
        COMMETHOD([], ct.HRESULT, "GetNextPacketSize",
                  (["out"], ct.POINTER(ct.c_uint32), "frames")),
    ]


class _IActivateOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_: ClassVar[list] = [
        COMMETHOD([], ct.HRESULT, "GetActivateResult",
                  (["out"], ct.POINTER(ct.HRESULT), "result"),
                  (["out"], ct.POINTER(ct.POINTER(IUnknown)), "interface")),
    ]


class _IAgileObject(IUnknown):
    """A marker meaning "the object does not care which apartment it is called from". It has no
    methods.
    """

    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_: ClassVar[list] = []


class _ICompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_: ClassVar[list] = [
        COMMETHOD([], ct.HRESULT, "ActivateCompleted",
                  (["in"], ct.POINTER(_IActivateOperation), "operation")),
    ]


class _Handler(COMObject):
    _com_interfaces_: ClassVar[list] = [_ICompletionHandler, _IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def _ICompletionHandler_ActivateCompleted(self, this, operation):
        self.done.set()
        return 0


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


def window_pid(hwnd: int) -> int:
    """Which process the window belongs to."""
    pid = wintypes.DWORD()
    ct.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ct.byref(pid))
    return int(pid.value)


def available() -> tuple[bool, str]:
    """Whether this Windows can deliver the audio of a single app."""
    version = sys.getwindowsversion()
    if (version.major, version.build) < (10, 20348):
        return False, f"Windows 10 build 20348 or newer is required, this one is {version.build}"
    return True, f"build {version.build}"


class ProcessLoopback:
    """A single process's audio stream.

    COM is bound to the thread here: we read where we opened. Hence the rule: do not move the
    object between threads, but create it next to the reading.
    """

    def __init__(self, pid: int, mode: int = INCLUDE_TREE, buffer_s: float = 2.0) -> None:
        self.pid = pid
        self.mode = mode
        self.buffer_s = buffer_s
        self._client = None
        self._capture = None

    def open(self) -> None:
        ct.windll.ole32.CoInitializeEx(None, 0)

        params = _ActivationParams()
        params.ActivationType = _ACTIVATION_PROCESS_LOOPBACK
        params.Loopback.TargetProcessId = self.pid
        params.Loopback.ProcessLoopbackMode = self.mode

        prop = _PropVariant()
        prop.vt = _VT_BLOB
        prop.blob.cbSize = ct.sizeof(params)
        prop.blob.pBlobData = ct.cast(ct.pointer(params), ct.c_void_p)

        mmdev = ct.windll.mmdevapi
        mmdev.ActivateAudioInterfaceAsync.argtypes = [
            wintypes.LPCWSTR, ct.POINTER(GUID), ct.POINTER(_PropVariant),
            ct.POINTER(_ICompletionHandler), ct.POINTER(ct.POINTER(_IActivateOperation)),
        ]

        handler = _Handler()
        operation = ct.POINTER(_IActivateOperation)()
        hr = mmdev.ActivateAudioInterfaceAsync(
            DEVICE_ID, ct.byref(IID_AUDIO_CLIENT), ct.byref(prop),
            handler.QueryInterface(_ICompletionHandler), ct.byref(operation),
        )
        if hr:
            raise ProcessAudioUnavailable(
                f"the system refused the audio request for process {self.pid}: "
                f"0x{hr & 0xFFFFFFFF:08x}"
            )

        if not handler.done.wait(3.0):
            raise ProcessAudioUnavailable("the system did not answer the request within 3 s")

        result, iface = operation.GetActivateResult()
        if result:
            raise ProcessAudioUnavailable(
                f"process {self.pid} did not provide audio: 0x{result & 0xFFFFFFFF:08x} — "
                f"this usually means it has no audio session"
            )

        client = iface.QueryInterface(IAudioClient)
        block = CHANNELS * BITS // 8
        fmt = WAVEFORMATEX(1, CHANNELS, RATE, RATE * block, block, BITS, 0)
        client.Initialize(_SHARED, _STREAM_LOOPBACK,
                          int(self.buffer_s * 10_000_000), 0, ct.byref(fmt), None)
        self._capture = ct.cast(client.GetService(ct.byref(IID_CAPTURE_CLIENT)),
                                ct.POINTER(IAudioCaptureClient))
        client.Start()
        self._client = client
        log.info("app audio: process %d, rate (Hz): %d, channels: %d", self.pid, RATE, CHANNELS)

    def read(self) -> np.ndarray:
        """Take everything that has accumulated. Empty means the app is silent."""
        if self._capture is None:
            return np.empty(0, dtype=np.int16)

        parts: list[np.ndarray] = []
        while True:
            if not self._capture.GetNextPacketSize():
                break
            data, frames, flags, _, _ = self._capture.GetBuffer()
            if frames:
                if flags & _BUFFER_SILENT:
                    parts.append(np.zeros(frames * CHANNELS, dtype=np.int16))
                else:
                    raw = ct.cast(data, ct.POINTER(ct.c_int16 * (frames * CHANNELS)))
                    parts.append(np.frombuffer(bytes(raw.contents), dtype=np.int16))
            self._capture.ReleaseBuffer(frames)

        return np.concatenate(parts) if parts else np.empty(0, dtype=np.int16)

    def close(self) -> None:
        client, self._client = self._client, None
        self._capture = None
        if client is not None:
            try:
                client.Stop()
            except Exception as e:  # noqa: BLE001 — we stay quiet on close
                log.debug("stopping app capture: %s", e)
