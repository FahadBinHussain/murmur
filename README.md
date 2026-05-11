---
title: Murmur
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
---

# Murmur

Murmur is a Facebook Messenger bridge for [Open WebUI](https://github.com/open-webui/open-webui), using [fbchat-muqit](https://github.com/togashigreat/fbchat-muqit) for Messenger transport.

It lets a Messenger thread talk to the same model router, provider connections, image generation settings, authentication, and persistence that Open WebUI already manages.

```text
Facebook Messenger
  -> fbchat-muqit
  -> Murmur
  -> Open WebUI
  -> Murmur
  -> fbchat-muqit
  -> Facebook Messenger
```

## Project Boundary

Murmur is designed to be a bridge, not a second AI platform.

- Messenger transport belongs to `fbchat-muqit`.
- AI chat and image entry points belong to Open WebUI.
- Provider keys are synced into Open WebUI Connections.
- Per-thread model selection stores Open WebUI model IDs.
- Raw upstream errors are surfaced back through Messenger when possible.

There is one intentional adapter exception: Cloudflare Workers AI image generation. Open WebUI expects an OpenAI-compatible image endpoint, while Cloudflare image models use Cloudflare's `/ai/run/{model}` API. Murmur can expose a small local OpenAI-compatible image adapter so Open WebUI can call Cloudflare image models. Chat still goes through Open WebUI.

## Features

- Messenger listener and sender through `fbchat-muqit`
- Open WebUI chat bridge through `/api/chat/completions`
- Open WebUI image bridge through `/api/v1/images/generations`
- Bundled all-in-one Docker image based on the official Open WebUI image
- External Open WebUI mode for separate deployments
- Open WebUI JWT sign-in fallback when no API key is configured
- Provider connection sync for OpenAI-compatible providers
- Symmetric per-thread chat and image model selection
- Short per-thread memory before sending messages to Open WebUI
- Long Messenger replies split into deliverable chunks
- Optional allowed-thread allowlist and admin thread gate
- Optional Facebook HTTP, MQTT, and upload proxies
- Name-aware Messenger event logs
- Password-protected admin console for thread access and cookie upload

## Command Reference

The canonical Messenger command prefix is `/ai`.

```text
Chat
/ai <message>
/ai model <provider> [connection] <number>

Image
/ai image <prompt>
/ai image model <provider> [connection] <number>

Models
/ai models
/ai models <provider> [connection]
/ai status
```

`/help` and `/ai help` return the same short command reference inside Messenger.

Examples:

```text
/ai explain this in one sentence
/ai models
/ai models openrouter 2
/ai model openrouter 2 1
/ai image a brutalist library in heavy rain
/ai image model cloudflare 1 12
/ai status
```

Model choices are remembered per Messenger thread. Chat model selection and image model selection are separate, but they use the same provider/connection/number syntax.

Responses include the selected provider and model:

```text
[openrouter 2 - deepseek/deepseek-chat-v3-0324:free]
```

## Architecture

The all-in-one Docker deployment starts three processes:

- Open WebUI on an internal port
- Murmur's public proxy on the public port
- Murmur's Messenger listener

The public proxy forwards normal web traffic to Open WebUI and also hosts the optional local Cloudflare image adapter.

```text
Browser / HF Space URL
  -> Murmur public proxy
  -> Open WebUI

Messenger event
  -> fbchat-muqit
  -> Murmur listener
  -> Open WebUI API
  -> fbchat-muqit
```

## Requirements

- Python 3.10+
- Facebook cookies usable by `fbchat-muqit`
- Open WebUI API key or Open WebUI admin email/password
- At least one model/provider configured in Open WebUI

For hosted deployments, use a dedicated Facebook account. `fbchat-muqit` is unofficial, and Facebook can change or restrict behavior without notice.

Logging out of Facebook, clearing browser sessions, or using "log out of all devices" can expire the cookies Murmur depends on. Export fresh cookies after any logout.

## Local Setup

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Create a local environment file:

```bash
cp .env.example .env
```

Minimal external Open WebUI configuration:

```env
OPENWEBUI_BASE_URL=https://your-openwebui.example.com
OPENWEBUI_API_KEY=your-openwebui-api-key
OPENWEBUI_MODEL=your-model-id
FB_COOKIES_PATH=cookies.json
```

Run Murmur:

```bash
python -m murmur
```

## Docker

Build the all-in-one image:

```bash
docker build -t murmur .
```

Run it:

```bash
docker run --env-file .env -p 7860:7860 -v ./cookies.json:/app/murmur/cookies.json:ro murmur
```

Open WebUI is exposed on port `7860`.

## Provider Connections

Murmur syncs provider keys into Open WebUI Connections at startup. The provider shape is consistent:

```env
OPENWEBUI_PROVIDER_SYNC=true
OPENWEBUI_PROVIDER_FAMILIES=openrouter,cloudflare

OPENROUTER_API_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY_1=sk-or-v1-...
OPENROUTER_API_KEY_2=sk-or-v1-...
OPENROUTER_API_KEY_3=sk-or-v1-...
OPENROUTER_API_KEY_4=sk-or-v1-...
OPENROUTER_API_KEY_5=sk-or-v1-...

CF_ACCOUNT_ID=your-cloudflare-account-id
CF_API_TOKEN=your-cloudflare-workers-ai-token
CLOUDFLARE_API_BASE_URL=
CLOUDFLARE_MODEL_IDS=@cf/openai/gpt-oss-20b,@cf/black-forest-labs/flux-1-schnell
CLOUDFLARE_HIDE_EXPERIMENTAL_MODELS=true
```

At runtime these become separate Open WebUI connections:

```text
openrouter 1
openrouter 2
openrouter 3
openrouter 4
openrouter 5
cloudflare 1
```

Future OpenAI-compatible providers use the same pattern:

```env
OPENWEBUI_PROVIDER_FAMILIES=openrouter,cloudflare,gemini,mistral

GEMINI_API_BASE_URL=https://your-openai-compatible-gemini-gateway/v1
GEMINI_API_KEY_1=...

MISTRAL_API_BASE_URL=https://your-openai-compatible-mistral-gateway/v1
MISTRAL_API_KEY_1=...
```

## Image Generation

Open WebUI is the image-generation entry point:

```text
Murmur
  -> Open WebUI /api/v1/images/generations
```

For providers that expose an OpenAI-compatible image API, point Open WebUI image settings at that provider.

For Cloudflare Workers AI image models, Murmur can expose a local adapter:

```env
IMAGE_PROXY_API_KEY=random-local-image-proxy-key
IMAGE_PROXY_BASE_PATH=/murmur-image-openai/v1
ENABLE_IMAGE_GENERATION=true
IMAGE_GENERATION_ENGINE=openai
IMAGE_GENERATION_MODEL=@cf/black-forest-labs/flux-1-schnell
IMAGE_SIZE=1024x1024
IMAGE_STEPS=4
```

That route is:

```text
Murmur
  -> Open WebUI image endpoint
  -> Murmur local image adapter
  -> Cloudflare Workers AI
```

This adapter exists only because Open WebUI does not natively speak Cloudflare's image generation API shape.

## Messenger Configuration

Core Messenger settings:

```env
BOT_PREFIX=/ai
RESPOND_ONLY_ON_PREFIX=true
RESPOND_TO_BOT_REPLIES=true
ALLOWED_THREAD_IDS=
MURMUR_THREAD_REGISTRY_PATH=/tmp/murmur-threads.json
MURMUR_THREAD_ALLOWLIST_PATH=/tmp/murmur-thread-allowlist.json
MURMUR_THREAD_FETCH_LIMIT=100
MURMUR_PERSIST_COOKIES_TO_DB=true
MURMUR_STATE_DATABASE_URL=
MURMUR_STATE_TABLE=murmur_runtime_state
MURMUR_COOKIE_STATE_ENCRYPT=true
MURMUR_COOKIE_STATE_SECRET=
MURMUR_LOAD_PROXY_STATE=true

FB_COOKIES_PATH=cookies.json
FB_COOKIES_JSON_B64=
FB_USER_AGENT=
FB_PROXY=
FB_UPLOAD_PROXY=
FB_MQTT_PROXY=
FB_HTTP_TIMEOUT_SECONDS=120
FB_MQTT_WATCHDOG_SECONDS=15
FB_UPLOAD_RETRIES=3
```

Use `ALLOWED_THREAD_IDS` or the admin console thread gate in production so Murmur only answers in threads you control.

The admin console can also save Facebook proxy URLs to runtime state. Keep the login/upload proxy stable, and use a separate realtime MQTT proxy when the Messenger listener is the failing part.

Messenger one-to-one user messages may be limited by end-to-end encryption. Group chats, room chats, and pages are usually better test targets for `fbchat-muqit`.

Logging out of Facebook can invalidate the cookies in `cookies.json` or `FB_COOKIES_JSON_B64`. If Murmur suddenly cannot log in after a logout, export a fresh cookie file.

## Admin Console

The all-in-one public proxy includes a small admin console at:

```text
/murmur-admin
```

It is protected with a signed-cookie login page. By default it uses:

```text
username: WEBUI_ADMIN_EMAIL
password: WEBUI_ADMIN_PASSWORD
```

You can override those credentials:

```env
MURMUR_ADMIN_CONSOLE=true
MURMUR_ADMIN_PATH=/murmur-admin
MURMUR_ADMIN_USERNAME=admin@example.com
MURMUR_ADMIN_PASSWORD=strong-admin-password
MURMUR_ADMIN_SESSION_SECONDS=86400
MURMUR_ADMIN_SESSION_SECRET=
MURMUR_ADMIN_COOKIE_SECURE=
MURMUR_ADMIN_BASIC_AUTH=false
```

The console has two runtime tools:

- Thread Access shows the Messenger threads Murmur has discovered and lets you choose exactly where the bot may answer.
- Facebook Cookies uploads a fresh Facebook cookie JSON file, writes it to `FB_COOKIES_PATH`, syncs the latest encrypted cookie state to PostgreSQL when configured, and restarts only the Murmur Messenger listener.
- Runtime Status shows listener paths and includes a manual Messenger listener restart button.

Thread access changes are stored in `MURMUR_THREAD_ALLOWLIST_PATH` and are picked up by the listener on the next message. The thread list is stored in `MURMUR_THREAD_REGISTRY_PATH`; it is filled from recent inbox refreshes and from threads that send messages while Murmur is online. Open WebUI does not restart for admin console changes.

For ephemeral hosts such as free Hugging Face Spaces, runtime files disappear on rebuild. To make admin cookie uploads survive rebuilds without changing Space secrets on every upload, configure PostgreSQL with `DATABASE_URL` or `MURMUR_STATE_DATABASE_URL`. The admin console writes encrypted cookie state to `MURMUR_STATE_TABLE`, and startup reads it before falling back to `FB_COOKIES_JSON_B64`.

## Hugging Face Spaces

The repository is ready for Docker Spaces:

```yaml
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
```

Recommended Space variables/secrets:

```env
WEBUI_SECRET_KEY=long-random-secret
WEBUI_ADMIN_EMAIL=admin@example.com
WEBUI_ADMIN_PASSWORD=strong-admin-password
ENABLE_SIGNUP=false
DEFAULT_USER_ROLE=pending

OPENWEBUI_MODEL=openrouter_1.openai/gpt-oss-20b:free
OPENWEBUI_MODEL_ALIASES=free=openrouter_1.openai/gpt-oss-20b:free

OPENWEBUI_PROVIDER_SYNC=true
OPENWEBUI_PROVIDER_FAMILIES=openrouter,cloudflare
OPENROUTER_API_KEY_1=sk-or-v1-...
CF_ACCOUNT_ID=your-cloudflare-account-id
CF_API_TOKEN=your-cloudflare-workers-ai-token

FB_COOKIES_JSON_B64=optional-bootstrap-base64-cookies-json
FB_USER_AGENT=browser-user-agent-used-for-cookie-export
MURMUR_PERSIST_COOKIES_TO_DB=true
MURMUR_STATE_TABLE=murmur_runtime_state
MURMUR_COOKIE_STATE_ENCRYPT=true
```

Optional persistence can be configured with PostgreSQL:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB
PGSSLMODE=require
VECTOR_DB=pgvector
PGVECTOR_DB_URL=postgresql://USER:PASSWORD@HOST/DB
PGVECTOR_CREATE_EXTENSION=false
```

If hosted Facebook login fails, test the same cookies locally first. If local login works, the hosted issue is likely network, IP, or proxy related. If local login fails too, rotate the cookies.

## Render

Use this repository as a Docker Web Service. The service starts a public proxy immediately, waits for Open WebUI health, syncs provider connections, then starts the Messenger listener.

Render environment variables follow the same shape as Hugging Face Spaces. If using Neon PostgreSQL, create the vector extension once:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

For Neon/Open WebUI v0.9, prefer `PGSSLMODE=require` as a separate variable. Do not add `?sslmode=require` or `&channel_binding=require` to the database URLs.

## Cookie Utilities

### Browser-assisted refresh

Murmur includes a local helper for refreshing Facebook cookies through a real browser session. It can type the configured email or phone and password, generate an authenticator code from a local TOTP secret, then waits while you finish any Facebook checkpoint in the opened browser.

Install the optional local dependency once:

```powershell
python -m pip install -e .[facebook-login]
python -m playwright install chromium
```

Set these in `.env`:

```env
FB_LOGIN_EMAIL=your-facebook-email
FB_LOGIN_PHONE=your-facebook-phone-or-email
FB_LOGIN_PASSWORD=your-facebook-password
FB_LOGIN_TOTP_SECRET=your-authenticator-secret
```

Run the refresh:

```powershell
python .\scripts\facebook_login_refresh.py
```

By default the helper opens Chromium with a persistent profile at `.murmur-facebook-profile`, prefers `FB_LOGIN_EMAIL` over `FB_LOGIN_PHONE`, exports fresh cookies to `FB_LOGIN_EXPORT_PATH` or `FB_COOKIES_PATH`, backs up the previous cookie file, and verifies the result with `fbchat-muqit`. It uses `FB_LOGIN_PROXY` when set, otherwise it falls back to `FB_PROXY`; set `FB_LOGIN_PROXY=direct` to force a direct browser login.

Generate a base64 cookie payload from `cookies.json`:

```powershell
.\scripts\cookies-b64.ps1
```

Copy it directly to the clipboard:

```powershell
.\scripts\cookies-b64.ps1 -Copy
```

CMD wrapper:

```cmd
.\scripts\cookies-b64.cmd
```

Direct PowerShell one-liner:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.json"))
```

## Configuration Reference

### Open WebUI

| Variable | Default | Description |
|---|---|---|
| `OPENWEBUI_BASE_URL` | bundled Open WebUI | External Open WebUI URL. Leave empty in all-in-one Docker. |
| `OPENWEBUI_API_KEY` | empty | Open WebUI API key. |
| `OPENWEBUI_LOGIN_EMAIL` | `WEBUI_ADMIN_EMAIL` | Login email for JWT auth fallback. |
| `OPENWEBUI_LOGIN_PASSWORD` | `WEBUI_ADMIN_PASSWORD` | Login password for JWT auth fallback. |
| `OPENWEBUI_MODEL` | required | Default chat model ID from Open WebUI. |
| `OPENWEBUI_MODEL_ALIASES` | `default=OPENWEBUI_MODEL` | Comma-separated `alias=model-id` values. |
| `OPENWEBUI_WARMUP` | `true` | Warm Open WebUI before Messenger starts. |
| `OPENWEBUI_WARMUP_CHAT` | `true` | Send a tiny startup chat request. |
| `OPENWEBUI_ACCESS_LOG` | `false` | Enable Uvicorn access logs for Open WebUI. |

### Bundled Open WebUI

| Variable | Default | Description |
|---|---|---|
| `WEBUI_SECRET_KEY` | required for hosted deployments | Open WebUI secret key. |
| `WEBUI_ADMIN_EMAIL` | empty | Admin account email used by Open WebUI and Murmur JWT fallback. |
| `WEBUI_ADMIN_PASSWORD` | empty | Admin account password used by Open WebUI and Murmur JWT fallback. |
| `ENABLE_SIGNUP` | `false` recommended | Open WebUI signup toggle. |
| `DEFAULT_USER_ROLE` | `pending` recommended | Default role for new Open WebUI users. |
| `ENABLE_OLLAMA_API` | `false` | Disable unused Ollama checks in this all-in-one deployment. |
| `ENABLE_BASE_MODELS_CACHE` | `true` | Cache Open WebUI base model list after startup. |
| `ENV` | `prod` | Open WebUI runtime environment. |
| `USER_AGENT` | `Murmur/0.1` | User-agent for outbound Open WebUI/provider requests. |
| `WEBUI_URL` | empty | Public Open WebUI URL for hosted deployments. |
| `CORS_ALLOW_ORIGIN` | empty | Allowed CORS origin for hosted deployments. |

### Providers

| Variable | Default | Description |
|---|---|---|
| `OPENWEBUI_PROVIDER_SYNC` | `true` | Sync provider keys into Open WebUI Connections. |
| `OPENWEBUI_PROVIDER_FAMILIES` | `openrouter` when OpenRouter keys exist | Comma-separated provider families. |
| `<PROVIDER>_API_BASE_URL` | provider-specific | OpenAI-compatible provider base URL. |
| `<PROVIDER>_API_KEY_1` ... `<PROVIDER>_API_KEY_20` | empty | Numbered provider keys. |
| `<PROVIDER>_MODEL_IDS` | empty | Optional explicit model allowlist. |
| `OPENROUTER_API_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter base URL. |
| `OPENROUTER_API_KEY_1` ... `OPENROUTER_API_KEY_5` | empty | OpenRouter key slots. |
| `CF_ACCOUNT_ID` | empty | Cloudflare account ID. |
| `CF_API_TOKEN` | empty | Cloudflare Workers AI token. |
| `CLOUDFLARE_API_BASE_URL` | derived from `CF_ACCOUNT_ID` | Cloudflare OpenAI-compatible chat base URL. |
| `CLOUDFLARE_MODEL_IDS` | empty | Explicit Cloudflare models to expose. |
| `CLOUDFLARE_HIDE_EXPERIMENTAL_MODELS` | `true` | Hide experimental models during Cloudflare metadata lookup. |

### Messenger

| Variable | Default | Description |
|---|---|---|
| `BOT_PREFIX` | `/ai` | Messenger command prefix. |
| `RESPOND_ONLY_ON_PREFIX` | `true` | Only respond to prefixed messages. |
| `RESPOND_TO_BOT_REPLIES` | `true` | Respond to replies on bot messages without prefix. |
| `ALLOWED_THREAD_IDS` | empty | Comma-separated Messenger thread allowlist. |
| `MURMUR_THREAD_REGISTRY_PATH` | `/tmp/murmur-threads.json` | Runtime registry of discovered Messenger threads for the admin console. |
| `MURMUR_THREAD_ALLOWLIST_PATH` | `/tmp/murmur-thread-allowlist.json` | Runtime thread allowlist written by the admin console. |
| `MURMUR_THREAD_FETCH_LIMIT` | `100` | Recent Messenger threads to fetch after login for the admin console registry. |
| `MURMUR_PERSIST_COOKIES_TO_DB` | `true` | Sync uploaded admin cookies to PostgreSQL runtime state when configured. |
| `MURMUR_STATE_DATABASE_URL` | `DATABASE_URL` | Optional separate PostgreSQL URL for Murmur runtime state. |
| `MURMUR_STATE_TABLE` | `murmur_runtime_state` | PostgreSQL table for Murmur runtime state. Created automatically. |
| `MURMUR_COOKIE_STATE_ENCRYPT` | `true` | Encrypt stored cookie JSON before writing it to PostgreSQL. |
| `MURMUR_COOKIE_STATE_SECRET` | `WEBUI_SECRET_KEY` | Optional separate encryption secret for cookie state. |
| `MURMUR_LOAD_PROXY_STATE` | `true` | Load admin-saved Facebook proxy runtime state from PostgreSQL. |
| `FB_COOKIES_PATH` | `cookies.json` | Path to Facebook cookies JSON. |
| `FB_COOKIES_JSON_B64` | empty | Optional bootstrap base64 cookies JSON for hosted deployments. |
| `FB_USER_AGENT` | library default | Browser user-agent paired with exported cookies. |
| `FB_PROXY` | empty | Proxy for Facebook HTTP/login requests. |
| `FB_UPLOAD_PROXY` | `FB_PROXY` | Proxy for Messenger attachment uploads. |
| `FB_MQTT_PROXY` | `FB_PROXY` | Proxy for Messenger realtime MQTT. |
| `FB_HTTP_TIMEOUT_SECONDS` | `120` | Facebook HTTP timeout. |
| `FB_MQTT_WATCHDOG_SECONDS` | `15` | Restart listener if MQTT stops silently. |
| `FB_UPLOAD_RETRIES` | `3` | Attachment upload retry count. |
| `FB_UPLOAD_ENDPOINTS` | Facebook and Messenger upload hosts | Upload endpoints tried in order. |
| `FB_LOG_NAMES` | `true` | Resolve Messenger IDs to names in logs. |
| `FB_LOG_NAMES_KEEP_IDS` | `true` | Keep IDs beside resolved names. |
| `FB_LOG_NAME_CACHE_PATH` | temp file | JSON cache for resolved Messenger names. |
| `FB_LOGIN_PHONE` | empty | Local browser-login helper phone/email. Not used by the hosted bot. |
| `FB_LOGIN_EMAIL` | empty | Local browser-login helper email. Preferred over `FB_LOGIN_PHONE`. |
| `FB_LOGIN_PASSWORD` | empty | Local browser-login helper password. Not used by the hosted bot. |
| `FB_LOGIN_TOTP_SECRET` | empty | Local browser-login helper authenticator secret used to generate 2FA codes. |
| `FB_LOGIN_URL` | `https://www.facebook.com/login` | Facebook login page opened by the helper. |
| `FB_LOGIN_PROFILE_DIR` | `.murmur-facebook-profile` | Persistent local Chromium profile used by the helper. |
| `FB_LOGIN_EXPORT_PATH` | `FB_COOKIES_PATH` | Cookie JSON path written by the helper. |
| `FB_LOGIN_HEADLESS` | `false` | Run helper browser headless. Keep `false` for checkpoints and 2FA. |
| `FB_LOGIN_TIMEOUT_SECONDS` | `300` | Time to wait for `c_user` and `xs` cookies after login starts. |
| `FB_LOGIN_VERIFY` | `true` | Verify the exported cookies with `fbchat-muqit`. |
| `FB_LOGIN_PERSIST_DB` | `false` | Also persist exported cookies to Murmur runtime PostgreSQL state. |
| `FB_LOGIN_BACKUP_EXISTING` | `true` | Back up the old cookie file before overwriting it. |
| `FB_LOGIN_PROXY` | `FB_PROXY` | Browser proxy for local Facebook login. Use `direct` to disable. |
| `FB_LOGIN_VERIFY_PROXY` | `FB_LOGIN_PROXY` | Proxy used by the `fbchat-muqit` verification request. |
| `FB_LOGIN_USER_AGENT` | `FB_USER_AGENT` | Browser and verification user-agent override. |

### Runtime

| Variable | Default | Description |
|---|---|---|
| `MAX_HISTORY_MESSAGES` | `12` | Short per-thread memory window. |
| `MAX_REPLY_CHARS` | `1800` | Split Messenger replies above this size. |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Open WebUI request timeout. |
| `SYSTEM_PROMPT` | helpful assistant prompt | System message sent to Open WebUI. |
| `MURMUR_RESTART_SECONDS` | `60` | Delay before restarting the Messenger listener. |
| `MURMUR_PID_FILE` | `/tmp/murmur.pid` | PID file used by the admin console to restart the listener. |
| `MURMUR_RESTART_NOW_FILE` | `/tmp/murmur-restart-now` | Marker file for fast admin-requested restarts. |

### Admin Console

| Variable | Default | Description |
|---|---|---|
| `MURMUR_ADMIN_CONSOLE` | `true` | Enable the admin console. |
| `MURMUR_ADMIN_PATH` | `/murmur-admin` | Admin console URL path. |
| `MURMUR_ADMIN_USERNAME` | `WEBUI_ADMIN_EMAIL` or `admin` | Admin login username. |
| `MURMUR_ADMIN_PASSWORD` | `WEBUI_ADMIN_PASSWORD` | Admin login password. Required when enabled. |
| `MURMUR_ADMIN_SESSION_SECONDS` | `86400` | Admin session lifetime. |
| `MURMUR_ADMIN_SESSION_SECRET` | `WEBUI_SECRET_KEY` | Optional separate secret for signed admin sessions. |
| `MURMUR_ADMIN_COOKIE_SECURE` | auto | Force secure admin session cookies on or off. |
| `MURMUR_ADMIN_BASIC_AUTH` | `false` | Optional legacy Basic Auth fallback. Keep disabled for the login page flow. |

### Images

| Variable | Default | Description |
|---|---|---|
| `IMAGE_PROXY_API_KEY` | `IMAGES_OPENAI_API_KEY` or `CF_API_TOKEN` | Bearer token Open WebUI uses for the local image adapter. |
| `IMAGE_PROXY_BASE_PATH` | `/murmur-image-openai/v1` | Local adapter path. |
| `ENABLE_IMAGE_GENERATION` | auto-enabled for Cloudflare adapter | Open WebUI image generation toggle. |
| `IMAGE_GENERATION_ENGINE` | `openai` | Open WebUI image engine. |
| `IMAGE_GENERATION_MODEL` | empty | Default image model. |
| `IMAGES_OPENAI_API_BASE_URL` | local adapter when enabled | OpenAI-compatible image API base URL used by Open WebUI. |
| `IMAGES_OPENAI_API_KEY` | `IMAGE_PROXY_API_KEY` | OpenAI-compatible image API key used by Open WebUI. |
| `IMAGE_SIZE` | `1024x1024` | Image size. |
| `IMAGE_STEPS` | `4` | Cloudflare image diffusion steps. |

### Persistence

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | Open WebUI default SQLite | PostgreSQL URL for Open WebUI persistence. |
| `PGSSLMODE` | empty | PostgreSQL SSL mode, for example `require`. |
| `VECTOR_DB` | Open WebUI default | Vector database backend, for example `pgvector`. |
| `PGVECTOR_DB_URL` | empty | PostgreSQL URL for pgvector storage. |
| `PGVECTOR_CREATE_EXTENSION` | `true` upstream default | Whether Open WebUI should create the pgvector extension. |

## Security Notes

- Never commit `.env`, `cookies.json`, API keys, or Facebook cookies.
- Use a dedicated Facebook account.
- Do not log out of the Facebook session that produced the cookies unless you are ready to export new cookies.
- Keep the admin console protected with a strong password, especially on public hosts.
- Keep `RESPOND_ONLY_ON_PREFIX=true` unless you want automatic replies.
- Set `ALLOWED_THREAD_IDS` for production.
- Disable Open WebUI signup on public deployments.
- Treat hosted Facebook failures as network/proxy issues only after local cookie login succeeds.

## Development

Run a syntax check:

```bash
python -m compileall murmur
```

Run Murmur locally:

```bash
python -m murmur
```

The project intentionally keeps most application logic in `murmur/app.py`, the public proxy and image adapter in `murmur/proxy.py`, startup orchestration in `scripts/start-all.sh`, and log cleanup in `murmur/log_filter.py`.

## License

This repository depends on upstream projects with their own licenses. Review the licenses for Open WebUI and fbchat-muqit before redistribution.
