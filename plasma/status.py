"""Instrumentation failure classification — the single source of truth for how a
device/stream status maps to a severity level and a display colour.

The full model (an Airbus-ECAM-derived 3-level scheme) lives in
``docs/failure-levels.md``. In short:

* **Levels 1-3 are internal** and never shown to the operator. They drive
  behaviour: whether an alert is sticky or auto-clearing, escalation timers,
  and (future) whether to also emit a journal marker.

    - **L3 / WARNING** — capture is stopped *during an active collection*,
      unrecoverable, or the recording's integrity is compromised.
    - **L2 / CAUTION** — interrupted/degraded, but PLASMA is recovering it or
      one operator action fixes it, with the rest of the session intact.
    - **L1 / ADVISORY** — capture continues; a redundancy is reduced or a value
      is drifting toward a limit.
    - **NONE** — healthy / idle / a cleared caution / a clean operator stop.

* The operator sees only a **presentation category** → colour:

    - ``healthy``  green   — capturing normally
    - ``info``     neutral — status / idle / an expected stop ("white")
    - ``advisory`` blue    — L1 that needs an operator action, or a drift
    - ``caution``  amber   — L2 (and monitor-only L1)
    - ``warning``  red     — L3
    - ``guidance`` purple  — a special operator instruction (sub-line accent)
    - ``external`` grey    — an LSL stream on the network that isn't ours

Alerts are **phase-gated** (the ECAM take-off/landing inhibition analog): a
device reporting "stopped" is a WARNING mid-collection but neutral once the
operator has pressed Stop.

Pure module — stdlib only, no gradio / pylsl.
"""
from enum import IntEnum

__all__ = [
    "Level", "SETUP", "COLLECTING", "STOPPED", "CATEGORY_HEX",
    "classify", "render_class", "health_level", "sqc_level",
    "STALE_S", "IRREGULAR_STALE_S", "STALE_LOST_AGE",
]


class Level(IntEnum):
    NONE = 0
    L1 = 1   # ADVISORY  — capture continues; degradation / drift / needs attention
    L2 = 2   # CAUTION    — interrupted; recovering, or a one-click operator fix
    L3 = 3   # WARNING    — capture stopped during collection / unrecoverable


# collection phases (plain strings; IntegratedPanel derives the current one)
SETUP = "SETUP"
COLLECTING = "COLLECTING"
STOPPED = "STOPPED"

# recorder staleness thresholds (seconds)
STALE_S = 3.0             # periodic stream: no data this long -> 🟡 stale
IRREGULAR_STALE_S = 300.0  # irregular (event) stream: only stale after this long
STALE_LOST_AGE = 30.0     # 🟡 stale this long on a periodic stream -> treat as lost (L3)

# presentation category -> hex colour (replaces integrated_panel._STATUS_HEX)
CATEGORY_HEX = {
    "healthy": "#15803d",   # green
    "info": "#4b5563",      # neutral ("white" in the aviation sense — reads on light & dark)
    "advisory": "#2563eb",  # blue   — L1 + operator action
    "caution": "#a16207",   # amber  — L2 (and monitor-only L1)
    "warning": "#b91c1c",   # red    — L3
    "guidance": "#7c3aed",  # purple — special instruction (sub-line accent)
    "external": "#6b7280",  # grey
}

_LEVEL_CATEGORY = {
    Level.NONE: "info",
    Level.L1: "caution",     # rules override to "advisory" when an action is implied
    Level.L2: "caution",
    Level.L3: "warning",
}


# Ordered rules — first match wins. Each: (name, predicate(orig, low), level,
# category_override_or_None, stop_like). `stop_like` rules are phase-gated: a
# stop during COLLECTING is a WARNING, otherwise it keeps the rule's own level.
def _has(orig, *glyphs):
    return any(g in orig for g in glyphs)


_RULES = [
    # ── healthy / cleared ──────────────────────────────────────────────
    ("collecting", lambda o, l: o.startswith("🟢") or "in progress" in l,
     Level.NONE, "healthy", False),
    ("reconnected", lambda o, l: "reconnected" in l,
     Level.NONE, "healthy", False),
    ("bias saved", lambda o, l: "bias saved" in l,
     Level.NONE, "info", False),

    # ── construction / connection failures (SETUP-blocking) → L3 ───────
    ("connect failed", lambda o, l: "connect failed" in l, Level.L3, None, False),
    ("device not found", lambda o, l: "device not found" in l, Level.L3, None, False),
    ("obs fault", lambda o, l: l.startswith("fault:"), Level.L3, None, False),
    ("fault", lambda o, l: _has(o, "⛔", "🚫") or o.startswith("❌") or "fault" in l,
     Level.L3, None, False),

    # ── abnormal stop → L2 (before the generic stop_like rule) ─────────
    ("stop unconfirmed", lambda o, l: "still recording" in l or "stop unconfirmed" in l,
     Level.L2, None, False),
    ("stop failed", lambda o, l: "stop failed" in l, Level.L2, None, False),

    # ── device not collecting while the session runs → L3 ─────────────
    ("start failed", lambda o, l: "start failed" in l, Level.L3, None, False),

    # ── link / stream recovery in progress → L2 ───────────────────────
    ("stream stalled", lambda o, l: "stall" in l, Level.L2, None, False),
    # a dropped link is always L2 here — the watchdog retries indefinitely and
    # never emits a distinct "failed" status. The row still escalates to L3
    # (same string, red) via the recorder-stream fold once the wristband's
    # recorded stream has been silent past STALE_LOST_AGE (see health_level).
    ("disconnected", lambda o, l: "disconnected" in l or o.startswith("🔌"),
     Level.L2, None, False),

    # ── advisories / operational states → L1 ──────────────────────────
    ("erased", lambda o, l: "erased" in l or "🧨" in o, Level.L1, "advisory", False),
    ("calibrating", lambda o, l: "calibrat" in l or "🎯" in o, Level.L1, "info", False),
    ("acq unverifiable", lambda o, l: o.strip() == "🛑", Level.L1, "advisory", True),

    # ── generic stop (phase-gated) ───────────────────────────────────
    ("stopped", lambda o, l: _has(o, "🛑", "🟥", "⏹️") or "stopped" in l,
     Level.NONE, "info", True),

    # ── idle / ready / unknown text → NONE ──────────────────────────
    ("idle", lambda o, l: l in ("", "🟦", "welcome", "ready", "ready to start",
                                "initialized"),
     Level.NONE, "info", False),
]


def health_level(recorder_health, srate=0.0, stale_age=0.0, phase=COLLECTING):
    """Severity of a recorder per-stream ``health`` glyph
    (``"🟢"`` / ``"🟡 stale"`` / ``"🔴 lost"``). ``phase == STOPPED`` is the only
    phase where a vanished stream is expected (the operator pressed Stop)."""
    if not recorder_health:
        return Level.NONE
    expected_gone = (phase == STOPPED)
    if recorder_health.startswith("🔴"):
        return Level.L1 if expected_gone else Level.L3
    if recorder_health.startswith("🟡"):
        if srate and srate > 0:                       # periodic
            if stale_age and stale_age > STALE_LOST_AGE:
                return Level.L1 if expected_gone else Level.L3
            return Level.NONE if expected_gone else Level.L2
        # irregular (event) stream — silent by nature
        return Level.L1 if (stale_age and stale_age > IRREGULAR_STALE_S) else Level.NONE
    return Level.NONE


def sqc_level(sqc_status, error=None):
    """``(Level, category)`` for an MSense **SQC snapshot** or **live-stream**
    transfer state — the ``status`` + ``error`` fields of
    ``MotionSenseHRV.get_sqc_status()`` / ``get_live_stream_status()``.

    These are the FINITE / INFINITY signal-streaming states (SQC tab), not the
    memo-panel device status; the mapping mirrors the table in
    ``docs/failure-levels.md``:

    * ``rejected`` with reason ``NOT_RECORDING`` — the wristband simply isn't
      recording yet → **L1 / advisory** (one operator action).
    * any other ``rejected`` reason (``BUSY`` / ``NOT_SUBSCRIBED`` /
      ``MTU_TOO_SMALL`` / ``INVALID_COMMAND`` / ``WRONG_SESSION``) → **L2**.
    * ``error`` (no START_ACK, decode failed, protocol violation, live
      ``stalled``, request failed) → **L2**.
    * everything else (idle / requesting / receiving / streaming / ready /
      stopped / unavailable) → **NONE** — a transfer in flight or not started
      is not a fault.
    """
    s = (sqc_status or "").strip().lower()
    e = (error or "").strip().lower()
    if s == "rejected":
        if "not_recording" in e or "not recording" in e:
            return Level.L1, "advisory"
        return Level.L2, "caution"
    if s == "error":
        return Level.L2, "caution"
    return Level.NONE, "info"


def classify(sts, phase=COLLECTING, *, stream_health=None, srate=0.0, stale_age=0.0):
    """``(Level, presentation-category)`` for a status string.

    ``phase`` is one of SETUP / COLLECTING / STOPPED. ``stream_health`` /
    ``srate`` / ``stale_age`` fold the recorder's view of the underlying LSL
    stream into the result (a device row escalates when its own recorded
    stream goes silent, even if the driver never updated ``sts``).
    """
    orig = (sts or "").strip()
    low = orig.lower()

    sts_level, sts_cat = Level.NONE, "info"
    for _name, pred, lvl, cat_override, stop_like in _RULES:
        try:
            if not pred(orig, low):
                continue
        except Exception:
            continue
        if stop_like and phase == COLLECTING:
            sts_level, sts_cat = Level.L3, "warning"     # sensor stopped mid-collection
        else:
            sts_level = lvl
            sts_cat = cat_override or _LEVEL_CATEGORY[lvl]
        break

    h_level = health_level(stream_health, srate, stale_age, phase)
    level = Level(max(sts_level, h_level))
    if level == sts_level:
        category = sts_cat
    elif (h_level == Level.L1 and stream_health and stream_health.startswith("🟡")
          and not (srate and srate > 0)):
        category = "info"          # an idle event stream gone quiet — a note, not amber
    else:
        category = _LEVEL_CATEGORY[level]
    if level == Level.NONE and phase == COLLECTING and (
            orig.startswith("🟢") or "in progress" in low or "reconnected" in low):
        category = "healthy"
    return level, category


def render_class(sts, phase=COLLECTING, **kw):
    """The presentation category alone — for ``CATEGORY_HEX[...]``."""
    return classify(sts, phase, **kw)[1]
