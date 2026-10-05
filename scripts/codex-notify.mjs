// codex-notify.mjs - queued notification client for murmur's
// POST /api/automation/notifications endpoint (handled in internal/bridge/bridge.go).
// moved here from automata-private tools/codex 2026-10-05: murmur is the API's home.
// usage: node scripts/codex-notify.mjs <enqueue|send|flush|summary> [--message <text>] [--title <text>] [--dedupe-key <key>] [--thread-id <id>]
//   enqueue - queue a message locally, no network
//   send    - queue then flush immediately
//   flush   - POST queued messages to murmur (limit: --limit, default 10)
//   summary - print queue counts
// env: CODEX_NOTIFY_MURMUR_URL + CODEX_NOTIFY_MURMUR_TOKEN (flush target),
//      CODEX_NOTIFY_STATE_PATH (default game_detect_local/automation_state/codex_notifications.json, cwd-relative),
//      CODEX_NOTIFY_HF_PROFILE / CODEX_NOTIFY_HF_TOKEN (X-HF-Authorization for private spaces).
// exit 0 on success, 1 on error, 2 on bad usage.

import fs from 'node:fs';
import path from 'node:path';

const rawArgs = process.argv.slice(2);
if (rawArgs[0] === '--') rawArgs.shift();
const command = rawArgs[0] || 'summary';
const args = parseArgs(rawArgs.slice(1));

const statePath =
  args.get('state') ||
  process.env.CODEX_NOTIFY_STATE_PATH ||
  'game_detect_local/automation_state/codex_notifications.json';

const now = () => Date.now();

async function main() {
  if (command === 'enqueue') {
    const item = enqueue({
      source: args.get('source') || process.env.CODEX_NOTIFY_SOURCE || 'codex',
      threadId: args.get('thread-id') || args.get('threadId') || process.env.CODEX_NOTIFY_THREAD_ID || '',
      title: args.get('title') || '',
      message: args.get('message') || readStdinIfNeeded(),
      dedupeKey: args.get('dedupe-key') || args.get('dedupeKey') || '',
      url: args.get('url') || '',
    });
    console.log(JSON.stringify({ queued: true, item: publicItem(item), statePath }, null, 2));
    return;
  }

  if (command === 'send') {
    const item = enqueue({
      source: args.get('source') || process.env.CODEX_NOTIFY_SOURCE || 'codex',
      threadId: args.get('thread-id') || args.get('threadId') || process.env.CODEX_NOTIFY_THREAD_ID || '',
      title: args.get('title') || '',
      message: args.get('message') || readStdinIfNeeded(),
      dedupeKey: args.get('dedupe-key') || args.get('dedupeKey') || '',
      url: args.get('url') || '',
    });
    const flushResult = await flush(Number(args.get('limit') || 10));
    console.log(JSON.stringify({ queued: publicItem(item), flush: flushResult, statePath }, null, 2));
    return;
  }

  if (command === 'flush') {
    console.log(JSON.stringify(await flush(Number(args.get('limit') || 10)), null, 2));
    return;
  }

  if (command === 'summary') {
    console.log(JSON.stringify(summary(), null, 2));
    return;
  }

  console.error('Usage: node scripts/codex-notify.mjs <enqueue|send|flush|summary> [--message <text>] [--title <text>] [--dedupe-key <key>] [--thread-id <id>]');
  process.exit(2);
}

function parseArgs(values) {
  const parsed = new Map();
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (!value.startsWith('--')) continue;
    const key = value.slice(2);
    const next = values[index + 1];
    parsed.set(key, next && !next.startsWith('--') ? values[++index] : 'true');
  }
  return parsed;
}

function readStdinIfNeeded() {
  if (process.stdin.isTTY) return '';
  try {
    return fs.readFileSync(0, 'utf8').trim();
  } catch {
    return '';
  }
}

function blankState() {
  return { version: 1, items: [] };
}

function loadState() {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    return {
      version: 1,
      items: Array.isArray(parsed.items) ? parsed.items.map(normalizeItem).filter(Boolean) : [],
    };
  } catch {
    return blankState();
  }
}

function saveState(state) {
  const normalized = {
    version: 1,
    items: pruneItems(state.items.map(normalizeItem).filter(Boolean)),
  };
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  fs.writeFileSync(statePath, JSON.stringify(normalized, null, 2), 'utf8');
}

function normalizeItem(item) {
  if (!item || typeof item !== 'object') return null;
  const id = String(item.id || '').trim();
  const message = String(item.message || '').trim();
  if (!id || !message) return null;
  return {
    id,
    source: String(item.source || 'codex').trim(),
    threadId: String(item.threadId || '').trim(),
    title: String(item.title || '').trim(),
    message,
    dedupeKey: String(item.dedupeKey || '').trim(),
    url: String(item.url || '').trim(),
    status: String(item.status || 'pending'),
    attempts: Number(item.attempts || 0),
    createdAt: Number(item.createdAt || now()),
    updatedAt: Number(item.updatedAt || item.createdAt || now()),
    nextAttemptAt: Number(item.nextAttemptAt || 0),
    lastError: item.lastError ? String(item.lastError).slice(0, 2000) : '',
    murmurId: item.murmurId ? String(item.murmurId) : '',
  };
}

function pruneItems(items) {
  const cutoff = now() - 7 * 24 * 60 * 60 * 1000;
  return items.filter((item) => item.status !== 'sent' || item.updatedAt >= cutoff);
}

function enqueue(input) {
  const message = String(input.message || '').trim();
  const title = String(input.title || '').trim();
  if (!message && !title) throw new Error('notification message is required');

  const state = loadState();
  const dedupeKey = String(input.dedupeKey || '').trim();
  if (dedupeKey) {
    const existing = state.items.find(
      (item) =>
        item.dedupeKey === dedupeKey &&
        item.source === String(input.source || 'codex') &&
        item.status !== 'dead',
    );
    if (existing) return existing;
  }

  const item = {
    id: notificationId(input.source, dedupeKey, message || title),
    source: String(input.source || 'codex').trim(),
    threadId: String(input.threadId || '').trim(),
    title,
    message,
    dedupeKey,
    url: String(input.url || '').trim(),
    status: 'pending',
    attempts: 0,
    createdAt: now(),
    updatedAt: now(),
    nextAttemptAt: 0,
    lastError: '',
    murmurId: '',
  };
  state.items.push(item);
  saveState(state);
  return item;
}

function notificationId(source, dedupeKey, message) {
  const seed = `${Date.now()}:${Math.random()}:${source}:${dedupeKey}:${message}`;
  return `ntf_${hashString(seed).slice(0, 24)}`;
}

function hashString(value) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0') + Math.random().toString(16).slice(2);
}

async function flush(limit) {
  const endpoint = murmurEndpoint();
  const token = process.env.CODEX_NOTIFY_MURMUR_TOKEN || args.get('token') || '';
  if (!endpoint || !token) {
    return {
      ok: false,
      sent: 0,
      failed: 0,
      pending: summary().counts.pending || 0,
      reason: 'missing CODEX_NOTIFY_MURMUR_URL or CODEX_NOTIFY_MURMUR_TOKEN',
      statePath,
    };
  }

  const state = loadState();
  const due = state.items
    .filter((item) => ['pending', 'failed'].includes(item.status) && item.nextAttemptAt <= now())
    .slice(0, Math.max(1, limit || 10));
  const results = [];

  for (const item of due) {
    item.status = 'sending';
    item.attempts += 1;
    item.updatedAt = now();
    try {
      const response = await postNotification(endpoint, token, item);
      item.status = 'sent';
      item.murmurId = response.id || response.notificationId || '';
      item.lastError = '';
      item.nextAttemptAt = 0;
      item.updatedAt = now();
      results.push({ id: item.id, status: 'sent', murmurId: item.murmurId });
    } catch (error) {
      item.status = item.attempts >= 10 ? 'dead' : 'failed';
      item.lastError = redactedError(error);
      item.nextAttemptAt = item.status === 'dead' ? 0 : now() + retryDelayMs(item.attempts);
      item.updatedAt = now();
      results.push({ id: item.id, status: item.status, error: item.lastError });
    }
  }

  saveState(state);
  return {
    ok: results.every((item) => item.status === 'sent'),
    sent: results.filter((item) => item.status === 'sent').length,
    failed: results.filter((item) => item.status !== 'sent').length,
    results,
    statePath,
  };
}

function murmurEndpoint() {
  const base = args.get('murmur-url') || process.env.CODEX_NOTIFY_MURMUR_URL || '';
  if (!base) return '';
  const endpoint = args.get('endpoint') || process.env.CODEX_NOTIFY_MURMUR_ENDPOINT || '/api/automation/notifications';
  if (/\/api\/automation\/notifications\/?$/i.test(base)) return base;
  return new URL(endpoint.replace(/^\/+/, ''), base.endsWith('/') ? base : `${base}/`).toString();
}

async function postNotification(endpoint, token, item) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Number(process.env.CODEX_NOTIFY_TIMEOUT_MS || 30000));
  const headers = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    ...privateSpaceHeaders(),
  };
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        source: item.source,
        threadId: item.threadId || process.env.CODEX_NOTIFY_THREAD_ID || undefined,
        title: item.title || undefined,
        message: item.message,
        dedupeKey: item.dedupeKey || item.id,
        url: item.url || undefined,
      }),
      signal: controller.signal,
    });
    const text = await response.text();
    const body = text ? JSON.parse(text) : {};
    if (!response.ok) {
      throw new Error(`Murmur notification enqueue failed with HTTP ${response.status}: ${body.error?.message || response.statusText}`);
    }
    return body;
  } finally {
    clearTimeout(timeout);
  }
}

function privateSpaceHeaders() {
  const hfToken = readHfToken();
  return hfToken ? { 'X-HF-Authorization': `Bearer ${hfToken}` } : {};
}

function readHfToken() {
  const direct = String(process.env.CODEX_NOTIFY_HF_TOKEN || '').trim();
  if (direct) return direct;

  const tokenPath = String(process.env.CODEX_NOTIFY_HF_TOKEN_PATH || '').trim();
  if (tokenPath) return readSecretFile(tokenPath);

  const profile = String(process.env.CODEX_NOTIFY_HF_PROFILE || '').trim().toLowerCase();
  if (!profile) return '';
  const appData = process.env.APPDATA || '';
  if (!appData) return '';
  return readSecretFile(path.join(appData, 'mainframe', 'accounts', 'hf', profile, 'token.txt'));
}

function readSecretFile(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8').trim();
  } catch {
    return '';
  }
}

function retryDelayMs(attempts) {
  return Math.min(60 * 60 * 1000, 30_000 * 2 ** Math.max(0, attempts - 1));
}

function redactedError(error) {
  return String(error?.message || error)
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]')
    .replace(/access_token=[^&\s]+/gi, 'access_token=[redacted]')
    .slice(0, 2000);
}

function publicItem(item) {
  return {
    id: item.id,
    source: item.source,
    title: item.title,
    dedupeKey: item.dedupeKey,
    status: item.status,
    attempts: item.attempts,
    url: item.url,
  };
}

function summary() {
  const state = loadState();
  const counts = {};
  for (const item of state.items) counts[item.status] = (counts[item.status] || 0) + 1;
  return { statePath, counts, items: state.items.map(publicItem) };
}

main().catch((error) => {
  console.error(redactedError(error));
  process.exit(1);
});
