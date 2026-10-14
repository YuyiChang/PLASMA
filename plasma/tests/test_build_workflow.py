"""Release gating and signing-stage failure handling without Apple credentials."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SIGN_SCRIPT = ROOT / ".github/codesign_notarize_macos.sh"
WORKFLOW = ROOT / ".github/workflows/build.yml"


@pytest.fixture
def signing_runner(tmp_path):
    (tmp_path / "dist/PLASMA.app/Contents/Resources").mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    stub = f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([command, *args]) + "\\n")
if command == "security" and args[0] == "find-identity":
    print('1) ABCDEF "Developer ID Application: Test (TESTTEAM)"')
elif command == "security" and args[0] == "list-keychains" and "-s" not in args:
    print('"/tmp/test-login.keychain"')
elif command == "ditto":
    pathlib.Path(args[-1]).touch()
elif command == "codesign" and os.environ.get("MOCK_SIGN_ERROR"):
    print("Mock signature failure", file=sys.stderr)
    sys.exit(1)
elif command == "xcrun" and args[:2] == ["notarytool", "submit"]:
    if os.environ.get("MOCK_NOTARY_ERROR"):
        print("Error: HTTP status code: 403. A required agreement is missing or has expired.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"id": "test-submission", "status": os.environ.get("MOCK_NOTARY_STATUS", "Accepted")}))
'''
    for name in ["security", "codesign", "ditto", "xcrun"]:
        path = commands / name
        path.write_text(stub)
        path.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MACOS_")}
    env.update(PATH=f"{commands}{os.pathsep}{env['PATH']}", COMMAND_LOG=str(log),
               MACOS_CERTIFICATE_P12_BASE64="dGVzdA==", MACOS_CERTIFICATE_PASSWORD="test",
               MACOS_NOTARY_KEY_ID="test", MACOS_NOTARY_ISSUER_ID="test",
               MACOS_NOTARY_KEY_P8_BASE64="dGVzdA==")

    def run(stage="all", **overrides):
        result = subprocess.run(["bash", str(SIGN_SCRIPT), stage], cwd=tmp_path,
                                env={**env, **overrides}, capture_output=True, text=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run


@pytest.mark.parametrize("stage", ["all", "sign", "notarize", "staple"])
def test_unconfigured_signing_remains_unsigned(signing_runner, stage):
    result, calls = signing_runner(stage, MACOS_CERTIFICATE_P12_BASE64="")
    assert result.returncode == 0 and "shipping unsigned" in result.stdout
    assert calls == []


def test_missing_secret_fails_before_signing(signing_runner):
    result, calls = signing_runner("sign", MACOS_CERTIFICATE_PASSWORD="")
    assert result.returncode != 0
    assert "Missing required secret: MACOS_CERTIFICATE_PASSWORD" in result.stdout
    assert calls == []


def test_stages_run_independently(signing_runner):
    result, calls = signing_runner("notarize", MACOS_CERTIFICATE_PASSWORD="")
    assert result.returncode == 0
    assert not any(call[0] in {"security", "codesign"} for call in calls)
    assert not any(call[:2] == ["xcrun", "stapler"] for call in calls)


def test_all_stages_sign_then_notarize_then_validate(signing_runner):
    result, calls = signing_runner()
    assert result.returncode == 0, result.stderr
    verify = next(i for i, c in enumerate(calls) if c[:2] == ["codesign", "--verify"])
    submit = next(i for i, c in enumerate(calls) if c[:3] == ["xcrun", "notarytool", "submit"])
    staple = next(i for i, c in enumerate(calls) if c[:3] == ["xcrun", "stapler", "staple"])
    validate = next(i for i, c in enumerate(calls) if c[:3] == ["xcrun", "stapler", "validate"])
    assert verify < submit < staple < validate


def test_agreement_failure_has_actionable_error_and_never_staples(signing_runner):
    result, calls = signing_runner(MOCK_NOTARY_ERROR="1")
    assert result.returncode != 0
    assert "Account Holder must accept pending agreements" in result.stdout
    assert not any(call[:2] == ["xcrun", "stapler"] for call in calls)


def test_invalid_submission_never_staples_even_with_zero_tool_exit(signing_runner):
    result, calls = signing_runner(MOCK_NOTARY_STATUS="Invalid")
    assert result.returncode != 0
    assert "Notarization was not accepted: Invalid" in result.stdout
    assert not any(call[:2] == ["xcrun", "stapler"] for call in calls)


def test_signing_failure_never_submits(signing_runner):
    result, calls = signing_runner(MOCK_SIGN_ERROR="1")
    assert result.returncode != 0
    assert not any(call[:2] == ["xcrun", "notarytool"] for call in calls)


def test_only_release_job_can_publish_after_all_builds():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    release = jobs["release"]
    platforms = {"build-macos", "build-linux", "build-linux-arm64", "build-windows"}
    assert set(release["needs"]) == platforms | {"test"}
    assert "always()" not in release["if"]
    assert "inputs.nightly" in release["if"] and "refs/tags/" in release["if"]
    assert "workflow_dispatch" in release["if"]
    assert workflow["permissions"]["contents"] == "read"
    assert release["permissions"]["contents"] == "write"
    publishers = [name for name, job in jobs.items() for step in job["steps"]
                  if step.get("uses", "").startswith("softprops/action-gh-release")]
    assert publishers == ["release"]
    for name in platforms:
        checkout = next(s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/checkout"))
        assert checkout["with"]["ref"] == "${{ needs.test.outputs.source-sha }}"


def test_schedule_dispatches_actual_build_on_dev_only():
    # BaseLoader preserves the YAML 1.2 key "on" (SafeLoader treats it as the
    # YAML 1.1 boolean True). GitHub's workflow parser uses "on" as a key.
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    dispatcher = yaml.load((ROOT / ".github/workflows/nightly.yml").read_text(), Loader=yaml.BaseLoader)
    assert "schedule" not in workflow["on"]
    assert dispatcher["on"]["schedule"] == [{"cron": "0 7 * * *"}]
    assert dispatcher["permissions"]["actions"] == "write"
    script = dispatcher["jobs"]["dispatch"]["steps"][0]["with"]["script"]
    assert "createWorkflowDispatch" in script
    assert "workflow_id: 'build.yml'" in script and "ref: 'dev'" in script
    assert "inputs: { nightly: 'true' }" in script
    assert workflow["on"]["workflow_dispatch"]["inputs"]["nightly"]["default"] == "false"
    assert "Nightly dev build" in workflow["run-name"]


@pytest.mark.parametrize("branch,success", [("refs/heads/dev", True), ("refs/heads/main", False)])
def test_nightly_branch_guard_rejects_main(branch, success):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    guard = workflow["jobs"]["test"]["steps"][0]
    assert "inputs.nightly" in guard["if"]
    result = subprocess.run(["bash", "-e", "-c", guard["run"]],
                            env={**os.environ, "GITHUB_REF": branch}, capture_output=True, text=True)
    assert (result.returncode == 0) == success


def test_release_asset_validation_rejects_missing_or_corrupted_files(tmp_path):
    release = yaml.safe_load(WORKFLOW.read_text())["jobs"]["release"]
    publish = next(s for s in release["steps"] if s.get("uses", "").startswith("softprops/action-gh-release"))
    files = publish["with"]["files"].splitlines()
    (tmp_path / "dist").mkdir()
    for filename in files:
        path = tmp_path / filename
        if filename.endswith(".sha256"):
            continue
        path.write_bytes(b"test artifact")
    for filename in files:
        if filename.endswith(".sha256"):
            original = filename.removesuffix(".sha256")
            digest = hashlib.sha256((tmp_path / original).read_bytes()).hexdigest()
            (tmp_path / filename).write_text(f"{digest}  {original}\n")
    validate = next(s["run"] for s in release["steps"] if s.get("name") == "Verify complete release asset set")

    def check():
        return subprocess.run(["bash", "-e", "-c", validate], cwd=tmp_path,
                              capture_output=True, text=True)

    assert check().returncode == 0
    macos = tmp_path / "dist/PLASMA_MacOS_arm64.zip"
    macos.write_bytes(b"corrupted")
    assert check().returncode != 0
    macos.unlink()
    result = check()
    assert result.returncode != 0 and "Missing release asset" in result.stdout
