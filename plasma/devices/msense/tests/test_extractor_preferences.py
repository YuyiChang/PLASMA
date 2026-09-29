"""The UI save-format choice is a durable app preference."""
import json
import os
import subprocess
import sys


def test_save_format_survives_next_launch(tmp_path):
    env = {**os.environ, "PLASMA_HOME": str(tmp_path)}
    save = (
        "from plasma.devices.msense.panels.extractor import _save_save_format; "
        "_save_save_format('csv')"
    )
    load = (
        "from plasma.devices.msense.panels.extractor import _saved_save_format; "
        "assert _saved_save_format() == 'csv'"
    )
    subprocess.run([sys.executable, "-c", save], env=env, check=True)
    subprocess.run([sys.executable, "-c", load], env=env, check=True)

    config = json.loads((tmp_path / "plasma_device_config.json").read_text())
    assert config["plugins"]["msense_extraction"]["save_format"] == "csv"
