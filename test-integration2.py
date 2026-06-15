import asyncio, os, sys
sys.path.insert(0, r"C:\Users\Admin\Downloads\murmur")
os.environ["MURMUR_MESSENGER_BACKEND"] = "fca_unofficial"

from murmur.fca_client import Client

async def test():
    client = Client(cookies_file_path="cookies.json")
    ready = asyncio.Event()

    @client.event
    async def on_listening():
        print(f"READY: uid={client.uid}, name={client.name}")
        ready.set()

    @client.event
    async def on_message(msg):
        print(f"MSG: from={msg.sender_id} thread={msg.thread_id}")

    async def runner():
        await client._runner()

    async def controller():
        await ready.wait()
        print("Sending get_uid...")
        r = await client._send_cmd({"type": "get_uid"})
        print(f"get_uid response: {r}")
        print("Sending stop...")
        client._proc.stdin.write(b'{"type":"stop","id":"stop1"}\n')
        await client._proc.stdin.drain()

    await asyncio.gather(runner(), controller())
    print("Done!")

asyncio.run(test())
