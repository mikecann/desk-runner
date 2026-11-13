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
Accessibility. `desk --permissions` tells you whether it worked. It refuses to restart a busy service, including queued requests that have not
yet been accepted. It only
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

`desk` returns the command's exit status, or `128 + signal` if it was killed
by a signal. If the service cannot be reached or the connection ends before
an exit status arrives, it prints a diagnostic with a request ID and returns 255. That makes `git bisect run`
abort on a transport failure. A command's own status, including 125 (skip), is
preserved. Commands can also return 255 themselves; the diagnostic identifies
transport failures.

A command never outlives whoever is watching it, so a script can't go on
clicking and typing after nobody can see its result. If `desk` goes away
(Ctrl-C, a dropped SSH session, a closed output pipe) or Desk Runner itself
stops, crashes or restarts mid-command, the command and everything it started
are sent SIGTERM, then SIGKILL two seconds later. A small guard process per
command does this, so it works even when Desk Runner is gone, and Desk Runner
stops the command itself if the guard is killed. Anything the
command already did (a click, a file) stays done, so check before retrying.
`deskctl` lets go of held keys and a dragged mouse button when it's stopped.

Background jobs that are still running when the command exits normally are
left alone, and keep running even if Desk Runner restarts later; their output
is discarded. If the command is killed by a signal, they're stopped too. A
child that moves itself into a new process group (`set -m` job control, or a
program that calls `setsid`) has detached itself on purpose and is left alone.

If a reader that's still connected stops accepting output for 30 seconds, the
service keeps the command running and discards further output. If the reader
resumes, it reports the lost output and the command's actual status, and
returns 255.

The service logs request IDs, child process IDs and completion statuses without
logging command text. To inspect them on the machine running Desk Runner:

```bash
log show --last 10m --style compact --predicate 'subsystem == "com.mikecann.desk-runner"'
```

Key presses clear the event's process ID so apps treat them like real typing.
Some apps (pynput hotkeys, for one) still ignore them, because macOS fills the
ID back in.

Combos press each modifier down, then the key, then let the modifiers go, the
way a person would, so apps that read the text of a key event (event taps,
hotkey recorders) see "S" rather than "s", and no Shift is left held for the
next click or drag. `deskctl hold shift 2` holds just a modifier.

Small targets need exact coordinates. QuickTime's and AVKit's trim handles are
about 10 points wide, and `drag` moves them fine when it starts in the middle of
one. A downscaled screenshot is easy to misread by a few points, so crop the
spot at full size first: `desk 'screencapture -x -R 80,910,120,80 crop.png'`.

## Test

`python3 test/transport.py` checks the client against temporary Unix sockets.
To also test the server's disconnect handling without touching the desktop or
its running service:

```bash
mkdir -p build
taskpolicy -b swiftc -D DESK_RUNNER_TESTING Sources/main.swift -o build/test-server
DESK_RUNNER_TEST_BINARY="$PWD/build/test-server" taskpolicy -b python3 test/transport.py
```

`taskpolicy -b python3 test/update.py` checks the idle-update barrier against
real temporary Unix sockets, including a connected request still waiting in
the accept queue. Deployments use `idle_update.py` to block new connections and
wait for that queue to drain before checking for active requests.

`test/keys.sh` types into a scratch TextEdit document and checks what an event
tap sees. It takes over the keyboard and mouse for a few seconds, so run it on
the desk machine: `ssh tinker-desk 'bash ~/desk-runner/test/keys.sh'`.
