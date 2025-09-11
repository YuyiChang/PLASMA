"""
This Python application provides a real-time interface for streaming and visualizing 
Shimmer GSR (Galvanic Skin Response) data via LSL. The app runs a Gradio-based UI 
with three tabs:  

1. Streaming Control: Start or stop the Shimmer data stream.  
2. Plots: View a real-time streaming status indicator and selectively display 
   GSR_RAW and/or ADC_RAW plots using checkboxes. Each plot shows the last 10 seconds 
   of data, updated every 0.5 seconds, with time (s) on the x-axis.  
3. COM Port: Set or change the serial port used to connect to the Shimmer device.  

The data is streamed to an LSL outlet with two channels (GSR_RAW and ADC_RAW), and 
plots refresh automatically to reflect the most recent measurements.  

Created by Fang Yu Chang @ UCLA with assistance from ChatGPT - September 10, 2025
Credit: Suggestions and guidance by Yuyi Chang @ OSU
"""

import time
from pylsl import StreamInfo, StreamOutlet
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType
from serial import Serial
import gradio as gr
from collections import deque
import pandas as pd

# -----------------------
# Global variables
# -----------------------
outlet = None
shim_dev = None
streaming = False
com_port_global = "COM5"  # default COM port
sampling_rate = 102.4     # Shimmer sampling rate
max_points = int(sampling_rate * 10)  # keep last 10 seconds of data
data_buffer = deque(maxlen=max_points)  # store recent samples

# Initialize LSL stream
info = StreamInfo('ShimmerGSR', 'GSR', 2, sampling_rate, 'int32', 'shimmer_gsr_01')
outlet = StreamOutlet(info)

# -----------------------
# Callback for Shimmer packets
# -----------------------
def handler(pkt: DataPacket):
    global data_buffer
    gsr_raw = pkt[EChannelType.GSR_RAW]
    adc_raw = gsr_raw & 0x0FFF
    outlet.push_sample([gsr_raw, adc_raw])
    data_buffer.append([gsr_raw, adc_raw])

# -----------------------
# Functions for UI
# -----------------------
def set_com_port(port):
    global com_port_global
    com_port_global = port
    return f"COM port set to {port}"

def start_stream():
    global shim_dev, streaming, com_port_global
    if streaming:
        return "Already streaming"
    
    ser = Serial(com_port_global, DEFAULT_BAUDRATE)
    shim_dev = ShimmerBluetooth(ser)
    shim_dev.initialize()
    shim_dev.add_stream_callback(handler)
    shim_dev.start_streaming()
    streaming = True
    return f"Streaming started on {com_port_global}"

def stop_stream():
    global shim_dev, streaming
    if not streaming:
        return "Not streaming"
    shim_dev.stop_streaming()
    shim_dev.shutdown()  # closes the serial port
    streaming = False
    return "Streaming stopped"

def get_dataframe():
    buffer_list = list(data_buffer)
    if not buffer_list:
        return pd.DataFrame({'Time': [], 'GSR_RAW': [], 'ADC_RAW': []})
    data_array = list(zip(*buffer_list))
    t = [i / sampling_rate for i in range(len(buffer_list))]
    df = pd.DataFrame({'Time': t, 'GSR_RAW': data_array[0], 'ADC_RAW': data_array[1]})
    return df

def get_streaming_status():
    return "ON" if streaming else "OFF"

# -----------------------
# Helper functions for LinePlot
# -----------------------
def gsr_data():
    df = get_dataframe()
    return pd.DataFrame({'x': df['Time'], 'y': df['GSR_RAW']})

def adc_data():
    df = get_dataframe()
    return pd.DataFrame({'x': df['Time'], 'y': df['ADC_RAW']})

# -----------------------
# Gradio UI
# -----------------------
with gr.Blocks() as demo:
    # Tab 1: Streaming Control
    with gr.Tab("Streaming Control"):
        start_btn = gr.Button("Start Streaming")
        stop_btn = gr.Button("Stop Streaming")
        control_status = gr.Textbox(label="Status", interactive=False)
        start_btn.click(start_stream, outputs=control_status)
        stop_btn.click(stop_stream, outputs=control_status)

    # Tab 2: Combined Plots with Checkboxes
    with gr.Tab("Plots"):
        # Row 1: Streaming status
        status_text = gr.Textbox(label="Streaming Status", interactive=False, value=get_streaming_status())
        status_timer = gr.Timer(value=0.5)
        status_timer.tick(lambda: get_streaming_status(), outputs=[status_text])

        # Row 2: Checkboxes
        with gr.Row():
            gsr_checkbox = gr.Checkbox(label="Show GSR_RAW", value=False)
            adc_checkbox = gr.Checkbox(label="Show ADC_RAW", value=False)

        # Row 3: GSR_RAW plot (visible when checkbox checked)
        gsr_plot = gr.LinePlot(
            gsr_data,
            every=gr.Timer(0.5),
            x="x",
            y="y",
            y_title="GSR_RAW",
            overlay_point=True,
            width=700,
            height=300,
            visible=False  # start hidden
        )

        # Row 4: ADC_RAW plot (visible when checkbox checked)
        adc_plot = gr.LinePlot(
            adc_data,
            every=gr.Timer(0.5),
            x="x",
            y="y",
            y_title="ADC_RAW",
            overlay_point=True,
            width=700,
            height=300,
            visible=False  # start hidden
        )

        # Show/hide plots based on checkboxes
        def toggle_gsr(show):
            return gr.update(visible=show)

        def toggle_adc(show):
            return gr.update(visible=show)

        gsr_checkbox.change(toggle_gsr, inputs=gsr_checkbox, outputs=gsr_plot)
        adc_checkbox.change(toggle_adc, inputs=adc_checkbox, outputs=adc_plot)

    # Tab 3: COM Port
    with gr.Tab("COM Port"):
        com_input = gr.Textbox(label="Enter COM Port", value=com_port_global)
        com_btn = gr.Button("Set COM Port")
        com_status = gr.Textbox(label="COM Status", interactive=False)
        com_btn.click(set_com_port, inputs=com_input, outputs=com_status)

demo.queue().launch()
