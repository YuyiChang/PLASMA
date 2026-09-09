"""MSense shared sensor-stream protocol **v0** — pure codec + session FSM.

No BLE / device state lives here: this module turns raw NUS TX notification
byte strings into validated protocol events and drives byte-offset reassembly
of the sensor payload. ``plasma/devices/msense/device.py`` runs it over an
actual BLE link; ``plasma/devices/msense/records.py`` decodes the reassembled
bytes (ECB2 blocks for ECG, packed-16 records for PPG).

Wire contract: ``plasma/devices/msense/docs/SENSOR_STREAM_CENTRAL_HOWTO.md``
and ``ECG_BLOCK_FORMAT.md``. This is a hard cut-over from protocol v1 (the old
``NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md`` path): the version byte is now ``0``,
DATA is addressed by an absolute uint64 byte offset (no sequence number, record
index or phase field), START_ACK is 16 bytes with no device/git metadata, and
END/RESULT are a bare uint16 status. Sender and receiver must be upgraded
together — there is no negotiation and no fallback.

Two stream modes share one path:

* **FINITE** (``OP_START``) — 32 KiB rolling history + 96 KiB future =
  exactly 131 072 sensor bytes, then END.
* **INFINITY** (``OP_START_INFINITY``) — the same 32 KiB history, then future
  data continuously until STOP / disconnect / acquisition end / fault.
"""
from dataclasses import dataclass, field

MAGIC = b"\x4d\x53"  # 'MS'
PROTOCOL_VERSION = 0

# command opcodes (host -> peripheral, written to NUS RX)
OP_START = 0x01           # FINITE capture
OP_STOP = 0x02            # end the matching stream immediately
OP_START_INFINITY = 0x03  # continuous future capture

# TX message types (peripheral -> host, NUS TX notifications)
MSG_START_ACK = 0x81
MSG_DATA = 0x82
MSG_END = 0x83
MSG_RESULT = 0x84

HEADER_LEN = 12
START_ACK_PAYLOAD_LEN = 16
END_PAYLOAD_LEN = 2
RESULT_PAYLOAD_LEN = 2
DATA_OFFSET_LEN = 8

# stream geometry, fixed by the format version (no negotiable parameters)
HISTORY_BYTES = 32_768          # rolling history sent at the front of every stream
HISTORY_UNIT_BYTES = 4_096      # START_ACK reports history size in these units
HISTORY_UNITS = 8              # 8 * 4096 == HISTORY_BYTES
FINITE_FUTURE_BYTES = 98_304
FINITE_TOTAL_BYTES = HISTORY_BYTES + FINITE_FUTURE_BYTES   # 131 072
ATT_MTU_MIN = 128

MODE_FINITE = 0
MODE_INFINITY = 1
MODE_NAMES = {MODE_FINITE: "FINITE", MODE_INFINITY: "INFINITY"}

# END terminal status (uint16)
END_SUCCESS = 0x0000
END_NOT_RECORDING = 0x0001
END_STOPPED = 0x0008
END_STORAGE_ERROR = 0x0009
END_INTERNAL_ERROR = 0x000A
END_DISCONNECTED = 0x000D        # synthesized locally; END delivery not expected
END_BUFFER_OVERFLOW = 0x000E
END_STATUS_NAMES = {
    END_SUCCESS: "SUCCESS",
    END_NOT_RECORDING: "NOT_RECORDING",
    END_STOPPED: "STOPPED",
    END_STORAGE_ERROR: "STORAGE_ERROR",
    END_INTERNAL_ERROR: "INTERNAL_ERROR",
    END_DISCONNECTED: "DISCONNECTED",
    END_BUFFER_OVERFLOW: "BUFFER_OVERFLOW",
}

# RESULT command-rejection status (uint16). 0x0002 is a retired reservation
# (former HISTORY_NOT_READY) and is never emitted by this design.
RESULT_NOT_RECORDING = 0x0001
RESULT_NOT_SUBSCRIBED = 0x0003
RESULT_BUSY = 0x0004
RESULT_MTU_TOO_SMALL = 0x0005
RESULT_INVALID_COMMAND = 0x0006
RESULT_UNSUPPORTED_VERSION = 0x0007
RESULT_NOT_INITIALIZED = 0x000B
RESULT_WRONG_SESSION = 0x000C
RESULT_STATUS_NAMES = {
    RESULT_NOT_RECORDING: "NOT_RECORDING",
    0x0002: "RESERVED",
    RESULT_NOT_SUBSCRIBED: "NOT_SUBSCRIBED",
    RESULT_BUSY: "BUSY",
    RESULT_MTU_TOO_SMALL: "MTU_TOO_SMALL",
    RESULT_INVALID_COMMAND: "INVALID_COMMAND",
    RESULT_UNSUPPORTED_VERSION: "UNSUPPORTED_VERSION",
    RESULT_NOT_INITIALIZED: "NOT_INITIALIZED",
    RESULT_WRONG_SESSION: "WRONG_SESSION",
}

# product -> record geometry. The product is NOT reported in START_ACK any more;
# the central knows it from the advertised name (MSense4ECG / MSense4PPG) and
# looks the rest up here. ECG's "record" is one 4096-byte ECB2 block.
ECG = "ECG"
PPG = "PPG"
PROFILE = {
    ECG: {
        "name": "ECG",
        "record_size": 4_096,          # one ECB2 block
        "samples_per_block": 1_358,
        "sample_rate": 512.0,
        "bytes_per_second": 512.0 * 4_096 / 1_358,        # ≈ 1544 B/s
        "history_records": HISTORY_BYTES // 4_096,        # 8 blocks
        "finite_future_records": FINITE_FUTURE_BYTES // 4_096,   # 24 blocks
        "channels": ("ecg",),
    },
    PPG: {
        "name": "PPG",
        "record_size": 16,
        "sample_rate": 256.0,
        "bytes_per_second": 256.0 * 16,                   # 4096 B/s
        "history_records": HISTORY_BYTES // 16,           # 2048 records
        "finite_future_records": FINITE_FUTURE_BYTES // 16,      # 6144 records
        "channels": ("ir1", "ir2", "g1", "g2"),
    },
}

# Host safety bounds. The no-progress timeout (no ACK/DATA/END for this long) is
# the real stall detector; there is no whole-session ceiling for INFINITY.
HANDSHAKE_TIMEOUT_S = 5.0
NOPROGRESS_TIMEOUT_S = 15.0        # SENSOR_STREAM_CENTRAL_HOWTO.md §7 default
# INFINITY never declares a total; cap how many raw bytes the session will hold
# for diagnostics / a partial-record carry so a runaway stream can't grow
# unbounded host memory. The live decoder consumes spans as they arrive.
INFINITY_CARRY_CAP = 1 << 20      # 1 MiB


class ProtocolError(Exception):
    """A framing / ordering / geometry violation. Fatal to the session."""


# ── commands ────────────────────────────────────────────────────────────────

def build_command(opcode, stream_id):
    """8-byte command for the NUS RX characteristic."""
    if not (0 < stream_id <= 0xFFFFFFFF):
        raise ValueError("stream_id must be a nonzero uint32")
    return MAGIC + bytes([PROTOCOL_VERSION, opcode]) + stream_id.to_bytes(4, "little")


def new_stream_id():
    """A random nonzero uint32 suitable as a START / STOP stream ID."""
    import os
    return int.from_bytes(os.urandom(4), "little") or 1


# back-compat alias for callers/tests that still say "session"
new_session_id = new_stream_id


# ── framing ─────────────────────────────────────────────────────────────────

@dataclass
class Header:
    msg_type: int
    stream_id: int
    payload_len: int
    payload: bytes


def parse_header(msg):
    """Validate the common 12-byte TX header and split off the payload."""
    if len(msg) < HEADER_LEN:
        raise ProtocolError(f"notification too short: {len(msg)} bytes")
    if msg[0:2] != MAGIC:
        raise ProtocolError("bad magic")
    if msg[2] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {msg[2]}")
    msg_type = msg[3]
    stream_id = int.from_bytes(msg[4:8], "little")
    payload_len = int.from_bytes(msg[8:10], "little")
    flags = int.from_bytes(msg[10:12], "little")
    if flags != 0:
        raise ProtocolError(f"reserved flags nonzero: {flags:#06x}")
    if len(msg) != HEADER_LEN + payload_len:
        raise ProtocolError(
            f"length mismatch: notification {len(msg)}, header says {HEADER_LEN + payload_len}"
        )
    return Header(msg_type, stream_id, payload_len, bytes(msg[HEADER_LEN:]))


# ── message payloads ────────────────────────────────────────────────────────

@dataclass
class StartAck:
    mode: int
    history_units: int
    planned_total: int    # sensor bytes for FINITE; 0 (== unknown) for INFINITY

    @property
    def mode_name(self):
        return MODE_NAMES.get(self.mode, f"mode{self.mode}")


def parse_start_ack(payload):
    if len(payload) != START_ACK_PAYLOAD_LEN:
        raise ProtocolError(
            f"START_ACK payload is {len(payload)} bytes, expected {START_ACK_PAYLOAD_LEN}")

    mode = payload[0]
    history_units = payload[1]
    reserved = payload[2:8]
    planned_total = int.from_bytes(payload[8:16], "little")

    if mode not in (MODE_FINITE, MODE_INFINITY):
        raise ProtocolError(f"START_ACK bad mode {mode}")
    if history_units != HISTORY_UNITS:
        raise ProtocolError(
            f"START_ACK history units {history_units} != {HISTORY_UNITS}")
    if any(reserved):
        raise ProtocolError("START_ACK reserved bytes nonzero")
    if mode == MODE_FINITE and planned_total != FINITE_TOTAL_BYTES:
        raise ProtocolError(
            f"START_ACK FINITE total {planned_total} != {FINITE_TOTAL_BYTES}")
    if mode == MODE_INFINITY and planned_total != 0:
        raise ProtocolError(
            f"START_ACK INFINITY total {planned_total} != 0 (unknown)")
    return StartAck(mode, history_units, planned_total)


@dataclass
class DataMsg:
    offset: int
    data: bytes


def parse_data(payload):
    if len(payload) < DATA_OFFSET_LEN + 1:
        raise ProtocolError("DATA payload has no sensor bytes")
    offset = int.from_bytes(payload[0:DATA_OFFSET_LEN], "little")
    data = payload[DATA_OFFSET_LEN:]
    return DataMsg(offset, data)


@dataclass
class EndMsg:
    status: int

    @property
    def status_name(self):
        return END_STATUS_NAMES.get(self.status, f"0x{self.status:04x}")


def parse_end(payload):
    if len(payload) != END_PAYLOAD_LEN:
        raise ProtocolError(f"END payload is {len(payload)} bytes, expected {END_PAYLOAD_LEN}")
    return EndMsg(int.from_bytes(payload[0:2], "little"))


@dataclass
class ResultMsg:
    status: int

    @property
    def status_name(self):
        return RESULT_STATUS_NAMES.get(self.status, f"0x{self.status:04x}")


def parse_result(payload):
    if len(payload) != RESULT_PAYLOAD_LEN:
        raise ProtocolError(
            f"RESULT payload is {len(payload)} bytes, expected {RESULT_PAYLOAD_LEN}")
    return ResultMsg(int.from_bytes(payload[0:2], "little"))


# ── session state machine ───────────────────────────────────────────────────

START_PENDING = "START_PENDING"
RECEIVING = "RECEIVING"
COMPLETE = "COMPLETE"      # FINITE SUCCESS, fully validated
STOPPED = "STOPPED"       # explicit matching STOP won the terminal decision
REJECTED = "REJECTED"     # START rejected via RESULT
FAILED = "FAILED"         # framing / ordering / device-fault termination

_TERMINAL = {COMPLETE, STOPPED, REJECTED, FAILED}

_UINT64_MAX = (1 << 64) - 1


@dataclass
class StreamSession:
    """Feed one whole TX notification at a time via :meth:`feed`; never
    concatenate notifications.

    ``product`` is ``"ECG"`` / ``"PPG"`` (known from the advertised name).
    ``expect_mode`` is ``MODE_FINITE`` / ``MODE_INFINITY`` and must match the
    START command that was sent. ``on_span(offset, data)`` — if set — is called
    with every accepted contiguous DATA span, in order, for incremental
    decoding; the raw bytes are still accumulated in ``payload`` for FINITE
    (so the whole capture can be saved) but not for INFINITY.
    """

    stream_id: int
    product: str = ECG
    expect_mode: int = MODE_FINITE
    on_span: object = None

    state: str = START_PENDING
    start_ack: StartAck = None
    result: ResultMsg = None
    end: EndMsg = None
    error: str = None
    payload: bytearray = field(default_factory=bytearray)

    _next_offset: int = 0

    # ---- introspection --------------------------------------------------

    @property
    def is_terminal(self):
        return self.state in _TERMINAL

    @property
    def mode(self):
        return self.start_ack.mode if self.start_ack else self.expect_mode

    @property
    def bytes_received(self):
        return self._next_offset

    @property
    def bytes_total(self):
        """Planned FINITE total, or ``None`` for INFINITY / before START_ACK."""
        if self.start_ack and self.start_ack.mode == MODE_FINITE:
            return self.start_ack.planned_total
        return None

    @property
    def history_done(self):
        """True once the transport cursor has passed the 32 KiB history region."""
        return self._next_offset >= HISTORY_BYTES

    @property
    def phase_name(self):
        return "forward" if self.history_done else "history"

    @property
    def fs(self):
        return PROFILE[self.product]["sample_rate"]

    def provenance(self, **extra):
        d = {
            "protocol_version": PROTOCOL_VERSION,
            "stream_id": self.stream_id,
            "product": PROFILE[self.product]["name"],
            "mode": MODE_NAMES.get(self.mode, str(self.mode)),
            "final_state": self.state,
            "bytes_received": self._next_offset,
        }
        if self.start_ack:
            d["planned_total"] = self.start_ack.planned_total
        if self.end:
            d["final_status"] = self.end.status_name
        d.update(extra)
        return d

    # ---- the state machine --------------------------------------------

    def feed(self, notification):
        """Process one notification. Returns a list of ``(kind, obj)`` events
        where ``kind`` is ``'start_ack' | 'data' | 'end' | 'result'``. Raises
        :class:`ProtocolError` (and moves to FAILED) on any violation."""
        try:
            return self._feed(bytes(notification))
        except ProtocolError as e:
            if self.state not in (REJECTED, STOPPED, COMPLETE):
                self.state = FAILED
            self.error = str(e)
            raise

    def _feed(self, notification):
        if self.is_terminal:
            raise ProtocolError(f"notification after terminal state {self.state}")

        header = parse_header(notification)
        if header.stream_id != self.stream_id:
            raise ProtocolError(
                f"stream id {header.stream_id:#010x} != expected {self.stream_id:#010x}")

        if self.state == START_PENDING:
            if header.msg_type == MSG_START_ACK:
                ack = parse_start_ack(header.payload)
                if ack.mode != self.expect_mode:
                    raise ProtocolError(
                        f"START_ACK mode {ack.mode_name} != requested "
                        f"{MODE_NAMES.get(self.expect_mode)}")
                self.start_ack = ack
                self.state = RECEIVING
                return [("start_ack", ack)]
            if header.msg_type == MSG_RESULT:
                self.result = parse_result(header.payload)
                self.state = REJECTED
                self.error = self.result.status_name
                return [("result", self.result)]
            raise ProtocolError(
                f"expected START_ACK/RESULT in START_PENDING, got {header.msg_type:#04x}")

        # state == RECEIVING
        if header.msg_type == MSG_DATA:
            return [("data", self._accept_data(header.payload))]
        if header.msg_type == MSG_END:
            return [("end", self._accept_end(header.payload))]
        if header.msg_type == MSG_RESULT:
            # a rejected STOP (WRONG_SESSION, ...) — resolves the command but
            # leaves this stream running; surface it, don't terminate.
            self.result = parse_result(header.payload)
            return [("result", self.result)]
        if header.msg_type == MSG_START_ACK:
            raise ProtocolError("second START_ACK")
        raise ProtocolError(f"unexpected message type {header.msg_type:#04x} in RECEIVING")

    def _accept_data(self, payload):
        msg = parse_data(payload)
        if msg.offset != self._next_offset:
            raise ProtocolError(
                f"DATA offset {msg.offset} != expected {self._next_offset}")

        frag_len = len(msg.data)
        if frag_len > _UINT64_MAX - self._next_offset:
            raise ProtocolError("DATA offset would overflow uint64")

        total = self.bytes_total
        if total is not None and self._next_offset + frag_len > total:
            raise ProtocolError(
                f"FINITE DATA past declared total: "
                f"{self._next_offset + frag_len} > {total}")

        if self.on_span is not None:
            self.on_span(self._next_offset, bytes(msg.data))

        if self.mode == MODE_FINITE:
            self.payload.extend(msg.data)
        else:
            # INFINITY: don't grow payload unboundedly; keep only a bounded
            # tail for diagnostics (the live decoder already consumed the span)
            self.payload.extend(msg.data)
            if len(self.payload) > INFINITY_CARRY_CAP:
                del self.payload[:len(self.payload) - INFINITY_CARRY_CAP]

        self._next_offset += frag_len
        return msg

    def _accept_end(self, payload):
        end = parse_end(payload)
        self.end = end

        if end.status == END_SUCCESS:
            if self.mode != MODE_FINITE:
                self.state = FAILED
                self.error = "INFINITY stream ended with SUCCESS"
            elif self._next_offset != self.start_ack.planned_total:
                self.state = FAILED
                self.error = (f"SUCCESS but received {self._next_offset} of "
                              f"{self.start_ack.planned_total} bytes")
            else:
                self.state = COMPLETE
        elif end.status == END_STOPPED:
            # explicit user STOP — falling short of any target is expected;
            # a 1..(record_size-1) partial tail is permitted and kept as-is.
            self.state = STOPPED
        else:
            # NOT_RECORDING / STORAGE_ERROR / INTERNAL_ERROR / BUFFER_OVERFLOW
            # / anything unknown: a real termination. Keep the validated prefix
            # but mark the session failed.
            self.state = FAILED
            self.error = f"status {end.status_name}"
        return end
