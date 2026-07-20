#!/usr/bin/env python3
"""
测试 aipai 渠道的 claude-opus-4-8 是否可用。
直接调用 Anthropic Messages API，打印完整请求/响应信息。
"""

import json
import requests
import time

# === 配置 ===
BASE_URL = "https://apiapipp.com"
API_KEY = "sk-cxxy7ENoFKmNGn90SpLGtNFZEF9dKWPBED3moghCjn53xM1A"
MODEL = "claude-opus-4-8"
MESSAGES_PATH = "/v1/messages"  # aipai messages_path 为空，使用默认 anthropic 路径

# === 脱敏 api_key ===
masked_key = f"{API_KEY[:6]}...{API_KEY[-4:]}"

# === 构造请求 ===
url = f"{BASE_URL.rstrip('/')}{MESSAGES_PATH}"

headers = {
    "Content-Type": "application/json",
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
}

body = {
    "model": MODEL,
    "max_tokens": 64,
    "messages": [
        {"role": "user", "content": "hello"}
    ],
}

# === 打印请求信息 ===
print("=" * 60)
print("REQUEST")
print("=" * 60)
print(f"URL: {url}")
print(f"Method: POST")
print(f"Headers:")
for k, v in headers.items():
    if k == "x-api-key":
        print(f"  {k}: {masked_key}")
    else:
        print(f"  {k}: {v}")
print(f"Body:\n{json.dumps(body, indent=2, ensure_ascii=False)}")
print()

# === 发送请求 ===
print("=" * 60)
print("SENDING REQUEST...")
print("=" * 60)
start = time.time()
try:
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    elapsed = time.time() - start

    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Response Status: {resp.status_code}")
    print(f"Response Headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    print(f"Response Body:")
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
