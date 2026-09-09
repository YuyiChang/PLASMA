"""``plasma-ctl`` — drive a running PLASMA and read its state from the shell.

PLASMA is a single browser-only process. This talks to its curated
``gr.api()`` endpoints (registered by :func:`plasma.api.register`) over
``gradio_client``, so a script and a browser operate the *same* session.

    plasma-ctl status [--json]
    plasma-ctl start --sub sub-1001 --ses ses-02 --devices "MSense Wristbands" [--no-record]
    plasma-ctl stop
    plasma-ctl mark "subject seated"
    plasma-ctl watch [--interval 1] [--fail-on L3]
    plasma-ctl events [--since TS] [--min-level L2] [--follow] [--json]

Target URL: ``$PLASMA_URL`` or ``http://127.0.0.1:7860``.

Exit codes: 0 ok · 1 a control call reported ``ok: false`` · 2 could not reach
PLASMA · 3 ``watch``/``events`` threshold crossed (``--fail-on``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_URL = "http://127.0.0.1:7860"
_LEVELS = {"NONE": 0, "L1": 1, "L2": 2, "L3": 3}


def _level(v) -> int:
    if v is None:
        return 0
    s = str(v).upper()
    if s in _LEVELS:
        return _LEVELS[s]
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _client(url):
    try:
        from gradio_client import Client
    except Exception as e:  # pragma: no cover
        print(f"gradio_client not installed: {e}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return Client(url, verbose=False)
    except Exception as e:
        print(f"cannot reach PLASMA at {url}: {e}", file=sys.stderr)
        raise SystemExit(2)


def _call(client, api_name, *args):
    try:
        return client.predict(*args, api_name=api_name)
    except Exception as e:
        print(f"call {api_name} failed: {e}", file=sys.stderr)
        raise SystemExit(2)


# ── rendering ──────────────────────────────────────────────────────────────

_DOT = {0: "·", 1: "▲", 2: "▲", 3: "■"}


def _print_status(st):
    s = st["session"]
    rec = st["recorder"]
    hdr = f"{st['app']['name']} {st['app']['version']}  phase={st['phase']}"
    if s.get("sub_id"):
        hdr += f"  {s['sub_id']}/{s['ses_id']}"
    if s.get("elapsed_s") is not None:
        hdr += f"  +{s['elapsed_s']}s"
    print(hdr)
    print(f"  session : {s.get('sts','')}   dir={s.get('session_dir') or '-'}")
    print(f"  recorder: {rec['state']} — {rec.get('summary','')}")
    print(f"  worst   : {st['worst_level_name']} ({st['worst_category']})")
    for d in st["devices"]:
        print(f"  [{d['worst_level_name']:4}] {d['tag']}")
        for src in d["sources"]:
            mark = _DOT.get(src["level"], "?")
            line = f"      {mark} {src['name']}: {src['level_name']}/{src['category']}"
            if src.get("sts"):
                line += f"  «{src['sts']}»"
            if src.get("stream"):
                stm = src["stream"]
                line += f"  [{stm['health']} {stm['n_samples']} samp]"
            print(line)
            if src.get("level_reason"):
                print(f"          → {src['level_reason']}")
            acq = src.get("acq_stop")
            if acq and acq.get("status") not in (None, "unknown"):
                print(f"          acq-stop: {acq['status']}")
            for probe in ("sqc", "live_stream"):
                p = src.get(probe)
                if isinstance(p, dict) and p.get("status") not in (
                        None, "idle", "unavailable"):
                    txt = p["status"]
                    if p.get("error"):
                        txt += f": {p['error']}"
                    if p.get("bytes_received"):
                        txt += f"  ({p['bytes_received']}"
                        txt += f"/{p['bytes_total']}" if p.get("bytes_total") else ""
                        txt += " B)"
                    print(f"          {probe}: {txt}")
    for o in st["other_recorded_streams"]:
        print(f"      ~ {o['name']}: {o['level_name']} [{o['health']} {o['n_samples']} samp]")
    for e in st.get("external_streams", []):
        print(f"      🛰 {e.get('name')} ({e.get('type') or 'stream'})")


def _print_events(evs):
    for e in evs:
        dev = e.get("device") or ""
        src = e.get("source") or ""
        who = f"{dev}/{src}".strip("/")
        detail = e.get("detail") or e.get("sts") or ""
        print(f"{e.get('iso','')}  {e.get('event',''):14} {e.get('level_name',''):4} "
              f"{who}  {detail}".rstrip())


# ── subcommands ────────────────────────────────────────────────────────────

def cmd_status(client, args) -> int:
    st = _call(client, "/status")
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        _print_status(st)
    return 0


def cmd_start(client, args) -> int:
    devices = [d for d in (args.devices or []) if d]
    res = _call(client, "/start", args.sub, args.ses, devices, not args.no_record)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("ok" if res.get("ok") else "FAILED")
        for err in res.get("errors", []):
            print(f"  ! {err}")
        _print_status(res["status"])
    return 0 if res.get("ok") else 1


def cmd_stop(client, args) -> int:
    res = _call(client, "/stop")
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("ok" if res.get("ok") else "stopped with warnings")
        for err in res.get("errors", []):
            print(f"  ! {err}")
    return 0 if res.get("ok") else 1


def cmd_mark(client, args) -> int:
    res = _call(client, "/mark", args.text)
    print(json.dumps(res) if args.json else
          ("recorded: " + res.get("marker", "") if res.get("ok")
           else "FAILED: " + "; ".join(res.get("errors", []))))
    return 0 if res.get("ok") else 1


def cmd_watch(client, args) -> int:
    threshold = _level(args.fail_on) if args.fail_on else None
    last = None
    while True:
        st = _call(client, "/status")
        key = (st["phase"], st["worst_level"],
               tuple((d["tag"], d["worst_level"]) for d in st["devices"]),
               st["recorder"]["state"])
        if key != last:
            last = key
            ts = time.strftime("%H:%M:%S")
            print(f"{ts}  phase={st['phase']}  worst={st['worst_level_name']}"
                  f"  rec={st['recorder']['state']}")
            for d in st["devices"]:
                if d["worst_level"] > 0:
                    print(f"        [{d['worst_level_name']}] {d['tag']}")
        if threshold is not None and st["worst_level"] >= threshold:
            print(f"threshold {args.fail_on} reached "
                  f"(worst={st['worst_level_name']})", file=sys.stderr)
            return 3
        time.sleep(max(0.2, args.interval))


def cmd_events(client, args) -> int:
    min_level = _level(args.min_level)
    since = args.since or 0.0
    threshold = _level(args.fail_on) if args.fail_on else None
    seen_max = 0

    def _pull(s):
        evs = _call(client, "/events", float(s), int(min_level))
        return evs or []

    evs = _pull(since)
    if args.json:
        print(json.dumps(evs, indent=2))
    else:
        _print_events(evs)
    if evs:
        since = evs[-1]["ts"]
        seen_max = max(seen_max, max(e.get("level", 0) for e in evs))

    if args.follow:
        try:
            while True:
                time.sleep(max(0.5, args.interval))
                evs = _pull(since)
                if evs:
                    if args.json:
                        print(json.dumps(evs, indent=2))
                    else:
                        _print_events(evs)
                    since = evs[-1]["ts"]
                    seen_max = max(seen_max, max(e.get("level", 0) for e in evs))
                if threshold is not None and seen_max >= threshold:
                    print(f"threshold {args.fail_on} seen", file=sys.stderr)
                    return 3
        except KeyboardInterrupt:
            return 0

    if threshold is not None and seen_max >= threshold:
        return 3
    return 0


# ── argparse ───────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(prog="plasma-ctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("PLASMA_URL", DEFAULT_URL),
                   help=f"PLASMA base URL (default: $PLASMA_URL or {DEFAULT_URL})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="print the current session status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("start", help="initialise + start a collection")
    sp.add_argument("--sub", required=True)
    sp.add_argument("--ses", required=True)
    sp.add_argument("--devices", nargs="+", required=True,
                    help="catalog device name(s), exactly as shown in the UI")
    sp.add_argument("--no-record", action="store_true",
                    help="do not record LSL streams to XDF")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop the running collection")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("mark", help="push a journaler marker")
    sp.add_argument("text")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_mark)

    sp = sub.add_parser("watch", help="poll status, print changes, optionally fail")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--fail-on", metavar="L1|L2|L3",
                    help="exit 3 as soon as worst_level reaches this")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("events", help="read the durable fault history")
    sp.add_argument("--since", type=float, default=0.0,
                    help="only events with ts greater than this")
    sp.add_argument("--min-level", metavar="L1|L2|L3", default="0")
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--fail-on", metavar="L1|L2|L3",
                    help="exit 3 if an event at or above this level is seen")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_events)

    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    client = _client(args.url)
    try:
        return args.func(client, args) or 0
    except SystemExit as e:
        return int(e.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
