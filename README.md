# Murmur

Murmur is a Facebook Messenger AI bridge for Open WebUI.

It listens for Messenger messages with `fbchat-muqit`, sends prompts to Open WebUI's OpenAI-compatible chat completions API, and replies back in the same Messenger thread.

```text
Messenger thread -> Murmur -> Open WebUI -> AI reply -> Messenger thread
```

## Status

Early project. The current version is intentionally small:

- Messenger listener using `fbchat-muqit`
- Open WebUI `/api/chat/completions` backend
- Per-thread short conversation memory
- Optional `/ai` prefix trigger
- Optional allowed-thread allowlist
- Long-reply splitting
- Docker-ready runtime

## Important Messenger Limitation

`fbchat-muqit` is an unofficial Facebook Messenger API. Use a dedicated account and expect platform risk.

Messenger one-to-one user messages may be limited by end-to-end encryption. Murmur is best tested in group chats, room chats, or page contexts where `fbchat-muqit` can receive messages.

Never commit `cookies.json`, `.env`, API keys, or Facebook cookies.

## Requirements

- Python 3.10+
- An Open WebUI instance
- An Open WebUI API key
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
OPENWEBUI_BASE_URL=https://your-open-webui.onrender.com
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

## Docker

Build:

```bash
docker build -t murmur .
```

Run:

```bash
docker run --env-file .env -v ./cookies.json:/app/cookies.json:ro murmur
```

## Configuration

| Variable | Required | Default | Description |
|---|---:|---|---|
| `OPENWEBUI_BASE_URL` | Yes | | Open WebUI URL, without trailing slash |
| `OPENWEBUI_API_KEY` | Yes | | Open WebUI API key |
| `OPENWEBUI_MODEL` | Yes | | Model ID from Open WebUI |
| `FB_COOKIES_PATH` | No | `cookies.json` | Path to Facebook cookies JSON |
| `BOT_PREFIX` | No | `/ai` | Prefix that triggers Murmur |
| `RESPOND_ONLY_ON_PREFIX` | No | `true` | If false, replies to every allowed message |
| `ALLOWED_THREAD_IDS` | No | | Comma-separated Messenger thread IDs |
| `MAX_HISTORY_MESSAGES` | No | `12` | Short in-memory context window per thread |
| `MAX_REPLY_CHARS` | No | `1800` | Split replies above this size |
| `REQUEST_TIMEOUT_SECONDS` | No | `120` | Open WebUI request timeout |
| `SYSTEM_PROMPT` | No | helpful assistant prompt | System prompt sent to Open WebUI |

## Recommended Safety

After testing, set `ALLOWED_THREAD_IDS` so Murmur only works in threads you control.

Keep `RESPOND_ONLY_ON_PREFIX=true` unless you intentionally want Murmur to answer every message it can see.
