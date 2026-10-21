# Building the app

```
uv run pyinstaller --noconfirm packaging/argus.spec
uv run python packaging/stage.py
```

The result is in `dist/Argus`. The second command puts `argus.toml`, `.env` and `models/silero_vad.onnx` next to `Argus.exe` —
the program computes the root from its own location, not from the current folder: a shortcut launches it with any current folder.

## Building for distribution on Windows: one command

```
powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1            # builds into ..\_winbuild
powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1 -Out D:\x  # into your own folder
```

The script sets **both** build-flavor variables (below), computes the version once and passes it on as `ARGUS_FULL_VERSION` (when
the working folder has edits, a timestamp is appended to the version, and without this `PyInstaller` and `stage.py` would compute
two different versions — the app would "update" again on every launch), builds not into `dist\` (the developer's program may be
running there) and calls `stage.py --public`. A forgotten variable used to put a 217 MB `claude.exe` or the local engines into the
distribution; now that is impossible, and it is also caught at publish time (`deploy/win/publish_win.py`, `docs/updates.md`). On
the Mac, `build_macos.sh` does the same (the version is computed the same way there).

## A build that downloads nothing

```
$env:ARGUS_BUILD_API_ONLY = "1"
uv run pyinstaller --noconfirm packaging/argus.spec
```

It drops the local engines — Whisper and screen reading. The point is not the code but the 1.6 GB of weights that are no longer
needed: without this, on first launch on a stranger's machine the app silently goes to download them from HuggingFace and hangs
until the download finishes.

The config must match, otherwise the program builds but crashes at launch:

```toml
[stt]
compare_with = ""     # no fallback: if the network wobbles, there is no recognition at all
[ocr]
enabled = false
```

## What is inside and why it is so big

675 MB, a third of which is the subscription path to the model: `claude-agent-sdk` brings `claude.exe` along. It is needed for
development, and it is not suitable for distribution under its license. The build without it:

```
$env:ARGUS_BUILD_NO_SUBSCRIPTION = "1"
uv run pyinstaller --noconfirm packaging/argus.spec
```

— and `brain.provider = "anthropic-api"` in the config, otherwise the program has nothing to think with.

The Whisper weights (1.6 GB) are deliberately not put inside. It is now a fallback, not the main path: nobody will download an
installer that weighs two gigabytes for the sake of an engine meant for a network drop. `models/` lies alongside and is downloaded
by a button.

## When it does not open

```
$env:ARGUS_BUILD_CONSOLE = "1"
```

It builds with a console. Without it, a crashed app simply does not appear on screen, and there is nothing to tell why. In the
normal build all output goes into `argus.log` next to the program — that is the first thing to ask to be sent.

## What is still missing

- **A signature.** Without it SmartScreen scares people at every install.
- **An installer.** For now it is a folder that has to be unpacked by hand.
- **An icon.** The default PyInstaller one is in place.

## macOS

It is built on a Mac with a separate spec and one script (this cannot be done on Windows):

```
bash packaging/build_macos.sh
```

The result is `dist-mac/Argus.app`, `Argus-mac.zip` (for self-update), `Argus-mac.dmg` and `latest-mac.json`. The app keeps its data
not next to itself but in `~/Library/Application Support/Argus`: a signed `.app` must not be changed, so everything that
`stage.py` puts down lies in the package as `seed/` and is unpacked on first launch (`build_mac_seed.py`).

Signing and notarization are switched on by variables (`ARGUS_CODESIGN_IDENTITY`, `ARGUS_NOTARY_PROFILE`); without them the
signature is ad-hoc — on other people's machines the system will not open the program. Everything else — what is verified and what
is not, the risks, the first check on a Mac — is in [`docs/macos.md`](../docs/macos.md).
