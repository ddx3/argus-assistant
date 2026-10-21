"""Which audio capture is installed on this system.

The application calls `AudioCapture` and no longer thinks about the platform: on Windows
it is WASAPI (`capture/audio.py`), on macOS CoreAudio and ScreenCaptureKit
(`mac/audio.py`). Both classes provide the same set: `open`, `close`, `reopen`,
`healthy`, `first_error`, `complaints`, `inputs`.

The shared types (`DeviceInfo`, `DeviceNotFound`) live in `capture/audio.py` and are
imported from there on any system; this module does not affect them.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    from argus.mac.audio import MacAudioCapture as AudioCapture
else:
    from argus.capture.audio import AudioCapture

__all__ = ["AudioCapture"]
