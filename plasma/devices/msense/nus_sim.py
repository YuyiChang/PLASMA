"""Builders for MSense sensor-stream **v0** protocol messages — the inverse of
the parsers in :mod:`plasma.devices.msense.nus_stream`.

Pure byte assembly, no BLE and no device state. Two consumers:

* ``plasma.devices.msense.tests.test_nus_stream`` — round-trips every parser.
* ``plasma.devices.msense_demo`` — the simulated wristband streams these frames
  into the real driver's ``_nus_data_handler``.

Keeping the wire format in one shipped module (rather than redefining it in the
test) means the simulator and the tests can never drift from each other.
"""
from .nus_stream import (
    MAGIC, PROTOCOL_VERSION, MSG_START_ACK, MSG_DATA, MSG_END, MSG_RESULT,
    MODE_FINITE, MODE_INFINITY, HISTORY_UNITS, FINITE_TOTAL_BYTES,
    END_SUCCESS, DATA_OFFSET_LEN, HEADER_LEN, ATT_MTU_MIN,
)

__all__ = [
    "message", "start_ack_payload", "data_message", "data_stream",
    "end_message", "result_message", "finite_sequence", "infinity_chunks",
]

# ATT overhead (3) + envelope (12) + DATA offset prefix (8) = 23 non-sensor
# bytes per notification.
_DATA_NONSENSOR = 3 + HEADER_LEN + DATA_OFFSET_LEN


def message(msg_type, payload, stream_id):
    """Frame one TX notification: 12-byte header + payload."""
    return (
        MAGIC
        + bytes([PROTOCOL_VERSION, msg_type])
        + int(stream_id).to_bytes(4, "little")
        + len(payload).to_bytes(2, "little")
        + b"\x00\x00"
        + bytes(payload)
    )


def start_ack_payload(mode=MODE_FINITE, *, planned_total=None,
                      history_units=HISTORY_UNITS, reserved=b"\x00" * 6):
    """16-byte START_ACK payload. ``planned_total`` defaults to
    ``FINITE_TOTAL_BYTES`` for FINITE and ``0`` for INFINITY."""
    if planned_total is None:
        planned_total = FINITE_TOTAL_BYTES if mode == MODE_FINITE else 0
    body = bytes([mode, history_units]) + bytes(reserved) + planned_total.to_bytes(8, "little")
    assert len(body) == 16, len(body)
    return body


def data_message(offset, sensor_bytes, stream_id):
    """One DATA notification: uint64 offset + sensor bytes."""
    payload = int(offset).to_bytes(DATA_OFFSET_LEN, "little") + bytes(sensor_bytes)
    return message(MSG_DATA, payload, stream_id)


def data_stream(sensor_bytes, *, stream_id, start_offset=0, mtu=247,
                fragment=None):
    """Split ``sensor_bytes`` into a list of DATA notifications, each carrying
    at most ``fragment`` sensor bytes (default: the ATT-MTU limit
    ``mtu - 23``)."""
    if fragment is None:
        fragment = max(1, min(mtu, ATT_MTU_MIN if mtu < ATT_MTU_MIN else mtu) - _DATA_NONSENSOR)
    msgs = []
    off = start_offset
    view = memoryview(bytes(sensor_bytes))
    for i in range(0, len(view), fragment):
        chunk = view[i:i + fragment]
        msgs.append(data_message(off, chunk, stream_id))
        off += len(chunk)
    return msgs


def end_message(status, stream_id):
    return message(MSG_END, int(status).to_bytes(2, "little"), stream_id)


def result_message(status, stream_id):
    return message(MSG_RESULT, int(status).to_bytes(2, "little"), stream_id)


def finite_sequence(stream_id, sensor_bytes, *, mtu=247, status=END_SUCCESS):
    """Every framed notification for a clean FINITE capture: START_ACK, the
    DATA burst, then an END. ``sensor_bytes`` is the full history+future
    payload (exactly ``FINITE_TOTAL_BYTES`` for a real SUCCESS)."""
    frames = [message(MSG_START_ACK, start_ack_payload(MODE_FINITE), stream_id)]
    frames.extend(data_stream(sensor_bytes, stream_id=stream_id, mtu=mtu))
    frames.append(end_message(status, stream_id))
    return frames


def infinity_chunks(stream_id, chunk_iter, *, mtu=247):
    """Generator of framed notifications for an INFINITY capture: one
    START_ACK, then DATA notifications for each bytes object yielded by
    ``chunk_iter``. The caller decides when to stop (and may append an
    ``end_message`` afterwards)."""
    yield message(MSG_START_ACK, start_ack_payload(MODE_INFINITY), stream_id)
    off = 0
    for chunk in chunk_iter:
        for m in data_stream(chunk, stream_id=stream_id, start_offset=off, mtu=mtu):
            yield m
        off += len(chunk)
