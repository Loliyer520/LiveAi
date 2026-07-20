#!/usr/bin/env python3
"""
测试 models_config.json 中所有渠道（channel）的上游是否可用。

对每个 channel 的每个 model，发起一次最小化请求（'hello', max_tokens=32），
逐条汇报结果：✅ 正常 / ❌ 报错（含错误类型和消息）。
"""

import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from pack.anthropic_chat_model import AnthropicChatModel

CONFIG_PATH = os.path.join(PROJECT_ROOT, 'data', 'models_config.json')

TEST_MESSAGE = 'hello'
MAX_TOKENS = 32
TIMEOUT_SECONDS = 30


def mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return '***'
    return f'{key[:6]}...{key[-4:]}'


def main():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)

    upstreams = {u['name']: u for u in config['upstreams']}
    channels = config['channels']

    total = sum(len(ch.get('models', [])) for ch in channels)
    print(f'Loaded {len(upstreams)} upstreams, {len(channels)} channels, {total} model entries.\n')

    results = []
    idx = 0

    for ch in channels:
        ch_name = ch['name']
        strategy = ch.get('strategy', 'unknown')
        models = ch.get('models', [])

        for mi, m in enumerate(models):
            idx += 1
            upstream_name = m['upstream']
            model_id = m['model_id']
            up = upstreams.get(upstream_name)

            if not up:
                results.append({
                    'channel': ch_name,
                    'model_index': mi,
                    'upstream': upstream_name,
                    'model_id': model_id,
                    'status': 'SKIP',
                    'error': f'upstream "{upstream_name}" not found in config',
                })
                print(f'[{idx}/{total}] ⏭️  SKIP: channel="{ch_name}" upstream="{upstream_name}" model="{model_id}" — upstream not found')
                continue

            base_url = up['base_url']
            api_key = up['api_key']
            messages_path = up['messages_path']

            print(f'[{idx}/{total}] Testing: channel="{ch_name}" (strategy={strategy}) '
                  f'upstream="{upstream_name}" model="{model_id}"')
            print(f'       base_url={base_url}  messages_path={messages_path!r}  api_key={mask_key(api_key)}')

            try:
                model = AnthropicChatModel(
                    base_url=base_url,
                    api_key=api_key,
                    model_name=model_id,
                    messages_path=messages_path,
                )
                start = time.time()
                result = model.complete(
                    system_blocks='',
                    messages=[{'role': 'user', 'content': TEST_MESSAGE}],
                    max_tokens=MAX_TOKENS,
                )
                elapsed = time.time() - start
                text = result.text if result else ''
                results.append({
                    'channel': ch_name,
                    'model_index': mi,
                    'upstream': upstream_name,
                    'model_id': model_id,
                    'status': 'OK',
                    'elapsed_s': round(elapsed, 2),
                    'response_text': text[:200],
                })
                print(f'       ✅ OK ({elapsed:.2f}s) → {text[:120]!r}')
            except Exception as e:
                elapsed = time.time() - start if 'start' in dir() else 0
                results.append({
                    'channel': ch_name,
                    'model_index': mi,
                    'upstream': upstream_name,
                    'model_id': model_id,
                    'status': 'FAIL',
                    'elapsed_s': round(elapsed, 2),
                    'error_type': type(e).__name__,
                    'error': str(e)[:500],
                })
                print(f'       ❌ FAIL ({elapsed:.2f}s): {type(e).__name__}: {str(e)[:200]}')

    # ── Summary ──
    print()
    print('=' * 80)
    print('SUMMARY')
    print('=' * 80)
    ok_count = sum(1 for r in results if r['status'] == 'OK')
    fail_count = sum(1 for r in results if r['status'] == 'FAIL')
    skip_count = sum(1 for r in results if r['status'] == 'SKIP')
    print(f'Total: {len(results)}  |  ✅ OK: {ok_count}  |  ❌ FAIL: {fail_count}  |  ⏭️  SKIP: {skip_count}')
    print()

    # 按 channel 分组展示
    for ch in channels:
        ch_name = ch['name']
        ch_results = [r for r in results if r['channel'] == ch_name]
        print(f'── Channel: {ch_name} (strategy={ch.get("strategy")}) ──')
        for r in ch_results:
            icon = '✅' if r['status'] == 'OK' else '❌' if r['status'] == 'FAIL' else '⏭️'
            up = r['upstream']
            mid = r['model_id']
            if r['status'] == 'OK':
                print(f'  {icon} [{up}] {mid}  ({r["elapsed_s"]}s) → {r["response_text"][:80]!r}')
            elif r['status'] == 'FAIL':
                print(f'  {icon} [{up}] {mid}  ({r["elapsed_s"]}s) → {r["error_type"]}: {r["error"][:150]}')
            else:
                print(f'  {icon} [{up}] {mid}  → {r["error"]}')
        print()

    # 最终结论
    if fail_count == 0 and skip_count == 0:
        print('🎉 All channels passed!')
    elif fail_count > 0:
        print(f'⚠️  {fail_count} model(s) failed. Check error details above.')
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == '__main__':
    main()
