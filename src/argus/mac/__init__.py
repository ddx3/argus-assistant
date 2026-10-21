"""Everything in Argus that is tied to macOS, in one folder.

The Windows code stays where it was (`capture/audio.py`, `capture/wgc.py`,
`desktop.py`, `hotkeys.py`); its macOS counterparts live here. Shared modules call them
lazily and only on macOS, so the package itself can be imported anywhere: PyObjC,
`sounddevice` and the system libraries are loaded inside functions, at call time. This
way the tests on Windows check the logic against stand-in systems, and the real check
is done by CI on macOS (`.github/workflows/macos.yml`).

    permissions   the "Screen Recording" and "Microphone" rights: query, request, open
                  the settings
    coreaudio     the live list of audio devices and the default devices
    syscap        the `argus-syscap` helper (Swift, ScreenCaptureKit): system audio
    audio         `MacAudioCapture`, the same set as `capture.audio.AudioCapture`
    camera        screen frames
    windows       the active window, the window list, a window snapshot
    hotkeys       global key combinations (Carbon)
    desktop       the app's windows: invisibility to recording, panels, second launch
    update        self-update of the `.app`

About limitations. There are many calls here that cannot be tested on Windows; what
exactly is verified, and how, is in `docs/macos.md`.
"""
