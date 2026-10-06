#!/usr/bin/env node
/**
 * scripts/murmur-cookie-refresher.mjs
 *
 * Facebook cookie refresh for murmur HF Space, read LIVE from the real Edge
 * session (browser-use skill, http://127.0.0.1:9222) - no cookie vault, no
 * separate automation profile.
 *
 * Workflow:
 * 1. Pipes python into the `browser-use` helper (the same invocation the
 *    browser-use skill uses) and calls CDP `Network.getCookies` for
 *    MURMUR_REFRESH_FB_URL (default https://www.messenger.com - the same page
 *    the original refresher navigated to before extracting).
 * 2. Converts the CDP cookie array to the plain {name:value} map the bridge
 *    expects (c_user / xs / datr / sb / wd).
 * 3. POSTs to murmur /api/cookies/upload with Authorization: Bearer <HF_TOKEN>
 *    and verifies the "Cookies uploaded and bridge reloaded" response.
 *
 * Fails loudly: if the browser-use call fails (Edge not running, skill not on
 * PATH) or the required trio (c_user/xs/datr) is missing or expired in the
 * live session, it throws the exact fix instead of uploading a broken set.
 *
 * Usage:
 *   node scripts/murmur-cookie-refresher.mjs
 *
 * Env (from .env in repo root):
 *   HF_EMAIL                    - mainframe HF profile email (used to locate token)
 *   MURMUR_HF_SPACE_URL         - murmur space URL
 *   MURMUR_REFRESH_FB_URL       - cookie scope URL (default: https://www.messenger.com)
 *   MURMUR_REFRESH_ALLOW_EXPIRED- "1" to upload even if the live trio is
 *                                 past its expiry (default: fail instead)
 */

import { spawn } from "node:child_process";
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
  fbUrl: process.env.MURMUR_REFRESH_FB_URL || "https://www.messenger.com",
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

/**
 * Pipe python into the `browser-use` CLI and return the JSON payload of the
 * single `BU::<json>` line it prints. Anything else (non-zero exit, no BU::
 * line, unparseable payload) is a hard error carrying the full output.
 */
function invokeBU(pythonCode) {
  return new Promise((resolve, reject) => {
    const child = spawn("browser-use", [], { windowsHide: true });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (e) =>
      reject(new Error(`could not start browser-use: ${e.message}`)),
    );
    child.on("close", (code) => {
      const line = stdout.split(/\r?\n/).find((l) => l.startsWith("BU::"));
      if (code !== 0 || !line) {
        reject(
          new Error(
            `browser-use exited ${code} without a BU:: payload:\n${(stdout + stderr).trim()}`,
          ),
        );
        return;
      }
      try {
        resolve(JSON.parse(line.slice(4)));
      } catch {
        reject(
          new Error(`browser-use returned an unparseable BU:: payload:\n${line}`),
        );
      }
    });
    child.stdin.write(pythonCode);
    child.stdin.end();
  });
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

function buildCookieReadPython(scopeUrl) {
  const keep = [...REQUIRED_COOKIES, ...NICE_TO_HAVE_COOKIES]
    .map((n) => JSON.stringify(n))
    .join(", ");
  return [
    "import json",
    `cookies = cdp("Network.getCookies", urls=[${JSON.stringify(scopeUrl)}])["cookies"]`,
    `keep = [c for c in cookies if c.get("name") in (${keep})]`,
    'print("BU::" + json.dumps(keep))',
    "",
  ].join("\n");
}

async function main() {
  const args = DEFAULTS;

  if (!args.murmurUrl) {
    throw new Error("MURMUR_HF_SPACE_URL not set in .env");
  }

  const scopeUrl = args.fbUrl;
  log("reading live cookies from the real Edge (browser-use skill), scope:", scopeUrl);

  let cookies;
  try {
    cookies = await invokeBU(buildCookieReadPython(scopeUrl));
  } catch (e) {
    throw new Error(
      `${e.message}\nhint: the browser-use skill drives the real Edge at http://127.0.0.1:9222 - open Edge (signed in at ${scopeUrl}) and re-run.`,
    );
  }
  if (!Array.isArray(cookies)) {
    throw new Error(
      `unexpected browser-use payload (expected a cookie array): ${JSON.stringify(cookies).slice(0, 200)}`,
    );
  }

  const liveCookies = cookies.filter(
    (c) => c && typeof c.name === "string" && typeof c.value === "string",
  );
  log("live cookies read:", liveCookies.length);

  const missing = REQUIRED_COOKIES.filter((k) => !liveCookies.some((c) => c.name === k));
  if (missing.length > 0) {
    throw new Error(
      `the live Edge session (scope ${scopeUrl}) is missing required cookies: ${missing.join(", ")}. ` +
        `Open ${scopeUrl} in the real Edge window, sign in to facebook/messenger there, then re-run.`,
    );
  }

  const expired = expiredRequiredCookies(liveCookies);
  if (expired.length > 0 && !args.allowExpired) {
    throw new Error(
      `live session cookies are expired: ${expired.map((c) => c.name).join(", ")}. ` +
        `Sign in again at ${scopeUrl} in the real Edge window, then re-run ` +
        `(set MURMUR_REFRESH_ALLOW_EXPIRED=1 to upload anyway).`,
    );
  }
  if (expired.length > 0) {
    log("WARNING: required cookies are past expiry but MURMUR_REFRESH_ALLOW_EXPIRED=1 - uploading anyway");
  }

  const cookieMap = toCookieMap(liveCookies);
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
