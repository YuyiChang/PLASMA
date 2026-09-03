"""Shared helper for the MSense panels."""


def msense_device(ip):
    """The live `MotionSenseHRV` instance, or None. Imported lazily so building
    a panel never pulls `simplepyble`."""
    from ..device import MotionSenseHRV
    return ip.find_device(MotionSenseHRV)
