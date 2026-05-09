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
export MURMUR_PID_FILE="${MURMUR_PID_FILE:-/tmp/murmur.pid}"
export MURMUR_RESTART_NOW_FILE="${MURMUR_RESTART_NOW_FILE:-/tmp/murmur-restart-now}"
export MURMUR_ADMIN_CONSOLE="${MURMUR_ADMIN_CONSOLE:-true}"
export MURMUR_ADMIN_PATH="${MURMUR_ADMIN_PATH:-/murmur-admin}"
export ENABLE_OLLAMA_API="${ENABLE_OLLAMA_API:-false}"
export ENABLE_BASE_MODELS_CACHE="${ENABLE_BASE_MODELS_CACHE:-true}"
export OPENWEBUI_ACCESS_LOG="${OPENWEBUI_ACCESS_LOG:-false}"

image_generation_model="${IMAGE_GENERATION_MODEL:-${CLOUDFLARE_IMAGE_MODEL:-}}"
if [[ -n "${CF_ACCOUNT_ID:-}" && -n "${CF_API_TOKEN:-}" && "$image_generation_model" == @cf/* ]]; then
  export IMAGE_PROXY_BASE_PATH="${IMAGE_PROXY_BASE_PATH:-/murmur-image-openai/v1}"
  export IMAGE_PROXY_API_KEY="${IMAGE_PROXY_API_KEY:-${IMAGES_OPENAI_API_KEY:-${CF_API_TOKEN}}}"
  export IMAGES_OPENAI_API_KEY="${IMAGES_OPENAI_API_KEY:-${IMAGE_PROXY_API_KEY}}"
  export IMAGES_OPENAI_API_BASE_URL="${IMAGES_OPENAI_API_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}${IMAGE_PROXY_BASE_PATH}}"
  export ENABLE_IMAGE_GENERATION="${ENABLE_IMAGE_GENERATION:-true}"
  export IMAGE_GENERATION_ENGINE="${IMAGE_GENERATION_ENGINE:-openai}"
  export IMAGE_GENERATION_MODEL="$image_generation_model"
  export IMAGE_SIZE="${IMAGE_SIZE:-1024x1024}"
  export IMAGE_STEPS="${IMAGE_STEPS:-4}"
  echo "Configured Open WebUI image generation through Murmur Cloudflare bridge."
fi

configure_provider_connections() {
  local python_bin="${PYTHON_BIN:-/app/murmur/.venv/bin/python}"
  if [[ ! -x "$python_bin" ]]; then
    python_bin="python"
  fi

  eval "$("$python_bin" - <<'PY'
import json
import os
import re
import shlex
import sys


def split_items(value):
    return [item.strip() for item in re.split(r"[;,\s]+", value or "") if item.strip()]


def env_prefix(family):
    return re.sub(r"[^A-Z0-9]+", "_", family.upper()).strip("_")


def canonical_family(value):
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value


def provider_keys(prefix):
    keys = []
    for item in (os.getenv(f"{prefix}_API_KEYS") or "").replace("\n", ";").split(";"):
        item = item.strip()
        if item:
            keys.append(item)
    for index in range(1, 21):
        item = (os.getenv(f"{prefix}_API_KEY_{index}") or "").strip()
        if item:
            keys.append(item)
    if prefix == "CLOUDFLARE":
        for name in ("CLOUDFLARE_API_KEY", "CF_API_TOKEN"):
            item = (os.getenv(name) or "").strip()
            if item:
                keys.append(item)
    return list(dict.fromkeys(keys))


def provider_model_ids(prefix):
    raw = os.getenv(f"{prefix}_MODEL_IDS") or os.getenv(f"{prefix}_MODELS") or ""
    return split_items(raw.replace("\n", ";"))


families = [canonical_family(item) for item in split_items(os.getenv("OPENWEBUI_PROVIDER_FAMILIES", ""))]
families = [family for family in families if family]
if not families and provider_keys("OPENROUTER"):
    families = ["openrouter"]

default_base_urls = {
    "openrouter": "https://openrouter.ai/api/v1",
}
cloudflare_account_id = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CF_ACCOUNT_ID") or "").strip()
if cloudflare_account_id:
    default_base_urls["cloudflare"] = (
        f"https://api.cloudflare.com/client/v4/accounts/{cloudflare_account_id}/ai/v1"
    )

connections = []
for family in families:
    prefix = env_prefix(family)
    base_url = (os.getenv(f"{prefix}_API_BASE_URL") or default_base_urls.get(family, "")).strip().rstrip("/")
    keys = provider_keys(prefix)
    model_ids = provider_model_ids(prefix)
    if not keys:
        print(f"Skipping {family}: no {prefix}_API_KEY_N values configured.", file=sys.stderr)
        continue
    if not base_url:
        print(f"Skipping {family}: {prefix}_API_BASE_URL is required.", file=sys.stderr)
        continue

    for index, key in enumerate(keys, start=1):
        connections.append(
            {
                "family": family,
                "index": index,
                "base_url": base_url,
                "key": key,
                "prefix_id": f"{family}_{index}",
                "model_ids": model_ids,
            }
        )


def emit(name, value):
    print(f"export {name}={shlex.quote(str(value))}")


if not connections:
    emit("MURMUR_PROVIDER_CONFIGURED", "false")
    raise SystemExit(0)

urls = [connection["base_url"] for connection in connections]
keys = [connection["key"] for connection in connections]
configs = {
    str(index): {
        "enable": True,
        "connection_type": "external",
        "prefix_id": connection["prefix_id"],
        **({"model_ids": connection["model_ids"]} if connection.get("model_ids") else {}),
    }
    for index, connection in enumerate(connections)
}

emit("MURMUR_PROVIDER_CONFIGURED", "true")
emit("MURMUR_PROVIDER_CONNECTIONS_JSON", json.dumps(connections, separators=(",", ":")))
emit("OPENAI_API_BASE_URL", os.getenv("OPENAI_API_BASE_URL") or urls[0])
emit("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY") or keys[0])
emit("OPENAI_API_BASE_URLS", os.getenv("OPENAI_API_BASE_URLS") or ";".join(urls))
emit("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEYS") or ";".join(keys))
emit("OPENAI_API_CONFIGS", os.getenv("OPENAI_API_CONFIGS") or json.dumps(configs, separators=(",", ":")))

counts = {}
for connection in connections:
    counts[connection["family"]] = counts.get(connection["family"], 0) + 1
summary = ", ".join(f"{family}={count}" for family, count in sorted(counts.items()))
print(f"Configured Open WebUI provider connections: {summary}.", file=sys.stderr)
PY
  )"
}

sync_provider_config_to_openwebui() {
  local provider_sync="${OPENWEBUI_PROVIDER_SYNC:-${OPENROUTER_SYNC_OPENWEBUI_CONFIG:-true}}"
  if [[ "$provider_sync" != "true" ]]; then
    return
  fi
  if [[ "${MURMUR_PROVIDER_CONFIGURED:-false}" != "true" ]]; then
    return
  fi

  local admin_email="${OPENWEBUI_LOGIN_EMAIL:-${WEBUI_ADMIN_EMAIL:-}}"
  local admin_password="${OPENWEBUI_LOGIN_PASSWORD:-${WEBUI_ADMIN_PASSWORD:-}}"
  if [[ -z "$admin_email" || -z "$admin_password" ]]; then
    echo "Skipping Open WebUI provider config sync: admin login is not configured."
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
import urllib.parse
import urllib.request

base_url = os.environ["OPENWEBUI_BASE_URL"].rstrip("/")
connections = json.loads(os.environ.get("MURMUR_PROVIDER_CONNECTIONS_JSON", "[]"))
email = os.environ["OPENWEBUI_ADMIN_EMAIL_FOR_SYNC"]
password = os.environ["OPENWEBUI_ADMIN_PASSWORD_FOR_SYNC"]

if not connections:
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


def desired_entries(model_ids_by_family=None):
    model_ids_by_family = model_ids_by_family or {}
    entries = []
    for connection in connections:
        model_ids = list(
            dict.fromkeys(
                list(connection.get("model_ids") or [])
                + list(model_ids_by_family.get(connection["family"], []))
            )
        )
        config = {
            "enable": True,
            "connection_type": "external",
            "prefix_id": connection["prefix_id"],
            **({"model_ids": model_ids} if model_ids else {}),
        }
        entries.append((connection["base_url"].rstrip("/"), connection["key"], config))
    return entries


def update_config(desired_entries, preserved_entries):
    entries = desired_entries + preserved_entries
    payload = {
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": [entry[0] for entry in entries],
        "OPENAI_API_KEYS": [entry[1] for entry in entries],
        "OPENAI_API_CONFIGS": {
            str(index): entry[2] for index, entry in enumerate(entries)
        },
    }
    request("/openai/config/update", "POST", payload, token=token)


def fetch_cloudflare_model_ids():
    account_id = (
        os.getenv("CLOUDFLARE_ACCOUNT_ID")
        or os.getenv("CF_ACCOUNT_ID")
        or ""
    ).strip()
    api_token = next(
        (
            connection["key"]
            for connection in connections
            if connection.get("family") == "cloudflare" and connection.get("key")
        ),
        "",
    )
    if not account_id or not api_token:
        return []

    hide_experimental = (
        os.getenv("CLOUDFLARE_HIDE_EXPERIMENTAL_MODELS", "true").lower()
        not in {"0", "false", "no", "off"}
    )
    model_ids = []
    for page in range(1, 6):
        params = urllib.parse.urlencode(
            {
                "page": page,
                "per_page": 100,
                "hide_experimental": str(hide_experimental).lower(),
            }
        )
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/models/search?{params}"
        )
        req.add_header("Authorization", f"Bearer {api_token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"cloudflare model-id search skipped: {exc}")
            return []

        page_models = body.get("result") if isinstance(body, dict) else None
        if not isinstance(page_models, list):
            break
        for model in page_models:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("name") or "").strip()
            if model_id:
                model_ids.append(model_id)
        if len(page_models) < 100:
            break

    return sorted(dict.fromkeys(model_ids))


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

    managed_prefixes = {connection["prefix_id"].lower() for connection in connections}
    managed_base_urls = {connection["base_url"].rstrip("/") for connection in connections}
    preserved = []
    for index, url in enumerate(urls):
        url = str(url)
        key = keys[index] if index < len(keys) else ""
        api_config = configs.get(str(index), configs.get(url, {}))
        if not isinstance(api_config, dict):
            api_config = {}
        prefix = str(api_config.get("prefix_id", "")).lower()
        if (
            prefix in managed_prefixes
            or re.fullmatch(r"or\d+", prefix)
            or url.rstrip("/") in managed_base_urls
        ):
            continue
        preserved.append((url, key, api_config))

    update_config(desired_entries(), preserved)

    first_index_by_family = {}
    for index, connection in enumerate(connections):
        first_index_by_family.setdefault(connection["family"], index)

    model_ids_by_family = {}
    for connection in connections:
        configured_model_ids = connection.get("model_ids") or []
        if configured_model_ids:
            existing = model_ids_by_family.setdefault(connection["family"], [])
            existing.extend(configured_model_ids)

    cloudflare_model_ids = fetch_cloudflare_model_ids()
    if cloudflare_model_ids:
        existing = model_ids_by_family.setdefault("cloudflare", [])
        existing.extend(cloudflare_model_ids)

    for family, index in first_index_by_family.items():
        if model_ids_by_family.get(family):
            continue
        model_ids = []
        try:
            model_response = request(f"/openai/models/{index}", token=token)
            for model in model_response.get("data", []):
                if isinstance(model, dict):
                    model_id = model.get("id") or model.get("name")
                else:
                    model_id = model
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())
            model_ids = sorted(dict.fromkeys(model_ids))
        except Exception as exc:
            print(f"{family} model-id sync skipped: {exc}")
        if model_ids:
            existing = model_ids_by_family.setdefault(family, [])
            existing.extend(model_ids)

    model_ids_by_family = {
        family: sorted(dict.fromkeys(model_ids))
        for family, model_ids in model_ids_by_family.items()
    }

    if model_ids_by_family:
        update_config(desired_entries(model_ids_by_family), preserved)

    counts = {}
    for connection in connections:
        counts[connection["family"]] = counts.get(connection["family"], 0) + 1
    summary = ", ".join(
        f"{family}={count}" for family, count in sorted(counts.items())
    )
    explicit_summary = ", ".join(
        f"{family}={len(model_ids)}" for family, model_ids in sorted(model_ids_by_family.items())
    )
    if explicit_summary:
        print(
            "Synced Open WebUI provider connections: "
            f"{summary}; explicit models: {explicit_summary}."
        )
    else:
        print(f"Synced Open WebUI provider connections: {summary}.")
except Exception as exc:
    print(f"Open WebUI provider config sync failed: {exc}")
PY
}

configure_provider_connections

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
  rm -f "$MURMUR_PID_FILE" "$MURMUR_RESTART_NOW_FILE"
  wait 2>/dev/null || true
  exit "$code"
}

trap 'shutdown 143' TERM INT

start_murmur() {
  echo "Starting Murmur..."
  /app/murmur/.venv/bin/python -m murmur &
  MURMUR_PID="$!"
  printf '%s' "$MURMUR_PID" > "$MURMUR_PID_FILE"
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

sync_provider_config_to_openwebui

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
    RESTART_DELAY="$MURMUR_RESTART_SECONDS"
    if [[ -f "$MURMUR_RESTART_NOW_FILE" ]]; then
      rm -f "$MURMUR_RESTART_NOW_FILE"
      RESTART_DELAY=1
      echo "Murmur restart requested by admin console."
    fi
    echo "Murmur exited with code ${MURMUR_EXIT_CODE}; restarting in ${RESTART_DELAY}s."
    MURMUR_PID=""
    rm -f "$MURMUR_PID_FILE"
    sleep "$RESTART_DELAY"
    start_murmur
  fi
  sleep 2
done
