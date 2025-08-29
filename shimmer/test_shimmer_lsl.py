import time
from serial import Serial
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType
from pylsl import StreamInfo, StreamOutlet

def handler(pkt: DataPacket) -> None:
    """Callback for each new Shimmer data packet."""
    try:
        gsr_raw_value = pkt[EChannelType.GSR_RAW]
        outlet.push_sample([gsr_raw_value])
        print(f"Sent GSR raw value: {gsr_raw_value}")
    except KeyError:
        # Ignore packets without GSR data
        pass

if __name__ == "__main__":
    # Create LSL stream (continuous until script is stopped)
    info = StreamInfo("ShimmerGSR", "GSR", 1, 102.4, "int32", "shimmer_gsr_01")
    outlet = StreamOutlet(info)

    # Open serial connection to Shimmer
    ser = Serial("COM5", DEFAULT_BAUDRATE)  # adjust COM port for your system
    shim_dev = ShimmerBluetooth(ser)

    # Initialize device
    shim_dev.initialize()
    shim_dev.set_sampling_rate(102.4)   # configure to 102.4 Hz
    print(f"Connected to Shimmer device: {shim_dev.get_device_name()}")

    # Register callback and start streaming
    shim_dev.add_stream_callback(handler)
    shim_dev.start_streaming()

    print("Streaming GSR... press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)  # keep script alive
    except KeyboardInterrupt:
        print("\nStopping stream...")

    # Stop streaming and close connection
    shim_dev.stop_streaming()
    shim_dev.shutdown()
    ser.close()
    print("Shimmer disconnected.")
