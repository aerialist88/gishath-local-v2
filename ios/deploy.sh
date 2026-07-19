#!/bin/bash
# deploy.sh — build 3vor Fetch and push it to the iPhone over USB/Wi-Fi.
#
# This is the free-Apple-ID workflow: the app's 7-day certificate expiry is a
# non-event because re-running this script re-signs and reinstalls in one shot
# (phone must be paired once via Xcode and on the same network / plugged in).
#
# Usage:
#   ./ios/deploy.sh                # build + install + launch
#   TEAM_ID=ABCDE12345 ./ios/deploy.sh   # override the signing team explicitly
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT=ios/ThreevorFetch.xcodeproj
SCHEME=ThreevorFetch
BUNDLE_ID=com.trevorjow.threevorfetch

if ! xcodebuild -version >/dev/null 2>&1; then
    echo "✗ Xcode is not installed (or xcode-select still points at CommandLineTools)." >&2
    echo "  Install Xcode from the App Store, then run:" >&2
    echo "  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer" >&2
    exit 1
fi

TEAM_FLAG=()
if [[ -n "${TEAM_ID:-}" ]]; then
    TEAM_FLAG=(DEVELOPMENT_TEAM="$TEAM_ID")
fi

# Bundle the freshest CK reference prices (ck_price.py publishes this copy
# outside the TCC-protected project folder on every refresh, so the nightly
# launchd build ships day-old prices instead of whatever was last rsynced).
CK_SIDE="$HOME/Library/Application Support/ThreevorFetch/ck_prices.json"
if [[ -f "$CK_SIDE" && "$CK_SIDE" -nt ios/ThreevorFetch/CKPrices.json ]]; then
    cp "$CK_SIDE" ios/ThreevorFetch/CKPrices.json
    echo "▸ Bundled fresh CK prices (published $(date -r "$CK_SIDE" '+%Y-%m-%d %H:%M'))"
fi

echo "▸ Building ${SCHEME}…"
BUILD_LOG=$(mktemp)
if ! xcodebuild -project "$PROJECT" -scheme "$SCHEME" \
    -destination 'generic/platform=iOS' \
    -allowProvisioningUpdates \
    ${TEAM_FLAG[@]+"${TEAM_FLAG[@]}"} \
    build > "$BUILD_LOG" 2>&1; then
    echo "✗ Build failed — last 30 lines:" >&2
    tail -30 "$BUILD_LOG" >&2
    echo "  (If it mentions provisioning/session: open Xcode once and re-sign" >&2
    echo "  into the Apple ID under Settings → Accounts.)" >&2
    rm -f "$BUILD_LOG"
    exit 1
fi
grep -E "Signing Identity|BUILD" "$BUILD_LOG" | tail -3 || true
rm -f "$BUILD_LOG"

APP_PATH=$(xcodebuild -project "$PROJECT" -scheme "$SCHEME" \
    -destination 'generic/platform=iOS' -showBuildSettings 2>/dev/null \
    | awk -F' = ' '/ CODESIGNING_FOLDER_PATH/{print $2; exit}')

if [[ -z "$APP_PATH" || ! -d "$APP_PATH" ]]; then
    echo "✗ Build product not found — check the xcodebuild output above." >&2
    echo "  (First run? Open ios/ThreevorFetch.xcodeproj in Xcode once, pick your" >&2
    echo "  Personal Team under Signing & Capabilities, and build to your phone.)" >&2
    exit 1
fi
echo "▸ Built: $APP_PATH"

# Find the first connected/paired iPhone via devicectl (iOS 17+).
DEVICE_JSON=$(mktemp)
trap 'rm -f "$DEVICE_JSON"' EXIT
xcrun devicectl list devices --json-output "$DEVICE_JSON" >/dev/null

DEVICE_ID=$(python3 - "$DEVICE_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
devices = data.get("result", {}).get("devices", [])
for d in devices:
    props = d.get("deviceProperties", {})
    hw = d.get("hardwareProperties", {})
    conn = d.get("connectionProperties", {})
    if hw.get("deviceType") == "iPhone" and conn.get("tunnelState") != "unavailable":
        print(d.get("identifier", ""))
        break
PY
)

if [[ -z "$DEVICE_ID" ]]; then
    echo "✗ No reachable iPhone found." >&2
    echo "  Plug the phone in (or ensure it's on the same Wi-Fi, unlocked, and" >&2
    echo "  paired with this Mac via Xcode → Window → Devices and Simulators)." >&2
    exit 1
fi
echo "▸ Installing to device $DEVICE_ID…"
xcrun devicectl device install app --device "$DEVICE_ID" "$APP_PATH"

echo "▸ Launching…"
xcrun devicectl device process launch --device "$DEVICE_ID" "$BUNDLE_ID" || {
    echo "  (Launch failed — if this is the first install, trust the developer" >&2
    echo "  cert on the phone: Settings → General → VPN & Device Management.)" >&2
}
echo "✓ Done. Cert is fresh for another 7 days."
