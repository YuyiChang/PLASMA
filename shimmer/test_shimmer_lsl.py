import time
from serial import Serial
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType
from pylsl import StreamInfo, StreamOutlet

# Create LSL stream with 2 channels: raw + ADC
info = StreamInfo('ShimmerGSR', 'GSR', 2, 102.4, 'int32', 'shimmer_gsr_01')
outlet = StreamOutlet(info)

def handler(pkt: DataPacket):
    """
    Callback for each new Shimmer data packet.
    Extracts the 12-bit ADC value from the 16-bit GSR_RAW output
    and pushes both the raw and ADC values to the LSL stream.

    Parameters:
    pkt (DataPacket): Incoming Shimmer data packet.
    
    Behavior:
    - gsr_raw: full 16-bit Shimmer GSR output
    - adc_raw: extracted 12-bit ADC value from gsr_raw
    - Both values are sent as a single sample [gsr_raw, adc_raw] to LSL
    """
    gsr_raw = pkt[EChannelType.GSR_RAW]      # 16-bit raw
    adc_raw = gsr_raw & 0x0FFF               # 12-bit ADC extraction
    
    # Send both values as a single LSL sample
    outlet.push_sample([gsr_raw, adc_raw])
    print(f"Raw: {gsr_raw}, ADC: {adc_raw}")

# Connect to Shimmer
ser = Serial('COM5', DEFAULT_BAUDRATE)  # use DEFAULT_BAUDRATE like the working reference
shim_dev = ShimmerBluetooth(ser)
shim_dev.initialize()
shim_dev.add_stream_callback(handler)
shim_dev.start_streaming()

print("Streaming GSR_RAW and ADC... press Ctrl+C to stop")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    shim_dev.stop_streaming()
    shim_dev.shutdown()
    ser.close()
