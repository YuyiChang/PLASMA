"""Builders for NUS bounded-stream protocol messages — the inverse of the
parsers in :mod:`plasma.devices.msense.nus_stream`.

Pure byte assembly, no BLE and no device state. Two consumers:

* ``plasma.devices.msense.tests.test_nus_stream`` — round-trips every parser.
* ``plasma.devices.msense_demo`` — the simulated wristband streams these frames
  into the real driver's ``_nus_data_handler``.

Keeping the wire format in one shipped module (rather than redefining it in the
test) means the simulator and the tests can never drift from each other.
"""
import struct

from .nus_stream import (
    MAGIC, PROTOCOL_VERSION, MSG_DATA, MSG_END, PROFILE,
    PHASE_HISTORY, PHASE_FORWARD, TOTAL_SENSOR_BYTES,
)

__all__ = [
    "message", "start_ack_payload", "data_messages", "end_payload",
    "full_sequence",
]


def message(msg_type, payload, session_id):
    """Frame one TX notification: 12-byte header + payload."""
    return (
        MAGIC
        + bytes([PROTOCOL_VERSION, msg_type])
        + int(session_id).to_bytes(4, "little")
        + len(payload).to_bytes(2, "little")
        + b"\x00\x00"
        + bytes(payload)
    )


def start_ack_payload(device_type, *, name=b"MSense4X-SIM", commit=b"a" * 40,
                      tree_state=0, reserved=b"\x00" * 6, history=None,
                      forward=None, total=None, override=None):
    """96-byte START_ACK payload for ``device_type`` (DEVICE_PPG / DEVICE_ECG).

    ``history`` / ``forward`` / ``total`` default to the PROFILE geometry;
    ``override`` is a dict of raw field values for fuzzing (used by the tests).
    """
    p = PROFILE[device_type]
    history = p["history_records"] if history is None else history
    forward = p["forward_records"] if forward is None else forward
    total = TOTAL_SENSOR_BYTES if total is None else total
    fields = dict(
        device_type=device_type, fmt_ver=1, record_size=p["record_size"],
        rate_num=int(p["rate_hz"]), rate_den=1,
        history=history, forward=forward, total=total,
    )
    if override:
        fields.update(override)
    body = struct.pack(
        "<BBHIIIII", fields["device_type"], fields["fmt_ver"], fields["record_size"],
        fields["rate_num"], fields["rate_den"], fields["history"], fields["forward"],
        fields["total"],
    )
    body += b"\xde\xad\xbe\xef\x01\x02\x03\x04"          # 8-byte device id
    body += bytes([len(name)]) + name + b"\x00" * (16 - len(name))
    body += commit + bytes([tree_state]) + reserved
    assert len(body) == 96, len(body)
    return body


def data_messages(device_type, chunk_records, *, session_id, records=None,
                  history=None, forward=None):
    """A full valid history+forward DATA sequence, ``chunk_records`` records per
    message. Returns ``(framed_messages, message_count)``.

    ``records`` — the concatenated (history+forward) * record_size payload
    bytes; defaults to ``0x5a`` filler when the caller doesn't care about the
    decoded signal.
    """
    p = PROFILE[device_type]
    rs = p["record_size"]
    history = p["history_records"] if history is None else history
    forward = p["forward_records"] if forward is None else forward
    total = history + forward
    if records is None:
        records = b"\x5a" * (total * rs)

    msgs, seq, idx = [], 0, 0
    while idx < total:
        phase = PHASE_HISTORY if idx < history else PHASE_FORWARD
        room = (history - idx) if phase == PHASE_HISTORY else (total - idx)
        count = min(chunk_records, room)
        prefix = struct.pack("<IIHBB", seq, idx, count, phase, 0)
        chunk = records[idx * rs:(idx + count) * rs]
        msgs.append(message(MSG_DATA, prefix + chunk, session_id))
        seq += 1
        idx += count
    return msgs, seq


def end_payload(device_type, data_count, *, status=0, detail=0, override=None,
                history=None, forward=None):
    """24-byte END payload. Defaults describe a clean SUCCESS of the full
    PROFILE geometry; ``override`` fuzzes raw fields."""
    p = PROFILE[device_type]
    history = p["history_records"] if history is None else history
    forward = p["forward_records"] if forward is None else forward
    fields = dict(
        status=status, state=2, history=history, forward=forward,
        total=(history + forward) * p["record_size"],
        data_count=data_count, detail=detail,
    )
    if override:
        fields.update(override)
    return struct.pack(
        "<HBBIIIIi", fields["status"], fields["state"], 0, fields["history"],
        fields["forward"], fields["total"], fields["data_count"], fields["detail"],
    )


def full_sequence(device_type, session_id, *, records=None, chunk_records=256):
    """Every framed notification for a clean capture: START_ACK, the DATA burst,
    then a SUCCESS END. ``records`` is the decoded-signal payload (see
    ``data_messages``)."""
    from .nus_stream import MSG_START_ACK

    frames = [message(MSG_START_ACK, start_ack_payload(device_type), session_id)]
    data, n = data_messages(device_type, chunk_records,
                            session_id=session_id, records=records)
    frames.extend(data)
    frames.append(message(MSG_END, end_payload(device_type, n), session_id))
    return frames
