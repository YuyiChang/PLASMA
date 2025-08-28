import gradio as gr 

__version__ = "0.1.0-beta"
__data_dir__ = "data"

device_table = {
    # 'test': {
    #     'module': 'plasma.devices.template',
    #     'class': 'PlasmaDevice'
    # },
     'MSense Wristbands': {
        'module': 'plasma.devices.msense',
        'class': 'MotionSenseHRV'
    },
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
    'ShimmerGSR': {
        'module': 'plasma.devices.shimmer',
        'class': 'ShimmerGSR'
    },
    'OBS Recorder': {
        'module': 'plasma.devices.obs',
        'class': 'ObsRecorder'
    }
}

MSENSE_DEV = {
    "MSense Left 74N": "D3:54:EB:A4:9B:82",
    "Msense Right 70N": "FF:7D:06:B4:51:98",
}

IP_QB2_LIDAR = "192.168.50.35"
IP_PUPIL_LABS = "192.168.50.167"


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
        print(f"IP of Qb2 LiDAR = {self.ip_lidar}")
        print(f"IP of Pupil Labs = {self.ip_pupil_labs}")


####
plasma_config = Config()
