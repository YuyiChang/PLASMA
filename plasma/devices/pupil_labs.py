from pupil_labs.realtime_api.simple import discover_one_device
import gradio as gr
from plasma.devices.template import PlasmaDevice
from pylsl import StreamInfo, StreamOutlet

class PupilLabsIMU(PlasmaDevice):
    def __init__(self, session_info):
        super().__init__(session_info)
        self.name = 'Pupil Labs IMU'

        self.device = discover_one_device(max_search_duration_seconds=10)
        if self.device is None:
            gr.Error("No device found.")
            self.sts = "❌ Fault"
            # raise SystemExit()

        info = StreamInfo('pupil_labs_imu', 'pupillabs', 11, 8)
        self.outlet = StreamOutlet(info)

    def streaming(self):
        while True:
            # gaze
            # gaze = self.device.receive_gaze_datum()

            # IMU
            imu = self.device.receive_imu_datum()
            imu_data = [imu.timestamp_unix_seconds, 
                        imu.accel_data.x, imu.accel_data.y, imu.accel_data.z, 
                        imu.gyro_data.x, imu.gyro_data.y, imu.gyro_data.z,
                        imu.quaternion.w, imu.quaternion.x, imu.quaternion.y, imu.quaternion.z]
            self.outlet.push_sample(imu_data)
        

    def stop(self):
        self._stop_event.set()

        if "device" in locals() and self.device:
            self.device.close()
        self.sts = "🟥"
