import asyncio
import os
from collections.abc import Iterable

import aiohttp
from aiohttp import ClientConnectorError, WSMsgType, web


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


def target_url(request: web.Request) -> str:
    base_url = request.app["target_base_url"]
    return f"{base_url}{request.rel_url}"


async def proxy_http(request: web.Request) -> web.Response:
    url = target_url(request)
    headers = filtered_headers(request.headers.items())
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme

    try:
        async with request.app["session"].request(
            request.method,
            url,
            headers=headers,
            data=await request.read(),
            allow_redirects=False,
        ) as response:
            body = await response.read()
            return web.Response(
                body=body,
                status=response.status,
                headers=filtered_headers(response.headers.items()),
            )
    except ClientConnectorError:
        if request.path == "/health":
            return web.json_response({"status": True, "backend": "starting"})
        return web.Response(
            text="Open WebUI is still starting. Try again in a moment.",
            status=503,
        )


async def proxy_websocket(request: web.Request) -> web.WebSocketResponse:
    ws_response = web.WebSocketResponse()
    await ws_response.prepare(request)

    backend_url = target_url(request).replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )

    try:
        async with request.app["session"].ws_connect(
            backend_url,
            headers=filtered_headers(request.headers.items()),
        ) as backend_ws:

            async def client_to_backend() -> None:
                async for message in ws_response:
                    if message.type == WSMsgType.TEXT:
                        await backend_ws.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await backend_ws.send_bytes(message.data)
                    elif message.type == WSMsgType.CLOSE:
                        await backend_ws.close()

            async def backend_to_client() -> None:
                async for message in backend_ws:
                    if message.type == WSMsgType.TEXT:
                        await ws_response.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await ws_response.send_bytes(message.data)
                    elif message.type == WSMsgType.CLOSE:
                        await ws_response.close()

            await asyncio.gather(client_to_backend(), backend_to_client())
    except ClientConnectorError:
        await ws_response.close(message=b"Open WebUI is still starting")

    return ws_response


async def handle(request: web.Request) -> web.StreamResponse:
    if request.headers.get("upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


async def create_app() -> web.Application:
    app = web.Application()
    app["target_base_url"] = os.environ["PROXY_TARGET_BASE_URL"].rstrip("/")
    app["session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
    )
    app.router.add_route("*", "/{path_info:.*}", handle)
    return app


def main() -> None:
    host = os.getenv("PROXY_LISTEN_HOST", "0.0.0.0")
    port = int(os.getenv("PROXY_LISTEN_PORT", "8080"))
    web.run_app(create_app(), host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
