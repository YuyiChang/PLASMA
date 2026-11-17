# Assigning participants to MSense wristbands

On **Session Dashboard**, set the Subject ID and shared Session ID, then
initialize the real MSense or simulated MSense devices. Expand **MSense
participant assignments** and enable **Assign participants per MSense wristband**.
The table refreshes after initialization; **Refresh assignments** updates it on
demand.

The **Subject ID to use** column beside each wristband shows its effective
subject ID, including inherited dashboard defaults. With per-wristband assignment
disabled, it shows the dashboard Subject ID for every wristband.

Select one or several wristbands, enter a participant such as `sub-1002`, and
press **Apply to selected**. An empty participant override uses the dashboard
Subject ID. Several wristbands can belong to the same participant. The UUID/MAC
address identifies each wristband; nicknames are for display. Assignments can
change after Initialize without reconnecting devices.

All wristbands share the dashboard Session ID. For example, `sub-1001` and
`sub-1002` in `ses-02` receive encodings `100102` and `100202`. Group mode accepts
numeric subject IDs and sessions `ses-00` through `ses-99`; encodings must fit
the firmware's unsigned 32-bit field.

At **Start**, PLASMA freezes the assignments and writes and reads back every
configured wristband's encoding before enabling acquisition on any wristband.
A missing device or incorrect readback aborts Start with its name/address.
If a subsequent acquisition start fails, PLASMA attempts to stop the devices
already started and labels the run failed. Check the Memo for stop failures.
Participant controls and dashboard identity fields are locked until **Stop**.
Bulk **Write encoding** in MSense Tools is blocked while group assignments are
enabled or a recording is running. The last group mapping also remains protected
after Stop; starting a run with group mode off returns to the dashboard encoding.
Reconnected wristbands use the frozen ID and
must pass another readback before resuming acquisition.

Group runs are saved under:

```text
data/group-sessions/ses-02/<timestamp>_<run-id>/
  participant_assignments.json
  session_encoding.csv
  plasma_recording_<timestamp>.xdf
  msense/sub-1001/msense/
  msense/sub-1002/msense/
```

Simulated wristbands use `msense_demo` as the final directory. Text data and SQC
captures are routed to their participant folder; SQC JSON sidecars include the
participant identity. The manifest records each wristband's plugin, label,
address, subject, session, encoding, LSL stream name/source ID, and run status.
`session_encoding.csv` can be loaded as the encoding table in the MSense
extractor. Repeated participant/session pairs produce one encoding-table row.

There is one combined XDF when recording is enabled. Its PLASMA journal contains
a `[PARTICIPANT_MAP]` JSON marker before acquisition. Group Start requires the
recorder to resolve the journal and have a recording consumer ready; it aborts
if this fails. Stream names and source IDs retain their physical-device identity;
the manifest/marker supplies participant attribution rather than modifying LSL
metadata announced at Initialize. Other sensor types use the dashboard identity
and the shared run directory. Turning off XDF recording still saves the manifest
and local MSense data.

Assignments are session setup, not persistent device-catalog settings. Use
**Export assignments CSV** to save overrides and **Import assignments CSV** to
reuse them after initialization. The format is:

```csv
plugin_id,address,participant_id
msense,DEVICE-UUID-OR-MAC,sub-1001
msense,ANOTHER-DEVICE-UUID-OR-MAC,
```

A blank `participant_id` inherits the dashboard Subject ID. Import rejects
unknown or duplicate wristbands and invalid IDs without applying a partial
table. **Clear overrides** returns all wristbands to the dashboard default.
Overrides remain in memory until cleared or PLASMA exits. Stopping and editing
the next run does not rewrite the preceding run's saved mapping or SQC identity.

With the per-wristband option off, the existing single-participant directory
layout and dashboard encoding are used. Simulated MSense supports the same
assignment workflow for training without Bluetooth hardware.
