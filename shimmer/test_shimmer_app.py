"""
This Python application provides a real-time interface for streaming and visualizing 
Shimmer GSR (Galvanic Skin Response) data via LSL. The app has three tabs in a Gradio UI:  

1. Streaming Control: Start or stop the Shimmer data stream.  
2. Real-time Visualization: Display the last 10 seconds of streaming data for GSR_RAW and ADC_RAW, 
   selectable via checkboxes, with streaming status indicator.  
3. COM Port: Set or change the serial port used to connect to the Shimmer device.  

The data is streamed to an LSL outlet with two channels (GSR_RAW and ADC_RAW), and the plot updates 
every 0.5 seconds to show recent measurements in seconds on the x-axis.

Created by Fang Yu Chang @ UCLA with assistance from ChatGPT - September 8, 2025
Credit: Suggestions and guidance by Yuyi Chang @ OSU
"""

import time
from pylsl import StreamInfo, StreamOutlet
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType
from serial import Serial
import gradio as gr
import matplotlib.pyplot as plt
from collections import deque

# -----------------------
# Global variables
# -----------------------
outlet = None
shim_dev = None
streaming = False
com_port_global = "COM3"  # default COM port
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
    shim_dev.shutdown()  # this already closes the serial port
    streaming = False
    return "Streaming stopped"

def plot_stream(raw_checkbox=True, adc_checkbox=True):
    plt.figure(figsize=(8,4))
    buffer_list = list(data_buffer)
    if not buffer_list:
        plt.text(0.5, 0.5, 'No data yet', ha='center')
        return plt
    data_array = list(zip(*buffer_list))
    t = [i / sampling_rate for i in range(len(buffer_list))]  # time in seconds
    lines = []
    if raw_checkbox:
        l1, = plt.plot(t, data_array[0], label='GSR_RAW')
        lines.append(l1)
    if adc_checkbox:
        l2, = plt.plot(t, data_array[1], label='ADC_RAW')
        lines.append(l2)
    if lines:
        plt.legend()
    plt.xlabel('Time (s)')
    plt.ylabel('Value')
    plt.grid(True)
    plt.tight_layout()
    return plt

def get_streaming_status():
    return "ON" if streaming else "OFF"

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

    # Tab 2: Real-time Visualization
    with gr.Tab("Real-time Visualization"):
        status_indicator = gr.Textbox(label="Streaming Status", interactive=False, value=get_streaming_status())
        raw_checkbox = gr.Checkbox(label="Show GSR_RAW", value=False)
        adc_checkbox = gr.Checkbox(label="Show ADC_RAW", value=False)
        plot_output = gr.Plot(label="Real-time Plot")

        # Timer for real-time plot
        timer = gr.Timer(value=0.5)
        def update_plot(raw_checkbox, adc_checkbox):
            return plot_stream(raw_checkbox, adc_checkbox)
        timer.tick(update_plot, inputs=[raw_checkbox, adc_checkbox], outputs=[plot_output])

        # Timer for streaming status
        status_timer = gr.Timer(value=0.5)
        status_timer.tick(lambda: get_streaming_status(), outputs=[status_indicator])

    # Tab 3: COM Port
    with gr.Tab("COM Port"):
        com_input = gr.Textbox(label="Enter COM Port", value=com_port_global)
        com_btn = gr.Button("Set COM Port")
        com_status = gr.Textbox(label="COM Status", interactive=False)
        com_btn.click(set_com_port, inputs=com_input, outputs=com_status)

demo.launch()
