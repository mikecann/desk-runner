#!/usr/bin/env python3
"""Cold-start recovery through real Launch Services, using a hidden private app.

Pass DESK_RUNNER_TEST_BINARY built with -D DESK_RUNNER_TESTING. No input or
screen capture is performed, and the real Desk Runner app is never touched.
"""
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SERVER = os.environ.get("DESK_RUNNER_TEST_BINARY")


@unittest.skipUnless(SERVER, "set DESK_RUNNER_TEST_BINARY for cold-start integration")
class StartupIntegration(unittest.TestCase):
    def test_cold_start_and_recovery_do_not_replay_commands(self):
        with tempfile.TemporaryDirectory(prefix="desk-wake-", dir="/tmp") as folder:
            base = Path(folder)
            home = base / "home"
            support = home / "Library/Application Support/Desk Runner"
            app = home / "Applications/Desk Runner.app"
            executable = app / "Contents/MacOS/desk-runner"
            executable.parent.mkdir(parents=True)
            pid_file = base / "pids"
            receipt = base / "receipt"
            server = base / "server"
            shutil.copy2(Path(SERVER).resolve(), server)
            source = base / "main.swift"
            source.write_text(f'''import AppKit
import Foundation
let child = Process()
child.executableURL = URL(fileURLWithPath: {json.dumps(str(server))})
var env = ProcessInfo.processInfo.environment
env["DESK_RUNNER_TEST_DIR"] = {json.dumps(str(support))}
child.environment = env
child.standardInput = FileHandle.nullDevice
child.standardOutput = FileHandle.nullDevice
child.standardError = FileHandle.nullDevice
child.terminationHandler = {{ _ in exit(0) }}
try child.run()
try "\\(getpid()) \\(child.processIdentifier)".write(toFile: {json.dumps(str(pid_file))}, atomically: true, encoding: .utf8)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
''')
            subprocess.run(["taskpolicy", "-b", "swiftc", str(source), "-o", str(executable)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.mikecann.desk-runner.test." + uuid.uuid4().hex,
                "CFBundleName": "Desk Wake Test", "CFBundleExecutable": "desk-runner",
                "CFBundlePackageType": "APPL", "LSUIElement": True,
            }))
            env = dict(os.environ, HOME=str(home))
            def stop_owned(pid):
                identity = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                          capture_output=True, text=True).stdout.strip()
                if identity.startswith(str(server)) or identity.startswith(str(executable)):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            try:
                for count in [1, 2]:
                    result = subprocess.run([sys.executable, str(ROOT / "desk"),
                                             f"printf 'ran\\n' >> {shlex.quote(str(receipt))}; printf '%s' \"$PPID\""],
                                            env=env, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    parent = int(result.stdout)
                    self.assertEqual(receipt.read_text().splitlines(), ["ran"] * count)
                    self.assertEqual(parent, int(pid_file.read_text().split()[1]))
                    stop_owned(parent)
                    # Allow Launch Services to notice both the helper and its
                    # app wrapper have ended before the next cold request.
                    time.sleep(1)
            finally:
                if pid_file.exists():
                    for pid in reversed([int(value) for value in pid_file.read_text().split()]):
                        stop_owned(pid)


if __name__ == "__main__":
    unittest.main()
