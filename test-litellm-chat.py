import urllib.request, json
url = 'https://alchoholpad-litellm-huggingface-template.hf.space/v1/chat/completions'
body = json.dumps({
    "model": "openrouter/google/gemma-4-31b-it:free",
    "messages": [{"role": "user", "content": "say hello in 3 words"}],
    "max_tokens": 50
}).encode()
req = urllib.request.Request(url, data=body, method='POST')
req.add_header('Content-Type', 'application/json')
resp = urllib.request.urlopen(req, timeout=60)
d = json.loads(resp.read())
print(d['choices'][0]['message']['content'])
