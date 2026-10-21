"""Global hotkeys on macOS through Carbon `RegisterEventHotKey`.

The idea is the same as on Windows (`hotkeys.py`): the system sees **exactly its own
combination and nothing else**. Intercepting all key presses (`CGEventTap`,
`NSEvent.addGlobalMonitor`) would require the "Input Monitoring" or "Accessibility"
permission. The user would see a prompt asking why the program may read everything they
type, and would be right to be wary. `RegisterEventHotKey` needs no permission at all:
it is old Carbon, but Alfred, Raycast and clipboard managers still use it.

`Hotkeys` (the shared class) parses a combination the Windows way: a set of modifier
bits and a Windows key code. Here they are translated to Carbon: the modifiers become
different bits, and the keys become ANSI layout codes (not characters but positions on
the keyboard, just like Windows codes). This gives the program one combination parser for
both systems and shared tests.

Carbon events arrive on the **main thread**, in the window loop that pywebview runs. Our
handler is tiny: it puts the combination number in a queue and returns. The shared
`Hotkeys` takes the queue with its own thread (`pressed`) and runs the action on a
separate thread, as on Windows. Registering combinations also goes to the main thread
(`run_on_main`): the Carbon handler is installed in the same place where the event loop
runs.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from typing import Any

from argus.mac.mainthread import run_in_main_thread

log = logging.getLogger(__name__)

# Modifier bits as `hotkeys.parse` returns them (the `RegisterEventHotKey` values on
# Windows).
WIN_ALT, WIN_CONTROL, WIN_SHIFT, WIN_WIN = 0x0001, 0x0002, 0x0004, 0x0008

# Carbon modifiers (`Events.h`).
CMD_KEY, SHIFT_KEY, OPTION_KEY, CONTROL_KEY = 0x0100, 0x0200, 0x0800, 0x1000

# Carbon error codes (`CarbonEventsCore.h`).
EVENT_INTERNAL_ERROR = -9868      # "generic error"; this is how macOS 15.0-15.1 answers a combination with only Option/Shift
EVENT_HOTKEY_EXISTS = -9878       # the same combination is already registered IN THIS SAME process
EVENT_HOTKEY_INVALID = -9879      # not used by the system

# Four-letter Carbon codes.
_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
_KIND_HOTKEY_PRESSED = 5
_PARAM_DIRECT_OBJECT = int.from_bytes(b"----", "big")
_TYPE_HOTKEY_ID = int.from_bytes(b"hkid", "big")
_SIGNATURE = int.from_bytes(b"ARGU", "big")

# Key codes (`kVK_ANSI_*`, `kVK_*` from `Events.h`) indexed by the Windows key codes that
# `parse` returns.
LETTERS = {
    "A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06, "X": 0x07,
    "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E, "R": 0x0F, "Y": 0x10,
    "T": 0x11, "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23, "L": 0x25, "J": 0x26, "K": 0x28,
    "N": 0x2D, "M": 0x2E,
}
DIGITS = {
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C,
    "9": 0x19, "0": 0x1D,
}
FUNCTION = {
    1: 0x7A, 2: 0x78, 3: 0x63, 4: 0x76, 5: 0x60, 6: 0x61, 7: 0x62, 8: 0x64, 9: 0x65,
    10: 0x6D, 11: 0x67, 12: 0x6F, 13: 0x69, 14: 0x6B, 15: 0x71, 16: 0x6A, 17: 0x40,
    18: 0x4F, 19: 0x50, 20: 0x5A,
}
NAMED = {
    0x20: 0x31,   # space
    0x1B: 0x35,   # Esc
    0x0D: 0x24,   # Enter
    0x09: 0x30,   # Tab
    0x08: 0x33,   # Backspace
    0x26: 0x7E, 0x28: 0x7D, 0x25: 0x7B, 0x27: 0x7C,       # arrows: up, down, left, right
    0x21: 0x74, 0x22: 0x79,                              # PageUp, PageDown
    0x24: 0x73, 0x23: 0x77,                              # Home, End
    0x2E: 0x75,                                          # forward Delete
}


def to_carbon(mods: int, vk: int) -> tuple[int, int] | None:
    """`(Windows modifiers, Windows key code)` -> `(Carbon modifiers, key code)`.

    `None` means there is no such key on a Mac (Insert, F21-F24).
    """
    if 0x30 <= vk <= 0x39:
        key = DIGITS.get(chr(vk))
    elif 0x41 <= vk <= 0x5A:
        key = LETTERS.get(chr(vk))
    elif 0x70 <= vk <= 0x87:
        key = FUNCTION.get(vk - 0x70 + 1)
    else:
        key = NAMED.get(vk)
    if key is None:
        return None
    carbon = 0
    if mods & WIN_WIN:
        carbon |= CMD_KEY
    if mods & WIN_CONTROL:
        carbon |= CONTROL_KEY
    if mods & WIN_ALT:
        carbon |= OPTION_KEY
    if mods & WIN_SHIFT:
        carbon |= SHIFT_KEY
    return carbon, key


# --------------------------------------------------------------------------
# The only place that touches the system
# --------------------------------------------------------------------------


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]  # noqa: RUF012


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]  # noqa: RUF012


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class CarbonFacade:
    """Five Carbon calls and nothing more. The rest in `MacAPI` is tested without the system."""

    def __init__(self) -> None:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.GetApplicationEventTarget.argtypes = []
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _HANDLER, ctypes.c_ulong, ctypes.POINTER(_EventTypeSpec),     # ItemCount — unsigned long
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _HotKeyID, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.UnregisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.GetEventParameter.restype = ctypes.c_int32
        carbon.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        self._c = carbon
        self._callback: Any = None      # keep a reference: otherwise the GC would kill the function under the system's feet
        self._handler_ref = ctypes.c_void_p()

    def install(self, on_hotkey: Callable[[int], None]) -> int:
        """Install the keypress handler; `on_hotkey(number)` is called on the main thread."""
        if self._handler_ref.value:
            # Already installed. This happens when a previous call did not wait for the main thread
            # (`TimeoutError`) but its work was still done later: a second handler would free the first
            # function, to which the system still holds a pointer.
            return 0

        def handler(_next, event, _user):
            hotkey = _HotKeyID()
            status = self._c.GetEventParameter(
                event, _PARAM_DIRECT_OBJECT, _TYPE_HOTKEY_ID, None,
                ctypes.sizeof(hotkey), None, ctypes.byref(hotkey),
            )
            if status == 0 and hotkey.signature == _SIGNATURE:
                try:
                    on_hotkey(int(hotkey.id))
                except Exception:  # noqa: BLE001 — an exception in the Carbon handler would kill the process
                    log.exception("hotkey handler crashed")
            return 0

        self._callback = _HANDLER(handler)
        spec = _EventTypeSpec(_CLASS_KEYBOARD, _KIND_HOTKEY_PRESSED)
        return int(self._c.InstallEventHandler(
            self._c.GetApplicationEventTarget(), self._callback, 1, ctypes.byref(spec), None,
            ctypes.byref(self._handler_ref),
        ))

    def register(self, number: int, modifiers: int, key: int) -> tuple[int, Any]:
        ref = ctypes.c_void_p()
        status = int(self._c.RegisterEventHotKey(
            key, modifiers, _HotKeyID(_SIGNATURE, number),
            self._c.GetApplicationEventTarget(), 0, ctypes.byref(ref),
        ))
        return status, ref.value

    def unregister(self, ref: Any) -> None:
        self._c.UnregisterEventHotKey(ctypes.c_void_p(ref))


def _post_to_main_thread(func: Callable[[], Any]) -> None:
    """Put `func` in the main thread's queue and do not wait."""
    from PyObjCTools import AppHelper

    AppHelper.callAfter(func)


# --------------------------------------------------------------------------
# What the shared `Hotkeys` sees
# --------------------------------------------------------------------------


class MacAPI:
    """The same set as `hotkeys.WindowsAPI`: `register`, `unregister`, `pressed`."""

    def __init__(
        self,
        facade: Any | None = None,
        run_on_main: Callable[[Callable[[], Any]], Any] | None = None,
        post_on_main: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> None:
        self._facade = facade
        self._run = run_on_main or run_in_main_thread
        # Unregistering a combination needs no result, so it is "fire and forget": on exit the
        # window loop has already stopped and would answer no one, so every unregistration would
        # sit out its full timeout.
        self._post = post_on_main or run_on_main or _post_to_main_thread
        self._lock = threading.Lock()
        self._queue: list[int] = []
        self._refs: dict[int, Any] = {}
        self._installed = False

    def _carbon(self) -> Any:
        if self._facade is None:
            self._facade = CarbonFacade()
        return self._facade

    def _ensure_handler(self) -> str:
        if self._installed:
            return ""
        status = self._run(lambda: self._carbon().install(self._on_hotkey))
        if status != 0:
            log.warning("the system did not accept the hotkey handler, code %s", status)
            return "the system refused"
        self._installed = True
        return ""

    def _on_hotkey(self, number: int) -> None:
        with self._lock:
            self._queue.append(number)

    def register(self, hid: int, mods: int, vk: int) -> str:
        """Empty means success; otherwise the reason in words."""
        translated = to_carbon(mods, vk)
        if translated is None:
            return "this key does not exist on a Mac"
        carbon_mods, key = translated
        try:
            failure = self._ensure_handler()
            if failure:
                return failure
            status, ref = self._run(lambda: self._carbon().register(hid, carbon_mods, key))
        except TimeoutError:
            log.warning("hotkeys: main thread did not respond")
            return "the system did not respond"
        except Exception:  # noqa: BLE001 — no Carbon, no PyObjC: the hotkeys will not be set, the rest keeps working
            log.exception("hotkeys were not registered")
            return "the system refused"
        if status == 0:
            self._refs[hid] = ref
            return ""
        # Not "taken by another program": Carbon refuses only when the same combination is already
        # taken in this process, and it does not notice a clash with another program or a system
        # shortcut (both will receive the key press). Two requests of ours for the same combination
        # are the only thing visible here.
        if status == EVENT_HOTKEY_EXISTS:
            return "this combination is already assigned to another Argus action"
        if status == EVENT_INTERNAL_ERROR and not carbon_mods & (CMD_KEY | CONTROL_KEY):
            return "this macOS version does not accept a combination with only Option or Shift — add Control or Command"
        log.warning("the system refused to register the shortcut, code %s", status)
        return "the system refused"

    def unregister(self, hid: int) -> None:
        ref = self._refs.pop(hid, None)
        if ref is None:
            return
        try:
            self._post(lambda: self._carbon().unregister(ref))
        except Exception:  # noqa: BLE001 — stay silent on exit
            log.debug("shortcut was not released", exc_info=True)

    def pressed(self) -> list[int]:
        """Take everything pressed since last time."""
        with self._lock:
            out, self._queue = self._queue, []
        return out
