#!/usr/bin/env node
/**
 * scripts/murmur-cookie-refresher.mjs
 *
 * Headed Facebook cookie refresh for murmur HF Space.
 *
 * Workflow:
 * 1. Launches Edge visibly with the saved mainframe browserui profile
 * 2. Navigates to Facebook messenger.com
 * 3. Looks for "Continue as NAME" button and clicks it
 * 4. Extracts c_user / xs / datr cookies
 * 5. POSTs to murmur /api/cookies/upload
 *
 * Usage:
 *   node scripts/murmur-cookie-refresher.mjs
 *
 * Env (from .env in repo root):
 *   HF_EMAIL                    - mainframe HF profile email (used to locate token)
 *   MURMUR_HF_SPACE_URL         - murmur space URL
 *   MAINFRAME_BROWSERUI_EMAIL   - browserui profile email
 *   MURMUR_REFRESH_PORT         - remote debugging port (default: 9377)
 *   MURMUR_REFRESH_FB_URL       - facebook url (default: https://www.messenger.com)
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
  email: process.env.MAINFRAME_BROWSERUI_EMAIL || "",
  hfEmail: process.env.HF_EMAIL || "",
  murmurUrl: process.env.MURMUR_HF_SPACE_URL || "",
  facebookUrl: process.env.MURMUR_REFRESH_FB_URL || "https://www.messenger.com",
  port: Number(process.env.MURMUR_REFRESH_PORT || 9377),
  connectMs: Number(process.env.MURMUR_REFRESH_CONNECT_MS || 8000),
  clickWaitMs: Number(process.env.MURMUR_REFRESH_CLICK_WAIT_MS || 10000),
  postClickWaitMs: Number(process.env.MURMUR_REFRESH_POST_CLICK_MS || 5000),
};

const REQUIRED_COOKIES = ["c_user", "xs", "datr"];

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function err(...args) {
  console.error(new Date().toISOString(), "[error]", ...args);
}

function getBrowserUiProfileDir(email) {
  return join(
    homedir(),
    "AppData",
    "Roaming",
    "mainframe",
    "accounts",
    "browserui",
    email,
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

function findBrowserPath() {
  const candidates = [
    join(process.env.LOCALAPPDATA || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    join(process.env.ProgramFiles || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    join(process.env.ProgramFilesX86 || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe"),
    join(process.env.ProgramFiles || "", "Google", "Chrome", "Application", "chrome.exe"),
    join(process.env.ProgramFilesX86 || "", "Google", "Chrome", "Application", "chrome.exe"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  throw new Error("No Chromium-compatible browser found. Install Chrome or Edge.");
}

function delay(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  return res.json();
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

async function connectCdp(port, connectMs) {
  const deadline = Date.now() + connectMs;
  while (Date.now() < deadline) {
    try {
      const targets = await fetchJson(`http://127.0.0.1:${port}/json`);
      const page = targets.find((t) => t.type === "page");
      if (page) {
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => {
          ws.addEventListener("open", () => resolve(ws), { once: true });
          ws.addEventListener("error", (e) => reject(e), { once: true });
          setTimeout(() => reject(new Error("WebSocket connect timeout")), 5000);
        });
        return { ws, page };
      }
    } catch {}
    await delay(500);
  }
  throw new Error("Could not connect to browser CDP");
}

function sendCdp(ws, id, method, params = {}) {
  return new Promise((resolve, reject) => {
    let resolved = false;
    const onMessage = (event) => {
      try {
        const data = event.data || event;
        const msg = JSON.parse(data.toString ? data.toString() : data);
        if (msg.id === id) {
          if (!resolved) {
            resolved = true;
            ws.removeEventListener("message", onMessage);
            resolve(msg);
          }
        }
      } catch {}
    };
    ws.addEventListener("message", onMessage);
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        ws.removeEventListener("message", onMessage);
        reject(new Error(`CDP timeout: ${method}`));
      }
    }, 15000);
  });
}

async function getCookies(ws) {
  const res = await sendCdp(ws, 1, "Network.getAllCookies");
  return res.result && res.result.cookies ? res.result.cookies : [];
}

async function navigate(ws, url) {
  await sendCdp(ws, 2, "Network.enable");
  await sendCdp(ws, 3, "Page.navigate", { url });
  await new Promise((resolve) => {
    const onMsg = (event) => {
      try {
        const data = event.data || event;
        const msg = JSON.parse(data.toString ? data.toString() : data);
        if (msg.method === "Page.loadEventFired") {
          ws.removeEventListener("message", onMsg);
          resolve();
        }
      } catch {}
    };
    ws.addEventListener("message", onMsg);
    setTimeout(() => {
      ws.removeEventListener("message", onMsg);
      resolve();
    }, 20000);
  });
  await delay(2000);
}

async function evaluateJs(ws, expression) {
  const res = await sendCdp(ws, 10, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  return res.result && res.result.result ? res.result.result.value : null;
}

async function clickContinueAs(ws) {
  const script = `
    (function() {
      const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"], span[role="button"]'));
      for (const btn of buttons) {
        const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
        if (text.includes('continue as') || (text.includes('continue') && text.includes('as'))) {
          btn.click();
          return { found: true, text: text };
        }
      }
      const all = Array.from(document.querySelectorAll('*'));
      for (const el of all) {
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('continue as')) {
          el.click();
          return { found: true, text: aria };
        }
      }
      return { found: false };
    })()
  `;
  return await evaluateJs(ws, script);
}

function extractRequiredCookies(allCookies) {
  const out = {};
  for (const c of allCookies) {
    if (REQUIRED_COOKIES.includes(c.name)) {
      out[c.name] = c.value;
    }
  }
  return out;
}

async function uploadCookiesToMurmur(cookies, murmurUrl, hfToken) {
  const url = `${murmurUrl.replace(/\/$/, "")}/api/cookies/upload`;
  log("uploading cookies to", url);
  const { status, text } = await postJson(url, cookies, hfToken ? { Authorization: `Bearer ${hfToken}` } : {});
  if (status >= 400) {
    throw new Error(`Murmur upload failed: HTTP ${status} ${text}`);
  }
  log("murmur upload response:", text);
  return status;
}

async function main() {
  const args = DEFAULTS;

  if (!args.email) {
    throw new Error("MAINFRAME_BROWSERUI_EMAIL not set in .env");
  }
  if (!args.murmurUrl) {
    throw new Error("MURMUR_HF_SPACE_URL not set in .env");
  }

  const profileDir = getBrowserUiProfileDir(args.email);
  if (!existsSync(profileDir)) {
    throw new Error(`BrowserUI profile not found: ${profileDir}. Run: browserui-account.ps1 use ${args.email}`);
  }

  const hfToken = resolveHfToken();
  if (!hfToken) {
    throw new Error("HF token is empty");
  }
  log("hf token loaded (...", hfToken.slice(-4), ")");

  const browserPath = findBrowserPath();
  log("using browser:", browserPath);
  log("profile:", profileDir);

  const browserArgs = [
    `--remote-debugging-port=${args.port}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-features=OptimizationHints,MediaRouter,AutofillServerCommunication",
    "--metrics-recording-only",
    "--disk-cache-size=1",
    "--media-cache-size=1",
  ];
  browserArgs.push(args.facebookUrl);

  log("launching browser (HEADED - visible window)...");
  const proc = spawn(browserPath, browserArgs, {
    detached: false,
    stdio: "inherit",
  });

  let ws = null;
  try {
    log("connecting CDP on port", args.port);
    const cdp = await connectCdp(args.port, args.connectMs);
    ws = cdp.ws;
    log("cdp connected");

    log("navigating to", args.facebookUrl);
    await navigate(ws, args.facebookUrl);

    log("checking for 'Continue as' button...");
    const clickResult = await clickContinueAs(ws);
    if (clickResult && clickResult.found) {
      log("clicked button:", clickResult.text);
      log("waiting", args.postClickWaitMs, "ms for cookies to refresh...");
      await delay(args.postClickWaitMs);
    } else {
      log("no 'Continue as' button found, proceeding with current cookies");
    }

    const cookies = await getCookies(ws);
    const required = extractRequiredCookies(cookies);
    log("extracted cookies:", Object.keys(required).join(", "));

    const hasAll = REQUIRED_COOKIES.every((k) => required[k]);
    if (!hasAll) {
      throw new Error("Missing required cookies: " + REQUIRED_COOKIES.filter((k) => !required[k]).join(", "));
    }

    await uploadCookiesToMurmur(required, args.murmurUrl, hfToken);
    log("done -- murmur cookies refreshed");

    try {
      const healthRes = await fetch(`${args.murmurUrl.replace(/\/$/, "")}/api/health`);
      if (healthRes.ok) {
        log("murmur health:", await healthRes.text());
      }
    } catch {}
  } finally {
    if (ws) {
      try {
        await sendCdp(ws, 99, "Browser.close");
      } catch {}
      try {
        ws.close();
      } catch {}
    }
    try {
      proc.kill();
    } catch {}
    log("browser closed");
  }
}

main().catch((e) => {
  err(e.message || e);
  process.exit(1);
});
