from plasma.devices.template import PlasmaDevice
import simplepyble
import datetime
import os
import logging
import gradio as gr
import time
from pylsl import StreamInfo, StreamOutlet, cf_double64
import numpy as np
import struct

yams_dir = "data"
__version__ = "0.1.0-beta"

# device name - mac addr here
device_list = {
    "MSense Left 74N": "D3:54:EB:A4:9B:82",
    "Msense Right 70N": "FF:7D:06:B4:51:98",
}

class MotionSenseHRV(PlasmaDevice):
    def __init__(self, session_info):
        super().__init__(session_info)

        self.name = {}
        for k in device_list.keys():
            self.name[k] = "🟦"

        # current YYMMDD
        now = datetime.datetime.now()
        date = now.strftime("%Y-%m-%d")
    
        # init logger
        self.logger = logging.getLogger(__name__)
        os.makedirs(yams_dir, exist_ok=True)
        logging.basicConfig(level=logging.INFO, 
                            format='%(asctime)s [%(levelname)s] %(message)s',
                            handlers=[
                                logging.FileHandler(os.path.join(yams_dir, f"{date}_yams_session.log")),
                                logging.StreamHandler()
                            ])
        self.logger.info(f"Begin PLASMA v{__version__} session log")

        self.init_adapter()

        self.active_devices = {}
        self.active_outlets = {}

        # self.connect_devices()
        
    def init_adapter(self):
        adapters = simplepyble.Adapter.get_adapters()
        assert len(adapters) > 0, "No BT adapter found"
        
        self.adapter = adapters[0]

        # print(f"Selected adapter: {self.adapter.identifier()} [{self.adapter.address()}]")
        self.logger.info(f"Selected adapter: {self.adapter.identifier()} [{self.adapter.address()}]")

    def scan_devices(self, filter_name="MSense"):
        print("start scanning devices")
        self.logger.info("start device scanning")
        self.ctl_state = "Start device scanning"
        self.adapter.scan_for(5000)
        peripherals = self.adapter.scan_get_results()

        self.devices = {}
        for i, peripheral in enumerate(peripherals):
            if filter_name in peripheral.identifier():
                self.logger.info(f"{i}: {peripheral.identifier()} [{peripheral.address()}]")
                # try to look up device alias
                addr = peripheral.address().upper()
                if addr in self.device_name.keys():
                    alias = self.device_name[addr]
                    name = f"{alias} ({peripheral.identifier()}) [{peripheral.address()}]"
                else:
                    name = f"{peripheral.identifier()} [{peripheral.address()}]"

                # self.devices[name] = 
                self.devices[peripheral.address()] = {
                    "name": name,
                    "pheripheral": peripheral
                }

        self.ctl_state = "Device scanning completed"

    def connect_devices(self):
        del(self.active_devices)
        self.active_devices = {}
        del(self.active_outlets)
        self.active_outlets = {}
        self.ctl_state = "Start device connection"

        # quick sanity check
        for addr in device_list.values():
            assert addr in self.devices.keys(), self.logger.info(f"Target device not found {addr}")

            dev = self.devices[addr]
            p = dev['pheripheral']
            n = dev['name']

            self.logger.info(f"Starting to connect to {n}")
            # gr.Info(f"Connecting to devices: {n}")
            print(f'==== {n}')
            p = self.devices[n]
            print(f"=== {p.identifier()} at {p.address()}")
            p.set_callback_on_connected(lambda: self.logger.info(f"{n} {p.identifier()} is connected"))
            p.set_callback_on_disconnected(lambda: self.logger.info(f"{n} {p.identifier()} is disconnected"))
            p.connect()
            self.active_devices[n] = p
            self.active_outlets[n] = MsenseOutlet(n, p, use_lsl=self.use_lsl)

        self.logger.info(f"All target devices connected")
        self.ctl_state = "Device(s) connected"

    def start(self):
        timestamp = time.strftime("%y%m%d_%H%M")
        # create log dir
        self.log_dir = os.path.join(yams_dir, 
                                    self.session_info['sub_id'], 
                                    self.session_info['ses_id'], 
                                    f"{self.session_info['participant_enc']}_{timestamp}")
        print(f"create log dir {self.log_dir}")
        os.makedirs(self.log_dir, exist_ok=True)

        gr.Info("▶️ Start data collection...")
        self.t_start = time.time()
        self.logger.info(f"Start data collection with out dir = {self.log_dir}")
        self.logger.info(f"Subject ID = {self.session_info['sub_id']}")
        self.logger.info(f"Session ID = {self.session_info['ses_id']}")
        self.logger.info(f"Participant encoding = {self.session_info['participant_enc']}")

        for name, p in self.active_devices.items():
            print(name, p.is_connected(), p.is_connectable())
            self.collection_ctl(name, True)
            self.active_outlets[name].log_dir = self.log_dir

        self.ctl_state = "Collection in progress"

    def stop(self):
        gr.Info("🛑 Stop data collection...")
        self.logger.info("Data collection stopped")
        for name, p in self.active_devices.items():
            print(name, p.is_connected(), p.is_connectable())
            self.collection_ctl(name, False)

        self.ctl_state = "Collection stopped"
    
    def collection_ctl(self, name, start=True):
        peripheral = self.active_devices[name]

        # if starting, do the initialization
        if start:
            # write unix time
            peripheral.write_request("da39c930-1d81-48e2-9c68-d0ae4bbd351f", 
                                     "da39c932-1d81-48e2-9c68-d0ae4bbd351f", 
                                     struct.pack("<Q", int(time.time())))
            # write participant hash
            peripheral.write_request("da39c930-1d81-48e2-9c68-d0ae4bbd351f",
                                     "da39c933-1d81-48e2-9c68-d0ae4bbd351f", 
                                     self.participant_byte)

        service_uuid = "da39c930-1d81-48e2-9c68-d0ae4bbd351f"
        characteristic_uuid = "da39c931-1d81-48e2-9c68-d0ae4bbd351f"
        peripheral.write_request(service_uuid, characteristic_uuid, struct.pack("<I", int(start)))

        self.register_enmo(peripheral, name)

        # 
        # if start and self.auto_reconnect:
        #     self.start_device_monitor()
        # elif not start:
        #     self.stop_device_monitor()
            

    def register_enmo(self, peripheral, name):
        # ENMO 
        service_uuid = "da39c950-1d81-48e2-9c68-d0ae4bbd351f"
        characteristic_uuid = "da39c951-1d81-48e2-9c68-d0ae4bbd351f"
        contents = peripheral.notify(service_uuid, characteristic_uuid, lambda data: self.enmo_handler(data, peripheral, name))


    def enmo_handler(self, data, peripheral, name):
        # print(peripheral.identifier(), data)
        packet_counter = data[4:6]
        ENMO = struct.unpack("<f", data[0:4])
        
        packet_counter = struct.unpack("<H", packet_counter)
        horizontal_array = [ENMO[0], packet_counter[0]]
        print(f"{name}: package counter", horizontal_array)

        self.active_outlets[name].push_sample([ENMO[0], packet_counter[0]])


class MsenseOutlet(StreamOutlet):
    def __init__(self, name, peripheral, chunk_size=32, max_buffered=360, use_lsl=True):
        self.name = name.replace(':', '-')
        self.use_lsl = use_lsl

        lsl_status = "OK" if self.use_lsl else "disabled"
        self.msg = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."
        self.msg_fun = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."

        if self.use_lsl:
            info = StreamInfo(name, "MotionSenSE", 3, 2, cf_double64, peripheral.address())
            super().__init__(info, chunk_size, max_buffered)

        self.log_dir = os.path.join(yams_dir, "default")

    def tic(self):
        now = datetime.datetime.now()
        return now.strftime("%H:%M:%S")

    def save_data(self, data):
        self.log_path = os.path.join(self.log_dir, f"{self.name}.txt")
        # Ensure the file exists
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w') as f: pass

        # Append NumPy array as a line
        with open(self.log_path, 'a') as f:
            np.savetxt(f, [data], fmt='%s')

    def push_sample(self, x):
        if self.use_lsl:
            formatted = '\t'.join(str(num) for num in x)
            self.msg = f"📻 {self.tic()} last LSL pushed: {formatted}"
            
            fun_msg = "".join(["✅" for i in range(int(time.time())%10)])
            self.msg_fun = f"📻 {self.tic()} {fun_msg}"

            x.append(time.time())
            super().push_sample(x)

        self.save_data(x)