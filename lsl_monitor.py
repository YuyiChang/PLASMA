"""
LSL Stream Monitor
------------------

This script provides a real-time graphical interface for monitoring active Lab Streaming Layer (LSL) streams.

Functionality:
- Continuously scans for available LSL streams every 10 seconds.
- Displays stream details in a live-updating Gradio dashboard:
    * Stream Name
    * Type
    * Host
    * Number of Channels
    * Sampling Rate (Hz)
    * Status (✅ active / ❌ inactive)
    * Status Change indicator (🟢 New / 🔴 Removed)
- Highlights newly appeared or recently disconnected streams.
- Allows manual refresh via a "Refresh Now" button.
- Provides automatic periodic refresh through a Gradio Timer.

Implementation details:
- Uses `pylsl.resolve_streams()` to detect all current streams.
- Compares with previous scan results to detect additions/removals.
- Maintains thread-safe shared state for stream data and status messages.
- Runs as a Gradio web app that can be launched locally or on a server.

Dependencies:
- gradio ≥ 4.0
- pylsl
- pandas

Author: Fang Yu Chang @ UCLA (Oct 16, 2025)
Disclaimer: ChatGPT was used to develop this script.
"""

import gradio as gr
from pylsl import resolve_streams
import pandas as pd
import time
import threading

# Shared state
previous_streams = set()
current_df = pd.DataFrame()
current_status = "Starting..."
lock = threading.Lock()

def get_stream_status():
    """Scan all available LSL streams and detect newly appeared/disappeared streams."""
    global previous_streams

    try:
        streams = resolve_streams(wait_time=1.0)
    except Exception as e:
        print(f"Error resolving streams: {e}")
        return pd.DataFrame([{
            "Stream Name": "Error resolving streams",
            "Type": str(e),
            "Host": "N/A",
            "Channels": "N/A",
            "Sampling Rate (Hz)": "N/A",
            "On LSL": "❌",
            "Status Change": "-"
        }])

    current_set = set()
    data = []

    for info in streams:
        stream_id = (info.name(), info.type(), info.hostname())
        current_set.add(stream_id)
        status_change = "🟢 New" if stream_id not in previous_streams else "-"
        data.append({
            "Stream Name": info.name(),
            "Type": info.type(),
            "Host": info.hostname(),
            "Channels": info.channel_count(),
            "Sampling Rate (Hz)": info.nominal_srate(),
            "On LSL": "✅",
            "Status Change": status_change
        })

    # Detect disappeared streams
    disappeared = previous_streams - current_set
    for name, type_, host in disappeared:
        data.append({
            "Stream Name": name,
            "Type": type_,
            "Host": host,
            "Channels": "-",
            "Sampling Rate (Hz)": "-",
            "On LSL": "❌",
            "Status Change": "🔴 Removed"
        })

    previous_streams = current_set

    if not data:
        data.append({
            "Stream Name": "No streams found",
            "Type": "-",
            "Host": "-",
            "Channels": "-",
            "Sampling Rate (Hz)": "-",
            "On LSL": "❌",
            "Status Change": "-"
        })

    return pd.DataFrame(data)

def manual_refresh():
    """Manual refresh or timer refresh action."""
    df = get_stream_status()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        global current_df, current_status
        current_df = df
        current_status = f"Last refreshed: {timestamp}"
    return current_df, current_status

# --- Gradio UI ---
with gr.Blocks(title="LSL Stream Monitor") as demo:
    gr.Markdown("## 🧠 LSL Stream Monitor")
    gr.Markdown("Displays currently available LSL streams and highlights new or removed streams since last refresh.\n\n🟢 New  🔴 Removed")

    refresh_btn = gr.Button("🔄 Refresh Now")
    stream_table = gr.DataFrame(
        headers=["Stream Name", "Type", "Host", "Channels", "Sampling Rate (Hz)", "On LSL", "Status Change"],
        interactive=False
    )
    status_text = gr.Textbox(label="Status", interactive=False)

    # Initial load
    demo.load(manual_refresh, None, [stream_table, status_text])
    refresh_btn.click(manual_refresh, None, [stream_table, status_text])

    # ⏱️ Automatic refresh every 10 seconds (Gradio ≥4 syntax)
    auto_timer = gr.Timer(10.0)
    auto_timer.tick(manual_refresh, None, [stream_table, status_text])

if __name__ == "__main__":
    demo.launch()
