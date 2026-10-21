"""The permissions without which nothing can be heard or seen on macOS.

There are two permissions, and both have the same difficult nature:

* **Microphone.** Without it the stream opens and faithfully delivers digital silence.
  That is not an error, just zeros. From the outside it looks like "the program does not
  hear", and the cause cannot be found from the levels alone. So we ask the system about
  the permission directly instead of guessing from the silence.
* **Screen recording** (in newer systems, "Screen & System Audio Recording"). It is
  needed for both the picture and the system sound. Without it a screenshot silently
  consists of the wallpaper alone, the titles of other apps' windows are missing, and
  the list of windows to pin is empty. After the permission is granted, the program must
  be restarted: the system reads it once per process.

System calls go through PyObjC, inside functions. If PyObjC is missing (tests on Windows,
a build without it), we answer "unknown" instead of crashing: not knowing is no reason to
refuse the user.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# Permission states. "unknown" means the system did not answer or there is nothing to ask
# with.
GRANTED = "granted"
DENIED = "denied"
NOT_DETERMINED = "not_determined"   # not asked yet
RESTRICTED = "restricted"           # forbidden by policy (MDM, parental controls)
UNKNOWN = "unknown"

# Where to lead the user in the settings. The `x-apple.systempreferences:` addresses work
# in macOS 13+ System Settings and in the earlier ones.
PANES = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}

# Words for the user. Every phrase must pass `friendly.plain`: shorter than 160
# characters, in plain English and with no traces of internals. Otherwise the page
# replaces it with the generic "stream stopped"; a test guards
# this. The row in the settings was called "Screen Recording", and since
# macOS 15 it is "Screen & System Audio Recording"; we
# write the general name.
SCREEN_ADVICE = (
    "No permission to record the screen and system audio. Turn on Argus in System Settings, "
    "in the “Screen Recording” section, and restart the app."
)
MIC_ADVICE = (
    "No permission to use the microphone. Turn on Argus in System Settings, in the “Microphone” section."
)
MIC_RESTRICTED = "Microphone access is blocked by this computer's settings."


def screen_recording() -> str:
    """Whether there is permission to capture the screen and listen to system audio."""
    try:
        import Quartz

        return GRANTED if Quartz.CGPreflightScreenCaptureAccess() else DENIED
    except Exception as e:  # noqa: BLE001 — no PyObjC, or no such function in this system
        log.debug("cannot find out the screen recording permission: %s", e)
        return UNKNOWN


def request_screen_recording() -> bool:
    """Show the system prompt. Returns whether the permission is already granted right now.

    The prompt is shown once per installation; a repeated call shows nothing and only returns
    the answer, and then the user is led to the settings (`open_pane`). It does not block:
    the system draws the prompt window itself.
    """
    try:
        import Quartz

        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception as e:  # noqa: BLE001
        log.debug("screen recording permission request failed: %s", e)
        return False


def microphone() -> str:
    """The state of the microphone permission."""
    try:
        import AVFoundation

        status = int(
            AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                AVFoundation.AVMediaTypeAudio
            )
        )
    except Exception as e:  # noqa: BLE001
        log.debug("cannot determine microphone permission: %s", e)
        return UNKNOWN
    return {0: NOT_DETERMINED, 1: RESTRICTED, 2: DENIED, 3: GRANTED}.get(status, UNKNOWN)


def request_microphone() -> None:
    """Show the system microphone prompt if it has not been shown yet.

    The answer arrives later, on a system thread; we do not need it, because the state is
    read through `microphone()`. Without the request, the microphone stream would get no
    permission at all and would silently deliver silence.
    """
    try:
        import AVFoundation

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda granted: None
        )
    except Exception as e:  # noqa: BLE001
        log.debug("microphone permission request failed: %s", e)


def open_pane(which: str) -> bool:
    """Open the needed System Settings page. Returns whether it worked."""
    url = PANES.get(which)
    if url is None or sys.platform != "darwin":
        return False
    try:
        subprocess.run(["open", url], check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("System Settings did not open: %s", e)
        return False
    return True


def advice(kind: str, state: str) -> str:
    """What to tell the person about a permission. Empty means all is well or there is nothing to
    say.
    """
    if kind == "screen":
        return SCREEN_ADVICE if state == DENIED else ""
    if kind == "microphone":
        if state == DENIED:
            return MIC_ADVICE
        if state == RESTRICTED:
            return MIC_RESTRICTED
    return ""
