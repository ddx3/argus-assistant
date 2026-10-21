# -*- mode: python ; coding: utf-8 -*-
"""Build of the app for macOS: `Argus.app`. Run on macOS: `packaging/build_macos.sh`.

A separate file rather than a branch inside `argus.spec`: the Windows spec is edited in
other work, and a shared file with conditions for two systems would become a place where
a fix for one breaks the other. What is common (the set of data and the exclusions) is
repeated here on purpose, briefly.

What sets the macOS package apart.

* **Data is not inside the package.** The `.app` is signed, and a file written into it
  breaks the signature, while an app launched from Downloads is mounted by the system
  read-only. So everything mutable lives in `~/Library/Application Support/Argus`
  (`argus/host.py`), and the package contains seeds (`seed/`: `argus.toml`, `.env`, the
  speech detector weights, `VERSION`) from which that folder is filled on first launch
  and on a version change.
* **The audio helper next to the program.** `argus-syscap` (Swift, `macos/helper`) is
  put into the package by the build script: nested programs must be signed before the
  package itself.
* **Signing and entitlements.** Everything is signed by `build_macos.sh` alone: from the
  inside out, with a real identity (`ARGUS_CODESIGN_IDENTITY`) or a trial signature
  (`-`), with which the program starts on this same computer, while on other people's
  computers the system will not let it through without a manual permission. PyInstaller
  signs here only as a trial and without the network (the system itself needs this for
  arm64): signing twice for real would mean going to Apple's time server twice as often,
  and a single network failure would bring down the whole build. The entitlements are in
  `packaging/macos/entitlements.plist`, a file in Latin script only (Apple: the
  entitlements list must be ASCII; Cyrillic in it breaks notarization), so the
  explanations are here:
    - `device.audio-input`: microphone; without the entitlement the stream opens and
      returns silence;
    - `cs.allow-unsigned-executable-memory`: Python and ctypes create functions in
      memory on the fly (libffi, audio callbacks); without the entitlement the process
      crashes on the first call;
    - `cs.disable-library-validation`: a reserve; we sign all libraries in the package
      ourselves, but wheels bring foreign ones too.
  There is no screen recording entitlement here and there cannot be: it is the person's
  permission (System Settings -> Privacy & Security), not a signing entitlement.
* **Built for the architecture of the computer that does the build** (arm64 on Apple
  Silicon): wheels of some dependencies (`opencv`, `onnxruntime`) are per-architecture,
  not universal2. The `onnxruntime` wheel in the lock file is built for macOS 14, so the
  minimum is macOS 14 too.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.dirname(SPEC)

BUNDLE_ID = os.environ.get("ARGUS_BUNDLE_ID", "com.argus.assistant")
VERSION = os.environ.get("ARGUS_VERSION", "1.0")
BUILD = os.environ.get("ARGUS_BUILD", VERSION)
IDENTITY = os.environ.get("ARGUS_CODESIGN_IDENTITY") or None
ENTITLEMENTS = os.path.join(HERE, "macos", "entitlements.plist")
SEED = os.environ.get("ARGUS_SEED_DIR", "")

# The same switches as in the Windows build (see `argus.spec`): a distributed build
# carries neither the subscription path to the model nor the local engines.
NO_SUBSCRIPTION = bool(os.environ.get("ARGUS_BUILD_NO_SUBSCRIPTION"))
API_ONLY = bool(os.environ.get("ARGUS_BUILD_API_ONLY"))
LOCAL_ENGINES = ("ctranslate2", "faster_whisper", "rapidocr")

datas = [("../src/argus/ui/static", "argus/ui/static")]
if os.path.isdir(os.path.join(HERE, "..", "src", "argus", "design")):
    datas.append(("../src/argus/design", "argus/design"))
if SEED and os.path.isdir(SEED):
    datas.append((SEED, "seed"))

# The interface language is different for each person; the system microphone prompt must
# speak it too, instead of Russian for everyone. The main text in Info.plist is English,
# and `<language>.lproj/InfoPlist.strings` (`packaging/macos/lproj`) replaces it according
# to the system language.
LANGUAGES = ("ru", "uk", "en", "es", "pt", "de", "fr", "tr", "pl")
for lang in LANGUAGES:
    datas.append((os.path.join(HERE, "macos", "lproj", f"{lang}.lproj", "InfoPlist.strings"), f"{lang}.lproj"))
binaries = []
hiddenimports = []

# Packages with native libraries and data: taken whole, otherwise the build passes and
# fails at the person's, on the first frame or the first utterance.
for name in (
    "onnxruntime",
    *(() if API_ONLY else LOCAL_ENGINES),
    "cv2",
    "sounddevice",       # microphone: PortAudio sits inside the wheel
    "_sounddevice_data",
    "soxr",
    "webview",
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

# The PyObjC wrappers are imported inside functions and conditionally on the platform;
# static analysis may not see them, and without them there is no window, no permissions
# and no hotkeys.
hiddenimports += [
    "objc", "Foundation", "AppKit", "WebKit", "Quartz", "AVFoundation", "CoreMedia",
    "UniformTypeIdentifiers", "Security", "PyObjCTools", "PyObjCTools.AppHelper",
    "argus.mac.audio", "argus.mac.camera", "argus.mac.coreaudio", "argus.mac.desktop",
    "argus.mac.doctor", "argus.mac.hotkeys", "argus.mac.mainthread", "argus.mac.permissions",
    "argus.mac.syscap", "argus.mac.update", "argus.mac.windows",
]
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
        + ["setuptools", "pkg_resources", "_distutils_hack"]
        + ["pythonnet", "clr_loader", "pyaudiowpatch", "bettercam", "windows_capture", "win32gui"]
        + (["claude_agent_sdk"] if NO_SUBSCRIPTION else [])
        + (list(LOCAL_ENGINES) if API_ONLY else [])
    ),
    noarchive=False,
)

# Wheels bring foreign systems' files: DLLs for Windows (WebView2 in pywebview, PortAudio
# in sounddevice). They have no business in the macOS package, and PyInstaller keeps only
# Mach-O files in `Contents/Frameworks`: signing such a package would trip over a
# "foreign" file.
FOREIGN = (".dll", ".exe", ".pyd", ".lib")
a.binaries = [item for item in a.binaries if not item[0].lower().endswith(FOREIGN)]
a.datas = [item for item in a.datas if not item[0].lower().endswith(FOREIGN)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # `faulthandler`: on a crash in native code and on SIGABRT the stacks of all threads go to
    # the log. Without it "the window opened and then silence" cannot be diagnosed: the log is
    # silent and there are no Python stacks.
    [("X faulthandler", None, "OPTION")],
    exclude_binaries=True,
    name="Argus",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    codesign_identity=None,          # the real signature is applied by build_macos.sh (see the header)
    entitlements_file=ENTITLEMENTS,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Argus",
)
app = BUNDLE(
    coll,
    name="Argus.app",
    icon=os.path.join(HERE, "argus.icns"),
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": "Argus",
        "CFBundleDisplayName": "Argus",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": BUILD,
        # The onnxruntime wheel from the lock file is built for macOS 14 (its library will not
        # load on an earlier system), and so is numpy. This also covers the page's WebKit
        # requirement: it needs Safari 16.4 (regular expressions with lookbehind in `index.html`),
        # and macOS 14 has Safari 17.
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": list(LANGUAGES),
        # Without this string the system will not show the microphone prompt but will silently
        # refuse. The language follows the system: see LANGUAGES above.
        "NSMicrophoneUsageDescription":
            "Argus listens to the microphone to understand what you say and to answer in time.",
        # The window shows a page from a local server on this same computer.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
