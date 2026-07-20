#!/usr/bin/env python3
"""
测试 aipai 渠道（claude-opus-4-8）——使用项目自己的 AnthropicChatModel 类。

从 data/models_config.json 读 aipai 配置 → 构造 AnthropicChatModel → 调 complete()
→ 打印实际请求 URL / headers / payload → 捕获完整异常（traceback / HTTP status / body）。

用法: /my/venv/bin/python test/test_aipai_via_class.py
"""

import json
import os
import sys
import traceback

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import requests
from pack.anthropic_chat_model import AnthropicChatModel


# ---------------------------------------------------------------------------
# 1. 读取 data/models_config.json，找到 aipai 上游
# ---------------------------------------------------------------------------
config_path = os.path.join(PROJECT_ROOT, "data", "models_config.json")
with open(config_path) as f:
    config = json.load(f)

aipai = None
for u in config["upstreams"]:
    if u["name"] == "aipai":
        aipai = u
        break

if not aipai:
    print("ERROR: aipai upstream not found in models_config.json")
    sys.exit(1)

BASE_URL = aipai["base_url"]
API_KEY = aipai["api_key"]
MESSAGES_PATH = aipai["messages_path"]  # 原始值：空字符串 ""
MODEL_NAME = "claude-opus-4-8"

print("=" * 70)
print("CONFIG (from models_config.json)")
print("=" * 70)
print(f"  base_url:      {BASE_URL}")
print(f"  api_key:       {API_KEY[:6]}...{API_KEY[-4:]}")
print(f"  messages_path: {MESSAGES_PATH!r}")
print(f"  model_name:    {MODEL_NAME}")
print()


# ---------------------------------------------------------------------------
# 2. 构造 AnthropicChatModel 实例
# ---------------------------------------------------------------------------
model = AnthropicChatModel(
    base_url=BASE_URL,
    api_key=API_KEY,
    model_name=MODEL_NAME,
    messages_path=MESSAGES_PATH,
)

print("=" * 70)
print("AnthropicChatModel INSTANCE")
print("=" * 70)
print(f"  base_url:       {model.base_url}")
print(f"  messages_path:  {model.messages_path}")
print(f"  is_openai_protocol: {model.is_openai_protocol}")
print(f"  model_name:     {model.model_name}")
print()


# ---------------------------------------------------------------------------
# 3. Monkey-patch requests.post 以捕获实际发出的 URL / headers / body / response
# ---------------------------------------------------------------------------
_original_post = requests.post
_captured = {}

def _patched_post(url, **kwargs):
    _captured["url"] = url
    _captured["headers"] = dict(kwargs.get("headers", {}))
    _captured["json_payload"] = kwargs.get("json")
    _captured["timeout"] = kwargs.get("timeout")

    # 发真实请求
    resp = _original_post(url, **kwargs)

    # 捕获响应细节（无论成功失败都记下来）
    _captured["response_status"] = resp.status_code
    _captured["response_headers"] = dict(resp.headers)
    _captured["response_body_text"] = resp.text[:5000]  # 截断以防过大
    _captured["response_body_len"] = len(resp.text)

    return resp

requests.post = _patched_post


# ---------------------------------------------------------------------------
# 4. 调 complete() 发简单消息，捕获所有异常
# ---------------------------------------------------------------------------
print("=" * 70)
print("CALLING complete() ...")
print("=" * 70)

try:
    result = model.complete(
        system_blocks="",  # 空字符串，falsy，不会被加入 payload
        messages=[{"role": "user", "content": "hello"}],
    )
except Exception as exc:
    # ── 打印完整异常信息 ──
    print()
    print("=" * 70)
    print("EXCEPTION CAUGHT")
    print("=" * 70)
    print(f"  Type:    {type(exc).__name__}")
    print(f"  Message: {exc}")
    print()

    # 尝试从异常中提取 HTTP response 细节（如 HTTPError 有 .response）
    resp = getattr(exc, "response", None)
    if resp is not None:
        print("--- HTTP Response (from exception.response) ---")
        print(f"  Status:  {resp.status_code}")
        print(f"  Headers: {json.dumps(dict(resp.headers), indent=4)}")
        try:
            body = resp.text
            print(f"  Body ({len(body)} chars):")
            print(body[:3000])
        except Exception:
            print("  (could not read response body)")
        print()

    print("--- Full Traceback ---")
    traceback.print_exc()
else:
    # ── 成功 ──
    print()
    print("=" * 70)
    print("SUCCESS")
    print("=" * 70)
    print(f"  text:        {result.text!r}")
    print(f"  stop_reason: {result.stop_reason!r}")
    print(f"  tool_calls:  {result.tool_calls}")
    print(f"  raw_content: {json.dumps(result.raw_content, indent=2, ensure_ascii=False)}")

finally:
    # ── 5. 打印实际发出的请求信息 + 响应信息 ──
    print()
    print("=" * 70)
    print("ACTUAL REQUEST (captured via monkey-patch)")
    print("=" * 70)
    if _captured:
        print(f"  URL:     {_captured.get('url')}")
        print(f"  Timeout: {_captured.get('timeout')}")
        print(f"  Headers:")
        for k, v in _captured.get("headers", {}).items():
            if any(s in k.lower() for s in ("api", "auth", "key")):
                print(f"    {k}: {v[:30]}...")
            else:
                print(f"    {k}: {v}")
        print(f"  JSON Payload:")
        payload = _captured.get("json_payload")
        if payload:
            print(json.dumps(payload, indent=4, ensure_ascii=False, default=str))
        else:
            print("    (none)")

        # 响应信息
        print()
        print("=" * 70)
        print("ACTUAL RESPONSE (captured via monkey-patch)")
        print("=" * 70)
        print(f"  Status:  {_captured.get('response_status')}")
        print(f"  Body length: {_captured.get('response_body_len')} chars")
        print(f"  Headers:")
        for k, v in _captured.get("response_headers", {}).items():
            print(f"    {k}: {v}")
        print(f"  Body:")
        body_text = _captured.get("response_body_text", "")
        if body_text.strip():
            # 尝试格式化 JSON
            try:
                print(json.dumps(json.loads(body_text), indent=4, ensure_ascii=False))
            except Exception:
                print(body_text[:3000])
        else:
            print("    (empty body)")
    else:
        print("  (no request was captured — complete() may not have reached requests.post)")
