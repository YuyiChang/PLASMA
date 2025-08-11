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
    # 'Pupil Lab Eye Event Fixation': {
    #     'module': 'plasma.devices.pupil_labs',
    #     'class': 'PupilLabsEyeEventFixation'
    # }
}

MSENSE_DEV = {
    "MSense Left 74N": "D3:54:EB:A4:9B:82",
    "Msense Right 70N": "FF:7D:06:B4:51:98",
}

IP_QB2_LIDAR = "192.168.50.35"
IP_PUPIL_LABS = "192.168.50.167"

