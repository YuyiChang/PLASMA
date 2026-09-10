"""Packaging metadata sanity — version is single-sourced and PEP 440."""
import importlib.util
import pathlib
import re
import sys

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


def test_pyinstaller_hook_entrypoint_uses_the_pyinstaller40_group():
    # PyInstaller discovers hook dirs via the `pyinstaller40` entry-point group
    # (build_main.discover_hook_directories). A plain `pyinstaller` group is
    # silently ignored, so hook-plasma.py never runs and the dynamically-imported
    # device plugins (plasma.devices.*) drop out of the frozen build.
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    eps = pyproject["project"]["entry-points"]
    assert "pyinstaller40" in eps, eps
    assert eps["pyinstaller40"]["hook-dirs"] == "plasma.__pyinstaller:get_hook_dirs"


# ── the frozen build must bundle the dynamically-imported device plugins ──────

def _plugin_module_paths():
    from plasma import plugins
    return list(plugins._DISCOVERY) + [p.module for p in plugins._STATIC]


def test_devices_init_is_non_empty():
    # A 0-byte plasma/devices/__init__.py has been observed to leave
    # `plasma.devices` out of the PyInstaller bundle (No module named
    # 'plasma.devices' at launch). Keep it non-empty.
    assert (_ROOT / "plasma" / "devices" / "__init__.py").read_text().strip()


def test_every_plugin_module_resolves_to_a_real_file():
    for name in _plugin_module_paths():
        spec = importlib.util.find_spec(name)
        assert spec and spec.origin and pathlib.Path(spec.origin).is_file(), name


def test_spec_common_walk_covers_every_dynamic_plugin_module():
    sys.path.insert(0, str(_ROOT))
    import spec_common

    hi = set(spec_common.plasma_hiddenimports())
    for name in _plugin_module_paths():
        assert name in hi, f"{name} missing from spec_common.plasma_hiddenimports()"
    # the intermediate package that regressed, + a leaf under panels/
    assert "plasma.devices" in hi
    assert "plasma.devices.msense" in hi
    assert any(m.startswith("plasma.devices.msense.panels.") for m in hi)


# ── the Linux spec builds natively for x86-64 and aarch64 from one file ───────

def test_linux_arch_tag():
    sys.path.insert(0, str(_ROOT))
    import spec_common

    assert spec_common.linux_arch_tag("x86_64") == "x64"
    assert spec_common.linux_arch_tag("amd64") == "x64"
    assert spec_common.linux_arch_tag("aarch64") == "arm64"
    assert spec_common.linux_arch_tag("arm64") == "arm64"
    # app_linux.spec derives the EXE name from this
    assert f"PLASMA_Linux_{spec_common.linux_arch_tag('aarch64')}" == "PLASMA_Linux_arm64"


def test_build_yml_has_the_arm64_job():
    ci = (_ROOT / ".github" / "workflows" / "build.yml").read_text()
    assert "build-linux-arm64:" in ci
    assert re.search(r"runs-on:\s*ubuntu-\S*-arm\b", ci), "arm64 job needs an -arm runner"
    assert "PLASMA-Linux-arm64" in ci and "dist/PLASMA_Linux_arm64" in ci
    # liblsl comes from the sccn release, not conda (no conda-forge aarch64 build)
    assert "sccn/liblsl/releases" in ci


def test_arm64_job_omits_qb2():
    # blickfeld-qb2 is sdist-only on aarch64; installing [all] would break the
    # build. The qb2 plugin still ships (lazy _STATIC import) — see plasma.plugins.
    ci = (_ROOT / ".github" / "workflows" / "build.yml").read_text()
    arm = ci.split("build-linux-arm64:", 1)[1].split("\n  build-windows:", 1)[0]
    assert 'pip install -e ".[msense,pupil,shimmer,obs,build]"' in arm
    assert '.[all' not in arm
