## Purpose
Preparation for Implementing GSR Streaming in PLASMA

## Usage
- The scripts use the "Bluetooth interface" to stream `GSR_raw`.
- Change `COM5` if you are using a laptop other than the streamer laptop.

## Scripts
- test_shimmer.py is developed to check if the device can be connected to the streamer laptop through `COM5` and then receive data from the `GSR_raw` channel.
- test_shimmer_lsl.py has LSL, and the stream can appear on LabRecorder. However, a test recording has not been performed yet.

## References
- pyshimmer: https://github.com/seemoo-lab/pyshimmer
