#!/usr/bin/env node
/**
 * scripts/murmur-cookie-refresher.mjs
 *
 * Browserless facebook cookie refresh for murmur HF Space.
 *
 * Workflow:
 * 1. Reads the fresh agent-browser lightweight cookie snapshot for the FB
 *    account (saved by `agent-browser-account.ps1 cookies save <email>`
 *    from a live login) at:
 *      %APPDATA%\mainframe\accounts\agent-browser\cookies\<email>.cookies.json
 * 2. Converts the CDP cookie array to the plain {name:value} map the bridge
 *    expects (c_user / xs / datr / sb / wd).
 * 3. POSTs to murmur /api/cookies/upload with Authorization: Bearer <HF_TOKEN>
 *    and verifies the "Cookies uploaded and bridge reloaded" response.
 *
 * No browser is spawned: if the vault is missing or the required trio
 * (c_user/xs/datr) is expired, the refresher fails loudly with the exact
 * helper command needed to refresh the vault (one manual login).
 *
 * Usage:
 *   node scripts/murmur-cookie-refresher.mjs
 *
 * Env (from .env in repo root):
 *   AGENT_BROWSER_EMAIL         - FB account email for the cookie vault
 *                                 (fallback: MAINFRAME_BROWSERUI_EMAIL, kept
 *                                 for compat with older .env files)
 *   HF_EMAIL                    - mainframe HF profile email (used to locate token)
 *   MURMUR_HF_SPACE_URL         - murmur space URL
 *   MURMUR_REFRESH_ALLOW_EXPIRED- "1" to upload even if the vault trio is
 *                                 past its expiry (default: fail instead)
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

// Simple .env loader — no dependencies required
function loadEnv(path = join(process.cwd(), ".env")) {
  if (!existsSync(path)) return;
  const text = readFileSync(path, "utf-8");
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m && process.env[m[1]] === undefined) {
      process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
    }
  }
}

loadEnv();

const DEFAULTS = {
  email:
    process.env.AGENT_BROWSER_EMAIL ||
    process.env.MAINFRAME_BROWSERUI_EMAIL ||
    "",
  hfEmail: process.env.HF_EMAIL || "",
  murmurUrl: process.env.MURMUR_HF_SPACE_URL || "",
  allowExpired: process.env.MURMUR_REFRESH_ALLOW_EXPIRED === "1",
};

const REQUIRED_COOKIES = ["c_user", "xs", "datr"];
const NICE_TO_HAVE_COOKIES = ["sb", "wd"];

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function err(...args) {
  console.error(new Date().toISOString(), "[error]", ...args);
}

function getCookieVaultPath(email) {
  return join(
    homedir(),
    "AppData",
    "Roaming",
    "mainframe",
    "accounts",
    "agent-browser",
    "cookies",
    `${email}.cookies.json`,
  );
}

function resolveHfToken() {
  const email = DEFAULTS.hfEmail;
  if (!email) throw new Error("HF_EMAIL not set in .env");
  const tokenPath = join(
    homedir(),
    "AppData",
    "Roaming",
    "mainframe",
    "accounts",
    "hf",
    email,
    "token",
  );
  return readFileSync(tokenPath, "utf-8").trim();
}

async function postJson(url, body, headers = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, text };
}

function loadVaultCookies(vaultPath) {
  const raw = JSON.parse(readFileSync(vaultPath, "utf-8"));
  const arr = Array.isArray(raw) ? raw : [];
  return arr.filter((c) => c && typeof c.name === "string" && typeof c.value === "string");
}

function toCookieMap(cookies) {
  const map = {};
  for (const name of [...REQUIRED_COOKIES, ...NICE_TO_HAVE_COOKIES]) {
    const c = cookies.find((x) => x.name === name);
    if (c) map[c.name] = c.value;
  }
  return map;
}

function expiredRequiredCookies(cookies, nowSec = Math.floor(Date.now() / 1000)) {
  return cookies.filter(
    (c) =>
      REQUIRED_COOKIES.includes(c.name) &&
      Number(c.expires) > 0 &&
      Number(c.expires) <= nowSec,
  );
}

async function main() {
  const args = DEFAULTS;

  if (!args.email) {
    throw new Error("AGENT_BROWSER_EMAIL (or MAINFRAME_BROWSERUI_EMAIL) not set in .env");
  }
  if (!args.murmurUrl) {
    throw new Error("MURMUR_HF_SPACE_URL not set in .env");
  }

  const vaultPath = getCookieVaultPath(args.email);
  if (!existsSync(vaultPath)) {
    throw new Error(
      `no agent-browser cookie vault for ${args.email}: ${vaultPath}. ` +
        `Log into messenger once in an agent-browser window, then run: ` +
        `agent-browser-account.ps1 cookies save ${args.email} (or with -FromSession after a login)`,
    );
  }
  log("cookie vault:", vaultPath);

  const cookies = loadVaultCookies(vaultPath);
  log("vault cookies:", cookies.length);

  const missing = REQUIRED_COOKIES.filter((k) => !cookies.some((c) => c.name === k));
  if (missing.length > 0) {
    throw new Error(
      `vault is missing required cookies: ${missing.join(", ")}. ` +
        `Re-save the vault after a fresh login: agent-browser-account.ps1 cookies run ${args.email}`,
    );
  }

  const expired = expiredRequiredCookies(cookies);
  if (expired.length > 0 && !args.allowExpired) {
    throw new Error(
      `vault required cookies are expired: ${expired.map((c) => c.name).join(", ")}. ` +
        `Re-login in an agent-browser window and re-save: ` +
        `agent-browser-account.ps1 cookies run ${args.email}  (then cookies save ${args.email} -FromSession)`,
    );
  }
  if (expired.length > 0) {
    log("WARNING: required cookies are past expiry but MURMUR_REFRESH_ALLOW_EXPIRED=1 - uploading anyway");
  }

  const cookieMap = toCookieMap(cookies);
  log(
    "cookie map:",
    Object.keys(cookieMap)
      .map((k) => `${k}=${cookieMap[k].slice(-4)}`)
      .join(", "),
  );

  const hfToken = resolveHfToken();
  if (!hfToken) {
    throw new Error("HF token is empty");
  }
  log("hf token loaded (...", hfToken.slice(-4), ")");

  const url = `${args.murmurUrl.replace(/\/$/, "")}/api/cookies/upload`;
  log("uploading cookies to", url);
  const { status, text } = await postJson(url, cookieMap, {
    Authorization: `Bearer ${hfToken}`,
  });
  if (status >= 400) {
    throw new Error(`Murmur upload failed: HTTP ${status} ${text}`);
  }
  log("murmur upload response:", text);
  if (!/Cookies uploaded and bridge reloaded/i.test(text)) {
    throw new Error(`unexpected murmur upload response: HTTP ${status} ${text}`);
  }
  log("done -- murmur cookies refreshed");

  try {
    const healthRes = await fetch(`${args.murmurUrl.replace(/\/$/, "")}/api/health`);
    if (healthRes.ok) {
      log("murmur health:", await healthRes.text());
    }
  } catch {}
}

main().catch((e) => {
  err(e.message || e);
  process.exit(1);
});