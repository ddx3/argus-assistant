"""Hotkeys on macOS: combo translation and the shared `Hotkeys` over a fake Carbon.

Carbon exists only on macOS, so its five calls (`CarbonFacade`) are replaced by an
object with the same methods. The tests check what does not depend on the system:
translation of Windows bits and codes into Carbon codes (per `Events.h`), answers in
words to refusals, the queue of key presses, and that the shared `Hotkeys` class works
on this system the same way as on Windows.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from argus.assist.settings import Hotkeys as HotkeySettings
from argus.hotkeys import Hotkeys, parse
from argus.mac import hotkeys as mac


def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class FakeCarbon:
    """Five Carbon calls: install a handler, register, release."""

    def __init__(self, busy: set[tuple[int, int]] | None = None, install_status: int = 0) -> None:
        self.busy = busy or set()
        self.install_status = install_status
        self.installs = 0
        self.handler = None
        self.registered: dict[int, tuple[int, int]] = {}
        self.statuses: dict[tuple[int, int], int] = {}

    def install(self, on_hotkey):
        self.installs += 1
        self.handler = on_hotkey
        return self.install_status

    def register(self, number, modifiers, key):
        if (modifiers, key) in self.busy:
            return mac.EVENT_HOTKEY_EXISTS, None
        if (modifiers, key) in self.statuses:
            return self.statuses[(modifiers, key)], None
        self.registered[number] = (modifiers, key)
        return 0, f"ref-{number}"

    def unregister(self, ref):
        self.registered.pop(int(str(ref).split("-")[1]), None)

    def press(self, number: int) -> None:
        self.handler(number)


def api(carbon: FakeCarbon) -> mac.MacAPI:
    return mac.MacAPI(facade=carbon, run_on_main=lambda f: f())


# --------------------------------------------------------------------------
# Translating codes
# --------------------------------------------------------------------------


def translate(combo: str):
    mods, vk = parse(combo)
    return mac.to_carbon(mods, vk)


def test_ctrl_alt_letter_becomes_control_option_and_the_ansi_position():
    assert translate("ctrl+alt+a") == (mac.CONTROL_KEY | mac.OPTION_KEY, 0x00)
    assert translate("ctrl+alt+s") == (mac.CONTROL_KEY | mac.OPTION_KEY, 0x01)
    assert translate("ctrl+alt+p") == (mac.CONTROL_KEY | mac.OPTION_KEY, 0x23)


def test_cmd_is_command_and_win_is_the_same_key_on_the_mac():
    assert translate("cmd+shift+z")[0] == mac.CMD_KEY | mac.SHIFT_KEY
    assert translate("win+z") == translate("cmd+z") == translate("command+z")
    assert translate("option+x") == translate("alt+x")


def test_digits_function_and_named_keys():
    assert translate("ctrl+1")[1] == 0x12 and translate("ctrl+0")[1] == 0x1D
    assert translate("ctrl+f1")[1] == 0x7A and translate("ctrl+f12")[1] == 0x6F
    assert translate("ctrl+space")[1] == 0x31
    assert translate("ctrl+esc")[1] == 0x35
    assert translate("ctrl+enter")[1] == 0x24
    assert translate("ctrl+up")[1] == 0x7E and translate("ctrl+left")[1] == 0x7B
    assert translate("ctrl+home")[1] == 0x73 and translate("ctrl+end")[1] == 0x77
    assert translate("ctrl+delete")[1] == 0x75


def test_every_letter_and_digit_has_a_distinct_key():
    codes = {}
    for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
        got = translate(f"ctrl+{ch}")
        assert got is not None, ch
        codes[ch] = got[1]
    assert len(set(codes.values())) == len(codes)


def test_keys_that_the_mac_does_not_have_are_refused_in_words():
    assert translate("ctrl+insert") is None
    assert translate("ctrl+f24") is None


def test_the_default_combos_all_exist_on_the_mac():
    """A default that does not exist on the Mac is a setting broken out of the box."""
    for combo in HotkeySettings().model_dump().values():
        assert translate(combo) is not None, combo


# --------------------------------------------------------------------------
# MacAPI
# --------------------------------------------------------------------------


def test_a_combo_is_registered_with_carbon_codes():
    carbon = FakeCarbon()
    mac_api = api(carbon)
    mods, vk = parse("ctrl+alt+a")
    assert mac_api.register(1, mods, vk) == ""
    assert carbon.registered[1] == (mac.CONTROL_KEY | mac.OPTION_KEY, 0x00)


def test_the_handler_is_installed_once_however_many_combos():
    carbon = FakeCarbon()
    mac_api = api(carbon)
    for number, combo in enumerate(("ctrl+alt+a", "ctrl+alt+s", "ctrl+alt+p"), start=1):
        assert mac_api.register(number, *parse(combo)) == ""
    assert carbon.installs == 1


def test_a_combo_already_given_to_another_argus_action_is_said_in_words():
    """Carbon does not notice a clash with someone else's program, so a refusal can only come from
    ourselves.
    """
    mods, vk = parse("ctrl+alt+a")
    carbon = FakeCarbon(busy={mac.to_carbon(mods, vk)})
    assert api(carbon).register(1, mods, vk) == "this combination is already assigned to another Argus action"


def test_a_combo_with_only_option_is_explained_on_the_systems_that_refuse_it():
    """macOS 15.0–15.1 answers such combos with the generic error -9868."""
    only_option, only_shift = parse("alt+x"), parse("shift+alt+x")
    carbon = FakeCarbon()
    carbon.statuses[mac.to_carbon(*only_option)] = mac.EVENT_INTERNAL_ERROR
    carbon.statuses[mac.to_carbon(*only_shift)] = mac.EVENT_INTERNAL_ERROR
    for combo in (only_option, only_shift):
        assert "Control or Command" in api(carbon).register(1, *combo)


def test_the_general_error_with_a_control_or_command_combo_is_not_blamed_on_option():
    carbon = FakeCarbon()
    for combo in ("ctrl+alt+a", "cmd+x"):
        carbon.statuses[mac.to_carbon(*parse(combo))] = mac.EVENT_INTERNAL_ERROR
        assert api(carbon).register(1, *parse(combo)) == "the system refused"


def test_a_handler_is_never_installed_twice_even_after_a_timeout(monkeypatch):
    """The previous request did not wait for the main thread but was executed later: a second
    handler would release the first.
    """
    import ctypes

    calls = []

    class Carbon:
        def GetApplicationEventTarget(self):
            return 1

        def InstallEventHandler(self, target, callback, count, spec, data, ref):
            calls.append(count)
            ref._obj.value = 0x1234          # the system writes a reference to the handler here
            return 0

    facade = mac.CarbonFacade.__new__(mac.CarbonFacade)
    facade._c, facade._callback, facade._handler_ref = Carbon(), None, ctypes.c_void_p()
    assert facade.install(lambda number: None) == 0
    first = facade._callback
    assert facade.install(lambda number: None) == 0
    assert calls == [1] and facade._callback is first, "the second time the system is not touched, the first function is alive"


def test_any_other_refusal_is_general_and_a_missing_key_is_named():
    mods, vk = parse("ctrl+alt+a")
    carbon = FakeCarbon()
    carbon.statuses[mac.to_carbon(mods, vk)] = -50
    assert api(carbon).register(1, mods, vk) == "the system refused"
    assert api(FakeCarbon()).register(2, *parse("ctrl+insert")) == "this key does not exist on a Mac"


def test_a_handler_the_system_refused_means_no_combos_but_no_crash():
    carbon = FakeCarbon(install_status=-1)
    assert api(carbon).register(1, *parse("ctrl+alt+a")) == "the system refused"


def test_a_main_thread_that_never_answers_is_a_refusal_not_a_hang():
    def stuck(_func):
        raise TimeoutError("the main thread did not answer")

    assert mac.MacAPI(facade=FakeCarbon(), run_on_main=stuck).register(1, *parse("ctrl+alt+a")) == \
        "the system did not respond"


def test_a_missing_system_library_is_a_refusal_not_a_crash():
    def broken(_func):
        raise ImportError("PyObjCTools")

    assert mac.MacAPI(facade=FakeCarbon(), run_on_main=broken).register(1, *parse("ctrl+alt+a")) == \
        "the system refused"


def test_presses_are_queued_and_taken_once():
    carbon = FakeCarbon()
    mac_api = api(carbon)
    mac_api.register(1, *parse("ctrl+alt+a"))
    mac_api.register(2, *parse("ctrl+alt+s"))
    carbon.press(2)
    carbon.press(1)
    carbon.press(2)
    assert mac_api.pressed() == [2, 1, 2]
    assert mac_api.pressed() == []


def test_unregister_releases_the_combo_and_forgetting_one_is_harmless():
    carbon = FakeCarbon()
    mac_api = api(carbon)
    mac_api.register(1, *parse("ctrl+alt+a"))
    mac_api.unregister(1)
    assert carbon.registered == {}
    mac_api.unregister(1)
    mac_api.unregister(99)


# --------------------------------------------------------------------------
# The shared Hotkeys over MacAPI
# --------------------------------------------------------------------------


@pytest.fixture
def actions():
    hits: dict[str, int] = {}
    done = threading.Event()

    def make(name):
        def action():
            hits[name] = hits.get(name, 0) + 1
            done.set()
        return action

    return {n: make(n) for n in ("ask", "hide_main", "hide_panel", "ask_screen", "recognition")}, hits, done


def test_the_shared_class_fires_actions_from_carbon_presses(actions):
    acts, hits, done = actions
    carbon = FakeCarbon()
    keys = Hotkeys(acts, api=api(carbon))
    keys.start({"ask": "ctrl+alt+a", "hide_main": "ctrl+alt+w"})
    try:
        assert wait_for(lambda: len(carbon.registered) == 2)
        assert not keys.problems
        number = next(n for n, (_m, k) in carbon.registered.items() if k == 0x00)
        carbon.press(number)
        assert done.wait(3.0) and hits == {"ask": 1}
    finally:
        keys.stop()
    assert carbon.registered == {}, "on exit the combos are released"


def test_a_busy_combo_lands_in_problems_like_on_windows(actions):
    acts, _hits, _done = actions
    mods, vk = parse("ctrl+alt+a")
    carbon = FakeCarbon(busy={mac.to_carbon(mods, vk)})
    keys = Hotkeys(acts, api=api(carbon))
    keys.start({"ask": "ctrl+alt+a"})
    try:
        assert wait_for(lambda: keys.problems)
        assert "already assigned to another" in keys.problems[0]
    finally:
        keys.stop()


def test_the_default_api_on_macos_is_the_carbon_one(monkeypatch):
    from argus import hotkeys

    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(hotkeys.default_api(), mac.MacAPI)


def test_releasing_a_combo_does_not_wait_for_the_main_thread():
    """On exit the window loop is already stopped: waiting for it would mean idling for a full
    timeout on every combo.
    """
    carbon = FakeCarbon()
    queued = []
    mac_api = mac.MacAPI(facade=carbon, run_on_main=lambda f: f(), post_on_main=queued.append)

    assert mac_api.register(1, *parse("ctrl+alt+a")) == ""
    assert carbon.registered, "registration waits: its result is needed"
    mac_api.unregister(1)
    assert len(queued) == 1 and carbon.registered, "releasing is only queued, it waits for nothing"
    queued[0]()
    assert carbon.registered == {}


def test_the_default_post_is_a_queue_not_a_wait():
    assert mac.MacAPI()._post is mac._post_to_main_thread
