# Assigning participants to MSense wristbands

Open **Configuration → MSense wristbands**. Enter your four-digit IDs in
**Available participant IDs**, separated by commas or newlines, for example
`1001, 1002, 0007`. Press **Update ID choices**, then select an ID from the
**Subject ID override** dropdown on each wristband row. Select **Use dashboard
Subject ID** to inherit the Session Dashboard default. Multiple wristbands can
select the same ID, and leading zeroes are preserved.

Turn on **Enable participant ID overrides**, then press **Apply MSense
wristbands** to save the available list, selections, toggle, and device edits.
Updating choices alone does not save configuration. IDs must contain exactly
four digits, and dropdown selections are restricted to the available list. To
remove an ID from the list, first change any wristbands still assigned to it.
Existing assignments from older configurations automatically populate the list.

Turning the toggle off preserves entered IDs and selections but uses the
dashboard Subject ID for every wristband. **Add wristband**, **Remove**, and
scan/Append/Overwrite operate on the rows; **Apply** saves the result. Row
changes and ID-list updates retain other unsaved edits. For real devices the
UUID/MAC address is the identity; nicknames are display labels.

Overrides and the toggle are saved in the existing device configuration. Saved
subject changes take effect at the next **Start**, including after Initialize,
without reconnecting. Device-list or IMU changes still need **Initialize**.
Changes saved while recording affect the next run; the current recording keeps
its frozen identities.

All wristbands share the dashboard Session ID. For example, `1001` and `1002` in
`ses-02` become `sub-1001` and `sub-1002`, with encodings `100102` and `100202`.
Group sessions accept `ses-00` through `ses-99`; encodings must fit the
firmware's unsigned 32-bit field. Simulated MSense offers the same editor, dropdowns, and
toggle in **Configuration → MSense Demo (simulated)**.

When at least one enabled, initialized wristband has an active override,
**Start** freezes a full participant map, then writes and reads back every
wristband's encoding before enabling acquisition. A missing device or incorrect
readback aborts Start with its name/address. If an acquisition start fails,
PLASMA attempts to stop the devices already started and labels the run failed.
Check Memo for stop failures. Reconnected wristbands reapply and verify the
frozen encoding before resuming acquisition.

Memo labels an overridden wristband **Subject ID override: sub-XXXX**. Wristbands
using the dashboard default receive no override message. When overrides are off
or all wristbands use the dashboard default, the existing single-participant Memo and recording path
are used. Advanced bulk **Write encoding** is blocked during collection and
while configured overrides apply. The last group map remains protected after
Stop until another recording starts, preserving SQC attribution.

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

Use the existing configuration export/import to transfer the wristband list,
subject overrides, and toggle together. The per-run participant manifest and
encoding table describe the identities actually used, even if configuration is
edited later.
