import threading
import time
import datetime
import numpy as np
from collections import deque

DEFAULT_WINDOW_S = 30.0

class PlasmaMemo():
    def __init__(self, name, channels=None, window_s=DEFAULT_WINDOW_S, label=None,
                 channel_groups=None):
        self.name = name
        # human-facing label for UI panels; defaults to name. The identifier
        # stays `name` (LSL stream, status keys) — `label` is cosmetic.
        self.label = label or name
        self.sts = "🟦" # status
        self.set_latest("initialized")
        # transient purple sub-line ("Starting..." etc.) shown in place of
        # `.latest` while a slow per-device operation is in flight; None the
        # rest of the time. Never fed through status.classify() — purely a
        # rendering hook (see plasma/status.py "guidance" category).
        self.transition = None
        self.window_s = window_s
        # named rolling buffers of (t, value), t = seconds since the caller's
        # own time reference (e.g. session start) — pruned to the last window_s
        # by set_data(). The maxlen is a hard safety cap so a device whose `t`
        # stalls (stops advancing) can't grow a buffer without bound; the
        # time-based prune is the precise trim in normal operation.
        self._maxlen = max(4096, int(window_s * 250))
        self.channels = {ch: deque(maxlen=self._maxlen) for ch in (channels or [])}
        # optional {group label: [channel names]} the Signal visualizer uses to
        # put related channels on one shared subplot instead of one row each
        self.channel_groups = dict(channel_groups or {})

    def get_sts(self):
        return {
            "type": self.sts,
            "description": f"------ {self.latest}",
        }

    def set_latest(self, msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.latest = f"{now} {msg}"

    def set_transition(self, msg):
        """Show `msg` as a purple sub-line (replacing `.latest`) while a slow
        operation — connect, start, stop — is in flight. Pair with
        `clear_transition()` in a `finally` so it can't get stuck."""
        self.transition = msg

    def clear_transition(self):
        self.transition = None

    def set_data(self, channel, value, t):
        buf = self.channels.get(channel)
        if buf is None:
            buf = self.channels[channel] = deque(maxlen=getattr(self, "_maxlen", 30000))
        buf.append((t, value))
        cutoff = t - self.window_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def get_series(self, channel):
        """Returns (xs, ys) for a channel, xs being the caller-supplied time values."""
        buf = self.channels.get(channel)
        if not buf:
            return [], []
        xs, ys = zip(*buf)
        return list(xs), list(ys)

    def get_latest(self, channel):
        """Returns the most recent (t, value) for a channel, or None if empty."""
        buf = self.channels.get(channel)
        return buf[-1] if buf else None


class PlasmaDevice:
    # Set by IntegratedPanel.start_collection() right before start(), to the one
    # timestamped folder the XDF recorder and every device share for a session.
    # None when a device is constructed outside the panel (tests/scripts) — the
    # device then falls back to its own path logic.
    session_dir = None

    # Callable[[str], None] pushing a marker onto the session journaler
    # (IntegratedPanel.journal). Set per-instance by IntegratedPanel right after
    # construction; stays None in tests / standalone use.
    journal_hook = None

    def __init__(self, session_info, logger=None, tag=None):
        self.session_info = session_info
        self.logger = logger
        self.last_data = []
        self._thread = None
        self._stop_event = threading.Event()

        self.memo = PlasmaMemo(tag)
        self.tag = tag

    def get_sources(self):
        """Map of source-name -> PlasmaMemo for live visualization.

        Most devices expose a single memo under their own tag; a device with
        multiple physical sub-devices overrides self.memo with a
        {sub_device_name: PlasmaMemo} dict instead."""
        if isinstance(self.memo, dict):
            return self.memo
        return {self.tag: self.memo}

    def lsl_streams(self):
        """{LSL stream name -> key in self.get_sources()} for every LSL outlet
        this device publishes, so the dashboard can fold the recorder's
        per-stream stats into the right memo row. Default: a single-memo device
        whose stream name matches its memo name (bitalino, shimmer). Devices
        whose stream name differs (msense, qb2, pupil_labs) override this."""
        if isinstance(self.memo, dict):
            return {}
        return {self.memo.name: self.tag}

    def disconnect(self):
        """No-op by default; a device holding an external connection overrides
        this to tear it down before the instance is discarded on
        re-initialization."""
        pass

    def info(self, msg):
        if self.logger is None:
            pass
        else:
            self.logger.info(f"[{self.tag}] {msg}")

    def journal(self, text):
        """Push a marker onto the session journaler if the panel wired one up
        (no-op otherwise), and mirror it to the device log."""
        self.info(f"journal: {text}")
        hook = self.journal_hook
        if callable(hook):
            try:
                hook(text)
            except Exception as e:
                self.info(f"journal push failed: {e}")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self.memo.sts = "🟢"
            self._stop_event.clear()
            self._thread = threading.Thread(target=self.streaming, daemon=True)
            self._thread.start()

    def streaming(self):
        # your custom sensor callback goes here
        while not self._stop_event.is_set():
            self.last_data = (f"{self.tag} reading at {time.time()}")
            self.memo.set_latest(f"{self.tag} reading at {time.time()}")
            time.sleep(1)

    def stop(self):
        self._stop_event.set()
        self.memo.sts = "🟥"

    def latest(self):
        return self.last_data if self.last_data else "N/A"


class PlasmaDemoDevice(PlasmaDevice):
    def __init__(self, session_info, logger=None, tag=None):
        super().__init__(session_info, logger, tag)

        self.curr_fid = np.random.randint(0, 10000)
        
        # demo fault
        if 'IMU' in tag:
            self.memo.sts = "🚫 FAULT"

        if "IMU" in tag:
            self.demo = self.demo_imu
        elif "PPG" in tag:
            self.demo = self.demo_ppg
        elif "eye" in tag:
            self.demo = self.demo_eye_tracking
        elif "camera" in tag.lower():
            self.demo = self.demo_cam
        else:
            self.demo = self.demo_default

    # device specific behavior
    def demo_imu(self):
        return np.random.randn(6)
    
    def demo_ppg(self):
        return np.random.randn(4)
    
    def demo_cam(self):
        self.curr_fid += 1
        return self.curr_fid
    
    def demo_eye_tracking(self):
        return np.random.randn(2)
    
    def demo_eda(self):
        return np.random.randn(2)
    
    def demo_default(self):
        return time.time()

    # demo with customized streaming behavior
    def streaming(self):
        while not self._stop_event.is_set():
            reading = self.demo()
            self.last_data = (f"{self.tag} reading at {reading}")
            self.memo.set_latest(f"{self.tag} reading at {reading}")
            time.sleep(1)