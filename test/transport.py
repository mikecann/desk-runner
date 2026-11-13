#!/usr/bin/env python3
"""No GUI actions. Optionally pass a -D DESK_RUNNER_TESTING server binary."""
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVER = os.environ.get("DESK_RUNNER_TEST_BINARY")
MARKER = b"\0DESK_RC="


class ClientHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desk-", dir="/tmp")
        self.home = Path(self.temp.name)
        self.support = self.home / "Library/Application Support/Desk Runner"
        self.support.mkdir(parents=True)
        self.path = str(self.support / "runner.sock")
        self.env = dict(os.environ, HOME=str(self.home))
        self.errors = []

    def tearDown(self):
        self.temp.cleanup()

    def client(self, command="ignored"):
        return subprocess.run([sys.executable, str(ROOT / "desk"), command],
                              env=self.env, capture_output=True, timeout=15)

    def response(self, chunks, read_request=True):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(self.path)
        listener.listen()
        listener.settimeout(15)

        def serve():
            try:
                with listener, listener.accept()[0] as conn:
                    if not read_request:
                        # Closing with the client's request unread resets the socket.
                        time.sleep(0.03)
                        return
                    request = b""
                    while not request.endswith(b"\n"):
                        request += conn.recv(65536)
                    self.request = json.loads(request)
                    for chunk in chunks:
                        conn.sendall(chunk)
                        time.sleep(0.002)
            except Exception as error:
                self.errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result = self.client()
        thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])
        return result


class TransportTests(ClientHarness):
    def test_split_marker_and_binary_output(self):
        payload = b"long output\n" * 10000 + b"\0binary\xfftail"
        result = self.response([payload] + [bytes([b]) for b in MARKER + b"23\n"])
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"")

    def test_missing_marker_preserves_tail_and_reports_unknown(self):
        result = self.response([b"compiling\nlast bytes"])
        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stdout, b"compiling\nlast bytes")
        self.assertIn(b"result is unknown", result.stderr)
        self.assertIn(self.request["id"].encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_empty_disconnect(self):
        result = self.response([])
        self.assertEqual(result.returncode, 125)
        self.assertIn(b"connection closed", result.stderr)

    def test_invalid_status(self):
        for status in [b"oops", b"", b"256", b"-1", b"0trailing"]:
            with self.subTest(status=status):
                result = self.response([b"output", MARKER + status + b"\n"])
                self.assertEqual(result.returncode, 125)
                self.assertEqual(result.stdout, b"output" + MARKER + status + b"\n")
                self.assertIn(b"invalid or incomplete", result.stderr)
                os.unlink(self.path)

    def test_close_before_reading_request_is_explained(self):
        result = self.response([], read_request=False)
        self.assertEqual(result.returncode, 125)
        self.assertIn(b"connection", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_zero_exit(self):
        result = self.response([MARKER + b"0\n"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")

    def test_unavailable_service(self):
        result = self.client()
        self.assertEqual(result.returncode, 127)
        self.assertIn(b"isn't running", result.stderr)


@unittest.skipUnless(SERVER, "set DESK_RUNNER_TEST_BINARY for isolated Swift tests")
class ServerTests(ClientHarness):
    def setUp(self):
        super().setUp()
        self.logs = tempfile.TemporaryFile()
        self.server = subprocess.Popen([SERVER], env=dict(self.env, DESK_RUNNER_TEST_DIR=str(self.support)),
                                       stdout=self.logs, stderr=self.logs)
        deadline = time.monotonic() + 5
        while not Path(self.path).exists():
            if self.server.poll() is not None or time.monotonic() > deadline:
                self.fail("isolated server did not start")
            time.sleep(0.02)

    def tearDown(self):
        if self.server.poll() is None:
            self.server.terminate()
        self.server.wait(timeout=5)
        self.logs.close()
        super().tearDown()

    def connect(self, command, request_id):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(5)
        conn.connect(self.path)
        conn.sendall((json.dumps({"cmd": command, "id": request_id}) + "\n").encode())
        return conn

    def test_disconnect_does_not_kill_other_request_or_server(self):
        with self.connect("sleep 0.3; printf survivor", "survivor") as survivor:
            with self.connect("printf ready; sleep 0.1; printf after-disconnect", "disconnect") as abandoned:
                self.assertIn(b"ready", abandoned.recv(1024))
            received = b""
            while True:
                chunk = survivor.recv(1024)
                if not chunk:
                    break
                received += chunk
        self.assertIsNone(self.server.poll(), "a disconnected client killed the server")
        self.assertEqual(received, b"survivor" + MARKER + b"0\n")
        self.assertEqual(self.client("exit 7").returncode, 7)
        self.logs.seek(0)
        logs = self.logs.read()
        self.assertIn(b"request=disconnect client disconnected", logs)
        self.assertIn(b"request=disconnect child=", logs)
        self.assertRegex(logs, rb"request=disconnect child=\d+ finished status=0")

    def test_signalled_command_has_shell_status_and_explanation(self):
        result = self.client("kill -TERM $$")
        self.assertEqual(result.returncode, 143)
        self.assertIn(b"terminated by signal 15", result.stdout)
        self.assertEqual(self.client("exit 0").returncode, 0)

    def test_service_loss_is_explained(self):
        started = self.home / "started"
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"),
                                   f"touch {started}; sleep 0.3"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        self.addCleanup(lambda: client.poll() is None and client.kill())
        while not started.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        # Only this test's isolated service is killed, never the desktop agent.
        self.server.terminate()
        self.server.wait(timeout=5)
        _, error = client.communicate(timeout=5)
        self.assertEqual(client.returncode, 125)
        self.assertIn(b"result is unknown", error)

    def test_slow_stdout_preserves_all_output(self):
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"), "head -c 2000000 /dev/zero"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.8)
        output, error = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 0, error)
        self.assertEqual(output, b"\0" * 2000000)

    def test_closed_stdout_is_explained(self):
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"), "head -c 200000 /dev/zero"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client.stdout.close()
        client.stdout = None
        _, error = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 141)
        self.assertIn(b"output consumer closed", error)
        self.assertNotIn(b"Traceback", error)

    def test_closed_combined_output_has_no_traceback(self):
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"), "head -c 200000 /dev/zero"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        client.stdout.close()
        client.stdout = None
        client.wait(timeout=15)
        self.assertEqual(client.returncode, 141)

    def test_background_writer_keeps_running_after_parent_exits(self):
        finished = self.home / "background-finished"
        result = self.client(f"(sleep 0.8; printf tick; touch {finished}) &")
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 3
        while not finished.exists():
            self.assertLess(time.monotonic(), deadline, "background writer was killed")
            time.sleep(0.02)

    def test_resumed_reader_gets_explicit_output_loss_and_real_status(self):
        with self.connect("head -c 1000000 /dev/zero; exit 7", "resumed") as conn:
            time.sleep(4)
            received = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                received += chunk
        self.assertIn(b"bytes of output were not delivered", received)
        self.assertIn(b"Command exit status: 7", received)
        self.assertTrue(received.endswith(MARKER + b"125\n"))

    def test_process_substitution_tail_is_forwarded(self):
        result = self.client("exec > >(sleep 0.3; cat) 2>&1; echo last-line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"last-line\n")

    def test_redirected_output_waits_for_real_exit_status(self):
        result = self.client("exec >/dev/null 2>&1; sleep 0.1; exit 7")
        self.assertEqual(result.returncode, 7)

    def test_interrupt_explains_that_child_continues(self):
        started = self.home / "interrupt-started"
        finished = self.home / "interrupt-finished"
        with subprocess.Popen([sys.executable, str(ROOT / "desk"),
                               f"touch {started}; sleep 0.3; touch {finished}"],
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as client:
            deadline = time.monotonic() + 3
            while not started.exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            client.send_signal(signal.SIGINT)
            _, error = client.communicate(timeout=5)
        self.assertEqual(client.returncode, 130)
        self.assertIn(b"may still be running", error)
        self.assertNotIn(b"Traceback", error)
        while not finished.exists():
            self.assertLess(time.monotonic(), deadline, "interrupt cancelled the child")
            time.sleep(0.01)

    def test_stalled_reader_does_not_stall_command_forever(self):
        finished = self.home / "finished"
        with self.connect(f"head -c 1000000 /dev/zero; touch {finished}", "stalled"):
            deadline = time.monotonic() + 5
            while not finished.exists():
                self.assertLess(time.monotonic(), deadline, "stalled reader left command blocked")
                time.sleep(0.05)
        self.assertIsNone(self.server.poll())

    def test_output_drains_before_status(self):
        result = self.client("head -c 200000 /dev/zero | tr '\\000' x")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"x" * 200000)


if __name__ == "__main__":
    unittest.main()
