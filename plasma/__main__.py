import gradio as gr
from plasma.integrated_panel import IntegratedPanel
from plasma.devices.pupil_labs import PupilLabsDashboard
from plasma.config import plasma_config

def main():
    ip = IntegratedPanel()
    # pl = PupilLabsDashboard()

    with gr.Blocks(title="PLASMA", theme=gr.themes.Ocean()) as app:
        with gr.Tab("Session dashboard"):
            ip.interface()
        with gr.Tab("Configuration"):
            plasma_config.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True, share=False)


if __name__ == '__main__':
    main()

    

