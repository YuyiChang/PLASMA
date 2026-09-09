import gradio as gr
import json
import os
import tempfile

from plasma import plugins, __version__  # noqa: F401  (re-exported for callers)
from plasma.app_context import app_context

# `enabled_devices` real default is computed in refresh_defaults() once the
# plugin registry is loaded (the file's own value wins when present).
_DEFAULTS = {
    "enabled_devices": [],
    "ip_qb2_lidar": "",
    "ip_pupil_labs": "",
    "plugins": {},
    # when True, plugins.load_plugins() also registers the simulated MSense
    # device (plasma.devices.msense_demo) into the catalog. Restart-gated —
    # the plugin registry is built once at startup. PLASMA_DEMO=1 forces it on.
    "demo_mode": False,
}


class DeviceConfig:
    def __init__(self):
        cfg = self._load()
        self._active = list(cfg["enabled_devices"])
        self.ip_lidar = cfg["ip_qb2_lidar"]
        self.ip_pupil_labs = cfg["ip_pupil_labs"]
        # env var wins so a wrapper / CI can force demo mode without touching
        # the saved file
        self.demo_mode = bool(cfg["demo_mode"]) or bool(os.environ.get("PLASMA_DEMO"))
        # per-plugin config blobs, namespaced by plugin id
        self.plugins = dict(cfg["plugins"])
        # True when the config file existed and named enabled_devices — then
        # refresh_defaults() must not overwrite the user's selection with the
        # all-enabled default.
        self._enabled_specified = cfg["_enabled_specified"]

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        cfg_path = app_context().config_path
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r') as f:
                    raw = json.load(f)
                return {
                    "enabled_devices": list(raw.get("enabled_devices", [])),
                    "ip_qb2_lidar": raw.get("ip_qb2_lidar", ""),
                    "ip_pupil_labs": raw.get("ip_pupil_labs", ""),
                    "plugins": dict(raw.get("plugins", {})),
                    "demo_mode": bool(raw.get("demo_mode", False)),
                    "_enabled_specified": "enabled_devices" in raw,
                }
            except Exception:
                pass
        # missing or corrupt — refresh_defaults() fills enabled_devices and saves
        return {
            "enabled_devices": [],
            "ip_qb2_lidar": "",
            "ip_pupil_labs": "",
            "plugins": {},
            "demo_mode": False,
            "_enabled_specified": False,
        }

    def _save(self):
        path = app_context().config_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, 'w') as f:
            json.dump({
                "enabled_devices": self._active,
                "ip_qb2_lidar": self.ip_lidar,
                "ip_pupil_labs": self.ip_pupil_labs,
                "plugins": self.plugins,
                "demo_mode": self.demo_mode,
            }, f, indent=2)

    def refresh_defaults(self):
        """Run once from ``main()`` after ``plugins.load_plugins()``: fill the
        all-enabled default on first launch, and drop enabled_devices entries no
        longer in the catalog. Persists the (possibly cleaned) config."""
        catalog = plugins.catalog()
        if not self._enabled_specified:
            self._active = [p.display_name for p in plugins.all_plugins()
                            if p.enabled_by_default]
        else:
            self._active = [d for d in self._active if d in catalog]
        self._enabled_specified = True
        self._save()

    # ── public API ────────────────────────────────────────────────────────────

    def get_active_table(self):
        """Display name -> PlasmaPlugin for the devices the user has enabled."""
        catalog = plugins.catalog()
        return {k: catalog[k] for k in self._active if k in catalog}

    def get_plugin_config(self, plugin_id):
        return self.plugins.get(plugin_id, {})

    def update_plugin_config(self, plugin_id, blob):
        self.plugins[plugin_id] = blob
        self._save()

    # ── UI callbacks ──────────────────────────────────────────────────────────

    def _apply(self, selected, ip_lidar, ip_pupil_labs, demo_mode):
        self._active = list(selected)
        self.ip_lidar = ip_lidar
        self.ip_pupil_labs = ip_pupil_labs
        self.demo_mode = bool(demo_mode) or bool(os.environ.get("PLASMA_DEMO"))
        self._save()
        note = " — restart PLASMA to load the simulated device" if self.demo_mode else ""
        return f"Saved — {len(self._active)} device type(s) enabled{note}"

    def _export_config(self, selected, ip_lidar, ip_pupil_labs):
        config = {
            "enabled_devices": selected,
            "ip_qb2_lidar": ip_lidar,
            "ip_pupil_labs": ip_pupil_labs,
            "plugins": self.plugins,
            "demo_mode": self.demo_mode,
        }
        path = os.path.join(tempfile.mkdtemp(), app_context().config_filename)
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
        return path, f"Config exported with {len(selected)} device(s)"

    def _import_config(self, file):
        if file is None:
            return gr.update(), gr.update(), gr.update(), ""
        try:
            with open(file, 'r') as f:
                raw = json.load(f)
            catalog = plugins.catalog()
            enabled = [d for d in raw.get("enabled_devices", []) if d in catalog]
            unknown = [d for d in raw.get("enabled_devices", []) if d not in catalog]
            ip_lidar = raw.get("ip_qb2_lidar", self.ip_lidar)
            ip_pupil = raw.get("ip_pupil_labs", self.ip_pupil_labs)
            if "plugins" in raw:
                # plugin config sections re-render from get_plugin_config on the
                # next Configuration-tab visit
                self.plugins = dict(raw["plugins"])
                self._active = enabled
                self.ip_lidar = ip_lidar
                self.ip_pupil_labs = ip_pupil
                self.demo_mode = (bool(raw.get("demo_mode", self.demo_mode))
                                  or bool(os.environ.get("PLASMA_DEMO")))
                self._save()
            msg = f"Loaded — {len(enabled)} device type(s)"
            if unknown:
                msg += f" (skipped unknown: {', '.join(unknown)})"
            return (
                gr.update(value=enabled),
                gr.update(value=ip_lidar),
                gr.update(value=ip_pupil),
                msg,
            )
        except Exception as e:
            return gr.update(), gr.update(), gr.update(), f"Import error: {e}"

    # ── Gradio interface ──────────────────────────────────────────────────────

    def interface(self):
        with gr.Column():
            with gr.Accordion("Device catalog", open=True):
                gr.Markdown("Select which sensors appear in the session dashboard.")
                checkbox_group = gr.CheckboxGroup(
                    choices=list(plugins.catalog()),
                    value=list(self._active),
                    label="Available sensors",
                )
                demo_mode_chk = gr.Checkbox(
                    value=self.demo_mode,
                    label="Demo mode — add the simulated MSense device to the catalog",
                    info="Restart PLASMA after changing this. Also settable with "
                         "PLASMA_DEMO=1.",
                )

            with gr.Accordion("Network settings", open=True):
                ip_lidar_txt = gr.Text(value=self.ip_lidar, label="QB2 LiDAR IP address")
                ip_pupil_txt = gr.Text(value=self.ip_pupil_labs, label="Pupil Labs IP address")

            with gr.Row():
                btn_apply = gr.Button("Apply", variant="primary")
                btn_export = gr.Button("Export config")

            file_import = gr.File(label="Import config (.json)", file_types=[".json"])
            export_file = gr.File(label="Config file", interactive=False)
            status = gr.Textbox(interactive=False, value="", show_label=False, container=False)

            btn_apply.click(
                self._apply,
                inputs=[checkbox_group, ip_lidar_txt, ip_pupil_txt, demo_mode_chk],
                outputs=status,
            )
            btn_export.click(
                self._export_config,
                inputs=[checkbox_group, ip_lidar_txt, ip_pupil_txt],
                outputs=[export_file, status],
            )
            file_import.change(
                self._import_config,
                inputs=file_import,
                outputs=[checkbox_group, ip_lidar_txt, ip_pupil_txt, status],
            )

            # Per-plugin Configuration sections. Each owns its own Apply button
            # and persists via self.update_plugin_config — decoupled from the
            # global Apply above (which only writes enabled_devices + the IPs).
            for plugin in plugins.all_plugins():
                if plugin.config_section:
                    plugin.config_section(self)

            self._power_section()

    # ── restart / shutdown ────────────────────────────────────────────────────

    _RESTART_LABEL = "↻ Restart PLASMA"
    _SHUTDOWN_LABEL = "⏻ Shut down PLASMA"

    def _power_section(self):
        """Two-click Restart / Shut down at the bottom of the Configuration tab.
        The first click arms a button (label → "Click again to confirm",
        auto-cancels after ~4 s); the second fires it."""
        gr.Markdown("### Power")
        with gr.Row():
            btn_restart = gr.Button(self._RESTART_LABEL)
            btn_shutdown = gr.Button(self._SHUTDOWN_LABEL, variant="stop")
        power_msg = gr.Markdown(
            "Restart reloads config from disk — click **Apply** first to keep "
            "unsaved catalog changes. Both actions stop any running recording "
            "(the XDF file is flushed) and close device connections."
        )
        armed = gr.State("")                    # "" | "restart" | "shutdown"
        disarm = gr.Timer(4.0, active=False)    # auto-cancels an armed button

        outs = [armed, btn_restart, btn_shutdown, power_msg, disarm]

        # api_name=False — these re-exec / kill the process; keep them off the
        # headless API surface (see plasma/api.py)
        btn_restart.click(lambda a: self._power_click("restart", a),
                          inputs=armed, outputs=outs, api_name=False)
        btn_shutdown.click(lambda a: self._power_click("shutdown", a),
                           inputs=armed, outputs=outs, api_name=False)
        disarm.tick(self._power_disarm, outputs=outs)

    def _power_click(self, which, armed):
        if armed == which:                     # confirmed → fire
            import threading
            from plasma import __main__ as _m
            threading.Timer(
                0.6, lambda: _m.shutdown(restart=(which == "restart"))
            ).start()
            busy = ("Restarting PLASMA — this tab will reload shortly…"
                    if which == "restart"
                    else "PLASMA is shutting down. You can close this tab.")
            return ("",
                    gr.update(value=self._RESTART_LABEL, interactive=False),
                    gr.update(value=self._SHUTDOWN_LABEL, interactive=False),
                    busy, gr.Timer(active=False))
        # first click → arm this button, reset the other, start the disarm timer
        return (which,
                gr.update(value="↻ Click again to confirm"
                          if which == "restart" else self._RESTART_LABEL),
                gr.update(value="⏻ Click again to confirm"
                          if which == "shutdown" else self._SHUTDOWN_LABEL),
                "Click the same button again to confirm (auto-cancels in ~4 s).",
                gr.Timer(active=True))

    def _power_disarm(self):
        return ("",
                gr.update(value=self._RESTART_LABEL),
                gr.update(value=self._SHUTDOWN_LABEL),
                "", gr.Timer(active=False))


device_config = DeviceConfig()

# Alias kept for existing device-module imports (qb2.py, pupil_labs.py)
plasma_config = device_config
