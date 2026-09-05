"""Minimal XDF (Extensible Data Format) file writer — the container
LabRecorder produces and pyxdf / MNE / EEGLAB / SigViewer read.

No official Python XDF *writer* exists (pyxdf is read-only), so this is a
hand-rolled encoder of the small, well-documented XDF chunk format
(https://github.com/sccn/xdf/wiki/Specifications). Pure stdlib — no pylsl,
no third-party deps — so it is fully unit-testable by writing a file and
reading it back with pyxdf.

Format recap (every integer little-endian):

    file    = b"XDF:" , chunk*
    chunk   = varlen(2 + len(content)) , uint16(tag) , content
    varlen(n) = b"\\x01" + n(1 byte)    if n < 2**8
                b"\\x04" + n(4 bytes)   if n < 2**32
                b"\\x08" + n(8 bytes)   otherwise

    tag 1 FileHeader    content = utf-8 xml  "<info><version>1.0</version></info>"
    tag 2 StreamHeader  content = uint32(stream_id) , utf-8 xml (<info>…</info>)
    tag 3 Samples       content = uint32(stream_id) , varlen(n_samples) ,
                                  per sample: b"\\x08" , float64(timestamp) , values
                                  values = numeric: struct-packed channel_count fields
                                           string : per channel varlen(len) + utf-8 bytes
    tag 4 ClockOffset   content = uint32(stream_id) , float64(collection_time) , float64(offset)
    tag 5 Boundary      content = 16-byte magic uuid
    tag 6 StreamFooter  content = uint32(stream_id) , utf-8 xml
                                  (<info><first_timestamp>…<last_timestamp>…<sample_count>…</info>)

This encoder always emits an explicit per-sample timestamp (the b"\\x08"
form). That is fully valid XDF; pyxdf still dejitters regular-rate streams
and does cross-stream clock correction on load.

Not thread-safe by design: a single writer thread owns the file (see
`plasma.lsl_recorder.SessionRecorder`).
"""
import os
import struct
from xml.sax.saxutils import escape

# tag 5 content — a fixed 16-byte marker so a truncated/crashed recording can
# still be recovered up to the last boundary.
_BOUNDARY_UUID = bytes.fromhex("43A546DCCBF5410FB30ED5467383CBE4")

# XDF channel-format word -> struct format char (None == string format)
_FMT_CHAR = {
    "double64": "d",
    "float32": "f",
    "int64": "q",
    "int32": "i",
    "int16": "h",
    "int8": "b",
    "string": None,
}

_FILE_HEADER_XML = b'<?xml version="1.0"?><info><version>1.0</version></info>'


def _varlen(n):
    """XDF length prefix: a 1-byte selector (1/4/8) then that many LE bytes."""
    if n < 0:
        raise ValueError(f"length cannot be negative: {n}")
    if n < 2 ** 8:
        return b"\x01" + n.to_bytes(1, "little")
    if n < 2 ** 32:
        return b"\x04" + n.to_bytes(4, "little")
    if n < 2 ** 64:
        return b"\x08" + n.to_bytes(8, "little")
    raise ValueError(f"length too large for XDF varlen: {n}")


class XDFWriter:
    MAGIC = b"XDF:"
    BOUNDARY_UUID = _BOUNDARY_UUID

    def __init__(self):
        self._f = None
        self._bytes = 0
        # stream_id -> (struct_char_or_None, channel_count)
        self._fmt = {}
        # stream_id -> [first_ts, last_ts, count] running totals for the footer
        self._stats = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    def open(self, path):
        if self._f is not None:
            raise RuntimeError("XDFWriter already open")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._f = open(path, "wb")
        self._bytes = 0
        self._f.write(self.MAGIC)
        self._bytes += len(self.MAGIC)
        self._chunk(1, _FILE_HEADER_XML)

    def close(self):
        if self._f is None:
            return
        self._f.flush()
        self._f.close()
        self._f = None

    @property
    def bytes_written(self):
        return self._bytes

    # ── chunk writers ──────────────────────────────────────────────────────

    def write_stream_header(self, stream_id, info_xml, channel_format, channel_count):
        """`info_xml` is passed straight through — typically
        `pylsl.StreamInfo.as_xml()`, which already carries name / type /
        channel_count / channel_format / nominal_srate / <desc>."""
        if channel_format not in _FMT_CHAR:
            raise ValueError(f"unknown channel_format {channel_format!r}")
        self._fmt[stream_id] = (_FMT_CHAR[channel_format], int(channel_count))
        self._stats[stream_id] = [None, None, 0]
        if isinstance(info_xml, str):
            info_xml = info_xml.encode("utf-8")
        self._chunk(2, struct.pack("<I", stream_id) + info_xml)

    def write_samples(self, stream_id, timestamps, samples):
        """`timestamps`: sequence of float LSL-clock seconds. `samples`:
        sequence of per-sample sequences (numeric values, or str values for a
        string-format stream). Both must be the same length."""
        if stream_id not in self._fmt:
            raise RuntimeError(f"stream {stream_id} has no header")
        if len(timestamps) != len(samples):
            raise ValueError("timestamps and samples length mismatch")
        if not timestamps:
            return
        char, nchan = self._fmt[stream_id]
        parts = [struct.pack("<I", stream_id), _varlen(len(timestamps))]
        if char is not None:
            packer = struct.Struct("<" + char * nchan).pack
            for ts, sample in zip(timestamps, samples):
                parts.append(b"\x08")
                parts.append(struct.pack("<d", float(ts)))
                parts.append(packer(*sample))
        else:
            for ts, sample in zip(timestamps, samples):
                parts.append(b"\x08")
                parts.append(struct.pack("<d", float(ts)))
                for value in sample:
                    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
                    parts.append(_varlen(len(raw)))
                    parts.append(raw)
        self._chunk(3, b"".join(parts))

        st = self._stats[stream_id]
        if st[0] is None:
            st[0] = float(timestamps[0])
        st[1] = float(timestamps[-1])
        st[2] += len(timestamps)

    def write_clock_offset(self, stream_id, collection_time, offset):
        self._chunk(4, struct.pack("<Idd", stream_id, float(collection_time), float(offset)))

    def write_boundary(self):
        self._chunk(5, self.BOUNDARY_UUID)

    def write_stream_footer(self, stream_id, first_ts=None, last_ts=None, sample_count=None):
        """Any of the totals may be omitted; when None they're filled from
        what this writer actually wrote for the stream."""
        st = self._stats.get(stream_id, [None, None, 0])
        first_ts = st[0] if first_ts is None else first_ts
        last_ts = st[1] if last_ts is None else last_ts
        sample_count = st[2] if sample_count is None else sample_count
        bits = ['<?xml version="1.0"?><info>']
        if first_ts is not None:
            bits.append(f"<first_timestamp>{escape(repr(float(first_ts)))}</first_timestamp>")
        if last_ts is not None:
            bits.append(f"<last_timestamp>{escape(repr(float(last_ts)))}</last_timestamp>")
        bits.append(f"<sample_count>{int(sample_count)}</sample_count>")
        bits.append("</info>")
        self._chunk(6, struct.pack("<I", stream_id) + "".join(bits).encode("utf-8"))

    # ── internal ───────────────────────────────────────────────────────────

    def _chunk(self, tag, content):
        if self._f is None:
            raise RuntimeError("XDFWriter is not open")
        body = struct.pack("<H", tag) + content
        blob = _varlen(len(body)) + body
        self._f.write(blob)
        self._bytes += len(blob)
