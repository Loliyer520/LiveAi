#!/usr/bin/env python3
"""
测试 kirof5 渠道的 claude-opus-4-8 能否正常调用。

从 data/models_config.json 读取 kirof5 上游配置 → 构造 AnthropicChatModel
→ 发一条 hello 请求 → 输出:
  1. 请求 URL
  2. HTTP 状态码
  3. 是否成功返回 AI 回复
  4. 如果失败，输出错误信息
"""

import json
import os
import sys
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import requests
from pack.anthropic_chat_model import AnthropicChatModel

UPSTREAM_NAME = "kirof5"
MODEL_NAME = "claude-opus-4-8"


def main():
    # ── 1. 读取配置 ──
    config_path = os.path.join(PROJECT_ROOT, "data", "models_config.json")
    with open(config_path) as f:
        config = json.load(f)

    upstream = None
    for u in config["upstreams"]:
        if u["name"] == UPSTREAM_NAME:
            upstream = u
            break

    if not upstream:
        print(f"ERROR: 未找到 upstream '{UPSTREAM_NAME}'")
        sys.exit(1)

    base_url = upstream["base_url"]
    api_key = upstream["api_key"]
    # 空字符串时默认走 Anthropic 标准路径
    messages_path = upstream["messages_path"] or "/v1/messages"

    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"

    # ── 2. 构造 AnthropicChatModel ──
    model = AnthropicChatModel(
        base_url=base_url,
        api_key=api_key,
        model_name=MODEL_NAME,
        messages_path=messages_path,
    )

    # ── 3. Monkey-patch requests.post 捕获实际请求/响应 ──
    _original_post = requests.post
    _captured = {}

    def _patched_post(url, **kwargs):
        _captured["url"] = url
        _captured["headers"] = dict(kwargs.get("headers", {}))
        _captured["json_payload"] = kwargs.get("json")
        resp = _original_post(url, **kwargs)
        _captured["response_status"] = resp.status_code
        _captured["response_body_text"] = resp.text[:5000]
        return resp

    requests.post = _patched_post

    # ── 4. 发请求 ──
    success = False
    reply_text = ""
    error_info = ""

    try:
        result = model.complete(
            system_blocks="",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=64,
        )
        success = True
        reply_text = result.text
    except Exception as exc:
        error_info = f"{type(exc).__name__}: {exc}"
        # 尝试从异常中提取 HTTP response
        resp = getattr(exc, "response", None)
        if resp is not None:
            error_info += f"\n  HTTP Status: {resp.status_code}"
            try:
                error_info += f"\n  Response Body: {resp.text[:2000]}"
            except Exception:
                pass

    # ── 5. 输出结果 ──
    print("=" * 60)
    print(f"测试渠道: {UPSTREAM_NAME} / {MODEL_NAME}")
    print("=" * 60)
    print(f"1. 请求 URL: {_captured.get('url', 'N/A')}")
    print(f"2. HTTP 状态码: {_captured.get('response_status', 'N/A')}")

    if success:
        print(f"3. ✅ 成功返回 AI 回复")
        print(f"   回复内容: {reply_text!r}")
    else:
        print(f"3. ❌ 未能返回 AI 回复")
        print(f"4. 错误信息: {error_info}")

    # 补充：打印请求 payload（脱敏）
    print()
    print("─" * 60)
    print("请求详情（脱敏）")
    print("─" * 60)
    print(f"  API Key: {masked_key}")
    payload = _captured.get("json_payload")
    if payload:
        print(f"  Payload:")
        print(json.dumps(payload, indent=4, ensure_ascii=False, default=str))
    else:
        print("  Payload: (无)")

    # 原始响应（截断）
    body = _captured.get("response_body_text", "")
    if body:
        print(f"\n  响应 Body (前 2000 字符):")
        try:
            print(json.dumps(json.loads(body), indent=4, ensure_ascii=False)[:2000])
        except Exception:
            print(body[:2000])

    print()
    print("=" * 60)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
