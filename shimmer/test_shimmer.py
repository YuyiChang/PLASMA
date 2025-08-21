import time
from serial import Serial
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType

def handler(pkt: DataPacket) -> None:
    # Access the GSR raw data channel
    gsr_raw_value = pkt[EChannelType.GSR_RAW]
    print(f'Received new GSR raw data point: {gsr_raw_value}')

if __name__ == '__main__':
    serial = Serial('COM5', DEFAULT_BAUDRATE)
    shim_dev = ShimmerBluetooth(serial)

    shim_dev.initialize()

    dev_name = shim_dev.get_device_name()
    print(f'My name is: {dev_name}')

    shim_dev.add_stream_callback(handler)

    shim_dev.start_streaming()
    time.sleep(5.0)
    shim_dev.stop_streaming()

    shim_dev.shutdown()

