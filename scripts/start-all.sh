#!/usr/bin/env bash
set -euo pipefail

export PUBLIC_PORT="${PORT:-8080}"
export PUBLIC_HOST="${HOST:-0.0.0.0}"
export OPENWEBUI_INTERNAL_HOST="${OPENWEBUI_INTERNAL_HOST:-127.0.0.1}"
export OPENWEBUI_INTERNAL_PORT="${OPENWEBUI_INTERNAL_PORT:-8081}"
export OPENWEBUI_BASE_URL="${OPENWEBUI_BASE_URL:-http://${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}}"
export PROXY_LISTEN_HOST="$PUBLIC_HOST"
export PROXY_LISTEN_PORT="$PUBLIC_PORT"
export PROXY_TARGET_BASE_URL="http://${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}"
export FB_COOKIES_PATH="${FB_COOKIES_PATH:-/app/murmur/cookies.json}"

shutdown() {
  local code="${1:-0}"
  if [[ -n "${MURMUR_PID:-}" ]]; then
    kill "$MURMUR_PID" 2>/dev/null || true
  fi
  if [[ -n "${PROXY_PID:-}" ]]; then
    kill "$PROXY_PID" 2>/dev/null || true
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

echo "Starting public proxy on ${PROXY_LISTEN_HOST}:${PROXY_LISTEN_PORT}"
python -m murmur.proxy &
PROXY_PID="$!"

echo "Starting Open WebUI on ${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}"
(
  cd /app/backend
  HOST="$OPENWEBUI_INTERNAL_HOST" PORT="$OPENWEBUI_INTERNAL_PORT" bash start.sh
) &
OPENWEBUI_PID="$!"

echo "Waiting for Open WebUI health check..."
OPENWEBUI_WAIT_SECONDS=0
while true; do
  if HEALTH_BODY="$(curl --silent --show-error --max-time 5 "${OPENWEBUI_BASE_URL}/health" 2>&1)"; then
    echo "Open WebUI is ready."
    break
  fi
  OPENWEBUI_WAIT_SECONDS=$((OPENWEBUI_WAIT_SECONDS + 2))
  if (( OPENWEBUI_WAIT_SECONDS % 30 == 0 )); then
    echo "Still waiting for Open WebUI after ${OPENWEBUI_WAIT_SECONDS}s."
    echo "Health check response: ${HEALTH_BODY}"
    if command -v ss >/dev/null 2>&1; then
      echo "Listening sockets:"
      ss -ltnp || true
    elif command -v netstat >/dev/null 2>&1; then
      echo "Listening sockets:"
      netstat -ltnp || true
    fi
    echo "Open WebUI process:"
    ps -fp "$OPENWEBUI_PID" || true
  fi
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "Public proxy exited before Open WebUI became healthy."
    wait "$PROXY_PID"
  fi
  if ! kill -0 "$OPENWEBUI_PID" 2>/dev/null; then
    echo "Open WebUI exited before becoming healthy."
    wait "$OPENWEBUI_PID"
  fi
  sleep 2
done

echo "Starting Murmur..."
python -m murmur &
MURMUR_PID="$!"

set +e
while true; do
  if ! kill -0 "$OPENWEBUI_PID" 2>/dev/null; then
    wait "$OPENWEBUI_PID"
    shutdown "$?"
  fi
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    wait "$PROXY_PID"
    shutdown "$?"
  fi
  if ! kill -0 "$MURMUR_PID" 2>/dev/null; then
    wait "$MURMUR_PID"
    shutdown "$?"
  fi
  sleep 2
done
