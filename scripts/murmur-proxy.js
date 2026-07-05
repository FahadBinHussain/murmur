const http = require('http');
const https = require('https');
const { execFile } = require('child_process');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PROXY_PORT ? parseInt(process.env.PROXY_PORT, 10) : 7870;
const HF_TARGET = (process.env.MURMUR_HF_SPACE_URL || 'https://fahadbinhussain-murmur.hf.space') + '/wacli/webhook';

// Load HF token from mainframe profile or env
const HF_TOKEN = process.env.HF_TOKEN || (() => {
  const tokenPath = process.env.HF_TOKEN_PATH || path.join(
    process.env.APPDATA,
    'mainframe/accounts/hf/fahadbinhussain001@gmail.com/token'
  );
  if (fs.existsSync(tokenPath)) return fs.readFileSync(tokenPath, 'utf8').trim();
  return '';
})();

const WACLI_STORE = process.env.WACLI_STORE || 'C:\\Users\\Admin\\AppData\\Roaming\\mainframe\\accounts\\whatsapp\\+8801911104251\\store';
const WACLI_BINARY = process.env.WACLI_BINARY || 'C:\\Users\\Admin\\go\\bin\\wacli.exe';
const SQLITE3 = process.env.SQLITE3 || 'sqlite3';

const PROCESSED_IDS_PATH = path.join(process.env.TEMP || 'C:\\tmp', 'murmur-proxy-processed.json');

let processedIds = new Set();
let lastPollTs = 0;

function loadProcessedIds() {
  try {
    const data = JSON.parse(fs.readFileSync(PROCESSED_IDS_PATH, 'utf8'));
    processedIds = new Set(data.ids || []);
    lastPollTs = data.lastPollTs || 0;
    console.log(`[state] loaded ${processedIds.size} processed IDs`);
  } catch {
    processedIds = new Set();
    lastPollTs = 0;
  }
}

function saveProcessedIds() {
  const data = { ids: Array.from(processedIds).slice(-5000), lastPollTs };
  fs.writeFileSync(PROCESSED_IDS_PATH, JSON.stringify(data, null, 2), 'utf8');
}

function isProcessed(msgId) {
  return processedIds.has(msgId);
}

function markProcessed(msgId) {
  processedIds.add(msgId);
  saveProcessedIds();
}

function computeSignature(payload) {
  const crypto = require('crypto');
  const secret = process.env.WEBHOOK_SECRET || 'murmur-wa-2026';
  const hmac = crypto.createHmac('sha256', secret).update(payload).digest('hex');
  return 'sha256=' + hmac;
}

function forwardToMurmur(payload) {
  return new Promise((resolve, reject) => {
    const url = new URL(HF_TARGET);
    const data = Buffer.from(payload);
    const sig = computeSignature(data);
    const options = {
      hostname: url.hostname,
      path: url.pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': data.length,
        'Authorization': `Bearer ${HF_TOKEN}`,
        'X-Wacli-Signature': sig,
      },
    };
    const proxy = https.request(options, proxyRes => {
      let body = '';
      proxyRes.on('data', c => body += c);
      proxyRes.on('end', () => {
        console.log(`[forward] murmur responded ${proxyRes.statusCode}: ${body.slice(0, 200)}`);
        resolve({ status: proxyRes.statusCode, body });
      });
    });
    proxy.on('error', err => {
      console.error('[forward] proxy error:', err.message);
      reject(err);
    });
    proxy.end(data);
  });
}

function sendViaWacli(jid, text) {
  return new Promise((resolve, reject) => {
    const args = ['send', 'text', '--store', WACLI_STORE, '--to', jid, '--message', text];
    execFile(WACLI_BINARY, args, (err, stdout, stderr) => {
      if (err) {
        console.error('[send] wacli error:', err.message, stderr);
        reject(err);
      } else {
        console.log('[send] wacli ok:', stdout.trim());
        resolve(stdout.trim());
      }
    });
  });
}

function pollWacliDB() {
  const args = [
    WACLI_STORE,
    '.mode json',
    `SELECT rowid, msg_id, ts, chat_jid, sender_jid, sender_name, text, display_text FROM messages WHERE text LIKE '/ai%' AND ts > ${lastPollTs} ORDER BY ts ASC;`
  ];
  const proc = spawn(SQLITE3, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  proc.stdout.on('data', c => stdout += c);
  proc.stderr.on('data', c => stderr += c);
  proc.on('close', async (code) => {
    if (code !== 0) {
      console.error('[poll] sqlite3 error:', stderr.slice(0, 500));
      return;
    }
    let rows;
    try {
      rows = JSON.parse(stdout || '[]');
    } catch {
      console.error('[poll] json parse error');
      return;
    }
    if (!Array.isArray(rows) || rows.length === 0) return;

    let maxTs = lastPollTs;
    for (const row of rows) {
      const msgId = row.msg_id || String(row.rowid);
      if (!msgId) continue;
      if (isProcessed(msgId)) continue;

      const text = row.text || '';
      if (!text.trim().startsWith('/ai')) continue;

      const chatJID = row.chat_jid || '';
      const senderJID = row.sender_jid || '';
      const senderName = row.sender_name || '';
      const ts = row.ts || Date.now() / 1000;

      console.log(`[poll] found unprocessed /ai: ${msgId} from ${chatJID}: ${text.slice(0, 80)}`);

      const payload = JSON.stringify({
        Chat: parseJID(chatJID),
        ID: msgId,
        SenderJID: senderJID,
        Timestamp: new Date(ts * 1000).toISOString(),
        FromMe: false,
        Text: text,
        PushName: senderName,
      });

      try {
        await forwardToMurmur(payload);
        markProcessed(msgId);
      } catch (e) {
        console.error('[poll] forward failed:', e.message);
        break;
      }

      if (ts > maxTs) maxTs = ts;
    }
    lastPollTs = maxTs;
    saveProcessedIds();
  });
}

function parseJID(jidStr) {
  if (!jidStr) return { user: '', server: 's.whatsapp.net' };
  const parts = jidStr.split('@');
  if (parts.length === 2) {
    return { user: parts[0], server: parts[1] };
  }
  return { user: jidStr, server: 's.whatsapp.net' };
}

const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/webhook') {
    return handleWebhook(req, res);
  }
  if (req.method === 'POST' && req.url === '/send') {
    return handleSend(req, res);
  }
  if (req.method === 'POST' && req.url === '/mark-processed') {
    return handleMarkProcessed(req, res);
  }
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ processedCount: processedIds.size, lastPollTs }));
  }
  res.writeHead(404);
  return res.end('not found');
});

function handleWebhook(req, res) {
  let body = [];
  req.on('data', c => body.push(c));
  req.on('end', () => {
    const data = Buffer.concat(body);
    const text = data.toString('utf8');
    let parsed;
    try { parsed = JSON.parse(text); } catch { parsed = {}; }
    if (parsed.ID && isProcessed(parsed.ID)) {
      console.log(`[webhook] already processed ${parsed.ID}, skipping`);
      res.writeHead(200);
      return res.end(JSON.stringify({ status: 'ok', note: 'already processed' }));
    }
    if (parsed.ID) {
      markProcessed(parsed.ID);
    }

    // Transform Chat from string to object if needed
    if (parsed.Chat && typeof parsed.Chat === 'string') {
      const parts = parsed.Chat.split('@');
      if (parts.length === 2) {
        parsed.Chat = { user: parts[0], server: parts[1] };
      } else {
        parsed.Chat = { user: parsed.Chat, server: 's.whatsapp.net' };
      }
      console.log(`[webhook] transformed Chat to object:`, parsed.Chat);
    }
    // NOTE: SenderJID must remain a string - do NOT transform it

    const transformedData = Buffer.from(JSON.stringify(parsed), 'utf8');
    const newSig = computeSignature(transformedData);

    const url = new URL(HF_TARGET);
    const options = {
      hostname: url.hostname,
      path: url.pathname,
      method: 'POST',
      headers: {
        'Content-Type': req.headers['content-type'] || 'application/json',
        'Content-Length': transformedData.length,
        'Authorization': `Bearer ${HF_TOKEN}`,
        'X-Wacli-Signature': newSig,
      },
    };
    const proxy = https.request(options, proxyRes => {
      res.writeHead(proxyRes.statusCode, proxyRes.headers);
      proxyRes.pipe(res);
    });
    proxy.on('error', err => {
      console.error('[webhook] proxy error:', err.message);
      res.writeHead(502);
      res.end('proxy error');
    });
    proxy.end(transformedData);
  });
}

function handleSend(req, res) {
  let body = [];
  req.on('data', c => body.push(c));
  req.on('end', () => {
    let data;
    try {
      data = JSON.parse(Buffer.concat(body));
    } catch (e) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: 'invalid json' }));
    }
    const jid = data.jid || '';
    const text = data.text || '';
    if (!jid || !text) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: 'jid and text required' }));
    }
    const args = ['send', 'text', '--store', WACLI_STORE, '--to', jid, '--message', text];
    execFile(WACLI_BINARY, args, (err, stdout, stderr) => {
      if (err) {
        console.error('[send] wacli error:', err.message, stderr);
        res.writeHead(500);
        return res.end(JSON.stringify({ error: err.message, stderr }));
      }
      console.log('[send] wacli ok:', stdout.trim());
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'sent', stdout: stdout.trim() }));
    });
  });
}

function handleMarkProcessed(req, res) {
  let body = [];
  req.on('data', c => body.push(c));
  req.on('end', () => {
    let data;
    try {
      data = JSON.parse(Buffer.concat(body));
    } catch (e) {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: 'invalid json' }));
    }
    const ids = data.ids || [];
    for (const id of ids) {
      processedIds.add(id);
    }
    saveProcessedIds();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ marked: ids.length, total: processedIds.size }));
  });
}

loadProcessedIds();
server.listen(PORT, () => {
  console.log(`murmur webhook proxy on :${PORT}`);
  console.log(`[state] tracking ${processedIds.size} processed message IDs`);
});

// Fallback DB polling removed - use wacli webhook only
// setInterval(pollWacliDB, 30_000);
// pollWacliDB(); // initial poll
