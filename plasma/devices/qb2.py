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
from plasma.devices.template import PlasmaDevice

class Qb2(PlasmaDevice):
    def __init__(self, session_info, addr="192.168.1.100") -> None:
        super().__init__(session_info)
        self.name = 'qb2 LiDAR'
        self.session_info = session_info
        self.addr = addr
        self.status = ""
        self.last_lsl = ""

        self.out_dir = os.path.join(self.session_info['log_dir'], "qb2")
        os.makedirs(self.out_dir, exist_ok=True)

        self._thread = None
        self._stop_event = threading.Event()
        
    # def init_device(self):
        info = StreamInfo('blickfield_qb2', 'Image', 1, 8, 'int32', 'qb2-xxxx')
        self.outlet = StreamOutlet(info)

        try:
            channel = blickfeld_qb2.Channel(fqdn_or_ip=self.addr)
            print("qb2 ready")
        except Exception as e:
            gr.Error(str(e))

    def interface(self):
        pass

    def streaming(self):
        with blickfeld_qb2.Channel(fqdn_or_ip=self.addr) as channel:
            service = blickfeld_qb2.core_processing.services.PointCloud(channel)

            for i, response in enumerate(service.stream()):

                # Extract a point cloud frame from the response
                frame = response.frame

                self.outlet.push_sample([frame.id])
                # print("Received frame with ID:", frame.id)

                # response = client.recv(1024).decode()
                # should_continue = response.lower() == 'true'
                # print(f"Should continue: {should_continue}")

                out_path = os.path.join(self.out_dir, f"qb2-{frame.id}.npy")
                with open(out_path, "wb") as f:
                    np.save(f, frame, allow_pickle=True)
