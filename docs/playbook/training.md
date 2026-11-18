# Train an RA with the Instructor Console

The trainee uses normal PLASMA. The mentor uses a separate web interface to
introduce unexpected behavior in simulated MSense wristbands. No additional
device registry, LSL outlet, or XDF recorder is created.

## Start a training session

Launch PLASMA from a terminal:

```bash
plasma --training
# From a source checkout:
python -m plasma --training
```

Open the trainee interface at **http://127.0.0.1:7860** and the mentor's
**Instructor Console** at **http://127.0.0.1:7861**. Training mode loads the
simulated plugin and selects it for this run without overwriting the saved
sensor selection. It does not initialize sensors automatically.

In the trainee interface, set the subject/session and Initialize **MSense Demo
(simulated)**. A new demo setup supplies a PPG and an ECG wristband. Use
**Fault = none** in saved demo settings for a healthy baseline. Start collection
before incidents if you want training markers saved in XDF. Instructor actions
can target only initialized simulated MSense wristbands, even if real sensors
are also present.

## Run an exercise

1. In the Instructor Console, enter an exercise name and optional private
   coaching objectives, then click **Begin exercise**.
2. Select a wristband and incident. Set **Trigger after** to introduce it later,
   or leave it at zero to introduce it immediately.
3. Click **Introduce incident**. Observe the trainee's response in PLASMA.
4. Use **Restore selected sensor**, or **Restore all / reset exercise conditions**
   to remove all injected conditions and pending triggers. Recordings remain
   intact. Restoring allows normal reconnect/recovery; it does not overwrite
   PLASMA's status with a healthy label or issue Stop on the trainee's behalf.
5. Save a mentor assessment and private notes: **Observed**, **Needed prompting**,
   or **Missed**. Assessment is manual; reconnecting alone does not prove that
   the trainee followed the complete lab procedure.

| Incident | Behavior |
|---|---|
| Brief disconnect | Drops the current link; the existing watchdog reconnects normally. |
| Hold sensor offline | Drops the link and refuses reconnects until restored. |
| Stall active live stream | Stops notifications for one live ECG/PPG transfer. Start that live stream in YAMS first; ENMO/IMU collection alone is not an active ECG/PPG transfer. |
| Stall next SQC snapshot | Arms one finite snapshot to stall. The trainee's next SQC request follows the normal timeout/recovery path. |
| Ignore acquisition Stop | Acknowledges the stop write while retaining the recording state. The real stop-confirmation checks report the failure. After restoration the trainee must retry Stop. |

**Restore after** applies to held conditions and untriggered stream/SQC faults;
zero means manual restoration. A triggered stream/SQC fault affects one transfer
and leaves the usual driver recovery in progress. Brief disconnect ignores
duration. Only one incident may be pending or active per wristband.

The timeline distinguishes scheduled/applied/restored incidents from observed
connection changes. Reinitializing sensors cancels instructor conditions and
pending triggers before replacing the devices. An old target cannot redirect a
delayed incident to a new device with the same name or address. Closing the
Instructor Console browser leaves an exercise running; exiting PLASMA cancels
its incidents and stops the controller.

## Logs and recordings

Each exercise writes a JSON-lines log beneath the app's `data/training/`
directory. It includes UTC event times, incident/device IDs, private objectives,
and assessments. The most recent 500 events appear in the console; the file
retains the full exercise history. Review the log path shown in the console.

Incident and exercise events also send `[TRAINING]` JSON markers on the existing
PLASMA journal stream. Private exercise names, objectives, and assessment notes
are excluded from those markers and are not displayed in the trainee UI.
Instructor faults are runtime overrides: they do not change saved simulator
configuration. Restoration returns to any faults already configured there.

## Mentor on another computer

The instructor server binds only to localhost by default. For access over a
trusted lab network, set `PLASMA_INSTRUCTOR_TOKEN` to an access token in the
environment and launch:

```bash
plasma --training --instructor-host 0.0.0.0
```

Open `http://<PLASMA-computer-address>:7861` and sign in as **instructor** using
the token as the password. A token is required for non-local binding. Use a
trusted network or an encrypted tunnel; this HTTP endpoint does not supply TLS.
The main trainee interface remains local by default. Change the instructor port
with `--instructor-port`; it must differ from trainee port 7860.
