from plasma.devices.template import PlasmaDevice, PlasmaMemo
import time
from serial import Serial
from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType
from pylsl import StreamInfo, StreamOutlet

def handler(pkt: DataPacket) -> None:
    # Access the GSR raw data channel
    gsr_raw_value = pkt[EChannelType.GSR_RAW]
    print(f'Received new GSR raw data point: {gsr_raw_value}')
    
    # Push the GSR raw value to the LSL outlet as a single sample (list with one value)
    outlet.push_sample([gsr_raw_value])

class ShimmerGSR(PlasmaDevice):
    def __init__(self, session_info, logger=None, tag=None):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo("ShimmerGSR")

        info = StreamInfo('ShimmerGSR', 'GSR', 1, 50, 'int32')
        self.outlet = StreamOutlet(info)

        try:
            print("Initializing SimmerGSR on COM5")
            serial = Serial('COM5', DEFAULT_BAUDRATE)
            self.shim_dev = ShimmerBluetooth(serial)

            self.shim_dev.initialize()

            dev_name = self.shim_dev.get_device_name()
            print(f'My name is: {dev_name}')

            self.shim_dev.add_stream_callback(handler)
        except Exception as e:
            self.memo.sts = f"❌ Fault {str(e)}"


    def start(self):
        self.shim_dev.start_streaming()
        self.memo.sts = "🟢"

    def stop(self):
        self.shim_dev.stop_streaming()
        self.memo.sts = "🟥"

        self.shim_dev.shutdown()
            

if __name__ == '__main__':
    # Create LSL stream info and outlet
    info = StreamInfo('ShimmerGSR', 'GSR', 1, 50, 'int32', 'myuid12345')
    outlet = StreamOutlet(info)

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