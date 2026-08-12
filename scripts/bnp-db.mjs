// bnp-db.mjs — BNP outbox queries over wss:443 via the Neon serverless driver.
// psql's 5432 is blocked while ProtonVPN is up (IDMWFP drops non-tunnel flows),
// so murmur.ps1 uses this for the cookie-health check + failed-outbox reset.
// usage: node bnp-db.mjs <count|reset>   (reads BNP_DATABASE_URL + BNP_WINDOW_MINUTES env)
// exit 0 + plain result on success; exit 1 + stderr on error.

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

function envOrDotenv(name) {
  if (process.env[name]) return process.env[name];
  const dotenv = join(root, '.env');
  if (!existsSync(dotenv)) return undefined;
  const line = readFileSync(dotenv, 'utf8').split(/\r?\n/).find((l) => l.startsWith(`${name}=`));
  return line ? line.slice(name.length + 1).trim() : undefined;
}

const url = envOrDotenv('BNP_DATABASE_URL');
const windowM = envOrDotenv('BNP_WINDOW_MINUTES') || '30';
if (!url) {
  console.error('ERR: BNP_DATABASE_URL not set (env or ../.env)');
  process.exit(1);
}

async function loadNeon() {
  const candidates = [];
  try {
    candidates.push(await import('@neondatabase/serverless'));
  } catch {}
  candidates.push(
    await import(
      'file:///C:/Users/Admin/scoop/persist/nodejs-lts/bin/node_modules/@neondatabase/serverless/index.js'
    )
  );
  for (const m of candidates) {
    const f = m?.neon ?? m?.default?.neon ?? m?.['module.exports']?.neon;
    if (typeof f === 'function') return f;
  }
  throw new Error('@neondatabase/serverless not resolvable');
}
const neonFn = await loadNeon();

const sql = neonFn(url);
const cmd = process.argv[2];
const w = /^\d+$/.test(windowM) ? windowM : '30';

try {
  if (cmd === 'count') {
    const rows = await sql`${sql.unsafe(`
      SELECT count(*)::int AS cnt FROM "BnpMessengerNotification"
      WHERE status = 'failed'
        AND "lastError" = 'send returned empty message ID'
        AND "updatedAt" >= NOW() - (INTERVAL '${w} minutes')`)}`;
    console.log(rows[0].cnt);
  } else if (cmd === 'reset') {
    const rows = await sql`${sql.unsafe(`
      UPDATE "BnpMessengerNotification"
      SET status = 'pending',
          "lockedAt" = NULL,
          "lastError" = NULL,
          attempts = 0,
          "updatedAt" = NOW()
      WHERE status = 'failed'
        AND "lastError" = 'send returned empty message ID'
        AND "updatedAt" >= NOW() - (INTERVAL '${w} minutes')
        AND phase IN ('detected', 'published')
      RETURNING id`)}`;
    console.log(`UPDATED ${rows.length}`);
  } else {
    console.error('ERR: usage: node bnp-db.mjs <count|reset>');
    process.exit(1);
  }
  process.exit(0);
} catch (e) {
  console.error(`ERR: ${e.message}`);
  process.exit(1);
}
