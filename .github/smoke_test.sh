#!/usr/bin/env bash
# Launch the frozen PLASMA binary from a CLEAN directory (no ./plasma next to it,
# no editable install on the path) and verify it actually starts and loads every
# device plugin. Reproduces the failure mode where the bundle isn't
# self-contained and the app only works when run from a source checkout.
#
# Env: PLASMA_BIN — path to the built binary (absolute, or relative to repo root).
set -uo pipefail

case "${PLASMA_BIN:?set PLASMA_BIN}" in
  /*) BIN="$PLASMA_BIN" ;;
  *)  BIN="$PWD/$PLASMA_BIN" ;;
esac
[ -x "$BIN" ] || { echo "::error::$BIN not found or not executable"; exit 1; }

WORK="$(mktemp -d)"
LOG="$WORK/plasma.log"
cd "$WORK" || exit 1

# Unbuffered so we can watch the log; the app's own stdout is block-buffered
# otherwise and only flushes on exit.
PYTHONUNBUFFERED=1 "$BIN" > "$LOG" 2>&1 &
PID=$!

ok=0
for _ in $(seq 1 120); do
  if curl -sf -o /dev/null "http://127.0.0.1:7860/"; then ok=1; break; fi
  kill -0 "$PID" 2>/dev/null || { echo "app process exited before serving"; break; }
  sleep 1
done

kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true

echo "----- app output -----"
cat "$LOG"
echo "----------------------"

if [ "$ok" -ne 1 ]; then
  echo "::error::Gradio server never came up on 127.0.0.1:7860"
  exit 1
fi

# A plugin that failed to import, a swallowed crash, or a missing module.
if grep -qE "plugin .* unavailable|An error occurred:|Traceback \(most recent call last\)|No module named" "$LOG"; then
  echo "::error::frozen app reported a missing module / import failure"
  exit 1
fi

echo "smoke test passed (clean dir, all plugins loaded)"
