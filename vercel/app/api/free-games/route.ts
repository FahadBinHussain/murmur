import { NextResponse } from "next/server";
import { inArray } from "drizzle-orm";
import { getDb } from "@/db/client";
import { gameSeen } from "@/db/schema";

export const runtime = "nodejs";
export const maxDuration = 60;

const FEED_URL = "https://feed.eikowagenknecht.com/lootscraper.xml";
const WEBHOOK_URL =
  process.env.MURMUR_WEBHOOK_URL ||
  "https://fahadbinhussain-murmur.hf.space/api/automation/notifications";
const HF_TOKEN = process.env.HF_TOKEN || "";

const BLOCKED_HOSTS = [
  "luna.amazon.com",
  "appraven.net",
  "fab.com",
];

type FeedItem = {
  guid: string;
  title: string;
  link: string;
  content: string;
  source: string;
};

export async function GET() {
  const threads = (process.env.GAMEBOT_THREAD_IDS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  let xml: string;
  try {
    const resp = await fetch(FEED_URL, { signal: AbortSignal.timeout(30_000) });
    if (!resp.ok) return NextResponse.json({ ok: false, error: `feed http ${resp.status}` }, { status: 502 });
    xml = await resp.text();
  } catch {
    return NextResponse.json({ ok: false, error: "feed fetch failed" }, { status: 502 });
  }

  const items: FeedItem[] = [];
  const entryRegex = /<entry>([\s\S]*?)<\/entry>/g;
  let match: RegExpExecArray | null;
  while ((match = entryRegex.exec(xml)) !== null) {
    const block = match[1];
    const getTag = (tag: string) => {
      const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`));
      return m ? m[1].trim() : "";
    };
    const getLink = () => {
      const m = block.match(/<link[^>]*href="([^"]*)"[^>]*>/);
      return m ? m[1] : "";
    };
    const getCategories = () => {
      const cats: string[] = [];
      const catRegex = /<category[^>]*term="([^"]*)"[^>]*>/g;
      let cm: RegExpExecArray | null;
      while ((cm = catRegex.exec(block)) !== null) cats.push(cm[1]);
      return cats;
    };

    const title = getTag("title");
    const guid = getTag("id") || getLink();
    const link = getLink();
    const content = stripHtml(getTag("content"));
    const categories = getCategories();

    const sourceCat = categories.find((c) => c.startsWith("source:"));
    const source = sourceCat ? sourceCat.replace("source:", "") : "";

    if (!guid || !title) continue;
    if (source.toLowerCase().includes("amazon")) continue;

    try {
      const host = link ? new URL(link).hostname.replace(/^www\./, "") : "";
      if (host && BLOCKED_HOSTS.some((b) => host === b || host.endsWith("." + b))) continue;
    } catch {
      // ignore malformed link, let it through
    }

    items.push({ guid, title, link, content, source });
  }

  if (items.length === 0) return NextResponse.json({ ok: true, checked: 0, sent: [] });

  const guids = items.map((i) => i.guid);
  let seen: Set<string>;
  try {
    const db = getDb();
    const rows = await db.select({ guid: gameSeen.guid }).from(gameSeen).where(inArray(gameSeen.guid, guids));
    seen = new Set(rows.map((r) => r.guid));
  } catch {
    return NextResponse.json({ ok: false, error: "db query failed" }, { status: 500 });
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (HF_TOKEN) headers["X-HF-Authorization"] = `Bearer ${HF_TOKEN}`;

  const sent: string[] = [];
  const failed: { guid: string; error: string }[] = [];

  for (const item of items) {
    if (seen.has(item.guid)) continue;

    const msg = item.content || "New free game available!";
    const sourceLabel = item.source ? `[${item.source}] ` : "";
    const linkLine = item.link ? `\n\n${item.link}` : "";

    let ok = true;
    for (const tid of threads) {
      try {
        const res = await fetch(WEBHOOK_URL, {
          method: "POST",
          headers,
          body: JSON.stringify({
            source: "gamebot",
            threadId: tid,
            title: `🎮 FREE: ${item.title}`,
            message: `${sourceLabel}${msg}${linkLine}`,
            url: item.link,
            dedupeKey: item.guid,
          }),
          signal: AbortSignal.timeout(10_000),
        });
        if (!res.ok) {
          ok = false;
          await new Promise((r) => setTimeout(r, 500));
          break;
        }
      } catch {
        ok = false;
        break;
      }
      await new Promise((r) => setTimeout(r, 500));
    }

    if (ok) {
      try {
        const db = getDb();
        await db.insert(gameSeen).values({ guid: item.guid, title: item.title }).onConflictDoNothing();
        sent.push(item.guid);
      } catch {
        failed.push({ guid: item.guid, error: "db insert failed" });
      }
    } else {
      failed.push({ guid: item.guid, error: "webhook failed" });
    }
  }

  return NextResponse.json({ ok: true, checked: items.length, sent, failed });
}

function stripHtml(html: string) {
  return html.replace(/<[^>]+>/g, "").trim();
}
