import urllib.request, json
d = json.loads(urllib.request.urlopen('https://alchoholpad-litellm-huggingface-template.hf.space/v1/models', timeout=30).read())
print(f'got {len(d.get("data",[]))} models')
print(f'image models: {len([m for m in d.get("data",[]) if "image" in m.get("id","") or "flux" in m.get("id","")])}')
print(f'first 5: {[m.get("id","") for m in d.get("data",[])[:5]]}')
