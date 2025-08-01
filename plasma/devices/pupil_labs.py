from datetime import datetime, timezone
from pupil_labs.realtime_api.simple import discover_one_device
from pupil_labs.realtime_api.streaming.eye_events import (
    BlinkEventData,
    FixationEventData,
)
import gradio as gr
from plasma.devices.template import PlasmaDevice
from pylsl import StreamInfo, StreamOutlet, cf_string

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


class PupilLabsEyeEvent(PlasmaDevice):
    def __init__(self, session_info):
        super().__init__(session_info)
        self.name = 'Pupil Labs Eye Event'

        self.device = discover_one_device(max_search_duration_seconds=10)
        if self.device is None:
            gr.Error("No device found.")
            self.sts = "❌ Fault"
            # raise SystemExit()

        info = StreamInfo('pupil_labs_eye_event', 'pupillabs', 
                          channel_count=1, channel_format=cf_string)
        self.outlet = StreamOutlet(info)

    def streaming(self):
        while True:
            eye_event = self.device.receive_eye_events()
            if isinstance(eye_event, BlinkEventData):
                # time_sec = eye_event.start_time_ns // 1e9
                # blink_time = datetime.fromtimestamp(time_sec, timezone.utc)
                evt = f"[BLINK] blinked at {eye_event.start_time_ns} ns"

            elif isinstance(eye_event, FixationEventData) and eye_event.event_type == 0:
                angle = eye_event.amplitude_angle_deg
                evt = f"[SACCADE] event with {angle:.0f}° amplitude"

            elif isinstance(eye_event, FixationEventData) and eye_event.event_type == 1:
                duration = (eye_event.end_time_ns - eye_event.start_time_ns) / 1e9
                evt = f"[FIXATION] event with duration of {duration:.2f} seconds."
                
            print(evt)
            self.outlet.push_sample([evt])
        

    def stop(self):
        self._stop_event.set()

        if "device" in locals() and self.device:
            self.device.close()
        self.sts = "🟥"
