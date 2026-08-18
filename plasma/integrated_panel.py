import gradio as gr
import struct
import os
from plasma.lsl_session import encode_participant
import importlib
import logging, datetime
from logging import Logger
from plasma.config import device_config, __data_dir__, __version__
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

VISUALIZER_PLOT_ELEM_ID = "plasma-visualizer-plot"

# Related scalar channels sharing one subplot instead of one row each.
# Channels not listed here fall back to being their own singleton group.
CHANNEL_GROUPS = {
    "Accel (g)": ["AccX", "AccY", "AccZ"],
    "Quaternion": ["Q0", "Q1", "Q2", "Q3"],
}
_CHANNEL_TO_GROUP = {ch: label for label, chs in CHANNEL_GROUPS.items() for ch in chs}


def _group_for_channel(ch):
    return _CHANNEL_TO_GROUP.get(ch, ch)

class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_config.get_active_table().keys())
        self.log_root = __data_dir__

        self.available_devices = []

        self.sts = "Welcome"

        self.logger = get_logger(__data_dir__)
        self.logger.info(f"Begin PLASMA v{__version__} session log")

    def visualizer_interface(self):
        with gr.Column():
            gr.Markdown(
                "Live device signal viewer. Initialize device(s) on the **Session dashboard** tab first, "
                "then refresh sources here. X-axis uses each device's own sample counter, not computer clock time."
            )
            with gr.Row():
                source_select = gr.Dropdown(choices=[], multiselect=True, label="Data source(s)")
                channel_select = gr.CheckboxGroup(choices=[], label="Channel(s)")

            with gr.Row():
                btn_refresh_sources = gr.Button("🔄 Refresh sources")
                btn_fullscreen = gr.Button("⛶ Fullscreen")

            with gr.Column(elem_id=VISUALIZER_PLOT_ELEM_ID):
                plot = gr.Plot(show_label=False)

            timer = gr.Timer(value=0.2, active=True)

            btn_refresh_sources.click(
                self.refresh_visual_sources, outputs=source_select
            ).then(
                self.refresh_channels, inputs=source_select, outputs=channel_select
            )
            source_select.change(self.refresh_channels, inputs=source_select, outputs=channel_select)
            timer.tick(fn=self.update_plot, inputs=[source_select, channel_select], outputs=plot)
            btn_fullscreen.click(
                None, None, None,
                js=f"""() => {{
                    const el = document.getElementById('{VISUALIZER_PLOT_ELEM_ID}');
                    if (el && el.requestFullscreen) {{ el.requestFullscreen(); }}
                }}""",
            )

    def get_visual_sources(self):
        """Flat {"device tag [· sub-source]": PlasmaMemo} map of every live
        source that currently has at least one data channel to plot."""
        sources = {}
        for dev in self.available_devices:
            for name, memo in dev.get_sources().items():
                if memo.channels:
                    # label = name if name == dev.tag else f"{dev.tag} · {name}"
                    label = name
                    sources[label] = memo
        return sources

    def refresh_visual_sources(self):
        names = list(self.get_visual_sources().keys())
        return gr.Dropdown(choices=names, value=names)

    def refresh_channels(self, selected_sources):
        sources = self.get_visual_sources()
        groups = []
        for name in selected_sources or []:
            memo = sources.get(name)
            if memo is None:
                continue
            for ch in memo.channels:
                g = _group_for_channel(ch)
                if g not in groups:
                    groups.append(g)
        return gr.CheckboxGroup(choices=groups, value=groups)

    def update_plot(self, selected_sources, selected_groups):
        sources = self.get_visual_sources()
        groups = selected_groups or []

        if not groups or not selected_sources:
            fig = go.Figure()
            fig.update_layout(
                title="Select a data source and channel(s) to visualize",
                height=300,
                uirevision="plasma-visualizer",
            )
            return fig

        fig = make_subplots(rows=len(groups), cols=1, shared_xaxes=True, vertical_spacing=0.015)

        for row, group in enumerate(groups, start=1):
            for ch in CHANNEL_GROUPS.get(group, [group]):
                for src_name in selected_sources:
                    memo = sources.get(src_name)
                    if memo is None or ch not in memo.channels:
                        continue
                    x, y = memo.get_series(ch)
                    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=f"{src_name} · {ch}"), row=row, col=1)

            fig.update_yaxes(title_text=group, title_standoff=4, row=row, col=1)
            # only the bottom-most subplot needs an x-axis title/tick labels;
            # the rest just waste vertical space repeating the same axis
            if row == len(groups):
                fig.update_xaxes(title_text="Time since session start (s)", row=row, col=1)
            else:
                fig.update_xaxes(showticklabels=False, row=row, col=1)

        fig.update_layout(
            height=max(150 * len(groups), 200),
            margin=dict(l=50, r=20, t=10, b=30),
            showlegend=True,
            uirevision="plasma-visualizer",
        )
        return fig

    def interface(self):
        with gr.Row():
            with gr.Column():
                with gr.Accordion(label="Session info", open=True):
                    default_sub = "sub-1000"
                    default_ses = "ses-00"

                    with gr.Row():
                        sub_name = gr.Text(default_sub, label="Subject ID", info="Format: sub-XXXX, X is integer")
                        ses_name = gr.Text(default_ses, label="Session ID", info="Format: ses-YY, Y is integer")
                        subject_enc = gr.Number(self.get_participant_encoding(default_sub, default_ses), label='Participant encoding (Read-only)', interactive=False,
                                                info="Format: XXXXYY")
                        sub_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        ses_name.change(self.get_participant_encoding, inputs=[sub_name, ses_name], outputs=subject_enc)
                        _ = self.get_participant_encoding(default_sub, default_ses)


                with gr.Accordion(label="Device initialization", open=True):
                    device_grp = gr.CheckboxGroup(choices=self.device_list, value=self.device_list, label="Select sensor(s)")
                    with gr.Row():
                        btn_init = gr.Button("🚦Initialize selected device(s)")
                        btn_refresh = gr.Button("Refresh list")

                    btn_init.click(self.init_devices, inputs=device_grp)
                    btn_refresh.click(self._refresh_devices, outputs=device_grp)

            with gr.Column():
                with gr.Accordion(label="Device control", open=True):
                
                    with gr.Row():
                        self.btn_start = gr.Button("Start▶️")
                        self.btn_stop = gr.Button("Stop🛑")
                                        
                    self.btn_start.click(self.start_collection)
                    self.btn_stop.click(self.stop_collection)

                self.params = {"Memo": {"type": "welcome!"}}
                params = gr.ParamViewer(self.params)
                timer = gr.Timer(value=1)
                timer.tick(fn=self.update_params, outputs=params)

        # with gr.Accordion("Help", open=False):
        #     with open("./plasma/help.md") as f:
        #         help_txt = f.read()
        #         # print(help_txt)
        #     md = gr.Markdown(help_txt)

    def _refresh_devices(self):
        active = list(device_config.get_active_table().keys())
        return gr.CheckboxGroup(choices=active, value=active)

    def init_devices(self, selected_devices):
        self.available_devices = []
        active_table = device_config.get_active_table()
        for dev in selected_devices:
            cls = active_table[dev]
            print(dev, cls)
            module = importlib.import_module(cls['module'])
            Device = getattr(module, cls['class'])
            device_instance = Device(self.session_info, self.logger, tag=dev)
            self.available_devices.append(device_instance)

        self.sts = "Ready to start"


    def start_collection(self):
        for dev in self.available_devices:
            dev.start()
        self.sts = "Collection in progress"

    def stop_collection(self):
        for dev in self.available_devices:
            dev.stop()
        self.sts = "Collection stopped"

    def update_params(self):
        params = {
            self.sts: {"type": f"{self.session_info['sub_id']} {self.session_info['ses_id']}",
                     "description": self.session_info['log_dir']}
        }

        for dev in self.available_devices:
            if isinstance(dev.memo, dict):
                for k, v in dev.memo.items():
                    params[f"- {k}"] = v.get_sts()
            else:
                params[f"- {dev.memo.name}"] = dev.memo.get_sts()

        # print(params)
        return params


    def get_participant_encoding(self, sub, ses):
        integer_representation = encode_participant(sub, ses)

        # print(name, integer_representation)
        self.participant_byte = struct.pack("<I", integer_representation)
        self.session_info = {
            'sub_id': sub,
            'ses_id': ses,
            'participant_enc': integer_representation,
            'log_dir': os.path.join(self.log_root, sub, ses)
        }
        return integer_representation


def get_logger(yams_dir="data"):
    # current YYMMDD
    now = datetime.datetime.now()
    date = now.strftime("%Y-%m-%d")

    # init logger
    logger = logging.getLogger(__name__)
    os.makedirs(yams_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, 
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(yams_dir, f"{date}_plasma_session.log")),
                            logging.StreamHandler()
                        ])
    return logger