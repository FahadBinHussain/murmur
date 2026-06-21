#!/usr/bin/env bash
set -euo pipefail

export PUBLIC_PORT="${PORT:-7860}"
export PUBLIC_HOST="${HOST:-0.0.0.0}"
export PROXY_LISTEN_HOST="${PROXY_LISTEN_HOST:-$PUBLIC_HOST}"
export PROXY_LISTEN_PORT="${PROXY_LISTEN_PORT:-$PUBLIC_PORT}"
export FB_COOKIES_PATH="${FB_COOKIES_PATH:-/app/murmur/cookies.json}"
export MURMUR_ENABLED="${MURMUR_ENABLED:-true}"
export MURMUR_RESTART_SECONDS="${MURMUR_RESTART_SECONDS:-60}"
export MURMUR_PID_FILE="${MURMUR_PID_FILE:-/tmp/murmur.pid}"
export MURMUR_RESTART_NOW_FILE="${MURMUR_RESTART_NOW_FILE:-/tmp/murmur-restart-now}"
export MURMUR_FACEBOOK_COOKIE_EXPIRED_EXIT_CODE="${MURMUR_FACEBOOK_COOKIE_EXPIRED_EXIT_CODE:-42}"
export MURMUR_ADMIN_CONSOLE="${MURMUR_ADMIN_CONSOLE:-true}"
export MURMUR_ADMIN_PATH="${MURMUR_ADMIN_PATH:-/murmur-admin}"
export FB_LOGIN_AUTO_REFRESH="${FB_LOGIN_AUTO_REFRESH:-false}"
export FB_LOGIN_AUTO_REFRESH_COOLDOWN_SECONDS="${FB_LOGIN_AUTO_REFRESH_COOLDOWN_SECONDS:-60}"
export FB_LOGIN_AUTO_REFRESH_STAMP="${FB_LOGIN_AUTO_REFRESH_STAMP:-/tmp/murmur-facebook-login-refresh-last}"
export FB_LOGIN_BROWSER_ENGINE="${FB_LOGIN_BROWSER_ENGINE:-cloakbrowser}"
export FB_LOGIN_HEADLESS="${FB_LOGIN_HEADLESS:-true}"
export FB_LOGIN_EXPORT_PATH="${FB_LOGIN_EXPORT_PATH:-$FB_COOKIES_PATH}"
export FB_LOGIN_PERSIST_DB="${FB_LOGIN_PERSIST_DB:-true}"
export FB_LOGIN_PROFILE_VAULT_ENABLED="${FB_LOGIN_PROFILE_VAULT_ENABLED:-true}"
export FB_LOGIN_PROFILE_VAULT_RESTORE="${FB_LOGIN_PROFILE_VAULT_RESTORE:-true}"
export FB_LOGIN_PROFILE_VAULT_OVERWRITE="${FB_LOGIN_PROFILE_VAULT_OVERWRITE:-false}"
export FB_LOGIN_PROFILE_PERSIST_DB="${FB_LOGIN_PROFILE_PERSIST_DB:-true}"
export FB_LOGIN_PROFILE_BOOTSTRAP_COOKIES="${FB_LOGIN_PROFILE_BOOTSTRAP_COOKIES:-true}"
export FB_LOGIN_PROFILE_BOOTSTRAP_DB_COOKIES="${FB_LOGIN_PROFILE_BOOTSTRAP_DB_COOKIES:-true}"
export MURMUR_AI_BACKEND="${MURMUR_AI_BACKEND:-litellm}"
export LITELLM_WARMUP="${LITELLM_WARMUP:-false}"
export LITELLM_WARMUP_CHAT="${LITELLM_WARMUP_CHAT:-false}"

gateway_base_for_proxy="${LITELLM_BASE_URL:-${LITELLM_API_BASE_URL:-${OPENAI_API_BASE_URL:-}}}"
if [[ "${MURMUR_AI_BACKEND,,}" == "openwebui" ]]; then
  gateway_base_for_proxy="${OPENWEBUI_BASE_URL:-$gateway_base_for_proxy}"
fi
if [[ -z "$gateway_base_for_proxy" ]]; then
  echo "Selected AI backend base URL is empty; Murmur will expose health/admin routes, but chat/image requests need a configured backend."
fi

export PROXY_TARGET_BASE_URL="${PROXY_TARGET_BASE_URL:-${gateway_base_for_proxy:-http://127.0.0.1:9}}"

image_generation_model="${IMAGE_GENERATION_MODEL:-}"
if [[ "${MURMUR_ENABLE_IMAGE_PROXY:-false}" == "true" && -n "${CF_ACCOUNT_ID:-}" && -n "${CF_API_TOKEN:-}" && "$image_generation_model" == @cf/* ]]; then
  export IMAGE_PROXY_BASE_PATH="${IMAGE_PROXY_BASE_PATH:-/murmur-image-openai/v1}"
  export IMAGE_PROXY_API_KEY="${IMAGE_PROXY_API_KEY:-${IMAGES_OPENAI_API_KEY:-${CF_API_TOKEN}}}"
  export IMAGES_OPENAI_API_KEY="${IMAGES_OPENAI_API_KEY:-${IMAGE_PROXY_API_KEY}}"
  export IMAGES_OPENAI_API_BASE_URL="${IMAGES_OPENAI_API_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}${IMAGE_PROXY_BASE_PATH}}"
  export ENABLE_IMAGE_GENERATION="${ENABLE_IMAGE_GENERATION:-true}"
  export IMAGE_GENERATION_ENGINE="${IMAGE_GENERATION_ENGINE:-openai}"
  export IMAGE_GENERATION_MODEL="$image_generation_model"
  export IMAGE_SIZE="${IMAGE_SIZE:-1024x1024}"
  export IMAGE_STEPS="${IMAGE_STEPS:-4}"
  echo "Configured Murmur Cloudflare image bridge."
fi

shutdown() {
  local code="${1:-0}"
  if [[ -n "${MURMUR_PID:-}" ]]; then
    kill "$MURMUR_PID" 2>/dev/null || true
  fi
  if [[ -n "${PROXY_PID:-}" ]]; then
    kill "$PROXY_PID" 2>/dev/null || true
  fi
  rm -f "$MURMUR_PID_FILE" "$MURMUR_RESTART_NOW_FILE"
  wait 2>/dev/null || true
  exit "$code"
}

trap 'shutdown 143' TERM INT

python_bin() {
  if [[ -x "${PYTHON_BIN:-}" ]]; then
    printf '%s\n' "$PYTHON_BIN"
  elif [[ -x /app/murmur/.venv/bin/python ]]; then
    printf '%s\n' /app/murmur/.venv/bin/python
  else
    printf '%s\n' python
  fi
}

start_murmur() {
  echo "Starting Murmur..."
  "$(python_bin)" -m murmur &
  MURMUR_PID="$!"
  printf '%s' "$MURMUR_PID" > "$MURMUR_PID_FILE"
}

sleep_before_murmur_restart() {
  local delay="${1:-0}"
  local elapsed=0
  while (( elapsed < delay )); do
    if [[ -f "$MURMUR_RESTART_NOW_FILE" ]]; then
      rm -f "$MURMUR_RESTART_NOW_FILE"
      echo "Murmur restart sleep interrupted by admin console."
      return
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
}

facebook_login_refresh_recent() {
  local stamp_file="$FB_LOGIN_AUTO_REFRESH_STAMP"
  local cooldown="$FB_LOGIN_AUTO_REFRESH_COOLDOWN_SECONDS"
  if [[ ! -f "$stamp_file" ]]; then
    return 1
  fi

  local now
  now="$(date +%s)"
  local last
  last="$(cat "$stamp_file" 2>/dev/null || echo 0)"
  if [[ ! "$last" =~ ^[0-9]+$ ]]; then
    return 1
  fi

  (( now - last < cooldown ))
}

restore_facebook_profile_from_db_state() {
  if [[ "${FB_LOGIN_PROFILE_VAULT_ENABLED,,}" != "true" ]]; then
    return 0
  fi
  if [[ "${FB_LOGIN_PROFILE_VAULT_RESTORE,,}" != "true" ]]; then
    return 0
  fi

  local restore_cmd=("$(python_bin)" -m murmur.runtime_state restore-profile)
  if [[ "${FB_LOGIN_PROFILE_VAULT_OVERWRITE,,}" == "true" ]]; then
    restore_cmd+=(--overwrite)
  fi
  "${restore_cmd[@]}"
}

run_facebook_login_refresh() {
  if [[ "${FB_LOGIN_AUTO_REFRESH,,}" != "true" ]]; then
    echo "Facebook cookie auto-refresh is disabled."
    return 1
  fi
  if [[ -z "${FB_LOGIN_PASSWORD:-}" || ( -z "${FB_LOGIN_EMAIL:-}" && -z "${FB_LOGIN_PHONE:-}" ) ]]; then
    echo "Facebook cookie auto-refresh missing FB_LOGIN_EMAIL/FB_LOGIN_PHONE or FB_LOGIN_PASSWORD."
    return 1
  fi
  if facebook_login_refresh_recent; then
    echo "Facebook cookie auto-refresh skipped due to cooldown (${FB_LOGIN_AUTO_REFRESH_COOLDOWN_SECONDS}s)."
    return 1
  fi

  echo "Facebook cookie expiry detected; attempting hosted browser login refresh."
  restore_facebook_profile_from_db_state || true
  local refresh_cmd=("$(python_bin)" /app/murmur/scripts/facebook_login_refresh.py --persist-db --no-backup)
  if [[ "${FB_LOGIN_PROFILE_PERSIST_DB,,}" == "true" ]]; then
    refresh_cmd+=(--persist-profile-db)
  fi
  if [[ "${FB_LOGIN_HEADLESS,,}" == "false" ]]; then
    if command -v xvfb-run >/dev/null 2>&1; then
      echo "Running Facebook login refresh in headed Chromium with Xvfb."
      refresh_cmd=(xvfb-run -a "${refresh_cmd[@]}")
    else
      echo "Facebook login refresh requested headed mode, but xvfb-run is not installed."
      return 1
    fi
  else
    echo "Running Facebook login refresh in headless Chromium."
  fi

  if "${refresh_cmd[@]}"; then
    rm -f "$FB_LOGIN_AUTO_REFRESH_STAMP"
    echo "Facebook cookie auto-refresh succeeded."
    return 0
  fi

  if [[ "${FB_LOGIN_FORCE_FRESH_FALLBACK,,}" != "false" && "${FB_LOGIN_CLEAR_ON_VERIFY_FAILURE,,}" != "true" ]]; then
    echo "Facebook cookie auto-refresh did not verify saved cookies; retrying with stale cookies cleared."
    if FB_LOGIN_CLEAR_ON_VERIFY_FAILURE=true "${refresh_cmd[@]}"; then
      rm -f "$FB_LOGIN_AUTO_REFRESH_STAMP"
      echo "Facebook cookie auto-refresh succeeded after clearing stale cookies."
      return 0
    fi
  fi

  if [[ "${FB_LOGIN_DIRECT_FALLBACK_ON_PROXY_FAILURE,,}" == "true" && "${FB_LOGIN_PROXY:-}" != "direct" ]]; then
    echo "Facebook cookie auto-refresh failed through configured proxy; retrying browser refresh direct."
    if FB_LOGIN_PROXY=direct "${refresh_cmd[@]}"; then
      rm -f "$FB_LOGIN_AUTO_REFRESH_STAMP"
      echo "Facebook cookie auto-refresh succeeded with direct browser fallback."
      return 0
    fi

    if [[ "${FB_LOGIN_FORCE_FRESH_FALLBACK,,}" != "false" && "${FB_LOGIN_CLEAR_ON_VERIFY_FAILURE,,}" != "true" ]]; then
      echo "Facebook direct browser refresh did not verify saved cookies; retrying direct with stale cookies cleared."
      if FB_LOGIN_PROXY=direct FB_LOGIN_CLEAR_ON_VERIFY_FAILURE=true "${refresh_cmd[@]}"; then
        rm -f "$FB_LOGIN_AUTO_REFRESH_STAMP"
        echo "Facebook cookie auto-refresh succeeded with direct fresh-login fallback."
        return 0
      fi
    fi
  fi

  mkdir -p "$(dirname "$FB_LOGIN_AUTO_REFRESH_STAMP")"
  date +%s > "$FB_LOGIN_AUTO_REFRESH_STAMP"
  echo "Facebook cookie auto-refresh failed."
  return 1
}

write_cookies_from_db_state() {
  "$(python_bin)" -m murmur.runtime_state write-cookies
}

restore_facebook_profile_from_db_state || true

if write_cookies_from_db_state; then
  :
elif [[ -n "${FB_COOKIES_JSON_B64:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON_B64."
  printf '%s' "$FB_COOKIES_JSON_B64" | base64 -d > "$FB_COOKIES_PATH"
elif [[ -n "${FB_COOKIES_JSON:-}" ]]; then
  echo "Writing Messenger cookies from FB_COOKIES_JSON."
  printf '%s' "$FB_COOKIES_JSON" > "$FB_COOKIES_PATH"
fi

echo "Starting Murmur public proxy on ${PROXY_LISTEN_HOST}:${PROXY_LISTEN_PORT}"
"$(python_bin)" -m murmur.proxy &
PROXY_PID="$!"

if [[ "$MURMUR_ENABLED" == "true" ]]; then
  start_murmur
else
  echo "Murmur worker is disabled; proxy/admin routes will keep running."
fi

set +e
while true; do
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
    if [[ "$MURMUR_EXIT_CODE" == "$MURMUR_FACEBOOK_COOKIE_EXPIRED_EXIT_CODE" ]]; then
      if run_facebook_login_refresh; then
        RESTART_DELAY=1
      fi
    fi
    echo "Murmur exited with code ${MURMUR_EXIT_CODE}; restarting in ${RESTART_DELAY}s."
    MURMUR_PID=""
    rm -f "$MURMUR_PID_FILE"
    sleep_before_murmur_restart "$RESTART_DELAY"
    start_murmur
  fi
  sleep 2
done
