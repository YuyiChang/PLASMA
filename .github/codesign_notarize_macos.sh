#!/usr/bin/env bash
# Codesign + notarize + staple dist/PLASMA.app, in place.
#
# No-op (prints a message, exits 0) unless every required secret is present
# — safe to always call from build.yml; builds stay unsigned exactly as
# before until these are configured. See docs/reference/release-process.md
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
# Usage: codesign_notarize_macos.sh
# Signs/notarizes/staples dist/PLASMA.app in place; writes nothing else.
set -euo pipefail

APP="dist/PLASMA.app"

if [ -z "${MACOS_CERTIFICATE_P12_BASE64:-}" ]; then
  echo "No signing certificate configured (MACOS_CERTIFICATE_P12_BASE64 unset) — skipping codesign/notarization, shipping unsigned as before."
  exit 0
fi

WORK="$(mktemp -d)"
KEYCHAIN="$WORK/build.keychain-db"
trap 'security delete-keychain "$KEYCHAIN" 2>/dev/null || true; rm -rf "$WORK"' EXIT

# --- import the signing cert into a fresh, throwaway keychain -------------
KEYCHAIN_PWD="$(openssl rand -base64 24)"
security create-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN"
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
  | grep 'Developer ID Application' | head -1 | sed -E 's/.*"(.*)"/\1/')"
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

# --- notarize ---------------------------------------------------------
echo "$MACOS_NOTARY_KEY_P8_BASE64" | base64 --decode > "$WORK/notary_key.p8"
ditto -c -k --keepParent "$APP" "$WORK/notarize.zip"

xcrun notarytool submit "$WORK/notarize.zip" \
  --key "$WORK/notary_key.p8" \
  --key-id "$MACOS_NOTARY_KEY_ID" \
  --issuer "$MACOS_NOTARY_ISSUER_ID" \
  --wait

xcrun stapler staple "$APP"
echo "Signed + notarized + stapled: $APP"
