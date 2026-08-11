import { NextResponse } from "next/server";
import { inArray } from "drizzle-orm";
import { getDb } from "@/db/client";
import { steamSeen } from "@/db/schema";

export const runtime = "nodejs";
export const maxDuration = 10;

const WEBHOOK_URL =
  process.env.MURMUR_WEBHOOK_URL ||
  "https://fahadbinhussain-murmur.hf.space/api/automation/notifications";
const HF_TOKEN = process.env.HF_TOKEN || "";
const MAX_AGE_MS = Number(process.env.MAX_AGE_DAYS || 30) * 24 * 60 * 60 * 1000;

type NewsItem = {
  gid: string;
  title: string;
  feedlabel?: string;
  date?: number;
  contents?: string;
};

export async function GET() {
  const appSpecs = (process.env.GAME_APPIDS || "3527290:PEAK")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const threads = (process.env.STEAM_THREAD_IDS || "30738305889116993,2637078310061988")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const items: { gid: string; appid: string; gameName: string; title: string; date: number; contents: string }[] = [];

  for (const spec of appSpecs) {
    const [appid, ...nameParts] = spec.split(":");
    const gameName = nameParts.join(":").trim() || `app ${appid}`;
    try {
      const resp = await fetch(
        `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=${appid}&count=30&maxlength=800&format=json`,
        { signal: AbortSignal.timeout(10_000) }
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      const news = (data.appnews?.newsitems || []) as NewsItem[];
      for (const n of news) {
        if (n.feedlabel !== "Community Announcements") continue;
        const title = (n.title || "").trim();
        const gid = String(n.gid || "");
        if (!title || !gid) continue;
        const date = n.date ? n.date * 1000 : 0;
        if (date > 0 && Date.now() - date > MAX_AGE_MS) continue;
        items.push({ gid, appid, gameName, title, date, contents: stripHtml(n.contents || "").trim() });
      }
    } catch {
      continue;
    }
  }

  items.sort((a, b) => (a.date < b.date ? -1 : 1));
  if (items.length === 0) return NextResponse.json({ ok: true, checked: appSpecs.length, sent: 0 });

  const gids = items.map((i) => i.gid);
  let seen: Set<string>;
  try {
    const db = getDb();
    const rows = await db.select({ gid: steamSeen.gid }).from(steamSeen).where(inArray(steamSeen.gid, gids));
    seen = new Set(rows.map((r) => r.gid));
  } catch {
    return NextResponse.json({ ok: false, error: "db query failed" }, { status: 500 });
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (HF_TOKEN) headers["X-HF-Authorization"] = `Bearer ${HF_TOKEN}`;

  const sent: string[] = [];
  const failed: { gid: string; error: string }[] = [];

  for (const item of items) {
    if (seen.has(item.gid)) continue;

    const dateLine = item.date ? `\n\n(${new Date(item.date).toISOString().slice(0, 10)})` : "";
    const contentLine = item.contents ? `\n\n${item.contents.slice(0, 600)}` : "";
    const link = linkFor(item.appid, item.gid);
    const linkLine = `\n\n${link}`;

    let ok = true;
    for (const tid of threads) {
      try {
        const res = await fetch(WEBHOOK_URL, {
          method: "POST",
          headers,
          body: JSON.stringify({
            source: "steam-updates",
            threadId: tid,
            title: `🆕 ${item.gameName}: ${item.title}`,
            message: `${contentLine}${dateLine}${linkLine}`,
            url: link,
            dedupeKey: item.gid,
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
        await db.insert(steamSeen).values({
          gid: item.gid,
          gameName: item.gameName,
          title: item.title,
        }).onConflictDoNothing();
        sent.push(item.gid);
      } catch {
        failed.push({ gid: item.gid, error: "db insert failed" });
      }
    } else {
      failed.push({ gid: item.gid, error: "webhook failed" });
    }
  }

  return NextResponse.json({ ok: true, checked: appSpecs.length, sent, failed });
}

function linkFor(appid: string, gid: string) {
  return `https://steamcommunity.com/games/${appid}/announcements/detail/${gid}`;
}

function stripHtml(html: string) {
  return html.replace(/<[^>]+>/g, "").trim();
}
