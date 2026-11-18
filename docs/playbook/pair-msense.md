# Pair MSense wristbands

MSense wristbands are identified by their **BLE name** (e.g. `MSense4PPG-8NR1S`)
and connected by **UUID / MAC address**. Pairing is telling PLASMA which
name ↔ address pairs to look for. It lives in **Configuration → "MSense
wristbands"**.

![The MSense wristbands section of the Configuration tab](../assets/screenshots/configure.png){ .shot }

## Scan and add

!!! warning "Finish configuration scans before Initialize"
    Avoid running an independent Configuration scan during initialization or
    recording: it shares the Mac's Bluetooth radio and can compete with device
    setup and streaming. Use **Initialize** again for missing wristbands; its
    bounded retry scan preserves the already connected devices.

1. Turn the wristband on and make sure it's advertising and in range.
2. Click **🔍 Scan for MSense wristbands (5 s)**.
3. In **Discovered wristbands**, tick the units you want.
4. Choose how to add them:
    - **Append (skip addresses already listed)** — keeps your existing rows (default).
    - **Overwrite table** — replaces the whole list with the scan results.
5. Click **➕ Add selected**. A row appears with the advertised **Name**, an
   empty **Nickname**, **Enabled** on, and **IMU Stream** off.
6. Click **Apply MSense wristbands** to save. *"Saved — n/N MSense wristband(s)
   enabled"* confirms it.

Edits here take effect the next time you click **Initialize** on the Session
Dashboard.

## Initialize several wristbands

Initialize discovers configured addresses, retains the native Bluetooth device
objects, and connects them serially. Discovery stops early when every configured
wristband has supplied its advertised name; otherwise the initial window is
five seconds. A second pass scans for missing/failed wristbands for up to three
seconds and retries only those peers. A configured address is still considered
if its advertisement has no name. UUID/MAC matching ignores case and surrounding
whitespace.

If only five of six connect, leave those five connected and click **Initialize**
again with collection stopped. With the same saved MSense configuration, this
reuses the driver, its healthy links, and their LSL outlets, and retries the
missing wristband. When all are healthy, repeating Initialize does not rescan
them. Changing the wristband list, names/nicknames, or IMU settings performs a
full reinitialization; simulated devices also retain their full reinitialization
behavior. Initialize is blocked during collection.

For troubleshooting, inspect the session log for `MSense scan:` (addresses seen
and elapsed time), per-address `connect`/`setup` timings, and the final connection
count. A scan/permission error is distinguished from a wristband not advertising
and from a connection failure. Physical six-device reliability and timings still
depend on the Mac's Bluetooth controller, radio conditions, and wristband firmware.

## The table columns

| Column | Meaning |
|---|---|
| **Name** | The BLE advertised name. Used to label the LSL stream and the saved files. Two wristbands *may* share a Name — the address below is the identity. |
| **Nickname** | Optional, display-only. Shown as *"Name (Nickname)"* on the dashboard and in the signal viewer. Use it for "left wrist" / "chest" — and to tell apart two wristbands that share a Name. |
| **UUID / MAC Address** | The connection address (a UUID on macOS, a MAC elsewhere). This is the **identity**: each wristband's status, gyro-bias record and per-device state is keyed by it. Listing the same address on two enabled rows is flagged on Apply (only the last is used). |
| **Enabled** | Untick to **park** a wristband — kept in the list, skipped at Initialize. |
| **IMU Stream** | Tick **only** for units whose firmware has the demo IMU characteristic. Ticking it on a unit that lacks it just logs a warning at Start. |

You can also edit cells directly and add / delete rows in the table, then Apply.

## Pairing without a scan

If the wristband is mounted over USB you can pair from **YAMS → 📋 Devices**:

- **UUID extractor** — pick the mounted drive, **🔧 Read UUID** reads the
  address from the wristband's `uuid.txt`, type a serial / name, **📝 Add to
  wristband list**.
- **Import device_info.json** — load a `{serial: MAC}` map exported elsewhere.

Both write straight into the same saved wristband list.

---

Next: [Run a session](run-a-session.md).
