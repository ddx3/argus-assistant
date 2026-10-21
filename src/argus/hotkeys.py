"""Global hotkeys.

No Alt-Tabbing, and mouse movement is visible in a screen share, so all control is from
the keyboard, and it has to work while the focus is in someone else's window.

Via `RegisterHotKey` from `user32`, with no new dependency. The choice is not about build
size: a low-level hook (`WH_KEYBOARD_LL`), which libraries use for this, sees **every**
keypress in the system, including passwords in other windows. Registration sees exactly
its own combination and nothing else, and it can be explained to a person in one line. For
a program that already listens to the microphone and looks at the screen, that is the
difference between "tool" and "spy".

The `WM_HOTKEY` message arrives at the **thread** that registered the combination, so here
we have our own thread with its own loop. The WebView window loop does not fit: it is in
the main thread and busy drawing, and there is nothing to wedge into it.

What breaks silently, and is therefore checked out loud: a combination may already be
taken by another program. In that case the system simply answers with a refusal, and
without a check we would get the worst outcome: the setting is in place, the person
presses the keys, nothing happens, and why is unknown. Refusals accumulate in `problems`
and go to the same place as the complaints about audio.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# Modifiers of `RegisterHotKey`. On macOS the same values come into `mac/hotkeys.py` and
# are translated there to Carbon modifiers; `cmd` (⌘) is parsed as `win`.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008

# No repeat on hold. Otherwise a held "ask" goes to the model dozens of times per second,
# at a price for each call.
MOD_NOREPEAT = 0x4000

MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "контрол": MOD_CONTROL,
    "alt": MOD_ALT, "альт": MOD_ALT, "option": MOD_ALT, "opt": MOD_ALT,
    "shift": MOD_SHIFT, "шифт": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "cmd": MOD_WIN, "command": MOD_WIN, "super": MOD_WIN,
}

# Keys whose name does not match their symbol.
NAMED_KEYS = {
    "space": 0x20, "пробел": 0x20,
    "esc": 0x1B, "escape": 0x1B,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
}


def parse(combo: str) -> tuple[int, int]:
    """`ctrl+alt+a` -> (modifiers, key code).

    We raise the error with text that can be shown to a person: "unknown key" without the
    key's name makes them guess what exactly is wrong in a string of four words.
    """
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty combination")

    mods = 0
    key = ""
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif key:
            raise ValueError(f"two keys in the combination: '{key}' and '{part}'")
        else:
            key = part

    if not key:
        raise ValueError("the combination has only modifiers and no key")

    # Without a modifier a combination would take the key from the whole system: pressing "a"
    # in someone else's window would become impossible.
    if not mods:
        raise ValueError(f"'{key}' without ctrl, alt, shift or win is not allowed")

    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and (key.isdigit() or ("a" <= key <= "z")):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unknown key: '{key}'")

    return mods | MOD_NOREPEAT, vk


# Where letters and digits sit on the keyboard, by scan codes (QWERTY layout). The page
# records a combination by `KeyboardEvent.code`, by the key's position and not the letter on
# it, and `ctrl+alt+y` means "the key in Y's position". But `RegisterHotKey` takes a virtual
# code, which depends on the layout: on QWERTZ, Z sits in Y's place, and on AZERTY, A and Q
# are swapped, and so are Z and W. Without the translation, pressing a key the person had
# just assigned launched nothing.
POSITION_SCAN = {
    "A": 0x1E, "B": 0x30, "C": 0x2E, "D": 0x20, "E": 0x12, "F": 0x21, "G": 0x22, "H": 0x23, "I": 0x17,
    "J": 0x24, "K": 0x25, "L": 0x26, "M": 0x32, "N": 0x31, "O": 0x18, "P": 0x19, "Q": 0x10, "R": 0x13,
    "S": 0x1F, "T": 0x14, "U": 0x16, "V": 0x2F, "W": 0x11, "X": 0x2D, "Y": 0x15, "Z": 0x2C,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A,
    "0": 0x0B,
}
MAPVK_VSC_TO_VK = 1
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12


class WindowsAPI:
    """A thin wrapper over `user32`: exactly what needs to be patched in a test."""

    def __init__(self, user32: object | None = None) -> None:
        import ctypes

        # Specifically `WinDLL(..., use_last_error=True)`, not `windll.user32`. Without this flag
        # ctypes does not save the error code, `get_last_error` returns zero, and "taken by another
        # program", the only refusal reason a person can fix, turns into "the system refused (0)".
        self._ctypes = ctypes
        if user32 is not None:               # substitution in a test
            self._u32 = user32
            return
        self._u32 = u32 = ctypes.WinDLL("user32", use_last_error=True)
        # Layouts are pointer-sized handles: without explicit types ctypes would truncate them to
        # `int`.
        u32.GetKeyboardLayout.argtypes = [ctypes.c_uint]
        u32.GetKeyboardLayout.restype = ctypes.c_void_p
        u32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.c_void_p]
        u32.GetKeyboardLayoutList.restype = ctypes.c_int
        u32.MapVirtualKeyExW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        u32.MapVirtualKeyExW.restype = ctypes.c_uint
        u32.ToUnicodeEx.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_uint, ctypes.c_void_p,
        ]
        u32.ToUnicodeEx.restype = ctypes.c_int

    def _vk_on(self, vk: int, layout: object) -> int:
        """The virtual key code located at the place of `vk` (on QWERTY) in layout `layout`."""
        scan = POSITION_SCAN.get(chr(vk))
        if scan is None:
            return vk                         # key names and F1–F24 do not depend on the layout
        return int(self._u32.MapVirtualKeyExW(scan, MAPVK_VSC_TO_VK, layout)) or vk

    def typed_by_chord(self, mods: int, vk: int) -> str:
        """Which letter this combination types on one of the installed layouts. Empty means none.

        Ctrl+Alt is AltGr: on many national layouts `ctrl+alt+a` types ą, and `ctrl+alt+s` types
        ś. The system gives us the registered combination, and the letter would stop being typed
        in any program; the paid "ask" would fire instead. We check all installed layouts, not
        one: people switch between them as they work.
        """
        scan = POSITION_SCAN.get(chr(vk))
        if scan is None or not (mods & MOD_CONTROL and mods & MOD_ALT):
            return ""                         # only Ctrl+Alt gives AltGr, and only on letters and digits
        ctypes = self._ctypes
        count = self._u32.GetKeyboardLayoutList(0, None)
        if count <= 0:
            return ""
        layouts = (ctypes.c_void_p * count)()
        count = self._u32.GetKeyboardLayoutList(count, layouts)
        state = (ctypes.c_ubyte * 256)()
        state[VK_CONTROL] = state[VK_MENU] = 0x80
        if mods & MOD_SHIFT:
            state[VK_SHIFT] = 0x80
        for index in range(count):
            layout = layouts[index]
            buf = ctypes.create_unicode_buffer(8)
            # Flag 4 means do not change the keyboard state: otherwise the check would swallow someone's
            # "dead" key.
            got = self._u32.ToUnicodeEx(self._vk_on(vk, layout), scan, state, buf, 8, 0x4, layout)
            text = buf.value[:abs(got)] if got else ""
            if text and text[0] >= " " and text[0] != "\x7f":      # control characters are not a letter
                return text
        return ""

    def register(self, hid: int, mods: int, vk: int) -> str:
        """Empty means success; otherwise the reason in words."""
        vk = self._vk_on(vk, self._u32.GetKeyboardLayout(0))
        if self._u32.RegisterHotKey(None, hid, mods, vk):
            return ""
        code = self._ctypes.get_last_error()
        # 1409 is ERROR_HOTKEY_ALREADY_REGISTERED. It is the only reason a person can fix, so it is
        # named separately.
        if code == 1409:
            return "taken by another program"
        log.warning("the system refused to register the shortcut, code %s", code)
        return "the system refused"

    def unregister(self, hid: int) -> None:
        self._u32.UnregisterHotKey(None, hid)

    def pressed(self) -> list[int]:
        """Take from the thread's queue everything pressed since last time."""
        import ctypes.wintypes

        WM_HOTKEY, PM_REMOVE = 0x0312, 0x0001
        msg = ctypes.wintypes.MSG()
        out: list[int] = []
        while self._u32.PeekMessageW(self._ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_HOTKEY:
                out.append(int(msg.wParam))
        return out


def default_api() -> object:
    """The system that is asked to register the combinations: `user32` or Carbon (macOS).

    A combination is parsed the same way on both (`parse`); only the translation of bits and
    codes into what the system understands differs, and it lives in `mac/hotkeys.py`.
    """
    if sys.platform == "darwin":
        from argus.mac.hotkeys import MacAPI

        return MacAPI()
    return WindowsAPI()


class Hotkeys:
    """Combinations bound to actions.

    Actions are called not in the message loop but in a separate thread per keypress. The
    reason is simple: "ask" goes over the network to our own server, and until the answer
    comes the loop would not process the queue, so a "panic" pressed right after would fire a
    second later. It is a panic precisely so that it fires at once.
    """

    # How often to look into the queue. `GetMessage` can wait by itself, but then rebinding
    # requires waking the thread with a foreign message; polling twenty times a second costs
    # nothing, and a 50 ms delay between the press and the action goes unnoticed.
    TICK_S = 0.05

    # Combinations do not stay released longer than this without `resume`. The page releases
    # them while a combination is being assigned by pressing it and returns them on
    # `beforeunload`, which is not guaranteed: if the window is closed or the page crashes in
    # the middle of an assignment, all the combinations, including "hide", would stay removed
    # until a restart.
    SUSPEND_MAX_S = 60.0

    def __init__(self, actions: dict[str, Callable[[], None]], api: object | None = None) -> None:
        self.actions = actions
        self.api = api if api is not None else default_api()
        self.problems: list[str] = []

        self._ids: dict[int, str] = {}          # registration number -> action name
        self._combos: dict[str, str] = {}       # the last requested layout
        self._wanted: dict[str, str] | None = None   # what to bind on the next round
        self._suspended_at: float | None = None     # when the combinations were released; None means they are not released
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._applied = threading.Event()       # the thread has handled the request
        self._thread: threading.Thread | None = None

    # -- from outside ------------------------------------------------------------

    def start(self, combos: dict[str, str]) -> None:
        self.bind(combos)
        self._thread = threading.Thread(target=self._run, name="argus-hotkeys", daemon=True)
        self._thread.start()

    def bind(self, combos: dict[str, str]) -> None:
        """Order a new layout. It is the thread that rebinds, not the caller.

        A registration belongs to the thread that made it: it cannot be removed from another
        thread, and an attempt to bind from there would give a combination whose presses arrive
        in a queue that nobody reads.
        """
        with self._lock:
            self._combos = dict(combos)
            self._wanted = dict(combos)
            self._suspended_at = None
            self._applied.clear()

    def suspend(self, timeout: float = 1.0) -> bool:
        """Release the combinations and wait until they are released.

        This is needed exactly while a combination is being assigned by pressing it. Otherwise
        assigning "ctrl+alt+a" again is physically impossible: the system gives the press to
        whoever registered it, that is, to us, and it never reaches the page; instead of an
        assignment, "ask" fires.

        We wait for confirmation instead of returning at once: the page starts listening to the
        keyboard upon our reply, and to return it before the registration is removed would mean
        losing the race to the very first keypress.
        """
        with self._lock:
            self._wanted = {}
            self._suspended_at = time.monotonic()
            self._applied.clear()
        return self._applied.wait(timeout)

    def resume(self) -> None:
        """Restore the last requested layout."""
        with self._lock:
            self._wanted = dict(self._combos)
            self._suspended_at = None
            self._applied.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- thread -------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                wanted, self._wanted = self._wanted, None
                if (
                    wanted is None and self._suspended_at is not None
                    and time.monotonic() - self._suspended_at > self.SUSPEND_MAX_S
                ):
                    # The page did not return the combinations (closed, or crashed in the middle of an
                    # assignment), so we return them ourselves.
                    log.warning("shortcuts have been released for longer than %.0f s - restoring them", self.SUSPEND_MAX_S)
                    wanted = dict(self._combos)
                    self._suspended_at = None
            if wanted is not None:
                self._apply(wanted)
                self._applied.set()
            for hid in self.api.pressed():
                self._fire(self._ids.get(hid, ""))
            self._stop.wait(self.TICK_S)
        self._apply({})

    def _apply(self, combos: dict[str, str]) -> None:
        for hid in self._ids:
            self.api.unregister(hid)
        self._ids.clear()
        self.problems.clear()

        for number, (name, combo) in enumerate(sorted(combos.items()), start=1):
            action = self.actions.get(name)
            if action is None or not str(combo).strip():
                continue
            try:
                mods, vk = parse(combo)
            except ValueError as e:
                self.problems.append(f"“{combo}” — {e}")
                continue
            typed = self._typed(mods, vk)
            if typed:
                # We do not register it: the system would give the press to us, and this letter could not be
                # typed anywhere.
                self.problems.append(
                    f"'{combo}' — on this keyboard layout it types '{typed}', pick another combination"
                )
                continue
            failure = self.api.register(number, mods, vk)
            if failure:
                self.problems.append(f"“{combo}” — {failure}")
                continue
            self._ids[number] = name

        for line in self.problems:
            log.warning("hotkey was not registered: %s", line)

    def _typed(self, mods: int, vk: int) -> str:
        """The letter that the combination types on the layout (AltGr); empty if none, or if there is
        nothing to ask.
        """
        check = getattr(self.api, "typed_by_chord", None)
        if check is None:
            return ""
        try:
            return check(mods, vk) or ""
        except Exception:
            log.warning("could not check the AltGr layout", exc_info=True)
            return ""

    def _fire(self, name: str) -> None:
        action = self.actions.get(name)
        if action is None:
            return

        def run() -> None:
            try:
                action()
            except Exception:
                log.exception("action '%s' failed", name)

        threading.Thread(target=run, name=f"argus-hotkey-{name}", daemon=True).start()
