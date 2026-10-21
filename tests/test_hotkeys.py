"""Global hotkeys.

This breaks more quietly than anything else in the program: the setting is there, the
key is pressed, and nothing happens. There are exactly two causes, the combo was parsed
wrongly or somebody else has already taken it, and both must be visible in words, not as
silence.
"""

from __future__ import annotations

import threading
import time

import pytest

from argus.assist.settings import Hotkeys as HotkeySettings
from argus.hotkeys import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, Hotkeys, parse


def wait_for(condition, timeout: float = 3.0) -> bool:
    """Wait until the hotkey thread reaches the needed state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FakeWindows:
    """A system that behaves like `user32`, but without `user32`."""

    def __init__(self, busy: set[tuple[int, int]] | None = None) -> None:
        self.busy = busy or set()          # combos taken by "another program"
        self.registered: dict[int, tuple[int, int]] = {}
        self._queue: list[int] = []
        self._lock = threading.Lock()

    def register(self, hid: int, mods: int, vk: int) -> str:
        if (mods, vk) in self.busy:
            return "taken by another program"
        self.registered[hid] = (mods, vk)
        return ""

    def unregister(self, hid: int) -> None:
        self.registered.pop(hid, None)

    def pressed(self) -> list[int]:
        with self._lock:
            out, self._queue = self._queue, []
        return out

    # -- for the test ----------------------------------------------------------

    def press(self, combo: str) -> None:
        target = parse(combo)
        for hid, registered in self.registered.items():
            if registered == target:
                with self._lock:
                    self._queue.append(hid)
                return
        raise AssertionError(f"'{combo}' is not registered")


@pytest.fixture
def counter():
    """Five actions that count their presses."""
    hits: dict[str, int] = {}
    done = threading.Event()

    def make(name: str):
        def action() -> None:
            hits[name] = hits.get(name, 0) + 1
            done.set()
        return action

    actions = {
        name: make(name)
        for name in ("ask", "hide_main", "hide_panel", "ask_screen", "recognition")
    }
    return actions, hits, done


# --------------------------------------------------------------------------
# Combo parsing
# --------------------------------------------------------------------------


def test_combo_becomes_modifiers_and_a_key():
    mods, vk = parse("ctrl+alt+a")
    assert mods == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    assert vk == ord("A")


def test_held_key_does_not_repeat():
    """A held-down "ask" would otherwise go to the model dozens of times a second."""
    mods, _ = parse("ctrl+alt+x")
    assert mods & MOD_NOREPEAT


def test_defaults_are_all_valid():
    """A default that does not parse is a setting that is broken out of the box."""
    for combo in HotkeySettings().model_dump().values():
        parse(combo)


def test_case_and_spaces_do_not_matter():
    assert parse("Ctrl + Alt + A") == parse("ctrl+alt+a")


@pytest.mark.parametrize("combo, says", [
    ("a", "without ctrl"),                 # would intercept the key from the whole system
    ("ctrl+alt", "only modifiers and no key"),
    ("ctrl+alt+zz", "unknown key"),
    ("ctrl+a+b", "two keys"),
    ("", "empty"),
])
def test_bad_combo_says_what_is_wrong(combo, says):
    """A bare "Invalid combination" without details makes people guess."""
    with pytest.raises(ValueError, match=says):
        parse(combo)


# --------------------------------------------------------------------------
# Binding on a live system
# --------------------------------------------------------------------------


def test_all_five_reach_the_system(counter):
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 5)
        assert keys.problems == []
    finally:
        keys.stop()


def test_busy_combo_is_said_out_loud(counter):
    """Silence here is the worst outcome: the person presses the keys and nothing happens."""
    actions, _, _ = counter
    api = FakeWindows(busy={parse("ctrl+alt+a")})
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 4)
        assert any("ctrl+alt+a" in line and "taken" in line for line in keys.problems)
    finally:
        keys.stop()


def test_one_bad_combo_does_not_take_the_rest_down(counter):
    """A typo in one field must not leave the person without the other four."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start({**HotkeySettings().model_dump(), "ask": "ctrl+alt+zz"})
    try:
        assert wait_for(lambda: len(api.registered) == 4)
        assert any("unknown key" in line for line in keys.problems)
        assert wait_for(lambda: parse("ctrl+alt+p") in api.registered.values())
    finally:
        keys.stop()


def test_empty_combo_is_a_choice_not_a_mistake(counter):
    """An erased field means "I don't need this", and there is nothing to complain about."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start({**HotkeySettings().model_dump(), "ask_screen": ""})
    try:
        assert wait_for(lambda: len(api.registered) == 4)
        assert keys.problems == []
    finally:
        keys.stop()


def test_press_runs_its_own_action(counter):
    actions, hits, done = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 5)
        api.press("ctrl+alt+p")
        assert done.wait(3.0)
        assert wait_for(lambda: hits.get("recognition") == 1)
        assert "ask" not in hits
    finally:
        keys.stop()


def test_rebinding_lets_the_old_combo_go(counter):
    """Otherwise the old combo would stay taken until a restart."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: parse("ctrl+alt+a") in api.registered.values())
        keys.bind({**HotkeySettings().model_dump(), "ask": "ctrl+shift+q"})
        assert wait_for(lambda: parse("ctrl+shift+q") in api.registered.values())
        assert parse("ctrl+alt+a") not in api.registered.values()
    finally:
        keys.stop()


def test_capture_releases_the_combos_and_says_so(counter):
    """Otherwise a combo we hold cannot be assigned again.

    The system gives a keypress to whoever registered it: while we hold "ctrl+alt+a", it
    does not reach the page at all, and instead of assigning, "ask" fires.
    """
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 5)
        # It answers only after the thread has really let go: listening to the keyboard earlier
        # means losing to the first keypress.
        assert keys.suspend(timeout=3.0)
        assert api.registered == {}

        keys.resume()
        assert wait_for(lambda: len(api.registered) == 5)
    finally:
        keys.stop()


def test_capture_returns_exactly_what_was_bound(counter):
    """It must return what was asked for, not the default: the edit was made before the binding."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start({**HotkeySettings().model_dump(), "ask": "ctrl+shift+q"})
    try:
        assert wait_for(lambda: parse("ctrl+shift+q") in api.registered.values())
        keys.suspend(timeout=3.0)
        keys.resume()
        assert wait_for(lambda: parse("ctrl+shift+q") in api.registered.values())
    finally:
        keys.stop()


def test_stop_returns_the_combos_to_the_system(counter):
    """Otherwise "ctrl+alt+a" works nowhere for the twenty seconds the pipeline takes to stop."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.start(HotkeySettings().model_dump())
    assert wait_for(lambda: len(api.registered) == 5)
    keys.stop()
    assert api.registered == {}


# --------------------------------------------------------------------------
# Layouts: AltGr and key position (audit of 2026-10-14, desk-b-4, desk-b-6, desk-b-17)
# --------------------------------------------------------------------------

import sys

from argus.hotkeys import MOD_SHIFT, POSITION_SCAN, WindowsAPI

GERMAN, US = 0x0407, 0x0409
_CHAR_AT = {scan: char for char, scan in POSITION_SCAN.items()}


class FakeUser32:
    """`user32` with two layouts: German (Z sits where Y is, AltGr+Q types "@") and American
    (Ctrl+letter gives a control character, as on a real system)."""

    def __init__(self, layouts=(GERMAN, US), current=GERMAN) -> None:
        self.layouts, self.current = list(layouts), current
        self.registered: list[tuple[int, int, int]] = []

    def GetKeyboardLayout(self, thread):
        return self.current

    def GetKeyboardLayoutList(self, n, buf):
        if n == 0:
            return len(self.layouts)
        for i, handle in enumerate(self.layouts[:n]):
            buf[i] = handle
        return min(n, len(self.layouts))

    def MapVirtualKeyExW(self, scan, kind, layout):
        assert kind == 1, "scan code -> virtual key code"
        char = _CHAR_AT[scan]
        if layout == GERMAN and char in "YZ":
            char = "Z" if char == "Y" else "Y"
        return ord(char)

    def ToUnicodeEx(self, vk, scan, state, buf, size, flags, layout):
        assert state[0x11] == 0x80 and state[0x12] == 0x80, "Ctrl+Alt are held"
        assert flags & 4, "do not change the keyboard state"
        if layout == GERMAN and scan == POSITION_SCAN["Q"]:
            buf[0] = "@"
            return 1
        if layout == US:
            buf[0] = "\x01"
            return 1
        return 0

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        self.registered.append((hid, mods, vk))
        return 1

    def UnregisterHotKey(self, hwnd, hid):
        return 1


def test_a_letter_key_is_registered_where_it_stands_not_by_its_letter():
    """The page records a key by its position (`KeyboardEvent.code`), while `RegisterHotKey`
    expects a layout code: on the German layout Z sits where Y is, so "ctrl+alt+y" would be
    pressed not where the person assigned it."""
    mods, vk = parse("ctrl+alt+y")
    german = FakeUser32(current=GERMAN)
    assert WindowsAPI(german).register(1, mods, vk) == ""
    assert german.registered == [(1, mods, ord("Z"))]

    american = FakeUser32(current=US)
    WindowsAPI(american).register(1, mods, vk)
    assert american.registered == [(1, mods, ord("Y"))]


def test_named_and_function_keys_do_not_depend_on_the_layout():
    api = FakeUser32(current=GERMAN)
    for combo in ("ctrl+alt+f5", "ctrl+alt+space", "ctrl+alt+up", "ctrl+alt+delete"):
        mods, vk = parse(combo)
        WindowsAPI(api).register(1, mods, vk)
        assert api.registered[-1] == (1, mods, vk), combo


def test_a_chord_that_types_a_letter_under_altgr_is_reported_and_not_registered(counter):
    """`ctrl+alt+a` on the Polish layout is ą: a registered combo would swallow the letter in all
    programs.
    """
    actions, _, _ = counter
    system = FakeUser32()
    keys = Hotkeys(actions, api=WindowsAPI(system))

    keys._apply({"ask": "ctrl+alt+q", "hide_main": "ctrl+alt+w"})

    assert [h[2] for h in system.registered] == [ord("W")], "'q' is not registered, the others are as usual"
    assert len(keys.problems) == 1
    assert "ctrl+alt+q" in keys.problems[0] and "'@'" in keys.problems[0]
    from argus.core.friendly import plain

    assert plain(keys.problems[0]), "the line goes to the person"


def test_control_characters_are_not_letters_and_only_ctrl_alt_is_checked():
    api = WindowsAPI(FakeUser32(layouts=(US,)))
    assert api.typed_by_chord(MOD_CONTROL | MOD_ALT, ord("A")) == "", "Ctrl+A gives a control character, not a letter"

    german = WindowsAPI(FakeUser32(layouts=(GERMAN,)))
    assert german.typed_by_chord(MOD_CONTROL | MOD_ALT, ord("Q")) == "@"
    assert german.typed_by_chord(MOD_CONTROL | MOD_SHIFT, ord("Q")) == "", "AltGr is Ctrl+Alt, and only that"
    assert german.typed_by_chord(MOD_ALT, ord("Q")) == ""
    assert german.typed_by_chord(MOD_CONTROL | MOD_ALT, 0x70) == "", "F keys print nothing"


def test_every_installed_layout_is_checked_not_only_the_current_one():
    """People switch layouts while they work: with the Polish one in the list, "ctrl+alt+a" is
    dangerous even if the American layout is active right now.
    """
    system = FakeUser32(layouts=(US, GERMAN), current=US)
    assert WindowsAPI(system).typed_by_chord(MOD_CONTROL | MOD_ALT, ord("Q")) == "@"


def test_an_api_that_cannot_check_layouts_registers_as_before(counter):
    actions, _, _ = counter
    api = FakeWindows()                                  # without `typed_by_chord`, like Carbon on macOS
    keys = Hotkeys(actions, api=api)
    keys._apply(HotkeySettings().model_dump())

    assert len(api.registered) == 5 and keys.problems == []


@pytest.mark.skipif(sys.platform != "win32", reason="user32")
def test_the_real_layout_calls_work_on_this_machine():
    """Real calls (argument types, pointer-sized layout handles): a string, not a crash."""
    api = WindowsAPI()
    mods, vk = parse("ctrl+alt+a")
    assert isinstance(api.typed_by_chord(mods, vk), str)
    assert isinstance(api.typed_by_chord(*parse("ctrl+alt+3")), str)
    assert 0x30 <= api._vk_on(ord("Y"), api._u32.GetKeyboardLayout(0)) <= 0x5A


def test_hotkeys_come_back_by_themselves_if_the_page_never_resumes(counter):
    """The page released the combos for the time of the assignment and closed without returning
    them: the "hide" combo and the others would stay released until a restart."""
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.SUSPEND_MAX_S = 0.3
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 5)
        assert keys.suspend(timeout=3.0)
        assert api.registered == {}

        assert wait_for(lambda: len(api.registered) == 5, timeout=3.0), "they came back by themselves, resume was never called"
    finally:
        keys.stop()


def test_a_resumed_page_does_not_trigger_the_deadman_again(counter):
    actions, _, _ = counter
    api = FakeWindows()
    keys = Hotkeys(actions, api=api)
    keys.SUSPEND_MAX_S = 0.3
    keys.start(HotkeySettings().model_dump())
    try:
        assert wait_for(lambda: len(api.registered) == 5)
        keys.suspend(timeout=3.0)
        keys.resume()
        assert wait_for(lambda: len(api.registered) == 5)
        keys.bind({**HotkeySettings().model_dump(), "ask": "ctrl+shift+q"})
        time.sleep(0.6)                                  # longer than the deadline: the watchdog must not "return" anything
        assert parse("ctrl+shift+q") in api.registered.values()
        assert parse("ctrl+alt+a") not in api.registered.values()
    finally:
        keys.stop()
