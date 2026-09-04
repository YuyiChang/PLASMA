"""Offline coverage for the NUS bounded-stream protocol codec / session FSM."""
import struct

import pytest

from plasma.devices.msense import nus_stream as ns
from plasma.devices.msense.nus_stream import (
    StreamSession, ProtocolError, parse_start_ack, build_command,
    OP_START, MSG_START_ACK, MSG_DATA, MSG_END, MSG_RESULT,
    PROFILE, DEVICE_PPG, DEVICE_ECG, TOTAL_SENSOR_BYTES,
)

SID = 0x11223344


def _msg(msg_type, payload, sid=SID):
    return ns.MAGIC + bytes([ns.PROTOCOL_VERSION, msg_type]) + sid.to_bytes(4, "little") \
        + len(payload).to_bytes(2, "little") + b"\x00\x00" + payload


def _start_ack_payload(device_type, *, name=b"MSense4X-TEST", commit=b"a" * 40,
                       tree_state=0, reserved=b"\x00" * 6, override=None):
    p = PROFILE[device_type]
    fields = dict(
        device_type=device_type, fmt_ver=1, record_size=p["record_size"],
        rate_num=int(p["rate_hz"]), rate_den=1,
        history=p["history_records"], forward=p["forward_records"],
        total=TOTAL_SENSOR_BYTES,
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


def _data_msgs(device_type, chunk_records, *, history=None, forward=None):
    """A full valid history+forward DATA sequence, `chunk_records` per message.

    `history`/`forward` default to the PROFILE table but can be overridden to
    exercise a different (still self-consistent) geometry, e.g. a newer
    firmware's record counts.
    """
    p = PROFILE[device_type]
    rs = p["record_size"]
    history = p["history_records"] if history is None else history
    forward = p["forward_records"] if forward is None else forward
    total = history + forward
    msgs, seq, idx = [], 0, 0
    while idx < total:
        phase = 0 if idx < history else 1
        # never cross the history/forward boundary
        room = (history - idx) if phase == 0 else (total - idx)
        count = min(chunk_records, room)
        prefix = struct.pack("<IIHBB", seq, idx, count, phase, 0)
        msgs.append(_msg(MSG_DATA, prefix + b"\x5a" * (count * rs)))
        seq += 1
        idx += count
    return msgs, seq


def _end_payload(device_type, data_count, *, status=0, detail=0, override=None,
                  history=None, forward=None):
    p = PROFILE[device_type]
    history = p["history_records"] if history is None else history
    forward = p["forward_records"] if forward is None else forward
    fields = dict(
        status=status, state=2, history=history,
        forward=forward, total=(history + forward) * p["record_size"],
        data_count=data_count, detail=detail,
    )
    if override:
        fields.update(override)
    return struct.pack(
        "<HBBIIIIi", fields["status"], fields["state"], 0, fields["history"],
        fields["forward"], fields["total"], fields["data_count"], fields["detail"],
    )


def _run(device_type, chunk_records=64):
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(device_type)))
    data, n = _data_msgs(device_type, chunk_records)
    for m in data:
        s.feed(m)
    s.feed(_msg(MSG_END, _end_payload(device_type, n)))
    return s


# ── command / header ────────────────────────────────────────────────────────

def test_build_command_shape():
    cmd = build_command(OP_START, SID)
    assert cmd == b"MS" + bytes([1, 1]) + SID.to_bytes(4, "little")
    assert len(cmd) == 8


def test_build_command_rejects_zero_session():
    with pytest.raises(ValueError):
        build_command(OP_START, 0)


def test_header_length_mismatch():
    s = StreamSession(SID)
    good = _msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG))
    with pytest.raises(ProtocolError):
        s.feed(good + b"\x00")


def test_header_wrong_session():
    s = StreamSession(SID)
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG), sid=0x99999999))


# ── START_ACK validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("device_type", [DEVICE_PPG, DEVICE_ECG])
def test_start_ack_valid_profiles(device_type):
    ack = parse_start_ack(_start_ack_payload(device_type))
    p = PROFILE[device_type]
    assert ack.record_size == p["record_size"]
    assert ack.history_records == p["history_records"]
    assert ack.device_id_hex == "DEADBEEF01020304"


def test_start_ack_bad_name_length():
    bad = bytearray(_start_ack_payload(DEVICE_PPG))
    bad[32] = 20  # name-length byte > 16
    with pytest.raises(ProtocolError):
        parse_start_ack(bytes(bad))


def test_start_ack_nonzero_after_name():
    bad = bytearray(_start_ack_payload(DEVICE_PPG, name=b"AB"))
    bad[40] = 0x7F  # inside the 16-byte name field, past the 2-char name
    with pytest.raises(ProtocolError):
        parse_start_ack(bytes(bad))


def test_start_ack_bad_commit():
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_PPG, commit=b"Z" * 40))


def test_start_ack_nonzero_reserved():
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_PPG, reserved=b"\x00\x00\x01\x00\x00\x00"))


def test_start_ack_inconsistent_geometry_rejected():
    """history/forward changed without recomputing total -> arithmetic
    self-consistency check ((history+forward)*record_size == total) fires.
    Exact history/forward counts are no longer pinned to PROFILE, but the
    reported fields must still agree with each other."""
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_PPG, override={"history": 999}))


def test_start_ack_inconsistent_total_rejected():
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_ECG, override={"total": 1234}))


def test_start_ack_new_firmware_geometry_accepted():
    """A self-consistent geometry that differs from the historical PROFILE
    counts (e.g. an ECG firmware build with a larger forward window) is
    accepted. Regression test for the 131,076-vs-98,304 outage: only
    self-consistency and the sane-record-count bound are enforced now, not
    an exact match against one memorized geometry."""
    history, forward, rs = 2731, 8192, PROFILE[DEVICE_ECG]["record_size"]
    total = (history + forward) * rs
    assert total == 131076 and total != TOTAL_SENSOR_BYTES
    ack = parse_start_ack(_start_ack_payload(DEVICE_ECG, override={
        "history": history, "forward": forward, "total": total,
    }))
    assert ack.history_records == history
    assert ack.forward_records == forward
    assert ack.total_bytes == total


def test_start_ack_record_size_mismatch_rejected():
    """record_size must still match the known per-device-type record format
    — the decoders are written for exactly 16-byte PPG / 12-byte ECG
    frames, so this is not relaxed."""
    p = PROFILE[DEVICE_PPG]
    bad_size = 99
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_PPG, override={
            "record_size": bad_size,
            "total": (p["history_records"] + p["forward_records"]) * bad_size,
        }))


def test_start_ack_geometry_exceeds_sane_bound_rejected():
    """A self-consistent but absurdly large record count is still rejected,
    so a garbled ACK can't make the host buffer an unbounded payload."""
    p = PROFILE[DEVICE_ECG]
    known_total = p["history_records"] + p["forward_records"]
    huge = known_total * (ns.MAX_RECORD_MULTIPLE + 1)
    history, forward, rs = huge // 2, huge - huge // 2, p["record_size"]
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_ECG, override={
            "history": history, "forward": forward, "total": (history + forward) * rs,
        }))


def test_start_ack_nonpositive_geometry_rejected():
    p = PROFILE[DEVICE_ECG]
    with pytest.raises(ProtocolError):
        parse_start_ack(_start_ack_payload(DEVICE_ECG, override={
            "history": 0, "forward": p["forward_records"],
            "total": p["forward_records"] * p["record_size"],
        }))


# ── happy path + reassembly ─────────────────────────────────────────────────

@pytest.mark.parametrize("device_type", [DEVICE_PPG, DEVICE_ECG])
@pytest.mark.parametrize("chunk", [1, 7, 64, 511])
def test_full_stream_reassembles(device_type, chunk):
    s = _run(device_type, chunk)
    assert s.state == ns.COMPLETE
    assert len(s.payload) == TOTAL_SENSOR_BYTES
    assert s.records_received == s.records_total
    prov = s.provenance()
    assert prov["final_status"] == "SUCCESS"
    assert prov["device_type"] == PROFILE[device_type]["name"]


# ── ordering / framing violations ───────────────────────────────────────────

def test_data_before_start_ack():
    s = StreamSession(SID)
    prefix = struct.pack("<IIHBB", 0, 0, 1, 0, 0)
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_DATA, prefix + b"\x00" * 16))


def test_second_start_ack_is_error():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    assert s.state == ns.FAILED


def test_result_after_start_ack_is_error():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_RESULT, struct.pack("<HBB", 0, 2, 0)))


def test_dropped_data_notification_fails_session():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    data, n = _data_msgs(DEVICE_PPG, 64)
    s.feed(data[0])
    with pytest.raises(ProtocolError):
        s.feed(data[2])  # skipped data[1]
    assert s.state == ns.FAILED


def test_reordered_data_notification_fails_session():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    data, n = _data_msgs(DEVICE_PPG, 64)
    s.feed(data[0])
    s.feed(data[1])
    with pytest.raises(ProtocolError):
        s.feed(data[1])  # duplicate / stale
    assert s.state == ns.FAILED


def test_data_after_end_is_error():
    s = _run(DEVICE_PPG, 128)
    data, _ = _data_msgs(DEVICE_PPG, 128)
    with pytest.raises(ProtocolError):
        s.feed(data[0])


# ── END validation ──────────────────────────────────────────────────────────

def test_full_stream_new_geometry_reassembles():
    """End-to-end run with new-firmware-style ECG geometry (a different
    total than the historical 98,304): the session must complete, proving
    the whole pipeline (not just parse_start_ack) is geometry-agnostic."""
    history, forward = 2731, 8192
    rs = PROFILE[DEVICE_ECG]["record_size"]
    total = (history + forward) * rs
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_ECG, override={
        "history": history, "forward": forward, "total": total,
    })))
    data, n = _data_msgs(DEVICE_ECG, 200, history=history, forward=forward)
    for m in data:
        s.feed(m)
    s.feed(_msg(MSG_END, _end_payload(DEVICE_ECG, n, history=history, forward=forward)))
    assert s.state == ns.COMPLETE
    assert len(s.payload) == total == 131076


def test_end_bytes_checked_against_session_total_not_global():
    """Regression test: _accept_end must compare END's total_bytes_sent (and
    the locally accumulated payload) against *this session's own*
    start_ack.total_bytes, not the historical TOTAL_SENSOR_BYTES constant.
    An END that reports the old global total instead of this session's real
    (larger) total must fail, not silently pass."""
    history, forward = 2731, 8192
    rs = PROFILE[DEVICE_ECG]["record_size"]
    total = (history + forward) * rs
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_ECG, override={
        "history": history, "forward": forward, "total": total,
    })))
    data, n = _data_msgs(DEVICE_ECG, 200, history=history, forward=forward)
    for m in data:
        s.feed(m)
    bad_end = _end_payload(DEVICE_ECG, n, history=history, forward=forward,
                            override={"total": TOTAL_SENSOR_BYTES})
    s.feed(_msg(MSG_END, bad_end))
    assert s.state == ns.FAILED
    assert "bytes" in s.error


def test_end_wrong_data_count_fails():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_ECG)))
    data, n = _data_msgs(DEVICE_ECG, 200)
    for m in data:
        s.feed(m)
    s.feed(_msg(MSG_END, _end_payload(DEVICE_ECG, n + 5)))
    assert s.state == ns.FAILED
    assert "DATA count" in s.error


def test_end_nonzero_detail_fails():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_ECG)))
    data, n = _data_msgs(DEVICE_ECG, 200)
    for m in data:
        s.feed(m)
    s.feed(_msg(MSG_END, _end_payload(DEVICE_ECG, n, detail=-3)))
    assert s.state == ns.FAILED


def test_rejected_start_via_result():
    s = StreamSession(SID)
    events = s.feed(_msg(MSG_RESULT, struct.pack("<HBB", 0x0002, 0x01, 0)))
    assert s.state == ns.REJECTED
    assert events[0][1].status_name == "HISTORY_NOT_READY"


def test_partial_session_never_complete():
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_PPG)))
    data, n = _data_msgs(DEVICE_PPG, 64)
    for m in data[:-1]:
        s.feed(m)
    # END arrives early, before the last DATA chunk
    s.feed(_msg(MSG_END, _end_payload(DEVICE_PPG, n)))
    assert s.state == ns.FAILED
    assert len(s.payload) != TOTAL_SENSOR_BYTES


def test_quick_mode_cancel_retains_partial_payload():
    """Quick mode: CANCEL during the stream -> device answers END/CANCELLED ->
    session is CANCELLED (not FAILED) and the partial payload is kept."""
    rs = PROFILE[DEVICE_ECG]["record_size"]
    s = StreamSession(SID)
    s.feed(_msg(MSG_START_ACK, _start_ack_payload(DEVICE_ECG)))

    data, _ = _data_msgs(DEVICE_ECG, 100)          # 100 records / message
    kept = 0
    for m in data[:10]:                            # ~1000 history records
        s.feed(m)
        kept += 100
    assert s.records_received == kept
    assert len(s.payload) == kept * rs

    # END with CANCELLED status and the partial counts the device actually sent
    end = _end_payload(DEVICE_ECG, 10, status=0x0008,
                       override={"history": kept, "forward": 0, "total": kept * rs})
    s.feed(_msg(MSG_END, end))

    assert s.state == ns.CANCELLED               # not FAILED
    assert s.is_terminal
    assert len(s.payload) == kept * rs           # payload retained, decodable
    assert s.records_received == kept
