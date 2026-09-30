"""Bounded cleanup of the subprocess and descendants started by this app."""
import os
import signal
import subprocess


def stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        if result.returncode and process.poll() is None:
            raise RuntimeError("Could not stop the processing subprocess tree")
        process.wait(timeout=5)
        return
    # All callers launch children in their own session on POSIX.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
