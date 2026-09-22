#!/usr/bin/env bash
# Wrap dist/PLASMA.app (built by build_macos_app.sh) into a drag-to-install
# DMG, for anyone who'd rather not use the Homebrew cask.
#
# Unsigned/unnotarized, same as the raw binary and the .app.zip — first
# launch is still blocked by Gatekeeper until the user right-clicks →
# Open once. The commented-out steps below are exactly where codesign /
# notarytool would go if signing gets set up later; nothing else in this
# script would need to change.
#
# Usage: build_macos_dmg.sh <path-to-PLASMA.app>
# Writes dist/PLASMA_MacOS_arm64.dmg.
set -euo pipefail

APP="${1:?usage: build_macos_dmg.sh <path-to-PLASMA.app>}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$APP" "$STAGE/PLASMA.app"
# drag-to-install: a Finder window showing PLASMA.app next to a shortcut to
# /Applications, the standard macOS DMG affordance
ln -s /Applications "$STAGE/Applications"

# --- future, once there's a signing identity/notarization profile: ---------
# codesign --force --deep --options runtime \
#   --sign "$SIGNING_IDENTITY" "$STAGE/PLASMA.app"
# -----------------------------------------------------------------------

rm -f dist/PLASMA_MacOS_arm64.dmg
hdiutil create -volname "PLASMA" -srcfolder "$STAGE" -ov -format UDZO \
  "dist/PLASMA_MacOS_arm64.dmg"

# --- future: ------------------------------------------------------------
# xcrun notarytool submit dist/PLASMA_MacOS_arm64.dmg \
#   --keychain-profile "plasma-notary" --wait
# xcrun stapler staple dist/PLASMA_MacOS_arm64.dmg
# -----------------------------------------------------------------------
