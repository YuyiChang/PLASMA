"""Standalone disconnect/reconnect test harness, mimicking PLASMA's planned
architecture (see the "Replace simplepyble with bleak" plan): all BLE work
(via `bleak`) lives on a dedicated background thread with its own asyncio
event loop, completely decoupled from the "main" thread — here a simple
heartbeat stands in for the Gradio UI thread, to demonstrate it keeps
running the whole time, including across a real disconnect/reconnect.

NOT wired into the app — plain script, run directly:

    python -m plasma.devices.msense.bleak_reconnect_test
    # or
    python plasma/devices/msense/bleak_reconnect_test.py [device-name]

Connects to MSense4PPG-KA5SA by default (pass a different name as argv[1]).
Once connected, physically move the wristband out of range (or power it
off) to trigger a real disconnect — watch for the 🔌 DISCONNECTED line —
then bring it back to see it auto-reconnect (✅ CONNECTED again). The main
thread's heartbeat keeps ticking throughout, on a 1s cadence, proving the
BLE thread going through connect/disconnect/reconnect never blocks it.
Ctrl+C to quit; prints a final connect/disconnect count summary.
"""
import asyncio
import sys
import threading
import time

from bleak import BleakClient, BleakScanner

SCAN_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 15.0
RECONNECT_POLL_S = 1.0          # how often the supervise loop checks connection state
RECONNECT_RETRY_DELAY_S = 3.0   # backoff after a failed connect attempt
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"  # same as device.py


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BLEWorker:
    """Owns a dedicated asyncio event loop on its own thread. The main
    thread only ever calls is_connected()/counters — a plain attribute
    read, never touching a bleak object directly — mirroring how
    MotionSenseHRV's public methods would talk to this in the real app."""

    def __init__(self, target_name):
        self.target_name = target_name
        self.address = None        # cached once found — real reconnects skip rescanning
        self.connect_count = 0
        self.disconnect_count = 0
        self._connected = threading.Event()
        self._stop = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="ble-loop", daemon=True)

    def start(self):
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._supervise(), self.loop)

    def stop(self):
        self._stop.set()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5.0)

    def is_connected(self):
        return self._connected.is_set()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ---- runs on the BLE loop thread ----

    def _on_disconnected(self, client):
        self._connected.clear()
        self.disconnect_count += 1
        log(f"🔌 DISCONNECTED: {self.target_name}  (disconnect #{self.disconnect_count})")

    async def _supervise(self):
        """Mirrors the real app's watchdog reconnect sweep: on a fixed poll
        interval, if not currently connected, attempt (re)connect."""
        while not self._stop.is_set():
            if not self._connected.is_set():
                await self._connect_once()
            await asyncio.sleep(RECONNECT_POLL_S)

    async def _find_address(self):
        log(f"Scanning for {self.target_name!r} (timeout {SCAN_TIMEOUT_S:g}s)...")
        try:
            devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT_S)
        except Exception as e:
            log(f"⚠️  scan failed: {type(e).__name__}: {e}")
            return None
        match = next((d for d in devices if d.name == self.target_name), None)
        if match is None:
            log(f"  not found this sweep ({len(devices)} other device(s) seen) — will retry")
            return None
        log(f"  found at {match.address}")
        return match.address

    async def _connect_once(self):
        if self.address is None:
            self.address = await self._find_address()
            if self.address is None:
                return

        log(f"Connecting to {self.target_name} ({self.address})...")
        try:
            # a fresh BleakClient per attempt — reusing one across a
            # disconnect isn't reliable on macOS's CoreBluetooth backend
            client = BleakClient(self.address, disconnected_callback=self._on_disconnected,
                                 timeout=CONNECT_TIMEOUT_S)
            await client.connect()
            self._connected.set()
            self.connect_count += 1
            log(f"✅ CONNECTED: {self.target_name}  (connect #{self.connect_count}, "
                f"mtu={getattr(client, 'mtu_size', '?')})")
            try:
                raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
                log(f"  battery: {raw[0]}%")
            except Exception as e:
                log(f"  (battery read failed, non-fatal: {e})")
        except Exception as e:
            log(f"⚠️  connect failed: {type(e).__name__}: {e} "
                f"— retrying in {RECONNECT_RETRY_DELAY_S:g}s")
            await asyncio.sleep(RECONNECT_RETRY_DELAY_S)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "MSense4PPG-KA5SA"
    worker = BLEWorker(target)
    log(f"Starting BLE worker thread for {target!r}...")
    worker.start()

    log("Main thread heartbeat starting (1s tick) — this simulates the Gradio "
        "UI thread staying responsive. Move the wristband away to trigger a "
        "disconnect, then bring it back to watch it auto-reconnect. Ctrl+C to quit.")
    tick = 0
    try:
        while True:
            time.sleep(1.0)
            tick += 1
            status = "🟢 connected" if worker.is_connected() else "⚪ disconnected"
            log(f"(main thread heartbeat #{tick} — still responsive; BLE status: {status})")
    except KeyboardInterrupt:
        print()
        log("Ctrl+C — shutting down...")
    finally:
        worker.stop()
        log(f"Summary: {worker.connect_count} connect(s), "
            f"{worker.disconnect_count} disconnect(s)")


if __name__ == "__main__":
    main()
