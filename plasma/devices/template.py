import threading
import time
import datetime
import numpy as np

class PlasmaMemo():
    def __init__(self, name):
        self.name = name
        self.sts = "🟦" # status
        self.set_latest("initialized")
        self.data = [0] * 100

    def get_sts(self):
        return {
            "type": self.sts,
            "description": f"------ {self.latest}",
        }
    
    def set_latest(self, msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.latest = f"{now} {msg}"

    def set_data(self, data):
        print('=========', data)
        self.data.append(data)
        self.data.pop(0)


class PlasmaDevice:
    def __init__(self, session_info, logger=None, tag=None):
        self.session_info = session_info
        self.logger = logger
        self.last_data = []
        self._thread = None
        self._stop_event = threading.Event()

        self.memo = PlasmaMemo(tag)
        self.tag = tag

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
