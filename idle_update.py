"""Keep an existing Desk Runner socket quiet while applying an idle-only update."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket


class BusyError(RuntimeError):
    pass


@contextmanager
def startup_lock(support, shared=False):
    """Serialize recovery with installs; readers may wake the same managed job."""
    import errno
    import fcntl
    import os
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(support / "startup.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSError(errno.EBUSY, "Desk Runner startup or update is in progress; retry shortly") from error
        yield
    finally:
        os.close(fd)


@contextmanager
def idle_socket(path: Path, is_idle, timeout=10):
    """Block new callers, drain the accept queue, then verify existing work is idle.

    is_idle must check the service PID, accepted sockets and child commands.
    A connected request can still be queued in the kernel and invisible to
    lsof. Sending a permissions request through the renamed socket puts a
    barrier behind those connections in the accept queue. Its reply means
    earlier connections have been accepted and can be counted by is_idle.
    """
    if not is_idle():
        raise BusyError("Desk Runner is busy; leaving it running")
    parked = path.with_name("runner.update.sock")
    if parked.exists():
        raise BusyError("A Desk Runner update socket already exists")
    path.rename(parked)
    try:
        with socket.socket(socket.AF_UNIX) as barrier:
            barrier.settimeout(timeout)
            barrier.connect(str(parked))
            barrier.sendall((json.dumps({"cmd": "__permissions", "id": "idle-update-barrier"}) + "\n").encode())
            response = b""
            while True:
                chunk = barrier.recv(1024)
                if not chunk:
                    break
                response += chunk
                if len(response) > 4096:
                    raise BusyError("Unexpected response from Desk Runner's idle barrier")
            if not response.endswith(b"\0DESK_RC=0\n"):
                raise BusyError("Desk Runner did not finish its idle barrier")
        if not is_idle():
            raise BusyError("Desk Runner has queued or active work; leaving it running")
        yield
    finally:
        if path.exists():
            # An updated server created a new socket at the original address.
            parked.unlink(missing_ok=True)
        else:
            parked.rename(path)


def service_pid(label):
    import re
    import subprocess
    result = subprocess.run(["launchctl", "print", label], capture_output=True, text=True)
    if result.returncode:
        if "could not find service" in result.stderr.lower():
            return None
        raise BusyError(f"Cannot inspect Desk Runner: {result.stderr.strip()}")
    match = re.search(r"\bpid = (\d+)", result.stdout)
    return int(match[1]) if match else None


def service_is_idle(label, pid):
    import subprocess
    if service_pid(label) != pid:
        return False
    table = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
    if any(int(line.split()[1]) == pid for line in table.splitlines()):
        return False
    result = subprocess.run(["lsof", "-n", "-a", "-p", str(pid), "-F", "ft"],
                            capture_output=True, text=True)
    # A reparented background writer is no longer a child, but still depends
    # on the service's PIPE read end. Restarting would kill it with SIGPIPE.
    types = [line[1:] for line in result.stdout.splitlines() if line.startswith("t")]
    return result.returncode == 0 and types.count("unix") == 1 and "PIPE" not in types


def socket_is_stale(path):
    import errno
    import stat
    if not stat.S_ISSOCK(path.stat().st_mode):
        raise BusyError("Desk socket path contains a different kind of file")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(2)
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno in (errno.ECONNREFUSED, errno.ENOENT):
                return True
            raise BusyError(f"Cannot determine whether Desk Runner is alive: {error}") from error
        # A reachable socket is live even if launchctl didn't expose a PID.
        # Finish a harmless request rather than abruptly dropping a legacy peer.
        probe.sendall(b'{"cmd":"__permissions"}\n')
        received = 0
        while True:
            chunk = probe.recv(1024)
            if not chunk:
                break
            received += len(chunk)
            if received > 4096:
                raise BusyError("Unexpected response while checking Desk Runner")
    return False


def install_prepared(root, stage, home=None):
    """Install only while no startup request or other installer owns the service."""
    home = home or Path.home()
    # Refuse known busy services before excluding any new callers. The
    # guarded implementation checks again after taking the lock and barrier.
    import os
    label = f"gui/{os.getuid()}/com.mikecann.desk-runner"
    pid = service_pid(label)
    if pid and not service_is_idle(label, pid):
        raise BusyError("Desk Runner is busy; leaving it running")
    sock = home / "Library/Application Support/Desk Runner/runner.sock"
    if pid is None and sock.exists() and not socket_is_stale(sock):
        raise BusyError("Desk socket is live without an inspectable service; refusing to replace it")
    with startup_lock(home / "Library/Application Support/Desk Runner"):
        _install_prepared(root, stage, home)


def _install_prepared(root, stage, home):
    """Install a signed staged app only after the live service is demonstrably idle."""
    from contextlib import nullcontext
    import hashlib
    import os
    import plistlib
    import shutil
    import subprocess
    import time
    import uuid

    home = home or Path.home()
    app = home / "Applications/Desk Runner.app"
    support = home / "Library/Application Support/Desk Runner"
    sock = support / "runner.sock"
    label = f"gui/{os.getuid()}/com.mikecann.desk-runner"
    agent = home / "Library/LaunchAgents/com.mikecann.desk-runner.plist"
    staged_app = stage / "Desk Runner.app"
    digest = hashlib.sha256((root / "Sources/main.swift").read_bytes()).hexdigest()[:16]
    if (stage / "source-hash").read_text().strip() != digest:
        raise BusyError("Prepared Desk Runner does not match its source")
    if not (stage / "deskctl").is_file():
        raise BusyError("Prepared deskctl is missing")
    subprocess.run(["codesign", "--verify", str(staged_app)], check=True)
    pid = service_pid(label)
    stale_inode = None
    if pid is None and sock.exists():
        stale_inode = sock.stat().st_ino
        if not socket_is_stale(sock):
            raise BusyError("Desk socket is live without an inspectable service; refusing to replace it")
    guard = idle_socket(sock, lambda: service_is_idle(label, pid)) if pid else nullcontext()

    # Prepare a sibling directory for atomic renames, never overwrite a live
    # executable in place. A refused update doesn't change the installed files.
    app.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    ready = app.with_name(f".DeskRunner-new-{token}.app")
    previous = app.with_name(f".DeskRunner-old-{token}.app")
    shutil.copytree(staged_app, ready)
    hash_file = support / "source-hash"
    old_hash = hash_file.read_bytes() if hash_file.exists() else None
    old_agent = agent.read_bytes() if agent.exists() else None
    old_helper = root / "build/deskctl"
    helper_backup = stage / "deskctl.previous"
    if old_helper.exists():
        shutil.copy2(old_helper, helper_backup)
    stopped = False
    replaced = False
    installed = False
    try:
        with guard:
            if pid:
                subprocess.run(["launchctl", "bootout", label], check=True)
            else:
                # Recovery from a stopped/crash-looping job is allowed only
                # while it is still stopped and its old socket is still stale.
                if service_pid(label) is not None:
                    raise BusyError("Desk Runner started during preparation; retry when idle")
                if sock.exists() and (sock.stat().st_ino != stale_inode or not socket_is_stale(sock)):
                    raise BusyError("Desk Runner's socket changed during preparation")
                subprocess.run(["launchctl", "bootout", label], capture_output=True)
            stopped = True
            if pid is None:
                sock.unlink(missing_ok=True)
            if app.exists():
                app.rename(previous)
            ready.rename(app)
            replaced = True
            support.mkdir(parents=True, exist_ok=True)
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_bytes(plistlib.dumps({
                "Label": "com.mikecann.desk-runner",
                "AssociatedBundleIdentifiers": ["com.mikecann.desk-runner"],
                "ProgramArguments": [str(app / "Contents/MacOS/desk-runner")],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Interactive",
            }))
            old_helper.parent.mkdir(parents=True, exist_ok=True)
            helper_ready = old_helper.with_name(f"deskctl-{token}")
            shutil.copy2(stage / "deskctl", helper_ready)
            os.replace(helper_ready, old_helper)
            hash_file.write_text(digest + "\n")
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)], check=True)
            # Explicit demand also works when macOS defers RunAtLoad/KeepAlive.
            subprocess.run(["launchctl", "kickstart", label], check=True, timeout=15)
            for _ in range(100):
                if sock.exists():
                    break
                time.sleep(0.1)
            else:
                raise BusyError("Updated Desk Runner did not create its socket")
            installed = True
            stopped = False
    finally:
        if stopped:
            subprocess.run(["launchctl", "bootout", label], capture_output=True)
            if replaced:
                shutil.rmtree(app)
            if previous.exists():
                previous.rename(app)
            if old_hash is not None:
                hash_file.write_bytes(old_hash)
            else:
                hash_file.unlink(missing_ok=True)
            if old_agent is not None:
                agent.write_bytes(old_agent)
            else:
                agent.unlink(missing_ok=True)
            if helper_backup.exists():
                os.replace(helper_backup, old_helper)
            else:
                old_helper.unlink(missing_ok=True)
            if pid:
                subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)], check=True)
                subprocess.run(["launchctl", "kickstart", label], check=True, timeout=15)
        if ready.exists():
            shutil.rmtree(ready)
        if installed and previous.exists():
            shutil.rmtree(previous)
    bins = home / ".local/bin"
    bins.mkdir(parents=True, exist_ok=True)
    for name, target in [("desk", root / "desk"), ("deskctl", old_helper)]:
        link = bins / name
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    print("Desk Runner installed after the idle queue barrier")


if __name__ == "__main__":
    import argparse
    import subprocess
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        install_prepared(args.root.resolve(), args.stage.resolve())
    except (BusyError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
