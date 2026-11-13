#!/usr/bin/env python3
"""Exercise update barriers on real temporary Unix sockets, with no GUI actions."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import idle_update
from idle_update import BusyError, idle_socket


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desk-update-", dir="/tmp")
        self.path = Path(self.temp.name) / "runner.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.path))
        self.listener.listen(5)
        self.listener.settimeout(3)
        self.threads = []

    def tearDown(self):
        self.listener.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.temp.cleanup()

    def start(self, work):
        thread = threading.Thread(target=work, daemon=True)
        self.threads.append(thread)
        thread.start()

    def respond_to_barrier(self):
        with self.listener.accept()[0] as conn:
            request = b""
            while not request.endswith(b"\n"):
                request += conn.recv(1024)
            self.assertEqual(json.loads(request)["cmd"], "__permissions")
            conn.sendall(b"screen-recording: yes\n\0DESK_RC=0\n")

    def test_idle_service_is_restored_if_update_does_not_replace_socket(self):
        self.start(self.respond_to_barrier)
        with idle_socket(self.path, lambda: True):
            self.assertFalse(self.path.exists())
            self.assertTrue(self.path.with_name("runner.update.sock").exists())
        self.assertTrue(self.path.exists())

    def test_busy_service_never_loses_its_socket(self):
        with self.assertRaises(BusyError):
            with idle_socket(self.path, lambda: False):
                self.fail("busy service entered update")
        self.assertTrue(self.path.exists())

    def test_queued_connection_is_accepted_before_idle_check(self):
        # It is connected but not yet accepted, so an FD/child scan looks idle.
        queued = socket.socket(socket.AF_UNIX)
        queued.connect(str(self.path))
        self.addCleanup(queued.close)
        accepted = threading.Event()
        finished = threading.Event()
        release = threading.Event()
        outcome = []

        def update():
            try:
                with idle_socket(self.path, lambda: not accepted.is_set()):
                    outcome.append("unsafe update")
            except BusyError:
                outcome.append("refused")
            finally:
                finished.set()

        self.start(update)
        deadline = time.monotonic() + 2
        while self.path.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

        def serve():
            with self.listener.accept()[0] as active:
                accepted.set()
                self.respond_to_barrier()
                release.wait(2)
                active.sendall(b"original request survived")

        self.start(serve)
        self.assertTrue(finished.wait(2))
        self.assertEqual(outcome, ["refused"])
        self.assertTrue(self.path.exists())
        release.set()
        queued.settimeout(2)
        self.assertEqual(queued.recv(1024), b"original request survived")

    def test_barrier_timeout_restores_socket_and_never_enters_update(self):
        with self.assertRaises(TimeoutError):
            with idle_socket(self.path, lambda: True, timeout=0.05):
                self.fail("unanswered barrier entered update")
        self.assertTrue(self.path.exists())

    def test_new_service_socket_is_kept(self):
        self.start(self.respond_to_barrier)
        replacement = socket.socket(socket.AF_UNIX)
        self.addCleanup(replacement.close)
        with idle_socket(self.path, lambda: True):
            replacement.bind(str(self.path))
            replacement.listen(1)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_name("runner.update.sock").exists())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desk-install-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.home, self.root, self.stage = base / "home", base / "repo", base / "stage"
        self.app = self.home / "Applications/Desk Runner.app"
        self.app_binary = self.app / "Contents/MacOS/desk-runner"
        self.app_binary.parent.mkdir(parents=True)
        self.app_binary.write_bytes(b"old app")
        new_binary = self.stage / "Desk Runner.app/Contents/MacOS/desk-runner"
        new_binary.parent.mkdir(parents=True)
        new_binary.write_bytes(b"new app")
        (self.stage / "deskctl").write_bytes(b"new helper")
        (self.root / "Sources").mkdir(parents=True)
        (self.root / "Sources/main.swift").write_bytes(b"source")
        (self.root / "desk").write_bytes(b"client")
        digest = hashlib.sha256(b"source").hexdigest()[:16]
        (self.stage / "source-hash").write_text(digest)
        self.helper = self.root / "build/deskctl"
        self.helper.parent.mkdir()
        self.helper.write_bytes(b"old helper")
        self.support = self.home / "Library/Application Support/Desk Runner"
        self.support.mkdir(parents=True)
        self.hash_file = self.support / "source-hash"
        self.hash_file.write_bytes(b"old hash")

    def test_busy_install_never_stops_service_or_replaces_files(self):
        with mock.patch.object(idle_update, "service_pid", return_value=123), \
             mock.patch.object(idle_update, "service_is_idle", return_value=False), \
             mock.patch("subprocess.run") as run:
            with self.assertRaises(BusyError):
                idle_update.install_prepared(self.root, self.stage, self.home)
        self.assertEqual(self.app_binary.read_bytes(), b"old app")
        self.assertEqual(self.helper.read_bytes(), b"old helper")
        self.assertEqual(self.hash_file.read_bytes(), b"old hash")
        self.assertFalse(any("bootout" in call.args[0] for call in run.call_args_list))

    def test_failed_start_restores_installed_app_helper_and_hash(self):
        def run(command, **kwargs):
            if command[:2] == ["launchctl", "bootstrap"]:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.object(idle_update, "service_pid", return_value=None), \
             mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                idle_update.install_prepared(self.root, self.stage, self.home)
        self.assertEqual(self.app_binary.read_bytes(), b"old app")
        self.assertEqual(self.helper.read_bytes(), b"old helper")
        self.assertEqual(self.hash_file.read_bytes(), b"old hash")

    def test_first_install_applies_prepared_files_and_links(self):
        def run(command, **kwargs):
            if command[:2] == ["launchctl", "bootstrap"]:
                (self.support / "runner.sock").touch()
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.object(idle_update, "service_pid", return_value=None), \
             mock.patch("subprocess.run", side_effect=run), \
             mock.patch("builtins.print"):
            idle_update.install_prepared(self.root, self.stage, self.home)
        self.assertEqual(self.app_binary.read_bytes(), b"new app")
        self.assertEqual(self.helper.read_bytes(), b"new helper")
        self.assertEqual(self.hash_file.read_text().strip(), (self.stage / "source-hash").read_text())
        self.assertEqual((self.home / ".local/bin/desk").resolve(), (self.root / "desk").resolve())

    def test_idle_scan_accepts_any_listener_fd_and_refuses_connections(self):
        with mock.patch.object(idle_update, "service_pid", return_value=123), \
             mock.patch("subprocess.check_output", return_value="123 1\n"), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "p123\nf7\n")):
            self.assertTrue(idle_update.service_is_idle("label", 123))
        with mock.patch.object(idle_update, "service_pid", return_value=123), \
             mock.patch("subprocess.check_output", return_value="123 1\n"), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "p123\nf7\nf9\n")):
            self.assertFalse(idle_update.service_is_idle("label", 123))

    def test_idle_scan_refuses_child_commands_and_changed_pid(self):
        with mock.patch.object(idle_update, "service_pid", return_value=123), \
             mock.patch("subprocess.check_output", return_value="124 123\n"):
            self.assertFalse(idle_update.service_is_idle("label", 123))
        with mock.patch.object(idle_update, "service_pid", return_value=124):
            self.assertFalse(idle_update.service_is_idle("label", 123))


if __name__ == "__main__":
    unittest.main()
