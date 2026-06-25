import gradio as gr
from plasma.integrated_panel import IntegratedPanel
from plasma.config import device_config

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
    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    with gr.Blocks(title="PLASMA", theme=gr.themes.Ocean(), js=js_func) as app:
        with gr.Tab("Session dashboard"):
            ip.interface()
        with gr.Tab("Signal visualizer"):
            ip.visualizer_interface()
        with gr.Tab("Configuration"):
            device_config.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

