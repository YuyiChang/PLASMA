# original credit: https://github.com/SenSE-Lab-OSU/MotionSenseHRV4Flash/blob/main/DataExtraction/data_extraction.py
"""Offline `.bin` -> CSV extraction pipeline.

Pure: no Gradio. Returns an `ExtractionReport`; the Gradio panels
(`plasma/devices/msense/panels/extractor.py`) turn that into `gr.Info` /
`gr.DownloadButton`. `print(...)` progress diagnostics are kept (console / CI log).
"""
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from glob import glob

import numpy
import numpy as np
import pandas as pd
from tqdm import tqdm

from .. import detect, formats
from ..detect import Resolution
from .options import ExtractionOptions

# ---------------------------------------------------------------------------
# Record layouts and format resolution live in ..formats / ..detect.
# Re-exported here because the packed16 unit tests and external callers import
# these names from this module.
# ---------------------------------------------------------------------------
get_CDCT_init = formats.get_CDCT_init
read_bin = formats.read_bin
decode_ppg_packed16 = formats.decode_ppg_packed16
read_ppg_bin_packed16 = formats.read_ppg_bin_packed16
PPG_PACKED_RECORD_SIZE = formats.PPG_PACKED_RECORD_SIZE
PPG_PACKED_SAMPLE_MASK = formats.PPG_PACKED_SAMPLE_MASK
PPG_PACKED_RESERVED_MASK = formats.PPG_PACKED_RESERVED_MASK

SENSOR_ORDER = ("ac", "ppg", "ecg")

# save_format -> output file extension. "csv" is the implicit default (not
# listed — see DataExtractor.run()). Feather (Arrow IPC, via pyarrow) is the
# default: ~20-30x faster to write than CSV and ~2-4x smaller on disk for
# these numeric-heavy tables, at the cost of no longer being plain text —
# `clocksync.py`'s YAMS sync tool is CSV-only, so pick save_format="csv"
# explicitly if you need to feed extracted output into that.
_SAVE_FORMAT_EXT = {"pickle": ".pkl", "feather": ".feather"}

# Session encoding table — subject/session <-> numeric encoding, for CSV
# filename aliasing. Relative to CWD by default (run from your data dir); the
# panels pass an explicit path under the app data dir.
DEFAULT_SESSION_TABLE = "session_table.csv"


@dataclass
class ExtractionReport:
    """What one extraction produced."""
    resolutions: list = field(default_factory=list)   # list[Resolution]
    malformed: int = 0                                 # corrupt records dropped
    dropped: int = 0                                   # samples the firmware never wrote (ACF3 seq gaps)
    out_paths: list = field(default_factory=list)      # written CSV/PKL paths
    readme_path: str | None = None
    n_files: int = 0                                   # .bin files inspected

    def summary(self) -> str:
        n_out = len(self.out_paths)
        s = f"Extracted {n_out} file(s) from {self.n_files} binary(ies)"
        if self.malformed:
            s += f"; {self.malformed} malformed record(s) dropped"
        if self.dropped:
            s += f"; {self.dropped} sample(s) lost to firmware drops"
        conflicts = sum(1 for r in self.resolutions if r.agrees is False)
        if conflicts:
            s += f"; {conflicts} uuid.txt conflict(s)"
        return s


def get_participant_ids(folder_path):
    prefixes = set()
    for filename in os.listdir(folder_path):
        if not filename.endswith(".bin"):
            continue

        match = re.match(r"(\d*)ppg\d+\.bin$", filename)
        if match:
            prefix = match.group(1)
            if prefix == "":
                prefixes.add('')
            else:
                prefixes.add(str(prefix))
    return sorted(prefixes, key=lambda x: (x is None, x))


def get_device_version(folder_path):
    uuid_path = os.path.join(folder_path, "uuid.txt")
    if not os.path.exists(uuid_path):
        return (0, 0, 0)
    with open(uuid_path, 'r') as f:
        content = f.read()
    match = re.search(r'Version:\s*(\d+)\.(\d+)\.(\d+)', content)
    if match:
        return tuple(int(x) for x in match.groups())
    return (0, 0, 0)


_UUID_NAME_RE = re.compile(r'^\s*name\s*[:=]\s*(\S.*?)\s*$', re.IGNORECASE | re.MULTILINE)


def get_device_name(folder_path):
    """The ``Name:`` field from a device's ``uuid.txt``, sanitised for use as a
    folder name — ``None`` when there is no ``uuid.txt`` or no ``Name`` line.

    New firmware (v5+) writes e.g. ``Name: MSense4ECG-EX4BT``; this replaces the
    old BLE-address → configured-name lookup table for per-device folders.
    ``folder_path`` may be the device folder or the ``uuid.txt`` itself.
    """
    uuid_path = (folder_path if os.path.basename(folder_path) == "uuid.txt"
                 else os.path.join(folder_path, "uuid.txt"))
    try:
        with open(uuid_path, "r") as f:
            content = f.read()
    except OSError:
        return None
    m = _UUID_NAME_RE.search(content)
    if not m:
        return None
    safe = re.sub(r'[^\w.\-]+', '_', m.group(1)).strip('_.')
    return safe or None


def sniff_ppg_format(filepath, n_probe=2000, threshold=0.9):
    """Detect a PPG file's layout from its contents. None if inconclusive."""
    return detect.sniff_file(filepath, "ppg", threshold=threshold)


def get_session_encoding(path=None):
    path = path or DEFAULT_SESSION_TABLE
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame(data={
        'subject_id': ["sub-Test"],
        "session_id": ["ses-01"],
        "encoding": [123],
    })


def sensor_of(filename):
    """Which sensor a binary belongs to, by the tag in its name."""
    for sensor in ("ppg", "ecg", "ac"):
        if sensor in filename:
            return sensor
    return None


class DataExtractor():
    def __init__(self, in_dir, out_dir, df=None, note="", options=None):
        options = options or ExtractionOptions()
        self.options = options
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.note = note
        self.df = df
        self.save_format = options.save_format
        self.ignore_id_parsing = options.ignore_id_parsing
        self.strict = options.strict_ppg

        self.device_version = get_device_version(in_dir)
        version_str = ".".join(str(x) for x in self.device_version)
        if self.device_version == (0, 0, 0):
            version_str += " (no uuid.txt)"
        print(f"device version: {version_str}")
        print("record formats: " + ", ".join(
            f"{s}={options.format_for(s)}" for s in SENSOR_ORDER))

        # Format is resolved per file, not per folder: a folder can hold captures
        # from more than one firmware, and the resolution is evidence we keep.
        self.resolutions = []
        self.malformed = 0
        self.dropped = 0
        self.out_paths = []

        self.encoding_alias = self.get_encoding_alias() if self.df is not None else {}

        if not options.dry_run:
            os.makedirs(out_dir, exist_ok=True)
            self.write_readme_header()

    def write_readme_header(self):
        options = self.options
        self.readme_path = os.path.join(self.out_dir, "README.txt")
        with open(self.readme_path, "w") as file:
            file.write(f"Raw data directory = {self.in_dir}\n")
            file.write(f"Legacy sampling rate = {options.legacy_fs} (no effect; see docs/data_extraction.md)\n")
            file.write(f"Save format = {options.save_format}\n")
            file.write(f"Ignore subject/session ID parsing = {options.ignore_id_parsing}\n")
            for sensor in SENSOR_ORDER:
                file.write(f"{sensor.upper()} record format = {options.format_for(sensor)} (requested)\n")
            file.write(f"Cross-check against uuid.txt = {options.validate_with_uuid}"
                       f" (on conflict: {options.on_format_conflict})\n")
            file.write(f"Strict record validation = {options.strict_ppg}\n")
            file.write(f"Detection threshold = {options.sniff_threshold}\n")
            file.write("I m-sense with YAMS at https://github.com/SenSE-Lab-OSU/YAMS\n")
            uuid_path = os.path.join(self.in_dir, "uuid.txt")
            if os.path.exists(uuid_path):
                file.write("\n--- Device info (uuid.txt) ---\n")
                with open(uuid_path, "r") as uuid_file:
                    file.write(uuid_file.read())

    def get_encoding_alias(self):
        alias_dict = {}
        for i in range(len(self.df.index)):
            curr = self.df.iloc[i]
            alias_dict[f"{curr['encoding']}"] = f"{curr['subject_id']}_{curr['session_id']}_{self.note}_{curr['encoding']}"
        return alias_dict

    def run(self):
        if self.options.dry_run:
            return self.dry_run()

        ids = self.obtain_predix_ids()
        for id in ids:
            for sensor in SENSOR_ORDER:
                search_prefix = id + sensor
                file_name = search_prefix + _SAVE_FORMAT_EXT.get(self.save_format, ".csv")
                self.extract_csv(search_prefix, file_name, id=id)

        self.write_provenance()

    def dry_run(self):
        """Resolve every binary and report, without decoding or writing anything."""
        for file in sorted(os.listdir(self.in_dir)):
            if not file.endswith(".bin"):
                continue
            sensor = sensor_of(file)
            if sensor is not None:
                self.resolve(os.path.join(self.in_dir, file), sensor)

        print("\n" + Resolution.header())
        for res in self.resolutions:
            print(res.row())
        print(f"\n(dry run — {len(self.resolutions)} file(s) inspected, nothing written)")
        return self.resolutions

    def write_provenance(self):
        """Append the per-file format resolution to README.txt.

        packed16 carries no version number, so for those files this table is the
        only record of how a CSV was decoded.
        """
        if not self.resolutions:
            return
        with open(os.path.join(self.out_dir, "README.txt"), "a") as file:
            file.write("\n--- Format resolution ---\n")
            file.write(Resolution.header() + "\n")
            for res in self.resolutions:
                file.write(res.row() + "\n")
            file.write(f"\nMalformed records dropped = {self.malformed}\n")
            file.write(f"Samples lost to firmware drops (ACF3 seq gaps) = {self.dropped}\n")
            conflicts = [r for r in self.resolutions if r.agrees is False]
            if conflicts:
                file.write(f"uuid.txt conflicts = {len(conflicts)} "
                           f"(content used unless on_format_conflict=trust_uuid)\n")

    def resolve(self, full_path, sensor):
        res = detect.resolve(
            full_path, sensor, self.options.format_for(sensor), self.device_version,
            force_new_format=self.options.force_new_format,
            validate_with_uuid=self.options.validate_with_uuid,
            on_conflict=self.options.on_format_conflict,
            threshold=self.options.sniff_threshold,
        )
        self.resolutions.append(res)
        return res

    def read_file(self, full_path, sensor):
        res = self.resolve(full_path, sensor)
        df, dt = formats.read_bin(full_path, res.spec, strict=self.strict)
        self.malformed += df.attrs.get('malformed_records', 0)
        self.dropped += df.attrs.get('dropped_samples', 0)
        return df, res

    def extract_csv(self, search_prefix, file_name, id=-1):
        self.generate_csv_for_pattern(self.in_dir, file_name, search_prefix,
                                      out_dir=self.out_dir, id=id)

    def generate_csv_for_pattern(self, in_dir, type_prefix: str, search_key: str, out_dir="./", id=-1):
        # 1. Ignore ID Parsing Handling
        if self.ignore_id_parsing:
            file_name = type_prefix  # Defaults to id + "ac.csv" or ".pkl"
        else:
            if str(id) in self.encoding_alias.keys():
                alias = self.encoding_alias[str(id)]
                print('=====', id, alias)
                file_name = f"{type_prefix}".replace(id, alias)
            else:
                sub_id = str(id)[:-2]
                ses_id = str(id)[-2:]
                alias = f"sub-{sub_id}_ses-{ses_id}_{self.note}_"
                file_name = f"{type_prefix}".replace(id, alias)

        print(type_prefix, search_key, '********')
        # session_dfs: one DataFrame per distinct recording/session sharing this
        # prefix (already chunk-stitched — see _collect_session_frames). Kept as
        # a list rather than one big pd.concat so each one can be finished
        # (Datetime + unit conversion) and written straight to disk in turn —
        # this avoids holding a second, fully-combined copy of everything
        # sharing this prefix in memory at once, and lets writing session N
        # overlap with whatever the caller does next instead of "concat
        # everything, then write everything."
        session_dfs, spec = self._collect_session_frames(in_dir, search_key)
        if not session_dfs:
            return

        os.makedirs(out_dir, exist_ok=True)
        is_ac = 'ac' in search_key
        out_path = os.path.join(out_dir, file_name)
        # pickle and feather are whole-object writes (no incremental append at
        # the pandas level, unlike CSV) — collect the finished chunks and write
        # once. Both formats are fast enough (see docs/data_extraction.md)
        # that concatenating first costs little next to the write itself.
        whole_frame = self.save_format in ("pickle", "feather")
        pieces = [] if whole_frame else None

        for i, chunk in enumerate(session_dfs):
            # Counter semantics come from the layout that was actually decoded.
            # Checked per session rather than on one outer concat of possibly
            # several distinct recordings, so a legitimate gap *between*
            # recordings is never mistaken for a dropped-sample run within one.
            counter_validity_check(chunk, spec)
            chunk = _finish_chunk(chunk, is_ac=is_ac, spec=spec)
            if whole_frame:
                pieces.append(chunk)
            else:
                chunk.to_csv(out_path, index=False, mode="w" if i == 0 else "a",
                             header=(i == 0))

        if whole_frame:
            combined = pd.concat(pieces, ignore_index=True)
            if self.save_format == "pickle":
                combined.to_pickle(out_path)
            else:
                combined.reset_index(drop=True).to_feather(out_path)

        self.out_paths.append(out_path)

    def collect_all_data_by_prefix(self, path, prefix: str):
        """Concatenate every binary matching `prefix`. Returns (df, spec) or (None, None).

        Chunks of one session (chunked `<id><sensor><session_id>_<chunk>.bin`
        naming) share a single filename-derived t0, but the flat per-record
        formats' `read_bin` computes CDCT as a cumsum starting at 0 per file —
        concatenating chunks as-is would restart the clock at every chunk
        boundary. Group by t0 (== by session; a non-chunked file is its own
        one-chunk "session") and, for any session spanning more than one chunk,
        restitch CDCT as one continuous clock anchored at that session's t0.

        Container formats need no CDCT recompute:
        - `ac:v3` (ACF3) anchors every sample to a session-continuous RTC tick
          per block internally. Chunks are re-ordered by `chunk_index` and a
          `first_sample_sequence` discontinuity at a chunk boundary (firmware
          drop, or a missing chunk) is reported.
        - `ecg:block_v2` (ECF2) makes `CDCT = t0 + Counter/512` where `Counter`
          is the recording-local sample ordinal that already advances across
          chunks — so plain concatenation is continuous. ECF2 chunks are
          re-ordered by `chunk_index` and split by `recording_id`, and a gap
          in `chunk_index` or `Counter` is reported (not silently joined).
        """
        session_dfs, spec = self._collect_session_frames(path, prefix)
        if not session_dfs:
            return None, None
        return pd.concat(session_dfs, ignore_index=True), spec

    def _collect_session_frames(self, path, prefix: str):
        """As `collect_all_data_by_prefix`, but returns `(session_dfs, spec)` —
        the list of per-recording/session DataFrames — without the final
        `pd.concat`, so `generate_csv_for_pattern` can finish and write each
        one in turn instead of materializing a second, fully-combined copy of
        every recording sharing this prefix."""
        files = gather_files_by_prefix(prefix, path)
        if len(files) == 0:
            return [], None

        sessions, spec = {}, None    # t0 -> [df, ...], in chunk order
        for file in files:
            sensor = sensor_of(file)
            if sensor is None:
                continue
            full_path = os.path.join(path, file)
            df, res = self.read_file(full_path, sensor)
            spec = res.spec
            t0, _ = formats.get_CDCT_init(full_path)
            sessions.setdefault(t0, []).append(df)

        if not sessions:
            return [], None

        is_ecb2 = spec is not None and spec.key == "ecg:block_v2"
        is_ac_v3 = spec is not None and spec.key == "ac:v3"
        session_dfs = []
        for t0, dfs in sessions.items():
            if is_ecb2:
                session_dfs.extend(_stitch_ecb2_chunks(dfs))
            elif is_ac_v3:
                session_dfs.append(_stitch_ac_v3_chunks(dfs))
            elif len(dfs) == 1:
                session_dfs.append(dfs[0])
            else:
                combined = pd.concat(dfs, ignore_index=True)
                if spec.read_file is None:     # flat per-record formats only
                    combined = formats.recompute_cdct(combined, spec, t0)
                session_dfs.append(combined)

        return session_dfs, spec

    def obtain_predix_ids(self):
        all_files = [""]
        files = os.listdir(self.in_dir)
        for file in files:
            if file[0].isdigit():
                id = re.search(r'\d+', file)
                if id is not None:
                    id = id.group()
                    if id not in all_files:
                        all_files.append(id)
        return all_files


def file_sort(element1: str):
    numeric_index = element1.find(it_prefix)
    numeric_time = element1[numeric_index + len(it_prefix):len(element1)]
    return int(re.sub(r"\D", "", numeric_time))


def gather_files_by_prefix(prefix: str, path):
    global it_prefix
    it_prefix = prefix
    all_files = []
    files = os.listdir(path)
    for file in files:
        if file.startswith(prefix) and file.endswith('.bin'):
            all_files.append(file)
    all_files.sort(key=file_sort)
    return all_files


def _stitch_ecb2_chunks(dfs):
    """Order ECF2 chunk DataFrames by `recording_id` then `chunk_index`,
    report any `chunk_index` or `Counter` discontinuity, and return one
    concatenated DataFrame per recording (CDCT is already continuous — see
    `formats._read_ecf2`)."""
    by_recording = {}
    for df in dfs:
        by_recording.setdefault(df.attrs.get("recording_id"), []).append(df)

    out = []
    for rec_id, group in by_recording.items():
        group.sort(key=lambda d: d.attrs.get("chunk_index", 0))
        prev = None
        for d in group:
            ci = d.attrs.get("chunk_index", 0)
            if prev is not None:
                if ci != prev.attrs.get("chunk_index", 0) + 1:
                    print(f"ECG recording {rec_id}: missing chunk between index "
                          f"{prev.attrs.get('chunk_index')} and {ci} — joined anyway")
                gap = d.attrs.get("first_sample_index", 0) - prev.attrs.get("last_sample_index", -1)
                if gap != 1:
                    print(f"ECG recording {rec_id}: {gap - 1} sample gap at the "
                          f"chunk {ci} boundary")
            prev = d
        out.append(pd.concat(group, ignore_index=True))
    return out


def _stitch_ac_v3_chunks(dfs):
    """Order ACF3 chunk DataFrames by `chunk_index` and report a
    `first_sample_sequence` discontinuity at a chunk boundary — a firmware drop
    ("a larger difference records the number of missing samples", per the
    format doc) or a missing chunk. CDCT is already absolute per chunk (each
    ACB1 block carries its own RTC anchor), so this only concatenates."""
    group = sorted(dfs, key=lambda d: d.attrs.get("chunk_index", 0))
    prev = None
    for d in group:
        ci = d.attrs.get("chunk_index", 0)
        if ci == 0 and d.attrs.get("first_seq") not in (None, 0):
            print(f"AC session: chunk 0 starts at sequence "
                  f"{d.attrs.get('first_seq')}, not 0 — earlier data missing")
        if prev is not None:
            if ci != prev.attrs.get("chunk_index", 0) + 1:
                print(f"AC session: missing chunk between index "
                      f"{prev.attrs.get('chunk_index')} and {ci} — joined anyway")
            first, last = d.attrs.get("first_seq"), prev.attrs.get("last_seq")
            if first is not None and last is not None:
                gap = (int(first) - int(last) - 1) & ((1 << 32) - 1)
                if gap:
                    print(f"AC session: {gap} sample(s) dropped at the chunk "
                          f"{ci} boundary")
        prev = d
    return pd.concat(group, ignore_index=True)


def counter_validity_check(df: pd.DataFrame, spec=None):
    """Report how many counter deltas depart from the layout's expected step.

    The expected step comes from the spec that was actually decoded, so this no
    longer has to guess it from the data or branch on a version flag.
    """
    if spec is None:
        print("pass counter check: N/A (no format resolved)")
        return
    # The readers append CDCT/init_CDCT, so the last column is not the counter.
    counter_columns = df[['Counter']] if 'Counter' in df.columns else df.iloc[:, -1:]
    counter_arr = numpy.array(counter_columns).flatten()
    diff_arr = numpy.diff(counter_arr)
    step = spec.tick_step
    # step: nominal. 2*step: one dropped sample. |d| near the modulus: rollover,
    # in either sign depending on whether the column survived as signed.
    check_array = ((diff_arr == step) | (diff_arr == step * 2)
                   | (numpy.abs(diff_arr) > spec.wrap * 0.9))
    print(f"pass counter check: {numpy.all(check_array)} "
          f"({spec.sensor}/{spec.name}, expected step {step})")
    print("and number of non matching samples: " + str(numpy.count_nonzero(check_array == 0)))


def _finish_chunk(data_set, *, is_ac, spec):
    """Add the human-readable Datetime column and (for AC) convert counts to
    g — replacing a Python `datetime.fromtimestamp()` + `.strftime()` call per
    row, the dominant cost of the old single-shot extraction (millions of
    interpreted calls for a long high-rate recording), not the CSV write it
    was blamed for.

    Deliberately NOT `pd.to_datetime(...).dt.strftime(...)`: benchmarked at
    ~2.9s for 2M rows vs. ~2.9s for the original per-row loop — pandas'
    `.dt.strftime` isn't vectorized for a custom format string, it loops
    internally too (plus tz-localization overhead with `utc=True`). What
    actually wins is doing the string formatting in numpy: cast straight to
    `datetime64[s]` and format the whole array at once
    (`numpy.datetime_as_string`), then two vectorized character replaces to
    turn ISO-8601 into this project's on-disk format — ~0.6s for the same 2M
    rows, ~5x the original loop, verified to produce byte-identical strings.
    """
    try:
        secs = data_set['CDCT'].to_numpy()
        if not np.isfinite(secs).all():
            raise ValueError("non-finite CDCT value(s) — can't convert to a timestamp")
        iso = np.datetime_as_string(secs.astype(np.int64).astype('datetime64[s]'), unit='s')
        data_set['Datetime'] = np.char.replace(np.char.replace(iso, '-', '/'), 'T', ' ')
    except Exception as e:
        print(str(e))
        data_set['Datetime'] = -1
    if is_ac:
        print("perform unit conversion for IMU")
        data_set = unit_conversion_ac(data_set, spec)
    return data_set


def unit_conversion_ac(data_set, spec=None):
    """Raw counts -> g. The v3 (ACF3) layout documents its own scale; legacy/v2
    (the wristband) keep the original conversion so their output is unchanged.
    """
    if spec is not None and spec.key == "ac:v3":
        for c in ['AccX', 'AccY', 'AccZ']:
            data_set[c] = data_set[c] / formats.AC_V3_COUNTS_PER_G
    else:
        for c in ['AccX', 'AccY', 'AccZ']:
            data_set[c] = data_set[c] / (2**16 - 1) * 8
    return data_set


def get_t0(file_list):
    pattern = r'\d*[A-Za-z]+(\d+)\.bin$'
    t = sorted([int(match.group(1)) for filename in file_list if (match := re.search(pattern, filename))])
    return t[0]


def get_cdct(df, bin_list, fs=320, counter_bits=16):
    t0 = get_t0(bin_list)
    counter_diff = np.diff(df['Counter']) % (2 ** counter_bits)
    counter_diff = np.insert(counter_diff, 0, 0)
    df['CDCT'] = t0 + np.cumsum(counter_diff) / fs
    return df


# ---------------------------------------------------------------------------
# top-level entry points (pure — no Gradio)
# ---------------------------------------------------------------------------

def extract_dir(in_dir, out_dir, *, df=None, note="", options=None,
                session_table_path=None) -> ExtractionReport:
    """Extract every `.bin` in `in_dir` to CSV/PKL under `out_dir`."""
    if df is None:
        df = get_session_encoding(session_table_path)
    extractor = DataExtractor(in_dir, out_dir, df=df, note=note, options=options)
    extractor.run()
    n_files = sum(1 for f in os.listdir(in_dir) if f.endswith(".bin"))
    print("operation completed.")
    return ExtractionReport(
        resolutions=extractor.resolutions,
        malformed=extractor.malformed,
        dropped=extractor.dropped,
        out_paths=extractor.out_paths,
        readme_path=getattr(extractor, "readme_path", None),
        n_files=n_files,
    )


def _extract_all_devices(root_dir, df, options):
    """Per-device rename-to-uuid-Name + extract_dir loop, in place under
    `root_dir`. Shared by `extract_folder` and `extract_zip` (the latter just
    unzips into a tempdir first) so there's one definition of "how a batch of
    device folders gets extracted."""
    for dev in os.listdir(root_dir):
        in_dir = os.path.join(root_dir, dev)
        if not os.path.isdir(in_dir):
            continue
        # Prefer the device's own Name from uuid.txt over whatever the
        # folder happens to be called (a BLE address on older downloads).
        name = get_device_name(in_dir)
        if name and name != dev:
            renamed = os.path.join(root_dir, name)
            if not os.path.exists(renamed):
                os.rename(in_dir, renamed)
                in_dir, dev = renamed, name
        extract_dir(in_dir, in_dir, df=df, note=dev, options=options)


def _zip_dir(root_dir, out_zip_path):
    with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(root_dir):
            for file in files:
                fp = os.path.join(root, file)
                zipf.write(fp, os.path.relpath(fp, start=root_dir))


def extract_folder(in_dir, out_dir="./data", *, out_name=None, options=None,
                   session_table_path=None) -> str | None:
    """Extract an *already-unzipped* folder of device subfolders (e.g. one
    just copied off a USB drive) in place, then zip the result once.

    Use this instead of zipping the raw data and calling `extract_zip` on
    that zip whenever the raw `.bin` files are already sitting on local disk
    — zipping them only to immediately unzip them again for extraction is a
    full compress+decompress round trip over data that never needed to leave
    disk, for no benefit (raw sensor binaries don't compress well anyway).
    See `plasma/devices/msense/panels/downloader.py`'s auto-extract path.
    """
    if in_dir is None:
        return None
    options = options or ExtractionOptions()
    df = get_session_encoding(session_table_path)
    os.makedirs(out_dir, exist_ok=True)
    out_name = out_name or f"{os.path.basename(os.path.normpath(in_dir))}_extracted.zip"
    out_zip_path = os.path.join(out_dir, out_name)
    _extract_all_devices(in_dir, df, options)
    _zip_dir(in_dir, out_zip_path)
    return out_zip_path


def extract_zip(zip_path, out_dir="./data", options=None,
                session_table_path=None) -> str | None:
    """Extract a downloaded `<...>_msense.zip` (one folder per device) and write
    a `<name>_extracted.zip` into `out_dir`. Returns that zip's path, or None.

    Thin wrapper: unzip into a scratch dir, then `extract_folder` it — that
    function owns the actual per-device extraction + single output zip."""
    if zip_path is None:
        return None
    out_name = os.path.basename(zip_path).replace('.zip', '_extracted.zip')
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)
        return extract_folder(tmpdir, out_dir, out_name=out_name, options=options,
                              session_table_path=session_table_path)


def batch_extract_zips(in_path, out_dir=None, options=None) -> list:
    """Extract every `*.zip` in `in_path`. Returns the list of output-zip paths."""
    out_dir = out_dir or os.path.join(in_path, "out")
    out = []
    for z in tqdm(glob(os.path.join(in_path, "*.zip"))):
        p = extract_zip(z, out_dir=out_dir, options=options)
        if p:
            out.append(p)
    return out
