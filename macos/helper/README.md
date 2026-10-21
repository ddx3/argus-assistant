# argus-syscap

A small Swift program: it gives Argus the **system audio** on macOS (ScreenCaptureKit, macOS 13+). The contract with the app
(what goes to stdout, what to stderr, the arguments, the exit codes) is in the header of `Sources/ArgusSyscap/main.swift`; on the
Python side it is launched by `src/argus/mac/syscap.py`. Why it is a separate program and not a call from Python is explained
in `docs/macos.md`.

```
swift build -c release            # in this folder; the result is .build/release/argus-syscap
.build/release/argus-syscap --probe   # is the screen and audio recording permission there; code 0 — yes, 2 — no
```

By hand, to hear what it gives out (the audio is raw int16, 48 kHz, 2 channels):

```
.build/release/argus-syscap > /tmp/system.raw          # Ctrl+C to stop
ffplay -f s16le -ar 48000 -ac 2 /tmp/system.raw        # if ffmpeg is installed
```

The first launch asks for the screen-recording permission — for the **program it is launched from** (Terminal); inside
`Argus.app` the permission belongs to the app itself. Change the contract only together with `syscap.py`; the cross-check is done
by `tests/test_mac_helper_contract.py`.

It was written without a Mac, but it compiles and runs on real macOS: CI (`.github/workflows/macos.yml`) builds the helper, and
the package smoke test launches it; CI does not check that it gives out real sound — that needs a live Mac (`docs/macos.md`,
"First check on a Mac").
