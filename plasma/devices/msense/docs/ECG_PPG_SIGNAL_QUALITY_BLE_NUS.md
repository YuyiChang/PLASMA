# ECG/PPG signal-quality snapshot over Nordic UART Service

**Superseded.** This file described a provisional, guessed wire protocol (single
trigger byte, one combined ECG+PPG `int16` blob, accumulate-until-length
receive loop). That guess has been replaced by the real firmware contract.

Authoritative specs now live in `local_docs/`:

- `NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md` — protocol version 1: structured
  `START`/`CANCEL` commands, the 12-byte TX message header, `START_ACK` /
  `DATA` / `END` / `RESULT` messages, session validation (sequence / record
  index / phase boundary / END counts), timeouts, and the required provenance
  sidecar.
- `PPG_PACKED_16_BYTE_FORMAT.md` — 16-byte PPG record (`ir1`, `ir2`, `g1`,
  `g2` as uint24 LE + `global_tick_512hz`), 256 Hz.
- `ECG_TEMP_DATA_FORMAT.md` — 12-byte MAX30001 ECG frame (`A5 EC` sync,
  `type`, `flags`, `rtc_tick_le`, `raw24` MSB-first, `crc8`), 512 Hz.

## Host-side implementation

- `plasma/nus_stream.py` — pure protocol codec + `StreamSession` state machine
  (no BLE). `build_command`, `parse_header`, `parse_start_ack`, `parse_data`,
  `parse_end`, `parse_result`.
- `plasma/ppg_ecg_records.py` — `decode_ppg` / `decode_ecg` / `crc8_07`.
- `plasma/devices/msense.py` — `register_nus_notify` (subscribe TX at connect),
  `request_sqc_snapshot` / `request_all_sqc_snapshots` (MTU check + structured
  `START`), `_nus_data_handler` (feeds each whole notification to
  `StreamSession`), `_finish_sqc_snapshot` (decode + save raw `.ppg`/`.ecg` +
  `.json` sidecar), `get_sqc_status` / `get_sqc_result`.
- `plasma/signal_quality.py` — `filter_ecg` / `filter_ppg` only, used for the
  light overlay on the raw waveform in the "ECG/PPG Signal Quality" tab
  (`plasma/integrated_panel.py`).
- `tests/test_nus_stream.py`, `tests/test_records.py` — offline protocol /
  decoder coverage.

One connected device is **either** PPG **or** ECG; multiple units can be
connected and are snapshotted **one at a time** (single Mac BLE radio), each
channel in its own subplot.

**Quick mode** (default in the tab): the protocol has no "request less", so the
Central lets `START` run, writes `CANCEL` once N seconds of records have
arrived (or at the history→forward boundary for "history only"), and keeps the
partial payload as a usable capture (`*_pNs.ecg` / `.ppg` on disk, sidecar
`partial: true`). With N ≤ the history window (~5 s ECG / ~8 s PPG) the whole
live-acquisition wait is skipped. The early `CANCEL` is issued from the
watchdog thread; if the device doesn't answer with `END` within
`SQC_EARLY_CANCEL_GRACE_S`, the partial is finalized locally and the link is
reconnected.
