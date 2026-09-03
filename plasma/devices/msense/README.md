# MSense plugin

Everything MSense lives here. Two halves:

## Live acquisition (BLE)

`device.py` — `MotionSenseHRV(PlasmaDevice)`: scan/connect wristbands over
`simplepyble`, ENMO stream, demo IMU stream (accel + composed quaternion
orientation), gyro-bias calibration, and the ECG/PPG **signal-quality snapshot**
(SQC) over the NUS bounded-stream protocol (`nus_stream.py` codec, `records.py`
live-payload decoders, `signal_quality.py` filters). Contributed tabs:
"ECG/PPG Signal Quality" (`panels/sqc.py`) and "MSense IMU" (`panels/imu.py`).

## Offline field-data toolkit

Pull recorded `.bin` files off the device (USB mass storage), decode them to CSV,
and align them to a wall clock. **No BLE, no Gradio in the pure layer.**

| module | what |
|---|---|
| `formats/` | on-disk `.bin` record layouts — `RecordSpec` + `REGISTRY` (PPG legacy/v2/packed16, IMU legacy/v2/v3-ACF3, ECG framed), decoders, ACF3 container reader, `read_bin`, CDCT math |
| `detect.py` | content-based format resolution (`resolve`, `Resolution`, `uuid.txt` cross-check) |
| `extract/pipeline.py` | `extract_dir()` / `extract_zip()` / `batch_extract_zips()` → `ExtractionReport` |
| `extract/clocksync.py` | counter-align an extracted CSV to a YAMS `.txt` unix-time reference |
| `extract/options.py` | `ExtractionOptions` dataclass |
| `panels/` | `tools.build_tools_tab` — the "MSense Tools" tab (Downloader / Extractor / Extractor-zip / Clock Sync / Data viewer / UUID tools) |

### CLI

```
python -m plasma.devices.msense.extract dir   -i <folder of .bin> -o <out> [--save_format csv|pickle]
                                              [--ignore_id] [--force_new_format]
                                              [--{ppg,ac,ecg}_format auto|version|legacy|v2|packed16|framed|v3]
                                              [--validate_with_uuid] [--on_format_conflict warn|raise|trust_uuid]
                                              [--dry_run] [--strict_ppg]
python -m plasma.devices.msense.extract batch -i <folder of *.zip> [-o <out>]
python -m plasma.devices.msense.extract sync  --csv <ac.csv> --txt <yams.txt> [--ppg <ppg.csv>] [--out <dir>]
```

The `dir` subcommand is the default — `python -m plasma.devices.msense.extract -i … -o …`
also works. Output is byte-for-byte identical to YAMS's `python -m yams.data_extraction`.

### Wire-format specs

`docs/` — NUS handoff, packed-16 PPG, framed ECG, ACF3 accelerometer container,
the gyro→quaternion column rename.
