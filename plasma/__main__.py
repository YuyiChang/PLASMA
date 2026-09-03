import atexit
import os
import signal
import gradio as gr
from plasma import plugins
from plasma.integrated_panel import IntegratedPanel
from plasma.config import device_config


def _handle_sigterm(signum, frame):
    # run atexit hooks (device shutdown-cleanup handlers) then exit hard, so
    # `kill <pid>` doesn't leave a device mid-transfer holding a resource
    # (e.g. a BLE connection slot) until its own supervision timeout
    atexit._run_exitfuncs()
    os._exit(143)

js_func = """
function refresh() {
    const url = new URL(window.location);

    if (url.searchParams.get('__theme') !== 'light') {
        url.searchParams.set('__theme', 'light');
        window.location.href = url.href;
    }
}
"""

def main():
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass  # not the main thread (e.g. imported oddly) — atexit still covers normal exit

    plugins.load_plugins()
    device_config.refresh_defaults()

    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    with gr.Blocks(title="PLASMA", theme=gr.themes.Ocean(), js=js_func) as app:
        with gr.Tab("Session dashboard"):
            ip.interface()
        with gr.Tab("Signal visualizer"):
            ip.visualizer_interface()
        # extra tabs contributed by enabled plugins (e.g. MSense SQC / IMU)
        for plugin in device_config.get_active_table().values():
            for tab_title, builder in plugin.tabs:
                with gr.Tab(tab_title):
                    builder(ip)
        with gr.Tab("Configuration"):
            device_config.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

