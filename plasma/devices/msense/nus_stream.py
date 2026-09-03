"""NUS bounded sensor-stream protocol (version 1) — pure codec + session FSM.

No BLE / device state lives here: this module turns raw notification byte
strings into validated protocol events and reassembles the fixed 96 KiB sensor
payload. See local_docs/NUS_SENSOR_STREAM_CENTRAL_HANDOFF.md for the wire
contract; plasma/devices/msense.py drives it over an actual BLE link and
plasma/ppg_ecg_records.py decodes the reassembled payload.
"""
from dataclasses import dataclass, field

MAGIC = b"\x4d\x53"  # 'MS'
PROTOCOL_VERSION = 1

# command opcodes (host -> peripheral, written to NUS RX)
OP_START = 0x01
OP_CANCEL = 0x02

# TX message types (peripheral -> host, NUS TX notifications)
MSG_START_ACK = 0x81
MSG_DATA = 0x82
MSG_END = 0x83
MSG_RESULT = 0x84

HEADER_LEN = 12
START_ACK_PAYLOAD_LEN = 96
RESULT_PAYLOAD_LEN = 4
END_PAYLOAD_LEN = 24
DATA_PREFIX_LEN = 12
TOTAL_SENSOR_BYTES = 98304  # fixed in protocol version 1

PHASE_HISTORY = 0
PHASE_FORWARD = 1

STATUS_NAMES = {
    0x0000: "SUCCESS",
    0x0001: "NOT_RECORDING",
    0x0002: "HISTORY_NOT_READY",
    0x0003: "NOT_SUBSCRIBED",
    0x0004: "BUSY",
    0x0005: "MTU_TOO_SMALL",
    0x0006: "INVALID_COMMAND",
    0x0007: "UNSUPPORTED_VERSION",
    0x0008: "CANCELLED",
    0x0009: "STORAGE_ERROR",
    0x000A: "INTERNAL_ERROR",
    0x000B: "NOT_INITIALIZED",
    0x000C: "WRONG_SESSION",
    0x000D: "DISCONNECTED",
}
STATUS_SUCCESS = 0x0000

STATE_NAMES = {
    0x00: "NOT_RECORDING",
    0x01: "HISTORY_FILLING",
    0x02: "READY",
    0x03: "ACTIVE",
    0x04: "ABORTING",
    0x05: "UNINITIALIZED",
}

# device-type -> expected record geometry (START_ACK validation table)
DEVICE_PPG = 1
DEVICE_ECG = 2
PROFILE = {
    DEVICE_PPG: {
        "name": "PPG",
        "record_size": 16,
        "rate_hz": 256.0,
        "history_records": 2048,
        "forward_records": 4096,
        "channels": ("ir1", "ir2", "g1", "g2"),
        "overall_timeout_s": 120.0,
        "fresh_history_s": 8.0,
    },
    DEVICE_ECG: {
        "name": "ECG",
        "record_size": 12,
        "rate_hz": 512.0,
        "history_records": 2731,
        "forward_records": 5461,
        "channels": ("ecg",),
        "overall_timeout_s": 90.0,
        "fresh_history_s": 5.334,
    },
}

# Host safety bounds. The idle timeout (no DATA/END for this long) is the real
# stall detector; overall_timeout_s above is just a generous backstop. The
# handoff doc's "at least 35/45 s" overall figures are provisional minimums
# measured on a fast link — a 96 KiB transfer over a slow BLE link plus the
# forward-window acquisition legitimately runs longer while still progressing.
HANDSHAKE_TIMEOUT_S = 5.0
IDLE_TIMEOUT_S = 8.0


class ProtocolError(Exception):
    """A framing / ordering / geometry violation. Fatal to the session."""


# ── commands ────────────────────────────────────────────────────────────────

def build_command(opcode, session_id):
    """8-byte command for the NUS RX characteristic."""
    if not (0 < session_id <= 0xFFFFFFFF):
        raise ValueError("session_id must be a nonzero uint32")
    return MAGIC + bytes([PROTOCOL_VERSION, opcode]) + session_id.to_bytes(4, "little")


def new_session_id():
    """A random nonzero uint32 suitable as a START request/session ID."""
    import os
    return int.from_bytes(os.urandom(4), "little") or 1


# ── framing ─────────────────────────────────────────────────────────────────

@dataclass
class Header:
    msg_type: int
    session_id: int
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
    session_id = int.from_bytes(msg[4:8], "little")
    payload_len = int.from_bytes(msg[8:10], "little")
    flags = int.from_bytes(msg[10:12], "little")
    if flags != 0:
        raise ProtocolError(f"reserved flags nonzero: {flags:#06x}")
    if len(msg) != HEADER_LEN + payload_len:
        raise ProtocolError(
            f"length mismatch: notification {len(msg)}, header says {HEADER_LEN + payload_len}"
        )
    return Header(msg_type, session_id, payload_len, bytes(msg[HEADER_LEN:]))


# ── message payloads ────────────────────────────────────────────────────────

@dataclass
class StartAck:
    device_type: int
    record_format_version: int
    record_size: int
    rate_num: int
    rate_den: int
    history_records: int
    forward_records: int
    total_bytes: int
    device_id_hex: str
    device_name: str
    git_commit: str
    git_tree_state: int

    @property
    def device_name_label(self):
        return PROFILE.get(self.device_type, {}).get("name", f"type{self.device_type}")

    @property
    def rate_hz(self):
        return self.rate_num / self.rate_den if self.rate_den else 0.0

    @property
    def git_tree_state_label(self):
        return {0: "clean", 1: "dirty", 2: "unknown"}.get(self.git_tree_state, "?")


def parse_start_ack(payload):
    if len(payload) != START_ACK_PAYLOAD_LEN:
        raise ProtocolError(f"START_ACK payload is {len(payload)} bytes, expected 96")

    device_type = payload[0]
    record_format_version = payload[1]
    record_size = int.from_bytes(payload[2:4], "little")
    rate_num = int.from_bytes(payload[4:8], "little")
    rate_den = int.from_bytes(payload[8:12], "little")
    history_records = int.from_bytes(payload[12:16], "little")
    forward_records = int.from_bytes(payload[16:20], "little")
    total_bytes = int.from_bytes(payload[20:24], "little")
    device_id_hex = payload[24:32].hex().upper()
    name_len = payload[32]
    name_field = payload[33:49]
    git_commit = payload[49:89].decode("ascii", errors="replace")
    git_tree_state = payload[89]
    reserved = payload[90:96]

    if name_len > 16:
        raise ProtocolError(f"START_ACK device-name length {name_len} > 16")
    if any(name_field[name_len:]):
        raise ProtocolError("START_ACK bytes after device name are nonzero")
    if len(git_commit) != 40 or any(c not in "0123456789abcdef" for c in git_commit):
        raise ProtocolError("START_ACK git commit is not 40 lowercase hex chars")
    if any(reserved):
        raise ProtocolError("START_ACK reserved bytes nonzero")
    if total_bytes != TOTAL_SENSOR_BYTES:
        raise ProtocolError(f"START_ACK total bytes {total_bytes} != {TOTAL_SENSOR_BYTES}")
    if (history_records + forward_records) * record_size != total_bytes:
        raise ProtocolError("START_ACK counts * record_size != total bytes")

    profile = PROFILE.get(device_type)
    if profile is None:
        raise ProtocolError(f"START_ACK unknown device type {device_type}")
    if record_size != profile["record_size"]:
        raise ProtocolError(
            f"START_ACK record size {record_size} != {profile['record_size']} for {profile['name']}"
        )
    if history_records != profile["history_records"] or forward_records != profile["forward_records"]:
        raise ProtocolError(
            f"START_ACK record geometry {history_records}/{forward_records} != "
            f"{profile['history_records']}/{profile['forward_records']} for {profile['name']}"
        )

    device_name = name_field[:name_len].decode("utf-8", errors="replace")
    return StartAck(
        device_type, record_format_version, record_size, rate_num, rate_den,
        history_records, forward_records, total_bytes, device_id_hex, device_name,
        git_commit, git_tree_state,
    )


@dataclass
class DataMsg:
    sequence: int
    first_record_index: int
    record_count: int
    phase: int
    records: bytes


def parse_data(payload, record_size):
    if len(payload) < DATA_PREFIX_LEN:
        raise ProtocolError("DATA payload shorter than prefix")
    sequence = int.from_bytes(payload[0:4], "little")
    first_record_index = int.from_bytes(payload[4:8], "little")
    record_count = int.from_bytes(payload[8:10], "little")
    phase = payload[10]
    reserved = payload[11]
    records = payload[DATA_PREFIX_LEN:]

    if reserved != 0:
        raise ProtocolError("DATA reserved byte nonzero")
    if phase not in (PHASE_HISTORY, PHASE_FORWARD):
        raise ProtocolError(f"DATA bad phase {phase}")
    if record_count == 0:
        raise ProtocolError("DATA record_count is zero")
    if len(payload) != DATA_PREFIX_LEN + record_count * record_size:
        raise ProtocolError(
            f"DATA length {len(payload)} != {DATA_PREFIX_LEN + record_count * record_size}"
        )
    return DataMsg(sequence, first_record_index, record_count, phase, records)


@dataclass
class ResultMsg:
    status: int
    peripheral_state: int

    @property
    def status_name(self):
        return STATUS_NAMES.get(self.status, f"0x{self.status:04x}")

    @property
    def peripheral_state_name(self):
        return STATE_NAMES.get(self.peripheral_state, f"0x{self.peripheral_state:02x}")


def parse_result(payload):
    if len(payload) != RESULT_PAYLOAD_LEN:
        raise ProtocolError(f"RESULT payload is {len(payload)} bytes, expected 4")
    if payload[3] != 0:
        raise ProtocolError("RESULT reserved byte nonzero")
    return ResultMsg(int.from_bytes(payload[0:2], "little"), payload[2])


@dataclass
class EndMsg:
    status: int
    peripheral_state: int
    history_records_sent: int
    forward_records_captured: int
    total_bytes_sent: int
    data_message_count: int
    detail: int

    @property
    def status_name(self):
        return STATUS_NAMES.get(self.status, f"0x{self.status:04x}")


def parse_end(payload):
    if len(payload) != END_PAYLOAD_LEN:
        raise ProtocolError(f"END payload is {len(payload)} bytes, expected 24")
    if payload[3] != 0:
        raise ProtocolError("END reserved byte nonzero")
    return EndMsg(
        status=int.from_bytes(payload[0:2], "little"),
        peripheral_state=payload[2],
        history_records_sent=int.from_bytes(payload[4:8], "little"),
        forward_records_captured=int.from_bytes(payload[8:12], "little"),
        total_bytes_sent=int.from_bytes(payload[12:16], "little"),
        data_message_count=int.from_bytes(payload[16:20], "little"),
        detail=int.from_bytes(payload[20:24], "little", signed=True),
    )


# ── session state machine ───────────────────────────────────────────────────

# states
START_PENDING = "START_PENDING"
RECEIVING = "RECEIVING"
COMPLETE = "COMPLETE"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

_TERMINAL = {COMPLETE, REJECTED, CANCELLED, FAILED}


@dataclass
class StreamSession:
    """Feed one whole TX notification at a time via ``feed``; never concatenate
    notifications. Terminal state is one of COMPLETE / REJECTED / CANCELLED /
    FAILED. On COMPLETE, ``payload`` holds exactly 98,304 validated bytes."""

    session_id: int
    state: str = START_PENDING
    start_ack: StartAck = None
    result: ResultMsg = None
    end: EndMsg = None
    error: str = None
    payload: bytearray = field(default_factory=bytearray)

    # running validation cursors
    _next_sequence: int = 0
    _next_record_index: int = 0
    _data_message_count: int = 0
    _phase: int = PHASE_HISTORY

    # ---- introspection --------------------------------------------------

    @property
    def is_terminal(self):
        return self.state in _TERMINAL

    @property
    def device_type(self):
        return self.start_ack.device_type if self.start_ack else None

    @property
    def records_total(self):
        if not self.start_ack:
            return 0
        return self.start_ack.history_records + self.start_ack.forward_records

    @property
    def records_received(self):
        if not self.start_ack:
            return 0
        return self._next_record_index

    @property
    def phase_name(self):
        return "forward" if self._phase == PHASE_FORWARD else "history"

    def provenance(self, **extra):
        """Sidecar metadata dict. Callers add wall-clock times etc. via kwargs."""
        d = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "final_state": self.state,
        }
        if self.start_ack:
            a = self.start_ack
            d.update(
                device_type=a.device_name_label,
                device_type_code=a.device_type,
                device_name=a.device_name,
                device_id=a.device_id_hex,
                git_commit=a.git_commit,
                git_tree_state=a.git_tree_state_label,
                record_format_version=a.record_format_version,
                record_size=a.record_size,
                record_rate=f"{a.rate_num}/{a.rate_den}",
                history_records=a.history_records,
                forward_records=a.forward_records,
            )
        if self.end:
            d.update(
                final_status=self.end.status_name,
                total_bytes=self.end.total_bytes_sent,
                data_message_count=self.end.data_message_count,
            )
        d.update(extra)
        return d

    # ---- the state machine --------------------------------------------

    def feed(self, notification):
        """Process one notification. Returns a list of (kind, obj) events where
        kind is 'start_ack' | 'data' | 'end' | 'result'. Raises ProtocolError
        (and moves to FAILED) on any violation."""
        try:
            return self._feed(bytes(notification))
        except ProtocolError as e:
            if self.state not in (REJECTED, CANCELLED, COMPLETE):
                self.state = FAILED
            self.error = str(e)
            raise

    def _feed(self, notification):
        if self.is_terminal:
            raise ProtocolError(f"notification after terminal state {self.state}")

        header = parse_header(notification)
        if header.session_id != self.session_id:
            raise ProtocolError(
                f"session id {header.session_id:#010x} != expected {self.session_id:#010x}"
            )

        if self.state == START_PENDING:
            if header.msg_type == MSG_START_ACK:
                self.start_ack = parse_start_ack(header.payload)
                self.state = RECEIVING
                return [("start_ack", self.start_ack)]
            if header.msg_type == MSG_RESULT:
                self.result = parse_result(header.payload)
                self.state = REJECTED
                self.error = self.result.status_name
                return [("result", self.result)]
            raise ProtocolError(f"expected START_ACK/RESULT in START_PENDING, got {header.msg_type:#04x}")

        # state == RECEIVING
        if header.msg_type == MSG_DATA:
            return [("data", self._accept_data(header.payload))]
        if header.msg_type == MSG_END:
            return [("end", self._accept_end(header.payload))]
        if header.msg_type == MSG_START_ACK:
            raise ProtocolError("second START_ACK")
        if header.msg_type == MSG_RESULT:
            raise ProtocolError("RESULT after accepted START_ACK")
        raise ProtocolError(f"unexpected message type {header.msg_type:#04x} in RECEIVING")

    def _accept_data(self, payload):
        a = self.start_ack
        msg = parse_data(payload, a.record_size)

        if msg.sequence != self._next_sequence:
            raise ProtocolError(f"DATA sequence {msg.sequence} != expected {self._next_sequence}")
        if msg.first_record_index != self._next_record_index:
            raise ProtocolError(
                f"DATA first index {msg.first_record_index} != expected {self._next_record_index}"
            )

        last_index = msg.first_record_index + msg.record_count - 1
        if msg.phase == PHASE_HISTORY:
            if self._phase == PHASE_FORWARD:
                raise ProtocolError("history DATA after forward phase started")
            if last_index >= a.history_records:
                raise ProtocolError("history DATA crosses into forward range")
        else:  # forward
            if self._phase == PHASE_HISTORY:
                if msg.first_record_index != a.history_records:
                    raise ProtocolError(
                        f"forward phase starts at index {msg.first_record_index}, "
                        f"expected {a.history_records}"
                    )
                self._phase = PHASE_FORWARD
            if last_index >= a.history_records + a.forward_records:
                raise ProtocolError("forward DATA past total record count")

        self.payload.extend(msg.records)
        self._next_sequence += 1
        self._next_record_index += msg.record_count
        self._data_message_count += 1
        return msg

    def _accept_end(self, payload):
        a = self.start_ack
        end = parse_end(payload)
        self.end = end

        failures = []
        if end.status != STATUS_SUCCESS:
            failures.append(f"status {end.status_name}")
        if end.history_records_sent != a.history_records:
            failures.append(f"history {end.history_records_sent} != {a.history_records}")
        if end.forward_records_captured != a.forward_records:
            failures.append(f"forward {end.forward_records_captured} != {a.forward_records}")
        if end.total_bytes_sent != TOTAL_SENSOR_BYTES or len(self.payload) != TOTAL_SENSOR_BYTES:
            failures.append(
                f"bytes end={end.total_bytes_sent} local={len(self.payload)} != {TOTAL_SENSOR_BYTES}"
            )
        if end.data_message_count != self._data_message_count:
            failures.append(
                f"DATA count end={end.data_message_count} local={self._data_message_count}"
            )
        if end.detail != 0:
            failures.append(f"detail {end.detail}")
        if self._next_record_index != a.history_records + a.forward_records:
            failures.append(
                f"records received {self._next_record_index} != {a.history_records + a.forward_records}"
            )

        if failures:
            if end.status == 0x0008:  # CANCELLED
                self.state = CANCELLED
            else:
                self.state = FAILED
            self.error = "; ".join(failures)
        else:
            self.state = COMPLETE
        return end
