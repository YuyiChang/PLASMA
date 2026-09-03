"""MSense wristband plugin — BLE motion (ENMO / demo IMU stream) + ECG/PPG
signal-quality snapshots over the NUS bounded-stream protocol.

Everything MSense-specific lives under this package: the device driver
(``device.py``), the NUS protocol codec (``nus_stream.py``), the packed
record decoders (``records.py``), offline ECG/PPG filters
(``signal_quality.py``), the Configuration-tab BLE scan (``ble_scan.py``),
persistent gyro bias (``gyro_bias.py``), quaternion math (``quaternion.py``),
the Configuration-tab section (``config.py``) and the extra Gradio tabs
(``panels.py``).

``register()`` has no import-time side effects — ``plasma.plugins.load_plugins()``
calls it explicitly.
"""


def register(register_fn):
    from plasma.plugins import PlasmaPlugin
    from plasma.devices.msense import config as _config, panels as _panels
    register_fn(PlasmaPlugin(
        id="msense",
        display_name="MSense Wristbands",
        module="plasma.devices.msense.device",
        class_name="MotionSenseHRV",
        config_section=_config.config_section,
        tabs=(
            ("MSense", _panels.build_msense_tab),
        ),
    ))
