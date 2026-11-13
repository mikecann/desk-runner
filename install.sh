#!/usr/bin/env bash
# Build into a private staging directory; idle_update.py applies it only after
# queued and active requests have finished, without touching a busy service.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/Applications/Desk Runner.app"
ID="com.mikecann.desk-runner"
mkdir -p "$ROOT/build"
STAGE="$(mktemp -d "$ROOT/build/install.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
STAGED_APP="$STAGE/Desk Runner.app"
if [[ -d "$APP" ]]; then
  ditto "$APP" "$STAGED_APP"
else
  mkdir -p "$STAGED_APP/Contents/MacOS"
fi
HASH_FILE="$HOME/Library/Application Support/Desk Runner/source-hash"
SOURCE_HASH="$(shasum -a 256 "$ROOT/Sources/main.swift" | cut -c1-16)"
# Reuse unchanged app builds: changing its signature can ask for TCC approval again.
if [[ "$(cat "$HASH_FILE" 2>/dev/null)" != "$SOURCE_HASH" || ! -x "$STAGED_APP/Contents/MacOS/desk-runner" ]]; then
  taskpolicy -b swiftc -O "$ROOT/Sources/main.swift" -o "$STAGED_APP/Contents/MacOS/desk-runner"
  cat > "$STAGED_APP/Contents/Info.plist" <<PLIST
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
    --requirements "=designated => identifier \"$ID\"" "$STAGED_APP"
fi
taskpolicy -b swiftc -O "$ROOT/Sources/deskctl.swift" -o "$STAGE/deskctl"
echo "$SOURCE_HASH" > "$STAGE/source-hash"
python3 "$ROOT/idle_update.py" "$STAGE" "$ROOT"
"$ROOT/desk" --permissions || true
