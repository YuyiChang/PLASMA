import gradio as gr
import obsws_python as obs
import threading

# --- CONFIG: OBS instances ---
OBS_INSTANCES = [
    {"host": "127.0.0.1", "port": 4455, "password": "local_password"}, # streamer laptop
    {"host": "192.168.1.184", "port": 4455, "password": "remote_password"}, # psychopy laptop
]

# Global variable to store connected clients
clients = []

def connect_all_obs():
    """Connect to all OBS instances"""
    global clients
    clients = []
    for cfg in OBS_INSTANCES:
        try:
            client = obs.ReqClient(host=cfg["host"], port=cfg["port"], password=cfg["password"], timeout=5)
            clients.append(client)
        except Exception as e:
            print(f"Failed to connect to OBS at {cfg['host']}: {e}")

def start_recording():
    """Start recording on all connected OBS"""
    if not clients:
        connect_all_obs()
    for client in clients:
        try:
            client.start_record()
        except Exception as e:
            print(f"Failed to start recording on {client.host}: {e}")
    return "✅ Recording started on all OBS instances"

def stop_recording():
    """Stop recording on all connected OBS"""
    if not clients:
        connect_all_obs()
    for client in clients:
        try:
            client.stop_record()
        except Exception as e:
            print(f"Failed to stop recording on {client.host}: {e}")
    return "✅ Recording stopped on all OBS instances"

# Optional: run start/stop in a separate thread to avoid blocking the UI
def threaded_start():
    threading.Thread(target=start_recording).start()
    return "Starting recording..."

def threaded_stop():
    threading.Thread(target=stop_recording).start()
    return "Stopping recording..."

# --- GRADIO INTERFACE ---
with gr.Blocks() as demo:
    gr.Markdown("## Multi-OBS Recorder Control")
    with gr.Row():
        start_btn = gr.Button("Start Recording")
        stop_btn = gr.Button("Stop Recording")
    output = gr.Textbox(label="Status", interactive=False)

    start_btn.click(threaded_start, [], output)
    stop_btn.click(threaded_stop, [], output)

demo.launch()
