"""Small LSL helpers shared by the outlet-creating device drivers and the
built-in recorder (`plasma.lsl_recorder`).

The `plasma_origin` desc tag is how the recorder tells PLASMA's own streams
(loopback) from streams produced by other software on the network — it shows
a 🛰️ icon for the latter and the tag also travels inside the recorded XDF,
so the archive is self-describing.
"""

_ORIGIN_TAG = "plasma_origin"


def mark_plasma_origin(info):
    """Tag a `pylsl.StreamInfo` as originating from PLASMA. Call this on the
    `StreamInfo` right before handing it to `StreamOutlet(info)`. Returns the
    same `info` for chaining. Best-effort — never raises (a missing/odd pylsl
    build must not break outlet creation)."""
    try:
        info.desc().append_child_value(_ORIGIN_TAG, "1")
    except Exception:
        pass
    return info


def is_plasma_origin(streaminfo):
    """True if `streaminfo` (a resolved `pylsl.StreamInfo`, e.g. from
    `inlet.info()`) carries the `plasma_origin` desc tag."""
    try:
        node = streaminfo.desc().child(_ORIGIN_TAG)
        return (not node.empty()) and node.first_child().value() == "1"
    except Exception:
        return False


# pylsl channel_format() int -> XDF channel-format word.
# (pylsl.cf_float32=1, cf_double64=2, cf_string=3, cf_int32=4,
#  cf_int16=5, cf_int8=6, cf_int64=7, cf_undefined=0)
_FORMAT_NAMES = {
    1: "float32",
    2: "double64",
    3: "string",
    4: "int32",
    5: "int16",
    6: "int8",
    7: "int64",
}


def channel_format_name(fmt):
    """pylsl `channel_format()` int -> XDF format word. Raises on cf_undefined
    / anything unknown (an unrecordable stream — the caller should skip it)."""
    try:
        name = _FORMAT_NAMES.get(int(fmt))
    except (TypeError, ValueError):
        name = None
    if name is None:
        raise ValueError(f"unrecordable LSL channel_format: {fmt!r}")
    return name
