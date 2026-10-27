# QB2 recording and code review

## Configure and record

In **Configuration → QB2 LiDARs**, use **Add new LiDAR** to create a device.
Enter a name, IP address or hostname, and optional application key. Set
**Enable this LiDAR** and click **Save LiDAR**. Repeat for any number of devices.
Select an existing device under **Edit LiDAR** to change it, clear its key,
disable it, or remove it. The overview shows whether a key is configured without
showing the secret. Each Save persists immediately; the global Apply button
controls the sensor catalog separately.

Enable **qb2 LiDAR** in the catalog, click **Apply**, refresh the dashboard's
device list, and Initialize it. Re-Initialize after changing LiDAR settings.
Existing single-device address/key settings are read as one enabled LiDAR; the
first Save stores the list under `plugins.qb2.devices`. Importing a legacy config
also migrates it. After importing, click **Reload saved LiDARs** in the editor.
An explicitly empty list overrides legacy values, so deleted devices stay deleted.

Disabled devices create no connections, capture workers, LSL outlets, or recording
folders. Each enabled device has its own memo row, LSL stream, worker, bounded
queue, and output subfolder. A connection, authentication, or disk error on one
unit leaves its peers running. Stop signals all units before waiting against one
shared five-second deadline. There is no configured device-count limit; actual
capacity depends on host resources, storage, and network throughput.

The key is optional only for devices which permit unauthenticated access.
PLASMA uses the SDK's `TokenFactory(application_key_secret=...)` when a key is
present and always requests encrypted transport. Blickfeld deprecates
unauthenticated connections; see its [Python client guide][python].

The password field masks the UI value. The local JSON config and exported
config contain the actual key. Local config writes use atomic replacement and
owner-only permissions on POSIX. Windows permissions follow the containing
folder's ACL. Avoid sharing a config export containing a real key.

**Initialize** prepares each enabled device’s LSL outlet without opening a sensor connection.
**Start** connects and authenticates; the row stays `Starting...` until the first
frame arrives. This is intentional: SDK channel construction alone does not
verify authentication. No-frame timeout is 10 seconds per read, including the
initial read. It is not a maximum total recording duration.

Point clouds go under the exact folder injected by the session controller:

```text
data/<subject>/<session>/<encoding>_<YYMMDD_HHMMSS>/
├── plasma_recording_<YYMMDD_HHMMSS>.xdf
├── msense/
└── qb2/
    └── <stable-device-id>/
        └── frame_<run-uuid>_<sequence>_<device-frame-id>.npy
```

The UUID and sequence prevent overwriting if a device repeats/resets its frame
counter or PLASMA starts twice in the same shared folder. Filenames changed
from `qb2-<id>.npy` in the old plugin and `frame_<id>.npy` in the standalone script.
Paths are resolved at Start so subject/session edits made after Initialize are
respected. Standalone use without an injected `session_dir` gets a timestamped
folder beneath `session_info['log_dir']`.

## Frame format and timing

Each `.npy` contains a dictionary, following `lib/blickfeld-qb2-lidar/main.py`:

| Key | Meaning |
|---|---|
| `frame` | Complete Blickfeld SDK frame, including device timestamps and binary point arrays |
| `unix_time` | Host receipt time, seconds since Unix epoch |
| `datetime` | The same receipt time, ISO 8601 UTC with milliseconds and explicit offset |
| `lsl_time` | Host receipt time on the LSL local clock, seconds |
| `device_id` | Stable configured device ID (also the output subfolder and stream suffix) |
| `device_address` | Configured sensor IP address or hostname |

The standalone script's `datetime` was a local-time string without a timezone;
PLASMA writes UTC explicitly. The previous plugin saved the bare frame object;
update readers to extract the dictionary's `frame` entry:

```python
import numpy as np

# Load only trusted recordings: this format uses Python pickle and requires
# a compatible blickfeld_qb2 SDK to reconstruct its objects.
record = np.load(path, allow_pickle=True).item()
frame = record["frame"]
xyz = frame.binary.cartesian
```

Each unit publishes `blickfield_qb2_<stable-device-id>`, using the existing name
as a prefix. The suffix distinguishes units even if display names are identical.
Update consumers that resolved the exact old stream name `blickfield_qb2`.
Each stream’s single channel carries a **decimal string**, replacing `int32`:
Blickfeld specifies `frame.id` as `uint64`, whose full range cannot be represented
in signed int32/int64. Convert marker values with `int(value)` in consumers.
Stream source IDs are unique per initialized instance to avoid reattaching to a
previous instance under the old shared `qb2-xxxx` identity.

Markers are published only after the matching file is completely written and
renamed, but carry the original **receipt** LSL timestamp. A disk write failure
therefore creates no marker claiming that frame was saved. A crash between file
rename and marker publication can leave an unreferenced file. Network and
writer errors remain visible in the memo, including when XDF recording is off.

Receipt time includes network and device processing latency; it is not the
measurement time. Blickfeld defines `frame.timestamp` as frame-start time in
nanoseconds and says it is synchronized only when device time synchronization
is configured. Preserve it for subsequent timing checks. The plugin does not
change the sensor's clock configuration. See [Frame][frame] and
[Time Synchronization][clock].

## Review findings and implemented changes

| Severity | Existing code/reference script problem | Integration behavior |
|---|---|---|
| High | Plugin calls nonexistent `memo.latest(...)`, terminating the capture thread on its first frame | Uses `set_latest()`; worker exceptions become persistent visible faults |
| High | Stop is checked only after blocking `stream()` returns a frame | Uses async stream cancellation; closes the channel on its owning event loop |
| High | Output folder is created at Initialize, outside the shared timestamped XDF/MSense folder | Resolves injected session directory at Start |
| High | Reference script authenticates, old plugin does not | Masked application-key setting, persisted/imported/exported with the address |
| High | Worker exceptions can terminate capture while leaving a healthy status | Reports capture, timeout, queue, disk and LSL failures in the memo; Stop preserves faults |
| High | Frame-ID-only names overwrite data on counter reset or repeat runs | Run UUID plus sequence and device ID |
| Medium | `int32` LSL marker cannot hold the documented frame ID range | Decimal strings preserve uint64 values |
| Medium | Synchronous frame writes block network reception; reference script also prints every frame | One asynchronous reader and one disk writer, no per-frame console logging |
| Medium | No disk-backlog policy | Eight queued frames maximum; overflow explicitly faults capture and drains accepted frames |
| Medium | Direct `.npy` writes leave final-looking incomplete files on failure | Write a unique `.partial`, rename on success, clean up partial on normal write errors |
| Medium | Unused initialization channel and unclosed event loop | No unused channel; `asyncio.run()` owns and closes the worker loop |
| Medium | Repeated Start/Stop can leave workers running unnoticed | Refuses parallel Start; Stop waits up to five seconds and reports an unconfirmed stop if I/O remains blocked |
| Medium | Broken optional SDK or outlet creation can abort device initialization | QB2 shows its own fault without raising from its constructor |
| Medium | Config is overwritten in place and legacy imports without `plugins` are not persisted | Atomic config replacement; legacy imports persist the address and clear missing keys |

Queued writes drain on ordinary Stop and network failure. A write failure halts
capture and may leave accepted but unwritten frames; the failure is explicit.
The queue is bounded by frame count, so memory still depends on the scan pattern
and point count. Atomic rename prevents a partially written file from appearing
under its final name; it does not promise durability across power loss (`fsync`
is not performed). After a forced process exit, orphan `.partial` files may
remain and are not complete recordings.

## Recommended follow-up work

1. **Validate real hardware first.** Check the installed firmware/SDK pair,
   correct/incorrect key, unplugged Ethernet, device reboot, sustained capture
   alongside MSense/XDF, and Stop during a stream stall. Measure received/saved
   frame rate, disk throughput, queue occupancy, and peak memory at the actual
   scan pattern. Current verification uses the installed 2.15.4 SDK and simulated
   responses; it does not establish physical-device throughput or compatibility
   with every SDK version.
2. **Verify measurement-time synchronization.** Configure/check device time sync
   separately, measure device-clock offset/drift and host receipt latency, then
   decide how to map frame timestamps into the XDF timebase. Host receipt
   timestamps alone do not prove alignment with MSense samples.
3. **Consider a versioned archival format.** One pickled file per frame preserves
   the reference script's complete objects, but depends on Python/SDK classes and
   creates many directory entries. Benchmark chunked numeric arrays plus a frame
   index, or Blickfeld's [Record service][record], before changing the format.
   Record the SDK/firmware version, scan pattern, filter settings, and device
   identity in a future manifest, without credentials.
4. **Add measured health diagnostics.** Persist frame-counter gaps/resets, queue
   depth, write latency and bytes written, and show an advisory before backlog
   becomes fatal. Counter gaps can reflect sensor-side behavior and require
   interpretation; the present queue guard cannot detect every upstream loss.
5. **Make reconnection policy explicit.** Current faults require operator action
   and a new Start. Automatic reconnect should use bounded exponential backoff,
   separate authentication failures from transient transport errors, and journal
   each gap/recovery. Do not silently label an interrupted session continuous.

## Validation

`plasma/tests/test_qb2.py` exercises capture with simulated async responses and
real SDK frame objects, including point-array serialization, uint64 IDs,
authenticated/keyless channels, shared session paths, duplicate counters and
repeated starts, immediate/stalled Stop, disk failure, queue overflow, network
failure and redaction, stream termination, missing SDK/outlet faults, and
settings persistence/import/export/atomic failure. Multi-unit tests cover mixed
keyed/keyless devices, disabled units, isolated failures, unique folders and
streams, duplicate-address validation, stable IDs, and legacy migration. The session-directory and UI
lifecycle regression tests cover the surrounding integration.

[python]: https://docs.blickfeld.com/qb2/Qb2/developer/client_libraries/python.html
[frame]: https://docs.blickfeld.com/qb2/Qb2/protocol/blickfeld/core_processing/data/frame.html
[clock]: https://docs.blickfeld.com/qb2/Qb2/protocol/blickfeld/system/config/time_synchronization.html
[record]: https://docs.blickfeld.com/qb2/Qb2/protocol/blickfeld/toolkit/services/record.html
