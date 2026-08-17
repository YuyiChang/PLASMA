# PLASMA v1.0.0 Release Notes

**PLASMA** — Platform for LSL-based Acquisition of Sensor Metrics and Analytics

---

## 🎉 What's new in v1.0.0

This is the first stable release of PLASMA. It brings together a full multi-device data acquisition pipeline, a graphical session dashboard, and a new configuration system — all in one self-contained desktop app.

---

## 🔌 Device support

Seven sensor integrations are included out of the box:

| Device | Modality |
|---|---|
| MSense Wristbands | BLE motion + heart rate |
| qb2 LiDAR | Depth / spatial sensing |
| Pupil Labs IMU | Head motion (Realtime API) |
| Pupil Labs Eye Events | Blinks & fixations |
| ShimmerGSR | Galvanic skin response |
| OBS Recorder | Video capture |
| Bitalino | Biosignals (ECG, EEG, EMG, EDA) |

All devices stream data over **LSL** (Lab Streaming Layer) for synchronized, time-stamped acquisition.

---

## 🖥️ Session dashboard

- Subject ID / Session ID entry with automatic **participant encoding** (format `XXXXYY`)
- Per-session device selection and one-click initialization
- **Start / Stop** data collection controls
- Live parameter viewer showing per-device status

---

## ⚙️ Configuration tab

### Device catalog
- Toggle any device on or off from the full catalog — enabled devices appear in the session dashboard
- Changes take effect after clicking **Apply**; refresh the device list in the dashboard with **Refresh list**

### MSense wristband pairing
- Editable table of BLE device name ↔ UUID / MAC address pairs — no more hardcoded values in source

### Network settings
- Configurable IP addresses for qb2 LiDAR and Pupil Labs Realtime API

### Import / Export
- Save and load the full configuration as a `.json` file for easy sharing across machines or study setups

---

## 🔒 Security

- Device UUIDs, MAC addresses, and IP addresses are stored exclusively in `plasma_device_config.json` — **never in source code**
- The config file is excluded from version control via `.gitignore`
- On first launch, PLASMA creates the config file with all devices enabled and empty credential fields

---

## 🐛 Bug fixes

- Fixed MSense 32-bit counter rollover handling
- Fixed qb2 async event loop on initialization
- Fixed Pupil Labs eye event stream stability
- Fixed device name mismatch in memo status display
- Fixed MSense BLE adapter initialization error on re-connect

---

## 📦 Distribution

Pre-built app bundles are available for macOS and Windows (see `app_macos.spec` / `app_windows.spec`).

To run from source:

```bash
conda create -n plasma python=3.12
conda activate plasma
pip install -r requirements.txt
python -m plasma
```
