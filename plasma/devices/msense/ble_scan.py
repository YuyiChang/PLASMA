"""Config-tab BLE scan helper.

Kept separate from ``plasma/devices/msense.py`` on purpose: that module owns the
adapter/connection lifecycle for a live recording, this one just answers "which
MSense wristbands are advertising right now?" for the Configuration tab. Mirrors
the scan pattern in ``MotionSenseHRV`` (blocking ``adapter.scan_for`` +
``scan_get_results``); RSSI is not read because simplepyble only exposes it
post-connect.
"""


def scan_msense(timeout_ms=5000, filter_name="MSense", adapter=None):
    """Blocking BLE scan on adapter 0.

    Returns a list of ``{"name": str, "address": str, "connectable": bool}`` —
    one per advertising peripheral whose advertised name contains
    ``filter_name`` — deduped by (uppercased) address, first seen wins.

    Raises ``RuntimeError`` when no Bluetooth adapter is available.

    ``adapter`` is injectable for testing; when ``None`` the first system
    adapter is used.
    """
    if adapter is None:
        import simplepyble

        adapters = simplepyble.Adapter.get_adapters()
        if not adapters:
            raise RuntimeError("No Bluetooth adapter found")
        adapter = adapters[0]

    adapter.scan_for(timeout_ms)

    found = {}
    for p in adapter.scan_get_results():
        name = (p.identifier() or "").strip()
        if not name or filter_name not in name:
            continue
        addr = p.address().upper()
        if addr in found:
            continue
        try:
            connectable = bool(p.is_connectable())
        except Exception:
            connectable = True
        found[addr] = {"name": name, "address": addr, "connectable": connectable}

    return list(found.values())
