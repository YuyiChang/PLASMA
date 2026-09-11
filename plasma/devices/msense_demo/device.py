"""``MSenseDemo`` — a simulated MSense wristband.

It subclasses :class:`plasma.devices.msense.device.MotionSenseHRV` and overrides
only the three BLE seams (``_load_device_config``, ``scan_devices``,
``_make_client``). Everything else — the SQC engine, the no-progress watchdog,
``_reconnect_peripheral``, the LSL outlet, the journaler, the orientation math —
is inherited and runs unmodified against :class:`FakePeripheral`, a bleak-shaped
fake that generates synthetic notifications.
"""
import struct
import threading
import time

from plasma.devices.msense.device import (
    MotionSenseHRV, NUS_RX_CHAR_UUID, NUS_TX_CHAR_UUID, BATTERY_CHAR_UUID,
    CTL_ENC_CHAR_UUID,
)
from plasma.devices.msense.nus_stream import (
    MAGIC, OP_START, OP_STOP, OP_START_INFINITY,
)
from plasma.devices.msense_demo import config as _config, faults as _faults
from plasma.devices.msense_demo import fake_stream

# control / notify characteristics referenced by the base driver as string
# literals — named here for the fake's dispatch
CTL_STARTSTOP_CHAR_UUID = "da39c931-1d81-48e2-9c68-d0ae4bbd351f"
CTL_TIME_CHAR_UUID = "da39c932-1d81-48e2-9c68-d0ae4bbd351f"
ENMO_CHAR_UUID = "da39c951-1d81-48e2-9c68-d0ae4bbd351f"
IMU_STREAM_CHAR_UUID = "da39c953-1d81-48e2-9c68-d0ae4bbd351f"

# how long after collection starts a "disconnect_reconnect" fault drops the link
# (module-level so tests can shrink it)
DEMO_DISCONNECT_AFTER_S = 15.0


class MSenseDemo(MotionSenseHRV):
    CONFIG_KEY = "msense_demo"

    def _load_device_config(self):
        blob = self._read_blob()
        self.device_list = _config.active_devices(blob)        # addr -> Name
        self.imu_stream_devices = _config.imu_stream_devices(blob)   # {addr}
        self.display_labels = _config.display_labels(blob)     # addr -> label
        self._sensor_type = _config.sensor_types(blob)     # addr -> "PPG"/"ECG"
        self._demo_faults = _config.device_faults(blob)    # addr -> fault id
        self._demo_collecting = False

    def _read_blob(self):
        from plasma.config import device_config
        return device_config.get_plugin_config(self.CONFIG_KEY)

    def fault_for(self, addr):
        return self._demo_faults.get(addr, _faults.NO_FAULT)

    # ── BLE seams ───────────────────────────────────────────────────────────

    def scan_devices(self, filter_name="MSense"):
        """Build ``self.devices`` straight from config — no radio. A wristband
        with the ``device_not_found`` fault is left out, so the inherited
        ``connect_devices`` marks it '⛔ device not found'."""
        self.devices = {}
        for addr, blename in self.device_list.items():
            if self.fault_for(addr) == "device_not_found":
                continue
            self.devices[addr] = {
                "name": f"{blename} [{addr}]",
                "address": addr,
                "rssi": -55,
                "product": self._sensor_type.get(addr, "PPG"),
            }
        self.info(f"demo scan: {list(self.devices)}")

    def _make_client(self, addr, key):
        # the driver keys per-wristband state by address — the fake carries it
        # as its handle so fault_for() / _on_unexpected_disconnect() line up
        return FakePeripheral(self, key, addr)

    def _bluez_release_peer(self, addr, reason=""):
        # no real BlueZ stack behind the simulator
        pass

    # ── collection state (seen by the fake's live generators) ───────────────

    def start(self):
        self._demo_collecting = True
        super().start()

    def stop(self):
        self._demo_collecting = False
        super().stop()

    def trigger_disconnect(self, name=None):
        """Fire the disconnect half of a ``disconnect_reconnect`` fault now
        (otherwise it happens ~15 s into collection). The watchdog reconnects."""
        names = [name] if name else list(self.active_devices)
        for n in names:
            p = self.active_devices.get(n)
            if isinstance(p, FakePeripheral):
                p.drop_link()


class FakePeripheral:
    """Bleak-``BleakClient``-shaped fake. ``connect`` / ``disconnect`` /
    ``*_gatt_char`` / ``*_notify`` are coroutines run on the driver's dedicated
    BLE event loop via ``_run_async``; synthetic notifications are delivered
    from plain daemon threads, exactly as bleak delivers real ones."""

    def __init__(self, driver, name, address):
        self._driver = driver
        self._name = name
        self.address = address
        self.is_connected = False
        self.mtu_size = 247
        self.services = []

        self._notify = {}                 # char uuid -> callback(char, data)
        self._alive = False
        self._last_enc = 0
        self._sqc_cancel = threading.Event()
        self._sqc_thread = None
        self._live_thread = None
        self._disconnect_fired = False
        self._collect_started_at = None
        self._recording = False           # da39c931 state, read back by the driver
        self.writes = []                  # (char_uuid, bytes) — for tests

    # ---- BleakClient surface -------------------------------------------------

    async def connect(self):
        self.is_connected = True
        self._alive = True

    async def disconnect(self):
        self.is_connected = False
        self._alive = False
        self._sqc_cancel.set()

    async def start_notify(self, char_uuid, callback):
        self._notify[char_uuid] = callback
        # ENMO / IMU / battery notifications are driven by one live generator
        if char_uuid in (ENMO_CHAR_UUID, IMU_STREAM_CHAR_UUID, BATTERY_CHAR_UUID):
            self._ensure_live_thread()

    async def stop_notify(self, char_uuid):
        self._notify.pop(char_uuid, None)

    async def read_gatt_char(self, char_uuid):
        if char_uuid == BATTERY_CHAR_UUID:
            return bytes([self._battery_pct()])
        if char_uuid == CTL_ENC_CHAR_UUID:
            return struct.pack("<I", self._last_enc)
        if char_uuid == CTL_STARTSTOP_CHAR_UUID:
            return bytes([1 if self._recording else 0])
        return b"\x00"

    async def write_gatt_char(self, char_uuid, data, response=True):
        data = bytes(data)
        self.writes.append((char_uuid, data))
        if char_uuid == CTL_ENC_CHAR_UUID and len(data) >= 4:
            self._last_enc = struct.unpack("<I", data[:4])[0]
        elif char_uuid == CTL_STARTSTOP_CHAR_UUID and len(data) >= 1:
            on = data[0]  # v0: one-byte acquisition enable/disable
            if on:
                self._recording = True
                self._collect_started_at = time.time()
                self._ensure_live_thread()
            elif self.fault() != "acq_stop_ignored":
                self._recording = False
            # acq_stop_ignored: ACK the write but keep _recording True
        elif char_uuid == NUS_RX_CHAR_UUID:
            self._handle_nus_command(data)
        # time-sync / erase / anything else: accepted, ignored

    # ---- fault helpers -----------------------------------------------------

    def drop_link(self):
        """Simulate a spontaneous BLE drop — the inherited watchdog's
        auto-reconnect sweep then rebuilds the link via _make_client."""
        if not self.is_connected:
            return
        self._disconnect_fired = True
        self.is_connected = False
        self._alive = False
        self._sqc_cancel.set()
        try:
            self._driver._on_unexpected_disconnect(self._name, self)
        except Exception:
            pass

    # ---- synthetic streams -----------------------------------------------

    def _battery_pct(self):
        # slow drain from ~90, floor 40
        base = 90 - (time.time() % 3000) / 60.0
        return max(40, min(100, int(base)))

    def _ensure_live_thread(self):
        if self._live_thread and self._live_thread.is_alive():
            return
        self._live_thread = threading.Thread(
            target=self._live_loop, name=f"demo-live-{self._name}", daemon=True)
        self._live_thread.start()

    def _live_loop(self):
        """ENMO (1 Hz) + battery (~every 20 s) + IMU-stream (25 Hz), but only
        while the driver says a collection is running. Survives a reconnect: the
        new FakePeripheral starts its own loop when ENMO/IMU are re-subscribed
        and driver._demo_collecting is still True."""
        enmo_ctr = imu_ctr = 0
        t0 = time.time()
        next_enmo = next_batt = 0.0
        while self._alive:
            now = time.time() - t0
            if getattr(self._driver, "_demo_collecting", False):
                if now >= next_enmo:
                    next_enmo = now + 1.0
                    self._emit(ENMO_CHAR_UUID, fake_stream.enmo_packet(enmo_ctr))
                    enmo_ctr += 1
                if now >= next_batt:
                    next_batt = now + 20.0
                    self._emit(BATTERY_CHAR_UUID, fake_stream.battery_packet(self._battery_pct()))
                if self._name in getattr(self._driver, "imu_stream_devices", set()):
                    self._emit(IMU_STREAM_CHAR_UUID,
                               fake_stream.imu_stream_packet(imu_ctr, now))
                    imu_ctr += 1
                self._maybe_fire_disconnect()
            time.sleep(0.04)

    def _maybe_fire_disconnect(self):
        if (self._disconnect_fired
                or self.fault() != "disconnect_reconnect"
                or self._collect_started_at is None):
            return
        if time.time() - self._collect_started_at >= DEMO_DISCONNECT_AFTER_S:
            self.drop_link()

    def fault(self):
        return self._driver.fault_for(self._name)

    def _emit(self, char_uuid, data):
        cb = self._notify.get(char_uuid)
        if cb is None:
            return
        try:
            cb(char_uuid, data)
        except Exception:
            pass

    # ---- SQC ------------------------------------------------------------

    def _handle_nus_command(self, data):
        if len(data) < 8 or data[0:2] != MAGIC:
            return
        opcode = data[3]
        stream_id = struct.unpack("<I", data[4:8])[0]
        if opcode == OP_START:
            self._sqc_cancel.clear()
            self._sqc_thread = threading.Thread(
                target=self._sqc_loop, args=(stream_id,),
                name=f"demo-sqc-{self._name}", daemon=True)
            self._sqc_thread.start()
        elif opcode == OP_START_INFINITY:
            self._sqc_cancel.clear()
            self._sqc_thread = threading.Thread(
                target=self._infinity_loop, args=(stream_id,),
                name=f"demo-live-{self._name}", daemon=True)
            self._sqc_thread.start()
        elif opcode == OP_STOP:
            self._sqc_cancel.set()

    def _sqc_loop(self, stream_id):
        sensor = self._driver._sensor_type.get(self._name, "PPG")
        fault = self.fault()
        frames, terminal = fake_stream.sqc_frames(
            sensor, stream_id,
            fault=(fault if fault in ("sqc_error", "stream_stall") else None))

        time.sleep(0.05)   # handshake latency
        for i, frame in enumerate(frames):
            if self._sqc_cancel.is_set() or not self._alive:
                # user STOP mid-stream: answer with a STOPPED END
                self._emit(NUS_TX_CHAR_UUID,
                           fake_stream.nus_sim.end_message(
                               fake_stream.END_STOPPED, stream_id))
                return
            self._emit(NUS_TX_CHAR_UUID, frame)
            time.sleep(0.03 if i == 0 else 0.01)
        # terminal is False for the sqc_error/stream_stall faults: we just stop
        # here and let the driver's no-progress watchdog STOP + reconnect.

    def _infinity_loop(self, stream_id):
        sensor = self._driver._sensor_type.get(self._name, "PPG")
        fault = self.fault()
        gen = fake_stream.infinity_frame_iter(sensor, stream_id,
                                              stop_event=self._sqc_cancel)
        time.sleep(0.05)
        for i, frame in enumerate(gen):
            if not self._alive:
                return
            if fault == "stream_stall" and i > 6:
                return  # go silent -> watchdog fires
            self._emit(NUS_TX_CHAR_UUID, frame)
            time.sleep(0.03 if i == 0 else 0.008)
