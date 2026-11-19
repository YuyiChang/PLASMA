"""Bounded startup check of a frozen app from an isolated, clean directory."""
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def stop_tree(process, grace=5):
    """Never let a frozen app's shutdown hooks block the CI worker forever."""
    if os.name == "nt":
        # PyInstaller may have a bootstrap parent plus a child app. Killing just
        # the MSYS shell PID can leave the actual Windows exe running.
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # Clean up descendants too, even if the parent already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return False
    return True


def smoke(binary, startup_timeout=120, shutdown_timeout=5):
    binary = Path(binary).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(f"::error::{binary} not found or not executable")
        return 1
    work = Path(tempfile.mkdtemp(prefix="plasma-smoke-")).resolve()
    log = work / "plasma.log"
    # A unique port prevents an orphan from a previous smoke test satisfying
    # this test's readiness probe. The app must bind this exact requested port.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PLASMA_HOME": str(work),
           "GRADIO_SERVER_NAME": "127.0.0.1", "GRADIO_SERVER_PORT": str(port),
           "GRADIO_NUM_PORTS": "1",
           "PLASMA_NO_BROWSER": "1",
           "GRADIO_ANALYTICS_ENABLED": "False"}
    env.pop("PYTHONPATH", None)
    url = f"http://127.0.0.1:{port}/"
    print(f"Smoke test: {binary}\nClean directory: {work}\nReadiness URL: {url}", flush=True)
    ready, stopped = False, False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with log.open("wb") as output:
        try:
            process = subprocess.Popen([str(binary)], cwd=work, env=env, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       start_new_session=(os.name != "nt"))
        except OSError as exc:
            print(f"::error::Cannot launch frozen app: {exc}")
            return 1
        try:
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    print(f"App exited before serving (exit {process.returncode})")
                    break
                try:
                    with opener.open(url, timeout=min(2, max(.05, deadline - time.monotonic()))) as response:
                        if response.status == 200:
                            ready = True
                            break
                except (OSError, urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
        finally:
            stopped = stop_tree(process, shutdown_timeout)
    text = log.read_text(encoding="utf-8", errors="replace")
    print("----- app output -----\n" + text[-30000:] + "\n----------------------")
    if not stopped:
        print("::error::App process could not be terminated within the shutdown deadline")
        return 1
    if not ready:
        print(f"::error::Gradio server never came up on {url} within {startup_timeout}s")
        return 1
    if re.search(r"plugin .* unavailable|An error occurred:|Traceback \(most recent call last\)|No module named", text):
        print("::error::Frozen app reported a missing module / import failure")
        return 1
    print("Smoke test passed (clean directory, startup and shutdown bounded)")
    return 0


if __name__ == "__main__":
    sys.exit(smoke(os.environ["PLASMA_BIN"]))
