"""The live list of macOS audio devices and the default devices, straight from CoreAudio.

Why not PortAudio. It takes the device list once, at initialization, and a live stream
sees until the end of its life the picture that was there at the start (the
`sounddevice` help flatly advises restarting the library, and doing that with open
streams is impossible, as they would close). We already went through the same thing on
Windows (`capture/audio.py`, `DEVICE_POLL_S`): the microphone choice showed one headset,
a webcam plugged in later did not appear, and the "listening to the wrong one" check
compared the state at start with itself.

Here the list is requested from the system anew on every call: that is one CoreAudio
call per device, a fraction of a millisecond. Streams are still opened by PortAudio, and
it comes here only for the names, and devices are matched by name, not by number (the
PortAudio numbers are a position in its own list).

All through `ctypes`, without PyObjC: the needed functions are plain C, and
`AudioObjectGetPropertyData` returns data through a `void *`, which PyObjC handles
worst. There is one reading part (`_get`), the rest is byte parsing; the tests on
Windows replace only it.
"""

from __future__ import annotations

import ctypes
import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)


def _fourcc(code: str) -> int:
    """`'dev#'` -> a number, the way CoreAudio writes four-letter codes."""
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
UNKNOWN_OBJECT = 0

SEL_DEVICES = _fourcc("dev#")             # kAudioHardwarePropertyDevices
SEL_DEFAULT_INPUT = _fourcc("dIn ")       # kAudioHardwarePropertyDefaultInputDevice
SEL_DEFAULT_OUTPUT = _fourcc("dOut")      # kAudioHardwarePropertyDefaultOutputDevice
SEL_NAME = _fourcc("lnam")                # kAudioObjectPropertyName
SEL_UID = _fourcc("uid ")                 # kAudioDevicePropertyDeviceUID
SEL_STREAMS_LAYOUT = _fourcc("slay")      # kAudioDevicePropertyStreamConfiguration
SEL_NOMINAL_RATE = _fourcc("nsrt")        # kAudioDevicePropertyNominalSampleRate
SEL_TRANSPORT = _fourcc("tran")           # kAudioDevicePropertyTransportType

SCOPE_GLOBAL = _fourcc("glob")
SCOPE_INPUT = _fourcc("inpt")
SCOPE_OUTPUT = _fourcc("outp")

# The connection type: virtual devices (BlackHole, Loopback, Soundflower) declare
# themselves this way, and this flag is how they are told apart from real ones.
TRANSPORT_VIRTUAL = _fourcc("virt")
TRANSPORT_AGGREGATE = _fourcc("grup")

_CF_UTF8 = 0x08000100    # kCFStringEncodingUTF8
_FRAMEWORKS = "/System/Library/Frameworks"


@dataclass(slots=True)
class AudioDevice:
    """A device through the eyes of CoreAudio."""

    id: int
    name: str
    uid: str
    inputs: int           # number of input channels (0 means output)
    outputs: int
    rate: int
    virtual: bool = False
    default_input: bool = False
    default_output: bool = False


# --------------------------------------------------------------------------
# The only place that touches the system
# --------------------------------------------------------------------------

_libs: dict[str, ctypes.CDLL] = {}


def _lib(name: str) -> ctypes.CDLL:
    if name not in _libs:
        _libs[name] = ctypes.CDLL(f"{_FRAMEWORKS}/{name}.framework/{name}")
    return _libs[name]


class _Address(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 - this is how ctypes structures are described
        ("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32),
    ]


def _get(obj: int, selector: int, scope: int = SCOPE_GLOBAL) -> bytes | None:
    """A CoreAudio object property as raw bytes. `None` means the system refused."""
    try:
        ca = _lib("CoreAudio")
        address = _Address(selector, scope, 0)
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(obj), ctypes.byref(address), ctypes.c_uint32(0), None,
            ctypes.byref(size),
        ):
            return None
        buffer = ctypes.create_string_buffer(size.value or 1)
        if ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj), ctypes.byref(address), ctypes.c_uint32(0), None,
            ctypes.byref(size), buffer,
        ):
            return None
        return buffer.raw[: size.value]
    except (OSError, AttributeError) as e:
        log.debug("CoreAudio did not respond (%#x): %s", selector, e)
        return None


def _cfstring(raw: bytes) -> str:
    """A string property: the bytes hold a pointer to a `CFString` that must be returned to the
    system.
    """
    if len(raw) < ctypes.sizeof(ctypes.c_void_p):
        return ""
    pointer = ctypes.c_void_p(int.from_bytes(raw[: ctypes.sizeof(ctypes.c_void_p)], "little"))
    if not pointer.value:
        return ""
    cf = _lib("CoreFoundation")
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    try:
        out = ctypes.create_string_buffer(1024)
        ok = cf.CFStringGetCString(pointer, out, len(out), _CF_UTF8)
        return out.value.decode("utf-8", "replace") if ok else ""
    finally:
        cf.CFRelease(pointer)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _u32(raw: bytes | None) -> int:
    return struct.unpack_from("<I", raw)[0] if raw and len(raw) >= 4 else 0


def _channels(raw: bytes | None) -> int:
    """The sum of channels from `AudioBufferList`: `UInt32 n`, then (aligned to 8 bytes) `n`
    structures of 16 bytes: channels, size, pointer."""
    if not raw or len(raw) < 4:
        return 0
    count = struct.unpack_from("<I", raw)[0]
    total = 0
    for i in range(count):
        offset = 8 + 16 * i
        if offset + 4 > len(raw):
            break
        total += struct.unpack_from("<I", raw, offset)[0]
    return total


def _name(obj: int, selector: int = SEL_NAME) -> str:
    raw = _get(obj, selector)
    return _cfstring(raw) if raw else ""


def devices() -> list[AudioDevice]:
    """All audio devices of the system, anew each time."""
    raw = _get(SYSTEM_OBJECT, SEL_DEVICES)
    if not raw:
        return []
    ids = struct.unpack_from(f"<{len(raw) // 4}I", raw)
    default_in = default_input_id()
    default_out = default_output_id()

    found: list[AudioDevice] = []
    for obj in ids:
        name = _name(obj)
        if not name:
            continue
        rate_raw = _get(obj, SEL_NOMINAL_RATE)
        rate = int(struct.unpack_from("<d", rate_raw)[0]) if rate_raw and len(rate_raw) >= 8 else 0
        transport = _u32(_get(obj, SEL_TRANSPORT))
        found.append(AudioDevice(
            id=obj,
            name=name,
            uid=_name(obj, SEL_UID),
            inputs=_channels(_get(obj, SEL_STREAMS_LAYOUT, SCOPE_INPUT)),
            outputs=_channels(_get(obj, SEL_STREAMS_LAYOUT, SCOPE_OUTPUT)),
            rate=rate,
            virtual=transport in (TRANSPORT_VIRTUAL, TRANSPORT_AGGREGATE),
            default_input=obj == default_in,
            default_output=obj == default_out,
        ))
    return found


def default_input_id() -> int:
    return _u32(_get(SYSTEM_OBJECT, SEL_DEFAULT_INPUT))


def default_output_id() -> int:
    return _u32(_get(SYSTEM_OBJECT, SEL_DEFAULT_OUTPUT))


def inputs() -> list[AudioDevice]:
    """Devices that can be recorded from."""
    return [d for d in devices() if d.inputs > 0]


def default_names() -> dict[str, str]:
    """What the system considers current: `{"in": …, "out": …}`. Empty means it could not be found
    out.
    """
    out: dict[str, str] = {}
    for key, obj in (("in", default_input_id()), ("out", default_output_id())):
        if obj and obj != UNKNOWN_OBJECT:
            name = _name(obj)
            if name:
                out[key] = name
    return out
