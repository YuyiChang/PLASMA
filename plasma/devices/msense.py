from plasma.devices.template import PlasmaDevice, PlasmaMemo
import simplepyble
import datetime
import os
import logging
import gradio as gr
import time
from pylsl import StreamInfo, StreamOutlet, cf_double64
import numpy as np
import struct
from plasma.config import __data_dir__, __version__, device_config
from plasma.quaternion import IDENTITY_QUAT, quat_multiply, quat_normalize
from plasma.gyro_bias import load_gyro_bias, save_gyro_bias

yams_dir = __data_dir__

class MotionSenseHRV(PlasmaDevice):
    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)

        self.device_list = device_config.get_active_msense_devices()
        self.imu_stream_devices = device_config.get_imu_stream_devices()

        bias_by_addr = load_gyro_bias()

        self.memo = {}
        self.orientation_quat = {}
        self.gyro_bias = {}
        self.gyro_calib = {}
        for k, addr in self.device_list.items():
            channels = ["ENMO", "counter"]
            if k in self.imu_stream_devices:
                channels += ["AccX", "AccY", "AccZ", "Q0", "Q1", "Q2", "Q3", "OrientX", "OrientY", "OrientZ", "OrientW"]
                self.orientation_quat[k] = IDENTITY_QUAT
                self.gyro_bias[k] = bias_by_addr.get(addr, (0.0, 0.0, 0.0))
            self.memo[k] = PlasmaMemo(k, channels=channels)

        # fallback reference in case data arrives before start() is clicked;
        # start() resets this to the true session-start time
        self.t_start = time.time()

        self.init_adapter()

        self.active_devices = {}
        self.active_outlets = {}

        self.scan_devices()
        self.connect_devices()
        
    def init_adapter(self):
        adapters = simplepyble.Adapter.get_adapters()
        assert len(adapters) > 0, "No BT adapter found"
        
        self.adapter = adapters[0]

        # print(f"Selected adapter: {self.adapter.identifier()} [{self.adapter.address()}]")
        self.info(f"Selected adapter: {self.adapter.identifier()} [{self.adapter.address()}]")

    def scan_devices(self, filter_name="MSense"):
        print("start scanning devices")
        self.info("start device scanning")
        self.ctl_state = "Start device scanning"
        self.adapter.scan_for(5000)
        peripherals = self.adapter.scan_get_results()

        self.devices = {}
        for i, peripheral in enumerate(peripherals):
            if filter_name in peripheral.identifier():
                self.info(f"{i}: {peripheral.identifier()} [{peripheral.address()}]")
                # try to look up device alias
                addr = peripheral.address().upper()
                if addr in self.device_list.keys():
                    alias = self.device_list[addr]
                    name = f"{alias} ({peripheral.identifier()}) [{peripheral.address()}]"
                else:
                    name = f"{peripheral.identifier()} [{peripheral.address()}]"

                # self.devices[name] = 
                self.devices[peripheral.address().upper()] = {
                    "name": name,
                    "pheripheral": peripheral
                }


        print(self.devices)
        self.info("device scanning completed")
        self.ctl_state = "Device scanning completed"

    def connect_devices(self):
        del(self.active_devices)
        self.active_devices = {}
        del(self.active_outlets)
        self.active_outlets = {}
        self.ctl_state = "Start device connection"

        # quick sanity check
        for name, addr in self.device_list.items():
            self.info(f"Connecting to device {addr}")
            # assert addr in self.devices.keys(), self.info(f"Target device not found {addr}")

            if addr in self.devices.keys():
                dev = self.devices[addr]
                p = dev['pheripheral']
                n = dev['name']
                
                self.info(f"Starting to connect to {n}")
                # gr.Info(f"Connecting to devices: {n}")
                print(f'==== {n}')
                print(f"=== {p.identifier()} at {p.address()}")
                p.set_callback_on_connected(lambda: self.info(f"{n} {p.identifier()} is connected"))
                p.set_callback_on_disconnected(lambda: self.info(f"{n} {p.identifier()} is disconnected"))
                p.connect()
                self.active_devices[name] = p
                self.active_outlets[name] = MsenseOutlet(n, p)
            else:
                self.memo[name].sts = "⛔ device not found"

        # self.info(f"All target devices connected")
        # self.ctl_state = "Device(s) connected"

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
        self.reset_orientation()
        self.info(f"Start data collection with out dir = {self.log_dir}")
        self.info(f"Subject ID = {self.session_info['sub_id']}")
        self.info(f"Session ID = {self.session_info['ses_id']}")
        self.info(f"Participant encoding = {self.session_info['participant_enc']}")

        for name, p in self.active_devices.items():
            print(name, p.is_connected(), p.is_connectable())
            self.collection_ctl(name, True)
            self.active_outlets[name].log_dir = self.log_dir

        self.ctl_state = "Collection in progress"

        for m in self.memo.values():
            m.sts = "🟢"

    def stop(self):
        gr.Info("🛑 Stop data collection...")
        self.info("Data collection stopped")
        for name, p in self.active_devices.items():
            print(name, p.is_connected(), p.is_connectable())
            self.collection_ctl(name, False)

        self.ctl_state = "Collection stopped"

        for m in self.memo.values():
            m.sts = "🛑"

    def reset_orientation(self):
        for name in self.orientation_quat:
            self.orientation_quat[name] = IDENTITY_QUAT

    def disconnect(self):
        for name, p in list(self.active_devices.items()):
            try:
                if p.is_connected():
                    p.disconnect()
            except Exception as e:
                self.info(f"Error disconnecting {name}: {e}")
        self.active_devices = {}
        self.active_outlets = {}

    def start_gyro_calibration(self, duration=3.0):
        now = time.time()
        for name in self.imu_stream_devices:
            if name in self.active_devices:
                self.gyro_calib[name] = {"until": now + duration, "sum": [0.0, 0.0, 0.0], "n": 0}
                self.memo[name].sts = "🎯 Calibrating..."
                self.info(f"Started gyro bias calibration for {name} ({duration}s) — keep the wristband still")

    def _finish_gyro_calibration(self, name):
        calib = self.gyro_calib.pop(name, None)
        if calib is None or calib["n"] == 0:
            return
        bias = tuple(s / calib["n"] for s in calib["sum"])
        self.gyro_bias[name] = bias
        addr = self.device_list.get(name)
        if addr:
            save_gyro_bias(addr, bias, calib["n"])
        self.memo[name].sts = "✅ Bias saved"
        self.info(f"Gyro bias calibrated for {name}: {bias} (n={calib['n']})")

    def collection_ctl(self, name, start=True):
        peripheral = self.active_devices[name]

        # if starting, do the initialization
        if start:
            # write unix time
            peripheral.write_request("da39c930-1d81-48e2-9c68-d0ae4bbd351f", 
                                     "da39c932-1d81-48e2-9c68-d0ae4bbd351f", 
                                     struct.pack("<Q", int(time.time())))
            # write participant hash
            self.participant_byte = struct.pack("<I", self.session_info['participant_enc'])
            peripheral.write_request("da39c930-1d81-48e2-9c68-d0ae4bbd351f",
                                     "da39c933-1d81-48e2-9c68-d0ae4bbd351f", 
                                     self.participant_byte)

        service_uuid = "da39c930-1d81-48e2-9c68-d0ae4bbd351f"
        characteristic_uuid = "da39c931-1d81-48e2-9c68-d0ae4bbd351f"
        peripheral.write_request(service_uuid, characteristic_uuid, struct.pack("<I", int(start)))

        self.register_enmo(peripheral, name)

        if name in self.imu_stream_devices:
            try:
                self.register_imu_stream(peripheral, name)
            except Exception as e:
                self.info(f"IMU stream unavailable on {name} (demo firmware not present?): {e}")

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
        ENMO = struct.unpack("<f", data[0:4])

        if len(data) == 8:
            packet_counter = struct.unpack("<I", data[4:8])
        elif len(data) == 6:
            packet_counter = struct.unpack("<H", data[4:6])
        
        horizontal_array = [ENMO[0], packet_counter[0]]
        # print(f"{name}: package counter", horizontal_array)

        self.active_outlets[name].push_sample([ENMO[0], packet_counter[0]])
        # print(f"{ENMO[0]} {packet_counter[0]}", self.memo.keys(), name)
        self.memo[name].set_latest(f"{ENMO[0]} {packet_counter[0]}")
        elapsed = time.time() - self.t_start
        self.memo[name].set_data("ENMO", ENMO[0], elapsed)
        self.memo[name].set_data("counter", packet_counter[0], elapsed)

    # demo feature: real-time accel + orientation, only on wristbands with the
    # demo firmware (see data/IMU_STREAM_BLE_CHARACTERISTIC.md)
    def register_imu_stream(self, peripheral, name):
        service_uuid = "da39c950-1d81-48e2-9c68-d0ae4bbd351f"
        characteristic_uuid = "da39c953-1d81-48e2-9c68-d0ae4bbd351f"
        peripheral.notify(service_uuid, characteristic_uuid, lambda data: self.imu_stream_handler(data, peripheral, name))

    def imu_stream_handler(self, data, peripheral, name):
        # ±4g default sensitivity divisor; see IMU_STREAM_BLE_CHARACTERISTIC.md
        ACCEL_DIVISOR = 8192
        acc_x, acc_y, acc_z, q0, q1, q2, counter = struct.unpack("<hhhfffH", data)

        q3_sq = 1.0 - q0 * q0 - q1 * q1 - q2 * q2
        q3 = q3_sq ** 0.5 if q3_sq > 0 else 0.0

        # gyro bias calibration: accumulate the *raw* per-frame vector part
        # while stationary — see start_gyro_calibration/_finish_gyro_calibration
        calib = self.gyro_calib.get(name)
        if calib is not None:
            if time.time() < calib["until"]:
                calib["sum"][0] += q0
                calib["sum"][1] += q1
                calib["sum"][2] += q2
                calib["n"] += 1
            else:
                self._finish_gyro_calibration(name)

        # subtract the calibrated bias (small-angle approx: bias lives in the
        # same near-identity vector-part space as the delta itself), then
        # re-derive the scalar term the same way the raw q3 was reconstructed
        bx, by, bz = self.gyro_bias.get(name, (0.0, 0.0, 0.0))
        cx, cy, cz = q0 - bx, q1 - by, q2 - bz
        cw_sq = 1.0 - cx * cx - cy * cy - cz * cz
        cw = cw_sq ** 0.5 if cw_sq > 0 else 0.0

        # per-frame delta rotation (x, y, z, w); NOT absolute orientation —
        # composed below into a running estimate since the last reset
        # (see data/IMU_STREAM_BLE_CHARACTERISTIC.md)
        delta = (cx, cy, cz, cw)
        prev = self.orientation_quat.get(name, IDENTITY_QUAT)
        composed = quat_normalize(quat_multiply(prev, delta))
        self.orientation_quat[name] = composed
        ox, oy, oz, ow = composed

        elapsed = time.time() - self.t_start
        self.memo[name].set_data("AccX", acc_x / ACCEL_DIVISOR, elapsed)
        self.memo[name].set_data("AccY", acc_y / ACCEL_DIVISOR, elapsed)
        self.memo[name].set_data("AccZ", acc_z / ACCEL_DIVISOR, elapsed)
        self.memo[name].set_data("Q0", q0, elapsed)
        self.memo[name].set_data("Q1", q1, elapsed)
        self.memo[name].set_data("Q2", q2, elapsed)
        self.memo[name].set_data("Q3", q3, elapsed)
        self.memo[name].set_data("OrientX", ox, elapsed)
        self.memo[name].set_data("OrientY", oy, elapsed)
        self.memo[name].set_data("OrientZ", oz, elapsed)
        self.memo[name].set_data("OrientW", ow, elapsed)


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