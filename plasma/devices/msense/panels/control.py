"""MSense "Control" sub-tab — flash erase (passcode-gated) + advanced BLE
diagnostics (auto-reconnect toggle, manual reconnect, GATT dump, write-encoding).
Reaches the live driver via `_common.msense_device(ip)`."""
import gradio as gr

from ._common import msense_device as _msense_device

# mirrors device.ERASE_CODE — kept local so importing this panel (at plugin
# registration) never pulls simplepyble via device.py
ERASE_CODE = 68
_NO_DEV = "⛔ Initialize MSense on the Session dashboard first."


def build_control_tab(ip=None):
    with gr.Column():
        gr.Markdown(
            "Device-level controls. Battery %, connection status and the journaler are on "
            "the **Session dashboard**; this tab is flash erase + diagnostics."
        )

        with gr.Accordion("🚨🚨🚨 Danger zone 🚨🚨🚨", open=False):
            gr.Markdown(
                f"**Flash erase wipes every recording on the wristband.** Type the erase "
                f"code (`{ERASE_CODE}`), tick *Enable*, then press the button. The device "
                f"disconnects — wait for its lights to go out, then re-Initialize."
            )
            with gr.Row():
                erase_code = gr.Number(label="Erase code", precision=0)
                erase_enable = gr.Checkbox(label="Enable erase feature")
            erase_btn = gr.Button("Erase flash data", interactive=False)
            erase_status = gr.Markdown()

            def _gate(enabled, code):
                ok = bool(enabled) and code == ERASE_CODE
                if enabled and not ok:
                    gr.Warning("Incorrect erase code")
                return gr.Button(interactive=ok)

            def _erase():
                dev = _msense_device(ip)
                if dev is None:
                    return _NO_DEV
                return dev.erase_flash_data(ERASE_CODE)

            erase_enable.change(_gate, inputs=[erase_enable, erase_code], outputs=erase_btn)
            erase_code.change(_gate, inputs=[erase_enable, erase_code], outputs=erase_btn)
            erase_btn.click(_erase, outputs=erase_status)

        with gr.Accordion("⚙️ Advanced", open=False):
            auto_rc = gr.Checkbox(True, label="Auto-reconnect dropped wristbands (~10 s)")
            with gr.Row():
                btn_reconnect = gr.Button("🔄 Reconnect now")
                btn_services = gr.Button("📋 GATT services")
            adv_status = gr.Markdown()

            with gr.Row():
                enc_val = gr.Number(label="Participant encoding", precision=0)
                btn_write_enc = gr.Button("Write encoding")

            def _set_auto(on):
                dev = _msense_device(ip)
                if dev is not None:
                    dev.auto_reconnect = bool(on)

            def _reconnect():
                dev = _msense_device(ip)
                return dev.reconnect_all() if dev is not None else _NO_DEV

            def _services():
                dev = _msense_device(ip)
                return dev.get_services() if dev is not None else _NO_DEV

            def _write_enc(v):
                dev = _msense_device(ip)
                return dev.write_enc(v) if dev is not None else _NO_DEV

            auto_rc.change(_set_auto, inputs=auto_rc)
            btn_reconnect.click(_reconnect, outputs=adv_status)
            btn_services.click(_services, outputs=adv_status)
            btn_write_enc.click(_write_enc, inputs=enc_val, outputs=adv_status)
