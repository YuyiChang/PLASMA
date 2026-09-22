#!/usr/bin/env bash
# Wrap dist/PLASMA.app (built by build_macos_app.sh, then optionally
# codesigned/notarized/stapled by codesign_notarize_macos.sh) into a
# drag-to-install DMG, for anyone who'd rather not use the Homebrew cask.
#
# Signing/notarization happens earlier, on the .app itself — this script
# just packages whatever .app it's given (signed or not) into a DMG; the
# DMG container itself is never separately signed (Gatekeeper checks the
# .app's own signature/staple when it's launched, not the DMG's).
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

rm -f dist/PLASMA_MacOS_arm64.dmg
hdiutil create -volname "PLASMA" -srcfolder "$STAGE" -ov -format UDZO \
  "dist/PLASMA_MacOS_arm64.dmg"
