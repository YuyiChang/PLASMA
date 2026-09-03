"""Packaging metadata sanity — version is single-sourced and PEP 440."""
import pathlib
import re

import pytest

import plasma

tomllib = pytest.importorskip("tomllib")  # stdlib on 3.11+

_ROOT = pathlib.Path(__file__).resolve().parents[2]
# PEP 440 (permissive subset — enough to catch "0.1.0-beta"-style mistakes)
_PEP440 = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


def test_version_is_pep440():
    assert _PEP440.match(plasma.__version__), plasma.__version__


def test_pyproject_points_dynamic_version_at_the_package():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "plasma-app"
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "plasma.__version__"
    }


def test_every_plugin_extra_exists():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    for name in ("msense", "qb2", "pupil", "shimmer", "obs", "all"):
        assert name in extras


def test_console_script_entrypoint():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["plasma"] == "plasma.__main__:main"
