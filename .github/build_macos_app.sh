#!/usr/bin/env bash
# Wrap the frozen console binary (dist/PLASMA_MacOS_arm64) in a minimal .app
# bundle, for the Homebrew cask / a double-clickable Applications-folder icon.
#
# PyInstaller's own BUNDLE() can't be used for this: EXE(console=True) makes
# BUNDLE() set LSBackgroundOnly=True (PyInstaller/building/osx.py), which
# hides the app entirely — no Dock icon, no Terminal, no console output — so
# any startup failure (liblsl missing, port in use, ...) becomes silent.
#
# Instead this builds an app whose CFBundleExecutable is a launcher script
# that opens the real console binary inside a visible Terminal window — the
# same technique plasma.desktop_shortcut (via pyshortcuts) already uses for
# the pip-install Desktop icon.
#
# Usage: build_macos_app.sh <path-to-onedir-folder> <version>
# Writes dist/PLASMA_MacOS_arm64.app.zip.
set -euo pipefail

BIN="${1:?usage: build_macos_app.sh <path-to-onedir-folder> <version>}"
VERSION="${2:?usage: build_macos_app.sh <path-to-onedir-folder> <version>}"
OUT="dist/PLASMA.app"

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"

# $BIN is PyInstaller onedir output (a folder: the exe + its _internal/
# support files) — copy the whole folder in, keeping its own name, rather
# than flattening to a single file the way the old onefile binary was.
cp -R "$BIN" "$OUT/Contents/Resources/"
chmod +x "$OUT/Contents/Resources/$(basename "$BIN")/$(basename "$BIN")"
cp plasma/resources/icons/plasma.icns "$OUT/Contents/Resources/PLASMA.icns"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>PLASMA</string>
    <key>CFBundleDisplayName</key><string>PLASMA</string>
    <key>CFBundleIdentifier</key><string>io.github.yuyichang.plasma</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>PLASMA</string>
    <key>CFBundleIconFile</key><string>PLASMA.icns</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Single-quoted heredoc: written byte-for-byte, no interpolation here — $DIR
# / $BIN / $ESCAPED are resolved when a user later launches this script, not
# now while we're generating it.
cat > "$OUT/Contents/MacOS/PLASMA" <<'LAUNCHER'
#!/bin/bash
# This app has no window of its own — it opens the real console binary in a
# visible Terminal window (see build_macos_app.sh for why).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"
BIN="$DIR/PLASMA_MacOS_arm64/PLASMA_MacOS_arm64"
ESCAPED=${BIN//\"/\\\"}
osascript -e "tell application \"Terminal\" to activate" \
          -e "tell application \"Terminal\" to do script \"'$ESCAPED'\""
LAUNCHER
chmod +x "$OUT/Contents/MacOS/PLASMA"

# ditto (not zip) preserves the bundle's permissions/resource forks/xattrs
# correctly — the standard way to archive a .app for distribution.
ditto -c -k --sequesterRsrc --keepParent "$OUT" "dist/PLASMA_MacOS_arm64.app.zip"
