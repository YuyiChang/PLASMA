"""Config-tab BLE scan + USB-drive provisioning helpers.

Kept separate from ``device.py`` on purpose: that module owns the
adapter/connection lifecycle for a live recording, this one just answers "which
MSense wristbands are advertising right now?" (``scan_msense``) and "what is the
MAC on this mounted MSense drive?" (``read_uuid_from_drive``) for the
Configuration / MSense-Tools tabs.
"""
import os
import re
from glob import glob

_MAC_RE = re.compile(r'([0-9A-Fa-f]{2}[:]){5}([0-9A-Fa-f]{2})')


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


def read_uuid_from_drive(target_path):
    """Return the MAC address written in ``uuid.txt`` on a mounted MSense USB
    drive, or an explanatory string if none is found.

    (Was ``yams.uuid_extractor.get_uuid_from_path``.)
    """
    matches = glob(os.path.join(target_path, "uuid.txt"))
    if not matches:
        return "No MotionSenSE found. Plug in or change the MotionSenSE path?"
    try:
        with open(matches[0], "r") as f:
            txt = f.read()
    except Exception as e:
        return str(e)
    m = _MAC_RE.search(txt)
    return m.group(0) if m else "uuid.txt present but no MAC address in it"
