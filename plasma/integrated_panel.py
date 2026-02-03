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
import time

# class UpdateFunction():
#     def __init__(self):
#         self.fn = self.default_fn
#         self.max_channels = 6
#         self.channels = [f"ch{i+1}" for i in range(self.max_channels)]
#         self. available_devices = []
#         self.available_fns = {
#             'Bitalino' : self.bitalino_update_data, 
#             'MotionSENSE HRV wristband' : self.MotionSENSE_HRV_wristband_update_data
#             }
#     def init_update_fn(self, available_devices):
#         self.available_devices=available_devices

#     def switch_device(self, device):
#         try:
#             self.fn = self.available_fns[device] 
#         except:
#             self.fn = self.default_fn
#             print("device not supported or not initalized yet")
    
#     def default_fn(self):

#         samples = 100
#         print("running default update function")

#         # Construct long-form dataframe
#         df = pd.DataFrame({
#             "index": np.tile(np.arange(samples), self.max_channels),
#             "data": np.concatenate([
#                 np.sin(np.linspace(0, np.pi * 2, samples) + i) * 0.5 + i
#                 for i in range(self.max_channels)
#             ]),
#             "channel": np.repeat(self.channels, samples)
#         })

#         return df

#     def bitalino_update_data(self):

#         print("running bitalino update function")

#         length = 0
#         readings = np.zeros(0, dtype=np.int64)

#         for dev in self.available_devices:
#             if 'Bitalino' in dev.tag:
#                 length = dev.n_samples
#                 for reading in dev.memo.data:
#                     data = np.zeros((dev.n_samples, self.max_channels), dtype = np.int64)
#                     if isinstance(reading, np.ndarray):
#                         data = reading[:, 5:11]        
#                     data = data.flatten(order='F')
#                     readings = np.concatenate((readings, data))
#                 break
        
#         df = pd.DataFrame(data={
#             'index': np.tile(np.arange(100 * length), self.max_channels),
#             'data': readings,
#             'channel': np.tile(np.repeat(self.channels, length), 100)
#         })
        
#         return df
        

#     def MotionSENSE_HRV_wristband_update_data(self):
#         # very dummy way of pulling data to be visualized

#         print("running Motion sense update function")

#         sel_dev = None
#         data = np.arange(100) / 100

#         for dev in self.available_devices:
#             if "SENSE" in dev.tag:
#                 sel_dev = dev
#                 data = np.array(sel_dev.memo['MSense Left 01S'].data)
#                 print('=====', data.shape)
#                 break

#         df = pd.DataFrame(data={
#             'index': np.arange(100),
#             'data': data,
#             'channel' : ['ch1'] * 100
#         })
#         return df
        

class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_table.keys())
        self.log_root = __data_dir__

        self.available_devices = []

        self.sts = "Welcome"

        self.logger = get_logger(__data_dir__)
        self.logger.info(f"Begin PLASMA v{__version__} session log")

        #self.update = UpdateFunction()
        self.current_dev = None
        self.channel = np.array(['ch1'] * 100)
        self.selected_channel = []
        self.index = np.arange(100)
        

    def visualizer_interface(self):
        with gr.Row():
            with gr.Column(scale=1):
                # refresh = gr.Button("Refresh available devices")
                radio = gr.Radio(self.device_list, label="devices")
                timer = gr.Timer(0.1)  # seconds
                radio.select(self.select_device)

                # allows the page to appear with the number of available channels for the device
                @gr.render(inputs = radio)
                def draw_checkboxes(dev):
                    if not dev:
                        gr.Markdown("## no channels available")
                    else:
                        #find the dev in available devices and read the number of channels they have 
                        #TODO: this code could replace some other functions, optimize if possible
                        for i in self.available_devices:
                            if dev in i.tag:
                                select_channel = gr.CheckboxGroup([f"ch{j+1}" for j in range(i.num_channel)], label="available channels")
                                select_channel.select(self.select_channel)
                                break
                
            # refresh.click(self.update_devices, outputs=checkbox_group)
            with gr.Column(scale=4):
                plot = gr.LinePlot(value=self.dynamic_update, x='index', y='data', color='channel', every=timer)

    def select_device(self, evt: gr.SelectData):
        self.current_dev = evt.value
        self.init_update_fn()
    
    def select_channel(self, evt: gr.SelectData):
        if evt.value not in self.selected_channel:
            self.selected_channel.append(evt.value)
        else:
            self.selected_channel.remove(evt.value)
        self.init_update_fn()

    def init_update_fn(self):
        if len(self.selected_channel) > 0:
            self.index = np.tile(np.arange(100), len(self.selected_channel))
            self.channel = np.concatenate([np.tile(ch, 100) for ch in self.selected_channel])
            
        
        # length = 1
        # num_channel = 1

        # #get information that are specific to the device 
        # for dev in self.available_devices:
        #     if self.current_dev in dev.tag:
        #         length = dev.data_length
        #         num_channel = dev.num_channel
        #         break
        
        # #ensures channel is [ch1, ch1, ..., ch1, ch2, ch2, ..., ch2, ch3...] * 100
        # self.channel = [f"ch{i+1}" for i in range(num_channel)]
        # self.channel = np.tile(np.repeat(self.channel, length), 100)

        # #Example: length = 3, num_channel = 2
        # #ensures index is [1, 2, 3, 1, 2, 3, 4, 5, 6, 4, 5, 6, ..., 97,98,100,97,98,100]
        # chunks = np.array_split(np.arange(100 * length), 100)
        # self.index = [np.tile(section, num_channel) for section in chunks]
        # self.index = np.concatenate(self.index)

    
    def dynamic_update(self):
        if not self.current_dev or len(self.selected_channel) <= 0:
            return self.dummy_update()
        
        data = np.arange(100) / 100

        for dev in self.available_devices:
            if self.current_dev in dev.tag:
                data = [np.array(v) for k,v in dev.memo.data.items() if k in self.selected_channel]
                data = np.concatenate(data)
                break

        try:
            df = pd.DataFrame(data={
                'index': self.index,
                'data': data,
                'channel': self.channel
            })

        except:
            #redo initialization if the user initalizes device after they initialize update function
            self.init_update_fn()
            df = pd.DataFrame(data={
                'index': self.index,
                'data': data,
                'channel': self.channel
            })
        
        return df

    def dummy_update(self):
        """
        This dummpy update function is NECESSARY for initalizing all channels at the beginning of the app
        DO NOT DELETE until a better solution is found
        """
        samples = 100
        max_channels = 6
        channels = [f"ch{i+1}" for i in range(max_channels)]
        #print("running default update function")

        # Construct long-form dataframe
        df = pd.DataFrame({
            "index": np.tile(np.arange(samples), max_channels),
            "data": np.concatenate([
                np.sin(np.linspace(0, np.pi * 2, samples) + i) * 0.5 + i
                for i in range(max_channels)
            ]),
            "channel": np.repeat(channels, samples)
        })

        return df
        

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
        
        #self.update.init_update_fn(self.available_devices)
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
