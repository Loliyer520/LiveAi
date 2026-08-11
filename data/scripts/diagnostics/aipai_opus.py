#!/usr/bin/env python3
"""
测试 aipai 渠道的 claude-opus-4-8 是否可用。
直接调用 Anthropic Messages API，打印完整请求/响应信息。
"""

from pathlib import Path
import json
import time

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "data" / "models_config.json"
UPSTREAM_NAME = "aipai"
MODEL = "claude-opus-4-8"
TIMEOUT_SECONDS = 30


def mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

upstream = None
for item in config["upstreams"]:
    if item["name"] == UPSTREAM_NAME:
        upstream = item
        break

if not upstream:
    raise SystemExit(f"ERROR: 未找到 upstream {UPSTREAM_NAME!r}")

base_url = upstream["base_url"]
api_key = upstream["api_key"]
messages_path = upstream["messages_path"] or "/v1/messages"
masked_key = mask_key(api_key)

url = f"{base_url.rstrip('/')}{messages_path}"
headers = {
    "Content-Type": "application/json",
    "x-api-key": api_key,
    "anthropic-version": "2023-06-01",
}
body = {
    "model": MODEL,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hello"}],
}

print("=" * 60)
print("REQUEST")
print("=" * 60)
print(f"URL: {url}")
print("Method: POST")
print("Headers:")
for k, v in headers.items():
    if k == "x-api-key":
        print(f"  {k}: {masked_key}")
    else:
        print(f"  {k}: {v}")
print(f"Body:\n{json.dumps(body, indent=2, ensure_ascii=False)}")
print()

print("=" * 60)
print("SENDING REQUEST...")
print("=" * 60)
start = time.time()
try:
    resp = requests.post(url, headers=headers, json=body, timeout=TIMEOUT_SECONDS)
    elapsed = time.time() - start

    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Response Status: {resp.status_code}")
    print("Response Headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    print("Response Body:")
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except Exception:
        print(resp.text)

except requests.exceptions.Timeout:
    elapsed = time.time() - start
    print(f"TIMEOUT after {elapsed:.2f}s")
except requests.exceptions.ConnectionError as e:
    elapsed = time.time() - start
    print(f"CONNECTION ERROR after {elapsed:.2f}s: {e}")
except Exception as e:
    elapsed = time.time() - start
    print(f"ERROR after {elapsed:.2f}s: {type(e).__name__}: {e}")
