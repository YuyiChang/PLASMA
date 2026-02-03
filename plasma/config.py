import gradio as gr 

__version__ = "0.1.0-beta"
__data_dir__ = "data"

device_table = {
     'ShimmerGSR skin conductance': {
        'module': 'plasma.devices.template',
        'class': 'PlasmaDemoDevice'
    },
    'Camera recorder': {
        'module': 'plasma.devices.template',
        'class': 'PlasmaDemoDevice'
    },
    'Pupil Labs eye event blink': {
        'module': 'plasma.devices.template',
        'class': 'PlasmaDemoDevice'
    },
    'MotionSENSE HRV wristbands': {
        'module': 'plasma.devices.template',
        'class': 'PlasmaDemoDevice'
    },
    'mBrainTrainEEG': {
        'module': 'plasma.devices.template',
        'class': 'PlasmaDemoDevice'
    }
}

MSENSE_DEV = {
    "MSense Left": "AA:AA:AA:AA:AA:AA", 
    "MSense Right": "FF:FF:FF:FF:FF:FF",
}

IP_QB2_LIDAR = "192.168.0.100"
IP_PUPIL_LABS = "192.168.0.101"


class Config():
    def __init__(self):
        self.ip_lidar = IP_QB2_LIDAR
        self.ip_pupil_labs = IP_PUPIL_LABS

    def interface(self):
        with gr.Column():
            ip_qb2_lidar = gr.Text(value=self.ip_lidar, label="QB2 LiDAR IP Address")
            ip_qb2_lidar.change(self._update_pupil_labs, inputs=ip_qb2_lidar)

            ip_pupil_labs = gr.Text(value=self.ip_pupil_labs, label="Pupil Labs IP Address")
            ip_pupil_labs.change(self._update_pupil_labs, inputs=ip_pupil_labs)

            save = gr.Button(value="Save")
            save.click(self._save_config)

    def _update_lidar_ip(self, ip):
        self.ip_lidar = ip

    def _update_pupil_labs(self, ip):
        self.ip_pupil_labs = ip

    def _save_config(self):
        print(f"IP of Camera = {self.ip_lidar}")
        print(f"IP of EEG = {self.ip_pupil_labs}")


####
plasma_config = Config()
