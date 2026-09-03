"""CLI: python -m plasma.devices.msense.extract {dir | batch | sync}

Replaces YAMS's `python -m yams.data_extraction` and `python -m yams.msense_yams_sync`.
A bare first token that isn't a known subcommand is treated as `dir` (so the old
`-m ... -i <dir> -o <out>` muscle memory keeps working).
"""
import argparse
import os
import sys

from .options import (
    AC_FORMAT_CHOICES,
    CONFLICT_CHOICES,
    ECG_FORMAT_CHOICES,
    PPG_FORMAT_CHOICES,
    ExtractionOptions,
)
from .pipeline import batch_extract_zips, extract_dir
from .clocksync import sync_paths

_SUBCOMMANDS = {"dir", "batch", "sync"}


def _add_extract_flags(p):
    p.add_argument('-i', '--in_dir', required=True, help="directory with the .bin files")
    p.add_argument('-o', '--out_dir', default="./", help="output directory")
    p.add_argument('--legacy_fs', action='store_true', default=False,
                   help="(no effect; kept for compatibility)")
    p.add_argument('--save_format', choices=['csv', 'pickle'], default='csv')
    p.add_argument('--ignore_id', action='store_true', default=False,
                   help="skip subject/session ID parsing for filenames")
    p.add_argument('--force_new_format', action='store_true', default=False,
                   help="assume v4.7.0+ when the layout has to be guessed (does not override content detection)")
    p.add_argument('--ppg_format', choices=PPG_FORMAT_CHOICES, default='auto')
    p.add_argument('--ac_format', choices=AC_FORMAT_CHOICES, default='auto')
    p.add_argument('--ecg_format', choices=ECG_FORMAT_CHOICES, default='auto')
    p.add_argument('--validate_with_uuid', action='store_true', default=False)
    p.add_argument('--on_format_conflict', choices=CONFLICT_CHOICES, default='warn')
    p.add_argument('--sniff_threshold', type=float, default=0.90)
    p.add_argument('--dry_run', action='store_true', default=False)
    p.add_argument('--strict_ppg', action='store_true', default=False)
    p.add_argument('--note', default="", help="note recorded in the output README")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # old muscle memory: `-m ... -i <dir> -o <out>` with no subcommand -> `dir`
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        argv.insert(0, "dir")

    parser = argparse.ArgumentParser(prog="python -m plasma.devices.msense.extract")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dir = sub.add_parser("dir", help="extract one folder of .bin files")
    _add_extract_flags(p_dir)

    p_batch = sub.add_parser("batch", help="extract a folder of *.zip archives")
    p_batch.add_argument('-i', '--in_dir', required=True)
    p_batch.add_argument('-o', '--out_dir', default=None)
    for name, kw in (("save_format", dict(choices=['csv', 'pickle'], default='csv')),
                     ("ppg_format", dict(choices=PPG_FORMAT_CHOICES, default='auto')),
                     ("ac_format", dict(choices=AC_FORMAT_CHOICES, default='auto')),
                     ("ecg_format", dict(choices=ECG_FORMAT_CHOICES, default='auto'))):
        p_batch.add_argument(f'--{name}', **kw)
    p_batch.add_argument('--ignore_id', action='store_true', default=False)
    p_batch.add_argument('--force_new_format', action='store_true', default=False)
    p_batch.add_argument('--validate_with_uuid', action='store_true', default=False)
    p_batch.add_argument('--on_format_conflict', choices=CONFLICT_CHOICES, default='warn')
    p_batch.add_argument('--sniff_threshold', type=float, default=0.90)
    p_batch.add_argument('--strict_ppg', action='store_true', default=False)
    p_batch.add_argument('--legacy_fs', action='store_true', default=False)
    p_batch.add_argument('--dry_run', action='store_true', default=False)
    p_batch.add_argument('--note', default="")

    p_sync = sub.add_parser("sync", help="counter-align a CSV to a YAMS .txt reference")
    p_sync.add_argument('--csv', required=True)
    p_sync.add_argument('--txt', required=True)
    p_sync.add_argument('--ppg', default=None, help="explicit sibling PPG CSV (ac.csv input only)")
    p_sync.add_argument('--out', default='./out')
    p_sync.add_argument('--no-plots', dest='make_plots', action='store_false', default=True)

    args = parser.parse_args(argv)

    if args.cmd == "sync":
        os.makedirs(args.out, exist_ok=True)
        written = sync_paths(args.csv, args.txt, args.out,
                             ppg_path=args.ppg, make_plots=args.make_plots)
        print("\n".join(written))
        return

    options = ExtractionOptions.from_args(args)

    if args.cmd == "batch":
        paths = batch_extract_zips(args.in_dir, out_dir=args.out_dir, options=options)
        print("\n".join(paths))
        return

    report = extract_dir(args.in_dir, args.out_dir, note=args.note, options=options)
    print(report.summary())


if __name__ == "__main__":
    main()
