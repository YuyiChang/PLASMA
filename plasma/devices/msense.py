from plasma.devices.template import PlasmaDevice, PlasmaMemo
import simplepyble
import atexit
import datetime
import json
import os
import logging
import queue
import gradio as gr
import threading
import time
from collections import deque
from pylsl import StreamInfo, StreamOutlet, cf_double64
import numpy as np
import struct
from plasma.config import __data_dir__, __version__, device_config
from plasma.quaternion import IDENTITY_QUAT, quat_multiply, quat_normalize
from plasma.gyro_bias import load_gyro_bias, save_gyro_bias
from plasma import nus_stream
from plasma.nus_stream import (
    StreamSession, ProtocolError, build_command, new_session_id,
    OP_START, OP_CANCEL, PROFILE, DEVICE_PPG,
    HANDSHAKE_TIMEOUT_S,
)
from plasma.ppg_ecg_records import decode_ppg, decode_ecg

yams_dir = __data_dir__

# --- ECG/PPG signal-quality-check (SQC) snapshot, via Nordic UART Service ---
# Protocol v1 bounded sensor stream — see
# local_docs/NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md (framing/handshake),
# local_docs/PPG_PACKED_16_BYTE_FORMAT.md and
# local_docs/ECG_TEMP_DATA_FORMAT.md (record layouts). One connected device is
# either a PPG or an ECG peripheral; each START pulls the fixed 96 KiB payload
# (history recorded just before START, then a forward window captured after).
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device (write)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host (notify)

SQC_MIN_MTU = 128
SQC_DEBUG = True  # emit per-notification telemetry (printed off the BLE thread)

# NOTE: the overall per-transaction backstop timeout stays DISABLED — the real
# BLE data rate is far slower than the handoff doc's provisional 35/45 s
# figures, so any fixed *duration* ceiling trips a healthy-but-slow transfer.
#
# What IS enforced is a NO-PROGRESS watchdog: if an active stream receives
# nothing at all for this long, the peripheral/link has wedged. Observed
# firmware failure: the ECG peripheral's three BLE TX buffers stayed occupied
# >72 s, outbound notifications failing -ENOMEM, while NAND recording and
# inbound writes (incl. reset) still worked. The watchdog CANCELs the stream,
# disconnects, and reconnects. Distinct from the duration cap: this fires only
# on true silence, never while bytes are still trickling in.
SQC_NOPROGRESS_TIMEOUT_S = 5.0
SQC_AUTO_RECONNECT = True

# Quick-mode capture: the protocol always sends the full 96 KiB (history +
# a live-acquired forward window). When the operator only wants the first N s
# for a contact check, we let START run, then write CANCEL once enough records
# have arrived and keep the partial payload. If the device doesn't answer the
# CANCEL with END within this grace period, finalize the partial locally and
# reconnect (the firmware stream may be wedged).
SQC_EARLY_CANCEL_GRACE_S = 3.0


class MotionSenseHRV(PlasmaDevice):
    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)

        self.device_list = device_config.get_active_msense_devices()
        self.imu_stream_devices = device_config.get_imu_stream_devices()
        # Name -> "Name (Nickname)" for UI panels; identifier stays the Name.
        self.display_labels = device_config.get_msense_display_labels()

        bias_by_addr = load_gyro_bias()

        self.memo = {}
        self.orientation_quat = {}
        self.gyro_bias = {}
        self.gyro_calib = {}
        self.sqc_state = {}
        # guards orientation_quat/gyro_calib/gyro_bias, mutated from both the
        # Gradio/main thread (start/stop/reset/calibrate) and the BLE notify
        # callback thread (imu_stream_handler)
        self._state_lock = threading.Lock()
        for k, addr in self.device_list.items():
            channels = ["ENMO", "counter"]
            if k in self.imu_stream_devices:
                channels += ["AccX", "AccY", "AccZ", "Q0", "Q1", "Q2", "Q3", "OrientX", "OrientY", "OrientZ", "OrientW"]
                self.orientation_quat[k] = IDENTITY_QUAT
                self.gyro_bias[k] = bias_by_addr.get(addr, (0.0, 0.0, 0.0))
            self.memo[k] = PlasmaMemo(k, channels=channels, label=self.display_labels.get(k, k))
            self.sqc_state[k] = self._new_sqc_state()

        # fallback reference in case data arrives before start() is clicked;
        # start() resets this to the true session-start time
        self.t_start = time.time()

        self.init_adapter()

        self.active_devices = {}
        self.active_outlets = {}

        self.scan_devices()
        self.connect_devices()

        # best-effort teardown if PLASMA exits without a clean disconnect — see
        # _shutdown_cleanup. Covers Ctrl-C / uncaught exception / normal exit
        # (and SIGTERM via the handler in plasma/__main__.py); NOT kill -9 or
        # host power loss (those fall back to the peripheral's BLE supervision
        # timeout).
        atexit.register(self._shutdown_cleanup)

    def _shutdown_cleanup(self):
        for name, p in list(getattr(self, "active_devices", {}).items()):
            try:
                st = self.sqc_state.get(name)
                sess = st.get("session") if st else None
                if sess is not None and not sess.is_terminal:
                    p.write_request(NUS_SERVICE_UUID, NUS_RX_CHAR_UUID,
                                    build_command(OP_CANCEL, sess.session_id))
            except Exception:
                pass
        try:
            self.disconnect()
        except Exception:
            pass

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
                try:
                    # bind n/p/name by default arg — otherwise every callback
                    # closes over the loop variable and fires with whichever
                    # device happened to be last when the loop finished
                    p.set_callback_on_connected(lambda n=n, p=p: self.info(f"{n} {p.identifier()} is connected"))
                    p.set_callback_on_disconnected(lambda nm=name: self._on_unexpected_disconnect(nm))
                    p.connect()
                    self.active_devices[name] = p
                    self.active_outlets[name] = MsenseOutlet(n, p)
                    try:
                        self.register_nus_notify(p, name)
                    except Exception as e:
                        self.info(f"NUS (ECG/PPG SQC) unavailable on {n}: {e}")
                except Exception as e:
                    self.info(f"Error connecting to {n}: {e}")
                    self.memo[name].sts = "⛔ connect failed"
                    self.active_devices.pop(name, None)
                    self.active_outlets.pop(name, None)
                    try:
                        if p.is_connected():
                            p.disconnect()
                    except Exception:
                        pass
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

        for name, p in list(self.active_devices.items()):
            print(name, p.is_connected(), p.is_connectable())
            try:
                self.collection_ctl(name, True)
                self.active_outlets[name].log_dir = self.log_dir
                self.memo[name].sts = "🟢"
            except Exception as e:
                self.info(f"Error starting {name}: {e}")
                self.memo[name].sts = "⚠️ start failed"

        self.ctl_state = "Collection in progress"

    def stop(self):
        gr.Info("🛑 Stop data collection...")
        self.info("Data collection stopped")
        for name, p in list(self.active_devices.items()):
            print(name, p.is_connected(), p.is_connectable())
            try:
                self.collection_ctl(name, False)
                self.memo[name].sts = "🛑"
            except Exception as e:
                self.info(f"Error stopping {name}: {e}")
                self.memo[name].sts = "⚠️ stop failed"

        self.ctl_state = "Collection stopped"

    def reset_orientation(self):
        with self._state_lock:
            for name in self.orientation_quat:
                self.orientation_quat[name] = IDENTITY_QUAT

    def _on_unexpected_disconnect(self, name):
        """Fired by simplepyble when a wristband drops BLE on its own
        (out of range, battery) — without this the UI never reflected an
        in-session disconnect until the next Stop press."""
        self.info(f"{name} disconnected unexpectedly")
        if name in self.memo:
            self.memo[name].sts = "🔌 disconnected"

    def disconnect(self):
        self._sqc_threads_stopped = True  # let the SQC watchdog loop exit
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
                with self._state_lock:
                    self.gyro_calib[name] = {"until": now + duration, "sum": [0.0, 0.0, 0.0], "n": 0}
                self.memo[name].sts = "🎯 Calibrating..."
                self.info(f"Started gyro bias calibration for {name} ({duration}s) — keep the wristband still")

    def _finish_gyro_calibration(self, name):
        with self._state_lock:
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

        if not peripheral.is_connected():
            raise RuntimeError(f"{name} is not connected (BLE link dropped)")

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

        # only (re-)subscribe on start; stop should just tell the firmware to
        # stop streaming, not stack another notify callback on top
        if start:
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
        # runs on the BLE library's callback thread — never let an exception
        # escape here, it would otherwise silently kill notifications for
        # this device with no visible status change
        try:
            ENMO = struct.unpack("<f", data[0:4])

            if len(data) == 8:
                packet_counter = struct.unpack("<I", data[4:8])
            elif len(data) == 6:
                packet_counter = struct.unpack("<H", data[4:6])
            else:
                self.info(f"Unexpected ENMO packet length {len(data)} from {name}, dropping")
                return

            self.active_outlets[name].push_sample([ENMO[0], packet_counter[0]])
            self.memo[name].set_latest(f"{ENMO[0]} {packet_counter[0]}")
            elapsed = time.time() - self.t_start
            self.memo[name].set_data("ENMO", ENMO[0], elapsed)
            self.memo[name].set_data("counter", packet_counter[0], elapsed)
        except Exception as e:
            self.info(f"Error handling ENMO packet from {name}: {e}")

    # demo feature: real-time accel + orientation, only on wristbands with the
    # demo firmware (see data/IMU_STREAM_BLE_CHARACTERISTIC.md)
    def register_imu_stream(self, peripheral, name):
        service_uuid = "da39c950-1d81-48e2-9c68-d0ae4bbd351f"
        characteristic_uuid = "da39c953-1d81-48e2-9c68-d0ae4bbd351f"
        peripheral.notify(service_uuid, characteristic_uuid, lambda data: self.imu_stream_handler(data, peripheral, name))

    def imu_stream_handler(self, data, peripheral, name):
        # runs on the BLE library's callback thread — never let an exception
        # escape here, it would otherwise silently kill notifications for
        # this device with no visible status change
        try:
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
                    with self._state_lock:
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
            with self._state_lock:
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
        except Exception as e:
            self.info(f"Error handling IMU stream packet from {name}: {e}")

    # ── ECG/PPG signal-quality-check (SQC) snapshot ─────────────────────────
    # NUS bounded sensor stream, protocol v1. Framing/handshake/validation
    # live in plasma/nus_stream.py; record decoding in
    # plasma/ppg_ecg_records.py. See local_docs/NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md.

    @staticmethod
    def _new_sqc_diag():
        # per-request notification telemetry, to answer "did the Central stop
        # draining, or did the link stop delivering?" — max_proc_ms large =>
        # our callback is the bottleneck; max_gap_s large with small
        # max_proc_ms => delivery stalled upstream of us.
        return {
            "count": 0, "bytes": 0, "first_t": None, "last_t": None,
            "last_gap_s": 0.0, "max_gap_s": 0.0, "max_proc_ms": 0.0,
            "last_seq": None, "seq_gaps": 0, "recoveries": 0,
            "mtu": None, "rssi": None,
            "log": deque(maxlen=64),  # (count, gap_s, proc_ms, seq)
        }

    @classmethod
    def _new_sqc_state(cls):
        return {
            "status": "idle",  # idle|requesting|receiving|ready|rejected|error
            "session": None,   # nus_stream.StreamSession
            "requested_at": None,
            "last_rx_at": None,
            "device_type": None,
            "provenance": None,
            "decoded": None,   # {"channels": {name: np.ndarray}, "fs": float, "tick": np.ndarray}
            "preview": None,   # throttled partial decode while receiving
            "saved_path": None,
            "error": None,
            "diag": cls._new_sqc_diag(),
            # quick mode
            "max_seconds": None,      # stop after this many seconds of signal
            "history_only": False,    # stop at the history->forward boundary
            "early_cancel_sent": False,
            "early_cancel_at": None,
            "partial": False,         # this capture was cut short on purpose
            "quick_seconds": None,    # seconds of signal actually kept
        }

    def register_nus_notify(self, peripheral, name):
        peripheral.notify(NUS_SERVICE_UUID, NUS_TX_CHAR_UUID,
                           lambda data: self._nus_data_handler(data, name))

    def get_sqc_devices(self):
        """Wristband names currently connected and eligible for an SQC snapshot request."""
        return list(self.active_devices.keys())

    def display_name(self, name):
        """UI label for a wristband: ``"Name (Nickname)"`` when a nickname is
        configured, else the bare Name. ``name`` stays the identifier."""
        return self.display_labels.get(name, name)

    def request_sqc_snapshot(self, name, max_seconds=None, history_only=False):
        """Pull a snapshot. max_seconds / history_only enable quick mode: the
        stream is CANCELled early and the partial payload kept (see
        _sqc_watchdog_loop)."""
        peripheral = self.active_devices.get(name)
        if peripheral is None or not peripheral.is_connected():
            return f"⛔ {name} not connected"

        state = self.sqc_state.setdefault(name, self._new_sqc_state())
        if state["status"] in ("requesting", "receiving", "finishing"):
            return f"⏳ {name} snapshot already in progress"

        if max_seconds is not None and max_seconds <= 0:
            max_seconds = None

        try:
            mtu = peripheral.mtu()
        except Exception:
            mtu = None
        if mtu is not None and mtu < SQC_MIN_MTU:
            state.update(status="error", error=f"ATT MTU {mtu} < {SQC_MIN_MTU} — reconnect")
            return f"⛔ {name}: MTU {mtu} < {SQC_MIN_MTU}"

        self._ensure_sqc_threads()
        try:
            rssi = peripheral.rssi()
        except Exception:
            rssi = None

        sid = new_session_id()
        diag = self._new_sqc_diag()
        diag.update(mtu=mtu, rssi=rssi)
        state.update(
            status="requesting", session=StreamSession(sid),
            requested_at=time.time(), last_rx_at=time.time(),
            device_type=None, provenance=None, decoded=None, preview=None,
            saved_path=None, error=None, diag=diag,
            max_seconds=max_seconds, history_only=bool(history_only),
            early_cancel_sent=False, early_cancel_at=None,
            partial=False, quick_seconds=None,
        )
        try:
            peripheral.write_request(NUS_SERVICE_UUID, NUS_RX_CHAR_UUID,
                                     build_command(OP_START, sid))
        except Exception as e:
            state.update(status="error", error=f"request failed: {e}")
            self.info(f"SQC START failed for {name}: {e}")
            return f"⛔ {name} request failed: {e}"

        mode = ("history-only" if history_only
                else f"quick {max_seconds:g}s" if max_seconds else "full")
        self.info(f"SQC START sent to {name} (session {sid:#010x}, mtu={mtu}, rssi={rssi}, {mode})")
        self._sqc_debug(name, f"START session={sid:#010x} mtu={mtu} rssi={rssi} mode={mode}")
        return f"📡 {name}: waiting for START_ACK…"

    # generous per-device ceiling for the sequential runner; real stalls are
    # caught much sooner by _sqc_watchdog_loop / the handshake timeout
    SQC_SEQ_PER_DEVICE_TIMEOUT_S = 180.0

    def request_all_sqc_snapshots(self, max_seconds=None, history_only=False):
        """Snapshot every connected wristband ONE AT A TIME. The Mac has a single
        BLE radio time-sliced across all connections, so running pulls in
        parallel makes each ~N× slower and multiplies stall risk — sequential is
        faster per device and easier to reason about. max_seconds / history_only
        pass through to quick mode."""
        names = self.get_sqc_devices()
        if not names:
            return "⛔ No MSense wristbands connected"
        if getattr(self, "_sqc_seq_thread", None) and self._sqc_seq_thread.is_alive():
            return "⏳ A snapshot run is already in progress"
        self._ensure_sqc_threads()
        self._sqc_seq_thread = threading.Thread(
            target=self._run_sqc_sequential, args=(names, max_seconds, bool(history_only)),
            name="sqc-sequential", daemon=True)
        self._sqc_seq_thread.start()
        mode = ("history-only" if history_only
                else f"quick {max_seconds:g}s" if max_seconds else "full")
        return f"📡 Snapshotting {len(names)} wristband(s) one at a time ({mode}): {', '.join(names)}"

    def _run_sqc_sequential(self, names, max_seconds=None, history_only=False):
        _TERMINAL = ("ready", "error", "rejected", "idle", "unavailable")
        for name in names:
            msg = self.request_sqc_snapshot(name, max_seconds=max_seconds,
                                            history_only=history_only)
            self.info(f"SQC sequential: {name} — {msg}")
            deadline = time.time() + self.SQC_SEQ_PER_DEVICE_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(0.5)
                # get_sqc_status() also evaluates the handshake timeout
                if self.get_sqc_status(name)["status"] in _TERMINAL:
                    break
            self.info(f"SQC sequential: {name} finished — "
                      f"{self.get_sqc_status(name)['status']}")
        self.info("SQC sequential: run complete")

    def cancel_sqc_snapshot(self, name):
        state = self.sqc_state.get(name)
        peripheral = self.active_devices.get(name)
        if not state or not state.get("session") or peripheral is None:
            return f"⛔ {name}: nothing to cancel"
        try:
            peripheral.write_request(NUS_SERVICE_UUID, NUS_RX_CHAR_UUID,
                                     build_command(OP_CANCEL, state["session"].session_id))
        except Exception as e:
            return f"⛔ {name} cancel failed: {e}"
        return f"✖ {name}: cancel sent"

    def cancel_all_sqc_snapshots(self):
        active = [n for n, s in self.sqc_state.items()
                  if s.get("status") in ("requesting", "receiving")]
        for name in active:
            self.cancel_sqc_snapshot(name)
        return f"✖ Cancel sent to {len(active)} wristband(s)" if active else "Nothing in progress"

    def _nus_data_handler(self, data, name):
        # runs on the BLE library's callback thread. Two hard rules:
        #  1. never let an exception escape (would kill notifications silently)
        #  2. never block on I/O here — a slow print/log/disk write on this
        #     thread stops us draining notifications, which (observed) wedges
        #     the peripheral's TX buffers. All logging goes through the async
        #     _sqc_debug queue; heavy work (decode/save) is deferred off-thread.
        t_entry = time.perf_counter()
        now = time.time()
        try:
            state = self.sqc_state.get(name)
            session = state["session"] if state else None

            diag = state["diag"] if state else None
            if diag is not None:
                gap = now - diag["last_t"] if diag["last_t"] else 0.0
                diag["last_t"] = now
                diag["count"] += 1
                diag["bytes"] += len(data)
                diag["last_gap_s"] = gap
                diag["max_gap_s"] = max(diag["max_gap_s"], gap)

            if session is None or session.is_terminal:
                self._sqc_debug(name, f"rx {len(data)}B ignored (session "
                                      f"{getattr(session, 'state', None)})")
                return  # unsolicited / late data — ignore
            state["last_rx_at"] = now

            try:
                events = session.feed(data)
            except ProtocolError as e:
                state.update(status="error", error=f"protocol violation: {e}")
                self._sqc_debug(name, f"  PROTOCOL VIOLATION: {e}")
                self.info(f"SQC protocol violation from {name}: {e}")
                return

            seq = None
            for kind, obj in events:
                if kind == "start_ack":
                    state["device_type"] = obj.device_type
                    state["status"] = "receiving"
                    self._sqc_debug(name, f"  START_ACK {obj.device_name_label} name={obj.device_name} "
                                          f"id={obj.device_id_hex} rate={obj.rate_hz:g}Hz "
                                          f"hist={obj.history_records} fwd={obj.forward_records} "
                                          f"commit={obj.git_commit[:10]} tree={obj.git_tree_state_label}")
                    self.info(
                        f"SQC START_ACK from {name}: {obj.device_name_label} "
                        f"{obj.device_name} id={obj.device_id_hex} commit={obj.git_commit[:10]}"
                    )
                elif kind == "data":
                    seq = obj.sequence
                    if diag is not None:
                        if diag["first_t"] is None:  # throughput clock starts at first DATA
                            diag["first_t"] = now
                            diag["bytes"] = len(data)
                            diag["count"] = 1
                        if diag["last_seq"] is not None and seq != diag["last_seq"] + 1:
                            diag["seq_gaps"] += 1
                        diag["last_seq"] = seq
                    self._sqc_debug(
                        name,
                        f"  DATA seq={obj.sequence} idx={obj.first_record_index} "
                        f"n={obj.record_count} phase={'fwd' if obj.phase else 'hist'} "
                        f"gap={diag['last_gap_s']:.2f}s -> "
                        f"{session.records_received}/{session.records_total} records "
                        f"({len(session.payload)}B)")
                elif kind == "result":
                    state.update(status="rejected", error=obj.status_name)
                    self._sqc_debug(name, f"  RESULT {obj.status_name} state={obj.peripheral_state_name}")
                    self.info(f"SQC rejected for {name}: {obj.status_name} "
                              f"(state {obj.peripheral_state_name})")
                elif kind == "end":
                    self._sqc_debug(
                        name,
                        f"  END {obj.status_name} hist={obj.history_records_sent} "
                        f"fwd={obj.forward_records_captured} bytes={obj.total_bytes_sent} "
                        f"data_msgs={obj.data_message_count} detail={obj.detail} "
                        f"-> session {session.state}")
                    # decode + file I/O off the BLE thread; "finishing" keeps
                    # the watchdog from treating the brief decode gap as a stall
                    if session.state == nus_stream.COMPLETE:
                        state["status"] = "finishing"
                        threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                         daemon=True).start()
                    elif session.state == nus_stream.CANCELLED and state.get("early_cancel_sent"):
                        state["status"] = "finishing"
                        threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                         kwargs={"partial": True}, daemon=True).start()
                    else:
                        state.update(status="error",
                                     error=f"{obj.status_name}: {session.error}")
                        self.info(f"SQC END non-success for {name}: {session.error}")

            if diag is not None:
                proc_ms = (time.perf_counter() - t_entry) * 1000.0
                diag["max_proc_ms"] = max(diag["max_proc_ms"], proc_ms)
                diag["log"].append((diag["count"], round(diag["last_gap_s"], 3),
                                    round(proc_ms, 1), seq))
        except Exception as e:
            self.info(f"Error handling SQC data from {name}: {e}")

    # ── async debug sink + no-progress watchdog ─────────────────────────────
    # class-level so a single printer / monitor thread serves every instance
    _dbg_q = queue.Queue(maxsize=4000)
    _dbg_thread = None
    _sqc_threads_lock = threading.Lock()

    def _ensure_sqc_threads(self):
        """Start the async debug printer and the no-progress watchdog once."""
        self._sqc_threads_stopped = False
        with MotionSenseHRV._sqc_threads_lock:
            if MotionSenseHRV._dbg_thread is None:
                MotionSenseHRV._dbg_thread = threading.Thread(
                    target=self._dbg_drain, name="sqc-debug", daemon=True)
                MotionSenseHRV._dbg_thread.start()
            if getattr(self, "_sqc_wd_thread", None) is None or not self._sqc_wd_thread.is_alive():
                self._sqc_wd_thread = threading.Thread(
                    target=self._sqc_watchdog_loop, name="sqc-watchdog", daemon=True)
                self._sqc_wd_thread.start()

    @classmethod
    def _dbg_drain(cls):
        while True:
            try:
                line = cls._dbg_q.get()
                print(line, flush=True)
            except Exception:
                pass

    def _sqc_debug(self, name, msg):
        if not SQC_DEBUG:
            return
        try:
            MotionSenseHRV._dbg_q.put_nowait(f"[SQC {name}] {msg}")
        except queue.Full:
            pass  # never block the BLE thread on a slow console

    def _sqc_diag_summary(self, name):
        d = self.sqc_state.get(name, {}).get("diag")
        if not d:
            return {}
        dur = (d["last_t"] - d["first_t"]) if d["first_t"] and d["last_t"] else 0.0
        kbps = (d["bytes"] / 1024.0 / dur) if dur > 0 else 0.0
        return {
            "notifs": d["count"],
            "kib_s": round(kbps, 1),
            "notif_s": round(d["count"] / dur, 1) if dur > 0 else 0.0,
            "mean_notif_bytes": round(d["bytes"] / d["count"]) if d["count"] else 0,
            "last_gap_s": round(d["last_gap_s"], 2),
            "max_gap_s": round(d["max_gap_s"], 2),
            "max_proc_ms": round(d["max_proc_ms"], 1),
            "seq_gaps": d["seq_gaps"],
            "recoveries": d["recoveries"],
            "mtu": d["mtu"],
            "rssi": d["rssi"],
        }

    def _sqc_watchdog_loop(self):
        """1 Hz supervisor for every active stream (runs off the BLE callback
        thread). Per stream, in priority order: quick-mode early terminate →
        quick-mode grace finalize → no-progress recovery."""
        while not getattr(self, "_sqc_threads_stopped", False):
            time.sleep(1.0)
            now = time.time()
            for name, state in list(self.sqc_state.items()):
                if state.get("status") != "receiving":
                    continue
                try:
                    if self._sqc_quick_check(name, state, now):
                        continue
                    last = state.get("last_rx_at") or state.get("requested_at") or now
                    if now - last > SQC_NOPROGRESS_TIMEOUT_S:
                        self._sqc_recover(name, f"no notification for {now - last:.1f}s")
                except Exception as e:
                    self.info(f"SQC watchdog error for {name}: {e}")

    def _sqc_quick_check(self, name, state, now):
        """Quick-mode handling. Returns True if it acted (skip no-progress)."""
        session = state.get("session")
        if session is None or session.start_ack is None:
            return False

        # (2) grace finalize — CANCEL sent but the device never answered with END
        if state.get("early_cancel_sent"):
            if now - (state.get("early_cancel_at") or now) > SQC_EARLY_CANCEL_GRACE_S:
                self._sqc_debug(name, "  quick: no END after CANCEL — finalizing partial locally")
                session.state = nus_stream.CANCELLED
                session.error = "quick mode: local finalize (no END from device)"
                state["status"] = "finishing"
                threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                 kwargs={"partial": True}, daemon=True).start()
                self._reconnect_peripheral(name, "quick-mode grace finalize")
            return True

        # (1) early terminate — enough signal has arrived
        if state.get("history_only"):
            target = session.start_ack.history_records
        elif state.get("max_seconds"):
            rate = session.start_ack.rate_hz or PROFILE[session.device_type]["rate_hz"]
            target = state["max_seconds"] * rate
        else:
            return False

        if session.records_received < target:
            return False

        peripheral = self.active_devices.get(name)
        if peripheral is None:
            return False
        try:
            peripheral.write_request(NUS_SERVICE_UUID, NUS_RX_CHAR_UUID,
                                     build_command(OP_CANCEL, session.session_id))
            state["early_cancel_sent"] = True
            state["early_cancel_at"] = now
            self._sqc_debug(
                name,
                f"  quick: {session.records_received} records ≥ target {target:.0f} "
                f"({'history-only' if state.get('history_only') else str(state.get('max_seconds')) + 's'})"
                f" — CANCEL sent")
            self.info(f"SQC quick mode: {name} CANCEL sent after "
                      f"{session.records_received} records")
        except Exception as e:
            self._sqc_debug(name, f"  quick: CANCEL write failed: {e}")
        return True

    def _reconnect_peripheral(self, name, reason):
        """disconnect → wait → connect → re-subscribe NUS (+ ENMO/IMU if a
        recording session is running). Runs on the watchdog thread."""
        if not SQC_AUTO_RECONNECT:
            return
        peripheral = self.active_devices.get(name)
        if peripheral is None:
            return
        try:
            peripheral.disconnect()
        except Exception as e:
            self._sqc_debug(name, f"  disconnect failed: {e}")
        time.sleep(1.5)
        try:
            peripheral.connect()
            self.register_nus_notify(peripheral, name)
            if getattr(self, "log_dir", None):
                try:
                    self.register_enmo(peripheral, name)
                except Exception as e:
                    self._sqc_debug(name, f"  ENMO re-subscribe failed: {e}")
                if name in self.imu_stream_devices:
                    try:
                        self.register_imu_stream(peripheral, name)
                    except Exception as e:
                        self._sqc_debug(name, f"  IMU re-subscribe failed: {e}")
            self.info(f"SQC: {name} reconnected + re-subscribed ({reason})")
            self._sqc_debug(name, "  reconnected + re-subscribed")
            if name in self.memo:
                self.memo[name].sts = "🔄 reconnected"
        except Exception as e:
            self.info(f"SQC: {name} reconnect FAILED: {e}")
            self._sqc_debug(name, f"  reconnect FAILED: {e}")
            if name in self.memo:
                self.memo[name].sts = "🔌 reconnect failed"

    def _sqc_recover(self, name, reason):
        """CANCEL a wedged stream, then disconnect + reconnect. Runs on the
        watchdog thread (never the BLE callback thread)."""
        state = self.sqc_state.get(name)
        if not state or state.get("status") != "receiving":
            return
        session = state.get("session")
        diag = state.get("diag") or {}
        diag["recoveries"] = diag.get("recoveries", 0) + 1
        summary = self._sqc_diag_summary(name)
        self.info(f"SQC watchdog: {name} stalled ({reason}) — CANCEL + reconnect. diag={summary}")
        self._sqc_debug(name, f"WATCHDOG stall: {reason}; diag={summary}")
        state.update(status="error",
                     error=f"stalled ({reason}) — reconnected, press Request to retry")
        if session and not session.is_terminal:
            session.state = nus_stream.FAILED
        if name in self.memo:
            self.memo[name].sts = "⚠️ stream stalled"

        peripheral = self.active_devices.get(name)
        if peripheral is None:
            return
        # inbound writes still work when TX is wedged, so try CANCEL first —
        # it lets the firmware tear down its stream thread and free TX slots
        if session is not None:
            try:
                peripheral.write_request(NUS_SERVICE_UUID, NUS_RX_CHAR_UUID,
                                         build_command(OP_CANCEL, session.session_id))
                self._sqc_debug(name, "  CANCEL written")
            except Exception as e:
                self._sqc_debug(name, f"  CANCEL write failed: {e}")
        self._reconnect_peripheral(name, f"stall: {reason}")

    _NON_CHANNEL_KEYS = ("fs", "tick", "rtc_tick", "crc_ok_frac", "oob_frac")

    def _finish_sqc_snapshot(self, name, partial=False):
        state = self.sqc_state[name]
        session = state["session"]
        payload = bytes(session.payload)
        fs = PROFILE[session.device_type]["rate_hz"]
        secs = round(session.records_received / fs, 2) if fs else 0.0

        provenance = session.provenance(
            requested_at=self._iso(state["requested_at"]),
            completed_at=self._iso(time.time()),
            host_version=__version__,
        )
        suffix = ""
        if partial:
            suffix = f"_p{secs:.0f}s"
            provenance.update(
                partial=True,
                seconds_captured=secs,
                records_captured=session.records_received,
                phase_at_cancel=session.phase_name,
                requested_max_seconds=state.get("max_seconds"),
                history_only=state.get("history_only", False),
            )
            state["partial"] = True
            state["quick_seconds"] = secs

        # save the raw payload + sidecar FIRST — a decode hiccup must never lose
        # a captured payload
        try:
            state["saved_path"] = self._save_sqc_capture(name, session, payload, provenance,
                                                         suffix=suffix)
            provenance["raw_file"] = os.path.basename(state["saved_path"])
        except Exception as e:
            self.info(f"SQC save failed for {name}: {e}")

        try:
            decoded = decode_ppg(payload) if session.device_type == DEVICE_PPG else decode_ecg(payload)
            channels = {k: v for k, v in decoded.items() if k not in self._NON_CHANNEL_KEYS}
            tick = decoded.get("tick", decoded.get("rtc_tick"))
            state["decoded"] = {"channels": channels, "fs": decoded["fs"], "tick": tick}
            for k in ("crc_ok_frac", "oob_frac"):
                if k in decoded:
                    provenance[k] = round(float(decoded[k]), 4)
            state["provenance"] = provenance
            state["status"] = "ready"

            n = len(next(iter(channels.values())))
            self.info(f"SQC {'partial ' if partial else ''}snapshot ready for {name}: {n} "
                      f"{PROFILE[session.device_type]['name']} records ({secs}s) → {state['saved_path']}")
        except Exception as e:
            state["provenance"] = provenance
            state.update(status="error", error=f"decode failed (raw saved): {e}")
            self.info(f"SQC decode failed for {name}: {e}")

    @staticmethod
    def _iso(epoch):
        return datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")

    def _save_sqc_capture(self, name, session, payload, provenance, suffix=""):
        """Persist the raw sensor payload + a provenance sidecar. Goes to the
        active session log dir when a recording is running, else data/sqc_snapshots/.
        `suffix` marks partial (quick-mode) captures on disk, e.g. "_p5s"."""
        ts = time.strftime("%y%m%d_%H%M%S")
        base = getattr(self, "log_dir", None) or os.path.join(yams_dir, "sqc_snapshots", ts)
        os.makedirs(base, exist_ok=True)

        safe = str(name).replace(":", "-").replace(" ", "_")
        ext = ".ppg" if session.device_type == DEVICE_PPG else ".ecg"
        stem = f"{safe}_{ts}{suffix}"
        raw_path = os.path.join(base, f"{stem}{ext}")
        with open(raw_path, "wb") as f:
            f.write(payload)
        with open(os.path.join(base, f"{stem}.json"), "w") as f:
            json.dump({**provenance, "raw_file": os.path.basename(raw_path)}, f, indent=2)
        return raw_path

    def get_sqc_status(self, name):
        state = self.sqc_state.get(name)
        if state is None:
            return {"status": "unavailable", "phase": None, "records_received": 0,
                    "records_total": 0, "provenance": None, "saved_path": None,
                    "error": None, "diag": {}}

        session = state["session"]
        now = time.time()
        # a missing START_ACK is not a wedged stream (the watchdog only recovers
        # streams that were progressing) — time it out here instead
        if state["status"] == "requesting" and now - state["requested_at"] > HANDSHAKE_TIMEOUT_S:
            state.update(status="error",
                         error=f"no START_ACK within {HANDSHAKE_TIMEOUT_S:.0f}s "
                               "(device recording? in BLE range?)")
        # stalls during "receiving" are handled by _sqc_watchdog_loop
        # (CANCEL + disconnect + reconnect), not here.

        return {
            "status": state["status"],
            "phase": session.phase_name if session else None,
            "records_received": session.records_received if session else 0,
            "records_total": session.records_total if session else 0,
            "provenance": state["provenance"],
            "saved_path": state["saved_path"],
            "error": state["error"],
            "diag": self._sqc_diag_summary(name),
        }

    def get_sqc_result(self, name):
        state = self.sqc_state.get(name)
        if state is None or state["status"] != "ready" or not state["decoded"]:
            return None
        d = state["decoded"]
        return {
            "device_type": PROFILE[state["device_type"]]["name"],
            "channels": d["channels"],
            "fs": d["fs"],
            "tick": d["tick"],
            "provenance": state["provenance"],
            "partial": False,               # not a live preview
            "quick_seconds": state.get("quick_seconds"),  # set if cut short on purpose
        }

    # min seconds between live-preview re-decodes (the transfer is slow, so a
    # coarse refresh is plenty and keeps the UI cheap)
    SQC_PREVIEW_MIN_INTERVAL_S = 1.0

    def get_sqc_preview(self, name):
        """Decode whatever DATA has arrived so far, so the tab can draw the
        signal while it's still streaming in. Same shape as get_sqc_result with
        partial=True. Returns None if there is nothing plottable yet."""
        state = self.sqc_state.get(name)
        if state is None or state["status"] != "receiving":
            return None
        session = state["session"]
        if session is None or session.device_type is None or len(session.payload) == 0:
            return None

        now = time.time()
        cached = state.get("preview")
        if cached is not None and cached["_len"] == len(session.payload):
            return cached["result"]
        if cached is not None and now - cached["_at"] < self.SQC_PREVIEW_MIN_INTERVAL_S:
            return cached["result"]

        try:
            payload = bytes(session.payload)
            decoded = decode_ppg(payload) if session.device_type == DEVICE_PPG else decode_ecg(payload)
        except Exception as e:
            self._sqc_debug(name, f"  preview decode failed: {e}")
            return cached["result"] if cached else None

        channels = {k: v for k, v in decoded.items() if k not in self._NON_CHANNEL_KEYS}
        result = {
            "device_type": PROFILE[session.device_type]["name"],
            "channels": channels,
            "fs": decoded["fs"],
            "tick": decoded.get("tick", decoded.get("rtc_tick")),
            "provenance": None,
            "partial": True,
        }
        state["preview"] = {"result": result, "_len": len(session.payload), "_at": now}
        return result


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