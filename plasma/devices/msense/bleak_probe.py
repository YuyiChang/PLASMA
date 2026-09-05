"""Standalone pre-flight check: can `bleak` see and connect to MSense
wristbands on this machine at all?

This is NOT wired into the app — plain script, run directly:

    python -m plasma.devices.msense.bleak_probe
    # or
    python plasma/devices/msense/bleak_probe.py

Exists to answer one question before any `device.py` rewrite is attempted:
does bleak (an asyncio-native BLE library, being evaluated as a replacement
for `simplepyble` — see the "Replace simplepyble with bleak" plan) actually
work against real hardware here? `simplepyble`'s blocking connect() has been
confirmed (via a macOS thread dump) to hang without ever releasing the
Python GIL, freezing the whole app with no recovery possible from Python.
Bleak is architecturally different (PyObjC/asyncio, not a compiled blocking
C++ call) and its own docs note explicit macOS connect-timeout handling was
added specifically to avoid exactly this failure class — but that needs
confirming against this Mac + these wristbands before committing to the
full rewrite.

Prints a clear PASS/FAIL summary. Deliberately has zero dependency on the
rest of the `plasma` package — safe to run standalone, independent of
whatever state `device.py` is in.
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

# same UUIDs device.py uses today — kept as plain literals here (not
# imported) so this script has no dependency on plasma/device.py at all.
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

SCAN_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 15.0
NAME_FILTER = "MSense"


async def main():
    print(f"Scanning for {SCAN_TIMEOUT_S:g}s (looking for names containing {NAME_FILTER!r})...")
    try:
        found = await BleakScanner.discover(timeout=SCAN_TIMEOUT_S, return_adv=True)
    except Exception as e:
        print(f"\n❌ FAIL — scan itself raised: {type(e).__name__}: {e}")
        return False

    wristbands = []
    for address, (device, adv) in found.items():
        name = device.name or adv.local_name or ""
        if NAME_FILTER in name:
            wristbands.append((name, address, adv.rssi))

    if not wristbands:
        print(f"\n❌ FAIL — no device with {NAME_FILTER!r} in its name was seen in "
              f"{SCAN_TIMEOUT_S:g}s ({len(found)} other device(s) seen). "
              "Make sure a wristband is powered on and nearby, then try again.")
        return False

    print(f"\nFound {len(wristbands)} wristband(s):")
    for name, address, rssi in wristbands:
        print(f"  - {name}  address={address}  rssi={rssi}")

    name, address, _ = wristbands[0]
    print(f"\nAttempting to connect to {name} ({address}), timeout={CONNECT_TIMEOUT_S:g}s...")
    try:
        async with BleakClient(address, timeout=CONNECT_TIMEOUT_S) as client:
            print(f"  connected. is_connected={client.is_connected}, "
                  f"mtu_size={getattr(client, 'mtu_size', '?')}")
            try:
                raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
                pct = raw[0] if raw else "?"
                print(f"  battery read OK: {pct}% (raw={bytes(raw)!r})")
            except Exception as e:
                print(f"  ⚠️  connected, but battery read failed: {type(e).__name__}: {e}")
                print("\n⚠️  PARTIAL — bleak can scan and connect, but the battery "
                      "characteristic read failed. Worth investigating before the "
                      "full rewrite, but connect() itself (the thing that hangs "
                      "today) works.")
                return True
    except Exception as e:
        print(f"\n❌ FAIL — connect failed/timed out: {type(e).__name__}: {e}")
        return False

    print("\n✅ PASS — bleak scanned, connected, and read a characteristic "
          "successfully against real hardware.")
    return True


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
