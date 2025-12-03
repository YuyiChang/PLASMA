from plasma.devices.template import PlasmaDevice, PlasmaMemo
import time
from serial import Serial
from pylsl import StreamInfo, StreamOutlet
from bitalino import BITalino
# from lib.revolution_python_api.bitalino import BITalino
import numpy as np

# def handler(pkt: DataPacket) -> None:
#     # Access the GSR raw data channel
#     gsr_raw_value = pkt[EChannelType.GSR_RAW]
#     print(f'Received new GSR raw data point: {gsr_raw_value}')
    
#     # Push the GSR raw value to the LSL outlet as a single sample (list with one value)
#     outlet.push_sample([gsr_raw_value])

class PlasmaBitalino(PlasmaDevice):
    def __init__(self, session_info, logger=None, tag=None):
        super().__init__(session_info, logger, tag)
        self.memo = PlasmaMemo("Bitalino")

        batteryThreshold = 30
        self.channels = [0, 1, 2, 3, 4, 5]
        self.fs = 1000
        self.n_samples = 10
        digitalOutput_on = [1, 1]
        digitalOutput_off = [0, 0]

        info = StreamInfo('Bitalino', 'bitalino', 11, self.fs, 'int64')
        self.outlet = StreamOutlet(info)

        mac_addr = "98:D3:41:FE:16:F7"

        # This example will collect data for 5 sec.
        running_time = 5

        self.data_length = 10
        self.num_channel = 6

        try:
            self.device = BITalino(mac_addr)
        except Exception as e:
            self.memo.sts = f"❌ Fault {str(e)}"

        #uses custom initialization for data because of the data shape 
        self.memo.data = [0] * 100 *self.data_length * self.num_channel


    # def start(self):
    #     self.device.start(self.fs, self.channels)
    #     self.memo.sts = "🟢"  
    def processing_data(self, data):
        readings = data[:, 5:11]
        readings = readings.flatten(order='F')
        readings = readings.tolist()
        return readings      

    def streaming(self):
        self.device.start(self.fs, self.channels)
        # your custom sensor callback goes here
        while not self._stop_event.is_set():
            data = self.device.read(self.n_samples)
            self.outlet.push_sample(data[0, :])
            self.last_data = (f"{self.tag} reading at {time.time()}", np.shape(data))
            self.memo.set_latest(f"{self.tag} reading at {time.time()}")
            self.memo.set_data(self.processing_data(data))

    # def stop(self):
    #     self.device.stop()
    #     self.memo.sts = "🟥"

    #     self.device.close()
            

if __name__ == '__main__':
    # # Create LSL stream info and outlet
    # info = StreamInfo('ShimmerGSR', 'GSR', 1, 50, 'int32', 'myuid12345')
    # outlet = StreamOutlet(info)

    dev = PlasmaBitalino()
