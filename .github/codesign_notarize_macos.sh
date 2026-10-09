#!/usr/bin/env bash
# Codesign + notarize + staple dist/PLASMA.app, in place.
#
# No-op (prints a message, exits 0) when no signing certificate is configured.
# A partially configured signing setup fails with a named missing secret.
# Safe to always call from build.yml; without a certificate builds stay
# unsigned as before. See docs/reference/release-process.md
# for how to obtain each value and set it as a GitHub Actions secret.
#
# Required env:
#   MACOS_CERTIFICATE_P12_BASE64  base64 of a "Developer ID Application"
#                                 .p12 export (certificate + private key)
#   MACOS_CERTIFICATE_PASSWORD    that .p12's export password
#   MACOS_NOTARY_KEY_ID           App Store Connect API key ID
#   MACOS_NOTARY_ISSUER_ID        App Store Connect API issuer ID
#   MACOS_NOTARY_KEY_P8_BASE64    base64 of that API key's .p8 file
#
# Usage: codesign_notarize_macos.sh [all|sign|notarize|staple]
# Signs/notarizes/staples dist/PLASMA.app in place; writes nothing else.
set -euo pipefail

APP="dist/PLASMA.app"
MODE="${1:-all}"
case "$MODE" in
  all|sign|notarize|staple) ;;
  *) echo "::error::Unknown signing stage: $MODE"; exit 1 ;;
esac

if [ -z "${MACOS_CERTIFICATE_P12_BASE64:-}" ]; then
  echo "No signing certificate configured (MACOS_CERTIFICATE_P12_BASE64 unset) — skipping codesign/notarization, shipping unsigned as before."
  exit 0
fi

require_secret() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "::error::Missing required secret: $name"
    exit 1
  fi
}
if [ "$MODE" = all ] || [ "$MODE" = sign ]; then
  require_secret MACOS_CERTIFICATE_PASSWORD
fi
if [ "$MODE" = all ] || [ "$MODE" = notarize ]; then
  require_secret MACOS_NOTARY_KEY_ID
  require_secret MACOS_NOTARY_ISSUER_ID
  require_secret MACOS_NOTARY_KEY_P8_BASE64
fi

WORK="$(mktemp -d)"
KEYCHAIN="$WORK/build.keychain-db"
KEYCHAIN_CREATED=false
cleanup() {
  if [ "$KEYCHAIN_CREATED" = true ]; then
    security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

if [ "$MODE" = all ] || [ "$MODE" = sign ]; then
# --- import the signing cert into a fresh, throwaway keychain -------------
KEYCHAIN_PWD="$(openssl rand -base64 24)"
security create-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN"
KEYCHAIN_CREATED=true
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN"

echo "$MACOS_CERTIFICATE_P12_BASE64" | base64 --decode > "$WORK/cert.p12"
security import "$WORK/cert.p12" -k "$KEYCHAIN" -P "$MACOS_CERTIFICATE_PASSWORD" \
  -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PWD" "$KEYCHAIN" >/dev/null

# make it search-visible to `codesign` without permanently touching the
# runner's own login keychain search list beyond this job
EXISTING_KEYCHAINS="$(security list-keychains -d user | tr -d '"')"
security list-keychains -d user -s "$KEYCHAIN" $EXISTING_KEYCHAINS

IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" \
  | grep 'Developer ID Application' | head -1 | sed -E 's/.*"(.*)"/\1/' || true)"
[ -n "$IDENTITY" ] || { echo "::error::no 'Developer ID Application' identity found in the imported certificate"; exit 1; }
echo "Signing with: $IDENTITY"

# --- sign every nested Mach-O binary first, then the .app itself ----------
# PyInstaller's onedir folder bundles third-party dylibs (numpy, scipy, …)
# as plain files — codesign --deep on the .app alone doesn't reliably reach
# binaries nested this deep, so sign each one explicitly before the outer
# --deep pass (which then just re-affirms/covers the bundle structure).
find "$APP/Contents/Resources" -type f -perm -u+x -print0 | while IFS= read -r -d '' f; do
  if file "$f" | grep -q "Mach-O"; then
    codesign --force --timestamp --options runtime \
      --entitlements .github/macos_entitlements.plist \
      --sign "$IDENTITY" "$f"
  fi
done

codesign --force --deep --timestamp --options runtime \
  --entitlements .github/macos_entitlements.plist \
  --sign "$IDENTITY" "$APP"

codesign --verify --deep --strict --verbose=2 "$APP"
echo "Signed and verified: $APP"
fi

# --- notarize ---------------------------------------------------------
if [ "$MODE" = all ] || [ "$MODE" = notarize ]; then
echo "$MACOS_NOTARY_KEY_P8_BASE64" | base64 --decode > "$WORK/notary_key.p8"
ditto -c -k --keepParent "$APP" "$WORK/notarize.zip"

if xcrun notarytool submit "$WORK/notarize.zip" \
  --key "$WORK/notary_key.p8" \
  --key-id "$MACOS_NOTARY_KEY_ID" \
  --issuer "$MACOS_NOTARY_ISSUER_ID" \
  --wait --output-format json > "$WORK/notary-result.json" 2> "$WORK/notary-error.log"; then
  cat "$WORK/notary-result.json"
  python3 - "$WORK/notary-result.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as source:
    result = json.load(source)
if result.get("status") != "Accepted":
    print(f"::error::Notarization was not accepted: {result.get('status', 'unknown')}; "
          f"submission {result.get('id', 'unknown')}. Retrieve its notarytool log for details.")
    sys.exit(1)
PY
else
  cat "$WORK/notary-result.json" "$WORK/notary-error.log"
  if grep -Eqi 'required agreement|agreement.*expired' "$WORK/notary-result.json" "$WORK/notary-error.log"; then
    echo "::error::Apple notarization is blocked by a missing or expired agreement. The Apple Developer Account Holder must accept pending agreements and verify membership at https://developer.apple.com/account/. Retrying CI will not fix this account restriction."
  else
    echo "::error::Apple notarization submission failed; see the service response above."
  fi
  exit 1
fi
fi

if [ "$MODE" = all ] || [ "$MODE" = staple ]; then
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
echo "Stapled and validated: $APP"
fi
