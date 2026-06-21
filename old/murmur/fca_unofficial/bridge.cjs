#!/usr/bin/env node
const login = require('@dongdev/fca-unofficial');
const { readFileSync, writeFileSync } = require('node:fs');
const { createInterface } = require('node:readline');

const COOKIE_PATH = process.argv[2];
if (!COOKIE_PATH) {
  process.stderr.write('Usage: bridge.mjs <cookies.json>\n');
  process.exit(1);
}

let api = null;
let uid = '';

function send(type, payload) {
  const line = JSON.stringify({ type, ...payload }) + '\n';
  process.stdout.write(line);
}

function sendError(id, error) {
  send('error', { id, error });
}

function sendResponse(id, result) {
  send('response', { id, result });
}

function sendEvent(name, data) {
  send('event', { name, data });
}

// Load appState
let appState;
try {
  appState = JSON.parse(readFileSync(COOKIE_PATH, 'utf8'));
} catch (err) {
  sendError('bootstrap', `Failed to read cookie file: ${err.message}`);
  process.exit(1);
}

// Normalise appState: if the file has a {cookies: [...]} wrapper, unwrap it
if (appState && typeof appState === 'object' && !Array.isArray(appState)) {
  const inner = appState.cookies || appState.state || null;
  if (Array.isArray(inner)) {
    appState = inner;
  }
}

login({ appState }, (err, apiObj) => {
  if (err) {
    sendError('login', err.message || String(err));
    process.exit(1);
    return;
  }
  api = apiObj;

  // Save fresh appState
  try { writeFileSync(COOKIE_PATH, JSON.stringify(api.getAppState())); } catch {}

  uid = typeof api.getCurrentUserID === 'function' ? String(api.getCurrentUserID()) : '';

  // Get own display name
  if (uid && typeof api.getUserInfo === 'function') {
    api.getUserInfo(uid, (err2, info) => {
      const name = (!err2 && info && info[uid])
        ? (info[uid].name || info[uid].firstName || info[uid].vanity || '')
        : '';
      send('ready', { uid, name });
    });
  } else {
    send('ready', { uid, name: '' });
  }

  // MQTT listen
  api.listenMqtt((err3, event) => {
    if (err3) {
      sendError('listen', err3.message || String(err3));
      return;
    }
    if (!event || !event.type) return;

    if (event.type === 'message' || event.type === 'message_reply') {
      sendEvent('message', {
        id: event.messageID || '',
        text: event.body || '',
        sender_id: String(event.senderID || ''),
        thread_id: String(event.threadID || ''),
        thread_type: event.isGroup ? 1 : 0,
      });
    }
  });
});

// Readline interface for stdin commands
const rl = createInterface({ input: process.stdin, terminal: false });

rl.on('line', (line) => {
  let cmd;
  try { cmd = JSON.parse(line); } catch { return; }

  switch (cmd.type) {
    case 'send_message': {
      if (!api) { sendError(cmd.id, 'Not logged in'); return; }
      const msgPayload = String(cmd.text || '');
      api.sendMessage(msgPayload, cmd.thread_id, (sendErr, info) => {
        if (sendErr) {
          sendError(cmd.id, sendErr.message || String(sendErr));
        } else {
          sendResponse(cmd.id, { message_id: info ? (info.messageID || null) : null });
        }
      });
      break;
    }

    case 'fetch_thread_list': {
      if (!api) { sendError(cmd.id, 'Not logged in'); return; }
      const limit = cmd.limit || 20;
      api.getThreadList(limit, null, [], (listErr, list) => {
        if (listErr) {
          sendError(cmd.id, listErr.message || String(listErr));
        } else {
          sendResponse(cmd.id, { threads: list || [] });
        }
      });
      break;
    }

    case 'get_uid': {
      const currentUid = api && typeof api.getCurrentUserID === 'function'
        ? String(api.getCurrentUserID()) : '';
      sendResponse(cmd.id, { uid: currentUid });
      break;
    }

    case 'stop': {
      process.exit(0);
      break;
    }
  }
});
