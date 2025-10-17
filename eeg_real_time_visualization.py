"""
MBT EEG Live Viewer
-------------------

This script provides a real-time EEG data visualization interface for MBT amplifiers
using the Lab Streaming Layer (LSL) protocol. It connects to an available EEG LSL
stream and dynamically plots multi-channel signals in a Gradio web dashboard.

Functionality:
- Automatically discovers available EEG streams via LSL.
- Connects to the first detected EEG stream and retrieves its metadata.
- Continuously reads incoming EEG samples in a background thread.
- Maintains a rolling buffer (deque) of the most recent samples for each channel.
- Displays live-updating plots for up to 42 EEG channels.
- Allows users to toggle which channels are displayed via checkboxes.
- Provides connection status feedback (e.g., number of detected channels).

Implementation details:
- Uses `pylsl.StreamInlet` to pull EEG samples in real time.
- Data from each channel is stored in a fixed-length buffer (`collections.deque`).
- Each channel’s data is converted to a `pandas.DataFrame` for plotting.
- Visualization is handled through Gradio `LinePlot` components, each refreshed at 20 Hz (REFRESH_INTERVAL = 0.05 s).
- Thread locking ensures safe concurrent access to EEG buffers.

Limitations:
- Currently displays only the first 42 channels (hardcoded upper bound).
- Assumes at least one EEG-type LSL stream is available.
- Designed for quick inspection rather than long-term recording.

Dependencies:
- gradio ≥ 4.0
- pylsl
- pandas
- numpy

Usage:
1. Run the script.
2. Click “Connect to EEG Stream” in the browser UI.
3. Select channels via checkboxes to visualize their live EEG traces.

Author: Fang Yu Chang @ UCLA (Oct 16, 2025)
Disclaimer: ChatGPT was used to develop this script.
"""

import time
from pylsl import resolve_streams, StreamInlet
import gradio as gr
import pandas as pd
from collections import deque
import threading
import numpy as np

# -----------------------
# Global variables
# -----------------------
eeg_inlet = None
num_channels = 0
BUFFER_LENGTH = 250  # samples to keep (like last ~1-2 seconds)
eeg_buffers = None   # deque per channel
streaming = False
lock = threading.Lock()
REFRESH_INTERVAL = 0.05  # seconds

# -----------------------
# Connect to EEG via LSL
# -----------------------
def connect_eeg():
    global eeg_inlet, num_channels, eeg_buffers, streaming
    streams = resolve_streams()
    eeg_streams = [s for s in streams if s.type() == 'EEG']
    if not eeg_streams:
        return "No EEG streams found"
    
    eeg_inlet = StreamInlet(eeg_streams[0])
    num_channels = eeg_inlet.channel_count
    eeg_buffers = [deque(maxlen=BUFFER_LENGTH) for _ in range(num_channels)]
    streaming = True
    threading.Thread(target=background_loop, daemon=True).start()

    if num_channels < 42:
        return f"Connected: {num_channels} channels (showing first {num_channels})"
    elif num_channels > 42:
        return f"Connected: {num_channels} channels (showing first 42)"
    else:
        return f"Connected to EEG stream with {num_channels} channels"


# -----------------------
# Background thread: pull samples continuously
# -----------------------
def background_loop():
    global eeg_buffers
    while streaming:
        sample, ts = eeg_inlet.pull_sample(timeout=0.001)
        if sample:
            with lock:
                for i in range(num_channels):
                    eeg_buffers[i].append(sample[i])
        time.sleep(REFRESH_INTERVAL)

# -----------------------
# Prepare DataFrame for LinePlot
# -----------------------
def get_eeg_dataframe(channel_idx=0):
    with lock:
        if not eeg_buffers or not eeg_buffers[channel_idx]:
            return pd.DataFrame({'x': [], 'y': []})
        y = list(eeg_buffers[channel_idx])
        x = np.arange(len(y)) * REFRESH_INTERVAL  # approximate time axis
        return pd.DataFrame({'x': x, 'y': y})

# -----------------------
# Gradio UI
# -----------------------
with gr.Blocks(title="MBT EEG Live Viewer") as demo:

    gr.Markdown("## 🧠 MBT EEG Live Viewer")
    gr.Markdown("Displays live multi-channel EEG from MBT amplifier via LSL.\nSelect channels to plot.")

    # --- Streaming control ---
    with gr.Row():
        connect_btn = gr.Button("Connect to EEG Stream")
        status_text = gr.Textbox(label="Status", interactive=False)
        connect_btn.click(connect_eeg, outputs=status_text)

    # --- Channel selection ---
    channels_input = gr.CheckboxGroup(
        label="Select channels to display",
        choices=[f"Ch{i+1}" for i in range(42)],  # default 32 channels; update after connection
        value=[]
    )

    # --- LinePlots for each channel ---
    plots = []
    for i in range(42):
        plot = gr.LinePlot(
            lambda idx=i: get_eeg_dataframe(idx),
            every=gr.Timer(REFRESH_INTERVAL),
            x="x",
            y="y",
            y_title=f"Ch{i+1}",
            overlay_point=True,
            width=700,
            height=150,
            visible=False
        )
        plots.append(plot)

    # --- Show/hide channels based on checkbox ---
    def toggle_channels(selected):
        updates = []
        for i in range(42):
            updates.append(gr.update(visible=f"Ch{i+1}" in selected))
        return updates

    channels_input.change(toggle_channels, inputs=channels_input, outputs=plots)

demo.queue().launch()
