import gradio as gr
import obsws_python as obs
import threading
from plasma.devices.template import PlasmaDevice, PlasmaMemo

# --- CONFIG: OBS instances ---
OBS_INSTANCES = [
    {"host": "127.0.0.1", "port": 4455, "password": "local_password"}, # streamer laptop
    {"host": "192.168.1.184", "port": 4455, "password": "remote_password"}, # psychopy laptop
]

class ObsRecorder(PlasmaDevice):
    def __init__(self, session_info, logger=None, tag=None):
        super().__init__(session_info, logger, tag)

        self.memo = PlasmaMemo('OBS Recorder')

        self.clients = []
        for cfg in OBS_INSTANCES:
            try:
                client = obs.ReqClient(host=cfg["host"], port=cfg["port"], password=cfg["password"], timeout=5)
                self.clients.append(client)
            except Exception as e:
                print(f"Failed to connect to OBS at {cfg['host']}: {e}")

    def start(self):
        try:
            for client in self.clients:
                client.start_record()
            self.memo.sts = "🟢"
        except Exception as e:
            self.memo.sts = f"FAULT: {str(e)}"
        
    def stop(self):
        try:
            for client in self.clients:
                client.stop_record()
            self.memo.sts = "🟥"
        except Exception as e:
            self.memo.sts = f"FAULT: {str(e)}"