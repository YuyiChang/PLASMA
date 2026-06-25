import blickfeld_qb2
import numpy as np
from pylsl import StreamInfo, StreamOutlet
import threading
import time
import asyncio
from blickfeld_qb2.base.grpc.channel import Channel
import socket
import os
import gradio as gr
import threading
import asyncio
from plasma.devices.template import PlasmaDevice, PlasmaMemo
# from plasma.config import IP_QB2_LIDAR
from plasma.config import plasma_config

class Qb2(PlasmaDevice):
    def __init__(self, session_info, logger, tag) -> None:
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo('qb2 LiDAR')
        self.session_info = session_info
        self.addr = plasma_config.ip_lidar
        self.status = ""
        self.last_lsl = ""

        self.out_dir = os.path.join(self.session_info['log_dir'], "qb2")
        os.makedirs(self.out_dir, exist_ok=True)

        print(f"Initializing Qb2 LiDAR at {self.addr}")


        self._thread = None
        self._stop_event = threading.Event()
        
    # def init_device(self):
        info = StreamInfo('blickfield_qb2', 'Image', 1, 8, 'int32', 'qb2-xxxx')
        self.outlet = StreamOutlet(info)

        try:
            channel = blickfeld_qb2.Channel(fqdn_or_ip=self.addr)
            print("qb2 ready")
            self.memo.sts = "Ready"
        except Exception as e:
            gr.Error(str(e))
            self.memo.sts = f"⛔ {str(e)}"

    def interface(self):
        pass

    def streaming(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # self.loop = asyncio.get_event_loop()
        # asyncio.set_event_loop(self.loop)
        # asyncio.new_event_loop()

        with blickfeld_qb2.Channel(fqdn_or_ip=self.addr) as channel:
            service = blickfeld_qb2.core_processing.services.PointCloud(channel)

            for i, response in enumerate(service.stream()):

                # Extract a point cloud frame from the response
                frame = response.frame

                self.outlet.push_sample([frame.id])
                self.memo.latest("frame ID = ", frame.id)

                # response = client.recv(1024).decode()
                # should_continue = response.lower() == 'true'
                # print(f"Should continue: {should_continue}")

                out_path = os.path.join(self.out_dir, f"qb2-{frame.id}.npy")

                if frame.id % 100 == 0:
                    print("Received frame with ID:", frame.id)

                with open(out_path, "wb") as f:
                    np.save(f, frame, allow_pickle=True)

                if self._stop_event.is_set():
                    break
