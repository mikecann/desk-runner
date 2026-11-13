"""Keep an existing Desk Runner socket quiet while applying an idle-only update."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket


class BusyError(RuntimeError):
    pass


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
