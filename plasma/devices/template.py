import threading
import time

class PlasmaDevice:
    def __init__(self, session_info):
        self.session_info = session_info
        self.name = "TestDevice"
        self.last_data = []
        self._thread = None
        self._stop_event = threading.Event()
        self.sts = "🟦"

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self.sts = "🟢"
            self._stop_event.clear()
            self._thread = threading.Thread(target=self.streaming, daemon=True)
            self._thread.start()

    def streaming(self):
        # your custom sensor callback goes here
        while not self._stop_event.is_set():
            self.last_data = (f"{self.name} reading at {time.time()}")
            time.sleep(1)

    def stop(self):
        self._stop_event.set()
        self.sts = "🟥"

    def latest(self):
        return self.last_data if self.last_data else "N/A"
