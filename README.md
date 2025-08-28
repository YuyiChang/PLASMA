## Purpose
Preparation for implementing GSR streaming and controlling OBS recordings in PLASMA.

## Usage
### GSR
- The scripts use the **Bluetooth interface** to stream `GSR_raw`.
- Update `COM5` if you are using a laptop other than the streamer laptop.

### OBS
- The script can start and stop recording by pressing the **Start** and **Stop** buttons on both the local and remote laptops.
- Update `HOST` and `password` as needed.

## Scripts
### GSR
- `test_shimmer.py`: checks whether the device can connect to the streamer laptop via `COM5` and receive data from the `GSR_raw` channel.
- `test_shimmer_lsl.py`: streams via LSL, and the stream should appear in LabRecorder. (Note: recording has not yet been tested.)

## References
- [pyshimmer](https://github.com/seemoo-lab/pyshimmer)  
- [obsws-python](https://pypi.org/project/obsws-python/)
