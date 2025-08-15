import gradio as gr
from plasma.integrated_panel import IntegratedPanel
from plasma.devices.pupil_labs import PupilLabsDashboard

def main():
    ip = IntegratedPanel()

    # pl = PupilLabsDashboard()

    with gr.Blocks(title="PLASMA") as app:
        with gr.Tab("Session dashboard"):
            ip.interface()
        # with gr.Tab("PL"):
        #     pl.interface()

    app.launch(inbrowser=True)


if __name__ == '__main__':
    main()

    

