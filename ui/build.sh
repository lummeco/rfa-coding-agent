#!/bin/zsh
# Builds build/RFA.app -- the menu bar app. Run from anywhere: ./ui/build.sh
#
# The checkout it drives is baked in as RFAHome, so the app works wherever macOS launches it from.
# Rebuild after moving the checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/RFA.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc -O -o "$APP/Contents/MacOS/RFA" "$ROOT/ui/MenuBar.swift"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>RFA</string>
  <key>CFBundleIdentifier</key><string>fi.lummeco.rfa.menubar</string>
  <key>CFBundleExecutable</key><string>RFA</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>RFAHome</key><string>$ROOT</string>
</dict>
</plist>
EOF

codesign --force --sign - "$APP" >/dev/null
echo "Built $APP — open it, and it sits in the menu bar."
