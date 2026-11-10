"""QB2 device-list persistence and credential-masked settings editor.

No vendor SDK or plasma.config imports: safe during plugin discovery.
"""
import hashlib
import ipaddress
import re
import uuid


def normalize_devices(records):
    if not isinstance(records, list):
        raise ValueError("QB2 devices must be a list")
    result, ids, addresses = [], set(), set()
    for rec in records:
        if not isinstance(rec, dict):
            raise ValueError("Each QB2 device must be an object")
        address = str(rec.get("address", "") or "").strip()
        enabled = rec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("QB2 enabled must be true or false")
        if address:
            try:
                ipaddress.ip_address(address)
            except ValueError:
                if len(address) > 253 or not all(
                        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                        for part in address.rstrip(".").split(".")):
                    raise ValueError("Enter a QB2 IP address or hostname, without a URL or port") from None
        elif enabled:
            raise ValueError("An enabled QB2 device needs an IP address or hostname")
        identity = rec.get("id") or hashlib.sha256(address.encode()).hexdigest()[:16]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
            raise ValueError("QB2 device IDs must use 1–64 letters, digits, underscores or hyphens")
        if identity in ids:
            raise ValueError("QB2 device IDs must be unique")
        canonical = address.lower().rstrip(".")
        try:
            canonical = ipaddress.ip_address(address).compressed
        except ValueError:
            pass
        if enabled and canonical in addresses:
            raise ValueError("The same QB2 address cannot be enabled twice")
        if enabled:
            addresses.add(canonical)
        ids.add(identity)
        result.append({"id": identity, "name": str(rec.get("name", "") or "").strip() or address or "LiDAR",
                       "address": address, "api_key": str(rec.get("api_key", "") or "").strip(),
                       "enabled": enabled})
    return result


def configured_devices(host):
    return device_records(host.get_plugin_config("qb2"), getattr(host, "ip_lidar", ""),
                          getattr(host, "qb2_api_key", ""))


def device_records(blob, legacy_address="", legacy_key=""):
    if isinstance(blob, dict) and "devices" in blob:
        return normalize_devices(blob["devices"])
    # Legacy single-device settings remain readable; an explicitly empty list
    # takes precedence, so removing the last device never resurrects it.
    address = legacy_address
    if not address:
        return []
    return normalize_devices([{"address": address, "name": "QB2 LiDAR",
                               "api_key": legacy_key, "enabled": True}])


def save_device(host, selected, name, address, api_key, enabled):
    rows = configured_devices(host)
    identity = selected or uuid.uuid4().hex
    if selected and not any(r["id"] == selected for r in rows):
        raise ValueError("This LiDAR no longer exists; reload saved LiDARs")
    replacement = {"id": identity, "name": name, "address": address,
                   "api_key": api_key, "enabled": enabled}
    rows = [replacement if r["id"] == identity else r for r in rows]
    if selected is None:
        rows.append(replacement)
    host.update_plugin_config("qb2", {"devices": normalize_devices(rows)})
    return identity


def remove_device(host, selected):
    rows = [r for r in configured_devices(host) if r["id"] != selected]
    host.update_plugin_config("qb2", {"devices": rows})


def config_section(host):
    import gradio as gr

    def summary():
        rows = configured_devices(host)
        return [[r["name"], r["address"], r["enabled"], "Configured" if r["api_key"] else "None"]
                for r in rows]

    def choices():
        return [(f"{r['name']} [{r['address']}]", r["id"]) for r in configured_devices(host)]

    with gr.Accordion("QB2 LiDARs", open=True):
        gr.Markdown(
            "Add any number of LiDARs. Each has an optional application key and an enable switch. "
            "Save each device, then **re-Initialize** QB2 on the dashboard to apply changes. "
            "Keys are saved locally and included in config exports."
        )
        table = gr.Dataframe(headers=["Name", "IP / hostname", "Enabled", "API key"],
                             datatype=["str", "str", "bool", "str"], value=summary(),
                             interactive=False)
        selection = gr.Dropdown(label="Edit LiDAR", choices=choices(), value=None)
        name = gr.Textbox(label="LiDAR name")
        address = gr.Textbox(label="IP address or hostname")
        key = gr.Textbox(label="Application key secret (optional)", type="password")
        enabled = gr.Checkbox(label="Enable this LiDAR", value=True)
        with gr.Row():
            add = gr.Button("Add new LiDAR")
            save = gr.Button("Save LiDAR", variant="primary")
            remove = gr.Button("Remove LiDAR")
            reload = gr.Button("Reload saved LiDARs")
        status = gr.Textbox(show_label=False, interactive=False)

        def edit(identity):
            row = next((r for r in configured_devices(host) if r["id"] == identity), {})
            return row.get("name", ""), row.get("address", ""), row.get("api_key", ""), row.get("enabled", True)

        def new():
            return gr.update(value=None), "", "", "", True, "Enter the new LiDAR and click Save LiDAR"

        def persist(identity, label, addr, secret, on):
            try:
                identity = save_device(host, identity, label, addr, secret, on)
                return gr.update(choices=choices(), value=identity), summary(), "Saved — re-Initialize QB2 to apply"
            except (ValueError, OSError) as error:
                # Validation messages never interpolate the supplied key.
                return gr.update(), gr.update(), f"Could not save LiDAR: {error}"

        def delete(identity):
            remove_device(host, identity)
            return gr.update(choices=choices(), value=None), summary(), "Removed — re-Initialize QB2 to apply"

        def refresh():
            return gr.update(choices=choices(), value=None), summary(), "Loaded saved LiDARs"

        selection.change(edit, inputs=selection, outputs=[name, address, key, enabled])
        add.click(new, outputs=[selection, name, address, key, enabled, status])
        save.click(persist, inputs=[selection, name, address, key, enabled], outputs=[selection, table, status])
        remove.click(delete, inputs=selection, outputs=[selection, table, status])
        reload.click(refresh, outputs=[selection, table, status])
