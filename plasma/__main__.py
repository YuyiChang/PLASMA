import gradio as gr
from plasma.integrated_panel import IntegratedPanel

def main():
    ip = IntegratedPanel()

    with gr.Blocks(title="PLASMA") as demo:
        with gr.Tab("Session dashboard"):
            ip.interface()


if __name__ == '__main__':
    with gr.Blocks() as app:
        main()

    app.launch(inbrowser=True)

