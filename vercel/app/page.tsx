export default function Page() {
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: 24, maxWidth: 560 }}>
      <h1>murmur steam-updates</h1>
      <p>
        Polls Steam dev announcements (ISteamNews, keyless) and notifies subscribed Messenger
        threads via the murmur HF Space webhook.
      </p>
      <p>
        Trigger: <code>GET /api/steam-updates</code> (cron-job.org, every 1 min)
      </p>
    </main>
  );
}
