# ECG/PPG sensor stream over Nordic UART Service — host-side map

The wire contract is **sensor-stream v0**. Authoritative specs (also in
`docs_local/`):

- **`SENSOR_STREAM_CENTRAL_HOWTO.md`** — v0: `START` (FINITE) / `START_INFINITY`
  / `STOP` commands, the 12-byte TX envelope, 16-byte `START_ACK`,
  offset-addressed `DATA`, 2-byte `END` / `RESULT`, the leading zero-history
  padding rule, timeouts.
- **`ECG_BLOCK_FORMAT.md`** — the 4096-byte `ECB2` ECG block (16-byte header +
  1358 three-byte samples, CRC-32/ISO-HDLC) and the `ECF2` file header.
- **`PPG_PACKED_16_BYTE_FORMAT.md`** — 16-byte PPG record (`ir1/ir2/g1/g2`
  uint24-LE + `global_tick_512hz`), 256 Hz — unchanged by v0.
- `NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md` / `ECG_TEMP_DATA_FORMAT.md` —
  historical v1 / framed-ECG, for decoding old capture blobs only.

## Host-side implementation

- `plasma/devices/msense/nus_stream.py` — pure v0 codec + `StreamSession`
  (offset reassembly, FINITE + INFINITY; no BLE). `build_command`,
  `parse_header`, `parse_start_ack`, `parse_data`, `parse_end`, `parse_result`.
- `plasma/devices/msense/records.py` — `decode_ppg`, `decode_ecg`
  (batch ECB2), `EcgBlockReassembler`, `Packed16Reassembler`, `crc32_iso_hdlc`.
- `plasma/devices/msense/nus_sim.py` — inverse frame builders (tests + the
  simulated wristband).
- `plasma/devices/msense/formats/__init__.py` — offline `ecg:block_v2` (`ECF2`)
  and `ppg:packed16` decoders for downloaded `.bin` files.
- `plasma/devices/msense/device.py`:
  - `request_sqc_snapshot` / `request_all_sqc_snapshots` — FINITE snapshot
    (MTU check + `OP_START`); `_nus_data_handler` → `_handle_sqc_notification`
    feeds each whole notification to `StreamSession`; `_finish_sqc_snapshot`
    decodes + saves raw `.ppg`/`.ecg` + `.json` sidecar.
  - `start_live_stream` / `stop_live_stream` / `get_live_stream_preview` —
    continuous `OP_START_INFINITY` decoded into an in-memory rolling buffer
    (`_handle_live_notification` → `_live_ingest`). **Not** written to disk or
    LSL/XDF in this build.
  - `_sqc_watchdog_loop` — 15 s no-progress `OP_STOP` + reconnect, for both
    snapshot and live streams.
  - product (`"ECG"` / `"PPG"`) comes from the advertised name
    (`MSense4ECG` / `MSense4PPG`), cached in `caps[name]["product"]` — v0
    `START_ACK` carries no product identity.
- `plasma/devices/msense/signal_quality.py` — `filter_ecg` / `filter_ppg` for
  the light overlay in the "ECG/PPG Signal Quality" tab.
- Tests: `tests/test_nus_stream.py`, `tests/test_ecb2.py`,
  `tests/test_records.py`, `tests/test_formats_detect.py`,
  `plasma/devices/msense_demo/tests/`.

## Quick mode

The protocol has no "request less", so for a FINITE snapshot the Central lets
`START` run and writes `STOP` once enough has arrived — at the 32 KiB history
mark for "History Only", or after N seconds of forward data for "Custom" — and
keeps the validated prefix (`*_pNs.ecg`/`.ppg`, sidecar `partial: true`). The
1–4095 B (ECG) / 1–15 B (PPG) partial tail is discarded. If the device doesn't
answer `STOP` with `END` within `SQC_EARLY_CANCEL_GRACE_S`, the partial is
finalized locally and the link reconnected.
