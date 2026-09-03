"""Generic device-plugin registry.

Two kinds of plugin:

* **Static** — a plain :class:`PlasmaPlugin` listed in ``_STATIC``. Its device
  class is imported lazily (only when the device is actually instantiated), so a
  missing vendor SDK doesn't stop the plugin from appearing in the catalog —
  matching the old hard-coded ``DEVICE_CATALOG`` behaviour.

* **Dynamic** — a package listed in ``_DISCOVERY`` that exposes a module-level
  ``register(register_fn)``. Used by plugins that contribute UI (a
  Configuration-tab section, extra Gradio tabs) and therefore need their support
  modules imported at registration time. A discovery module that raises is
  logged and skipped so the app still launches.

This module must NOT import ``plasma.config`` — ``plasma.config`` imports this
one, and the device modules pulled in by ``load_plugins()`` import
``plasma.config`` in turn. Discovery is an explicit step in
``plasma.__main__.main()``, never an import-time side effect.
"""
import importlib
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlasmaPlugin:
    id: str                      # stable slug, e.g. "msense"
    display_name: str            # catalog label shown in the UI, e.g. "MSense Wristbands"
    module: str                  # dotted path holding the device class (imported lazily)
    class_name: str              # attribute of ``module`` — a PlasmaDevice subclass
    enabled_by_default: bool = True
    # (host: plasma.config.DeviceConfig) -> None; builds this plugin's
    # Configuration-tab accordion and owns its own persistence via
    # host.get_plugin_config / host.update_plugin_config.
    config_section: Optional[Callable] = None
    # ((tab_title, builder(ip: IntegratedPanel)), ...) — extra top-level Gradio tabs.
    tabs: tuple = ()


# Devices with no UI contribution — declared here so a broken vendor SDK still
# leaves the entry in the catalog (the class is imported only on instantiation).
_STATIC = [
    PlasmaPlugin("qb2", "qb2 LiDAR", "plasma.devices.qb2", "Qb2"),
    PlasmaPlugin("pupil_labs_imu", "Pupil Lab IMU",
                 "plasma.devices.pupil_labs", "PupilLabsIMU"),
    PlasmaPlugin("pupil_labs_blink", "Pupil Lab Eye Event Blink",
                 "plasma.devices.pupil_labs", "PupilLabsEyeEventBlink"),
    PlasmaPlugin("shimmer_gsr", "ShimmerGSR", "plasma.devices.shimmer", "ShimmerGSR"),
    PlasmaPlugin("obs", "OBS Recorder", "plasma.devices.obs", "ObsRecorder"),
    PlasmaPlugin("bitalino", "Bitalino", "plasma.devices.bitalino", "PlasmaBitalino",
                 enabled_by_default=False),  # needs PyBluez, not bundled — opt-in
]

# Packages exposing register(register_fn). PyInstaller can't follow the dynamic
# import below, and can't follow the lazy imports of the _STATIC module paths
# either — app_macos.spec / app_windows.spec hiddenimports must list every
# module named in this file.
_DISCOVERY = [
    "plasma.devices.msense",
]

_REGISTRY: "dict[str, PlasmaPlugin]" = {}


def register(plugin: PlasmaPlugin):
    _REGISTRY[plugin.id] = plugin


def load_plugins():
    """Populate the registry. Idempotent; called once from ``main()``.

    Discovery modules register first so the catalog order matches the old
    hard-coded ``DEVICE_CATALOG`` (MSense first, then the static devices).
    """
    for mod in _DISCOVERY:
        try:
            importlib.import_module(mod).register(register)
        except Exception as e:  # missing SDK / Bluetooth stack / import error
            logger.warning("plugin %s unavailable: %s", mod, e)
    for plugin in _STATIC:
        register(plugin)


def all_plugins():
    return list(_REGISTRY.values())


def get(plugin_id):
    return _REGISTRY.get(plugin_id)


def catalog():
    """Display name -> PlasmaPlugin, for the device catalog / selection UI."""
    return {p.display_name: p for p in _REGISTRY.values()}


def load_device_class(plugin: PlasmaPlugin):
    return getattr(importlib.import_module(plugin.module), plugin.class_name)
