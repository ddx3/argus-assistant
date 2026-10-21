#!/bin/bash
# Argus.app, Argus-mac.zip (for self-update) and Argus-mac.dmg (for people).
# Run on macOS, from any folder:   bash packaging/build_macos.sh
#
# Environment variables, all optional:
#   ARGUS_CODESIGN_IDENTITY   "Developer ID Application: Name (TEAMID)": a real signature.
#                             Without it the signature is a trial one: it starts on this same
#                             computer, on other people's the system will not let it through
#                             without a manual permission.
#   ARGUS_NOTARY_PROFILE      a profile from `xcrun notarytool store-credentials`: notarization.
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD
#                             the same without a profile (for CI): the password is an "app
#                             password".
#   ARGUS_BUNDLE_ID           com.argus.assistant by default
#   ARGUS_BUILD_FULL=1        build with everything (the subscription path to the model, local
#                             engines); by default it is the distribution build without them.
#   ARGUS_SKIP_HELPER=1       do not build the audio helper, take a ready one from ARGUS_SYSCAP.
#
# The result is in dist-mac/. What is verified and what is not is in docs/macos.md.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "$(uname)" != "Darwin" ]; then
    echo "The .app can only be built on macOS (here: $(uname))." >&2
    exit 1
fi
for tool in uv codesign ditto hdiutil xcrun; do
    command -v "$tool" >/dev/null || { echo "Not found: $tool" >&2; exit 1; }
done

OUT="$ROOT/dist-mac"
WORK="$ROOT/build-mac"
IDENTITY="${ARGUS_CODESIGN_IDENTITY:-}"
ENTITLEMENTS="$ROOT/packaging/macos/entitlements.plist"
SEED="$WORK/seed"

# Apple accepts the entitlements list only in ASCII: Cyrillic in a comment passes signing,
# and then notarization answers "Embedded entitlements are invalid", after half an hour of
# waiting.
plutil -lint "$ENTITLEMENTS" >/dev/null
if LC_ALL=C grep -q '[^[:print:][:space:]]' "$ENTITLEMENTS"; then
    echo "$ENTITLEMENTS contains non-ASCII characters: Apple does not accept that" >&2
    exit 1
fi

rm -rf "$OUT" "$WORK/pyinstaller"
mkdir -p "$OUT" "$WORK"

step() { printf '\n==> %s\n' "$1"; }

# Apple's time server and hdiutil on shared machines occasionally fail once, so we retry.
retry() {
    local times=$1 attempt
    shift
    for ((attempt = 1; attempt <= times; attempt++)); do
        "$@" && return 0
        echo "attempt $attempt of $times failed: $1" >&2
        sleep $((attempt * 5))
    done
    return 1
}

# Send a file for notarization and wait for Apple's answer. Returns 1 if it did not pass.
notarize() {
    local file=$1 result
    if [ -n "${ARGUS_NOTARY_PROFILE:-}" ]; then
        result="$(xcrun notarytool submit "$file" --keychain-profile "$ARGUS_NOTARY_PROFILE" --wait 2>&1)" || true
    else
        result="$(xcrun notarytool submit "$file" --apple-id "$APPLE_ID" \
                  --team-id "${APPLE_TEAM_ID:?APPLE_TEAM_ID is required}" \
                  --password "${APPLE_APP_PASSWORD:?APPLE_APP_PASSWORD is required}" --wait 2>&1)" || true
    fi
    echo "$result"
    if ! echo "$result" | grep -q "status: Accepted"; then
        echo "Notarization failed: the reason is in the log above (xcrun notarytool log <id>)" >&2
        return 1
    fi
}

NOTARIZE=
if [ -n "$IDENTITY" ] && { [ -n "${ARGUS_NOTARY_PROFILE:-}" ] || [ -n "${APPLE_ID:-}" ]; }; then
    NOTARIZE=1
fi

# --- version ------------------------------------------------------------------------
# VERSION inside the package is "1.0+hash" (self-update compares it); for the system it is
# whole numbers.
# Computed once and passed on (ARGUS_FULL_VERSION): when the working folder has edits, a
# timestamp with minute precision is added to the version, and a second computation (the
# package seeds, after the Swift helper is built) gave a different string: the release
# description and the package diverged, and the app "updated" on every launch.
unset ARGUS_FULL_VERSION
FULL_VERSION="$(uv run python -c "import sys, pathlib; sys.path.insert(0, 'packaging'); import stage; print(stage.build_version(pathlib.Path('.')))")"
export ARGUS_FULL_VERSION="$FULL_VERSION"
export ARGUS_VERSION="${FULL_VERSION%%+*}"
ARGUS_BUILD="$(git rev-list --count HEAD 2>/dev/null || echo 1)"
export ARGUS_BUILD
echo "version: $FULL_VERSION (package $ARGUS_VERSION, build $ARGUS_BUILD)"

# --- audio helper ------------------------------------------------------------------
step "Audio helper (Swift)"
if [ -n "${ARGUS_SKIP_HELPER:-}" ]; then
    HELPER="${ARGUS_SYSCAP:?the ARGUS_SYSCAP path is required when ARGUS_SKIP_HELPER is set}"
else
    command -v swift >/dev/null || { echo "swift not found: install Xcode or the Command Line Tools" >&2; exit 1; }
    (cd macos/helper && swift build -c release)
    HELPER="$(cd macos/helper && swift build -c release --show-bin-path)/argus-syscap"
fi
[ -x "$HELPER" ] || { echo "The helper failed to build: $HELPER" >&2; exit 1; }
echo "helper: $HELPER"

# --- seeds -------------------------------------------------------------------------
step "Seeds"
uv run python packaging/build_mac_seed.py "$SEED"
export ARGUS_SEED_DIR="$SEED"

# --- build -------------------------------------------------------------------------
step "PyInstaller"
if [ -z "${ARGUS_BUILD_FULL:-}" ]; then
    export ARGUS_BUILD_API_ONLY=1
    export ARGUS_BUILD_NO_SUBSCRIPTION=1
fi
[ -f packaging/argus.icns ] || uv run python packaging/make_icns.py
uv run pyinstaller --noconfirm --distpath "$OUT" --workpath "$WORK/pyinstaller" packaging/argus_mac.spec

APP="$OUT/Argus.app"
[ -d "$APP" ] || { echo "The package failed to build: $APP" >&2; exit 1; }

# --- helper into the package and signing -------------------------------------------
step "Signing"
cp "$HELPER" "$APP/Contents/MacOS/argus-syscap"
chmod +x "$APP/Contents/MacOS/argus-syscap"

if [ -n "$IDENTITY" ]; then
    sign() { retry 3 codesign --force --timestamp --options runtime --sign "$IDENTITY" "$@"; }
    # From the inside out: first everything nested, then the package itself. `--deep` is no
    # good for signing: it signs nested items without our entitlements and in the wrong order.
    # Libraries are signed without entitlements, executables with them (so that, for example,
    # a program carried by the full build can create code in memory under the hardened
    # runtime).
    while IFS= read -r -d '' file; do
        if file -b "$file" | grep -q "Mach-O"; then
            case "$file" in
                *.so|*.dylib) sign "$file" ;;
                *)            sign --entitlements "$ENTITLEMENTS" "$file" ;;
            esac
        fi
    done < <(find "$APP" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -111 \) \
                ! -path "$APP/Contents/MacOS/Argus" ! -path "$APP/Contents/MacOS/argus-syscap" -print0)
    # The helper needs no entitlements: system audio goes through the person's permission, not
    # through a signing entitlement.
    sign "$APP/Contents/MacOS/argus-syscap"
    sign --entitlements "$ENTITLEMENTS" "$APP"
    echo "signed: $IDENTITY"
else
    codesign --force --deep --sign - --entitlements "$ENTITLEMENTS" "$APP"
    echo "WARNING: the signature is a trial one: on other people's computers the system will not let the program through without a manual permission"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

# --- notarization ------------------------------------------------------------------
if [ -n "$NOTARIZE" ]; then
    step "Notarizing the package (Apple checks it, usually a few minutes)"
    NOTARY_ZIP="$WORK/notarize.zip"
    ditto -c -k --keepParent "$APP" "$NOTARY_ZIP"
    notarize "$NOTARY_ZIP" || exit 1
    xcrun stapler staple "$APP"
    spctl --assess --type execute --verbose=2 "$APP"
else
    echo
    echo "Not notarized: ARGUS_CODESIGN_IDENTITY and ARGUS_NOTARY_PROFILE are needed (or APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD)."
fi

# --- archives ----------------------------------------------------------------------
step "Archives"
ditto -c -k --keepParent "$APP" "$OUT/Argus-mac.zip"
printf '{"version": "%s", "zip": "Argus-mac.zip"}\n' "$FULL_VERSION" > "$OUT/latest-mac.json"

DMG_DIR="$WORK/dmg"
rm -rf "$DMG_DIR"; mkdir -p "$DMG_DIR"
cp -R "$APP" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"
# ULMO (LZMA compression, macOS 10.15+) gives a noticeably smaller image than the usual
# UDZO, and it is downloaded over slow connections.
retry 3 hdiutil create -volname "Argus" -srcfolder "$DMG_DIR" -ov -format ULMO "$OUT/Argus-mac.dmg" >/dev/null
if [ -n "$IDENTITY" ]; then
    retry 3 codesign --force --timestamp --sign "$IDENTITY" "$OUT/Argus-mac.dmg"
fi
if [ -n "$NOTARIZE" ]; then
    step "Notarizing the image"
    notarize "$OUT/Argus-mac.dmg" || exit 1
    xcrun stapler staple "$OUT/Argus-mac.dmg"
fi

step "Done"
ls -lh "$OUT"
echo
echo "There are no keys in the build: the seeds are the distribution kind, without .env (packaging/build_mac_seed.py), and the app gets answers, voice and"
echo "recognition from the accounts server. Before distributing, still run: python deploy/mac/publish_mac.py."
