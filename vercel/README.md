# murmur steam-updates (Vercel)

Polls Steam dev announcements and notifies Messenger threads via the murmur HF Space
webhook. Triggered by cron-job.org every minute. Mirrors the dailybnp flow:
cron-job.org -> Vercel endpoint -> Neon dedupe -> murmur Space webhook -> FB send.

## env (set on Vercel, never in the repo)

- `DATABASE_URL` — Neon connection string (project `murmur`, account fahad.bin.hussain001@gmail.com)
- `HF_TOKEN` — HF token for the murmur Space webhook (X-HF-Authorization)
- `GAME_APPIDS` — default `3527290:PEAK` (comma-separated `appid:Display Name` pairs)
- `STEAM_THREAD_IDS` — default `30738305889116993,2637078310061988` (comma-separated)
- `MURMUR_WEBHOOK_URL` — default `https://fahadbinhussain-murmur.hf.space/api/automation/notifications`
- `MAX_AGE_DAYS` — default 30; skip announcements older than this (first-run flood guard)

## local

```sh
pnpm install
pnpm db:push     # create steam_seen table (needs DATABASE_URL env)
pnpm dev
```

## endpoint

- `GET /api/steam-updates` — fetch ISteamNews, filter "Community Announcements", dedupe via
  `steam_seen` table, POST new items to the murmur webhook (one per subscribed thread), returns
  `{ ok, checked, sent, failed }`.
