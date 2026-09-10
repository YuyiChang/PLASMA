from plasma.devices.template import PlasmaDevice, PlasmaMemo
from bleak import BleakClient, BleakScanner
import asyncio
import concurrent.futures
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
from plasma.lsl_util import mark_plasma_origin
import numpy as np
import struct
from plasma import __version__
from plasma.config import device_config
from plasma.app_context import app_context
from .quaternion import IDENTITY_QUAT, quat_multiply, quat_normalize
from .gyro_bias import load_gyro_bias, save_gyro_bias
from . import nus_stream
from .nus_stream import (
    StreamSession, ProtocolError, build_command, new_stream_id,
    OP_START, OP_STOP, OP_START_INFINITY, PROFILE, ECG, PPG,
    MODE_FINITE, MODE_INFINITY, HANDSHAKE_TIMEOUT_S, HISTORY_BYTES,
)
from .records import (
    decode_ppg, decode_ecg, EcgBlockReassembler, Packed16Reassembler,
)

# NOTE: the *_SERVICE_UUID constants below are documentary only — bleak's
# read/write/notify calls are addressed by characteristic UUID alone (it
# looks up the owning service internally), so they're never passed as call
# arguments. Kept for readability/grouping and because external docs
# (BLE_PROTOCOL_OVERVIEW.md) reference them by name.

# --- ECG/PPG sensor stream (v0), via Nordic UART Service ---
# Shared sensor-stream protocol v0 — see
# docs/SENSOR_STREAM_CENTRAL_HOWTO.md (framing/handshake/decoders),
# docs/ECG_BLOCK_FORMAT.md (ECB2 blocks) and docs/PPG_PACKED_16_BYTE_FORMAT.md
# (PPG records). One connected device is either an ECG or a PPG peripheral
# (known from its advertised MSense4ECG / MSense4PPG name). A FINITE START
# pulls exactly 131,072 sensor bytes (32 KiB rolling history + 96 KiB future);
# START_INFINITY streams the history then future data continuously until STOP.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device (write)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host (notify)

# control service — start/stop, time sync, participant encoding, flash erase
CTL_SERVICE_UUID = "da39c930-1d81-48e2-9c68-d0ae4bbd351f"
CTL_STARTSTOP_CHAR_UUID = "da39c931-1d81-48e2-9c68-d0ae4bbd351f"  # acquisition enable (write 1/0, readable)
CTL_TIME_CHAR_UUID = "da39c932-1d81-48e2-9c68-d0ae4bbd351f"   # unix time sync (write)
CTL_ENC_CHAR_UUID = "da39c933-1d81-48e2-9c68-d0ae4bbd351f"   # participant encoding (write/read)
CTL_ERASE_CHAR_UUID = "da39c934-1d81-48e2-9c68-d0ae4bbd351f"  # write 68 -> full flash erase
ERASE_CODE = 68

# Acquisition-stop confirmation: after a stop write to CTL_STARTSTOP_CHAR_UUID
# the firmware's ATT ack only means "request accepted", not "recording halted"
# (SENSOR_STREAM_CENTRAL_HOWTO.md §1). Read the characteristic back — 0 means
# stopped, 1 means still recording — polling a few times so the firmware has a
# moment past the ack to settle.
ACQ_STOP_READBACK_POLLS = 3
ACQ_STOP_READBACK_INTERVAL_S = 0.4
ACQ_STOP_READ_TIMEOUT_S = 3.0

# standard Bluetooth SIG Battery Service
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

SQC_MIN_MTU = 128
SQC_DEBUG = True  # emit per-notification telemetry (printed off the BLE thread)

# NOTE: the overall per-transaction backstop timeout stays DISABLED — the real
# BLE data rate is far slower than the handoff doc's provisional 35/45 s
# figures, so any fixed *duration* ceiling trips a healthy-but-slow transfer.
#
# What IS enforced is a NO-PROGRESS watchdog: if an active stream receives
# nothing at all for this long, the peripheral/link has wedged. Observed
# firmware failure: the ECG peripheral's BLE TX buffers stayed occupied
# >72 s, outbound notifications failing -ENOMEM, while NAND recording and
# inbound writes (incl. reset) still worked. The watchdog STOPs the stream,
# disconnects, and reconnects. Distinct from the duration cap: this fires only
# on true silence, never while bytes are still trickling in. 15 s is the
# SENSOR_STREAM_CENTRAL_HOWTO.md §7 default.
SQC_NOPROGRESS_TIMEOUT_S = nus_stream.NOPROGRESS_TIMEOUT_S
SQC_AUTO_RECONNECT = True

# We used to run on simplepyble, whose connect()/disconnect() are blocking
# C++ calls with no timeout of their own. Confirmed via a macOS thread dump
# (sample <pid>) that a stuck connect() held the Python GIL hostage inside
# native code (an NSThread sleepForTimeInterval: retry loop) for the whole
# capture window — freezing the entire app (browser unresponsive, Ctrl-C
# inert), since nothing in Python, including a Thread.join(timeout), can
# make progress without the GIL. Replaced with `bleak`, an asyncio-native
# library (PyObjC on macOS, not a compiled blocking call) — its own docs
# note OS X connect-timeout handling was added specifically so "connect
# cannot hang forever". All BLE calls now run as coroutines on a dedicated
# event-loop thread (_start_ble_loop/_run_async below); BLE_OP_TIMEOUT_S
# bounds how long any caller waits for one.
BLE_OP_TIMEOUT_S = 15.0

# Quick-mode capture: a FINITE START sends 131,072 sensor bytes (32 KiB history
# + 96 KiB live-acquired future). When the operator only wants the first N s
# for a contact check, we let START run, then write STOP once enough bytes
# have arrived and keep the validated prefix. If the device doesn't answer the
# STOP with END within this grace period, finalize the partial locally and
# reconnect (the firmware stream may be wedged).
SQC_EARLY_CANCEL_GRACE_S = 3.0

# Live INFINITY view: how many seconds of decoded signal to retain in the
# in-memory rolling buffer behind the "Live stream" plot. Nothing is written
# to disk or LSL in this pass.
LIVE_WINDOW_S = 30.0


def _product_from_name(advertised_name):
    """ECG / PPG from an advertised local name (MSense4ECG-… / MSense4PPG-…).
    START_ACK carries no product identity in v0, so the central relies on
    this. Returns None when it can't tell."""
    n = (advertised_name or "").upper()
    if "MSENSE4ECG" in n or "4ECG" in n:
        return ECG
    if "MSENSE4PPG" in n or "4PPG" in n:
        return PPG
    return None


class AcquisitionStopNotConfirmed(RuntimeError):
    """A stop command was written to the acquisition-enable characteristic but
    reading it back still shows the device recording (after one retry)."""


class MotionSenseHRV(PlasmaDevice):
    # which plugin-config blob to read wristband records from — MSenseDemo
    # overrides this to point at its own "msense_demo" section.
    CONFIG_KEY = "msense"

    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)

        self._load_device_config()

        bias_by_addr = load_gyro_bias()

        self.memo = {}
        self.orientation_quat = {}
        self.gyro_bias = {}
        self.gyro_calib = {}
        self.sqc_state = {}
        # Every per-wristband dict below is keyed by the wristband's BLE
        # address (config.active_devices) — unique even when two wristbands
        # share a Name. `display_name(addr)` maps a key back to "Name (Nickname)"
        # for anything the operator sees.
        # addr -> live INFINITY-stream state (see _new_live_state); only ever
        # one NUS stream (SQC snapshot OR live) active per wristband at a time.
        self.live_state = {}
        # addr -> {"status": confirmed|unconfirmed|unverifiable|unknown,
        #          "checked_at": ts} — set by collection_ctl(addr, False), see
        # _confirm_acq_stopped. Cleared on the next collection_ctl(addr, True).
        self._acq_stop_status = {}
        # addresses with an open "[SQC] … start" journaler marker, awaiting an
        # "end" once the stream reaches a terminal status (see _sqc_watchdog_loop).
        self._sqc_journal_open = set()
        self.battery = {}          # addr -> last battery %
        # rssi at connect time — bleak has no live RSSI query on a connected
        # client (that's not a standard GATT op; RSSI only comes from
        # advertisement data during a scan), so we cache what the scan saw
        # and use that for the SQC diagnostic instead of a fresh per-request read
        self._connect_rssi = {}
        # what each wristband actually subscribed to — older firmware lacks NUS,
        # not every unit has the demo IMU-stream characteristic
        self.caps = {}             # addr -> {"nus": bool, "imu": bool, "battery": bool}
        self.auto_reconnect = True
        self._last_reconnect_sweep = 0.0
        # guards orientation_quat/gyro_calib/gyro_bias, mutated from both the
        # Gradio/main thread (start/stop/reset/calibrate) and the BLE notify
        # callback thread (imu_stream_handler)
        self._state_lock = threading.Lock()
        for addr, blename in self.device_list.items():
            channels = ["ENMO", "counter", "battery"]
            self.caps[addr] = {"nus": False, "imu": False, "battery": False,
                               "product": None, "acq_readback": False}
            groups = {}
            if addr in self.imu_stream_devices:
                channels += ["AccX", "AccY", "AccZ", "Q0", "Q1", "Q2", "Q3", "OrientX", "OrientY", "OrientZ", "OrientW"]
                groups = {
                    "Accel (g)": ["AccX", "AccY", "AccZ"],
                    "Quaternion Δ (per-frame)": ["Q0", "Q1", "Q2", "Q3"],
                    "Orientation (composed)": ["OrientX", "OrientY", "OrientZ", "OrientW"],
                }
                self.orientation_quat[addr] = IDENTITY_QUAT
                self.gyro_bias[addr] = bias_by_addr.get(addr, (0.0, 0.0, 0.0))
            self.memo[addr] = PlasmaMemo(addr, channels=channels,
                                         label=self.display_labels.get(addr, blename),
                                         channel_groups=groups)
            self.sqc_state[addr] = self._new_sqc_state()

        # fallback reference in case data arrives before start() is clicked;
        # start() resets this to the true session-start time
        self.t_start = time.time()

        self._start_ble_loop()

        self.active_devices = {}
        self.active_outlets = {}
        # (name, char_uuid) -> the BleakClient the notify is live on; used by
        # _ensure_notify to make (re-)subscribing idempotent across a
        # Start/Stop/Start cycle or a reconnect.
        self._notify_state = {}

        self.scan_devices()
        self.connect_devices()

        # best-effort teardown if PLASMA exits without a clean disconnect — see
        # _shutdown_cleanup. Covers Ctrl-C / uncaught exception / normal exit
        # (and SIGTERM via the handler in plasma/__main__.py); NOT kill -9 or
        # host power loss (those fall back to the peripheral's BLE supervision
        # timeout).
        atexit.register(self._shutdown_cleanup)

    def _load_device_config(self):
        """Read the plugin config blob into device_list / imu_stream_devices /
        display_labels. Split out of __init__ so MSenseDemo can read its own
        ``msense_demo`` blob instead of ``msense``."""
        from . import config as mcfg
        blob = device_config.get_plugin_config(self.CONFIG_KEY)
        self.device_list = mcfg.active_devices(blob)          # addr -> BLE Name
        self.imu_stream_devices = mcfg.imu_stream_devices(blob)   # {addr}
        # addr -> "Name (Nickname)" for anything the operator sees; the
        # per-wristband identifier is the address (see display_name()).
        self.display_labels = mcfg.display_labels(blob)

    def _make_client(self, addr, key):
        """The BLE client object for ``addr``. ``key`` is the per-wristband
        dict key (the address) the disconnect callback reports. A seam so
        MSenseDemo can hand back a fake peripheral."""
        return BleakClient(
            addr,
            disconnected_callback=lambda c, nm=key: self._on_unexpected_disconnect(nm),
        )

    def _shutdown_cleanup(self):
        for name, p in list(getattr(self, "active_devices", {}).items()):
            try:
                for store in (self.sqc_state, self.live_state):
                    st = store.get(name)
                    sess = st.get("session") if st else None
                    if sess is not None and not sess.is_terminal:
                        # short timeout here — this is the atexit/SIGTERM path,
                        # which should exit promptly rather than wait the full
                        # interactive BLE_OP_TIMEOUT_S
                        self._run_async(p.write_gatt_char(
                            NUS_RX_CHAR_UUID,
                            build_command(OP_STOP, sess.stream_id),
                            response=True), timeout_s=3.0)
            except Exception:
                pass
        try:
            self.disconnect()
        except Exception:
            pass
        try:
            self._stop_ble_loop()
        except Exception:
            pass

    def _start_ble_loop(self):
        """All bleak calls run as coroutines on this dedicated event-loop
        thread — never on the Gradio/watchdog/SQC-runner threads directly.
        See _run_async."""
        self._ble_loop = asyncio.new_event_loop()
        self._ble_loop_thread = threading.Thread(
            target=self._ble_loop.run_forever, name="ble-loop", daemon=True)
        self._ble_loop_thread.start()

    def _stop_ble_loop(self):
        loop = getattr(self, "_ble_loop", None)
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread = getattr(self, "_ble_loop_thread", None)
        if thread is not None:
            thread.join(timeout=2.0)

    def _run_async(self, coro, timeout_s=None):
        """Submit a coroutine to the dedicated BLE event-loop thread and wait
        up to timeout_s for it, from whatever thread calls this (Gradio
        thread, watchdog thread, an SQC runner thread). Returns the
        coroutine's result, or raises (its own exception, or TimeoutError).

        Unlike simplepyble's blocking calls (confirmed via a macOS thread
        dump to hold the GIL hostage inside native code, defeating any
        Python-level timeout), this wait is a plain threading
        condition-variable wait on a *different* thread than the one running
        the coroutine — it actually times out even if bleak itself is slow,
        because bleak's asyncio-native code yields the GIL cooperatively
        instead of blocking inside a native call."""
        timeout_s = BLE_OP_TIMEOUT_S if timeout_s is None else timeout_s
        future = asyncio.run_coroutine_threadsafe(coro, self._ble_loop)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"BLE op timed out after {timeout_s:.0f}s")

    def scan_devices(self, filter_name="MSense"):
        print("start scanning devices")
        self.info("start device scanning")
        self.ctl_state = "Start device scanning"
        try:
            found = self._run_async(BleakScanner.discover(timeout=5.0, return_adv=True),
                                    timeout_s=10.0)
        except Exception as e:
            self.info(f"scan failed: {e}")
            found = {}

        self.devices = {}
        for i, (addr, (ble_device, adv)) in enumerate(found.items()):
            identifier = ble_device.name or adv.local_name or ""
            if filter_name in identifier:
                addr_u = addr.upper()
                self.info(f"{i}: {identifier} [{addr_u}]")
                self.devices[addr_u] = {
                    "name": f"{identifier} [{addr_u}]",
                    "address": addr_u,
                    "rssi": adv.rssi,
                    "product": _product_from_name(identifier),
                }

        print(self.devices)
        self.info("device scanning completed")
        self.ctl_state = "Device scanning completed"

    def connect_devices(self):
        self.active_devices = {}
        self.active_outlets = {}
        self._notify_state = {}
        self.ctl_state = "Start device connection"

        # quick sanity check
        for addr, blename in self.device_list.items():
            self.info(f"Connecting to device {addr}")
            # assert addr in self.devices.keys(), self.info(f"Target device not found {addr}")

            if addr in self.devices.keys():
                dev = self.devices[addr]
                n = dev['name']

                self.info(f"Starting to connect to {n}")
                # gr.Info(f"Connecting to devices: {n}")
                print(f'==== {n}')
                print(f"=== {n} at {addr}")
                p = None
                try:
                    # bind nm by default arg — otherwise the callback closes
                    # over the loop variable and fires with whichever device
                    # happened to be last when the loop finished
                    p = self._make_client(addr, addr)
                    # bounded: bleak's connect() actually respects this
                    # timeout (unlike simplepyble's, confirmed via a macOS
                    # thread dump to hold the GIL hostage indefinitely) —
                    # don't let one bad device freeze the whole connect loop
                    self._run_async(p.connect())
                    self.info(f"{n} connected")
                    self.active_devices[addr] = p
                    self.active_outlets[addr] = MsenseOutlet(n, addr)
                    self._connect_rssi[addr] = dev.get("rssi")
                    self.caps[addr]["product"] = dev.get("product")
                    try:
                        self._ensure_mtu(p, addr)
                    except Exception as e:
                        self.info(f"{n}: MTU negotiation error: {e}")
                    try:
                        self.register_nus_notify(p, addr)
                        self.caps[addr]["nus"] = True
                    except Exception as e:
                        self.info(f"NUS (ECG/PPG SQC) unavailable on {n}: {e}")
                    # can we read the acquisition-enable char back? (used to
                    # confirm a collection stop — see _confirm_acq_stopped)
                    self.caps[addr]["acq_readback"] = (
                        self._read_acq_enabled(addr, p) is not None)
                    try:
                        raw = self._run_async(p.read_gatt_char(BATTERY_CHAR_UUID))
                        pct = raw[0]
                        self.battery[addr] = pct
                        self.caps[addr]["battery"] = True
                        self.memo[addr].set_latest(f"🔋 {pct}%")
                    except Exception as e:
                        self.info(f"battery read unavailable on {n}: {e}")
                except Exception as e:
                    self.info(f"Error connecting to {n}: {e}")
                    self.memo[addr].sts = "⛔ connect failed"
                    self.active_devices.pop(addr, None)
                    self.active_outlets.pop(addr, None)
                    try:
                        if p is not None and p.is_connected:
                            self._run_async(p.disconnect())
                    except Exception:
                        pass
            else:
                self.memo[addr].sts = "⛔ device not found"

        # run the 1 Hz supervisor as soon as anything is connected — it now also
        # does the auto-reconnect sweep, not just SQC stall recovery
        self._ensure_sqc_threads()

    def lsl_streams(self):
        # TODO(next): dedicated per-wristband ECG (512 Hz) / PPG (256 Hz) LSL
        # outlets for the continuous INFINITY stream, so it lands in the XDF
        # recording. This pass keeps INFINITY in-memory only (see
        # start_live_stream / get_live_stream_preview).
        return {o.stream_name: cfg for cfg, o in
                dict(getattr(self, "active_outlets", {})).items()
                if getattr(o, "use_lsl", False)}

    def _resolve_log_dir(self):
        """Where MSense .txt / SQC captures go. Prefer the unified session dir
        injected by IntegratedPanel.start_collection(); fall back to a
        self-computed sibling dir when started outside the panel."""
        sdir = getattr(self, "session_dir", None)
        if sdir:
            return os.path.join(sdir, "msense")
        timestamp = time.strftime("%y%m%d_%H%M")  # legacy minute precision
        return os.path.join(app_context().data_dir,
                            self.session_info['sub_id'],
                            self.session_info['ses_id'],
                            f"{self.session_info['participant_enc']}_{timestamp}")

    def start(self):
        self.log_dir = self._resolve_log_dir()
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
            print(name, p.is_connected)
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
        try:
            self.stop_all_live_streams()
        except Exception as e:
            self.info(f"Error stopping live streams: {e}")
        for name, p in list(self.active_devices.items()):
            disp = self.display_name(name)
            print(name, p.is_connected)
            try:
                self.collection_ctl(name, False)
                confirmed = self._acq_stop_status.get(name, {}).get("status") == "confirmed"
                self.memo[name].sts = "🛑 stopped" if confirmed else "🛑"
                self.journal(f"[ACQ] {disp} stop "
                             + ("confirmed" if confirmed
                                else "not verifiable (da39c931 unreadable)"))
            except AcquisitionStopNotConfirmed as e:
                self.info(str(e))
                self.memo[name].sts = "⚠️ still recording — stop unconfirmed"
                self.journal(f"[ACQ] {disp} stop UNCONFIRMED (da39c931 still 1 after retry)")
            except Exception as e:
                self.info(f"Error stopping {disp}: {e}")
                self.memo[name].sts = "⚠️ stop failed"

        self.ctl_state = "Collection stopped"

    def reset_orientation(self):
        with self._state_lock:
            for name in self.orientation_quat:
                self.orientation_quat[name] = IDENTITY_QUAT

    def _on_unexpected_disconnect(self, name):
        """Fired by bleak when a wristband drops BLE on its own (out of
        range, battery) — without this the UI never reflected an in-session
        disconnect until the next Stop press."""
        self.info(f"{self.display_name(name)} disconnected unexpectedly")
        if name in self.memo:
            self.memo[name].sts = "🔌 disconnected"

    def disconnect(self):
        self._journal_finished_sqc(reason="disconnected")  # close any open SQC markers
        try:
            self.stop_all_live_streams()
        except Exception:
            pass
        self._sqc_threads_stopped = True  # let the SQC watchdog loop exit
        for name, p in list(self.active_devices.items()):
            try:
                if p.is_connected:
                    self._run_async(p.disconnect())
            except Exception as e:
                self.info(f"Error disconnecting {name}: {e}")
        self.active_devices = {}
        self.active_outlets = {}
        self._notify_state = {}

    # ── manual controls (surfaced in the MSense > Control sub-tab) ───────────

    def reconnect_all(self):
        """One-shot reconnect of any dropped wristband."""
        n = 0
        for name, p in list(self.active_devices.items()):
            try:
                if not p.is_connected:
                    self._reconnect_peripheral(name, "manual reconnect")
                    n += 1
            except Exception as e:
                self.info(f"reconnect {name} failed: {e}")
        return f"Reconnect attempted on {n} wristband(s)" if n else "All wristbands connected"

    def erase_flash_data(self, passcode):
        """Full on-device flash erase — gated on the fixed passcode 68.

        A single unsigned byte `68` to the reset characteristic on the
        control service (confirmed against firmware: a 4-byte write, as an
        earlier ported version of this code sent, is rejected by the
        peripheral's GATT server with CBATTErrorInvalidAttributeValueLength —
        this characteristic expects exactly 1 byte). The device wipes NAND
        and resets, dropping the BLE link on its own — we just clear our
        device dicts here rather than force-disconnecting mid-reset.
        """
        try:
            code = int(passcode)
        except (TypeError, ValueError):
            code = None
        if code != ERASE_CODE:
            return "⛔ wrong erase code"

        done, failed = 0, []
        for name, p in list(self.active_devices.items()):
            disp = self.display_name(name)
            try:
                self._run_async(p.write_gatt_char(CTL_ERASE_CHAR_UUID,
                                                   struct.pack("<B", ERASE_CODE),
                                                   response=True))
                self.memo[name].sts = "🧨 erased — re-Initialize"
                done += 1
            except Exception as e:
                self.info(f"erase write failed on {disp}: {e}")
                failed.append(f"{disp}: {e}")

        self.info(f"Flash erase issued to {done} wristband(s); failed: {failed}")
        self._journal_finished_sqc(reason="disconnected")
        self._sqc_threads_stopped = True        # let the watchdog loop exit
        self.active_devices = {}
        self.active_outlets = {}
        self._notify_state = {}

        if done:
            msg = (f"🧨 erase issued to {done} wristband(s) — wait for the lights out, "
                   f"then re-Initialize on the Session dashboard")
        else:
            msg = "⚠️ erase failed on every wristband"
        if failed:
            msg += "\n" + "\n".join(f"- {f}" for f in failed)
        return msg

    def get_services(self):
        """A text dump of every GATT service/characteristic on each connected wristband."""
        if not self.active_devices:
            return "No wristband connected."
        out = []
        for name, p in list(self.active_devices.items()):
            out.append(f"### {self.display_name(name)}")
            try:
                for service in p.services:
                    for ch in service.characteristics:
                        out.append(f"- `{service.uuid}` / `{ch.uuid}`")
            except Exception as e:
                out.append(f"  (error: {e})")
        return "\n".join(out)

    def write_enc(self, enc):
        """Write an arbitrary participant-encoding int to every wristband and read it back."""
        try:
            val = int(enc)
        except (TypeError, ValueError):
            return "⛔ enter an integer"
        lines = []
        for name, p in list(self.active_devices.items()):
            disp = self.display_name(name)
            try:
                self._run_async(p.write_gatt_char(CTL_ENC_CHAR_UUID, struct.pack("<I", val), response=True))
                back = struct.unpack("<I", self._run_async(p.read_gatt_char(CTL_ENC_CHAR_UUID)))[0]
                lines.append(f"{disp}: wrote {val}, read back {back}")
            except Exception as e:
                lines.append(f"{disp}: {e}")
        return "\n".join(lines) or "No wristband connected."

    def start_gyro_calibration(self, duration=3.0):
        now = time.time()
        for addr in self.imu_stream_devices:
            if addr in self.active_devices:
                with self._state_lock:
                    self.gyro_calib[addr] = {"until": now + duration, "sum": [0.0, 0.0, 0.0], "n": 0}
                self.memo[addr].sts = "🎯 Calibrating..."
                self.info(f"Started gyro bias calibration for {self.display_name(addr)} "
                          f"({duration}s) — keep the wristband still")

    def _finish_gyro_calibration(self, addr):
        with self._state_lock:
            calib = self.gyro_calib.pop(addr, None)
            if calib is None or calib["n"] == 0:
                return
            bias = tuple(s / calib["n"] for s in calib["sum"])
            self.gyro_bias[addr] = bias
        if addr:
            save_gyro_bias(addr, bias, calib["n"])
        self.memo[addr].sts = "✅ Bias saved"
        self.info(f"Gyro bias calibrated for {self.display_name(addr)}: {bias} (n={calib['n']})")

    def _write_acq_enable(self, peripheral, on):
        self._run_async(peripheral.write_gatt_char(
            CTL_STARTSTOP_CHAR_UUID, struct.pack("<B", int(bool(on))), response=True))

    def _read_acq_enabled(self, name, peripheral):
        """Read the acquisition-enable characteristic. Returns 0 / 1, or None
        when it can't be read (older firmware without a readable char, or a
        transient BLE error)."""
        try:
            raw = self._run_async(peripheral.read_gatt_char(CTL_STARTSTOP_CHAR_UUID),
                                  timeout_s=ACQ_STOP_READ_TIMEOUT_S)
            return int(raw[0]) if raw else None
        except Exception as e:
            self._sqc_debug(name, f"da39c931 readback failed: {e}")
            return None

    def _confirm_acq_stopped(self, name, peripheral):
        """Poll the readback after a stop write. Returns True (read back 0 —
        stopped), False (read back 1 on every poll — still recording), or None
        (never got a readable value — unverifiable)."""
        saw_value = False
        for i in range(ACQ_STOP_READBACK_POLLS):
            if i:
                time.sleep(ACQ_STOP_READBACK_INTERVAL_S)
            val = self._read_acq_enabled(name, peripheral)
            if val is None:
                continue
            saw_value = True
            if val == 0:
                return True
        return False if saw_value else None

    def collection_ctl(self, name, start=True):
        peripheral = self.active_devices[name]

        if not peripheral.is_connected:
            raise RuntimeError(f"{name} is not connected (BLE link dropped)")

        # if starting, do the initialization
        if start:
            self._acq_stop_status.pop(name, None)
            # write unix time
            self._run_async(peripheral.write_gatt_char(
                CTL_TIME_CHAR_UUID,
                struct.pack("<Q", int(time.time())), response=True))
            # write participant hash
            self.participant_byte = struct.pack("<I", self.session_info['participant_enc'])
            self._run_async(peripheral.write_gatt_char(
                CTL_ENC_CHAR_UUID,
                self.participant_byte, response=True))

        # acquisition enable/disable — one byte with response: 1 requests
        # acquisition, 0 requests a normal stop (SENSOR_STREAM_CENTRAL_HOWTO.md
        # §1, ECG_BLOCK_FORMAT.md §20). This is separate from the NUS stream
        # START/STOP — stream STOP leaves acquisition + NAND recording running.
        self._write_acq_enable(peripheral, start)

        # only (re-)subscribe on start; stop should just tell the firmware to
        # stop streaming, not stack another notify callback on top
        if start:
            self.register_enmo(peripheral, name)
            try:
                self.register_battery(peripheral, name)
            except Exception as e:
                self.info(f"battery notify unavailable on {name}: {e}")

            if name in self.imu_stream_devices:
                try:
                    self.register_imu_stream(peripheral, name)
                    self.caps[name]["imu"] = True
                except Exception as e:
                    self.info(f"IMU stream unavailable on {name} (demo firmware not present?): {e}")
            return

        # stop: the ATT ack only means "request accepted" — read da39c931 back
        # to confirm the firmware actually halted acquisition. One retry.
        ok = self._confirm_acq_stopped(name, peripheral)
        if ok is False:
            self._sqc_debug(name, "acq stop not confirmed — retrying stop write")
            try:
                self._write_acq_enable(peripheral, False)
            except Exception as e:
                self._sqc_debug(name, f"acq stop retry write failed: {e}")
            ok = self._confirm_acq_stopped(name, peripheral)
        self._acq_stop_status[name] = {
            "status": {True: "confirmed", False: "unconfirmed", None: "unverifiable"}[ok],
            "checked_at": time.time(),
        }
        if ok is False:
            raise AcquisitionStopNotConfirmed(
                f"{name}: da39c931 still reads 1 after stop + retry — "
                "device may still be recording")

    def get_acq_stop_status(self, name):
        """{"status": confirmed|unconfirmed|unverifiable|unknown, "checked_at": ts|None}
        — the outcome of the last collection stop's da39c931 readback."""
        return self._acq_stop_status.get(name, {"status": "unknown", "checked_at": None})

    def _ensure_notify(self, peripheral, name, char_uuid, handler):
        """Idempotent start_notify. A Start/Stop/Start cycle (stop doesn't
        unsubscribe, by design) or a reconnect-then-Start would otherwise call
        start_notify twice on the same client — which CoreBluetooth rejects
        with 'Characteristic notifications already started'. Keyed by the live
        client object, so a fresh BleakClient after a reconnect still
        re-subscribes."""
        if getattr(self, "_notify_state", None) is None:
            self._notify_state = {}
        key = (name, char_uuid)
        if self._notify_state.get(key) is peripheral:
            return
        # bounded — bleak's start_notify actually respects this timeout (unlike
        # simplepyble's notify(), confirmed via a macOS thread dump to hold the
        # GIL hostage indefinitely against a marginal link).
        self._run_async(peripheral.start_notify(char_uuid, handler))
        self._notify_state[key] = peripheral

    def _ensure_mtu(self, peripheral, name):
        """Force an ATT MTU exchange on the Linux/BlueZ backend.

        macOS (CoreBluetooth) and Windows (WinRT) report the OS-negotiated MTU
        automatically. bleak's BlueZ backend never negotiates on its own —
        ``mtu_size`` stays at the 23-byte ATT default (with a warning) until
        ``_acquire_mtu()`` is called once, which does a throwaway
        ``AcquireWrite`` / ``AcquireNotify`` on a characteristic purely to read
        the negotiated MTU. Without it every SQC request fails the
        ``>= SQC_MIN_MTU`` check as "ATT MTU 23 < 128 — reconnect".

        Best-effort: a no-op on non-BlueZ backends, and harmless when the
        adapter genuinely can't go above 23 (old dongle without LL data-length
        extension) or the char needs bonding first. Runs on the caller's
        thread (connect loop / watchdog), never the BLE event-loop thread.
        """
        backend = getattr(peripheral, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is None:
            return  # CoreBluetooth / WinRT — the OS already negotiated

        if getattr(backend, "_mtu_size", None):
            return  # already acquired on this client
        try:
            self._run_async(acquire(), timeout_s=5.0)
        except Exception as e:
            self._sqc_debug(name, f"  _acquire_mtu failed: {e}")

        mtu = getattr(backend, "_mtu_size", None)
        self._sqc_debug(name, f"  ATT MTU after negotiation = {mtu}")
        if not mtu or mtu < SQC_MIN_MTU:
            self.info(f"{name}: ATT MTU {mtu} (< {SQC_MIN_MTU}) after negotiation "
                      f"— the Bluetooth adapter/BlueZ may not support a larger "
                      f"MTU; SQC streaming will be refused")
        else:
            self.info(f"{name}: ATT MTU negotiated to {mtu}")

    def register_enmo(self, peripheral, name):
        self._ensure_notify(peripheral, name, "da39c951-1d81-48e2-9c68-d0ae4bbd351f",
                            lambda ch, data: self.enmo_handler(data, name))

    def enmo_handler(self, data, name):
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

    def register_battery(self, peripheral, name):
        self._ensure_notify(peripheral, name, BATTERY_CHAR_UUID,
                            lambda ch, data: self.battery_handler(data, name))

    def battery_handler(self, data, name):
        # runs on the BLE callback thread — never let an exception escape
        try:
            pct = int(data[0])
            self.battery[name] = pct
            self.caps[name]["battery"] = True
            self.memo[name].set_data("battery", pct, time.time() - self.t_start)
            self.memo[name].set_latest(f"🔋 {pct}%")
        except Exception as e:
            self.info(f"Error handling battery packet from {name}: {e}")

    # demo feature: real-time accel + orientation, only on wristbands with the
    # demo firmware (see data/IMU_STREAM_BLE_CHARACTERISTIC.md)
    def register_imu_stream(self, peripheral, name):
        self._ensure_notify(peripheral, name, "da39c953-1d81-48e2-9c68-d0ae4bbd351f",
                            lambda ch, data: self.imu_stream_handler(data, name))

    def imu_stream_handler(self, data, name):
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

    # ── ECG/PPG sensor stream (v0) ─────────────────────────────────────────
    # Framing / handshake / byte-offset reassembly live in
    # plasma/devices/msense/nus_stream.py; ECB2 + packed-16 decoding in
    # plasma/devices/msense/records.py. See docs/SENSOR_STREAM_CENTRAL_HOWTO.md.

    @staticmethod
    def _new_sqc_diag():
        # per-request notification telemetry, to answer "did the Central stop
        # draining, or did the link stop delivering?" — max_proc_ms large =>
        # our callback is the bottleneck; max_gap_s large with small
        # max_proc_ms => delivery stalled upstream of us.
        return {
            "count": 0, "bytes": 0, "first_t": None, "last_t": None,
            "last_gap_s": 0.0, "max_gap_s": 0.0, "max_proc_ms": 0.0,
            "offset": 0, "skipped_history_slots": 0, "recoveries": 0,
            "mtu": None, "rssi": None,
            "log": deque(maxlen=64),  # (count, gap_s, proc_ms, offset)
        }

    @classmethod
    def _new_sqc_state(cls):
        return {
            "status": "idle",  # idle|requesting|receiving|finishing|ready|rejected|error
            "session": None,   # nus_stream.StreamSession (FINITE — accumulates payload)
            "requested_at": None,
            "last_rx_at": None,
            "product": None,   # "ECG" | "PPG"
            "provenance": None,
            "decoded": None,   # {"channels": {name: np.ndarray}, "fs": float, ...}
            "preview": None,   # throttled partial decode while receiving
            "saved_path": None,
            "error": None,
            "diag": cls._new_sqc_diag(),
            # quick mode
            "max_seconds": None,      # stop after this many seconds of signal
            "history_only": False,    # stop once the 32 KiB history is through
            "early_cancel_sent": False,
            "early_cancel_at": None,
            "partial": False,         # this capture was cut short (on purpose or not)
            "quick_seconds": None,    # seconds of signal actually kept
            "warning": None,          # non-blocking note on why it's incomplete, if any
        }

    @classmethod
    def _new_live_state(cls):
        return {
            "status": "idle",  # idle|requesting|streaming|stopping|stopped|error
            "session": None,
            "reassembler": None,
            "product": None,
            "requested_at": None,
            "last_rx_at": None,
            "ring": None,      # {channel: collections.deque}  (LIVE_WINDOW_S wide)
            "fs": None,
            "error": None,
            "diag": cls._new_sqc_diag(),
        }

    def register_nus_notify(self, peripheral, name):
        self._ensure_notify(peripheral, name, NUS_TX_CHAR_UUID,
                            lambda ch, data: self._nus_data_handler(data, name))

    def get_sqc_devices(self):
        """Wristband names currently connected and eligible for an SQC snapshot
        request — i.e. those whose NUS characteristic actually subscribed
        (older firmware without NUS is silently skipped)."""
        return [n for n in self.active_devices if self.caps.get(n, {}).get("nus")]

    def caps_summary(self):
        """One-line 'NUS on 2/2 · IMU on 0/2 · battery on 2/2' over connected wristbands."""
        conn = list(self.active_devices)
        if not conn:
            return ""
        parts = []
        for cap in ("nus", "imu", "battery"):
            on = sum(1 for n in conn if self.caps.get(n, {}).get(cap))
            parts.append(f"{cap.upper() if cap == 'nus' else cap.capitalize()} on {on}/{len(conn)}")
        missing_nus = [self.display_name(n) for n in conn
                       if not self.caps.get(n, {}).get("nus")]
        s = " · ".join(parts)
        if missing_nus:
            s += f" — NUS unavailable on: {', '.join(missing_nus)}"
        return s

    def display_name(self, name):
        """UI label for a wristband: ``"Name (Nickname)"`` when a nickname is
        configured, else the bare Name. The per-wristband key (a BLE address)
        stays the identifier — this is display only."""
        return getattr(self, "display_labels", {}).get(name, name)

    def _new_reassembler(self, product):
        return EcgBlockReassembler() if product == ECG else Packed16Reassembler()

    def _sqc_product(self, name):
        """The wristband's product ("ECG"/"PPG"), from its advertised name."""
        return self.caps.get(name, {}).get("product")

    def _check_stream_mtu(self, peripheral, name):
        """Returns (mtu, error_string_or_None). A last-chance BlueZ exchange is
        attempted if the first read is below the minimum."""
        try:
            mtu = peripheral.mtu_size
        except Exception:
            mtu = None
        if mtu is not None and mtu < SQC_MIN_MTU:
            try:
                self._ensure_mtu(peripheral, name)
                mtu = peripheral.mtu_size
            except Exception:
                pass
        if mtu is not None and mtu < SQC_MIN_MTU:
            return mtu, f"ATT MTU {mtu} < {SQC_MIN_MTU} — reconnect"
        return mtu, None

    def request_sqc_snapshot(self, name, max_seconds=None, history_only=False):
        """Pull a FINITE snapshot. max_seconds / history_only enable quick mode:
        the stream is STOPped early and the validated prefix kept (see
        _sqc_watchdog_loop)."""
        disp = self.display_name(name)
        peripheral = self.active_devices.get(name)
        if peripheral is None or not peripheral.is_connected:
            return f"⛔ {disp} not connected"

        product = self._sqc_product(name)
        if product is None:
            return f"⛔ {disp}: unknown product (not an MSense4ECG / MSense4PPG?)"

        state = self.sqc_state.setdefault(name, self._new_sqc_state())
        if state["status"] in ("requesting", "receiving", "finishing"):
            return f"⏳ {disp} snapshot already in progress"
        live = self.live_state.get(name)
        if live and live["status"] in ("requesting", "streaming", "stopping"):
            return f"⏳ {disp} live stream running — stop it first"

        if max_seconds is not None and max_seconds <= 0:
            max_seconds = None

        mtu, mtu_err = self._check_stream_mtu(peripheral, name)
        if mtu_err:
            state.update(status="error", error=mtu_err)
            return f"⛔ {disp}: {mtu_err}"

        self._ensure_sqc_threads()
        rssi = self._connect_rssi.get(name)

        sid = new_stream_id()
        diag = self._new_sqc_diag()
        diag.update(mtu=mtu, rssi=rssi)
        state.update(
            status="requesting",
            session=StreamSession(sid, product=product, expect_mode=MODE_FINITE),
            requested_at=time.time(), last_rx_at=time.time(),
            product=product, provenance=None, decoded=None, preview=None,
            saved_path=None, error=None, diag=diag,
            max_seconds=max_seconds, history_only=bool(history_only),
            early_cancel_sent=False, early_cancel_at=None,
            partial=False, quick_seconds=None, warning=None,
        )
        try:
            self._run_async(peripheral.write_gatt_char(NUS_RX_CHAR_UUID,
                                                        build_command(OP_START, sid), response=True))
        except Exception as e:
            state.update(status="error", error=f"request failed: {e}")
            self.info(f"SQC START failed for {disp}: {e}")
            return f"⛔ {disp} request failed: {e}"

        mode = ("history-only" if history_only
                else f"quick {max_seconds:g}s" if max_seconds else "full")
        self.info(f"SQC START sent to {disp} ({product}, stream {sid:#010x}, mtu={mtu}, "
                  f"rssi={rssi} @connect, {mode})")
        self._sqc_debug(name, f"START stream={sid:#010x} {product} mtu={mtu} rssi={rssi}@connect mode={mode}")
        if name in self._sqc_journal_open:   # prior run's marker never closed
            self.journal(f"[SQC] {disp} end (superseded)")
        self._sqc_journal_open.add(name)
        self.journal(f"[SQC] {disp} start ({mode})")
        return f"📡 {disp}: waiting for START_ACK…"

    # terminal statuses shared by every SQC runner's completion polling and by
    # the hybrid history/forward handoff predicate below
    _SQC_TERMINAL_STATUSES = ("ready", "error", "rejected", "idle", "unavailable")

    # generous per-device ceiling for a run's completion polling (shared by
    # all three runners' tails, not just sequential's); real stalls are
    # caught much sooner by _sqc_watchdog_loop / the handshake timeout
    SQC_SEQ_PER_DEVICE_TIMEOUT_S = 180.0

    # hybrid mode: how long to wait for one device's history phase to finish
    # before giving up and starting the next device anyway. The history burst
    # is normally ~5-8s (see the SQC tab help text), so this is a generous
    # backstop for a stuck device, not the normal path.
    SQC_HYBRID_STAGE_TIMEOUT_S = 30.0

    @staticmethod
    def _sqc_history_phase_done(status):
        """True once it's safe to start the next wristband in a hybrid
        pipeline: the device's history phase gave way to the live forward
        phase, or it reached a terminal status without ever entering forward
        (history-only quick mode ending exactly at that boundary, an early
        error, a rejection)."""
        if status["phase"] == "forward":
            return True
        return status["status"] in MotionSenseHRV._SQC_TERMINAL_STATUSES

    def request_all_sqc_snapshots(self, max_seconds=None, history_only=False,
                                   stream_mode="sequential"):
        """Snapshot every connected wristband. stream_mode selects how the
        pulls are scheduled across wristbands:
          - "sequential" (default, safest): one wristband fully finishes
            before the next starts. The Mac has a single BLE radio time-sliced
            across all connections, so this is the most reliable choice for
            the bandwidth-heavy NUS burst transfer.
          - "parallel": every wristband starts at once and streams
            concurrently; fastest wall-clock time, at the cost of per-device
            throughput (radio time-sliced N ways) and higher stall risk.
          - "hybrid": a pipeline — start the first wristband alone; once it
            moves past its brief history burst into the lighter live forward
            phase, start the next wristband (now running alongside it);
            repeat down the list. Only one wristband is ever in the heavy
            history phase at a time — a middle ground between the other two.
        max_seconds / history_only pass through to quick mode, applied
        uniformly to every wristband regardless of stream_mode."""
        if stream_mode not in ("sequential", "parallel", "hybrid"):
            return f"⛔ unknown streaming mode: {stream_mode!r}"
        names = self.get_sqc_devices()
        if not names:
            return "⛔ No MSense wristbands connected"
        if getattr(self, "_sqc_run_thread", None) and self._sqc_run_thread.is_alive():
            return "⏳ A snapshot run is already in progress"
        self._ensure_sqc_threads()

        runner = {
            "sequential": self._run_sqc_sequential,
            "parallel": self._run_sqc_parallel,
            "hybrid": self._run_sqc_hybrid,
        }[stream_mode]
        self._sqc_run_thread = threading.Thread(
            target=runner, args=(names, max_seconds, bool(history_only)),
            name=f"sqc-{stream_mode}", daemon=True)
        self._sqc_run_thread.start()

        mode = ("history-only" if history_only
                else f"quick {max_seconds:g}s" if max_seconds else "full")
        if stream_mode == "sequential":
            return f"📡 Snapshotting {len(names)} wristband(s) one at a time ({mode}): {', '.join(names)}"
        if stream_mode == "parallel":
            return f"📡 Snapshotting {len(names)} wristband(s) in parallel ({mode}): {', '.join(names)}"
        return f"📡 Snapshotting {len(names)} wristband(s) pipelined (hybrid, {mode}): {', '.join(names)}"

    def _run_sqc_sequential(self, names, max_seconds=None, history_only=False):
        for name in names:
            msg = self.request_sqc_snapshot(name, max_seconds=max_seconds,
                                            history_only=history_only)
            self.info(f"SQC sequential: {name} — {msg}")
            deadline = time.time() + self.SQC_SEQ_PER_DEVICE_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(0.5)
                # get_sqc_status() also evaluates the handshake timeout
                if self.get_sqc_status(name)["status"] in self._SQC_TERMINAL_STATUSES:
                    break
            self.info(f"SQC sequential: {name} finished — "
                      f"{self.get_sqc_status(name)['status']}")
        self.info("SQC sequential: run complete")

    def _run_sqc_parallel(self, names, max_seconds=None, history_only=False):
        for name in names:
            msg = self.request_sqc_snapshot(name, max_seconds=max_seconds,
                                            history_only=history_only)
            self.info(f"SQC parallel: {name} — {msg}")
        # fired back-to-back, no waiting in between — devices stream
        # concurrently; the watchdog (already running) supervises every
        # "receiving" device regardless of which runner started it.
        deadline = time.time() + self.SQC_SEQ_PER_DEVICE_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.5)
            if all(self.get_sqc_status(n)["status"] in self._SQC_TERMINAL_STATUSES
                   for n in names):
                break
        self.info("SQC parallel: run complete")

    def _run_sqc_hybrid(self, names, max_seconds=None, history_only=False):
        for i, name in enumerate(names):
            msg = self.request_sqc_snapshot(name, max_seconds=max_seconds,
                                            history_only=history_only)
            self.info(f"SQC hybrid: {name} — {msg}")
            if i == len(names) - 1:
                break  # last device — nothing to pipeline into
            deadline = time.time() + self.SQC_HYBRID_STAGE_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(0.25)
                if self._sqc_history_phase_done(self.get_sqc_status(name)):
                    break
            else:
                self.info(f"SQC hybrid: {name} history phase timed out after "
                          f"{self.SQC_HYBRID_STAGE_TIMEOUT_S:.0f}s — "
                          "advancing pipeline anyway")

        deadline = time.time() + self.SQC_SEQ_PER_DEVICE_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.5)
            if all(self.get_sqc_status(n)["status"] in self._SQC_TERMINAL_STATUSES
                   for n in names):
                break
        self.info("SQC hybrid: run complete")

    def _stop_stream(self, name, session, reason):
        """Write a STOP command for ``session`` (best effort — inbound writes
        still work when the TX path is wedged)."""
        peripheral = self.active_devices.get(name)
        if peripheral is None or session is None:
            return False
        try:
            self._run_async(peripheral.write_gatt_char(
                NUS_RX_CHAR_UUID, build_command(OP_STOP, session.stream_id), response=True))
            self._sqc_debug(name, f"  STOP written ({reason})")
            return True
        except Exception as e:
            self._sqc_debug(name, f"  STOP write failed ({reason}): {e}")
            return False

    def cancel_sqc_snapshot(self, name):
        disp = self.display_name(name)
        state = self.sqc_state.get(name)
        if not state or not state.get("session"):
            return f"⛔ {disp}: nothing to cancel"
        return (f"✖ {disp}: stop sent"
                if self._stop_stream(name, state["session"], "user cancel")
                else f"⛔ {disp} cancel failed")

    def cancel_all_sqc_snapshots(self):
        active = [n for n, s in self.sqc_state.items()
                  if s.get("status") in ("requesting", "receiving")]
        for name in active:
            self.cancel_sqc_snapshot(name)
        return f"✖ Stop sent to {len(active)} wristband(s)" if active else "Nothing in progress"

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
            sqc = self.sqc_state.get(name)
            live = self.live_state.get(name)
            if sqc and sqc.get("session") and not sqc["session"].is_terminal:
                self._handle_sqc_notification(sqc, name, bytes(data), now, t_entry)
            elif live and live.get("session") and not live["session"].is_terminal:
                self._handle_live_notification(live, name, bytes(data), now, t_entry)
            else:
                self._sqc_debug(name, f"rx {len(data)}B ignored (no active stream)")
        except Exception as e:
            self.info(f"Error handling NUS data from {name}: {e}")

    @staticmethod
    def _diag_rx(diag, data, now):
        if diag is None:
            return
        gap = now - diag["last_t"] if diag["last_t"] else 0.0
        diag["last_t"] = now
        diag["count"] += 1
        diag["bytes"] += len(data)
        diag["last_gap_s"] = gap
        diag["max_gap_s"] = max(diag["max_gap_s"], gap)

    @staticmethod
    def _diag_proc(diag, t_entry, offset):
        if diag is None:
            return
        proc_ms = (time.perf_counter() - t_entry) * 1000.0
        diag["max_proc_ms"] = max(diag["max_proc_ms"], proc_ms)
        diag["offset"] = offset
        diag["log"].append((diag["count"], round(diag["last_gap_s"], 3),
                            round(proc_ms, 1), offset))

    def _handle_sqc_notification(self, state, name, data, now, t_entry):
        session = state["session"]
        diag = state["diag"]
        self._diag_rx(diag, data, now)
        state["last_rx_at"] = now

        try:
            events = session.feed(data)
        except ProtocolError as e:
            if len(session.payload) > 0:
                state["status"] = "finishing"
                threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                 kwargs={"partial": True, "warning": f"protocol violation: {e}"},
                                 daemon=True).start()
            else:
                state.update(status="error", error=f"protocol violation: {e}")
            self._sqc_debug(name, f"  PROTOCOL VIOLATION: {e}")
            self.info(f"SQC protocol violation from {name}: {e}")
            return

        for kind, obj in events:
            if kind == "start_ack":
                state["status"] = "receiving"
                self._sqc_debug(name, f"  START_ACK {obj.mode_name} total={obj.planned_total}B")
                self.info(f"SQC START_ACK from {name}: {state['product']} {obj.mode_name} "
                          f"total={obj.planned_total}B")
            elif kind == "data":
                if diag["first_t"] is None:      # throughput clock starts at first DATA
                    diag["first_t"] = now
                    diag["bytes"] = len(data)
                    diag["count"] = 1
                self._sqc_debug(
                    name, f"  DATA off={obj.offset} n={len(obj.data)} "
                          f"gap={diag['last_gap_s']:.2f}s -> {session.bytes_received}"
                          f"/{session.bytes_total or '∞'}B {session.phase_name}")
            elif kind == "result":
                state.update(status="rejected", error=obj.status_name)
                self._sqc_debug(name, f"  RESULT {obj.status_name}")
                self.info(f"SQC rejected for {name}: {obj.status_name}")
            elif kind == "end":
                self._sqc_debug(name, f"  END {obj.status_name} -> session {session.state}")
                if session.state == nus_stream.COMPLETE:
                    state["status"] = "finishing"
                    threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                     daemon=True).start()
                elif len(session.payload) > 0:
                    state["status"] = "finishing"
                    warning = (session.error if session.state == nus_stream.FAILED
                               else None)
                    threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                     kwargs={"partial": True, "warning": warning},
                                     daemon=True).start()
                else:
                    state.update(status="error",
                                 error=f"{obj.status_name}: {session.error}")
                    self.info(f"SQC END non-success for {name}: {session.error}")

        self._diag_proc(diag, t_entry, session.bytes_received)

    def _handle_live_notification(self, state, name, data, now, t_entry):
        session = state["session"]
        diag = state["diag"]
        self._diag_rx(diag, data, now)
        state["last_rx_at"] = now

        try:
            events = session.feed(data)
        except ProtocolError as e:
            state.update(status="error", error=f"protocol violation: {e}")
            self._sqc_debug(name, f"  LIVE PROTOCOL VIOLATION: {e}")
            self._stop_stream(name, session, "protocol violation")
            return

        for kind, obj in events:
            if kind == "start_ack":
                state["status"] = "streaming"
                self._sqc_debug(name, f"  LIVE START_ACK {obj.mode_name}")
                self.info(f"live stream running: {name} ({state['product']})")
            elif kind == "data":
                if diag["first_t"] is None:
                    diag["first_t"] = now
                self._live_ingest(state, name)
            elif kind == "result":
                self._sqc_debug(name, f"  LIVE RESULT {obj.status_name}")
            elif kind == "end":
                self._sqc_debug(name, f"  LIVE END {obj.status_name}")
                if session.state == nus_stream.STOPPED:
                    state["status"] = "stopped"
                else:
                    state.update(status="error",
                                 error=f"{obj.status_name}: {session.error or ''}".strip())
                self.info(f"live stream ended for {name}: {obj.status_name}")

        r = state["reassembler"]
        diag["skipped_history_slots"] = getattr(r, "skipped_history_slots", 0)
        self._diag_proc(diag, t_entry, session.bytes_received)

        # once the stream is terminal no more notifications route here
        # (_nus_data_handler gates on `not session.is_terminal`), so the 30 s
        # ring + StreamSession + reassembler are just dead weight until the
        # next start_live_stream — release them now. `status`/`error`/`diag`
        # stay so the SQC tab still shows the final state.
        if state["status"] in ("stopped", "error"):
            state["ring"] = None
            state["session"] = None
            state["reassembler"] = None

    def _live_ingest(self, state, name):
        """Pull whatever the reassembler has completed and append it to the
        rolling ring buffer (bounded to LIVE_WINDOW_S)."""
        r = state["reassembler"]
        ring = state["ring"]
        fs = state["fs"]
        cap = int(LIVE_WINDOW_S * fs)
        if state["product"] == ECG:
            if r.error and state["status"] == "streaming":
                state.update(status="error", error=r.error)
                self._stop_stream(name, state["session"], f"decode: {r.error}")
                return
            for blk in r.take():
                ring["ecg"].extend(blk["ecg"].tolist())
            while len(ring["ecg"]) > cap:
                ring["ecg"].popleft()
        else:
            raw = r.take()
            if raw:
                dec = decode_ppg(raw)
                for ch in ("ir1", "ir2", "g1", "g2"):
                    ring[ch].extend(dec[ch].tolist())
                    while len(ring[ch]) > cap:
                        ring[ch].popleft()

    # ── async debug sink + no-progress watchdog ─────────────────────────────
    # class-level so a single printer / monitor thread serves every instance
    _dbg_q = queue.Queue(maxsize=4000)
    _dbg_thread = None
    _sqc_threads_lock = threading.Lock()

    def _ensure_sqc_threads(self):
        """Start the async debug printer and the no-progress watchdog once."""
        self._sqc_threads_stopped = False
        if not hasattr(self, "_sqc_journal_open"):
            self._sqc_journal_open = set()
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

    def _sqc_diag_summary(self, name, store=None):
        d = (store or self.sqc_state).get(name, {}).get("diag")
        if not d:
            return {}
        dur = (d["last_t"] - d["first_t"]) if d["first_t"] and d["last_t"] else 0.0
        kbps = (d["bytes"] / 1024.0 / dur) if dur > 0 else 0.0
        return {
            "notifs": d["count"],
            "bytes": d["bytes"],
            "duration_s": round(dur, 2),
            "kib_s": round(kbps, 1),
            "notif_s": round(d["count"] / dur, 1) if dur > 0 else 0.0,
            "mean_notif_bytes": round(d["bytes"] / d["count"]) if d["count"] else 0,
            "last_gap_s": round(d["last_gap_s"], 2),
            "max_gap_s": round(d["max_gap_s"], 2),
            "max_proc_ms": round(d["max_proc_ms"], 1),
            "skipped_history_slots": d.get("skipped_history_slots", 0),
            "recoveries": d["recoveries"],
            "mtu": d["mtu"],
            "rssi": d["rssi"],
        }

    # how often the watchdog checks connected wristbands for a dropped link
    RECONNECT_SWEEP_S = 10.0

    def _journal_finished_sqc(self, reason=None):
        """Emit a "[SQC] <name> end (…)" journaler marker for every wristband
        whose SQC stream has reached a terminal status since the last check —
        pairing the "[SQC] <name> start" pushed in request_sqc_snapshot. Called
        from the watchdog tick and on disconnect. `reason` overrides the
        parenthetical (e.g. "disconnected") when the caller forces a flush."""
        for name in list(getattr(self, "_sqc_journal_open", ())):
            status = self.sqc_state.get(name, {}).get("status")
            if reason is None and status not in self._SQC_TERMINAL_STATUSES:
                continue
            self._sqc_journal_open.discard(name)
            self.journal(f"[SQC] {self.display_name(name)} end ({reason or status})")

    def _sqc_watchdog_loop(self):
        """1 Hz supervisor (runs off the BLE callback thread). Per active SQC
        stream, in priority order: quick-mode early terminate → quick-mode grace
        finalize → no-progress recovery. Also, every RECONNECT_SWEEP_S, an
        auto-reconnect sweep over every connected wristband.

        NOTE: this loop processes stalled devices one at a time within a
        single tick — if several devices stall in the same tick (more likely
        under the "parallel"/"hybrid" streaming modes than "sequential"),
        their recovery (_sqc_recover -> _reconnect_peripheral, ~2-4s each)
        runs serially, adding that much extra delay per additional
        simultaneously-stalled device. Each device still recovers correctly,
        just later — a known, accepted latency cost, not a correctness issue."""
        while not getattr(self, "_sqc_threads_stopped", False):
            time.sleep(1.0)
            now = time.time()
            self._journal_finished_sqc()
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

            for name, state in list(self.live_state.items()):
                if state.get("status") != "streaming":
                    continue
                try:
                    last = state.get("last_rx_at") or state.get("requested_at") or now
                    if now - last > SQC_NOPROGRESS_TIMEOUT_S:
                        self._sqc_debug(name, f"live stream stalled ({now - last:.1f}s) — STOP")
                        self.info(f"live stream {name} stalled — stopping")
                        state.update(status="error",
                                     error=f"stalled (no data for {now - last:.0f}s)")
                        self._stop_stream(name, state.get("session"), "watchdog stall")
                        self._reconnect_peripheral(name, "live stall")
                except Exception as e:
                    self.info(f"live watchdog error for {name}: {e}")

            if self.auto_reconnect and now - self._last_reconnect_sweep > self.RECONNECT_SWEEP_S:
                self._last_reconnect_sweep = now
                for name, p in list(self.active_devices.items()):
                    try:
                        if not p.is_connected:
                            self.info(f"{name} link down — auto-reconnecting")
                            self._reconnect_peripheral(name, "connection watchdog")
                    except Exception as e:
                        self.info(f"reconnect sweep error for {name}: {e}")

    def _sqc_quick_check(self, name, state, now):
        """Quick-mode handling. Returns True if it acted (skip no-progress)."""
        session = state.get("session")
        if session is None or session.start_ack is None:
            return False

        # (2) grace finalize — STOP sent but the device never answered with END
        if state.get("early_cancel_sent"):
            if now - (state.get("early_cancel_at") or now) > SQC_EARLY_CANCEL_GRACE_S:
                self._sqc_debug(name, "  quick: no END after STOP — finalizing partial locally")
                session.state = nus_stream.STOPPED
                state["status"] = "finishing"
                threading.Thread(target=self._finish_sqc_snapshot, args=(name,),
                                 kwargs={"partial": True,
                                         "warning": "quick mode: local finalize (no END from device)"},
                                 daemon=True).start()
                self._reconnect_peripheral(name, "quick-mode grace finalize")
            return True

        # (1) early terminate — enough of the stream has arrived
        if state.get("history_only"):
            target = HISTORY_BYTES
        elif state.get("max_seconds"):
            bps = PROFILE[state["product"]]["bytes_per_second"]
            target = HISTORY_BYTES + state["max_seconds"] * bps
        else:
            return False

        if session.bytes_received < target:
            return False

        if self._stop_stream(name, session, "quick mode"):
            state["early_cancel_sent"] = True
            state["early_cancel_at"] = now
            self.info(f"SQC quick mode: {name} STOP sent after {session.bytes_received}B")
        return True

    def _reconnect_peripheral(self, name, reason):
        """disconnect → wait → connect (a fresh BleakClient, not the old one
        — reusing a client across a disconnect isn't reliable on macOS's
        CoreBluetooth backend) → re-subscribe NUS (+ ENMO/IMU if a recording
        session is running). Runs on the watchdog thread. Both BLE calls are
        bounded (BLE_OP_TIMEOUT_S, via _run_async) — this is the exact path
        that used to hang forever with simplepyble (confirmed via a macOS
        thread dump); bleak's connect() actually respects the timeout."""
        if not SQC_AUTO_RECONNECT:
            return
        old = self.active_devices.get(name)
        if old is None:
            return
        addr = old.address

        try:
            self._run_async(old.disconnect())
        except Exception as e:
            self._sqc_debug(name, f"  disconnect failed/timed out: {e}")

        time.sleep(1.5)

        peripheral = self._make_client(addr, name)
        try:
            self._run_async(peripheral.connect())
        except Exception as e:
            self.info(f"SQC: {name} reconnect FAILED ({reason}): {e}")
            self._sqc_debug(name, f"  reconnect FAILED/timed out: {e}")
            if name in self.memo:
                self.memo[name].sts = "🔌 reconnect failed"
            return
        self.active_devices[name] = peripheral

        try:
            self._ensure_mtu(peripheral, name)
        except Exception as e:
            self._sqc_debug(name, f"  MTU negotiation error: {e}")
        try:
            self.register_nus_notify(peripheral, name)
            self.caps[name]["nus"] = True
        except Exception as e:
            self._sqc_debug(name, f"  NUS re-subscribe failed: {e}")
        try:
            self.register_battery(peripheral, name)
        except Exception as e:
            self._sqc_debug(name, f"  battery re-subscribe failed: {e}")
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

    def _sqc_recover(self, name, reason):
        """STOP a wedged stream, then disconnect + reconnect. Runs on the
        watchdog thread (never the BLE callback thread)."""
        state = self.sqc_state.get(name)
        if not state or state.get("status") != "receiving":
            return
        session = state.get("session")
        diag = state.get("diag") or {}
        diag["recoveries"] = diag.get("recoveries", 0) + 1
        summary = self._sqc_diag_summary(name)
        self.info(f"SQC watchdog: {name} stalled ({reason}) — STOP + reconnect. diag={summary}")
        self._sqc_debug(name, f"WATCHDOG stall: {reason}; diag={summary}")
        state.update(status="error",
                     error=f"stalled ({reason}) — reconnected, press Request to retry")
        if session and not session.is_terminal:
            session.state = nus_stream.FAILED
        if name in self.memo:
            self.memo[name].sts = "⚠️ stream stalled"

        # inbound writes still work when TX is wedged, so try STOP first — it
        # lets the firmware tear down its stream and free TX slots
        self._stop_stream(name, session, f"stall: {reason}")
        self._reconnect_peripheral(name, f"stall: {reason}")

    def _sqc_channels(self, product, decoded):
        if product == PPG:
            return {k: decoded[k] for k in ("ir1", "ir2", "g1", "g2")}
        return {"ecg": decoded["ecg"]}

    def _finish_sqc_snapshot(self, name, partial=False, warning=None):
        state = self.sqc_state[name]
        session = state["session"]
        product = state["product"]
        payload = bytes(session.payload)
        fs = PROFILE[product]["sample_rate"]

        provenance = session.provenance(
            requested_at=self._iso(state["requested_at"]),
            completed_at=self._iso(time.time()),
            host_version=__version__,
        )

        # save the raw payload + sidecar FIRST — a decode hiccup must never lose
        # a captured payload
        suffix_secs = round(session.bytes_received / PROFILE[product]["bytes_per_second"], 1)
        suffix = f"_p{suffix_secs:.0f}s" if partial else ""
        try:
            state["saved_path"] = self._save_sqc_capture(name, product, payload, provenance,
                                                         suffix=suffix)
            provenance["raw_file"] = os.path.basename(state["saved_path"])
        except Exception as e:
            self.info(f"SQC save failed for {name}: {e}")

        try:
            decoded = decode_ppg(payload) if product == PPG else decode_ecg(payload)
            channels = self._sqc_channels(product, decoded)
            n = len(next(iter(channels.values())))
            secs = round(n / fs, 2) if fs else 0.0

            boundary = (decoded.get("history_boundary_sample")
                        if product == ECG else HISTORY_BYTES // 16)
            state["decoded"] = {"channels": channels, "fs": decoded["fs"],
                                "history_boundary_sample": boundary}

            if product == PPG:
                provenance["oob_frac"] = round(float(decoded["oob_frac"]), 4)
            else:
                provenance.update(
                    blocks_valid=decoded["blocks"],
                    skipped_history_slots=decoded["skipped_history_slots"],
                    decode_error=decoded["error"],
                )
                if decoded["error"] and not warning:
                    warning = f"ECB2 decode: {decoded['error']}"

            if partial:
                provenance.update(partial=True, seconds_captured=secs,
                                  bytes_captured=session.bytes_received,
                                  phase_at_stop=session.phase_name,
                                  requested_max_seconds=state.get("max_seconds"),
                                  history_only=state.get("history_only", False))
                state["partial"] = True
                state["quick_seconds"] = secs
            if warning:
                provenance["warning"] = warning
                state["warning"] = warning

            state["provenance"] = provenance
            state["status"] = "ready"
            self.info(f"SQC {'partial ' if partial else ''}snapshot ready for {name}: {n} "
                      f"{product} samples ({secs}s) → {state['saved_path']}")
        except Exception as e:
            provenance["warning"] = warning
            state["provenance"] = provenance
            state.update(status="error", error=f"decode failed (raw saved): {e}")
            self.info(f"SQC decode failed for {name}: {e}")

        self._journal_finished_sqc()  # close this device's "[SQC] … start" marker

    @staticmethod
    def _iso(epoch):
        return datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")

    def _save_sqc_capture(self, name, product, payload, provenance, suffix=""):
        """Persist the raw sensor payload + a provenance sidecar. Goes to the
        active session log dir when a recording is running, else data/sqc_snapshots/.
        `suffix` marks partial (quick-mode) captures on disk, e.g. "_p5s"."""
        ts = time.strftime("%y%m%d_%H%M%S")
        base = getattr(self, "log_dir", None) or os.path.join(app_context().data_dir, "sqc_snapshots", ts)
        os.makedirs(base, exist_ok=True)

        safe = str(name).replace(":", "-").replace(" ", "_")
        ext = ".ppg" if product == PPG else ".ecg"
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
            return {"status": "unavailable", "phase": None, "bytes_received": 0,
                    "bytes_total": None, "provenance": None, "saved_path": None,
                    "error": None, "diag": {}}

        session = state["session"]
        now = time.time()
        # a missing START_ACK is not a wedged stream (the watchdog only recovers
        # streams that were progressing) — time it out here instead
        if state["status"] == "requesting" and now - state["requested_at"] > HANDSHAKE_TIMEOUT_S:
            state.update(status="error",
                         error=f"no START_ACK within {HANDSHAKE_TIMEOUT_S:.0f}s "
                               "(device recording? in BLE range?)")

        return {
            "status": state["status"],
            "phase": session.phase_name if session else None,
            "bytes_received": session.bytes_received if session else 0,
            "bytes_total": session.bytes_total if session else None,
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
            "device_type": PROFILE[state["product"]]["name"],
            "channels": d["channels"],
            "fs": d["fs"],
            "provenance": state["provenance"],
            "partial": bool(state.get("partial")),
            "quick_seconds": state.get("quick_seconds"),
            "warning": state.get("warning"),
            "history_boundary_sample": d.get("history_boundary_sample"),
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
        product = state["product"]
        if session is None or product is None or len(session.payload) == 0:
            return None

        now = time.time()
        cached = state.get("preview")
        if cached is not None and cached["_len"] == len(session.payload):
            return cached["result"]
        if cached is not None and now - cached["_at"] < self.SQC_PREVIEW_MIN_INTERVAL_S:
            return cached["result"]

        try:
            payload = bytes(session.payload)
            decoded = decode_ppg(payload) if product == PPG else decode_ecg(payload)
        except Exception as e:
            self._sqc_debug(name, f"  preview decode failed: {e}")
            return cached["result"] if cached else None

        boundary = (decoded.get("history_boundary_sample")
                    if product == ECG else HISTORY_BYTES // 16)
        result = {
            "device_type": PROFILE[product]["name"],
            "channels": self._sqc_channels(product, decoded),
            "fs": decoded["fs"],
            "provenance": None,
            "partial": True,
            "streaming": True,     # still coming in — the plot titles it "receiving…"
            "history_boundary_sample": boundary,
        }
        state["preview"] = {"result": result, "_len": len(session.payload), "_at": now}
        return result

    # ── live INFINITY stream (protocol + decode + in-memory plot only) ──────

    def start_live_stream(self, name):
        """Begin a continuous INFINITY stream — decoded into an in-memory
        rolling buffer for the live plot. Not written to disk or LSL (yet)."""
        disp = self.display_name(name)
        peripheral = self.active_devices.get(name)
        if peripheral is None or not peripheral.is_connected:
            return f"⛔ {disp} not connected"
        product = self._sqc_product(name)
        if product is None:
            return f"⛔ {disp}: unknown product (not an MSense4ECG / MSense4PPG?)"

        sqc = self.sqc_state.get(name)
        if sqc and sqc["status"] in ("requesting", "receiving", "finishing"):
            return f"⏳ {disp} snapshot in progress — wait for it to finish"
        live = self.live_state.setdefault(name, self._new_live_state())
        if live["status"] in ("requesting", "streaming", "stopping"):
            return f"⏳ {disp} live stream already running"

        mtu, mtu_err = self._check_stream_mtu(peripheral, name)
        if mtu_err:
            live.update(status="error", error=mtu_err)
            return f"⛔ {disp}: {mtu_err}"

        self._ensure_sqc_threads()
        fs = PROFILE[product]["sample_rate"]
        reassembler = self._new_reassembler(product)
        sid = new_stream_id()
        diag = self._new_sqc_diag()
        diag.update(mtu=mtu, rssi=self._connect_rssi.get(name))
        chans = PROFILE[product]["channels"]
        live.update(
            status="requesting",
            session=StreamSession(sid, product=product, expect_mode=MODE_INFINITY,
                                  on_span=reassembler.feed),
            reassembler=reassembler, product=product, fs=fs,
            requested_at=time.time(), last_rx_at=time.time(),
            ring={ch: deque(maxlen=int(LIVE_WINDOW_S * fs)) for ch in chans},
            error=None, diag=diag,
        )
        try:
            self._run_async(peripheral.write_gatt_char(
                NUS_RX_CHAR_UUID, build_command(OP_START_INFINITY, sid), response=True))
        except Exception as e:
            live.update(status="error", error=f"request failed: {e}")
            return f"⛔ {disp} live start failed: {e}"
        self.info(f"live stream START_INFINITY sent to {disp} ({product}, stream {sid:#010x})")
        self._sqc_debug(name, f"LIVE START_INFINITY stream={sid:#010x} {product}")
        return f"📡 {disp}: live {product} stream starting…"

    def stop_live_stream(self, name):
        disp = self.display_name(name)
        live = self.live_state.get(name)
        if not live or not live.get("session"):
            return f"⛔ {disp}: no live stream"
        live["status"] = "stopping"
        self._stop_stream(name, live["session"], "user stop")
        return f"✖ {disp}: live stream stop sent"

    def stop_all_live_streams(self):
        active = [n for n, s in self.live_state.items()
                  if s.get("status") in ("requesting", "streaming")]
        for name in active:
            self.stop_live_stream(name)
        return f"✖ Stop sent to {len(active)} live stream(s)" if active else "No live stream running"

    def get_live_stream_status(self, name):
        live = self.live_state.get(name)
        if live is None:
            return {"status": "idle", "error": None, "diag": {}}
        return {
            "status": live["status"],
            "product": live.get("product"),
            "error": live.get("error"),
            "diag": self._sqc_diag_summary(name, store=self.live_state),
        }

    def get_live_stream_preview(self, name):
        """Rolling-buffer snapshot for the live plot — same shape as
        get_sqc_result (partial=True). None if nothing plottable yet."""
        live = self.live_state.get(name)
        if live is None or live["status"] not in ("streaming", "stopping", "stopped", "error"):
            return None
        ring = live.get("ring") or {}
        channels = {ch: np.asarray(buf, dtype=float) for ch, buf in ring.items() if len(buf)}
        if not channels:
            return None
        return {
            "device_type": PROFILE[live["product"]]["name"],
            "channels": channels,
            "fs": live["fs"],
            "provenance": None,
            "partial": True,
            "streaming": live["status"] in ("streaming", "stopping"),
            "live": True,
            "history_boundary_sample": None,   # a continuous stream has no boundary line
        }


class MsenseOutlet(StreamOutlet):
    def __init__(self, name, address, chunk_size=32, max_buffered=360, use_lsl=True):
        self.name = name.replace(':', '-')
        # exact string handed to StreamInfo below (keeps colons) — this is what
        # the LSL recorder resolves the stream by; MotionSenseHRV.lsl_streams()
        # maps it back to the memo key.
        self.stream_name = name
        self.use_lsl = use_lsl

        lsl_status = "OK" if self.use_lsl else "disabled"
        self.msg = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."
        self.msg_fun = f"📻 {self.tic()} LSL {lsl_status}. Ready to start..."

        if self.use_lsl:
            info = StreamInfo(name, "MotionSenSE", 3, 2, cf_double64, address)
            super().__init__(mark_plasma_origin(info), chunk_size, max_buffered)

        self.log_dir = os.path.join(app_context().data_dir, "default")

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