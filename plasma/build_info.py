"""Runtime build identity — version, git commit, host OS — logged once at
launch (see IntegratedPanel.__init__) so a bug report or a support log
unambiguously identifies what was actually running.

Pure module — stdlib only, safe to import early (no gradio / pylsl / device
imports).
"""
import os
import platform
import subprocess

from plasma import __git_commit__, __version__

__all__ = ["git_commit_hash", "os_info", "launch_banner"]


def git_commit_hash():
    """Short git commit hash for this build, or "unknown".

    A frozen (PyInstaller) build ships no .git directory — CI stamps
    plasma.__git_commit__ with the short SHA before freezing a release build
    (see .github/workflows/build.yml), and that baked-in value is used as-is.
    A source checkout instead resolves it live from git.
    """
    if __git_commit__:
        return __git_commit__
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo_root,
            capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def os_info():
    """One-line OS/runtime descriptor, e.g.
    'macOS-14.5-arm64-arm-64bit, Python 3.12.4'."""
    return f"{platform.platform()}, Python {platform.python_version()}"


def launch_banner():
    """'PLASMA v2.0.0 (a1b2c3d) on macOS-14.5-arm64-arm-64bit, Python 3.12.4'
    — everything IntegratedPanel logs once at the top of every session."""
    return f"PLASMA v{__version__} ({git_commit_hash()}) on {os_info()}"
