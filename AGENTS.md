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

### Messenger sends fail SILENTLY on stale cookies (diagnosed 2026-08-12)
- `POST /api/automation/notifications` always returns `{"status":"sent"}`
  regardless of actual delivery — `handleAutomationNotification` calls
  `SendMessage` and ignores the error. Messenger-side send failures (stale
  `c_user`/`xs` cookies on the space) log only inside the HF space container
  ("Failed to send AI reply"), which is NOT visible from outside.
- Result: free-games posts and steam patchnote announcements (GAMEBOT threads,
  STEAM thread 30738305889116993) silently vanish while every cron job, Vercel
  route, and space endpoint reports healthy.
- Verify delivery by ASKING the user (or checking the thread): the webhook 200
  proves nothing.
- Fix without a browser: the messenger cookies live in the fresh agent-browser
  lightweight cookie snapshot at
  `%APPDATA%\mainframe\accounts\agent-browser\cookies\<fb-email>.cookies.json`
  (array of `{name,value,...}` for `.messenger.com` domains — c_user/xs/datr/sb/
  wd). Convert to a plain `{name:value}` JSON object and `POST` it to
  `https://fahadbinhussain-murmur.hf.space/api/cookies/upload` with
  `Authorization: Bearer <HF_TOKEN>` (NOT X-HF-...) — response
  `{"status":"ok","message":"Cookies uploaded and bridge reloaded"}` means the
  bridge reconnected with the new cookies. Verify with a test notification
  post.
- `murmur-cookie-refresher.mjs` (rewired 2026-08-12) is now BROWSERLESS: it
  reads the fresh agent-browser lightweight cookie vault
  (`%APPDATA%\mainframe\accounts\agent-browser\cookies\<email>.cookies.json`),
  converts it to the plain `{name:value}` map (c_user/xs/datr/sb/wd), and POSTs
  to `/api/cookies/upload` — no Edge spawn, no CDP, no browserui. If the vault
  is missing or the trio is expired it fails loudly with the exact helper
  command to refresh it (`cookies run` then `cookies save -FromSession`).
  Env: `AGENT_BROWSER_EMAIL` (fallback legacy `MAINFRAME_BROWSERUI_EMAIL`).
  Verified live 2026-08-12: upload → `{"status":"ok","message":"Cookies
  uploaded and bridge reloaded"}`.
- Neon via wss (ProtonVPN gotcha, fixed 2026-08-12): psql's 5432 outbound to
  Neon is silently dropped while ProtonVPN is up (the `IDMWFP` WFP driver kills
  non-tunnel flows; TCP connects but the server never answers the SSLRequest,
  and adding /32 routes around the tunnel gets WSAEACCES 10013). `murmur.ps1`
  now tries `scripts/bnp-db.mjs` FIRST — the Neon serverless driver
  (`@neondatabase/serverless`, installed globally at the scoop nodejs-lts
  `node_modules`) over `wss://` port 443, which rides the VPN fine — and falls
  back to psql if it fails. Usage: `node bnp-db.mjs <count|reset>`, reads
  `BNP_DATABASE_URL` + `BNP_WINDOW_MINUTES` env (or `../.env`), exit 0 + plain
  result on success. Gotchas: tagged-template `${}` interpolation inside SQL
  string literals breaks (bind param mismatch) — use
  `sql\`${sql.unsafe(rawSql)}\``; `await sql.unsafe(q)` does NOT execute, it
  only builds a fragment; the global package's ESM wrapper exposes the driver
  under `module.exports.neon` (or `default.neon`). The first BNP failure was
  8/11 22:03:25, ~9 min after ProtonVPN service start 8/11 21:54:59 — the
  check had 0 successes out of 30 before the fix.
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

## GameBot workflows (GitHub Actions)

- `gamebot.yml` was DELETED 2026-08-11 — fully superseded by the real-time vercel/ poller (below), same RSS feed, same webhook/threads, dedupe now in Neon `game_seen`.
- `steam-updates.yml` was DELETED 2026-08-11 — fully superseded by the real-time vercel/ poller (below), which uses the same "Community Announcements" filter and the same webhook/threads.
- **subscriptions**: `scripts/gamebot/subscriptions.json` maps thread id -> list of sources (`gamebot`, `steam-updates`). each script loads its own subscribers via `scripts/gamebot/subscriptions.js` (env `MURMUR_THREAD_ID` overrides the file when set). to subscribe a thread to a source, add its id to that source's list; to unsubscribe, remove it.
- note: the vercel/ pollers read their thread lists from Vercel env instead of subscriptions.json; the file now only documents which thread wants what. actual values (2026-08-11): `STEAM_THREAD_IDS=30738305889116993` (only; thread 2637078310061988 was NOT subscribed to steam-updates — a PATCH went to both at first deploy, fixed same day), `GAMEBOT_THREAD_IDS=30738305889116993,953525124128433` (2637078310061988 dropped as 2nd free-game thread 2026-08-11; replaced by 953525124128433).
- `poll-rss.js` behavior notes (deleted file, for reference): skipped `amazon prime` source + blocked hosts (luna.amazon.com, appraven.net, fab.com). the "amazon prime" check never actually fired on modern feeds (categories are uppercase `AMAZON`) — the vercel port fixes this with `source.toLowerCase().includes("amazon")`.

## Steam Updates - real-time poller (vercel/)

### what it is
- `vercel/` is a small Next.js + TypeScript + drizzle app (inside the murmur repo) that polls Steam community-announcement news every minute and posts new ones to Murmur, replacing the 6h GH Actions `steam-updates.yml` (deleted 2026-08-11; git history keeps it as reference).
- route: `GET https://triton.vercel.app/api/steam-updates` — polls keyless `ISteamNews/GetNewsForApp/v2` per appid in `GAME_APPIDS`, filters feedlabel == "Community Announcements", dedupes against Neon table `steam_seen`, POSTs new ones to the Murmur HF Space webhook with `X-HF-Authorization: <HF_TOKEN>`.
- pre-seeded `steam_seen` with the 29 current announcements on first deploy so nothing re-sends.

## Free Games RSS - real-time poller (vercel/)

### what it is
- `vercel/` also hosts the free-games poller: route `GET https://triton.vercel.app/api/free-games` — fetches the lootscraper Atom feed (`feed.eikowagenknecht.com/lootscraper.xml`), parses `<entry>` blocks with regex, skips `amazon` sources (case-insensitive — old GH script compared against `"amazon prime"` which never matched the uppercase `AMAZON` category, so Prime games leaked through; fixed in the port) and blocked hosts (`luna.amazon.com`, `appraven.net`, `fab.com`), dedupes against Neon table `game_seen`, POSTs new ones to the Murmur webhook as source `gamebot`. replaces the 6h GH Actions `gamebot.yml` (deleted 2026-08-11).
- threads come from Vercel env `GAMEBOT_THREAD_IDS` (not subscriptions.json). **the space has its OWN send allowlist** - `MURMUR_ALLOWED_THREAD_IDS` variable on the space (`internal/config/config.go`, default `984803114200952,2637078310061988`; `threadAllowed()` blocks non-listed threads with a log-only warn and the API still returns `{"status":"sent"}`). adding a new thread to the Vercel env is NOT enough - you must also add it to the HF space variable and restart the space. free-games thread `953525124128433` was added to `GAMEBOT_THREAD_IDS` 2026-08-11 but NOT to the space allowlist until 2026-08-13 - all its alerts were silently blocked at SendMessage while `game_seen`/cron/Vercel all looked healthy.
- seeded 2026-08-11 with all 28 feed items current at migration time (mirroring the GH cache state: last GH run 13:02Z processed everything before it; the 3 Fab items + AppRaven-linked Apple items were host-blocked there too).

### accounts/ownership
- Vercel project `murmur`: account `fahadbix@gmail.com`, team `fahads-projects-4ed2eafb`, project id `prj_EWeinTGTbfW5iC2bciQ65ZuA4WyZ`.
- **production domain is `triton.vercel.app`** (attached 2026-08-11; Neptune's largest moon — picked after ~200 availability probes because every meaningful single-word vercel.app name is taken). `murmur.vercel.app` is a DIFFERENT user's domain — never use it (this cost a long debug session 2026-08-11; the deployments list/alias API reports the real project domain via `/v9/projects/murmur/domains`). `murmur-beryl-ten.vercel.app` still works as fallback alias.
- Neon project `murmur` (`aged-leaf-93258928`) on `fahad.bin.hussain001@gmail.com` (its 1st and only project per the 1-project rule). DATABASE_URL backed up at `%APPDATA%\mainframe\state\murmur-neon-database-url.txt`.
- cron-job.org job `8250790` (profile `fahadbinhussain001@gmail.com`), every 1 min, Asia/Dhaka, 60s timeout — see global AGENTS.md rule 11 inventory.
- cron-job.org job `8251034` (profile `fahadbinhussain001@gmail.com`) `murmur - free-games-rss`, every 1 min (Asia/Dhaka, 60s timeout; was every 6h until 2026-08-11) — see global AGENTS.md rule 11 inventory.

### env vars (Vercel project env)
`DATABASE_URL`, `HF_TOKEN`, `GAME_APPIDS` (format `appid:Display Name`), `STEAM_THREAD_IDS` (comma-separated), `GAMEBOT_THREAD_IDS` (comma-separated). set/update via Vercel REST API, not CLI (CLI is broken on this machine — pnpm shim issue).

### deploying via REST API (vercel CLI broken on this machine)
- no git link on the project; deployments are created via `POST /v13/deployments?teamId=<team>&forceNew=1` with inline base64 files. body: `{target, name, project, projectSettings:{framework:"nextjs"}, files:[{file, encoding:"base64", data}]}`. do NOT add a top-level `config` key — v13 rejects it with `bad_request` (the "config.builds required" note in earlier docs was wrong; the working deploy never used it).
- after the FIRST project creation, Vercel Authentication (SSO) defaults to `all_except_custom_domains` — vercel.app domains get a login wall that breaks cron-job.org. disable with `PATCH /v9/projects/murmur` body `{"ssoProtection":null}` (valid deploymentType values: `prod_deployment_urls_and_all_previews` | `all` | `preview` | null).
- old API-only deployments get purged by Vercel (deployment-specific URLs 404 later); the project domain always serves the latest production.
