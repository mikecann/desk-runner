#!/usr/bin/env bash
# Builds Desk Runner, installs it to ~/Applications, starts it at login and
# links the `desk` command into ~/.local/bin.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/Applications/Desk Runner.app"
ID="com.mikecann.desk-runner"
AGENT="$HOME/Library/LaunchAgents/$ID.plist"

mkdir -p "$ROOT/build" "$APP/Contents/MacOS"

# Only rebuild the app when its source changed. Builds aren't byte-for-byte
# repeatable, and macOS asks to approve screen recording again for each new one.
HASH_FILE="$HOME/Library/Application Support/Desk Runner/source-hash"
SOURCE_HASH="$(shasum -a 256 "$ROOT/Sources/main.swift" | cut -c1-16)"
if [[ "$(cat "$HASH_FILE" 2>/dev/null)" != "$SOURCE_HASH" ]]; then
  swiftc -O "$ROOT/Sources/main.swift" -o "$ROOT/build/desk-runner"
  cp "$ROOT/build/desk-runner" "$APP/Contents/MacOS/desk-runner"
  mkdir -p "$(dirname "$HASH_FILE")"
  echo "$SOURCE_HASH" > "$HASH_FILE"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$ID</string>
  <key>CFBundleName</key><string>Desk Runner</string>
  <key>CFBundleExecutable</key><string>desk-runner</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
  codesign --force --sign - --identifier "$ID" \
    --requirements "=designated => identifier \"$ID\"" "$APP"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/bin"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$ID</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/desk-runner</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$ID" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"
ln -sfn "$ROOT/desk" "$HOME/.local/bin/desk"
swiftc -O "$ROOT/Sources/deskctl.swift" -o "$ROOT/build/deskctl"
ln -sfn "$ROOT/build/deskctl" "$HOME/.local/bin/deskctl"
chmod +x "$ROOT/desk"
sleep 1
"$ROOT/desk" --permissions || true
