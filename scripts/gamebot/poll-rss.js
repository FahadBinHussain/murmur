const FEED_URL = "https://feed.eikowagenknecht.com/lootscraper.xml";

async function main() {
  const webhookUrl = process.env.MURMUR_WEBHOOK_URL;
  if (!webhookUrl) {
    console.error("MURMUR_WEBHOOK_URL not set");
    process.exit(1);
  }

  const hfToken = process.env.HF_TOKEN;
  const headers = { "Content-Type": "application/json" };
  if (hfToken) {
    headers["Authorization"] = `Bearer ${hfToken}`;
  }

  const seenFile = process.env.SEEN_FILE || "seen-games.json";
  let seen = {};
  try {
    seen = JSON.parse(require("fs").readFileSync(seenFile, "utf8"));
  } catch {
    // first run
  }

  console.log("Fetching Atom feed...");
  const resp = await fetch(FEED_URL);
  if (!resp.ok) {
    console.error("Failed to fetch feed:", resp.status);
    process.exit(1);
  }
  const xml = await resp.text();

  // Atom feed parser — extract <entry> blocks
  const items = [];
  const entryRegex = /<entry>([\s\S]*?)<\/entry>/g;
  let match;
  while ((match = entryRegex.exec(xml)) !== null) {
    const block = match[1];
    const getTag = (tag) => {
      const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`));
      return m ? m[1].trim() : "";
    };
    const getLink = () => {
      const m = block.match(/<link[^>]*href="([^"]*)"[^>]*>/);
      return m ? m[1] : "";
    };
    const getCategories = () => {
      const cats = [];
      const catRegex = /<category[^>]*term="([^"]*)"[^>]*>/g;
      let cm;
      while ((cm = catRegex.exec(block)) !== null) cats.push(cm[1]);
      return cats;
    };

    const title = getTag("title");
    const guid = getTag("id") || getLink();
    const link = getLink();
    const content = stripHtml(getTag("content"));
    const categories = getCategories();

    // build a clean source label from categories
    const sourceCat = categories.find((c) => c.startsWith("source:"));
    const source = sourceCat ? sourceCat.replace("source:", "") : "";

    if (source === "amazon prime") continue;
    items.push({ title, guid, link, content, source });
  }

  console.log(`Found ${items.length} entries in feed`);

  let newCount = 0;
  for (const item of items) {
    if (!item.guid || seen[item.guid]) continue;

    console.log(`New: ${item.title}`);

    const msg = item.content || "New free game available!";
    const sourceLabel = item.source ? `[${item.source}] ` : "";
    const linkLine = item.link ? `\n\n${item.link}` : "";

    const payload = {
      source: "gamebot",
      threadId: "30738305889116993",
      title: `🎮 FREE: ${item.title}`,
      message: `${sourceLabel}${msg}${linkLine}`,
      url: item.link,
      dedupeKey: item.guid,
    };

    try {
      const res = await fetch(webhookUrl, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        console.log(`Sent: ${item.title}`);
        seen[item.guid] = Date.now();
        newCount++;
      } else {
        console.error(`Failed to send (${res.status}): ${await res.text()}`);
      }
    } catch (err) {
      console.error(`Network error: ${err.message}`);
    }

    await new Promise((r) => setTimeout(r, 500));
  }

  // trim seen list to last 500
  const guids = Object.keys(seen).sort((a, b) => seen[b] - seen[a]);
  if (guids.length > 500) {
    for (const g of guids.slice(500)) delete seen[g];
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