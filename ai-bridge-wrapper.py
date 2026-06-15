#!/usr/bin/env python3
import subprocess, json, sys, time, threading, os, urllib.request, urllib.error

LITELLM_BASE = os.getenv("LITELLM_BASE", "https://alchoholpad-litellm-huggingface-template.hf.space/v1")
BRIDGE = os.getenv("BRIDGE", "messenger-bridge.exe")
COOKIES = os.getenv("MESSENGER_COOKIES", "messenger-cookies.json")

DEFAULT_CHAT = os.getenv("DEFAULT_CHAT", "openrouter/google/gemma-4-31b-it:free")
DEFAULT_IMAGE = os.getenv("DEFAULT_IMAGE", "cloudflare/@cf/black-forest-labs/flux-1-schnell")
MY_UID = int(os.getenv("MY_UID", "100094912747838"))

IMAGE_KEYWORDS = ["image", "flux", "sd-xl", "stable-diffusion", "sdxl", "illustrious", "illustrij"]



def fmt_response(model, text):
    if "/" in model:
        provider, rest = model.split("/", 1)
        return f"[{provider} - {rest}]\n{text}"
    return f"[{model}]\n{text}"

def litellm_request(method, path, body=None):
    url = f"{LITELLM_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}"}
    except Exception as e:
        return {"error": str(e)}

PAGE_SIZE = 25

def paginate_models(models, page=1, header="Models", cmd="models"):
    total = len(models)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_models = models[start:end]
    lines = [f"{header} ({start+1}-{end} of {total}):", "", "[LiteLLM gateway]"]
    for i, m in enumerate(page_models, start=start+1):
        lines.append(f"{i}. {m}")
    lines.append("")
    lines.append(f"Page {page}/{total_pages}")
    if page > 1:
        lines.append(f"Previous: /ai {cmd} {page-1}")
    if page < total_pages:
        lines.append(f"Next: /ai {cmd} {page+1}")
    return "\n".join(lines)

def list_usable_models(page=1):
    data = litellm_request("GET", "/model-catalog")
    models = sorted([m["id"] for m in data.get("data", [])
                     if m.get("usable") and not m.get("image_usable")])
    return paginate_models(models, page, "Chat Models", "models")

def list_image_usable_models(page=1):
    data = litellm_request("GET", "/model-catalog")
    models = sorted([m["id"] for m in data.get("data", [])
                     if m.get("image_usable")])
    return paginate_models(models, page, "Image Models", "image models")

def list_all_models(page=1):
    data = litellm_request("GET", "/models")
    models = [m.get("id", "") for m in data.get("data", [])]
    return paginate_models(models, page, "Models", "models full")

def list_all_image_models(page=1):
    data = litellm_request("GET", "/models")
    models = [m.get("id", "") for m in data.get("data", [])
              if any(x in m.get("id", "") for x in ["image", "flux", "sd-xl", "stable-diffusion", "sdxl", "illustrious", "illustrij"])]
    return paginate_models(models, page, "Image Models", "image models full")

def handle_ai_command(text, thread_id):
    text = text.strip()
    lower = text.lower()

    parts = text.split()
    def parse_page(parts, cmd_idx):
        if len(parts) > cmd_idx and parts[cmd_idx].isdigit():
            return int(parts[cmd_idx])
        return 1

    if lower.startswith("/ai image models full") or lower.startswith("/ai imamge models full"):
        page = parse_page(parts, 4)
        return fmt_response("system", list_all_image_models(page))

    if lower.startswith("/ai image models") or lower.startswith("/ai imamge models"):
        page = parse_page(parts, 3)
        return fmt_response("system", list_image_usable_models(page))

    if lower.startswith("/ai models full"):
        page = parse_page(parts, 3)
        return fmt_response("system", list_all_models(page))

    if lower.startswith("/ai models"):
        page = parse_page(parts, 2)
        return fmt_response("system", list_usable_models(page))

    if lower.startswith("/ai image ") or lower.startswith("/ai imamge "):
        prompt = text[len("/ai image "):].strip() if lower.startswith("/ai image ") else text[len("/ai imamge "):].strip()
        prompt = prompt or "a cute cat"
        result = litellm_request("POST", "/images/generations", {
            "model": DEFAULT_IMAGE, "prompt": prompt, "n": 1
        })
        if "error" in result:
            return fmt_response("image", f"error: {result['error']}")
        data = result.get("data", [])
        if data and "url" in data[0]:
            return fmt_response(DEFAULT_IMAGE, data[0]["url"])
        elif data and "b64_json" in data[0]:
            return fmt_response(DEFAULT_IMAGE, f"image generated ({len(data[0]['b64_json'])} bytes base64)")
        return fmt_response("image", "generated but no url in response")

    prompt = text[len("/ai "):].strip()
    if not prompt:
        return "usage: /ai <text> | /ai image <prompt> | /ai models | /ai models usable | /ai image models usable"

    result = litellm_request("POST", "/chat/completions", {
        "model": DEFAULT_CHAT,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    })
    if "error" in result:
        return fmt_response("chat", f"error: {result['error']}")
    choices = result.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", "no response")
        return fmt_response(DEFAULT_CHAT, content)
    return fmt_response(DEFAULT_CHAT, "no response from model")

env = os.environ.copy()
env["MESSENGER_COOKIES"] = COOKIES
env["NO_COLOR"] = "1"
proc = subprocess.Popen([BRIDGE, "--cookies", COOKIES], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

def write_stdin(data):
    proc.stdin.write((json.dumps(data) + "\n").encode())
    proc.stdin.flush()

start_time = time.time()

def reader():
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.decode().strip()
        if not line:
            continue
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "message":
            msg = evt.get("data", {})
            text = msg.get("text", "")
            sender = msg.get("sender_id", 0)
            ts = msg.get("timestamp", 0)
            if sender == MY_UID:
                continue
            if ts < start_time * 1000 - 5000:
                continue
            if text.strip().startswith("/ai"):
                thread_id = msg.get("thread_id", 0)
                reply = handle_ai_command(text, thread_id)
                write_stdin({"type": "send_message", "id": "ai_reply",
                             "data": {"thread_id": thread_id, "text": reply}})
        elif evt.get("type") == "permanent_error":
            sys.stdout.write('{"type":"fatal","data":"bridge error, exiting"}\n')
            os._exit(1)

threading.Thread(target=reader, daemon=True).start()

def stderr_reader():
    while True:
        line = proc.stderr.readline()
        if not line:
            break
        sys.stderr.write(line.decode())
        sys.stderr.flush()

threading.Thread(target=stderr_reader, daemon=True).start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    write_stdin({"type": "stop"})
    proc.wait(timeout=10)
