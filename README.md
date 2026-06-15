---
title: murmur
emoji: 🗣️
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# murmur

Messenger AI bridge

## stack

- Go (wrapper around [mautrix-meta](https://github.com/mautrix/meta))
- config via env vars or YAML
- Scoop manifest for Windows install

## structure

```
cmd/murmur-bridge/    entrypoint (thin main)
internal/
  bridge/             wrapper around mautrix-meta
  config/             env/yaml config loader
  cookies/            cookie parsing (array or map format)
scoop/                scoop manifest
config.example.yaml   example config
```

## usage

```bash
# env vars
MURMUR_COOKIES=path/to/cookies.json murmur-bridge

# flags
murmur-bridge --cookies path/to/cookies.json --platform messenger

# config file
murmur-bridge --config config.yaml
```

## config

| env | flag | yaml | default |
|-----|------|------|---------|
| MURMUR_COOKIES | --cookies | cookies_path | ~/.config/murmur/cookies.json |
| MURMUR_PLATFORM | --platform | platform | messenger |
| MURMUR_LOG_LEVEL | | log_level | info |

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
