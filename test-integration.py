import asyncio, os, sys
sys.path.insert(0, r"C:\Users\Admin\Downloads\murmur")
os.environ["MURMUR_MESSENGER_BACKEND"] = "fca_unofficial"

from murmur.fca_client import Client

async def test():
    client = Client(cookies_file_path="cookies.json", userAgent="test")
    ready = asyncio.Event()

    @client.event
    async def on_listening():
        print(f"READY: uid={client.uid}, name={client.name}")
        ready.set()

    @client.event
    async def on_message(msg):
        print(f"MESSAGE: from={msg.sender_id} thread={msg.thread_id} text={msg.text[:60]}...")

    async def run_with_timeout():
        await asyncio.wait_for(client._runner(), timeout=30)

    async def stop_after_ready():
        await ready.wait()
        print("Sending get_uid...")
        uid_data = await client._send_cmd({"type": "get_uid"})
        print(f"UID response: {uid_data}")
        print("Sending stop...")
        await client._send_cmd({"type": "stop"})

    await asyncio.gather(run_with_timeout(), stop_after_ready())

asyncio.run(test())
