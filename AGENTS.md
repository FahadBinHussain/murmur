# Murmur — Agent Notes

## WhatsApp / wacli

### Account & Store
- Primary profile: `+8801911104251` (keyed by phone number, not email)
- Store path: `C:/Users/Admin/AppData/Roaming/mainframe/accounts/whatsapp/8801911104251/store`
- Binary: `C:\Users\Admin\go\bin\wacli.exe` (set via `WACLI_BIN` in `.env`)
- Use mainframe helper: `C:\Users\Admin\Downloads\mainframe\whatsapp-account.ps1`
  - `whatsapp-account.ps1 status-all` — check active profile
  - `whatsapp-account.ps1 use 8801911104251` — switch profile
  - `whatsapp-account.ps1 run [phone] [wacli args...]` — proxy to wacli

### Binary Version Gotcha (Windows)
- Official releases `<= v0.12.0` have a Windows SQLite file-URI bug:
  error `invalid uri authority: C:%5CUsers...` (backslash path encoding).
- Fix is in PR #304 (`goutamadwant/wacli` branch `fix-windows-sqlite-file-uri`).
  Build from that branch, or use a version `>= v0.12.1` when released.
- v0.11.0–v0.11.2 on a pre-v0.12 store may hit `no such module: fts5`.
  Fix: delete `wacli.db` (keep `session.db`) and let wacli rebuild the schema.

### Store Wipe / Re-pair Workflow
- If messages stop syncing or the store is corrupt:
  1. Stop `murmur` scheduled task: `schtasks /end /tn "murmur"`
  2. Kill all wacli + murmur-proxy node processes
  3. Delete `LOCK` and `.send.sock` from the store dir
  4. If `wacli.db` schema/migration is broken, delete `wacli.db` and `wacli.db-*` (keep `session.db`)
  5. If session is bad too, delete everything in the store dir and re-pair:
     `wacli auth --store <path> --qr-format terminal`
     (launch in a visible window so the QR can be scanned from the phone)
  6. After auth, bootstrap sync runs automatically and populates initial messages
  7. Run `wacli history backfill --store <path> --chat <jid> --count 500 --requests 10 --wait 90s` to pull older history
  8. Re-enable task: `schtasks /run /tn "murmur"`

### WhatsApp History Sync Limitation (IMPORTANT)
- WhatsApp multi-device protocol only keeps a limited buffer of messages on the
  primary phone for history sync to linked devices.
- Once a message is delivered live to a linked device (via `sync --follow`),
  WhatsApp removes it from the phone's history sync buffer.
- If the linked device was offline (wacli crashed, murmur stopped) for a period,
  messages from that window are **NOT re-fetchable via `history backfill`** —
  the phone's sync batch no longer has them.
- `history backfill` only goes **backward** from the oldest local message.
  It does NOT fill gaps between existing local messages.
- `sync --once` only fetches **new** messages since last sync — not historical.
- If a gap exists between local store messages, the only recovery options are:
  1. **Re-pair the linked device** (`wacli auth` after deleting the whole store).
     A fresh bootstrap sync pulls recent history from the phone, which may cover the gap.
  2. **Open the chat on the primary phone and scroll back** — this pushes those
     messages into the sync buffer, then `wacli history backfill` can fetch them.
  3. Check local backups (e.g. `riyad-7days.json`) in `$env:TEMP\opencode\` for
     messages extracted before the store was wiped.
- Backing up `wacli.db` before any destructive operation is strongly advised.

### Scheduled Task: murmur
- Runs `scripts\murmur.ps1` via Task Scheduler, trigger "At logon"
- Logon trigger has a 30-min repetition interval (PT30M / P9999D) with
  MultipleInstancesPolicy=IgnoreNew: the supervisor process can be killed on
  sleep/session transitions and the task would otherwise never re-fire until
  next logon (bug fixed 2026-08-09 — murmur and AgentsMdSync both died at
  sleep with LastTaskResult 0xFFFFFFFF). The repetition re-runs the task every
  30 min; IgnoreNew skips it while the supervisor is alive, so no duplicates.
- When murmur is broken, first kill any orphaned `node ... murmur-proxy.js`
  that still holds port 7870, else the new proxy can't bind on restart.
- murmur: wacli `sync --follow` + murmur-proxy node + reverse-poll for AI replies
- murmur script auto-restarts wacli if it exits (restart loop)
- Each wacli crash leaves `LOCK` and `.send.sock` in the store dir — pipeline
  cleans them on restart via `Kill-AllChildren`
- Singleton guard: `$env:TEMP\murmur.lock`
- Log: `$env:TEMP\murmur.log`
- Cookie health: pipeline queries `BnpMessengerNotification` outbox for FB send
  failures and runs `murmur-cookie-refresher.mjs` if threshold is hit
- Neon usage: pipeline runs mainframe `neon-hours-table.ps1 -Json` hourly and
  sends one Messenger warning per org/quota period at 90 of 100 CU-hours.
  State: `%APPDATA%\mainframe\state\murmur-neon-usage-warnings.json`.
  Overrides: `NEON_USAGE_CHECK_INTERVAL_SECONDS`, `NEON_USAGE_WARNING_HOURS`,
  `NEON_USAGE_WARNING_THREAD_ID`, `NEON_USAGE_TABLE_SCRIPT`, and
  `NEON_USAGE_WARNING_STATE_PATH`.
- Neon quota bug (fixed 2026-08-01): `neon-hours-table.ps1` previously used
  project-detail `compute_time_seconds`, which is LIFETIME-cumulative and never
  resets — so the murmur warning kept firing 93 CU-h used for Daily-BNP even
  after the period reset, while Neon UI showed ~0. The neon-hours script was
  rewritten to use the period-bounded `/organizations/{org_id}/consumption`
  endpoint, which matches Neon UI exactly. Quotas are now reported per-org
  (one row per org, `ProjectId` = org id, `Project` = comma-joined names); on
  this machine 13 of 14 Neon accounts have 1 project so per-org == per-project
  for them, and the 1 multi-project account (ahmedtouhid88) shows combined
  usage across its 3 projects. No local baseline state file is needed.
  Murmur dedup key also now uses canonically formatted `yyyy-MM-dd` UTC for
  both state compare and message (was previously comparing raw ISO against
  localized state, which mismatched and re-fired every hour).

### Useful wacli Commands
- `wacli sync --store <path> --once --idle-exit 60s` — quick sync, then exit
- `wacli sync --store <path> --follow --idle-exit 180s` — stay connected, catch new messages
- `wacli history backfill --store <path> --chat <jid> --count 500 --requests 10 --wait 90s --idle-exit 15s` — request older messages from the phone
- `wacli history coverage --store <path>` — show local archive coverage by chat
- `wacli messages list --store <path> --chat <jid> --limit 1000 --json` — list messages
- `wacli store stats --store <path>` — chat/message counts
- `wacli auth status --store <path>` — show authenticated JID
- `wacli doctor --store <path>` — show FTS5 / message / auth diagnostics
- `wacli send text --store <path> --to <jid> --message "..."` — send a message

### Riyad's Chat
- JID: `8801736454273@s.whatsapp.net`
- Name: "Riyad"
- Kind: dm

## dailyBNP Integration

### Source Link Import
- Endpoint: `POST https://dailybnp.com/api/internal/source-link-import`
- Auth: `Authorization: Bearer <SOURCE_IMPORT_TRIGGER_TOKEN>` (from Vercel env)
- Body: `{ "urls": ["..."], "refreshExisting": false }`
- Supported source domains (5 link importers): `bssnews.net`, `prothomalo.com`, `bonikbarta.com`, `banglastream.net`, `unb.com.bd`. (`bnpbd.org` is handled by its own importer, not this link importer.)
- BSS URLs require `/bangla/` in path: `https://www.bssnews.net/bangla/news-flash/<id>` (not `/news-flash/<id>`)
- banglastream.net is a Next.js App Router site; the article body + JSON-LD live inside `self.__next_f.push` RSC flight payload chunks, not inline `<script type="application/ld+json">` tags. The preparer extracts NewsArticle JSON-LD from those chunks and reads paragraphs from `<div class="block-full_richtext">`. og:image wraps the real image in an overlay URL — the `imageUrl` query param carries the underlying source image.
- unb.com.bd is an English-language wire with no JSON-LD; title from og:title, paragraphs from `<div class="news-article-text-block">`, image from `<img>` inside `<div class="image details">` (the og:image URL 404s on direct hits), and date parsed from the human string in `<li><span class="icon qb-clock"></span>...</li>` as Asia/Dhaka local time. Articles are stored with `language='en'` and title mirrored into `titleEn`.
- Local verification before deploying new sources: `pnpm exec tsx scripts/maintenance/dry-run-new-sources.ts [--apply] [--refresh]` (script committed with the importer code; uses prod DATABASE_URL from `.env.production`).

### Notification Trigger Gotcha
- The SQL trigger `queue_manual_article_publish_notification` on the `Article`
  table queues a `manual` published-phase notification for new articles.
- All supported source importers (bss/prothomalo/bonikbarta/banglastream/unb/bnpbd) queue their own
  detected + published notifications, so the trigger must skip those domains
  to avoid duplicate notifications.
- Trigger regex (line 24 of `install-article-publish-notification-trigger.sql`)
  must include ALL supported source domains: `(bnpbd\.org|bssnews\.net|prothomalo\.com|bonikbarta\.com|banglastream\.net|unb\.com\.bd)`.
- If a new source domain is added to the importers, update the trigger regex AND redeploy to Neon before re-importing, otherwise duplicate `manual:*` notifications will be inserted by the old trigger.

### Vercel Project
- Project: `dailybnp` (not `daily-bnp`) in team `daily-bnps-projects`
- Production URL: `dailybnp.com`
- Owning account: `dailybnp1978@gmail.com` (mainframe vercel profile)
- Env vars accessible via Vercel REST API: `GET /v9/projects/dailybnp/env?teamSlug=daily-bnps-projects`
