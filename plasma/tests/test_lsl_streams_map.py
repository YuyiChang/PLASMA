"""PlasmaDevice.lsl_streams() — the LSL-stream-name -> memo-key map used by the
memo panel to fold recorder stats into device rows."""
from types import SimpleNamespace

import pytest

from plasma.devices.template import PlasmaDevice, PlasmaMemo


def test_base_default_single_memo():
    dev = PlasmaDevice.__new__(PlasmaDevice)
    dev.memo = PlasmaMemo("BITalino")
    dev.tag = "bitalino"
    assert dev.lsl_streams() == {"BITalino": "bitalino"}


def test_base_default_dict_memo_is_empty():
    dev = PlasmaDevice.__new__(PlasmaDevice)
    dev.memo = {"a": PlasmaMemo("a"), "b": PlasmaMemo("b")}
    dev.tag = "x"
    assert dev.lsl_streams() == {}


def test_msense_lsl_streams_from_active_outlets():
    from plasma.devices.msense.device import MotionSenseHRV

    fake = SimpleNamespace(active_outlets={
        "CfgA": SimpleNamespace(stream_name="MSense4PPG-KA5SA [E4:B0:63:12:34:56]",
                                use_lsl=True),
        "CfgB": SimpleNamespace(stream_name="x", use_lsl=False),
    })
    assert MotionSenseHRV.lsl_streams(fake) == {
        "MSense4PPG-KA5SA [E4:B0:63:12:34:56]": "CfgA"}


def test_msense_lsl_streams_no_outlets_yet():
    from plasma.devices.msense.device import MotionSenseHRV
    assert MotionSenseHRV.lsl_streams(SimpleNamespace()) == {}


def test_msense_outlet_stream_name_keeps_colons():
    pytest.importorskip("pylsl")
    from plasma.devices.msense.device import MsenseOutlet

    o = MsenseOutlet("MSense4PPG-KA5SA [E4:B0:63:12:34:56]", "E4:B0:63:12:34:56")
    assert o.stream_name == "MSense4PPG-KA5SA [E4:B0:63:12:34:56]"
    assert o.name == "MSense4PPG-KA5SA [E4-B0-63-12-34-56]"
