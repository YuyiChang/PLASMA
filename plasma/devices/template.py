import threading
import time
import datetime

class PlasmaMemo():
    def __init__(self, name):
        self.name = name
        self.sts = "🟦" # status
        self.set_latest("initialized")

    def get_sts(self):
        return {
            "type": self.sts,
            "description": f"------ {self.latest}",
        }
    
    def set_latest(self, msg):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.latest = f"{now} {msg}"
    

class PlasmaDevice:
    def __init__(self, session_info, logger=None, tag=None):
        self.session_info = session_info
        self.logger = logger
        self.last_data = []
        self._thread = None
        self._stop_event = threading.Event()

        self.memo = PlasmaMemo("TestDevice")
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
