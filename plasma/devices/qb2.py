"""Blickfeld QB2 capture: full frames on disk, frame IDs over LSL.

The SDK's async API lets Stop cancel a pending network read. A bounded writer
queue absorbs short disk stalls without accumulating unbounded point clouds.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
import uuid

try:
    import blickfeld_qb2
except ImportError:
    blickfeld_qb2 = None  # optional plugin dependency; keep other sensors usable
import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock

from plasma.config import plasma_config
from plasma.devices.template import PlasmaDevice, PlasmaMemo
from plasma.devices.qb2_config import configured_devices
from plasma.lsl_util import mark_plasma_origin


class Qb2Unit(PlasmaDevice):
    STREAM_NAME = "blickfield_qb2"  # retain the existing stream name for consumers
    FRAME_TIMEOUT_S = 10.0
    STOP_TIMEOUT_S = 5.0
    WRITE_QUEUE_SIZE = 8

    def __init__(self, session_info, logger=None, tag="qb2 LiDAR", *,
                 address=None, api_key=None, device_id=None):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo(tag)
        self.addr = (address if address is not None else plasma_config.ip_lidar or "").strip()
        self._api_key = (api_key if api_key is not None else plasma_config.qb2_api_key or "").strip()
        self.device_id = device_id
        self.stream_name = f"{self.STREAM_NAME}_{device_id}" if device_id else self.STREAM_NAME
        self._loop = None
        self._reader = None
        self._state_lock = threading.Lock()
        self._fault = None
        self.outlet = None
        self.out_dir = None
        if not self.addr:
            self._fail(ValueError("Set the QB2 LiDAR IP address in Configuration"))
            return
        if blickfeld_qb2 is None:
            self._fail(ImportError('Install the QB2 SDK with pip install "plasma-app[qb2]"'))
            return
        info = StreamInfo(self.stream_name, "Image", 1, 8, "string",
                          f"plasma-qb2-{self.addr}-{uuid.uuid4().hex}")
        info.desc().append_child_value("frame_id_encoding", "decimal uint64")
        info.desc().append_child_value("timestamp_reference", "host receipt, LSL local_clock")
        info.desc().append_child_value("device_address", self.addr)
        info.desc().append_child_value("device_label", tag)
        try:
            self.outlet = StreamOutlet(mark_plasma_origin(info))
        except Exception as error:
            self._fail(error)
            return
        # Channel creation is lazy; the first frame verifies transport and auth.
        self.memo.sts = "Ready"
        self.memo.set_latest("Configured; connection will be checked on Start")

    def lsl_streams(self):
        return {self.stream_name: self.tag}

    def _fail(self, error):
        # Vendor exceptions can contain credentials; redact before logging/UI.
        detail = str(error)
        status = getattr(getattr(error, "status", None), "name", "")
        if status in {"NOT_FOUND", "UNAUTHENTICATED", "PERMISSION_DENIED"}:
            detail += "; check the QB2 application key and its account permissions in Configuration"
        if self._api_key:
            detail = detail.replace(self._api_key, "[redacted]")
        self._fault = f"{type(error).__name__}: {detail}"
        self.memo.sts = f"⛔ QB2 capture failed: {self._fault}"
        self.memo.set_latest(self._fault)
        self.info(self.memo.sts)

    def start(self):
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("QB2 capture is still active; wait for Stop to finish")
            if self.outlet is None:
                raise RuntimeError(self._fault or "QB2 is not configured")
            # Resolve at Start, after the panel injects its shared session folder.
            root = self.session_dir or os.path.join(
                self.session_info["log_dir"],
                f"{self.session_info['participant_enc']}_{time.strftime('%y%m%d_%H%M%S')}")
            self.out_dir = Path(root) / "qb2"
            if self.device_id:
                self.out_dir /= self.device_id
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self._fail(error)
                raise
            self._run_id = uuid.uuid4().hex
            self._fault = None
            self.frames_saved = 0
            self._stop_event.clear()
            self.memo.sts = "Starting..."
            self.memo.set_latest(f"Connecting to {self.addr}")
            self._thread = threading.Thread(target=self.streaming, daemon=True,
                                            name="plasma-qb2")
            self._thread.start()

    def streaming(self):
        try:
            asyncio.run(self._capture())
        except Exception as error:
            self._fail(error)
        finally:
            with self._state_lock:
                self._loop = None
                self._reader = None
            if self._fault is None:
                self.memo.sts = "🟥 stopped"
                self.memo.set_latest(f"Saved {self.frames_saved} frames to {self.out_dir}")

    async def _capture(self):
        queue = asyncio.Queue(maxsize=self.WRITE_QUEUE_SIZE)
        reader_done = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def receive():
            try:
                args = {"fqdn_or_ip": self.addr, "secure": True}
                if self._api_key:
                    args["token"] = blickfeld_qb2.TokenFactory(
                        application_key_secret=self._api_key)
                if self._stop_event.is_set():
                    return
                with blickfeld_qb2.Channel(**args) as channel:
                    stream = blickfeld_qb2.core_processing.services.PointCloud(channel).async_stream()
                    try:
                        while not self._stop_event.is_set():
                            try:
                                response = await asyncio.wait_for(
                                    stream.__anext__(), timeout=self.FRAME_TIMEOUT_S)
                            except StopAsyncIteration:
                                raise RuntimeError("Point-cloud stream ended unexpectedly") from None
                            except asyncio.TimeoutError:
                                raise TimeoutError(
                                    f"No QB2 frame received for {self.FRAME_TIMEOUT_S:g} seconds; "
                                    "check power, network, and application key") from None
                            if self._stop_event.is_set():
                                break
                            unix_now, lsl_now = time.time(), local_clock()
                            frame = response.frame
                            payload = {
                                "frame": frame,
                                "unix_time": unix_now,
                                "datetime": datetime.fromtimestamp(
                                    unix_now, timezone.utc).isoformat(timespec="milliseconds"),
                                "lsl_time": lsl_now,
                                "device_id": self.device_id,
                                "device_address": self.addr,
                            }
                            try:
                                queue.put_nowait((payload, lsl_now))
                            except asyncio.QueueFull:
                                raise RuntimeError(
                                    "QB2 disk writer cannot keep up (frame queue full); "
                                    "capture halted to avoid silent data loss") from None
                            self.memo.sts = "🟢"
                    finally:
                        await stream.aclose()
            except asyncio.CancelledError:
                if not self._stop_event.is_set():
                    raise
            finally:
                reader_done.set()

        async def write():
            while not (reader_done.is_set() and queue.empty()):
                try:
                    payload, lsl_now = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                # Keep disk I/O off the event loop so network reads and Stop
                # stay responsive. Only one write is in flight at a time.
                path = await asyncio.to_thread(self._save_frame, payload)
                self.frames_saved += 1
                # Publish only after a complete file exists. Strings preserve
                # uint64 frame IDs, which the old int32 outlet could overflow.
                self.outlet.push_sample([str(payload["frame"].id)], timestamp=lsl_now)
                self.memo.set_latest(
                    f"frame ID = {payload['frame'].id}; {self.frames_saved} saved → {path.name}")

        reader = asyncio.create_task(receive())
        writer = asyncio.create_task(write())
        with self._state_lock:
            self._loop, self._reader = loop, reader
            if self._stop_event.is_set():
                reader.cancel()
                reader_done.set()
        try:
            done, _ = await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
            if writer in done:
                # A writer failure must interrupt a stalled network read.
                await writer
            try:
                await reader
            finally:
                # Drain already accepted frames even on timeout/network failure.
                await writer
        finally:
            for task in (reader, writer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)

    def _save_frame(self, payload):
        frame_id = int(payload["frame"].id)
        path = self.out_dir / f"frame_{self._run_id}_{self.frames_saved:08d}_{frame_id}.npy"
        partial = path.with_suffix(".npy.partial")
        try:
            with partial.open("xb") as handle:
                np.save(handle, payload, allow_pickle=True)
            os.replace(partial, path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return path

    def stop(self):
        self.request_stop()
        self.wait_stopped(self.STOP_TIMEOUT_S)

    def request_stop(self):
        """Signal without joining, so all LiDARs receive Stop immediately."""
        self._stop_event.set()
        with self._state_lock:
            loop, reader = self._loop, self._reader
            if loop is not None and reader is not None:
                try:
                    loop.call_soon_threadsafe(reader.cancel)
                except RuntimeError:  # event loop finished while Stop acquired lock
                    pass

    def wait_stopped(self, timeout):
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
            if thread.is_alive():
                self.memo.sts = "⛔ QB2 stop unconfirmed; capture worker still active"
                raise RuntimeError("QB2 Stop timed out; wait for pending disk I/O before restarting")
        if self._fault is None:
            self.memo.sts = "🟥 stopped"

    def disconnect(self):
        self.stop()
        # Drop the native outlet only after its producer has exited.
        self.outlet = None


class Qb2(PlasmaDevice):
    """One catalog plugin with independently monitored physical LiDAR units."""

    def __init__(self, session_info, logger=None, tag="qb2 LiDAR"):
        super().__init__(session_info, logger, tag)
        self.units = {}
        self.memo = {}
        try:
            records = configured_devices(plasma_config)
        except ValueError as error:
            memo = PlasmaMemo(tag)
            memo.sts = "⛔ Invalid QB2 device settings"
            memo.set_latest(str(error))
            self.memo[tag] = memo
            return
        for row in records:
            if not row["enabled"]:
                continue
            identity = row["id"]
            unit = Qb2Unit(session_info, logger, f"{row['name']} [{row['address']}]",
                           address=row["address"], api_key=row["api_key"], device_id=identity)
            self.units[identity] = unit
            self.memo[identity] = unit.memo
        if not self.units:
            memo = PlasmaMemo(tag)
            memo.sts = "Ready"
            memo.set_latest("No enabled LiDARs — add or enable devices in Configuration → QB2 LiDARs")
            self.memo[tag] = memo

    def lsl_streams(self):
        return {unit.stream_name: identity for identity, unit in self.units.items()
                if unit.outlet is not None}

    def start(self):
        if any(u._thread is not None and u._thread.is_alive() for u in self.units.values()):
            raise RuntimeError("QB2 capture is still active; wait for Stop to finish")
        # Compute one fallback path for standalone use, as the panel normally
        # injects its own shared folder before this call.
        root = self.session_dir or os.path.join(
            self.session_info["log_dir"],
            f"{self.session_info['participant_enc']}_{time.strftime('%y%m%d_%H%M%S')}")
        for unit in self.units.values():
            unit.session_info = self.session_info
            unit.session_dir = root
            unit.journal_hook = self.journal_hook
            try:
                unit.start()
            except Exception as error:
                unit._fail(error)  # one broken unit must not block its peers

    def stop(self):
        for unit in self.units.values():
            unit.request_stop()
        # One total timeout, rather than N sequential five-second timeouts.
        deadline = time.monotonic() + Qb2Unit.STOP_TIMEOUT_S
        failed = 0
        for unit in self.units.values():
            try:
                unit.wait_stopped(deadline - time.monotonic())
            except RuntimeError:
                failed += 1
        if failed:
            raise RuntimeError(f"QB2 Stop unconfirmed for {failed} LiDAR(s); wait for pending disk I/O")

    def disconnect(self):
        try:
            self.stop()
        finally:
            for unit in self.units.values():
                if unit._thread is None or not unit._thread.is_alive():
                    unit.outlet = None
