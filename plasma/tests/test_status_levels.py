"""The failure-level model — the executable copy of docs/failure-levels.md.

`classify(sts, phase, **health)` -> (internal Level, presentation category).
Levels 1-3 are internal; the operator sees only the category colour.
"""
import pytest

from plasma import status
from plasma.status import (
    Level, classify, health_level, render_class, sqc_level,
    SETUP, COLLECTING, STOPPED, STALE_LOST_AGE, IRREGULAR_STALE_S,
)


# ── status string → (Level, category), per phase ─────────────────────────

CASES = [
    # (sts, phase, Level, category)

    # healthy / idle / cleared — NONE
    ("🟢", COLLECTING, Level.NONE, "healthy"),
    ("Collection in progress", COLLECTING, Level.NONE, "healthy"),
    ("🔄 reconnected", COLLECTING, Level.NONE, "healthy"),
    ("✅ Bias saved", COLLECTING, Level.NONE, "info"),
    ("🟦", SETUP, Level.NONE, "info"),
    ("Ready to start", SETUP, Level.NONE, "info"),
    ("Welcome", SETUP, Level.NONE, "info"),
    ("initialized", SETUP, Level.NONE, "info"),
    ("some text nobody wrote", SETUP, Level.NONE, "info"),
    ("", SETUP, Level.NONE, "info"),
    (None, SETUP, Level.NONE, "info"),

    # construction / connection failures — L3
    ("⛔ connect failed", SETUP, Level.L3, "warning"),
    ("⛔ device not found", SETUP, Level.L3, "warning"),
    ("❌ Fault", SETUP, Level.L3, "warning"),
    ("❌ Fault RuntimeError('x')", SETUP, Level.L3, "warning"),
    ("FAULT: start_record failed", COLLECTING, Level.L3, "warning"),
    ("🚫 FAULT", SETUP, Level.L3, "warning"),
    ("⛔ some vendor sdk error", SETUP, Level.L3, "warning"),

    # collection start / stop
    ("⚠️ start failed", COLLECTING, Level.L3, "warning"),
    ("🛑 stopped", STOPPED, Level.NONE, "info"),        # clean, expected
    ("🛑 stopped", COLLECTING, Level.L3, "warning"),    # sensor stopped mid-collection
    ("🟥", STOPPED, Level.NONE, "info"),
    ("🟥", COLLECTING, Level.L3, "warning"),
    ("Collection stopped", STOPPED, Level.NONE, "info"),
    ("🛑", STOPPED, Level.L1, "advisory"),              # acq-stop unverifiable
    ("⚠️ still recording — stop unconfirmed", STOPPED, Level.L2, "caution"),
    ("⚠️ stop failed", STOPPED, Level.L2, "caution"),

    # link / stream recovery — L2 (auto-clears). A dropped link stays L2 even
    # after a failed reconnect attempt (the watchdog retries forever); the row
    # only goes red via the recorded-stream stale fold — see test_memo_html.
    ("🔌 disconnected", COLLECTING, Level.L2, "caution"),
    ("⚠️ stream stalled", COLLECTING, Level.L2, "caution"),

    # advisories / operational states — L1
    ("🎯 Calibrating...", COLLECTING, Level.L1, "info"),
    ("🧨 erased — re-Initialize", STOPPED, Level.L1, "advisory"),
]


@pytest.mark.parametrize("sts,phase,level,category", CASES,
                         ids=[f"{c[1]}:{(c[0] or 'None')[:24]}" for c in CASES])
def test_classify(sts, phase, level, category):
    lvl, cat = classify(sts, phase)
    assert lvl == level
    assert cat == category
    assert render_class(sts, phase) == category


def test_levels_are_internal_only():
    """Nothing renders the word 'level' or L1/L2/L3 — only the category colour."""
    for sts, phase, _lvl, cat in CASES:
        assert classify(sts, phase)[1] in status.CATEGORY_HEX


# ── recorder stream-health fold ─────────────────────────────────────────

def test_health_periodic_stream_escalates_stale_to_lost():
    assert health_level("🟡 stale", srate=2.0, stale_age=5, phase=COLLECTING) == Level.L2
    assert health_level("🟡 stale", srate=2.0, stale_age=STALE_LOST_AGE + 1,
                        phase=COLLECTING) == Level.L3
    assert health_level("🔴 lost", srate=2.0, phase=COLLECTING) == Level.L3


def test_health_irregular_stream_is_quiet_by_nature():
    # journaler / eye-event stream: no data for a while is normal
    assert health_level("🟡 stale", srate=0.0, stale_age=10, phase=COLLECTING) == Level.NONE
    assert health_level("🟡 stale", srate=0.0, stale_age=IRREGULAR_STALE_S + 1,
                        phase=COLLECTING) == Level.L1


def test_health_gone_is_expected_only_after_stop():
    assert health_level("🔴 lost", srate=2.0, phase=STOPPED) == Level.L1
    assert health_level("🔴 lost", srate=2.0, phase=SETUP) == Level.L3


# ── SQC snapshot / live-stream transfer state fold ─────────────────────

@pytest.mark.parametrize("sqc_status,error,level,category", [
    ("idle", None, Level.NONE, "info"),
    ("requesting", None, Level.NONE, "info"),
    ("receiving", None, Level.NONE, "info"),
    ("ready", None, Level.NONE, "info"),
    ("streaming", None, Level.NONE, "info"),
    ("unavailable", None, Level.NONE, "info"),
    ("rejected", "NOT_RECORDING", Level.L1, "advisory"),
    ("rejected", "BUSY", Level.L2, "caution"),
    ("rejected", "MTU_TOO_SMALL", Level.L2, "caution"),
    ("rejected", "WRONG_SESSION", Level.L2, "caution"),
    ("error", "no START_ACK within 8s", Level.L2, "caution"),
    ("error", "decode failed (raw saved): boom", Level.L2, "caution"),
    ("error", "protocol violation: x", Level.L2, "caution"),
    ("error", "stalled", Level.L2, "caution"),
])
def test_sqc_level(sqc_status, error, level, category):
    assert sqc_level(sqc_status, error) == (level, category)


def test_device_row_takes_the_worse_of_sts_and_stream_health():
    # driver still says 🟢 but its recorded stream vanished mid-collection
    lvl, cat = classify("🟢", COLLECTING, stream_health="🔴 lost", srate=2.0)
    assert lvl == Level.L3 and cat == "warning"
    # ... and once the operator has stopped, that's expected, not a warning
    lvl, cat = classify("🛑 stopped", STOPPED, stream_health="🔴 lost", srate=2.0)
    assert lvl == Level.L1
