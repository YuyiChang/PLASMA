"""Offline coverage for the MSense sensor-stream **v0** codec / session FSM.

The message builders live in the shipped :mod:`plasma.devices.msense.nus_sim`
module (the simulated wristband in ``plasma.devices.msense_demo`` streams the
same frames), so the tests and the simulator can't drift from each other.
"""
import pytest

from plasma.devices.msense import nus_stream as ns, nus_sim
from plasma.devices.msense.nus_stream import (
    StreamSession, ProtocolError, parse_start_ack, build_command,
    OP_START, OP_STOP, OP_START_INFINITY,
    MSG_START_ACK, MSG_DATA, MSG_END, MSG_RESULT,
    MODE_FINITE, MODE_INFINITY, FINITE_TOTAL_BYTES, HISTORY_BYTES,
    END_SUCCESS, END_STOPPED, END_STORAGE_ERROR, END_NOT_RECORDING,
    ECG, PPG,
)

SID = 0x11223344


def _msg(msg_type, payload, sid=SID):
    return nus_sim.message(msg_type, payload, sid)


# ── command / header ────────────────────────────────────────────────────────

def test_build_command_shape():
    cmd = build_command(OP_START, SID)
    assert cmd == b"MS" + bytes([0, OP_START]) + SID.to_bytes(4, "little")
    assert len(cmd) == 8


def test_build_command_opcodes():
    assert build_command(OP_STOP, SID)[3] == 0x02
    assert build_command(OP_START_INFINITY, SID)[3] == 0x03


def test_build_command_rejects_zero_session():
    with pytest.raises(ValueError):
        build_command(OP_START, 0)


def test_header_length_mismatch():
    s = StreamSession(SID, product=PPG)
    good = _msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE))
    with pytest.raises(ProtocolError):
        s.feed(good + b"\x00")


def test_header_wrong_stream_id():
    s = StreamSession(SID, product=PPG)
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(), sid=0x99999999))


def test_header_wrong_version():
    s = StreamSession(SID, product=PPG)
    frame = bytearray(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    frame[2] = 1  # v1
    with pytest.raises(ProtocolError):
        s.feed(bytes(frame))


def test_header_nonzero_flags():
    s = StreamSession(SID, product=PPG)
    frame = bytearray(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    frame[10] = 1
    with pytest.raises(ProtocolError):
        s.feed(bytes(frame))


# ── START_ACK validation ────────────────────────────────────────────────────

def test_start_ack_finite():
    ack = parse_start_ack(nus_sim.start_ack_payload(MODE_FINITE))
    assert ack.mode == MODE_FINITE
    assert ack.planned_total == FINITE_TOTAL_BYTES
    assert ack.history_units == 8


def test_start_ack_infinity():
    ack = parse_start_ack(nus_sim.start_ack_payload(MODE_INFINITY))
    assert ack.mode == MODE_INFINITY
    assert ack.planned_total == 0


def test_start_ack_bad_length():
    with pytest.raises(ProtocolError):
        parse_start_ack(b"\x00" * 15)


def test_start_ack_bad_mode():
    p = bytearray(nus_sim.start_ack_payload())
    p[0] = 5
    with pytest.raises(ProtocolError):
        parse_start_ack(bytes(p))


def test_start_ack_bad_history_units():
    with pytest.raises(ProtocolError):
        parse_start_ack(nus_sim.start_ack_payload(history_units=4))


def test_start_ack_nonzero_reserved():
    with pytest.raises(ProtocolError):
        parse_start_ack(nus_sim.start_ack_payload(reserved=b"\x00\x01\x00\x00\x00\x00"))


def test_start_ack_finite_wrong_total():
    with pytest.raises(ProtocolError):
        parse_start_ack(nus_sim.start_ack_payload(MODE_FINITE, planned_total=999))


def test_start_ack_infinity_nonzero_total():
    with pytest.raises(ProtocolError):
        parse_start_ack(nus_sim.start_ack_payload(MODE_INFINITY, planned_total=131072))


# ── FINITE happy path + reassembly ─────────────────────────────────────────

@pytest.mark.parametrize("product", [PPG, ECG])
@pytest.mark.parametrize("mtu", [128, 247, 498])
def test_finite_stream_reassembles(product, mtu):
    body = bytes((i * 37) % 256 for i in range(FINITE_TOTAL_BYTES))
    s = StreamSession(SID, product=product, expect_mode=MODE_FINITE)
    for frame in nus_sim.finite_sequence(SID, body, mtu=mtu):
        s.feed(frame)
    assert s.state == ns.COMPLETE
    assert bytes(s.payload) == body
    assert s.bytes_received == FINITE_TOTAL_BYTES
    assert s.provenance()["final_status"] == "SUCCESS"


def test_finite_success_short_fails():
    body = bytes(FINITE_TOTAL_BYTES - 100)
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    for m in nus_sim.data_stream(body, stream_id=SID):
        s.feed(m)
    s.feed(nus_sim.end_message(END_SUCCESS, SID))
    assert s.state == ns.FAILED
    assert "of 131072" in s.error


def test_finite_data_past_total_fails():
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    for m in nus_sim.data_stream(bytes(FINITE_TOTAL_BYTES), stream_id=SID):
        s.feed(m)
    with pytest.raises(ProtocolError):
        s.feed(nus_sim.data_message(FINITE_TOTAL_BYTES, b"\x00" * 8, SID))
    assert s.state == ns.FAILED


# ── offset reassembly / framing violations ─────────────────────────────────

def test_data_before_start_ack():
    s = StreamSession(SID, product=PPG)
    with pytest.raises(ProtocolError):
        s.feed(nus_sim.data_message(0, b"\x00" * 16, SID))


def test_second_start_ack_is_error():
    s = StreamSession(SID, product=PPG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    assert s.state == ns.FAILED


def test_gap_in_offsets_fails():
    s = StreamSession(SID, product=PPG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    s.feed(nus_sim.data_message(0, b"\x11" * 32, SID))
    with pytest.raises(ProtocolError):
        s.feed(nus_sim.data_message(64, b"\x22" * 32, SID))   # skipped 32..64
    assert s.state == ns.FAILED


def test_duplicate_offset_fails():
    s = StreamSession(SID, product=PPG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    s.feed(nus_sim.data_message(0, b"\x11" * 32, SID))
    with pytest.raises(ProtocolError):
        s.feed(nus_sim.data_message(0, b"\x11" * 32, SID))
    assert s.state == ns.FAILED


def test_empty_data_payload_fails():
    s = StreamSession(SID, product=PPG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload()))
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_DATA, (0).to_bytes(8, "little"), SID))


def test_fragment_splitting_a_record_ok():
    """A DATA fragment may end mid-record; the session just tracks bytes and
    the decoder's reassembler handles record boundaries."""
    body = bytes((i * 7) % 256 for i in range(FINITE_TOTAL_BYTES))
    spans = []
    s = StreamSession(SID, product=PPG, on_span=lambda o, d: spans.append((o, d)))
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    # odd fragment size => most fragments split a 16-byte record
    for m in nus_sim.data_stream(body, stream_id=SID, fragment=17):
        s.feed(m)
    s.feed(nus_sim.end_message(END_SUCCESS, SID))
    assert s.state == ns.COMPLETE
    assert b"".join(d for _, d in spans) == body


# ── STOP / END semantics ───────────────────────────────────────────────────

def test_stop_keeps_partial_and_is_clean():
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    partial = bytes(5000)
    for m in nus_sim.data_stream(partial, stream_id=SID):
        s.feed(m)
    s.feed(nus_sim.end_message(END_STOPPED, SID))
    assert s.state == ns.STOPPED
    assert s.is_terminal
    assert s.bytes_received == 5000
    assert s.error is None


def test_not_recording_end_fails_but_keeps_prefix():
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    for m in nus_sim.data_stream(bytes(4096), stream_id=SID):
        s.feed(m)
    s.feed(nus_sim.end_message(END_NOT_RECORDING, SID))
    assert s.state == ns.FAILED
    assert s.error == "status NOT_RECORDING"
    assert s.bytes_received == 4096


def test_storage_error_end_fails():
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    s.feed(nus_sim.end_message(END_STORAGE_ERROR, SID))
    assert s.state == ns.FAILED
    assert s.error == "status STORAGE_ERROR"


def test_data_after_end_is_error():
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    s.feed(nus_sim.end_message(END_STOPPED, SID))
    with pytest.raises(ProtocolError):
        s.feed(nus_sim.data_message(0, b"\x00" * 16, SID))


# ── RESULT ─────────────────────────────────────────────────────────────────

def test_rejected_start_via_result():
    s = StreamSession(SID, product=PPG)
    events = s.feed(nus_sim.result_message(ns.RESULT_BUSY, SID))
    assert s.state == ns.REJECTED
    assert events[0][1].status_name == "BUSY"


def test_result_after_start_ack_is_rejected_stop_not_fatal():
    """A RESULT while RECEIVING is a rejected STOP: surfaced as an event, the
    stream stays active."""
    s = StreamSession(SID, product=ECG)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))
    events = s.feed(nus_sim.result_message(ns.RESULT_WRONG_SESSION, SID))
    assert events[0][0] == "result"
    assert s.state == ns.RECEIVING


# ── INFINITY ───────────────────────────────────────────────────────────────

def test_infinity_incremental_consume_bounded_buffer():
    spans = []
    s = StreamSession(SID, product=PPG, expect_mode=MODE_INFINITY,
                      on_span=lambda o, d: spans.append(len(d)))
    chunks = (bytes(4096) for _ in range(2000))   # ~8 MiB total
    for frame in nus_sim.infinity_chunks(SID, chunks, mtu=247):
        s.feed(frame)
    assert s.state == ns.RECEIVING
    assert s.bytes_received == 2000 * 4096
    assert sum(spans) == 2000 * 4096
    # payload carry stays bounded despite ~8 MiB streamed
    assert len(s.payload) <= ns.INFINITY_CARRY_CAP


def test_infinity_stop_ends_stopped():
    s = StreamSession(SID, product=PPG, expect_mode=MODE_INFINITY)
    frames = list(nus_sim.infinity_chunks(SID, [bytes(4096)]))
    for f in frames:
        s.feed(f)
    s.feed(nus_sim.end_message(END_STOPPED, SID))
    assert s.state == ns.STOPPED


def test_infinity_success_is_error():
    s = StreamSession(SID, product=PPG, expect_mode=MODE_INFINITY)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_INFINITY)))
    s.feed(nus_sim.end_message(END_SUCCESS, SID))
    assert s.state == ns.FAILED


def test_mode_mismatch_rejected():
    s = StreamSession(SID, product=PPG, expect_mode=MODE_INFINITY)
    with pytest.raises(ProtocolError):
        s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_FINITE)))


def test_history_done_flag():
    s = StreamSession(SID, product=ECG, expect_mode=MODE_INFINITY)
    s.feed(_msg(MSG_START_ACK, nus_sim.start_ack_payload(MODE_INFINITY)))
    assert not s.history_done
    for m in nus_sim.data_stream(bytes(HISTORY_BYTES), stream_id=SID):
        s.feed(m)
    assert s.history_done
    assert s.phase_name == "forward"
