"""Native regression check: a stale interface must not abort LSL discovery.

Run in a subprocess because the unfixed dylib aborts outside Python's handlers.
The healthy loopback interface must still discover an outlet after the bad one.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", default=os.environ.get("PYLSL_LIB"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="plasma-lsl-interfaces-") as work:
        cfg = Path(work) / "lsl_api.cfg"
        code = '''
import time
import os
from pathlib import Path
import pylsl
from pylsl.lib import lib
if os.environ.get("PYLSL_LIB"):
    assert Path(lib._name).resolve() == Path(os.environ["PYLSL_LIB"]).resolve(), lib._name
outlet = pylsl.StreamOutlet(pylsl.StreamInfo(
    "plasma-interface-regression", "Markers", 1, 0, "string", "interface-test"))
resolver = pylsl.ContinuousResolver()
deadline = time.monotonic() + 10
try:
    while time.monotonic() < deadline:
        if any(i.source_id() == "interface-test" for i in resolver.results()):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Discovery did not continue on the healthy interface")
    assert pylsl.resolve_byprop("source_id", "interface-test", timeout=3)
    # Allow the async send chain and another discovery wave to complete.
    time.sleep(1.2)
finally:
    del resolver
    del outlet
print("Native LSL unavailable-interface regression passed")
'''
        env = {**os.environ, "LSLAPICFG": str(cfg)}
        if args.library:
            env["PYLSL_LIB"] = str(Path(args.library).resolve())
        for interfaces in ("127.0.0.1, 192.0.2.123", "192.0.2.123, 127.0.0.1"):
            cfg.write_text("[ports]\nIPv6 = disable\n[multicast]\n"
                           f"Interfaces = {{{interfaces}}}\n"
                           "[lab]\nSessionID = plasma-interface-regression\n")
            subprocess.run([sys.executable, "-c", code], check=True, timeout=20,
                           cwd=work, env=env)


if __name__ == "__main__":
    main()
