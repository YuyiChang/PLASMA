import gradio as gr
import struct
import os
from plasma.lsl_session import encode_participant
import importlib
import logging, datetime
from logging import Logger
from plasma.config import device_table, __data_dir__, __version__
import pandas as pd
import numpy as np

class UpdateFunction():
    def __init__(self):
        self.fn = self.default_fn
        self.available_fns = {
            'Bitalino' : self.bitalino_update_data, 
            'MotionSENSE HRV wristband' : self.MotionSENSE_HRV_wristband_update_data
            }

    def switch_device(self, device):
        self.fn = self.available_fns[device] #temp solution
    
    def default_fn(self):
        
        df = pd.DataFrame(data={
            'index': np.arange(100),
            'data': np.arange(100)/100
        })
        return df

    def bitalino_update_data(self):

        df = pd.DataFrame(data={
            'index': np.arange(100),
            'data': np.zeros(100)
        })
        return df
        

    def MotionSENSE_HRV_wristband_update_data(self):
        # very dummy way of pulling data to be visualized
        sel_dev = None
        data = np.arange(100) / 100

        # for dev in self.available_devices:
        #     if "SENSE" in dev.tag:
        #         sel_dev = dev
        #         data = np.array(sel_dev.memo['MSense Left 01S'].data)
        #         print('=====', data.shape)
        #         break

        df = pd.DataFrame(data={
            'index': np.arange(100),
            'data': data
        })
        return df
        

class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_table.keys())
        self.log_root = __data_dir__

        self.available_devices = []

        self.sts = "Welcome"

        self.logger = get_logger(__data_dir__)
        self.logger.info(f"Begin PLASMA v{__version__} session log")

        self.update = UpdateFunction()
        

    def visualizer_interface(self):
        with gr.Row():
            # refresh = gr.Button("Refresh available devices")
            checkbox_group = gr.CheckboxGroup(self.device_list, label="devices", scale=1)
            # refresh.click(self.update_devices, outputs=checkbox_group)
            plot = gr.LinePlot(value=self.update.fn(), x='index', y='data', every=0.5, scale=4)

            checkbox_group.select(self.select_device, outputs=plot)

    def select_device(self, evt: gr.SelectData):
        self.update.switch_device(evt.value)
        return self.update.fn()

    def update_devices(self):
        aa = [dev.tag for dev in self.available_devices]
        return gr.CheckboxGroup(choices=aa)

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
                    btn_init = gr.Button("🚦Initialize selected device(s)")

                    btn_init.click(self.init_devices, inputs=device_grp)

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

    def init_devices(self, selected_devices):
        self.available_devices = []
        for dev in selected_devices:
            cls = device_table[dev]
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
