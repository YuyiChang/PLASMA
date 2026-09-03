"""Extraction options — the pure half.

`ExtractionOptions` is what the extraction pipeline consumes. The Gradio
accordion that produces one (`ExtractionOptionsPanel`) lives in
`plasma/devices/msense/panels/extractor.py`; what is shared is the field list
and the selector vocabularies declared here.

Leaf module: imports only `..formats`. No Gradio.
"""
from dataclasses import dataclass, field, fields

from ..formats import spec_names


def format_choices(sensor):
    """Selector vocabulary for one sensor: modes first, then explicit layouts."""
    return ["auto", "version"] + list(spec_names(sensor))


PPG_FORMAT_CHOICES = format_choices("ppg") + ["sniff"]   # "sniff" = deprecated alias for auto
AC_FORMAT_CHOICES = format_choices("ac")
ECG_FORMAT_CHOICES = format_choices("ecg")
CONFLICT_CHOICES = ["warn", "raise", "trust_uuid"]

FORMAT_HELP = """### Record format

Every layout is detected from file contents by default — `uuid.txt` is not
needed, and where the two disagree the contents win.

| Choice | Meaning |
|---|---|
| `auto` | Detect from content, per file. Falls back to the device version if inconclusive. **Default.** |
| `version` | Follow `uuid.txt` only (v4.7.0+ → `v2`, otherwise `legacy`). The pre-1.6 behaviour. |
| `legacy` / `v2` / `packed16` / `framed` / `v3` | Force that layout. |

| Sensor | Layouts |
|---|---|
| PPG | `legacy` 24 B · `v2` 20 B · `packed16` 16 B (no version tie) |
| IMU | `legacy` 30 B record · `v2` 26 B record · `v3` self-describing 4 MiB chunk (no version tie), 2 g / 16384 counts-per-g |
| ECG | `framed` 12 B |

**Cross-check against uuid.txt** reports when detection and the version file
disagree. It is off by default and does not override detection: use
*On conflict → trust_uuid* for that.

**Strict record validation**: raise on a record that fails its integrity check
(packed16 reserved bits, ECG CRC, a partial trailing record) instead of dropping
it and reporting a count.
"""


@dataclass
class ExtractionOptions:
    """Everything that changes how binaries are decoded and written out.

    Deliberately excludes per-run metadata (input/output paths, note, encoding
    table): those differ per surface, while these must not.
    """
    legacy_fs: bool = False
    save_format: str = "csv"
    ignore_id_parsing: bool = False
    ppg_format: str = "auto"
    ac_format: str = "auto"
    ecg_format: str = "auto"
    validate_with_uuid: bool = False
    on_format_conflict: str = "warn"
    strict_ppg: bool = False
    force_new_format: bool = False
    sniff_threshold: float = field(default=0.90, metadata={"panel": False})
    dry_run: bool = field(default=False, metadata={"panel": False})

    def __post_init__(self):
        # "sniff" was the 1.5 name for what "auto" now does.
        if self.ppg_format == "sniff":
            self.ppg_format = "auto"

    def format_for(self, sensor):
        return getattr(self, f"{sensor}_format")

    @classmethod
    def from_args(cls, args):
        """Build from the argparse namespace of the extract CLI."""
        return cls(
            legacy_fs=args.legacy_fs,
            save_format=args.save_format,
            ignore_id_parsing=args.ignore_id,
            ppg_format=args.ppg_format,
            ac_format=args.ac_format,
            ecg_format=args.ecg_format,
            validate_with_uuid=args.validate_with_uuid,
            on_format_conflict=args.on_format_conflict,
            strict_ppg=args.strict_ppg,
            force_new_format=args.force_new_format,
            sniff_threshold=args.sniff_threshold,
            dry_run=args.dry_run,
        )


# fields the Gradio panel exposes (CLI-only fields carry metadata panel=False)
PANEL_FIELDS = tuple(f.name for f in fields(ExtractionOptions)
                     if f.metadata.get("panel", True))
