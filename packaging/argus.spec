# -*- mode: python ; coding: utf-8 -*-
"""App build. Run: uv run pyinstaller packaging/argus.spec

It is built as a folder, not as a single file, and this is not about convenience. A
single-file app unpacks hundreds of megabytes into a temporary folder on every start:
seconds of waiting at the moment when the person has already opened the call window.

We do not put model weights inside. Whisper is 1.6 GB, and it is now insurance rather
than the main path: nobody will download an installer that weighs two gigabytes for an
engine that kicks in when the connection drops. `models/` sits next to the exe and is
downloaded with a button.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# A debug build: with a console you can see what the startup crashes on. Without it a
# crashed app simply does not open, and there is nothing to say why.
CONSOLE = bool(os.environ.get("ARGUS_BUILD_CONSOLE"))

# Whether to include the subscription path to the model inside.
#
# `claude-agent-sdk` brings claude.exe along: that is 217 MB of 675, a third of the whole
# build. It is needed for development: on your own machine the model goes through a
# subscription. It is not suitable for distribution under its license: the subscription
# does not cover a product for other people, and then these 217 MB ship for nothing.
#
# The default is to include it: what is built today must work the same as from source. A
# distribution build sets ARGUS_BUILD_NO_SUBSCRIPTION=1 and switches brain.provider to
# anthropic-api.
NO_SUBSCRIPTION = bool(os.environ.get("ARGUS_BUILD_NO_SUBSCRIPTION"))

# A build in which all recognition goes over the network.
#
# It drops the local engines: Whisper with its CTranslate2, and screen reading. The point
# is not the 110 megabytes of code but the 1.6 gigabytes of weights that are no longer
# needed with them: the app stops downloading anything on first launch on a stranger's
# machine. The price is honest: if the network flickers, there is no recognition at all
# and nothing to pick up the utterance.
#
# The config must match: stt.compare_with = "" and ocr.enabled = false. Otherwise the
# program builds but crashes on the first attempt to start them.
API_ONLY = bool(os.environ.get("ARGUS_BUILD_API_ONLY"))

LOCAL_ENGINES = ("ctranslate2", "faster_whisper", "rapidocr")

# Data that modules look for next to themselves (`Path(__file__).parent`). Inside the
# build this works only if they are placed at the same relative path.
#
# `design/` is not present in every branch (it is absent in master), so we go by presence,
# not by a list: a build of a branch without it would silently end up without the shared
# stylesheet, and the pages would arrive as bare HTML.
# The copy stays: it is what the person who is given the build will see. But on the
# development machine the app reads not it but the `src/` next to it; otherwise the page
# in the build freezes while the sources live on, and two indistinguishable versions end
# up on the same port 8420. See `argus/live.py`.
datas = [("../src/argus/ui/static", "argus/ui/static")]
if os.path.isdir(os.path.join(os.path.dirname(SPEC), "..", "src", "argus", "design")):
    datas.append(("../src/argus/design", "argus/design"))
binaries = []
hiddenimports = []

# Packages with native libraries and data: they must be taken whole. Without this the
# build passes but fails at the user's, on the first frame or on the first utterance,
# wherever a DLL is loaded late.
for name in (
    "onnxruntime",       # VAD: always needed, we cut speech ourselves
    *(() if API_ONLY else LOCAL_ENGINES),
    "cv2",               # screen snapshot to JPEG; needed in the network build too
    "pyaudiowpatch",     # WASAPI
    "bettercam",
    "windows_capture",
    "soxr",
    "webview",           # window; pulls in pythonnet
    "clr_loader",
    "anthropic",
    *(() if NO_SUBSCRIPTION else ("claude_agent_sdk",)),
):
    try:
        d, b, h = collect_all(name)
    except Exception:                       # noqa: BLE001 — the package may be missing
        continue
    datas += d
    binaries += b
    hiddenimports += h

# uvicorn picks implementations at runtime, by strings in the config; static analysis does
# not see them.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += ["websockets", "websockets.legacy", "argus.cli"]

a = Analysis(
    ["argus_app.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=(
        ["tkinter", "matplotlib", "pytest", "IPython"]
        # `cffi` (through `clr_loader`) pulls in `setuptools`, and because of this PyInstaller
        # adds its own runtime hook to the build, which imports it on EVERY launch: 130-150 ms
        # before the first line of our code (measured 2026-10-07) and 149 extra modules in the
        # archive. The app does not need it at runtime, only `cffi` does when building extensions.
        + ["setuptools", "pkg_resources", "_distutils_hack"]
        + (["claude_agent_sdk"] if NO_SUBSCRIPTION else [])
        + (list(LOCAL_ENGINES) if API_ONLY else [])
    ),
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Argus",
    debug=False,
    strip=False,
    upx=False,
    # A window, not a terminal: see CONSOLE above.
    console=CONSOLE,
    # Our own icon is not decoration. Without it Windows draws PyInstaller's standard one, and
    # in the taskbar the app is indistinguishable from someone else's; the person cannot pin
    # it because they cannot find it by eye.
    icon=os.path.join(os.path.dirname(SPEC), "argus.ico"),
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Argus",
)
