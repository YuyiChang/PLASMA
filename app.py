import gradio as gr

class IntegratedPanel():
    def __init__(self):
        pass

    def interface(self):
        device_grp = gr.CheckboxGroup(choices=['qb2 LiDAR', 'PupilLab'])
        btn_init = gr.Button("Initialize selected device(s)")


if __name__ == '__main__':
    with gr.Blocks() as app:
        panel = IntegratedPanel()
        panel.interface()
        app.launch()