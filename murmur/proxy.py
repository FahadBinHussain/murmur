import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from collections.abc import Iterable
from contextlib import suppress

import aiohttp
from aiohttp import ClientError, WSMsgType, web


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


def image_proxy_auth_key() -> str | None:
    return os.getenv("IMAGE_PROXY_API_KEY") or os.getenv("IMAGES_OPENAI_API_KEY")


def image_proxy_auth_error(request: web.Request) -> web.Response | None:
    expected_key = image_proxy_auth_key()
    if not expected_key:
        return web.json_response(
            {"error": {"message": "Image proxy API key is not configured."}},
            status=503,
        )

    image_proxy_key = request.headers.get("X-Image-Proxy-Key", "")
    auth_header = request.headers.get("Authorization", "")
    scheme, _, bearer_token = auth_header.partition(" ")
    token = image_proxy_key or bearer_token
    if (
        (not image_proxy_key and scheme.lower() != "bearer")
        or not secrets.compare_digest(token, expected_key)
    ):
        return web.json_response(
            {"error": {"message": "Invalid image proxy API key."}},
            status=401,
        )

    return None


def image_proxy_config_error() -> web.Response | None:
    missing = [
        name
        for name in ("CF_ACCOUNT_ID", "CF_API_TOKEN")
        if not os.getenv(name)
    ]
    if not cloudflare_image_model():
        missing.append("IMAGE_GENERATION_MODEL")
    if missing:
        return web.json_response(
            {
                "error": {
                    "message": "Image proxy is missing: " + ", ".join(missing),
                }
            },
            status=503,
        )
    return None


def cloudflare_image_model() -> str:
    return (
        os.getenv("IMAGE_GENERATION_MODEL")
        or os.getenv("CLOUDFLARE_IMAGE_MODEL")
        or ""
    ).strip()


def parse_positive_int(value: object, default: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(1, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def openai_error(message: str, status: int = 502) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "image_generation_error"}},
        status=status,
    )


def short_prompt_id(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]


def compact_log_value(value: object, max_length: int = 2000) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}...<truncated>"


def log_image_proxy_error(prompt: str, message: str) -> None:
    prompt_id = short_prompt_id(prompt)
    print(
        "IMAGE_PROXY_ERROR "
        f"prompt_id={prompt_id} "
        f"{compact_log_value(message)}",
        flush=True,
    )
    write_image_proxy_error(prompt_id, message)


def write_image_proxy_error(prompt_id: str, message: str) -> None:
    error_dir = Path(os.getenv("IMAGE_PROXY_ERROR_DIR", "/tmp/murmur-image-errors"))
    try:
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / f"{prompt_id}.json").write_text(
            json.dumps(
                {
                    "prompt_id": prompt_id,
                    "created": int(time.time()),
                    "message": message,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            "IMAGE_PROXY_ERROR_WRITE_FAILED "
            f"prompt_id={prompt_id} error={compact_log_value(exc)}",
            flush=True,
        )


async def cloudflare_image_proxy_models(request: web.Request) -> web.Response:
    auth_error = image_proxy_auth_error(request)
    if auth_error is not None:
        return auth_error

    config_error = image_proxy_config_error()
    if config_error is not None:
        return config_error

    model = cloudflare_image_model()
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "owned_by": "cloudflare",
                }
            ],
        }
    )


async def cloudflare_image_proxy_generations(request: web.Request) -> web.Response:
    if request.method != "POST":
        return web.json_response({"error": {"message": "Method not allowed."}}, status=405)

    auth_error = image_proxy_auth_error(request)
    if auth_error is not None:
        return auth_error

    config_error = image_proxy_config_error()
    if config_error is not None:
        return config_error

    try:
        payload = await request.json()
    except ValueError:
        return openai_error("Request body must be JSON.", status=400)

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return openai_error("Missing required field: prompt.", status=400)

    max_images = parse_positive_int(os.getenv("IMAGE_PROXY_MAX_IMAGES"), 1, 4)
    image_count = parse_positive_int(payload.get("n"), 1, max_images)
    steps = parse_positive_int(payload.get("steps") or os.getenv("IMAGE_STEPS"), 4, 8)

    results = []
    for _ in range(image_count):
        generated = await generate_cloudflare_image(
            request.app["session"],
            prompt=prompt,
            steps=steps,
        )
        if isinstance(generated, web.Response):
            return generated
        results.append({"b64_json": generated})

    return web.json_response({"created": int(time.time()), "data": results})


async def generate_cloudflare_image(
    session: aiohttp.ClientSession, prompt: str, steps: int
) -> str | web.Response:
    account_id = os.environ["CF_ACCOUNT_ID"]
    api_token = os.environ["CF_API_TOKEN"]
    model = cloudflare_image_model()
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

    try:
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json={"prompt": prompt, "steps": steps},
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status >= 400:
                body = await response.text()
                log_image_proxy_error(
                    prompt,
                    "cloudflare_status="
                    f"{response.status} content_type={content_type} body={body}",
                )
                return openai_error(
                    f"Cloudflare image generation failed ({response.status}): {body}",
                    status=response.status if response.status < 500 else 502,
                )

            if content_type.startswith("image/"):
                return base64.b64encode(await response.read()).decode("ascii")

            body = await response.json(content_type=None)
    except (ClientError, asyncio.TimeoutError) as exc:
        log_image_proxy_error(prompt, f"request_failed={exc}")
        return openai_error(f"Cloudflare image generation request failed: {exc}")
    except ValueError as exc:
        log_image_proxy_error(prompt, f"invalid_json={exc}")
        return openai_error(f"Cloudflare returned an invalid JSON response: {exc}")

    result = body.get("result", body) if isinstance(body, dict) else {}
    image = result.get("image") if isinstance(result, dict) else None
    if isinstance(image, str) and image:
        return image

    errors = body.get("errors") if isinstance(body, dict) else None
    log_image_proxy_error(
        prompt,
        f"missing_image errors={errors or body}",
    )
    return openai_error(f"Cloudflare response did not include an image: {errors or body}")


async def maybe_handle_image_proxy(request: web.Request) -> web.Response | None:
    base_path = os.getenv("IMAGE_PROXY_BASE_PATH", "/murmur-image-openai/v1").rstrip("/")
    if request.path == f"{base_path}/models":
        return await cloudflare_image_proxy_models(request)
    if request.path == f"{base_path}/images/generations":
        return await cloudflare_image_proxy_generations(request)
    return None


async def proxy_http(request: web.Request) -> web.Response:
    if request.path == "/health":
        return web.json_response({"status": True, "proxy": "ok"})

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
    except (ClientError, asyncio.TimeoutError):
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

    backend_ws = None
    try:
        backend_ws = await request.app["session"].ws_connect(
            backend_url,
            headers=filtered_headers(request.headers.items()),
        )

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
    except (ClientError, asyncio.TimeoutError):
        await ws_response.close(message=b"Open WebUI is still starting")
    finally:
        if backend_ws is not None:
            with suppress(Exception):
                await backend_ws.close()

    return ws_response


async def handle(request: web.Request) -> web.StreamResponse:
    image_proxy_response = await maybe_handle_image_proxy(request)
    if image_proxy_response is not None:
        return image_proxy_response

    if request.headers.get("upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


async def session_context(app: web.Application):
    app["session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
    )
    yield
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application()
    app["target_base_url"] = os.environ["PROXY_TARGET_BASE_URL"].rstrip("/")
    app.cleanup_ctx.append(session_context)
    app.router.add_route("*", "/{path_info:.*}", handle)
    return app


def main() -> None:
    host = os.getenv("PROXY_LISTEN_HOST", "0.0.0.0")
    port = int(os.getenv("PROXY_LISTEN_PORT", "8080"))
    web.run_app(create_app(), host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
