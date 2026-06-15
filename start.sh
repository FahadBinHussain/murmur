#!/bin/sh
set -e

if [ -f cookies_fresh.json ] && [ ! -f messenger-cookies.json ]; then
    python3 -c "
import json
with open('cookies_fresh.json') as f:
    arr = json.load(f)
flat = {c['name']: c['value'] for c in arr if c.get('name') and c.get('value')}
with open('messenger-cookies.json', 'w') as f:
    json.dump(flat, f)
" 2>&1
fi

exec python3 ai-bridge-wrapper.py
