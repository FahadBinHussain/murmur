#!/usr/bin/env bash
set -euo pipefail

export PUBLIC_PORT="${PORT:-7860}"
export PUBLIC_HOST="${HOST:-0.0.0.0}"
export OPENWEBUI_INTERNAL_HOST="${OPENWEBUI_INTERNAL_HOST:-127.0.0.1}"
export OPENWEBUI_INTERNAL_PORT="${OPENWEBUI_INTERNAL_PORT:-8081}"
export OPENWEBUI_BASE_URL="${OPENWEBUI_BASE_URL:-http://${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}}"
export PROXY_LISTEN_HOST="$PUBLIC_HOST"
export PROXY_LISTEN_PORT="$PUBLIC_PORT"
export PROXY_TARGET_BASE_URL="http://${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}"
export FB_COOKIES_PATH="${FB_COOKIES_PATH:-/app/murmur/cookies.json}"
export MURMUR_ENABLED="${MURMUR_ENABLED:-true}"
export MURMUR_RESTART_SECONDS="${MURMUR_RESTART_SECONDS:-60}"
export ENABLE_OLLAMA_API="${ENABLE_OLLAMA_API:-false}"
export ENABLE_BASE_MODELS_CACHE="${ENABLE_BASE_MODELS_CACHE:-true}"
export OPENWEBUI_ACCESS_LOG="${OPENWEBUI_ACCESS_LOG:-false}"

if [[ -n "${CF_ACCOUNT_ID:-}" && -n "${CF_API_TOKEN:-}" && -n "${CLOUDFLARE_IMAGE_MODEL:-}" ]]; then
  export IMAGE_PROXY_BASE_PATH="${IMAGE_PROXY_BASE_PATH:-/murmur-image-openai/v1}"
  export IMAGE_PROXY_API_KEY="${IMAGE_PROXY_API_KEY:-${IMAGES_OPENAI_API_KEY:-${CF_API_TOKEN}}}"
  export IMAGES_OPENAI_API_KEY="${IMAGES_OPENAI_API_KEY:-${IMAGE_PROXY_API_KEY}}"
  export IMAGES_OPENAI_API_BASE_URL="${IMAGES_OPENAI_API_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}${IMAGE_PROXY_BASE_PATH}}"
  export ENABLE_IMAGE_GENERATION="${ENABLE_IMAGE_GENERATION:-true}"
  export IMAGE_GENERATION_ENGINE="${IMAGE_GENERATION_ENGINE:-openai}"
  export IMAGE_GENERATION_MODEL="${IMAGE_GENERATION_MODEL:-${CLOUDFLARE_IMAGE_MODEL}}"
  export IMAGE_SIZE="${IMAGE_SIZE:-1024x1024}"
  export IMAGE_STEPS="${IMAGE_STEPS:-4}"
  echo "Configured Open WebUI image generation through Murmur Cloudflare bridge."
fi

trim_value() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

configure_openrouter_connections() {
  local openrouter_keys=()
  local key value

  if [[ -n "${OPENROUTER_API_KEYS:-}" ]]; then
    IFS=';' read -ra split_keys <<< "$OPENROUTER_API_KEYS"
    for key in "${split_keys[@]}"; do
      value="$(trim_value "$key")"
      if [[ -n "$value" ]]; then
        openrouter_keys+=("$value")
      fi
    done
  fi

  for index in 1 2 3 4 5; do
    key="OPENROUTER_API_KEY_${index}"
    value="$(trim_value "${!key:-}")"
    if [[ -n "$value" ]]; then
      openrouter_keys+=("$value")
    fi
  done

  if (( ${#openrouter_keys[@]} == 0 )); then
    return
  fi

  local base_url="${OPENROUTER_API_BASE_URL:-https://openrouter.ai/api/v1}"
  local repeated_urls=()
  for _ in "${openrouter_keys[@]}"; do
    repeated_urls+=("$base_url")
  done

  export OPENAI_API_BASE_URL="${OPENAI_API_BASE_URL:-$base_url}"
  if [[ -z "${OPENAI_API_KEYS:-}" ]]; then
    export OPENAI_API_KEYS="$(IFS=';'; printf '%s' "${openrouter_keys[*]}")"
  fi
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    export OPENAI_API_KEY="${openrouter_keys[0]}"
  fi
  if [[ -z "${OPENAI_API_BASE_URLS:-}" ]]; then
    export OPENAI_API_BASE_URLS="$(IFS=';'; printf '%s' "${repeated_urls[*]}")"
  fi
  if [[ -z "${OPENAI_API_CONFIGS:-}" ]]; then
    export OPENROUTER_CONNECTION_COUNT="${#openrouter_keys[@]}"
    export OPENAI_API_CONFIGS="$(
      python - <<'PY'
import json
import os

count = int(os.environ["OPENROUTER_CONNECTION_COUNT"])
configs = {
    str(index): {
        "enable": True,
        "connection_type": "external",
        "prefix_id": f"or{index + 1}",
    }
    for index in range(count)
}
print(json.dumps(configs, separators=(",", ":")))
PY
    )"
    unset OPENROUTER_CONNECTION_COUNT
  fi

  export MURMUR_OPENROUTER_CONFIGURED=true
  export MURMUR_OPENROUTER_BASE_URL="$base_url"
  export MURMUR_OPENROUTER_KEYS="$(IFS=';'; printf '%s' "${openrouter_keys[*]}")"
  echo "Configured ${#openrouter_keys[@]} OpenRouter key(s) for Open WebUI."
}

sync_openrouter_config_to_openwebui() {
  if [[ "${OPENROUTER_SYNC_OPENWEBUI_CONFIG:-true}" != "true" ]]; then
    return
  fi
  if [[ "${MURMUR_OPENROUTER_CONFIGURED:-false}" != "true" ]]; then
    return
  fi

  local admin_email="${OPENWEBUI_LOGIN_EMAIL:-${WEBUI_ADMIN_EMAIL:-}}"
  local admin_password="${OPENWEBUI_LOGIN_PASSWORD:-${WEBUI_ADMIN_PASSWORD:-}}"
  if [[ -z "$admin_email" || -z "$admin_password" ]]; then
    echo "Skipping OpenRouter Open WebUI config sync: admin login is not configured."
    return
  fi

  OPENWEBUI_ADMIN_EMAIL_FOR_SYNC="$admin_email" \
  OPENWEBUI_ADMIN_PASSWORD_FOR_SYNC="$admin_password" \
  /app/murmur/.venv/bin/python - <<'PY'
import json
import os
import re
import sys
import urllib.error
import urllib.request

base_url = os.environ["OPENWEBUI_BASE_URL"].rstrip("/")
openrouter_url = os.environ["MURMUR_OPENROUTER_BASE_URL"].rstrip("/")
openrouter_keys = [
    key.strip()
    for key in os.environ.get("MURMUR_OPENROUTER_KEYS", "").split(";")
    if key.strip()
]
email = os.environ["OPENWEBUI_ADMIN_EMAIL_FOR_SYNC"]
password = os.environ["OPENWEBUI_ADMIN_PASSWORD_FOR_SYNC"]

if not openrouter_keys:
    sys.exit(0)


def request(path, method="GET", body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned {exc.code}: {raw[:500]}") from exc


try:
    token = request(
        "/api/v1/auths/signin",
        "POST",
        {"email": email, "password": password},
    )["token"]
    config = request("/openai/config", token=token)

    urls = list(config.get("OPENAI_API_BASE_URLS") or [])
    keys = list(config.get("OPENAI_API_KEYS") or [])
    configs = config.get("OPENAI_API_CONFIGS") or {}
    if not isinstance(configs, dict):
        configs = {}

    preserved = []
    for index, url in enumerate(urls):
        url = str(url)
        key = keys[index] if index < len(keys) else ""
        api_config = configs.get(str(index), configs.get(url, {}))
        if not isinstance(api_config, dict):
            api_config = {}
        prefix = str(api_config.get("prefix_id", "")).lower()
        if url.rstrip("/") == openrouter_url or re.fullmatch(r"or\d+", prefix):
            continue
        preserved.append((url, key, api_config))

    desired = [
        (
            openrouter_url,
            key,
            {
                "enable": True,
                "connection_type": "external",
                "prefix_id": f"or{index + 1}",
            },
        )
        for index, key in enumerate(openrouter_keys)
    ]
    entries = desired + preserved
    payload = {
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": [entry[0] for entry in entries],
        "OPENAI_API_KEYS": [entry[1] for entry in entries],
        "OPENAI_API_CONFIGS": {
            str(index): entry[2] for index, entry in enumerate(entries)
        },
    }
    request("/openai/config/update", "POST", payload, token=token)
    print(
        f"Synced {len(openrouter_keys)} OpenRouter connection(s) into Open WebUI."
    )
except Exception as exc:
    print(f"OpenRouter Open WebUI config sync failed: {exc}")
PY
}

configure_openrouter_connections

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

start_murmur() {
  echo "Starting Murmur..."
  /app/murmur/.venv/bin/python -m murmur &
  MURMUR_PID="$!"
}

if [[ -n "${FB_COOKIES_JSON_B64:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON_B64."
  printf '%s' "$FB_COOKIES_JSON_B64" | base64 -d > "$FB_COOKIES_PATH"
elif [[ -n "${FB_COOKIES_JSON:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON."
  printf '%s' "$FB_COOKIES_JSON" > "$FB_COOKIES_PATH"
fi

echo "Starting public proxy on ${PROXY_LISTEN_HOST}:${PROXY_LISTEN_PORT}"
/app/murmur/.venv/bin/python -m murmur.proxy &
PROXY_PID="$!"

echo "Starting Open WebUI on ${OPENWEBUI_INTERNAL_HOST}:${OPENWEBUI_INTERNAL_PORT}"
OPENWEBUI_UVICORN_ARGS=(--workers "${UVICORN_WORKERS:-1}")
if [[ "${OPENWEBUI_ACCESS_LOG,,}" != "true" ]]; then
  OPENWEBUI_UVICORN_ARGS+=(--no-access-log)
fi
(
  cd /app/backend
  set -o pipefail
  HOST="$OPENWEBUI_INTERNAL_HOST" PORT="$OPENWEBUI_INTERNAL_PORT" \
    bash start.sh "${OPENWEBUI_UVICORN_ARGS[@]}" 2>&1 \
    | /app/murmur/.venv/bin/python -m murmur.log_filter
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
    if [[ -d "/proc/${OPENWEBUI_PID}" ]]; then
      echo "Open WebUI process ${OPENWEBUI_PID} is still running."
    else
      echo "Open WebUI process ${OPENWEBUI_PID} is not visible in /proc."
    fi
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

sync_openrouter_config_to_openwebui

if [[ "$MURMUR_ENABLED" == "true" ]]; then
  start_murmur
else
  echo "Murmur is disabled; Open WebUI will keep running."
fi

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
  if [[ "$MURMUR_ENABLED" == "true" ]] && [[ -n "${MURMUR_PID:-}" ]] && ! kill -0 "$MURMUR_PID" 2>/dev/null; then
    wait "$MURMUR_PID"
    MURMUR_EXIT_CODE="$?"
    echo "Murmur exited with code ${MURMUR_EXIT_CODE}; restarting in ${MURMUR_RESTART_SECONDS}s."
    MURMUR_PID=""
    sleep "$MURMUR_RESTART_SECONDS"
    start_murmur
  fi
  sleep 2
done
