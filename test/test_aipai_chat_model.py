#!/usr/bin/env python3
"""
用 AnthropicChatModel 类实际调用 aipai 渠道的 claude-opus-4-8，
打印完整错误信息（HTTP status、response body、请求 URL、headers 脱敏），
确认 AnthropicChatModel 类构造请求的方式是否有问题。
"""

import json
import sys
import os
import traceback

# 把项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pack.anthropic_chat_model import AnthropicChatModel

# === 从 models_config.json 读取 aipai 上游配置 ===
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'data', 'models_config.json')
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    config = json.load(f)

# 找到 aipai upstream
aipai = None
for u in config['upstreams']:
    if u['name'] == 'aipai':
        aipai = u
        break

if not aipai:
    print('ERROR: 未找到 aipai upstream')
    sys.exit(1)

BASE_URL = aipai['base_url']
API_KEY = aipai['api_key']
MESSAGES_PATH = aipai['messages_path']  # 空字符串
MODEL = 'claude-opus-4-8'

masked_key = f"{API_KEY[:6]}...{API_KEY[-4:]}" if len(API_KEY) > 10 else '***'

print("=" * 70)
print("CONFIG FROM models_config.json (aipai upstream)")
print("=" * 70)
print(f"  base_url:      {BASE_URL}")
print(f"  api_key:       {masked_key}")
print(f"  messages_path: {MESSAGES_PATH!r} (空字符串)")
print(f"  model_name:    {MODEL}")
print()

# === 构造 AnthropicChatModel 实例 ===
print("=" * 70)
print("CONSTRUCTING AnthropicChatModel")
print("=" * 70)
model = AnthropicChatModel(
    base_url=BASE_URL,
    api_key=API_KEY,
    model_name=MODEL,
    messages_path=MESSAGES_PATH,
)
print(f"  model.base_url:       {model.base_url}")
print(f"  model.messages_path:  {model.messages_path!r}")
print(f"  model.is_openai_protocol: {model.is_openai_protocol}")
print(f"  FULL URL:             {model.base_url}{model.messages_path}")
print()

# === 手动构造 request 看看 headers 和 body ===
# 模拟 complete() 里非 OpenAI 协议分支的构造逻辑
print("=" * 70)
print("REQUEST DETAILS (模拟 complete() 构造)")
print("=" * 70)

headers = {
    'Content-Type': 'application/json',
    'anthropic-version': '2023-06-01',
}
if API_KEY:
    headers['x-api-key'] = API_KEY
    headers['Authorization'] = f'Bearer {API_KEY}'

payload = {
    'model': MODEL,
    'max_tokens': 2048,
    'temperature': 0.7,
    'messages': [{'role': 'user', 'content': 'hello'}],
    'stream': False,
}
# system_blocks 传空字符串时不会加入 payload

print(f"URL: {model.base_url}{model.messages_path}")
print(f"Headers:")
for k, v in headers.items():
    if 'key' in k.lower() or k.lower() == 'authorization':
        print(f"  {k}: {masked_key}" if k == 'x-api-key' else f"  {k}: Bearer {masked_key}")
    else:
        print(f"  {k}: {v}")
print(f"Body:")
print(json.dumps(payload, indent=2, ensure_ascii=False))
print()

# === 实际调用 complete() ===
print("=" * 70)
print("CALLING complete()")
print("=" * 70)

import requests as req_lib
import time

start = time.time()
try:
    result = model.complete(
        system_blocks='',
        messages=[{'role': 'user', 'content': 'hello'}],
        max_tokens=64,
    )
    elapsed = time.time() - start
    print(f"SUCCESS! ({elapsed:.2f}s)")
    print(f"  text:       {result.text!r}")
    print(f"  stop_reason: {result.stop_reason!r}")
    print(f"  tool_calls:  {result.tool_calls}")
    print(f"  raw_content: {json.dumps(result.raw_content, indent=2, ensure_ascii=False)}")

except RuntimeError as e:
    elapsed = time.time() - start
    print(f"RuntimeError after {elapsed:.2f}s:")
    print(f"  {e}")
    print()
    # 尝试单独发一个请求拿到更多信息
    print("=" * 70)
    print("EXTRA: 手动重发请求获取完整 HTTP 信息")
    print("=" * 70)
    try:
        resp = req_lib.post(
            f'{model.base_url}{model.messages_path}',
            headers=headers,
            json=payload,
            timeout=30,
        )
        print(f"  HTTP Status: {resp.status_code}")
        print(f"  Response Headers:")
        for k, v in resp.headers.items():
            print(f"    {k}: {v}")
        print(f"  Response Body:")
        try:
            body_json = resp.json()
            print(json.dumps(body_json, indent=2, ensure_ascii=False))
        except Exception:
            print(f"    (raw) {resp.text[:2000]}")
    except Exception as e2:
        print(f"  Manual request also failed: {type(e2).__name__}: {e2}")

except req_lib.exceptions.Timeout:
    elapsed = time.time() - start
    print(f"TIMEOUT after {elapsed:.2f}s")

except req_lib.exceptions.ConnectionError as e:
    elapsed = time.time() - start
    print(f"CONNECTION ERROR after {elapsed:.2f}s: {e}")

except req_lib.exceptions.JSONDecodeError as e:
    elapsed = time.time() - start
    print(f"JSONDecodeError after {elapsed:.2f}s:")
    print(f"  message: {e}")
    print()
    # 尝试拿到原始响应内容
    print("=" * 70)
    print("EXTRA: 手动重发请求获取原始响应")
    print("=" * 70)
    try:
        resp = req_lib.post(
            f'{model.base_url}{model.messages_path}',
            headers=headers,
            json=payload,
            timeout=30,
        )
        print(f"  HTTP Status: {resp.status_code}")
        print(f"  Response Headers:")
        for k, v in resp.headers.items():
            print(f"    {k}: {v}")
        print(f"  Response Body (first 2000 chars):")
        print(f"    {resp.text[:2000]}")
    except Exception as e2:
        print(f"  Manual request also failed: {type(e2).__name__}: {e2}")

except Exception as e:
    elapsed = time.time() - start
    print(f"UNEXPECTED ERROR after {elapsed:.2f}s:")
    print(f"  type: {type(e).__name__}")
    print(f"  message: {e}")
    traceback.print_exc()
