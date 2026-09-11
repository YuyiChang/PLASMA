"""plasma.build_info — version/git-commit/OS identity logged once at launch.
Pure functions, no gradio / hardware."""
from plasma import build_info


def test_git_commit_hash_prefers_baked_value(monkeypatch):
    monkeypatch.setattr(build_info, "__git_commit__", "deadbee")
    assert build_info.git_commit_hash() == "deadbee"


def test_git_commit_hash_falls_back_to_live_git():
    """No baked commit (a source checkout) — resolves a real short SHA from
    this repo's own git history rather than "unknown"."""
    assert build_info.__git_commit__ is None       # sanity: not baked in dev
    sha = build_info.git_commit_hash()
    assert sha != "unknown"
    assert len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha)


def test_git_commit_hash_unknown_when_git_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise FileNotFoundError("no git")
    monkeypatch.setattr(build_info.subprocess, "run", _boom)
    assert build_info.git_commit_hash() == "unknown"


def test_os_info_names_the_python_runtime():
    info = build_info.os_info()
    assert "Python" in info


def test_launch_banner_includes_version_commit_and_os(monkeypatch):
    monkeypatch.setattr(build_info, "__git_commit__", "deadbee")
    banner = build_info.launch_banner()
    assert banner.startswith(f"PLASMA v{build_info.__version__} (deadbee) on ")
    assert "Python" in banner
