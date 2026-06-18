---
title: murmur
emoji: 🗣️
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# murmur

Messenger + WhatsApp AI bridge

## stack

- Go (wrapper around [mautrix-meta](https://github.com/mautrix/meta) + [wacli](https://github.com/openclaw/wacli))
- LiteLLM gateway as AI brain
- config via env vars
- Scoop manifest for Windows install

## structure

```
cmd/murmur-bridge/    entrypoint (thin main)
internal/
  ai/                 LiteLLM gateway client
  bnp/                BNP notification worker
  bridge/             core bridge logic (Messenger + WhatsApp)
  config/             env config loader
  cookies/            cookie parsing (array or map format)
  database/           Postgres persistence
  whatsapp/           wacli webhook + sender + sync manager
scoop/                scoop manifest
```

## usage

```bash
# env vars
MURMUR_COOKIES=path/to/cookies.json murmur-bridge

# flags
murmur-bridge --cookies path/to/cookies.json --platform messenger
```

## config

| env | default | description |
|-----|---------|-------------|
| MURMUR_COOKIES | ~/.config/murmur/cookies.json | Facebook cookies path |
| MURMUR_PLATFORM | messenger | messenger/facebook/messenger-lite |
| MURMUR_LOG_LEVEL | info | Log level |
| LITELLM_BASE | https://alchoholpad-litellm-huggingface-template.hf.space/v1 | LiteLLM gateway |
| DEFAULT_CHAT | openrouter/google/gemma-4-31b-it:free | Default chat model |
| DEFAULT_IMAGE | cloudflare/@cf/black-forest-labs/flux-1-schnell | Default image model |
| DATABASE_URL | | Postgres URL for persistence |
| CONTEXT_WINDOW | 100 | Conversation history window |
| WHATSAPP_ENABLED | 0 | Enable WhatsApp integration |
| WHATSAPP_BINARY | wacli | Path to wacli binary |
| WHATSAPP_STORE | ~/.wacli | wacli store directory |
| WHATSAPP_ACCOUNT | | wacli account name |
| WHATSAPP_WEBHOOK_SECRET | | HMAC secret for webhook verification |
| WHATSAPP_MAX_MESSAGES | | Max messages to store locally |
| WHATSAPP_DOWNLOAD_MEDIA | 0 | Download media during sync |

## install (scoop)

```powershell
scoop install murmur-bridge
```

## commands (stdin JSON)

```json
{"type":"send_message","id":"1","data":{"thread_id":123,"text":"hi"}}
{"type":"edit_message","id":"2","data":{"message_id":"mid.xxx","text":"edited"}}
{"type":"delete_message","id":"3","data":{"message_id":"mid.xxx"}}
{"type":"get_uid","id":"4"}
{"type":"stop"}
```

## events (stdout JSON)

```json
{"type":"ready","data":{"uid":123,"name":"User"}}
{"type":"mqtt_connected"}
{"type":"message","data":{"message_id":"mid.xxx","thread_id":123,"sender_id":456,"text":"hello","timestamp":1700000000}}
{"type":"sent","data":{"id":"1","otid":1700000000,"thread_id":123}}
{"type":"reconnected"}
{"type":"error","data":{"command":"send_message","error":"...","id":"1"}}
```
