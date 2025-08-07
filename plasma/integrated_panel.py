import gradio as gr
import struct
import os
from plasma.lsl_session import encode_participant
import importlib

device_table = {
    # 'Pupil Labs': 'pupil_labs',
    # 'test': {
    #     'module': 'plasma.devices.template',
    #     'class': 'PlasmaDevice'
    # },
    'qb2 LiDAR': {
        'module': 'plasma.devices.qb2',
        'class': 'Qb2'
    },
    'Pupil Lab IMU': {
        'module': 'plasma.devices.pupil_labs',
        'class': 'PupilLabsIMU'
    },
    'Pupil Lab Eye Event Blink': {
        'module': 'plasma.devices.pupil_labs',
        'class': 'PupilLabsEyeEventBlink'
    },
    'Pupil Lab Eye Event Fixation': {
        'module': 'plasma.devices.pupil_labs',
        'class': 'PupilLabsEyeEventFixation'
    }
}


class IntegratedPanel():
    def __init__(self):
        self.device_list = list(device_table.keys())
        self.log_root = "./data"

        self.available_devices = []

    def interface(self):
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
            btn_init = gr.Button("Initialize selected device(s)")

            btn_init.click(self.init_devices, inputs=device_grp)

        
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

    def init_devices(self, selected_devices):
        self.available_devices = []
        for dev in selected_devices:
            cls = device_table[dev]
            print(cls)
            module = importlib.import_module(cls['module'])
            Device = getattr(module, cls['class'])
            device_instance = Device(self.session_info)
            self.available_devices.append(device_instance)


    def start_collection(self):
        for dev in self.available_devices:
            dev.start()

    def stop_collection(self):
        for dev in self.available_devices:
            dev.stop()

    def update_params(self):
        params = {
            "Memo": {"type": f"Welcome to {self.session_info['sub_id']} {self.session_info['ses_id']}",
                     "description": self.session_info['log_dir']}
        }

        for dev in self.available_devices:
            params[dev.name] = {"type": dev.sts}

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

