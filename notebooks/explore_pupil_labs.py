# %%
import matplotlib.pyplot as plt
from pupil_labs.realtime_api.simple import discover_one_device, Device
import asyncio

async def get_fm():
    device = Device(address="192.168.50.167", port=8080)
    fm = device.receive_matched_scene_and_eyes_video_frames_and_gaze()


asyncio.run(get_fm)