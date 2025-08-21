from datetime import datetime, timezone
from pupil_labs.realtime_api.simple import discover_one_device, Device
from pupil_labs.realtime_api.streaming.eye_events import (
    BlinkEventData,
    FixationEventData,
)
import gradio as gr
from plasma.devices.template import PlasmaDevice, PlasmaMemo
from pylsl import StreamInfo, StreamOutlet, cf_string
# from plasma.config import IP_PUPIL_LABS
# from plasma.config import plasma_config
import plasma.config as c
import time
import cv2
from importlib import reload

from plasma.devices import *

ip = c.plasma_config.ip_pupil_labs

class PupilLabsDashboard():
    def __init__(self):
        self.device = Device(address=ip, port="8080")

    def interface(self):        
        img = gr.Image(streaming=True)
        with gr.Row():
            btn_start = gr.Button("Start")
            btn_stop = gr.Button("Stop")

        streaming = btn_start.click(self.stream_frame, outputs=img)
        stop_streaming = btn_stop.click(cancels=[streaming])

    def stream_src(self):
        while True:
            fm = self.device.receive_matched_scene_and_eyes_video_frames_and_gaze()
            im = fm.scene.bgr_pixels
            cv2.circle(im,
                       (int(fm.gaze.x), int(fm.gaze.y)),
                       radius=20,
                       color=(0, 0, 255),
                       thickness=10)
            im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
            yield im
            time.sleep(0.01)

    def stream_frame(self):
        for fm in self.stream_src():
            yield fm

class PupilLabsIMU(PlasmaDevice):
    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo('Pupil Labs IMU')

        ip = c.plasma_config.ip_pupil_labs
        print(f"Initializing Pupil Labs IMU at {ip}")

        # self.device = discover_one_device(max_search_duration_seconds=10)
        self.device = Device(address=ip, port="8080")
        if self.device is None:
            gr.Error("No device found.")
            self.memo.sts = "❌ Fault"
            # raise SystemExit()

        info = StreamInfo('pupil_labs_imu', 'pupillabs', 11, 8)
        self.outlet = StreamOutlet(info)

    def streaming(self):
        while not self._stop_event.is_set():
            # gaze
            # gaze = self.device.receive_gaze_datum()

            # IMU
            imu = self.device.receive_imu_datum()
            imu_data = [imu.timestamp_unix_seconds, 
                        imu.accel_data.x, imu.accel_data.y, imu.accel_data.z, 
                        imu.gyro_data.x, imu.gyro_data.y, imu.gyro_data.z,
                        imu.quaternion.w, imu.quaternion.x, imu.quaternion.y, imu.quaternion.z]
            self.outlet.push_sample(imu_data)
            self.memo.set_latest(str(imu_data))

    def stop(self):
        self._stop_event.set()

        if "device" in locals() and self.device:
            self.device.close()
        self.memo.sts = "🟥"

# def pupil_labs_video_feed():
#     with gr.Row():
#         btn_start = gr.Button("Start")
#         btn_stop = gr.Button("Stop")
#     video = gr.Video(f"http://{ip}:8080", streaming=True)

class PupilLabsEyeEventBlink(PlasmaDevice):
    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo('Pupil Labs Blink')

        ip = c.plasma_config.ip_pupil_labs
        print(f"Initializing Pupil Labs Eye Event at {ip}")

        # self.device = discover_one_device(max_search_duration_seconds=10)
        self.device = Device(address=ip, port="8080")

        print(self.device)
        if self.device is None:
            gr.Error("No device found.")
            self.memo.sts = "❌ Fault"
            # raise SystemExit()

        info = StreamInfo('pupil_labs_blink', 'pupillabs', 
                          channel_count=1, channel_format=cf_string)
        self.outlet = StreamOutlet(info)

    def streaming(self):
        while not self._stop_event.is_set():
            eye_event = self.device.receive_eye_events()

            if isinstance(eye_event, BlinkEventData):
                time_sec = eye_event.start_time_ns // 1e9
                blink_time = datetime.fromtimestamp(time_sec, timezone.utc)
                # print(f"[BLINK] blinked at {blink_time.strftime('%H:%M:%S')} UTC")

                evt = f"[BLINK] blinked at {blink_time.strftime('%H:%M:%S')} UTC"
                self.outlet.push_sample([evt])
                self.memo.set_latest(evt)

            elif isinstance(eye_event, FixationEventData) and eye_event.event_type == 0:
                angle = eye_event.amplitude_angle_deg
                # print(f"[SACCADE] event with {angle:.0f}° amplitude.")

                evt = f"[SACCADE] event with {angle:.0f}° amplitude."
                self.outlet.push_sample([evt])
                self.memo.set_latest(evt)

            elif isinstance(eye_event, FixationEventData) and eye_event.event_type == 1:
                duration = (eye_event.end_time_ns - eye_event.start_time_ns) / 1e9
                # print(f"[FIXATION] event with duration of {duration:.2f} seconds.")

                evt = f"[FIXATION] event with duration of {duration:.2f} seconds."
                self.outlet.push_sample([evt])
                self.memo.set_latest(evt)
        

    def stop(self):
        self._stop_event.set()

        if "device" in locals() and self.device:
            self.device.close()
        self.memo.sts = "🟥"
        


class PupilLabsEyeEventFixation(PlasmaDevice):
    def __init__(self, session_info, logger, tag):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo('Pupil Labs Eye Event Fixation')

        # self.device = discover_one_device(max_search_duration_seconds=10)
        self.device = Device(address=ip, port="8080")

        print(self.device)
        if self.device is None:
            gr.Error("No device found.")
            self.memo.sts = "❌ Fault"
            # raise SystemExit()

        info = StreamInfo('pupil_labs_fixation', 'pupillabs', 
                          channel_count=1, channel_format=cf_string)
        self.outlet = StreamOutlet(info)

    def streaming(self):
        while not self._stop_event.is_set():
            eye_event = self.device.receive_eye_events()

            if isinstance(eye_event, FixationEventData) and eye_event.event_type == 0:
                angle = eye_event.amplitude_angle_deg
                evt = f"[SACCADE] event with {angle:.0f}° amplitude"
                self.outlet.push_sample([evt])
                self.memo.set_latest(evt)

            elif isinstance(eye_event, FixationEventData) and eye_event.event_type == 1:
                duration = (eye_event.end_time_ns - eye_event.start_time_ns) / 1e9
                evt = f"[FIXATION] event with duration of {duration:.2f} seconds."
                self.outlet.push_sample([evt])
                self.memo.set_latest(evt)
        

    def stop(self):
        self._stop_event.set()

        if "device" in locals() and self.device:
            self.device.close()
        self.memo.sts = "🟥"