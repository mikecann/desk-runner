#!/usr/bin/env python3
"""Cold-start recovery of an isolated launchd job; no screen/input or real app.

Set DESK_RUNNER_TEST_BINARY to a -D DESK_RUNNER_TESTING build.
"""
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
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
    def test_cold_start_uses_managed_job_and_never_replays_commands(self):
        with tempfile.TemporaryDirectory(prefix="desk-wake-", dir="/tmp") as folder:
            base = Path(folder)
            home = base / "home"
            support = home / "Library/Application Support/Desk Runner"
            support.mkdir(parents=True)
            app = home / "Applications/Desk Runner.app"
            executable = app / "Contents/MacOS/desk-runner"
            executable.parent.mkdir(parents=True)
            shutil.copy2(Path(SERVER).resolve(), executable)
            label = "com.mikecann.desk-runner.test." + uuid.uuid4().hex
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": label, "CFBundleName": "Desk Wake Test",
                "CFBundleExecutable": "desk-runner", "CFBundlePackageType": "APPL", "LSUIElement": True,
            }))
            target = f"gui/{os.getuid()}/{label}"
            plist = base / "agent.plist"
            plist.write_bytes(plistlib.dumps({
                "Label": label, "ProgramArguments": [str(executable)],
                "EnvironmentVariables": {"DESK_RUNNER_TEST_DIR": str(support)},
                "RunAtLoad": False, "KeepAlive": False, "ProcessType": "Interactive",
                "StandardInPath": "/dev/null", "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            }))
            receipt = base / "receipt"
            runner = base / "client.py"
            runner.write_text('''import runpy, sys, uuid
script, label, command = sys.argv[1:]
main = runpy.run_path(script)["main"]
main.__globals__["SERVICE_LABEL"] = label
sys.argv = ["desk", command]
sys.exit(main(uuid.uuid4().hex))
''')
            env = dict(os.environ, HOME=str(home))
            def job_pid():
                info = subprocess.check_output(["launchctl", "print", target], text=True)
                match = re.search(r"\bpid = (\d+)", info)
                return int(match[1]) if match else None
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
            try:
                self.assertIsNone(job_pid())
                previous = None
                for count in [1, 2]:
                    command = f"printf 'ran\\n' >> {shlex.quote(str(receipt))}; printf '%s' \"$PPID\""
                    result = subprocess.run([sys.executable, str(runner), str(ROOT / "desk"), target, command],
                                            env=env, capture_output=True, timeout=25)
                    if result.returncode:
                        info = subprocess.check_output(["launchctl", "print", target], text=True)
                        safe = [line.strip() for line in info.splitlines()
                                if any(key in line for key in ["state =", "pid =", "runs =", "pended", "last exit", "properties ="]) ]
                        self.fail(f"cold request {count}: {result.stderr!r}; service: {safe}")
                    parent = int(result.stdout)
                    self.assertEqual(parent, job_pid(), "server isn't owned by the registered job")
                    self.assertNotEqual(parent, previous)
                    previous = parent
                    # A second cold caller can demand an already-starting job.
                    # Without -k, this must neither restart nor duplicate it.
                    demanded = subprocess.run(["launchctl", "kickstart", target], capture_output=True)
                    self.assertEqual(demanded.returncode, 0, demanded.stderr)
                    self.assertEqual(job_pid(), parent)
                    self.assertEqual(receipt.read_text().splitlines(), ["ran"] * count)
                    subprocess.run(["launchctl", "kill", "SIGTERM", target], check=True)
                    deadline = time.monotonic() + 5
                    while job_pid() is not None:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.05)
            finally:
                subprocess.run(["launchctl", "bootout", target], check=True)


if __name__ == "__main__":
    unittest.main()
