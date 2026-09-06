"""Simulated MSense-family wristband — a fake BLE peripheral that drives the
**real** ``MotionSenseHRV`` driver (NUS ``StreamSession`` FSM, the no-progress
watchdog, the LSL outlet, the ECG/PPG decoders, the journaler) with synthetic
data.

It supports everything a real unit does over BLE — connect, live ENMO / battery
/ IMU-orientation streaming, and the ECG/PPG signal-quality (SQC) snapshot — but
**not** the offline toolkit (USB download, ``.bin`` extraction, clock-sync,
viewer), which never touches BLE state.

Gated behind ``device_config.demo_mode`` (a Configuration-tab checkbox, or
``PLASMA_DEMO=1``) so it can't be mistaken for a real device. ``register()`` has
no import-time side effects — ``plasma.plugins.load_plugins()`` calls it.
"""


def register(register_fn):
    # deferred: plasma.plugins must not import plasma.config, but by the time
    # load_plugins() calls this, plasma.config is fully imported.
    from plasma.config import device_config
    if not device_config.demo_mode:
        return

    from plasma.plugins import PlasmaPlugin
    from plasma.devices.msense.panels import build_msense_tab
    from plasma.devices.msense_demo import config as _config
    register_fn(PlasmaPlugin(
        id="msense_demo",
        display_name="MSense Demo (simulated)",
        module="plasma.devices.msense_demo.device",
        class_name="MSenseDemo",
        enabled_by_default=False,
        config_section=_config.config_section,
        # same tab the real plugin contributes — reused unchanged; __main__
        # de-dupes by title if both plugins are enabled.
        tabs=(("🍠 YAMS (MSense Tools)", build_msense_tab),),
    ))
