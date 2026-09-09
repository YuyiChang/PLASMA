#!/usr/bin/env python3
"""Regenerate every screenshot in the PLASMA Playbook.

Boots PLASMA headless with the **simulated MSense device** (no hardware, no
Bluetooth), drives it into a series of normal and abnormal states, and captures
PNGs with Playwright + the system Google Chrome. Output → ``docs/assets/screenshots``.

    python scripts/capture_screenshots.py                 # default home, all shots
    python scripts/capture_screenshots.py --home /tmp/x --clean
    python scripts/capture_screenshots.py --only session-dashboard collecting

By default ``PLASMA_HOME`` is ``~/Library/Application Support/PLASMA`` — the same
directory the packaged macOS app uses — so paths shown in the memo panel read
like production. Pass ``--home`` to point somewhere disposable. ``--clean`` wipes
``<home>/data`` and rewrites the demo section of ``plasma_device_config.json``
before starting.

Requires ``pip install -e ".[docs]"`` and Google Chrome. One command, ~4 min.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
SHOT_DIR = REPO / "docs" / "assets" / "screenshots"
DEFAULT_HOME = pathlib.Path.home() / "Library" / "Application Support" / "PLASMA"

VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2
SUB, SES = "sub-4021", "ses-02"           # deterministic → stable visible paths

# MSense-only catalog — a clean dashboard with no spurious hardware faults
DEMO_CATALOG = ["MSense Demo (simulated)"]


def demo_devices(fault_ppg="none", fault_ecg="none"):
    return [
        {"Name": "DEMO-PPG-01", "Nickname": "left wrist", "Sensor": "PPG",
         "Enabled": True, "IMU Stream": True, "Fault": fault_ppg},
        {"Name": "DEMO-ECG-02", "Nickname": "chest", "Sensor": "ECG",
         "Enabled": True, "IMU Stream": False, "Fault": fault_ecg},
    ]


def write_config(home: pathlib.Path, devices):
    cfg = {
        "enabled_devices": DEMO_CATALOG,
        "ip_qb2_lidar": "",
        "ip_pupil_labs": "",
        "plugins": {"msense_demo": {"devices": devices}},
        "demo_mode": True,
    }
    (home).mkdir(parents=True, exist_ok=True)
    (home / "plasma_device_config.json").write_text(json.dumps(cfg, indent=2))


# ── server subprocess ───────────────────────────────────────────────────────

def _serve(port: int):
    """Run inside a child process: build the real PLASMA UI and block."""
    import gradio as gr  # noqa: F401
    from plasma import plugins
    from plasma.config import device_config
    from plasma.integrated_panel import IntegratedPanel
    from plasma.__main__ import build_blocks

    plugins.load_plugins()
    device_config.refresh_defaults()
    ip = IntegratedPanel()
    app = build_blocks(ip)
    app.launch(server_port=port, inbrowser=False, prevent_thread_lock=True,
               show_error=True, quiet=True)
    while True:
        time.sleep(3600)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, home: pathlib.Path):
        self.home = home
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def __enter__(self):
        env = dict(os.environ, PLASMA_HOME=str(self.home), PLASMA_DEMO="1")
        self.proc = subprocess.Popen(
            [sys.executable, __file__, "--serve", "--port", str(self.port)],
            env=env, cwd=str(REPO))
        for _ in range(120):
            try:
                urllib.request.urlopen(self.url, timeout=1)
                time.sleep(2.0)                 # let the first render settle
                return self
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("PLASMA server exited during startup")
                time.sleep(1.0)
        raise RuntimeError("PLASMA server did not come up in 120 s")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── Playwright helpers ──────────────────────────────────────────────────────

APP = ".gradio-container"


class Shooter:
    def __init__(self, page):
        self.page = page
        self.taken = []

    def _settle(self, ms=900):
        self.page.wait_for_timeout(ms)

    def tab(self, name, settle=1600):
        self.page.get_by_role("tab").filter(has_text=name).first.click()
        self._settle(settle)

    def subtab(self, name, settle=1500):
        # YAMS inner tabs are also role=tab; match the plain "Extractor"
        # ahead of "Extractor (zip)" by taking the shortest label
        tabs = self.page.get_by_role("tab").filter(has_text=name)
        n = tabs.count()
        pick = tabs.first
        if n > 1:
            texts = [(len(tabs.nth(i).inner_text()), i) for i in range(n)]
            pick = tabs.nth(min(texts)[1])
        pick.click()
        self._settle(settle)

    def expand(self, label):
        try:
            self.page.get_by_text(label, exact=False).first.click()
            self._settle(500)
        except Exception:
            pass

    def full(self, name, whole=False):
        """Screenshot the app content (crops trailing whitespace) unless
        `whole` forces a full-page capture."""
        self._settle()
        if whole:
            self.page.screenshot(path=str(SHOT_DIR / f"{name}.png"),
                                 full_page=True)
        else:
            try:
                self.page.locator(APP).first.screenshot(
                    path=str(SHOT_DIR / f"{name}.png"))
            except Exception:
                self.page.screenshot(path=str(SHOT_DIR / f"{name}.png"),
                                     full_page=True)
        self.taken.append(name)
        print("  ✓", name)

    def clip(self, name, selector):
        self._settle()
        self.page.locator(selector).first.screenshot(
            path=str(SHOT_DIR / f"{name}.png"))
        self.taken.append(name)
        print("  ✓", name)


MEMO = "#plasma-memo-panel"


def client(url):
    from gradio_client import Client
    return Client(url, verbose=False)


# Note: don't reload the page mid-scenario. The memo panel's 1 Hz timer returns
# gr.skip() when its HTML is unchanged, so a freshly-loaded page can sit on the
# build-time "Welcome" value forever. Keep one page open per scenario and let
# the live timer update it in place; poll /status (wait_for_sts) for the moment.


def _predict(c, api_name, *args):
    try:
        return c.predict(*args, api_name=api_name)
    except Exception as e:
        print(f"  ! {api_name}: {e}")
        return None


def wait_for_sts(c, needle, timeout=35):
    """Poll /status until any device source's status text contains `needle`."""
    needle = needle.lower()
    end = time.time() + timeout
    while time.time() < end:
        st = _predict(c, "/status")
        for d in (st or {}).get("devices", []):
            for src in d.get("sources", []):
                if needle in str(src.get("sts", "")).lower():
                    return True
        time.sleep(1.0)
    print(f"  ! timed out waiting for status ~ {needle!r}")
    return False


# ── scenarios ───────────────────────────────────────────────────────────────

def scenario_normal(page, url, want):
    c = client(url)
    s = Shooter(page)

    if "session-dashboard" in want:
        s.tab("Session Dashboard")
        s.full("session-dashboard")

    if "configure" in want:
        s.tab("Configuration")
        s.full("configure")

    need_init = "initialize" in want
    need_collect = {"collecting", "journaler"} & want
    need_yams = {w for w in want if w.startswith("yams")}
    need_stop = "stopped" in want

    if need_init or need_collect or need_yams or need_stop:
        s.tab("Session Dashboard")
        # Initialize via the button so the memo shows "Ready to start" + rows
        page.get_by_role("button", name="Initialize").first.click()
        page.wait_for_timeout(3500)
        if need_init:
            s.clip("memo-ready", MEMO)

    if need_collect or need_yams or need_stop:
        _predict(c, "/start", SUB, SES, ["MSense Demo (simulated)"], True)
        page.wait_for_timeout(6000)

    if "collecting" in want:
        s.tab("Session Dashboard")
        s.full("session-collecting")
        s.clip("memo-collecting", MEMO)
    if "journaler" in want:
        s.tab("Session Dashboard")
        s.expand("Journaler")
        s.full("journaler")

    if "data-dashboard" in want:
        s.tab("Data Dashboard")
        s.full("data-dashboard")

    if need_yams:
        s.tab("YAMS")
        for sub, shot in [
            ("Signal Quality", "yams-signal-quality"),
            ("IMU", "yams-imu"),
            ("Control", "yams-control"),
            ("Downloader", "yams-downloader"),
            ("Extractor", "yams-extractor"),
            ("Clock Sync", "yams-clocksync"),
            ("Data viewer", "yams-dataviewer"),
            ("Devices", "yams-devices"),
        ]:
            if shot in want:
                s.subtab(sub)
                if shot == "yams-control":
                    for a in ("Danger zone", "Advanced", "Acquisition-stop"):
                        s.expand(a)
                s.full(shot)
        if "yams-signal-quality-snapshot" in want:
            s.subtab("Signal Quality")
            _predict(c, "/_request", "History Only", 0, "Sequential")
            page.wait_for_timeout(3500)
            s.full("yams-signal-quality-snapshot")

    if "stopped" in want:
        _predict(c, "/stop")
        s.tab("Session Dashboard")
        page.wait_for_timeout(2500)
        s.clip("memo-stopped", MEMO)


def scenario_not_found(page, url, want):
    c = client(url)
    s = Shooter(page)
    s.tab("Session Dashboard")
    page.get_by_role("button", name="Initialize").first.click()
    page.wait_for_timeout(4000)
    s.clip("abnormal-device-not-found", MEMO)


def scenario_disconnect(page, url, want):
    c = client(url)
    s = Shooter(page)
    _predict(c, "/start", SUB, SES, ["MSense Demo (simulated)"], True)
    s.tab("Session Dashboard")
    wait_for_sts(c, "disconnect")             # DEMO_DISCONNECT_AFTER_S = 15
    page.wait_for_timeout(1500)
    s.clip("abnormal-disconnected", MEMO)
    wait_for_sts(c, "reconnected")            # watchdog reconnect (~10 s)
    page.wait_for_timeout(1500)
    s.clip("abnormal-reconnected", MEMO)
    _predict(c, "/stop")


def scenario_sqc(page, url, want):
    c = client(url)
    s = Shooter(page)
    _predict(c, "/start", SUB, SES, ["MSense Demo (simulated)"], True)
    page.wait_for_timeout(2500)
    _predict(c, "/_request", "All", 0, "Sequential")
    s.tab("Session Dashboard")
    wait_for_sts(c, "stall")                  # no-progress watchdog fires
    page.wait_for_timeout(1500)
    s.clip("abnormal-sqc-stalled", MEMO)
    s.tab("YAMS")
    s.subtab("Signal Quality")
    s.full("abnormal-sqc-stalled-tab")
    _predict(c, "/stop")


def scenario_acq_stop(page, url, want):
    c = client(url)
    s = Shooter(page)
    _predict(c, "/start", SUB, SES, ["MSense Demo (simulated)"], True)
    page.wait_for_timeout(3000)
    s.tab("Session Dashboard")
    _predict(c, "/stop")
    page.wait_for_timeout(6000)
    s.clip("abnormal-acq-stop-unconfirmed", MEMO)
    s.tab("YAMS")
    s.subtab("Control")
    s.expand("Acquisition-stop")
    s.full("abnormal-acq-stop-control")


SCENARIOS = {
    "normal": (
        lambda: demo_devices(),
        scenario_normal,
        ["session-dashboard", "configure", "initialize", "collecting",
         "journaler", "data-dashboard", "stopped",
         "yams-signal-quality", "yams-signal-quality-snapshot", "yams-imu",
         "yams-control", "yams-downloader", "yams-extractor", "yams-clocksync",
         "yams-dataviewer", "yams-devices"],
    ),
    "not-found": (
        lambda: demo_devices(fault_ppg="device_not_found"),
        scenario_not_found, ["abnormal-device-not-found"],
    ),
    "disconnect": (
        lambda: demo_devices(fault_ppg="disconnect_reconnect"),
        scenario_disconnect, ["abnormal-disconnected", "abnormal-reconnected"],
    ),
    "sqc": (
        lambda: demo_devices(fault_ppg="sqc_error"),
        scenario_sqc, ["abnormal-sqc-stalled", "abnormal-sqc-stalled-tab"],
    ),
    "acq-stop": (
        lambda: demo_devices(fault_ecg="acq_stop_ignored"),
        scenario_acq_stop, ["abnormal-acq-stop-unconfirmed",
                            "abnormal-acq-stop-control"],
    ),
}


# ── driver ─────────────────────────────────────────────────────────────────

def run(home: pathlib.Path, only: set[str] | None, clean: bool):
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    # the capture run overwrites plasma_device_config.json and writes demo
    # session data under <home>/data — snapshot the user's real config and
    # restore it afterwards so running against the production home is safe
    cfg_path = home / "plasma_device_config.json"
    saved_cfg = cfg_path.read_bytes() if cfg_path.exists() else None
    if clean and (home / "data").exists():
        shutil.rmtree(home / "data")

    try:
        _run(home, only)
    finally:
        if saved_cfg is not None:
            cfg_path.write_bytes(saved_cfg)
            print("\n· restored", cfg_path)
        elif cfg_path.exists():
            cfg_path.unlink()


def _run(home, only):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for key, (mk_devices, fn, shots) in SCENARIOS.items():
            want = set(shots) if only is None else (set(shots) & only)
            if not want:
                continue
            print(f"\n▶ scenario {key}  ({', '.join(sorted(want))})")
            write_config(home, mk_devices())
            with Server(home) as srv:
                ctx = browser.new_context(viewport=VIEWPORT,
                                          device_scale_factor=SCALE)
                page = ctx.new_page()
                page.goto(srv.url + "/?__theme=light",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                try:
                    fn(page, srv.url, want)
                finally:
                    ctx.close()
        browser.close()

    print(f"\n✓ screenshots in {SHOT_DIR}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--home", type=pathlib.Path, default=DEFAULT_HOME,
                    help="PLASMA_HOME for the capture run "
                         f"(default: {DEFAULT_HOME})")
    ap.add_argument("--clean", action="store_true",
                    help="wipe <home>/data before starting")
    ap.add_argument("--only", nargs="+", metavar="SHOT",
                    help="capture only these shot names")
    args = ap.parse_args(argv)

    if args.serve:
        _serve(args.port)
        return
    run(args.home.expanduser(), set(args.only) if args.only else None, args.clean)


if __name__ == "__main__":
    main()
