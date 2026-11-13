#!/usr/bin/env python3
"""Exercise update barriers on real temporary Unix sockets, with no GUI actions."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


if __name__ == "__main__":
    unittest.main()
