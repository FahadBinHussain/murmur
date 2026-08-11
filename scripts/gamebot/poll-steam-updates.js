// Polls Steam dev announcements (ISteamNews/GetNewsForApp/v2, keyless) for
// configured appids and notifies Murmur via webhook on new posts.
// Mirrors poll-rss.js: seen-dedupe via SEEN_FILE (GH Actions cache), same
// webhook payload shape (source/threadId/title/message/url/dedupeKey).
//
// env:
//   MURMUR_WEBHOOK_URL  (required)  murmur notifications endpoint
//   MURMUR_THREAD_ID    (required)  comma-separated thread ids
//   HF_TOKEN            (optional)  bearer auth for the webhook
//   GAME_APPIDS         (required)  comma-separated "appid:Display Name" pairs
//   SEEN_FILE           (optional)  default "seen-steam-updates.json"
//   MAX_AGE_DAYS        (optional)  skip announcements older than this (default 30) —
//                                   guards the first run against flooding old posts

const { loadThreads } = require("./subscriptions");

async function main() {
  const webhookUrl = process.env.MURMUR_WEBHOOK_URL;
  if (!webhookUrl) {
    console.error("MURMUR_WEBHOOK_URL not set");
    process.exit(1);
  }

  const threadIds = loadThreads("steam-updates");
  if (threadIds.length === 0) {
    console.error("no threads subscribed to steam-updates");
    process.exit(1);
  }

  const appids = (process.env.GAME_APPIDS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (appids.length === 0) {
    console.error("GAME_APPIDS not set (comma-separated appid:name pairs)");
    process.exit(1);
  }

  const hfToken = process.env.HF_TOKEN;
  const headers = { "Content-Type": "application/json" };
  if (hfToken) {
    headers["Authorization"] = `Bearer ${hfToken}`;
  }

  const seenFile = process.env.SEEN_FILE || "seen-steam-updates.json";
  const maxAgeDays = Number(process.env.MAX_AGE_DAYS || 30);
  const maxAgeMs = maxAgeDays * 24 * 60 * 60 * 1000;
  let seen = {};
  try {
    seen = JSON.parse(require("fs").readFileSync(seenFile, "utf8"));
  } catch {
    // first run
  }

  const items = [];
  for (const spec of appids) {
    const [appid, ...nameParts] = spec.split(":");
    const gameName = nameParts.join(":").trim() || `app ${appid}`;
    const url = `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=${appid}&count=30&maxlength=800&format=json`;
    console.log(`Fetching news for ${gameName} (${appid})...`);
    const resp = await fetch(url);
    if (!resp.ok) {
      console.error(`Failed to fetch news for ${appid}: ${resp.status}`);
      continue;
    }
    const data = await resp.json();
    const newsitems = (data.appnews && data.appnews.newsitems) || [];
    for (const n of newsitems) {
      if (n.feedlabel !== "Community Announcements") continue;
      const title = (n.title || "").trim();
      if (!title || !n.gid) continue;
      const date = n.date ? n.date * 1000 : 0;
      if (maxAgeMs > 0 && date > 0 && Date.now() - date > maxAgeMs) {
        seen[String(n.gid)] = Date.now();
        continue;
      }
      items.push({
        gid: String(n.gid),
        gameName,
        title,
        date: date ? new Date(date).toISOString() : "",
        contents: stripHtml(n.contents || "").trim(),
        url: `https://steamcommunity.com/games/${appid}/announcements/detail/${n.gid}`,
      });
    }
  }

  console.log(`Found ${items.length} community announcements`);
  items.sort((a, b) => (a.date < b.date ? -1 : 1));

  let newCount = 0;
  for (const item of items) {
    if (seen[item.gid]) continue;

    console.log(`New: [${item.gameName}] ${item.title}`);

    const dateLine = item.date ? `\n\n(${item.date.slice(0, 10)})` : "";
    const contentLine = item.contents ? `\n\n${item.contents.slice(0, 600)}` : "";
    const linkLine = `\n\n${item.url}`;

    let sentToAll = true;
    for (const tid of threadIds) {
      const payload = {
        source: "steam-updates",
        threadId: tid,
        title: `🆕 ${item.gameName}: ${item.title}`,
        message: `${contentLine}${dateLine}${linkLine}`,
        url: item.url,
        dedupeKey: item.gid,
      };

      try {
        const res = await fetch(webhookUrl, {
          method: "POST",
          headers,
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          console.log(`Sent to ${tid}: ${item.title}`);
        } else {
          sentToAll = false;
          console.error(`Failed to send to ${tid} (${res.status}): ${await res.text()}`);
        }
      } catch (err) {
        sentToAll = false;
        console.error(`Network error for ${tid}: ${err.message}`);
      }

      await new Promise((r) => setTimeout(r, 500));
    }

    if (sentToAll) {
      seen[item.gid] = Date.now();
      newCount++;
    }
  }

  // trim seen list to last 500
  const gids = Object.keys(seen).sort((a, b) => seen[b] - seen[a]);
  if (gids.length > 500) {
    for (const g of gids.slice(500)) delete seen[g];
  }
  require("fs").writeFileSync(seenFile, JSON.stringify(seen, null, 2));

  console.log(`Done. ${newCount} new notification(s) sent.`);
}

function stripHtml(html) {
  return html.replace(/<[^>]+>/g, "").trim();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
