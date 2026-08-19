import threading
import time
import datetime
import numpy as np
from collections import deque

DEFAULT_WINDOW_S = 30.0

class PlasmaMemo():
    def __init__(self, name, channels=None, window_s=DEFAULT_WINDOW_S):
        self.name = name
        self.sts = "🟦" # status
        self.set_latest("initialized")
        self.window_s = window_s
        # named rolling buffers of (t, value), t = seconds since the caller's
        # own time reference (e.g. session start) — pruned to the last window_s
        self.channels = {ch: deque() for ch in (channels or [])}

    def get_sts(self):
        return {
            "type": self.sts,
            "description": f"------ {self.latest}",
        }

    def set_latest(self, msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.latest = f"{now} {msg}"

    def set_data(self, channel, value, t):
        buf = self.channels.setdefault(channel, deque())
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

        Most devices expose a single memo under their own tag; devices with
        multiple physical sub-devices (e.g. MSense wristbands) override
        self.memo with a {sub_device_name: PlasmaMemo} dict instead."""
        if isinstance(self.memo, dict):
            return self.memo
        return {self.tag: self.memo}

    def reset_orientation(self):
        """No-op by default; devices that compose a running orientation
        estimate (e.g. MSense IMU stream) override this."""
        pass

    def start_gyro_calibration(self, duration=3.0):
        """No-op by default; devices with a gyro-bias-correctable orientation
        estimate (e.g. MSense IMU stream) override this."""
        pass

    def disconnect(self):
        """No-op by default; devices holding an external connection (e.g.
        MSense BLE peripherals) override this to tear it down before the
        instance is discarded on re-initialization."""
        pass

    def info(self, msg):
        if self.logger is None:
            pass
        else:
            self.logger.info(f"[{self.tag}] {msg}")

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