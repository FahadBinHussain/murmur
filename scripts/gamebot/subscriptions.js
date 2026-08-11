// loads scripts/gamebot/subscriptions.json and returns the thread ids
// subscribed to the given source (comma-separated env override wins if set).
//
// subscriptions.json shape:
// {
//   "threads": {
//     "30738305889116993": ["gamebot", "steam-updates"],
//     "2637078310061988": ["gamebot"]
//   }
// }

const fs = require("fs");
const path = require("path");

function loadThreads(source) {
  const envOverride = process.env.MURMUR_THREAD_ID;
  if (envOverride) {
    return envOverride
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
  }

  const file = path.join(__dirname, "subscriptions.json");
  let subs = { threads: {} };
  try {
    subs = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    console.error(`subscriptions.json missing or unreadable: ${file}`);
    process.exit(1);
  }

  return Object.entries(subs.threads || {})
    .filter(([, sources]) => Array.isArray(sources) && sources.includes(source))
    .map(([threadId]) => threadId);
}

module.exports = { loadThreads };
