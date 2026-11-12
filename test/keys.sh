#!/usr/bin/env bash
# Checks that deskctl's key presses carry the right text and modifiers, as a
# listen-only event tap sees them, and that no modifier is left held for the
# next mouse event. Needs Desk Runner set up (it runs through `desk`) and takes
# over the keyboard and mouse for a few seconds, so run it on the desk machine:
#   ssh tinker-desk 'bash ~/desk-runner/test/keys.sh'
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

swiftc -O "$ROOT/Sources/deskctl.swift" -o "$WORK/deskctl"
swiftc -O "$ROOT/test/probe.swift" -o "$WORK/probe"

# Typing has to land somewhere harmless, so give it a scratch document. TextEdit
# autosaves, so cmd+w closes it at the end without asking.
: > "$WORK/scratch.txt"
open -a TextEdit "$WORK/scratch.txt"
sleep 2

# The drag starts and ends inside the scratch window, so it only moves the caret.
read -r X Y W H < <("$ROOT/desk" "'$WORK/deskctl' windows TextEdit" | awk -F'\t' '$3 == "scratch.txt" && !done {print $4; done = 1}')
CX=$((X + W / 2)); CY=$((Y + H / 2))
"$ROOT/desk" "'$WORK/probe' 30 > '$WORK/out.txt' & PROBE=\$!
  until grep -q ready '$WORK/out.txt' 2>/dev/null; do sleep 0.1; done
  '$WORK/deskctl' type 'Snap It!'
  '$WORK/deskctl' key cmd+shift+left
  '$WORK/deskctl' drag $CX $CY $((CX + 40)) $((CY + 20)) 0.2
  sleep 0.5; kill \$PROBE
  '$WORK/deskctl' key cmd+w"

# Only the key presses and mouse buttons matter; flagsChanged lines vary.
got="$(grep -v -e '^flags' -e '^ready' "$WORK/out.txt")"
want="key S [shift]
key n []
key a []
key p []
key   []
key I [shift]
key t []
key ! [shift]
key U+001C [cmd+shift]
mousedown []
mouseup []"
if [[ "$got" == "$want" ]]; then
  echo "ok: keys carry the right text and modifiers"
else
  echo "FAIL: event tap saw:"; echo "$got"
  diff <(echo "$want") <(echo "$got") || true
  exit 1
fi
