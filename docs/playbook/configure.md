# Configure PLASMA

Everything on the **Configuration** tab is saved to
`<home>/plasma_device_config.json`. You can also import / export that file to
move a setup between machines.

![The Configuration tab](../assets/screenshots/configure.png){ .shot }

## Device catalog

**Available sensors** is the master list of device *types*. Tick the ones this
study uses; only ticked types appear in the **Select sensor(s)** picker on the
Session Dashboard. Click **Apply** to save, then **Refresh list** on the
dashboard to pick up the change.

Default-enabled after a fresh install: qb2 LiDAR, Pupil Lab IMU, Pupil Lab Eye
Event Blink, ShimmerGSR, OBS Recorder, and MSense Wristbands. Bitalino is
off by default (it needs PyBluez).

!!! note "Apply messages"
    *"Saved — N device type(s) enabled"* confirms the write. If demo mode is on
    it adds *"— restart PLASMA to load the simulated device"*.

## Network settings

- **Pupil Labs IP address** — the address that device listens on.

## QB2 LiDARs

Use **Add new LiDAR**, enter its name and IP address/hostname, optionally enter
an **Application key secret**, set **Enable this LiDAR**, and click **Save LiDAR**.
Repeat for as many devices as needed. Select a saved unit under **Edit LiDAR**
to change its key, enable/disable it, or remove it. Each device saves separately.
Keys are masked in the editor; the overview only shows whether a key is configured.

Enable **qb2 LiDAR** in the catalog and click **Apply**, then re-Initialize on the
dashboard after changing the list. Only enabled units connect and record. Each
writes to `qb2/<device-id>/` in the shared subject/session recording folder,
beside MSense data and the XDF file. A failed unit does not stop its peers.

Existing single-LiDAR settings are read automatically. After importing config,
click **Reload saved LiDARs** to refresh the editor. The local and exported JSON
config contain the actual keys. See [QB2 recording](../reference/qb2-recording.md)
for file format, timing, and fault handling.

## Import / export

- **Export config** downloads the current settings as JSON.
- **Import config (.json)** loads one back — unknown device names are skipped
  and reported. Per-plugin sections (MSense pairing, demo wristbands) re-render
  with the imported values the next time you open the Configuration tab.

## Demo mode

**"Demo mode — add the simulated MSense device to the catalog"** adds a
hardware-free MSense wristband ("MSense Demo (simulated)") you can connect,
stream, and run signal-quality checks against with no Bluetooth. It's how every
screenshot in this playbook was made.

Demo mode is **restart-gated** — tick it, click Apply, then use the Power
section to restart. You can also set `PLASMA_DEMO=1` in the environment (that
can't be turned off from the UI). Once enabled, you still have to tick "MSense
Demo (simulated)" in the catalog. See
[Abnormal handling](abnormal-handling.md) for the per-wristband fault injection
the demo device supports.

## Power

At the bottom of the tab:

- **↻ Restart PLASMA** — relaunches with the config on disk. This is the
  intended way to apply a demo-mode or catalog change. Click **Apply** first to
  keep unsaved edits.
- **⏻ Shut down PLASMA** — clean exit.

Both are **two-click**: the first click arms the button (*"Click again to
confirm"*, auto-cancels after ~4 s), the second fires it. Both flush any running
recording (the XDF is finalised) and close device connections before exiting.

---

Next: [Pair MSense wristbands](pair-msense.md).
