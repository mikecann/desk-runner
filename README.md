# Desk Runner

A tiny background app for macOS that lets scripts (and agents over SSH) record
the screen and drive the mouse and keyboard.

macOS only gives screen recording and accessibility to apps you've approved,
and commands run over SSH aren't one. Desk Runner is: you approve it once in
System Settings, and it runs the commands it's sent as its own children, so
they inherit that permission. It only listens on a Unix socket in
`~/Library/Application Support/Desk Runner` that just your user can open, so
it gives nothing beyond what a shell login already has.

## Set up

```bash
bash install.sh
```

That builds it into `~/Applications/Desk Runner.app`, starts it at login, and
links `desk` and `deskctl` into `~/.local/bin`. Then turn Desk Runner on in
System Settings > Privacy & Security, under both Screen Recording and
Accessibility. `desk --permissions` tells you whether it worked. It only
rebuilds the app when `Sources/main.swift` changes, because macOS asks you to
approve every new build.

## Use

```bash
desk 'screencapture -x shot.png'          # any shell command, with the permissions
desk 'deskctl click 400 300'              # glide the pointer there and click
desk 'deskctl type "hello there"'         # type with real key presses
desk 'deskctl key cmd+w'                  # press a combo
desk 'deskctl drag 100 100 600 400 1.2'   # drag over 1.2 seconds
desk 'deskctl button 3'                   # mouse back button
desk 'deskctl windows'                    # on-screen windows with their bounds
```

From another machine: `ssh host '~/.local/bin/desk "deskctl windows"'`.

Key presses clear the event's process ID so apps treat them like real typing.
Some apps (pynput hotkeys, for one) still ignore them, because macOS fills the
ID back in.
