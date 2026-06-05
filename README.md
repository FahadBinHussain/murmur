---
title: Murmur
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
---

# Murmur

Murmur is a Facebook Messenger bridge for LiteLLM, OpenWebUI, Lobe chat mirroring, and compatible model gateways, using [fbchat-muqit](https://github.com/togashigreat/fbchat-muqit) for Messenger transport.

It keeps Messenger handling, thread access, cookie refresh, and short per-thread memory inside Murmur. Model routing, provider keys, chat completion, and image generation stay in the external gateway.

```text
Facebook Messenger
  -> fbchat-muqit
  -> Murmur
  -> selected AI backend
  -> Murmur
  -> fbchat-muqit
  -> Facebook Messenger
```

## Project Boundary

Murmur is a bridge, not a second AI platform.

- Messenger transport belongs to `fbchat-muqit`.
- AI provider keys and model routing belong to the configured gateway.
- LiteLLM mode uses `/v1/chat/completions`, `/v1/images/generations`, and `/v1/models`.
- OpenWebUI mode uses `/api/chat/completions`, `/api/v1/images/generations`, `/api/models`, and `/openai/*` model endpoints.
- Lobe mirroring writes successful chat and image exchanges into Lobe's Postgres tables, without making Lobe the active model backend.
- Per-thread model choices are stored in Murmur memory while the worker is running.

## Features

- Messenger listener and sender through `fbchat-muqit`
- Chat and image generation through LiteLLM or OpenWebUI
- Optional Lobe mirror so Messenger chats and generated images appear in Lobe under readable topic names
- Symmetric per-thread chat and image model selection
- Short per-thread memory before sending chat completions
- Long Messenger replies split into deliverable chunks
- Optional allowed-thread allowlist and admin thread gate
- Optional Facebook HTTP, MQTT, and upload proxies
- Name-aware Messenger event logs
- Password-protected admin console for thread access and cookie upload
- Encrypted trusted-browser profile vault for hosted Facebook cookie refresh

## Command Reference

The canonical Messenger command prefix is `/ai`.

```text
Chat
/ai <message>
/ai model <number|model-id>

Image
/ai image <prompt>
/ai image models
/ai image model <number|model-id>

Models
/ai models
/ai models all
/ai models free
/ai providers
/ai status
```

`/help` and `/ai help` return the same short command reference inside Messenger.

Examples:

```text
/ai explain this in one sentence
/ai models
/ai models all
/ai model 12
/ai image a brutalist library in heavy rain
/ai image models
/ai image models all
/ai image model 44
/ai status
```

Model choices are remembered per Messenger thread while Murmur is online. Chat model selection and image model selection are separate. Use `/ai models` for a compact chat-model list, `/ai models all` for the full backend list, and `/ai models free` for all free chat models. Use `/ai image models` for a compact image-capable list, `/ai image models all` for the full image-capable list, and `/ai image models free` for all free image-capable models.

Responses include the selected provider and model:

```text
[LiteLLM gateway - deepseek/deepseek-chat-v3-0324:free]
```

If `MURMUR_AI_BACKEND=openwebui`, the header uses `Open WebUI` or the selected OpenWebUI connection/provider instead.

## Architecture

The hosted Docker deployment starts two processes:

- Murmur's public proxy on the public port
- Murmur's Messenger listener

The public proxy shows Murmur's status/admin surface at the root URL. The maintained Mermaid source for the current runtime flow is in [docs/current-flow.mmd](docs/current-flow.mmd).

```text
Browser / HF Space URL
  -> Murmur public proxy
  -> Murmur status/admin

Messenger event
  -> fbchat-muqit
  -> Murmur listener
  -> selected backend API
  -> fbchat-muqit
```

## Requirements

- Python 3.10+
- Facebook cookies usable by `fbchat-muqit`
- A LiteLLM or OpenWebUI URL
- A backend API key when the backend requires one
- At least one chat model configured in the selected backend

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

Minimal LiteLLM configuration:

```env
MURMUR_AI_BACKEND=litellm
LITELLM_BASE_URL=https://your-litellm.example.com/v1
LITELLM_API_KEY=your-gateway-key
LITELLM_MODEL=your-chat-model-id
IMAGE_GENERATION_MODEL=your-image-model-id
FB_COOKIES_PATH=cookies.json
```

`LITELLM_BASE_URL` may include or omit the trailing `/v1`; Murmur normalizes it.

Optional OpenWebUI configuration:

```env
MURMUR_AI_BACKEND=openwebui
OPENWEBUI_BASE_URL=https://your-openwebui.example.com
OPENWEBUI_API_KEY=your-openwebui-key
OPENWEBUI_MODEL=your-chat-model-id
IMAGE_GENERATION_MODEL=your-image-model-id
```

If `OPENWEBUI_API_KEY` is empty, Murmur can sign in with `OPENWEBUI_LOGIN_EMAIL` and `OPENWEBUI_LOGIN_PASSWORD`.

Optional Lobe mirror configuration:

```env
LOBE_SYNC_ENABLED=true
LOBE_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB
LOBE_SYNC_USER_EMAIL=you@example.com
LOBE_SYNC_TOPIC_PREFIX=Messenger
```

The Lobe user must already exist, usually by signing in to Lobe once. Murmur creates or reuses one `Murmur` Lobe agent/session, then mirrors each Messenger thread as a topic named like `Thread name | Messenger`.

Run Murmur:

```bash
python -m murmur
```

## Docker

Build the image:

```bash
docker build -t murmur .
```

Run it:

```bash
docker run --env-file .env -p 7860:7860 -v ./cookies.json:/app/murmur/cookies.json:ro murmur
```

Open the status/admin surface at `http://localhost:7860`.

## Backend Boundary

Murmur does not manage upstream AI provider keys. Configure OpenRouter, Gemini, Cloudflare, image providers, and other model backends in LiteLLM or OpenWebUI.

Murmur reads the selected backend's model list and sends chat/image requests back through the same backend. LiteLLM is the default backend; OpenWebUI remains supported through `MURMUR_AI_BACKEND=openwebui`.

Lobe mirroring is separate from the selected backend. Keep `MURMUR_AI_BACKEND=litellm` while setting `LOBE_SYNC_ENABLED=true` if LiteLLM should answer Messenger and Lobe should only show the resulting threads.

## Image Generation

Image generation uses the selected backend:

```text
Murmur
  -> LiteLLM /v1/images/generations
  or OpenWebUI /api/v1/images/generations
```

`/ai image models` filters backend model lists by image-generation metadata when available and known image model IDs as a fallback. The default list is compact; `/ai image models all` shows the full image-capable list. If the backend returns plain model IDs with no capability metadata, Murmur can still accept an exact image model ID through `/ai image model <model-id>`.

```env
IMAGE_GENERATION_MODEL=
IMAGE_SIZE=1024x1024
IMAGE_STEPS=4
```

The local Cloudflare image adapter in `murmur/proxy.py` is legacy and disabled by default. Leave it off when LiteLLM or OpenWebUI already owns image routing.

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
```

Murmur can run in all threads or only selected thread IDs. `ALLOWED_THREAD_IDS` is the startup allowlist; the admin console can manage the persisted allowlist while the app is running.

## Hosted State

For ephemeral hosts such as free Hugging Face Spaces, runtime files disappear on rebuild. To make admin cookie uploads and trusted browser profiles survive rebuilds, configure PostgreSQL with `MURMUR_STATE_DATABASE_URL`.

Murmur writes encrypted cookie state and an encrypted browser-profile vault to `MURMUR_STATE_TABLE`. Startup restores the profile, then reads cookie state before falling back to `FB_COOKIES_JSON_B64`.

```env
MURMUR_STATE_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB
MURMUR_STATE_TABLE=murmur_runtime_state
MURMUR_COOKIE_STATE_ENCRYPT=true
MURMUR_COOKIE_STATE_SECRET=long-random-secret
```

## Admin Console

The admin console is available at `/murmur-admin` by default.

```env
MURMUR_ADMIN_CONSOLE=true
MURMUR_ADMIN_PATH=/murmur-admin
MURMUR_ADMIN_USERNAME=admin
MURMUR_ADMIN_PASSWORD=strong-admin-password
MURMUR_ADMIN_SESSION_SECRET=another-long-random-secret
```

Use it to upload fresh Facebook cookies, restart the listener, and manage allowed Messenger threads.

## Deployment Notes

Hugging Face Spaces should use the Docker SDK. Required hosted variables are:

```env
LITELLM_BASE_URL=
LITELLM_API_KEY=
LITELLM_MODEL=
IMAGE_GENERATION_MODEL=
FB_COOKIES_JSON_B64=
MURMUR_COOKIE_STATE_SECRET=
MURMUR_ADMIN_USERNAME=
MURMUR_ADMIN_PASSWORD=
```

Add `MURMUR_STATE_DATABASE_URL` if cookie/profile state should survive rebuilds without replacing secrets.

## Environment Reference

### Gateway

| Variable | Default | Purpose |
| --- | --- | --- |
| `LITELLM_BASE_URL` | required | Gateway base URL. May include or omit `/v1`. |
| `LITELLM_API_BASE_URL` | empty | Alternate gateway base URL name. |
| `OPENAI_API_BASE_URL` | empty | Compatibility fallback for OpenAI-style deployments. |
| `LITELLM_API_KEY` | empty | Bearer token for the gateway. |
| `LITELLM_MODEL` | required | Default chat model ID. |
| `LITELLM_MODEL_ALIASES` | `default=LITELLM_MODEL` | Comma-separated `alias=model-id` values. |
| `LITELLM_PREFERRED_CHAT_MODELS` | empty | Optional fallback chat models after retryable upstream errors. |
| `LITELLM_WARMUP` | `true` | Warm the gateway before Messenger starts. |
| `LITELLM_WARMUP_CHAT` | `true` | Send a tiny startup chat request. |
| `IMAGE_GENERATION_MODEL` | empty | Default image model ID. |
| `IMAGE_SIZE` | empty | Optional image size, for example `1024x1024`. |
| `IMAGE_STEPS` | empty | Optional image step count for providers that support it. |

### Lobe Mirror

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOBE_SYNC_ENABLED` | `false` | Mirror successful chat and image replies into Lobe. |
| `LOBE_DATABASE_URL` | empty | Lobe Postgres database URL. |
| `LOBE_SYNC_USER_EMAIL` | empty | Lobe account email to attach mirrored topics to. |
| `LOBE_SYNC_USER_ID` | empty | Optional direct Lobe user id override. |
| `LOBE_SYNC_AGENT_TITLE` | `Murmur` | Lobe agent/session display title. |
| `LOBE_SYNC_AGENT_SLUG` | `murmur` | Stable Lobe agent slug. |
| `LOBE_SYNC_SESSION_TITLE` | `Murmur` | Stable Lobe session title. |
| `LOBE_SYNC_SESSION_SLUG` | `murmur` | Stable Lobe session slug. |
| `LOBE_SYNC_TOPIC_PREFIX` | `Messenger` | Suffix for topic names, for example `Cat fren | Messenger`. |

### Messenger

| Variable | Default | Purpose |
| --- | --- | --- |
| `FB_COOKIES_PATH` | `cookies.json` | Cookie file consumed by `fbchat-muqit`. |
| `FB_COOKIES_JSON_B64` | empty | Base64-encoded cookie JSON for hosted deployments. |
| `FB_USER_AGENT` | empty | Optional Facebook user-agent override. |
| `BOT_PREFIX` | `/ai` | Command prefix. |
| `MAX_HISTORY_MESSAGES` | `12` | Per-thread short memory size. |
| `MAX_REPLY_CHARS` | `1800` | Messenger reply chunk size. |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Gateway request timeout. |
| `SYSTEM_PROMPT` | helpful assistant prompt | System message sent to the gateway. |

### State And Admin

| Variable | Default | Purpose |
| --- | --- | --- |
| `MURMUR_STATE_DATABASE_URL` | empty | PostgreSQL URL for runtime state. |
| `MURMUR_STATE_TABLE` | `murmur_runtime_state` | Runtime state table name. |
| `MURMUR_COOKIE_STATE_SECRET` | empty | Encryption secret for cookies/profile state. |
| `MURMUR_ADMIN_CONSOLE` | `true` | Enable admin console. |
| `MURMUR_ADMIN_PATH` | `/murmur-admin` | Admin console path. |
| `MURMUR_ADMIN_USERNAME` | `admin` | Admin username. |
| `MURMUR_ADMIN_PASSWORD` | empty | Admin password. |
| `MURMUR_ADMIN_SESSION_SECRET` | empty | Optional separate signed-session secret. |

## Security Notes

- Keep the Space private if it has live Messenger cookies.
- Use a strong `MURMUR_COOKIE_STATE_SECRET`.
- Use a strong admin password.
- Do not paste cookies, API keys, or profile archives into public logs.

## Development Notes

The project keeps most application logic in `murmur/app.py`, the public proxy and optional local image adapter in `murmur/proxy.py`, startup orchestration in `scripts/start-hf.sh`, and log cleanup in `murmur/log_filter.py`.

## License

Review the licenses for `fbchat-muqit` and any gateway/provider you connect before redistribution.
