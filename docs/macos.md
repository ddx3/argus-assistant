# Argus on macOS

State as of 2026-10-12 (evening): **it builds and runs on real macOS (GitHub Actions, macOS 26, arm64), but nobody has tried it
on a Mac yet.** Development was done on Windows, which has no CoreAudio, ScreenCaptureKit or AppKit, so the checks ran on
stand-in systems, and CI supplied the real system (section "What is verified and what is not"). The build is published for
download (section "Distribution"). What still needs a person with a Mac is in the section "First check on a Mac".

## What lives where

The shared code knows nothing about the platform. The Windows code stayed where it was; everything for macOS lives next to it
in `src/argus/mac/`, and the shared modules call it **lazily and only on macOS**, so the package imports anywhere
(`tests/test_portability.py` guards this: a clean interpreter without Windows packages and without `ctypes.windll`).

| What | Windows (as it was) | macOS |
|---|---|---|
| Choice of the audio implementation | `capture/audio.py` | `capture/backend.py` → `mac/audio.py` |
| Microphone | PortAudio (WASAPI) | `sounddevice` (PortAudio in the wheel), devices in `mac/coreaudio.py` |
| System audio | WASAPI loopback | helper `argus-syscap` (Swift, ScreenCaptureKit): `mac/syscap.py`, `macos/helper/` |
| Audio of pinned windows | WASAPI process loopback (`capture/app_loopback.py`) | the same helper with `--pid` |
| Screen | DXGI (`bettercam`) | `mac/camera.py` — Quartz, one pixel per point, ≤5 frames/s |
| Windows (active, list, snapshot) | `win32gui` | `mac/windows.py` — `CGWindowListCopyWindowInfo` |
| Live stream of a pinned window | `windows-capture` | `mac/windows.py: WindowPoller` (polls twice a second) |
| Hotkeys | `RegisterHotKey` | `mac/hotkeys.py` — Carbon `RegisterEventHotKey` (needs no permissions) |
| Windows, invisibility, panels, second launch | `desktop.py` + `user32`/DWM | the same `desktop.py` functions → primitives in `mac/desktop.py` |
| Permissions (screen, microphone) | — | `mac/permissions.py` |
| Self-update | PowerShell script | `mac/update.py` (bash script, signature check) |
| Data | next to `Argus.exe` | `~/Library/Application Support/Argus` (`host.py`), log in `~/Library/Logs/Argus` |
| Build | `packaging/argus.spec`, `stage.py` | `packaging/argus_mac.spec`, `build_macos.sh`, `build_mac_seed.py`, `make_icns.py` |
| CI | — | `.github/workflows/macos.yml` |

The seams in the shared files are early `if sys.platform == "darwin"` checks at the top of functions: `capture/window.py`,
`capture/screen.py`, `capture/wgc.py`, `capture/backend.py` (new), `hotkeys.py` (`default_api`), `desktop.py` (primitives and
`run_desktop`), `update.py`, `recap/pdf.py`, `ui/i18n.py`, `cli.py`, `packaging/argus_app.py`. On Windows the behavior did not
change: the full test run before the edits — 1551 passed, 30 failed; after — 1786 passed, the **same** 30 failed (they call Edge
without a window, and on this machine it returns nothing); there are no new failures.

### Why the data does not live next to the program

The `.app` is signed: any file added inside breaks the signature, and an app launched from "Downloads" is mounted by the system
read-only (App Translocation). So the package carries seed files (`Contents/Resources/seed`: `argus.toml`, `.env`, the weights of
the speech detector, `VERSION`), and at launch `host.seed_user_data` lays them out into the data folder: the seed files are
refreshed when the version changes; personal files (`argus.local.json`, login, recordings, chats) never are.

### System audio: why a separate program

macOS has no analog of WASAPI loopback. Without installing a driver, only ScreenCaptureKit (macOS 13+) gives out the system
audio. It can be called from Python (PyObjC), but assembling audio from a `CMSampleBuffer` means pointers and structures, where a
mistake does not crash but corrupts the sound. So `argus-syscap` is a small Swift program (about 350 lines), and Python only
launches it.

The contract: stdout is raw int16 LE audio, 2 channels, 48 kHz (the same format as in `app_loopback.py`, so the same resampler
works downstream); stderr is JSON lines (`ready`, `error`, `warning`, `stopped`; after `error` the helper exits, a `warning` is
not the end); closing stdin means "the owner died, exit"; `--pid` — audio of these programs only; `--exclude-pid` — do not take
audio of these (first of all Argus itself: its voice-over must not return into recognition); `--probe` — only check the
permission. Exit codes: 0, 1, 2 (no permission), 3, 4.

Fallback path: a virtual device (BlackHole and similar). Set it in `audio.loopback_device` and the sound comes from it like from
an ordinary input.

### A refusal does not crash the launch

No permission to record the screen, or macOS older than 13: the app **opens and listens to the microphone**, and the reason goes
into the page's complaints (`mac/audio.py: _DeadChannel`, `complaints()`). On Windows, with no loopback there is no point in
starting; on a Mac the first launch almost always has no permission, and refusing to open would be the worst first impression.

## Permissions

| Permission | Why | How we ask |
|---|---|---|
| Microphone | your own voice | at launch, `permissions.request_microphone()`; in `Info.plist` — `NSMicrophoneUsageDescription`, in the signature — `device.audio-input` |
| Screen and system audio recording | the screen picture, system audio, titles of other apps' windows | the first helper call shows the prompt itself; settings — "Privacy & Security → Screen Recording" |

After the screen-recording permission is granted, **the program must be restarted**: the system reads it once per process.
The helper runs as a child process, so the request is attributed to `Argus.app`, and in the settings list the person sees
"Argus", not an unnamed file (check on a Mac — item 4 below).

Hotkeys need no permissions: Carbon sees only the registered combinations, not all key presses.

## What is verified and what is not

**Verified on Windows (tests `tests/test_mac_*.py`, `test_portability.py`, 200+ checks):**
the logic of choosing devices by name and the live list; the helper through real pipes (a stand-in program speaks the same
contract); complaints in words and their cleanliness (`friendly.plain`); translation of key combinations into Carbon codes (per
`Events.h`); window primitives on a stand-in AppKit and the real `Panels.open()` on top of them (creation, resizing that keeps
the top edge, "panic" without surfacing, closing); the Swift↔Python contract, cross-checked against the text of both files
(`test_mac_helper_contract.py`); the single-instance lock; the layout of seed files and data; the launch error window; the whole
self-update, including the **real bash swap script and the rollback** (Git Bash); finding a browser for PDF; the system
language; `argus doctor`/`devices`; importing every module without Windows packages and without `ctypes.windll`.

**Verified by reading, not by running:** names and signatures of the Quartz/CoreMedia/AVFoundation calls — from the metadata of
`pyobjc-framework-*` 12.2.2 (the wheels were downloaded and read); the structure of `webview/platforms/cocoa.py` (pywebview
6.2.1: what `window.native` is, how the window is built, what `frameless`/`on_top` do); Swift syntax — with the tree-sitter
parser; `bash -n`, YAML, plist.

**Verified on real macOS (GitHub Actions: `macos-26-arm64`, macOS 26.6.2, Swift 6.3.3; branch `macos-port`, 2026-10-12):**
- The Swift helper **built on the first try** (two warnings about a lock in an `async` function were fixed); `argus-syscap
  --probe` answers `{"ok":true,"event":"probe"}`, code 0 (on CI the screen-recording permission is present).
- Tests: the required group (`test_portability`, `test_mac_*`, `test_hotkeys`, `test_update`) is green; the whole suite — 1762
  passed, the rest are about Windows or about the environment (in the `workflow` the "All tests" step does not block yet).
- The package builds (PyInstaller: 2917 files, `.app` 312 MB, `.dmg` 139 MB); the ad-hoc signature is correct (`codesign --verify
  --deep --strict`); the seed files are in `Contents/Resources/seed` (where `host.seed_dir` looks for them); there is no `.env`
  in the package.
- Launch: the window is shown in ~1.7 s, the server answers in ~2–2.7 s; the microphone and "system audio" open
  (`argus.mac.audio`); the page is served (79 KB), the window draws the login screen in the dark theme, with traffic-light
  buttons and the menu items "Argus / Edit / View" (CI screenshot). The "A" icon from `argus.icns` is in the Dock.
- Hiding from recording works for `screencapture`: a window with `sharingType = .none` is absent from the screenshot (an empty
  desktop) — on macOS 26 the flag is honored for this tool; for Zoom/Meet/QuickTime it still has to be checked by hand.
- **First-launch trap**: an app started through `open` (Finder) without the microphone permission sat on the system prompt until
  the person answered, never reached the server, and closed itself after 30 seconds. So on macOS the wait for the server is 180
  seconds (`desktop.MAC_READY_TIMEOUT_S`); started from the CI terminal, it did not get the prompt (the permission belongs to the
  parent) and came up in 2 seconds. The real fix is to open the microphone after the answer, without holding up the server
  (in the backlog).

**Independent cross-check by reading (2026-10-12, three reviewers without a Mac):** the Swift helper, the Python macOS glue and the
build files were read against Apple's documentation, the SDK headers (`CarbonEvents*.h`, `Events.h`, `MacTypes.h`), the sources
of PyInstaller 6.22, pywebview 6.2.1, PyObjC 12.2.2, sounddevice 0.5.6, and open projects (OBS, Azayaka, Qt, WebRTC, mss). They
found no compile errors in Swift; they did find, and we **fixed**: the window list hid "Docker Desktop" and "Notes — Spotlight
ideas" (service names were filtered out by substring); without the screen permission the window owner's name can be empty (it is
now taken from the program by process number); the signing entitlements list had Cyrillic in comments, while Apple accepts only
ASCII (notarization would have rejected the package half an hour later); the minimum system 13.3 → **14.0** (the `onnxruntime`
and `numpy` wheels from the lock file are built for macOS 14); signing ran three times (now one script, with retries on a
network failure), and the `.dmg` image is now notarized too; Carbon declared "taken by another program" although it does not
notice other programs' combinations; the key handler could be installed twice after a timeout; self-update downloaded an
archive without having anything to verify its signature against; the helper accepted any stream rate (the system silently
substitutes 48 kHz for an unsupported one), its exit was not one-time, and reading stdin could fail with an Objective-C
exception; panels would not have stood on top of another program's full-screen window (risk 12). This is reading, not running:
a reviewer cannot say the code works, only that it agrees with the documentation.

**Not verified yet — needs a person with a Mac:**
whether the helper actually receives sound (CI confirmed the launch and the permission, but not the sound; there is nothing to
play on CI); the ctypes glue for CoreAudio and Carbon (nobody has pressed the hotkeys); panels over another program's
full-screen window and the `ui.mac_overlay` mode; how `WKWebView` draws the other screens (the working screen, "Sessions",
"Record", settings — only the login page has been seen); permission prompts and whom the system
attributes them to; Apple signing and notarization; swapping the package on a real `.app`; `activateIgnoringOtherApps` in
macOS 14+; autorelease pools in long-running PyObjC threads. (The pywebview line `cocoa.py:710` was executed on macOS 26 — the
window was created.)

## Known risks and decisions

1. **"Invisible in recordings" on macOS 15 and newer.** The documented way is implemented —
   `NSWindow.sharingType = .none` (`mac/desktop.py`). Developer reports contradict each other: according to some (Tauri #14200,
   September 2025), ScreenCaptureKit has not honored it since macOS 15; according to others, it works on 26.x too. It can be
   checked only on a real Mac and **with the program the person shares the screen with** (Zoom, Meet, Teams, QuickTime). The
   interface does not promise too much: the function returns "windows are marked hidden" and for macOS 15+ adds "some programs
   still see it — check on yours". One cannot rely on this as protection on Windows either (see `docs/go-to-market.md`).
2. **Signing and notarization.** One script, `build_macos.sh`, signs (PyInstaller signs only ad hoc and without network); the
   entitlements list `packaging/macos/entitlements.plist` must be **strictly ASCII, without comments** (`plutil -lint` and a
   test check it). Without the Apple Developer Program ($99/year) the package gets only an ad-hoc signature: on other people's
   machines the system will not open it without a manual permission ("Open Anyway" in settings, and since macOS 15 the familiar
   right-click no longer works). For distribution a "Developer ID Application" certificate and notarization are needed —
   `build_macos.sh` does both if `ARGUS_CODESIGN_IDENTITY` and `ARGUS_NOTARY_PROFILE` are set (or `APPLE_ID`, `APPLE_TEAM_ID`,
   `APPLE_APP_PASSWORD`). Check that the program's availability and payment work from the operator's jurisdiction (there were
   difficulties in 2020).
3. **Trust in self-update comes from the release signature, not the package signature.** The update arrives from the address in
   the settings, and while that is a bare `http://` (like the Windows version), a swap on the way cannot be told from a real
   package. The basis of trust is the **signed release description** (`latest-mac.json`, Ed25519, the public key is built into the
   app) and the archive hash in it (`docs/updates.md`). On top of this, before replacing, `mac/update.py` checks the package
   integrity (`codesign --verify --deep --strict`), the same package identifier (`com.argus.assistant`) and — **only if the
   running program has a permanent signature** (a certificate, not an ad-hoc one) — the same "code requirement", that is, the
   same signing key: otherwise macOS would reset the microphone and screen-recording permissions. A build with an ad-hoc
   signature **may** update (it has no requirement to compare; this is pinned by the test
   `test_a_trial_signed_program_may_update…`), but the permissions will have to be granted again after every such update — so the
   distributed builds are signed with a permanent certificate (`deploy/mac/signing_setup.py`). The Team ID is not compared: a
   self-signed certificate has no team; it will become meaningful only with Developer ID. The version file is `latest-mac.json`
   next to `latest.json` (`build_macos.sh` puts it there), the archive is `Argus-mac.zip`.
4. **Apple Silicon and macOS 14+ only.** The `opencv` and `onnxruntime` wheels are separate, not universal2, so one build for
   both architectures is impossible. An Intel Mac needs a second build on an Intel machine; in GitHub CI, by their
   announcement, the `macos-15-intel` image remains for this (until August 2027 — to be checked), and then self-update needs its
   own `latest-mac-x86_64.json`.
5. **There are no keys in the build** (since 2026-10-14). Recognition, like answers and voice-over, goes through the account
   server (`server/relay_stt.py`), so `build_mac_seed.py` always builds the seed files "for distribution" — without `.env`, even
   if it lies in the project (and fails if `.env` still ended up in the seed files); `deploy/mac/publish_mac.py` checks the
   archive once more before publishing. If you want to recognize with your own key, put `.env` into the data folder
   (`~/Library/Application Support/Argus`) and turn off `realtime_stt.relay`.
6. **The interface was written for Chromium, and on macOS it is WebKit.** Not verified: `backdrop-filter`, `:has()`,
   `color-mix()`, container queries, `Intl`, the clipboard (`ClipboardItem`), file drag-and-drop, `navigator.locks`. By feature
   checks reviewed (2026-10-12): the pages need WebKit from Safari 16.4 — `index.html` has regular expressions with lookbehind
   (`(?<!`), and on an earlier WebKit the script does not parse at all; `LSMinimumSystemVersion` = 14.0 (it is dictated by the
   wheels, but it also covers this: macOS 14 has Safari 17). `scrollbar-color`/`scrollbar-width` are ignored before Safari
   18.2 — the scrollbars will be the system ones. Checked statically against Safari 16.4 (`eslint-plugin-compat` over the
   scripts, `doiuse` over the styles, 2026-10-12): no findings except partial support for scrollbars, `::marker` and
   `text-decoration`. This is a reference based on MDN data, not a run: walking through the interface by eye is the first thing
   to do after launch.
7. **Voice-over on a hotkey.** WKWebView may refuse to play sound without a gesture in the page (`NotAllowedError`); the player
   handles it (`index.html: makeSpeechPlayer` — "blocked", waits for the first click), but on a hotkey, with no click, the
   voice-over may stay silent. If so, set `mediaTypesRequiringUserActionForPlayback = []` in the WKWebView settings (pywebview
   creates them internally, so a substitution is needed).
8. **Screen on Retina.** Captured at one pixel per point (`kCGWindowImageNominalResolution`): four times less data, and window
   coordinates match the frame pixels. For reading text this is usually enough; if not, change it in `mac/camera.py`.
9. **`CGWindowListCreateImage` is declared deprecated** (in the macOS 15 SDK — "obsoleted"). PyObjC 12 still exposes it, and so
   does the system; if Apple removes it, the fallback is to capture the screen and windows from the helper through
   ScreenCaptureKit (`SCScreenshotManager`, macOS 14+).
10. **PDF** is printed by Chrome/Edge from the command line (`recap/pdf.py`); if neither is present, the person is told honestly
    what to install. The fallback without a browser is printing from `WKWebView` (`printOperation`); not done.
11. **App Nap.** Argus windows are sometimes hidden (by a hotkey while the screen is shared), and it only listens to audio — to
    the system such an app looks unneeded, and it slows its timers and network: replies would arrive late. So at launch it takes
    the activity `NSActivityUserInitiatedAllowingIdleSystemSleep` (`mac/desktop.py: prevent_app_nap`): the timers are accurate,
    and the computer is still allowed to sleep on idle. To check on a Mac: hide the windows, wait a minute, make sure the
    replies arrive without delay (`Activity Monitor` → the "App Nap" column: "No").
12. **Panels over another program's full-screen window (a call, a presentation).** Three conditions at once (an Apple engineer's
    analysis, developer forum, thread 826308): the window is "on all spaces" + "full-screen auxiliary" (done), **screen-saver
    level 1000** (done: `finish_panel`) and an **"agent" app** (the `accessory` policy: no Dock icon and no menu bar) — for an
    ordinary app the system does not layer windows over another program's full-screen space at all. **Verified on real macOS 26
    (CI, 2026-10-13, Safari in full screen): without the mode the panel is not visible, with it the panel stands on top,
    transparent.** The price is the Dock icon and the menu, so the `ui.mac_overlay` setting has three values: `auto` (the
    default) — the app becomes an agent **while a panel is open** and gets its icon back when the last one is closed
    (`desktop._panel_overlay`); `always` — always (open the program from "Applications" or Spotlight); `off` — never. The earlier
    `true`/`false` from the settings are `always`/`off`. To bring a panel back by a hotkey — `orderFrontRegardless`
    (`show_panel`), not pywebview's `window.show()`: that one activates the app and pulls the person out of the call. Not
    verified: typing in a panel in "agent" mode and the behavior of the main window after the policy switch (on CI one can see
    only that the panel stands on top).
13. **Hotkeys (Carbon).** It does not notice a combination clashing with another program or with the system: an error occurs
    only if the same combination is already taken in this process (`-9878`). What happens when a combination is taken by another
    program must be checked by hand. macOS 15.0–15.1 answers with a generic error (`-9868`) for combinations with only Option or
    Shift; the complaints name it in words, and the defaults (`ctrl+alt+…`) do not fall under the restriction.
14. **The window list without the screen-recording permission.** The system does not give out the titles of other programs'
    windows, and on newer versions the owner's name can be empty too — then it is taken from the program by process number
    (`NSRunningApplication`); a window whose name still could not be found does not get into the selection list.
15. **Header and extra window — same as on PC (requirement of 2026-10-13: the interface must be adapted to the Mac so that it
    looks the same as on PC).**
    - *Main window.* Before, the Mac kept the system bar and the page header lay under it as a second row. Now, as on Windows,
      the bar is removed (`mac/desktop.py: use_page_caption` → `NSWindowStyleMaskFullSizeContentView`, the title and the three
      system buttons are hidden), the page gets `desk=1`, and `winbar.js` assembles the whole header into one top block starting
      at the very top of the window. The window buttons are **traffic lights on the left, drawn by the page** (`.winbar-lights`:
      12 px, system colors, icons on hover, gray without focus, hidden in "full screen" mode), not three squares on the right.
      It is our own bar rather than the system buttons moved into place: moving them to the header height (38 px; the system
      one is 28) is possible only through loopholes in the AppKit structure (`NSTitlebarContainerView`) that change from version
      to version and have already changed on macOS 26. The cost: this is an imitation, not real buttons: there is no menu on the
      green one (window tiling), no Option-"+". ⌘W, ⌘M and the "Window" menu items work, and the window is an ordinary one
      (shadow, corners, resizing from the edge).
      **A trap found on real macOS 26 (CI, 2026-10-13):** `FullSizeContentView` alone is not enough — the system bar remains a
      view above the content, and pywebview paints it itself in the "window color" (`cocoa.py`, not for `frameless`), so a gray
      28 px strip covered the top of the header together with the traffic lights. So the bar container (the parent of the parent
      of any system button) is hidden entirely (`_set_titlebar_hidden`); it also no longer intercepts clicks in the top 28 px.
    - *Dragging* — `main_window_action("drag_start"/"drag_move")`: the window moves by `NSEvent.mouseLocation` (like `GetCursorPos`
      on Windows), and the top does not go above the visible area, leaving room for the menu bar. A double click on the bar is
      `zoom`.
    - *Extra window ("All at once", answers, screen).* It used to be opaque: `glass_supported()` was false everywhere
      except Windows 11. Now the window is born ordinary (`transparent=False`), and right after (`desktop._glass` →
      `mac/desktop.py: make_glass`) it becomes transparent: `setOpaque(false)`, `clearColor` background, no shadow, and **KVC
      `drawsBackground = false`** on the `WKWebView` (otherwise white is drawn under the transparent page). We do not use
      pywebview's `transparent=True`: it sets the key `drawsTransparentBackground`, which does not exist in some system
      versions, and then the window is not created at all; with us every key fails "silently", so it will never be worse than an
      opaque window. The page is painted the same way as on Windows (`?glass=1`, veil `rgba(10,12,16,.9)`), and there is no
      blur behind the window — also like on Windows. To check on a Mac by eye: the desktop shows through, there is no white
      fringe, the corners are rounded by the system, dragging by the bar and the resize handle work (pywebview's
      `NSTexturedBackgroundWindowMask` for `frameless=True` has never been run on recent macOS).

## Distribution

Since 2026-10-13 there is no separate download page: the landing buttons (`http://203.0.113.10/`) lead to `/download`, and nginx
itself gives a Mac visitor `Argus-mac.dmg` (a Windows visitor gets `Argus-win.zip`; while it was missing, the landing showed an
explanation). This is the same nginx on ports 80 and 8080 (8080 is the former address, built into the issued builds for
self-update); the layout, addresses and publication of the landing are in `deploy/landing/README.md`. The files lie in
`/opt/argus-server/deploy/server/downloads/`: `Argus-mac.dmg` (what a person downloads), `Argus-mac.zip` and `latest-mac.json`
(for self-update; the release description is signed with our key, and a build with an ad-hoc signature can update, but the
microphone and screen permissions will have to be granted again, see risk 3). The Windows build is also served — a clean
`Argus-win.zip` from 2026-10-13 (`deploy/win/publish_win.py`); the earlier one was withdrawn on 2026-10-09.
What to do on the first launch without an Apple signature is written on the landing's "Help" page (the question about
the Mac).

**Publishing a new build**: `python deploy/mac/publish_mac.py` (`--check` — only download and verify). It takes the latest
successful `Argus-macOS` artifact of the `macos-port` branch from GitHub Actions, verifies it (the image is intact; **no `.env`
and no strings that look like keys**), uploads the files under temporary names, compares size and sha256, and only then renames
them. It no longer touches the page: the "Download for Mac" link works by itself as soon as the file is in
place.

**How it is built**: a push to the `macos-port` branch starts `.github/workflows/macos.yml` (tests → build → the "no keys in the
build" check → artifact → a smoke check with the process's input/output and a window screenshot). The branch is a snapshot of the
working tree (which is far ahead of master), extended by commits with listed files. The free quota is enough for roughly 3–4 full
runs a month (private repository, macOS ×10).

**What the person sees**: an image without an Apple signature — macOS will say it cannot verify the developer ("System Settings →
Privacy & Security → Open Anyway"; the fallback is `xattr -dr com.apple.quarantine /Applications/Argus.app`). It is too early to
distribute widely: signing/notarization is needed (Apple Developer, $99/year). Speech recognition goes through our server
(`[realtime_stt] relay = true`, 2026-10-14), so an OpenAI key is not needed on the person's computer and is not put into the
build; `.env` is needed only for the mode with your own keys (`~/Library/Application Support/Argus/`).

**Download speed**: from the server to the developer's machine a file goes at ~90 KB/s per connection (on all ports; BBR did not
help — this is a per-stream limit, not packet loss; several connections add up). So the image is compressed with LZMA
(`hdiutil -format ULMO`: 103 MB instead of 146). The link "Download in several streams" (six
streams of 4 MB through `Range`, the file is assembled in the browser: 103 MB in ~4.5 min instead of ~19, sha256 matched) lived
on the download page and went away with it on 2026-10-13 (the code is `deploy/mac/index.src.html`). If the speed gets in the way —
a mirror on a CDN host.

## First check on a Mac

Items 1–2 and the launch from item 3 have already passed on CI (section "Verified on real macOS"); what remains is what can be
seen only by eye.

First read `docs/macos.md` in full; then in order (each item is a separate question "does it work"):

1. `uv sync --group dev`, then `swift build -c release --package-path macos/helper` — does the helper build (if not, the
   compiler's error text will show what to fix in `main.swift`).
2. `uv run argus doctor` — sound, screen, window. The "Audio" and "Screen" lines must tell "no permission" from
   "does not work".
3. `uv run argus run --no-open` (**do not turn recognition on** — it is paid). Does the window open? Do the complaints on the
   page speak about permissions in words?
4. The screen-recording permission request: in the settings list, is it "Argus" or an unnamed `argus-syscap`? If the latter,
   the helper has to be launched differently (a separate helper package with its own identifier).
5. System audio: play any video and look at the "System" levels on the page; Argus's own voice-over must not get into
   this channel.
6. Hotkeys (`ctrl+alt+a` and the others): do they fire over another program's window; does another program's combination fire
   too (Carbon does not notice clashes — risk 13).
7. Panels ("All at once", answers): over an ordinary window; then over a full-screen Zoom/Keynote — first without
   `ui.mac_overlay`, then with it (risk 12); dragging by the bar, resizing, hiding/restoring by a hotkey (the call must not
   lose focus and its space meanwhile).
8. "Invisible in recordings": turn it on, share the screen in Zoom/Meet/QuickTime — is the window visible
   (risk 1).
9. Pin a window (thumbnails in the window picker), pin a window's audio; reading a window when it is covered.
10. The build: `bash packaging/build_macos.sh` → `dist-mac/Argus.app`; launch from `/Applications`; data in
    `~/Library/Application Support/Argus`, log in `~/Library/Logs/Argus/argus.log`; exiting with `⌘Q` appends a record.
11. Self-update: on a signed build, put `latest-mac.json` and the archive on the update server.

CI (`.github/workflows/macos.yml`) does item 1 and part of item 10 itself: it builds the helper, runs it with `--probe`, runs
the tests and builds the package with a smoke check "it starts, the server answers". Run it by hand (Actions → macOS → Run
workflow).

## Backlog

- Walk through the interface under WebKit (risk 6).
- The main window can go "full screen" (the green circle, Option — "zoom", ⌃⌘F, the "View" menu): the window flag
  `NSWindowCollectionBehaviorFullScreenPrimary` is set by `style_main_window`; for the duration of the mode the system buttons
  and the bar come back (`_watch_full_screen`: the menu bar slides in over our header, and our circles cannot be clicked there).
- Open the microphone after the answer to the system prompt, without holding up the server (now we wait up to 180 s — see
  "First-launch trap").
- Transparent panels ("glass") on macOS were turned on on 2026-10-13 (`mac/desktop.py: make_glass`); not checked by eye on a real
  Mac — risk 15.
- A window for permission requests at the first launch (now: complaints on the page and system prompts).
- Screenshots of the screen and windows through ScreenCaptureKit (risk 9); a rate above 5 frames/s.
- Printing to PDF without a browser (risk 10); local Whisper and screen reading on macOS (they are not in the distributed
  build).
- Landing: the "Download" button (`/download`) gives the image to a Mac visitor, the question about the first launch
  without a signature is in "Help"; `latest-mac.json` on the distribution server.
