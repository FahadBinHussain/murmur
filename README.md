---
title: Murmur
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
---

# Murmur

Murmur is a Facebook Messenger AI bridge for Open WebUI.

The Docker image bundles Open WebUI and Murmur together, so one deployment can run the web UI, model router, database-backed chat platform, and Messenger bridge.

```text
Messenger thread -> Murmur -> bundled Open WebUI -> AI reply -> Messenger thread
```

## Status

Early project. The current version is intentionally small:

- Bundled Open WebUI runtime
- Messenger listener using `fbchat-muqit`
- Open WebUI `/api/chat/completions` backend
- Open WebUI `/api/v1/images/generations` image bridge
- Automatic Open WebUI JWT sign-in when no API key is provided
- Per-thread short conversation memory
- Optional `/ai` prefix trigger
- Optional allowed-thread allowlist
- Long-reply splitting
- Docker-ready runtime

## Important Messenger Limitation

`fbchat-muqit` is an unofficial Facebook Messenger API. Use a dedicated account and expect platform risk.

Messenger one-to-one user messages may be limited by end-to-end encryption. Murmur is best tested in group chats, room chats, or page contexts where `fbchat-muqit` can receive messages.

Never commit `cookies.json`, `.env`, API keys, or Facebook cookies.

If a hosted deployment cannot reach or authenticate with Facebook, always test Murmur locally with the same cookies before rotating them. If local login also fails, treat the cookie as expired or invalid. If local login works, treat the hosted deployment as a host/IP/networking problem.

## Requirements

- Python 3.10+
- An Open WebUI API key, or Open WebUI login credentials
- A Facebook cookies JSON file usable by `fbchat-muqit`

## Setup

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Copy the env template:

```bash
cp .env.example .env
```

Fill in:

```env
OPENWEBUI_BASE_URL=https://your-open-webui.example.com
OPENWEBUI_API_KEY=sk-your-open-webui-api-key
OPENWEBUI_MODEL=your-model-name
FB_COOKIES_PATH=cookies.json
```

Run:

```bash
python -m murmur
```

By default, Murmur only answers messages that start with:

```text
/ai
```

Example:

```text
/ai write a short reply to this conversation
```

Help and status:

```text
/help
/ai help
/ai status
```

`/help` and `/ai help` show the full Messenger command guide. `/ai status` shows the current Open WebUI text model for the thread.

Model switching is per Messenger thread:

```text
/ai models
/ai model free
/ai model 7
/ai @free summarize this in one sentence
/ai @7 summarize this in one sentence
```

`/ai models` fetches Open WebUI's model endpoint and lists free models when it can detect them. The numbered list is remembered per thread, so `/ai model 7` selects item 7 from the most recent `/ai models` response.

AI replies start with the provider and model that produced the answer, for example:

```text
[OpenWebUI - openrouter/free]
```

Configure short aliases with `OPENWEBUI_MODEL_ALIASES` for favorites:

```env
OPENWEBUI_MODEL=deepseek/deepseek-chat-v3-0324:free
OPENWEBUI_MODEL_ALIASES=free=deepseek/deepseek-chat-v3-0324:free,fast=google/gemini-2.0-flash-exp:free
```

Murmur is bridge-only for Messenger commands: text goes through Open WebUI chat completions, and image prompts go through Open WebUI image generation:

```text
/ai image a neon cyberpunk teashop in the rain
/ai see what is in this image?
```

Vision commands still reply with a bridge-only disabled message until they can be wired through Open WebUI. For Cloudflare Workers AI image models, the bundled public proxy exposes a local OpenAI-compatible image endpoint that Open WebUI can use as its image backend.

Current Open WebUI bridge surface:

- Text chat uses Open WebUI `/api/chat/completions`.
- Image generation uses Open WebUI `/api/v1/images/generations`.
- Auth uses `OPENWEBUI_API_KEY` or the configured WebUI admin email/password.
- Text model listing uses Open WebUI `/api/models` or `/api/v1/models`.
- Murmur keeps short per-thread memory and sends that context to Open WebUI.
- Cloudflare image generation is exposed to Open WebUI as an OpenAI-compatible local endpoint.
- Vision is disabled until Murmur can bridge it through Open WebUI.

## Docker: All In One

The default Dockerfile builds an all-in-one image from the official Open WebUI image and starts both Open WebUI and Murmur.

```bash
docker build -t murmur .
```

Run:

```bash
docker run --env-file .env -p 7860:7860 -v ./cookies.json:/app/murmur/cookies.json:ro murmur
```

Open WebUI is served on port `7860` by default for Hugging Face Spaces compatibility.

## Hugging Face Spaces

Create a new Hugging Face Space with:

```text
SDK: Docker
Hardware: CPU basic
```

This repository is already configured for Docker Spaces through the README front matter:

```yaml
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
```

Required Hugging Face Space secrets:

```env
WEBUI_SECRET_KEY=long-random-secret
WEBUI_ADMIN_EMAIL=your-admin-email@example.com
WEBUI_ADMIN_PASSWORD=strong-admin-password

OPENAI_API_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=your-openrouter-api-key
OPENWEBUI_MODEL=openrouter/free

FB_COOKIES_JSON_B64=base64-encoded-cookies-json

CF_ACCOUNT_ID=your-cloudflare-account-id
CF_API_TOKEN=your-cloudflare-workers-ai-token
CLOUDFLARE_IMAGE_MODEL=@cf/black-forest-labs/flux-1-schnell
IMAGE_PROXY_API_KEY=random-local-image-proxy-key
```

Optional but recommended:

```env
WEBUI_URL=https://YOUR_USERNAME-YOUR_SPACE_NAME.hf.space
CORS_ALLOW_ORIGIN=https://YOUR_USERNAME-YOUR_SPACE_NAME.hf.space
USER_AGENT=Murmur/0.1
ENABLE_SIGNUP=false
DEFAULT_USER_ROLE=pending
FB_USER_AGENT=the-browser-user-agent-that-exported-your-cookies
FB_MQTT_WATCHDOG_SECONDS=15
IMAGE_STEPS=4
```

Free Spaces have enough RAM for Open WebUI, but the disk is ephemeral and free CPU basic Spaces sleep after inactivity. For persistence without paid Hugging Face storage, an external database would be ideal; however, Hugging Face Spaces networking may block direct Postgres connections on port `5432`, so Neon may not work from a free Space. If Neon fails to connect, remove `DATABASE_URL`, `PGVECTOR_DB_URL`, `VECTOR_DB`, and `PGSSLMODE` and use the default local SQLite storage, understanding that state may be lost on restart.

## Render Free + Neon Free

Use this repo as a Render Docker Web Service. The service starts Open WebUI first, waits for `/health`, then starts Murmur.
Murmur also starts a small public proxy immediately so Render sees an open port while Open WebUI performs first-boot migrations.

Blueprint deploy URL:

```text
https://render.com/deploy?repo=https://github.com/FahadBinHussain/murmur
```

Required Render env vars:

```env
WEBUI_SECRET_KEY=long-random-secret
WEBUI_ADMIN_EMAIL=your-admin-email@example.com
WEBUI_ADMIN_PASSWORD=strong-admin-password

DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB
PGSSLMODE=require
VECTOR_DB=pgvector
PGVECTOR_DB_URL=postgresql://USER:PASSWORD@HOST/DB
PGVECTOR_CREATE_EXTENSION=false

OPENAI_API_BASE_URL=https://your-provider.example.com/v1
OPENAI_API_KEY=your-provider-api-key
OPENWEBUI_MODEL=your-model-name

FB_COOKIES_JSON_B64=base64-encoded-cookies-json
```

Run this once in Neon before deploying:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

For Neon/Open WebUI v0.9, do not put `?sslmode=require` or `&channel_binding=require` in `DATABASE_URL` / `PGVECTOR_DB_URL`. Open WebUI currently has mixed psycopg2 and asyncpg startup paths, and their SSL query string handling conflicts. Use `PGSSLMODE=require` as a separate environment variable instead.

Generate the cookie base64 from your local `cookies.json`:

```powershell
.\scripts\cookies-b64.ps1
```

Or use the CMD wrapper if PowerShell script execution is blocked:

```cmd
.\scripts\cookies-b64.cmd
```

To copy it straight to your clipboard:

```powershell
.\scripts\cookies-b64.ps1 -Copy
```

Direct PowerShell one-liner:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.json"))
```

Open WebUI still needs an AI provider key or an OpenAI-compatible provider with free quota. Murmur only bridges Messenger to Open WebUI; it cannot make paid model usage free.

## Configuration

| Variable | Required | Default | Description |
|---|---:|---|---|
| `OPENWEBUI_BASE_URL` | No | local bundled Open WebUI | External Open WebUI URL, without trailing slash |
| `OPENWEBUI_API_KEY` | No | | Open WebUI API key |
| `OPENWEBUI_LOGIN_EMAIL` | No | `WEBUI_ADMIN_EMAIL` | Login email for JWT auth |
| `OPENWEBUI_LOGIN_PASSWORD` | No | `WEBUI_ADMIN_PASSWORD` | Login password for JWT auth |
| `OPENWEBUI_MODEL` | Yes | | Model ID from Open WebUI |
| `OPENWEBUI_MODEL_ALIASES` | No | `default=OPENWEBUI_MODEL` | Comma-separated `alias=model-id` list for Messenger model switching |
| `OPENWEBUI_WARMUP` | No | `true` | Warm Open WebUI auth/model endpoints before Messenger starts listening |
| `OPENWEBUI_WARMUP_CHAT` | No | `true` | Send a tiny non-history chat completion at startup to reduce first real reply latency |
| `FB_COOKIES_PATH` | No | `cookies.json` | Path to Facebook cookies JSON |
| `FB_COOKIES_JSON_B64` | No | | Base64 cookies JSON, useful on Render |
| `FB_USER_AGENT` | No | library default | Browser user-agent to use with Facebook cookies |
| `FB_PROXY` | No | | HTTP/SOCKS proxy for Facebook requests |
| `FB_HTTP_TIMEOUT_SECONDS` | No | `120` | Timeout for Facebook web login/session extraction and attachment uploads |
| `FB_UPLOAD_PROXY` | No | | HTTP/SOCKS proxy used only for Messenger attachment upload POSTs |
| `FB_MQTT_PROXY` | No | `FB_PROXY` | HTTP/SOCKS proxy for Messenger MQTT websocket |
| `FB_MQTT_WATCHDOG_SECONDS` | No | `15` | Restart Murmur when the Messenger realtime listener stops silently |
| `FB_UPLOAD_RETRIES` | No | `3` | Retry Messenger attachment uploads when Facebook's upload host times out |
| `FB_UPLOAD_ENDPOINTS` | No | `upload.facebook.com`, `upload.messenger.com` | Comma-separated Messenger Web upload endpoints to try in order |
| `BOT_PREFIX` | No | `/ai` | Prefix that triggers Murmur |
| `RESPOND_ONLY_ON_PREFIX` | No | `true` | If false, replies to every allowed message |
| `RESPOND_TO_BOT_REPLIES` | No | `true` | If true, replies to direct replies on bot messages without requiring the prefix |
| `ALLOWED_THREAD_IDS` | No | | Comma-separated Messenger thread IDs |
| `MAX_HISTORY_MESSAGES` | No | `12` | Short in-memory context window per thread |
| `MAX_REPLY_CHARS` | No | `1800` | Split replies above this size |
| `REQUEST_TIMEOUT_SECONDS` | No | `120` | Open WebUI request timeout |
| `SYSTEM_PROMPT` | No | helpful assistant prompt | System prompt sent to Open WebUI |
| `ENABLE_OLLAMA_API` | No | `false` | Disable unused Ollama checks in this all-in-one deployment |
| `ENABLE_BASE_MODELS_CACHE` | No | `true` | Cache Open WebUI base model list after startup |
| `CF_ACCOUNT_ID` | No | | Cloudflare account ID for the optional image bridge |
| `CF_API_TOKEN` | No | | Cloudflare Workers AI token for the optional image bridge |
| `CLOUDFLARE_IMAGE_MODEL` | No | | Cloudflare text-to-image model, e.g. `@cf/black-forest-labs/flux-1-schnell` |
| `IMAGE_PROXY_API_KEY` | No | `IMAGES_OPENAI_API_KEY` or `CF_API_TOKEN` | Bearer token Open WebUI uses to call Murmur's local image proxy |
| `IMAGE_PROXY_BASE_PATH` | No | `/murmur-image-openai/v1` | Internal OpenAI-compatible image proxy path served by Murmur's public proxy |
| `ENABLE_IMAGE_GENERATION` | No | `true` when Cloudflare image vars exist | Open WebUI image generation toggle |
| `IMAGE_GENERATION_ENGINE` | No | `openai` when Cloudflare image vars exist | Open WebUI image engine |
| `IMAGE_PROVIDER_LABEL` | No | derived from model | Provider label shown in Messenger image captions |
| `IMAGE_GENERATION_MODEL` | No | `CLOUDFLARE_IMAGE_MODEL` | Image model sent through Open WebUI |
| `IMAGES_OPENAI_API_BASE_URL` | No | local Murmur image proxy | Open WebUI image API base URL |
| `IMAGES_OPENAI_API_KEY` | No | `IMAGE_PROXY_API_KEY` | Open WebUI image API bearer token |
| `IMAGE_SIZE` | No | `1024x1024` | Open WebUI image size value |
| `IMAGE_STEPS` | No | `4` | Cloudflare image diffusion steps, max `8` |

## Recommended Safety

After testing, set `ALLOWED_THREAD_IDS` so Murmur only works in threads you control.

Keep `RESPOND_ONLY_ON_PREFIX=true` unless you intentionally want Murmur to answer every message it can see.

For public deployments, disable signups and create an admin account through environment variables:

```env
WEBUI_ADMIN_EMAIL=admin@example.com
WEBUI_ADMIN_PASSWORD=strong-password
ENABLE_SIGNUP=false
DEFAULT_USER_ROLE=pending
```
