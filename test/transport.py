#!/usr/bin/env python3
"""No GUI actions. Optionally pass a -D DESK_RUNNER_TESTING server binary."""
import errno
import io
import json
import os
import re
import runpy
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from idle_update import startup_lock

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
                        # Closing before reading the request may yield EOF on a Unix socket.
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
        self.assertEqual(result.returncode, 255)
        self.assertEqual(result.stdout, b"compiling\nlast bytes")
        self.assertIn(b"result is unknown", result.stderr)
        self.assertIn(self.request["id"].encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_empty_disconnect(self):
        result = self.response([])
        self.assertEqual(result.returncode, 255)
        self.assertIn(b"connection closed", result.stderr)

    def test_invalid_status(self):
        for status in [b"oops", b"", b"256", b"-1", b"0trailing"]:
            with self.subTest(status=status):
                result = self.response([b"output", MARKER + status + b"\n"])
                self.assertEqual(result.returncode, 255)
                self.assertEqual(result.stdout, b"output" + MARKER + status + b"\n")
                self.assertIn(b"invalid or incomplete", result.stderr)
                os.unlink(self.path)

    def test_close_before_reading_request_is_explained(self):
        result = self.response([], read_request=False)
        self.assertEqual(result.returncode, 255)
        self.assertIn(b"connection closed before an exit status arrived", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def run_socket_error(self, operation):
        entry = runpy.run_path(str(ROOT / "desk"))["main"]
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        if operation == "send":
            connection.sendall.side_effect = BrokenPipeError("injected send failure")
        else:
            connection.recv.side_effect = [b"partial output\n", ConnectionResetError("injected receive failure")]
        output = io.BytesIO()
        error = io.StringIO()
        poller = mock.MagicMock()
        poller.poll.return_value = [(42, 1)]
        with mock.patch.object(entry.__globals__["socket"], "socket", return_value=connection), \
             mock.patch.object(entry.__globals__["select"], "poll", return_value=poller), \
             mock.patch.object(sys, "argv", ["desk", "ignored"]), \
             mock.patch.object(sys, "stdout", SimpleNamespace(buffer=output)), \
             mock.patch.object(sys, "stderr", error), \
             mock.patch.object(entry.__globals__["subprocess"], "run") as launch:
            status = entry("socket-error-request")
        launch.assert_not_called()
        self.assertEqual(status, 255)
        self.assertIn("connection failed", error.getvalue())
        self.assertIn("socket-error-request", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())
        return connection, output.getvalue(), error.getvalue()

    def test_send_oserror_is_explained(self):
        connection, output, error = self.run_socket_error("send")
        connection.recv.assert_not_called()
        self.assertEqual(output, b"")
        self.assertIn("injected send failure", error)

    def test_receive_oserror_preserves_output_and_is_explained(self):
        _, output, error = self.run_socket_error("recv")
        self.assertEqual(output, b"partial output\n")
        self.assertIn("injected receive failure", error)

    def test_command_skip_status_is_preserved(self):
        result = self.response([MARKER + b"125\n"])
        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stderr, b"")

    def bisect_repo(self):
        repo = self.home / "bisect-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, env=self.env, capture_output=True, check=True)
        git("init", "-q")
        for n in range(3):
            git("-c", "user.name=Transport Test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", str(n))
        git("bisect", "start", "HEAD", "HEAD~2")
        return repo

    def bisect_run(self, repo):
        return subprocess.run(["git", "bisect", "run", sys.executable, str(ROOT / "desk"), "ignored"],
                              cwd=repo, env=self.env, capture_output=True, timeout=15)

    def assert_bisect_aborted(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"255", result.stderr)
        self.assertNotIn(b"skip", result.stderr)

    def test_bisect_aborts_on_lost_transport(self):
        repo = self.bisect_repo()
        with mock.patch.object(self, "client", side_effect=lambda: self.bisect_run(repo)):
            result = self.response([])
        self.assert_bisect_aborted(result)

    def test_bisect_aborts_when_service_is_unavailable(self):
        self.assert_bisect_aborted(self.bisect_run(self.bisect_repo()))

    def test_command_not_found_status_is_preserved(self):
        result = self.response([MARKER + b"127\n"])
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stderr, b"")

    def test_incomplete_client_installation_is_a_transport_failure(self):
        standalone = self.home / "standalone"
        standalone.mkdir()
        script = standalone / "desk"
        script.write_bytes((ROOT / "desk").read_bytes())
        for old_helper in [False, True]:
            with self.subTest(old_helper=old_helper):
                if old_helper:
                    (standalone / "idle_update.py").write_text("# predates startup_lock\n")
                result = subprocess.run([sys.executable, str(script), "exit 0"],
                                        env=dict(self.env, PYTHONPATH=""), capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 255)
                self.assertIn(b"client installation is incomplete", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_zero_exit(self):
        result = self.response([MARKER + b"0\n"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")

    def test_unavailable_service(self):
        result = self.client()
        self.assertEqual(result.returncode, 255)
        self.assertIn(b"Cannot connect", result.stderr)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(ROOT / "desk"))
        self.connect = self.module["connect_client"]
        self.globals = self.connect.__globals__
        self.temp = tempfile.TemporaryDirectory(prefix="desk-startup-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.globals["SOCKET"] = str(Path(self.temp.name) / "runner.sock")

    def test_running_service_needs_no_launch_request(self):
        client = mock.MagicMock()
        with mock.patch.object(self.globals["socket"], "socket", return_value=client), \
             mock.patch.object(self.globals["subprocess"], "run") as launch:
            self.assertIs(self.connect(), client)
        launch.assert_not_called()

    def test_refused_or_missing_socket_demands_managed_job_and_waits(self):
        for code in [errno.ECONNREFUSED, errno.ENOENT]:
            with self.subTest(code=code):
                first, waiting, ready = [mock.MagicMock() for _ in range(3)]
                first.connect.side_effect = OSError(code, "not ready")
                waiting.connect.side_effect = OSError(errno.ENOENT, "starting")
                with mock.patch.object(self.globals["socket"], "socket", side_effect=[first, waiting, ready]), \
                     mock.patch.object(self.globals["os"].path, "isfile", return_value=True), \
                     mock.patch.object(self.globals["subprocess"], "run") as launch, \
                     mock.patch.object(self.globals["time"], "sleep"):
                    self.assertIs(self.connect(), ready)
                self.assertEqual(launch.call_args.args[0], ["/bin/launchctl", "kickstart", self.globals["SERVICE_LABEL"]])
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(ready.settimeout.call_args_list[-1], mock.call(None))
                first.close.assert_called_once()
                waiting.close.assert_called_once()
                for client in [first, waiting, ready]:
                    client.sendall.assert_not_called()

    def test_permission_failure_never_launches_app(self):
        client = mock.MagicMock()
        client.connect.side_effect = PermissionError(errno.EACCES, "denied")
        with mock.patch.object(self.globals["socket"], "socket", return_value=client), \
             mock.patch.object(self.globals["subprocess"], "run") as launch:
            with self.assertRaises(PermissionError):
                self.connect()
        launch.assert_not_called()
        client.close.assert_called_once()

    def test_missing_app_never_launches_something_else(self):
        client = mock.MagicMock()
        client.connect.side_effect = FileNotFoundError(errno.ENOENT, "missing socket")
        with mock.patch.object(self.globals["socket"], "socket", return_value=client), \
             mock.patch.object(self.globals["os"].path, "isfile", return_value=False), \
             mock.patch.object(self.globals["subprocess"], "run") as launch:
            with self.assertRaises(FileNotFoundError):
                self.connect()
        launch.assert_not_called()

    def test_update_lock_prevents_any_connect_or_recovery(self):
        client = mock.MagicMock()
        with startup_lock(Path(self.globals["SOCKET"]).parent), \
             mock.patch.object(self.globals["socket"], "socket", return_value=client) as factory, \
             mock.patch.object(self.globals["os"].path, "isfile", return_value=True), \
             mock.patch.object(self.globals["subprocess"], "run") as launch:
            with self.assertRaises(OSError) as error:
                self.connect()
        self.assertEqual(error.exception.errno, errno.EBUSY)
        factory.assert_not_called()
        launch.assert_not_called()
        client.sendall.assert_not_called()

    def test_older_updater_marker_prevents_recovery(self):
        Path(self.globals["SOCKET"]).with_name("runner.update.sock").touch()
        client = mock.MagicMock()
        with mock.patch.object(self.globals["socket"], "socket", return_value=client) as factory, \
             mock.patch.object(self.globals["os"].path, "isfile", return_value=True), \
             mock.patch.object(self.globals["subprocess"], "run") as launch:
            with self.assertRaises(OSError) as error:
                self.connect()
        self.assertEqual(error.exception.errno, errno.EBUSY)
        factory.assert_not_called()
        launch.assert_not_called()

    def test_start_timeout_is_bounded_and_never_submits_a_command(self):
        client = mock.MagicMock()
        client.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "not ready")
        with mock.patch.object(self.globals["socket"], "socket", return_value=client), \
             mock.patch.object(self.globals["os"].path, "isfile", return_value=True), \
             mock.patch.object(self.globals["subprocess"], "run") as launch, \
             mock.patch.object(self.globals["time"], "monotonic", side_effect=[0, 16]):
            with self.assertRaises(TimeoutError):
                self.connect()
        self.assertEqual(launch.call_count, 1)
        client.sendall.assert_not_called()

    def test_launch_error_is_reported_without_submitting_a_command(self):
        client = mock.MagicMock()
        client.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "not ready")
        with mock.patch.object(self.globals["socket"], "socket", return_value=client), \
             mock.patch.object(self.globals["os"].path, "isfile", return_value=True), \
             mock.patch.object(self.globals["subprocess"], "run", side_effect=subprocess.TimeoutExpired("launchctl", 15)):
            with self.assertRaisesRegex(OSError, "could not start"):
                self.connect()
        client.sendall.assert_not_called()


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

    def connect(self, command, request_id, **options):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(5)
        conn.connect(self.path)
        conn.sendall((json.dumps({"cmd": command, "id": request_id, **options}) + "\n").encode())
        return conn

    def wait_for(self, path, seconds=3):
        deadline = time.monotonic() + seconds
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, f"{path.name} never appeared")
            time.sleep(0.01)

    def assert_never(self, path, seconds):
        time.sleep(seconds)
        self.assertFalse(path.exists(), f"{path.name} appeared: the command outlived its supervisor")

    def test_disconnect_cancels_only_that_request(self):
        abandoned_done = self.home / "abandoned-done"
        with self.connect("sleep 0.6; printf survivor", "survivor") as survivor:
            with self.connect(f"printf ready; sleep 0.4; touch {abandoned_done}", "disconnect") as abandoned:
                self.assertIn(b"ready", abandoned.recv(1024))
            received = b""
            while True:
                chunk = survivor.recv(1024)
                if not chunk:
                    break
                received += chunk
        self.assertIsNone(self.server.poll(), "a disconnected client killed the server")
        self.assertEqual(received, b"survivor" + MARKER + b"0\n")
        self.assert_never(abandoned_done, 0.3)
        self.assertEqual(self.client("exit 7").returncode, 7)
        self.logs.seek(0)
        logs = self.logs.read()
        self.assertIn(b"request=disconnect client hung up", logs)
        self.assertRegex(logs, rb"request=disconnect child=\d+ cancelled")
        self.assertNotRegex(logs, rb"request=survivor child=\d+ cancelled")

    def test_disconnect_stops_the_commands_children(self):
        child_done = self.home / "child-done"
        with self.connect(f"(sleep 0.5; touch {child_done}) & printf ready; wait", "children") as conn:
            self.assertIn(b"ready", conn.recv(1024))
        self.assert_never(child_done, 0.8)

    def test_disconnect_with_closed_output_still_cancels(self):
        started = self.home / "quiet-started"
        done = self.home / "quiet-done"
        with self.connect(f"exec >/dev/null 2>&1; touch {started}; sleep 0.5; touch {done}", "quiet"):
            self.wait_for(started)
        self.assert_never(done, 0.8)

    def test_command_ignoring_sigterm_is_killed(self):
        started = self.home / "stubborn-started"
        done = self.home / "stubborn-done"
        with self.connect(f"trap '' TERM; touch {started}; sleep 4.5; touch {done}", "stubborn"):
            self.wait_for(started)
        self.assert_never(done, 5)

    def test_server_crash_stops_running_command(self):
        started = self.home / "crash-started"
        done = self.home / "crash-done"
        child_done = self.home / "crash-child-done"
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"),
                                   f"(sleep 0.6; touch {child_done}) & touch {started}; sleep 0.6; touch {done}"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_for(started)
        # Only this test's isolated service is killed, never the desktop agent.
        self.server.kill()
        self.server.wait(timeout=5)
        _, error = client.communicate(timeout=5)
        self.assertEqual(client.returncode, 255)
        self.assertIn(b"result is unknown", error)
        self.assert_never(done, 0.9)
        self.assertFalse(child_done.exists())

    def test_children_of_a_signalled_command_are_stopped(self):
        child_done = self.home / "orphan-done"
        result = self.client(f"(sleep 0.5; touch {child_done}) & kill -TERM $$")
        self.assertEqual(result.returncode, 143)
        self.assert_never(child_done, 0.9)

    def test_half_closed_client_still_gets_its_result(self):
        done = self.home / "half-closed-done"
        with self.connect(f"sleep 0.4; touch {done}; exit 3", "half") as conn:
            conn.shutdown(socket.SHUT_WR)
            received = b""
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                received += chunk
        self.assertTrue(done.exists())
        self.assertTrue(received.endswith(MARKER + b"3\n"), received)

    def test_killed_guard_does_not_leave_command_unprotected(self):
        started = self.home / "unguarded-started"
        done = self.home / "unguarded-done"
        with self.connect(f"touch {started}; sleep 1; touch {done}", "guard-killed"):
            self.wait_for(started)
            self.logs.seek(0)
            guard = re.search(rb"request=guard-killed guard=(\d+) started", self.logs.read())
            self.assertIsNotNone(guard, "no guard logged")
            os.kill(int(guard.group(1)), signal.SIGKILL)
            self.assert_never(done, 1.5)

    def test_guard_killed_during_its_grace_period_still_kills(self):
        started = self.home / "grace-started"
        done = self.home / "grace-done"
        with self.connect(f"trap '' TERM; touch {started}; sleep 4; touch {done}", "grace") as conn:
            self.wait_for(started)
            self.logs.seek(0)
            guard = re.search(rb"request=grace guard=(\d+) started", self.logs.read())
            self.assertIsNotNone(guard, "no guard logged")
        # The client has gone, so the guard is in its two-second grace period.
        time.sleep(0.5)
        os.kill(int(guard.group(1)), signal.SIGKILL)
        self.assert_never(done, 4.5)

    def test_server_crash_right_after_start_stops_command(self):
        # The command starts inside the guard's group, so there is no window
        # between it starting and the guard knowing it. A race this short can't
        # be forced from a test, so this only checks the earliest crash point.
        done = self.home / "early-crash-done"
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"),
                                   f"kill -KILL $PPID; sleep 0.6; touch {done}"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: client.poll() is None and client.kill())
        client.communicate(timeout=5)
        self.server.wait(timeout=5)
        self.assert_never(done, 1)

    def test_background_job_survives_server_exit_after_parent_finished(self):
        finished = self.home / "detached-finished"
        result = self.client(f"(sleep 0.6; touch {finished}) &")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.server.kill()
        self.server.wait(timeout=5)
        self.wait_for(finished)

    def test_signalled_command_has_shell_status_and_explanation(self):
        result = self.client("kill -TERM $$")
        self.assertEqual(result.returncode, 143)
        self.assertIn(b"terminated by signal 15", result.stdout)
        self.assertEqual(self.client("exit 0").returncode, 0)

    def test_slow_stdout_preserves_all_output(self):
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"), "head -c 2000000 /dev/zero"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.8)
        output, error = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 0, error)
        self.assertEqual(output, b"\0" * 2000000)

    def test_continuously_slow_reader_keeps_all_output(self):
        payload_size = 131072
        with self.connect(f"dd if=/dev/zero bs=65536 count=2 2>/dev/null", "steady-reader") as conn:
            received = b""
            started = time.monotonic()
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                received += chunk
                time.sleep(0.07)
        self.assertGreater(time.monotonic() - started, 3)
        self.assertTrue(received == b"\0" * payload_size + MARKER + b"0\n", f"received {len(received)} bytes; tail={received[-250:]!r}")

    def test_closed_stdout_is_explained(self):
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"), "head -c 200000 /dev/zero"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client.stdout.close()
        client.stdout = None
        _, error = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 141)
        self.assertIn(b"output consumer closed", error)
        self.assertNotIn(b"Traceback", error)

    def test_quiet_command_stops_when_output_reader_goes_away(self):
        # What a dropped SSH session looks like to desk: its stdout's reader is
        # gone, but the command prints nothing that would hit the broken pipe.
        started = self.home / "quiet-reader-started"
        done = self.home / "quiet-reader-done"
        client = subprocess.Popen([sys.executable, str(ROOT / "desk"),
                                   f"touch {started}; sleep 1.5; touch {done}"],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_for(started)
        client.stdout.close()
        client.stdout = None
        _, error = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 141)
        self.assertIn(b"output consumer closed", error)
        self.assert_never(done, 1.5)

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
        self.assertTrue(b"bytes of output were not delivered" in received, f"missing output-loss notice; tail={received[-250:]!r}")
        self.assertTrue(b"Command exit status: 7" in received, f"missing actual status; tail={received[-250:]!r}")
        self.assertTrue(received.endswith(MARKER + b"255\n"))

    def test_process_substitution_tail_is_forwarded(self):
        result = self.client("exec > >(sleep 0.3; cat) 2>&1; echo last-line")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"last-line\n")

    def test_redirected_output_waits_for_real_exit_status(self):
        result = self.client("exec >/dev/null 2>&1; sleep 0.1; exit 7")
        self.assertEqual(result.returncode, 7)

    def test_interrupt_stops_the_command(self):
        started = self.home / "interrupt-started"
        finished = self.home / "interrupt-finished"
        with subprocess.Popen([sys.executable, str(ROOT / "desk"),
                               f"touch {started}; sleep 0.4; touch {finished}"],
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as client:
            self.wait_for(started)
            client.send_signal(signal.SIGINT)
            _, error = client.communicate(timeout=5)
        self.assertEqual(client.returncode, 130)
        self.assertIn(b"stops the command", error)
        self.assertNotIn(b"Traceback", error)
        self.assert_never(finished, 0.7)

    def test_stalled_reader_does_not_stall_command_forever(self):
        finished = self.home / "finished"
        with self.connect(f"head -c 1000000 /dev/zero; touch {finished}", "stalled"):
            deadline = time.monotonic() + 5
            while not finished.exists():
                self.assertLess(time.monotonic(), deadline, "stalled reader left command blocked")
                time.sleep(0.05)
        self.assertIsNone(self.server.poll())

    def test_protocol_error_is_not_a_command_exit_status(self):
        with self.connect("ignored", "bad-request", cmd=12) as conn:
            received = b""
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                received += chunk
        self.assertIn(b"bad request", received)
        self.assertTrue(received.endswith(MARKER + b"255\n"))
        self.assertEqual(self.client("exit 2").returncode, 2)

    def test_service_start_failure_is_not_a_command_exit_status(self):
        cwd_file = self.home / "not-a-directory"
        cwd_file.write_text("a file")
        with self.connect("exit 0", "bad-cwd", cwd=str(cwd_file)) as conn:
            received = b""
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                received += chunk
        self.assertIn(b"could not start", received)
        self.assertTrue(received.endswith(MARKER + b"255\n"))
        self.assertEqual(self.client("exit 127").returncode, 127)

    def test_output_drains_before_status(self):
        result = self.client("head -c 200000 /dev/zero | tr '\\000' x")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"x" * 200000)


if __name__ == "__main__":
    unittest.main()
