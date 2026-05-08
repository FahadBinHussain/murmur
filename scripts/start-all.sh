#!/usr/bin/env bash
set -euo pipefail

export PORT="${PORT:-8080}"
export HOST="${HOST:-0.0.0.0}"
export OPENWEBUI_BASE_URL="${OPENWEBUI_BASE_URL:-http://127.0.0.1:${PORT}}"
export FB_COOKIES_PATH="${FB_COOKIES_PATH:-/app/murmur/cookies.json}"

shutdown() {
  local code="${1:-0}"
  if [[ -n "${MURMUR_PID:-}" ]]; then
    kill "$MURMUR_PID" 2>/dev/null || true
  fi
  if [[ -n "${OPENWEBUI_PID:-}" ]]; then
    kill "$OPENWEBUI_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit "$code"
}

trap 'shutdown 143' TERM INT

if [[ -n "${FB_COOKIES_JSON_B64:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON_B64."
  printf '%s' "$FB_COOKIES_JSON_B64" | base64 -d > "$FB_COOKIES_PATH"
elif [[ -n "${FB_COOKIES_JSON:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON."
  printf '%s' "$FB_COOKIES_JSON" > "$FB_COOKIES_PATH"
fi

echo "Starting Open WebUI on ${HOST}:${PORT}"
(
  cd /app/backend
  bash start.sh
) &
OPENWEBUI_PID="$!"

echo "Waiting for Open WebUI health check..."
for _ in $(seq 1 90); do
  if curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "Open WebUI is ready."
    break
  fi
  if ! kill -0 "$OPENWEBUI_PID" 2>/dev/null; then
    echo "Open WebUI exited before becoming healthy."
    wait "$OPENWEBUI_PID"
  fi
  sleep 2
done

if ! curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "Open WebUI did not become healthy in time."
  shutdown 1
fi

echo "Starting Murmur..."
python -m murmur &
MURMUR_PID="$!"

set +e
while true; do
  if ! kill -0 "$OPENWEBUI_PID" 2>/dev/null; then
    wait "$OPENWEBUI_PID"
    shutdown "$?"
  fi
  if ! kill -0 "$MURMUR_PID" 2>/dev/null; then
    wait "$MURMUR_PID"
    shutdown "$?"
  fi
  sleep 2
done
