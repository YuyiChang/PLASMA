"""The fault catalogue for the simulated MSense device.

Each entry is a fault the :class:`FakePeripheral` (in ``device.py``) knows how
to reproduce. Keyed by a stable id so the Configuration-tab dropdown, the config
blob and the peripheral all agree on the same string, and so "the other errors
from the test scripts" (rejected RESULT, dropped/reordered DATA, STORAGE_ERROR,
garbled START_ACK, MTU stuck at 23, decode failure, reconnect-fails, …) are a
one-line-each future addition here.
"""

NO_FAULT = "none"

FAULTS = {
    NO_FAULT: {
        "label": "No fault",
        "help": "Behaves like a healthy wristband.",
    },
    "device_not_found": {
        "label": "Device not found at scan",
        "help": "The wristband never turns up in the BLE scan, so Initialize "
                "leaves it '⛔ device not found'.",
    },
    "disconnect_reconnect": {
        "label": "Drop BLE mid-session, then auto-reconnect",
        "help": "≈15 s after Start the link drops ('🔌 disconnected'); the "
                "driver's watchdog reconnects it ('🔄 reconnected') and streaming "
                "resumes.",
    },
    "sqc_error": {
        "label": "SQC snapshot stalls mid-transfer",
        "help": "A FINITE SQC snapshot gets START_ACK and a few DATA frames, "
                "then the peripheral goes silent — the no-progress watchdog "
                "STOPs, reconnects, and the tab shows '⚠️ stream stalled'.",
    },
    "stream_stall": {
        "label": "Live stream stalls mid-transfer",
        "help": "A continuous (INFINITY) live stream gets START_ACK and a "
                "handful of DATA frames, then goes silent — the no-progress "
                "watchdog STOPs and disconnects, and the tab shows an error.",
    },
}

# what the Configuration-tab "Fault" column accepts
FAULT_IDS = tuple(FAULTS)


def normalize(value):
    """Coerce an arbitrary cell value to a known fault id (default NO_FAULT)."""
    v = str(value or "").strip()
    return v if v in FAULTS else NO_FAULT
