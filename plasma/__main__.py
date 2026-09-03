import atexit
import os
import signal
import gradio as gr
from plasma.integrated_panel import IntegratedPanel
from plasma.config import device_config


def _handle_sigterm(signum, frame):
    # run atexit hooks (device _shutdown_cleanup — CANCEL + BLE disconnect) then
    # exit hard, so `kill <pid>` doesn't leave an MSense mid-transfer holding its
    # single connection slot until the supervision timeout
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

    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    with gr.Blocks(title="PLASMA", theme=gr.themes.Ocean(), js=js_func) as app:
        with gr.Tab("Session dashboard"):
            ip.interface()
        with gr.Tab("Signal visualizer"):
            ip.visualizer_interface()
        with gr.Tab("ECG/PPG Signal Quality"):
            ip.signal_quality_interface()
        with gr.Tab("Configuration"):
            device_config.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

