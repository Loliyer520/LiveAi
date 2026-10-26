/** 用量统计：计数/分桶/快照，以及 completeChatTracked 包装的成功与失败记账。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { UsageTracker, completeChatTracked, formatUsageText, usageStats } from '../src/models/usage.js';
import type { ResolvedModel } from '../src/models/types.js';

const MODEL: ResolvedModel = {
  baseUrl: 'https://stub.test',
  apiKey: 'sk-x',
  modelName: 'm1',
  messagesPath: '/v1/messages',
  protocol: 'anthropic',
  displayName: 'a/m1',
  channelName: 'main',
  upstreamName: 'a',
  thinkingDisabled: false,
  dropTemperature: false,
};

const REQUEST = {
  system: [],
  messages: [{ role: 'user' as const, content: '你好' }],
  tools: [],
  temperature: 0.7,
  maxTokens: 64,
  thinkingLevel: 'off' as const,
};

test('tracker: success/failure accumulate into total + role + model buckets', () => {
  const tracker = new UsageTracker();
  tracker.recordSuccess('main', 'a/m1', { text: '好', toolCalls: [], usage: { inputTokens: 100, outputTokens: 5, cacheReadTokens: 80, cacheWriteTokens: 20 } });
  tracker.recordSuccess('summary', 'a/m1', { text: '摘', toolCalls: [], usage: { inputTokens: 40, outputTokens: 10 } });
  tracker.recordFailure('main', 'a/m1', new Error('boom'));

  const snap = tracker.snapshot();
  assert.deepEqual(snap.total, { requests: 3, failures: 1, inputTokens: 140, outputTokens: 15, cacheReadTokens: 80, cacheWriteTokens: 20 });
  assert.deepEqual(snap.byRole.main, { requests: 2, failures: 1, inputTokens: 100, outputTokens: 5, cacheReadTokens: 80, cacheWriteTokens: 20 });
  assert.deepEqual(snap.byModel['a/m1'], snap.total, 'model 桶与 total 同形');
  assert.equal(snap.lastError?.message, 'boom');
  assert.equal(snap.lastError?.role, 'main');

  tracker.reset();
  assert.equal(tracker.snapshot().total.requests, 0);
  assert.equal(tracker.snapshot().lastError, null);
});

test('tracker: missing usage counts as zero tokens, snapshot is a copy', () => {
  const tracker = new UsageTracker();
  tracker.recordSuccess('vision', 'a/m1', { text: '图', toolCalls: [] });
  const snap = tracker.snapshot();
  assert.equal(snap.total.inputTokens, 0);
  snap.total.requests = 999;
  assert.equal(tracker.snapshot().total.requests, 1, '改快照不影响内部状态');
});

test('completeChatTracked records success with usage from the reply', async () => {
  usageStats.reset();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    content: [{ type: 'text', text: '好的' }],
    usage: { input_tokens: 321, output_tokens: 7 },
  }), { status: 200, headers: { 'content-type': 'application/json' } })) as typeof fetch;
  try {
    const reply = await completeChatTracked('main', MODEL, REQUEST);
    assert.equal(reply.text, '好的');
    const snap = usageStats.snapshot();
    assert.equal(snap.total.requests, 1);
    assert.equal(snap.total.failures, 0);
    assert.equal(snap.byRole.main.inputTokens, 321);
    assert.equal(snap.byRole.main.outputTokens, 7);
  } finally {
    globalThis.fetch = originalFetch;
    usageStats.reset();
  }
});

test('completeChatTracked records failure then rethrows', async () => {
  usageStats.reset();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response('{"error":{"message":"预扣费额度失败"}}', {
    status: 403,
    headers: { 'content-type': 'application/json' },
  })) as typeof fetch;
  try {
    await assert.rejects(() => completeChatTracked('main', MODEL, REQUEST));
    const snap = usageStats.snapshot();
    assert.equal(snap.total.requests, 1);
    assert.equal(snap.total.failures, 1);
    assert.ok(snap.lastError?.message.includes('403'));
  } finally {
    globalThis.fetch = originalFetch;
    usageStats.reset();
  }
});

test('formatUsageText renders totals, buckets and last error for the AI tool', () => {
  const tracker = new UsageTracker();
  const empty = formatUsageText(tracker.snapshot());
  assert.ok(empty.includes('还没有任何模型请求'));

  tracker.recordSuccess('main', 'a/m1', { text: '好', toolCalls: [], usage: { inputTokens: 2400, outputTokens: 120 } });
  tracker.recordFailure('summary', 'a/m1', new Error('empty content without tool calls (stop=end_turn)'));
  const text = formatUsageText(tracker.snapshot());
  assert.ok(text.includes('合计：2 次（失败 1） · 输入 2.4k · 输出 120'));
  assert.ok(text.includes('角色 main：1 次'));
  assert.ok(text.includes('角色 summary：1 次（失败 1）'));
  assert.ok(text.includes('模型 a/m1：2 次'));
  assert.ok(text.includes('最近错误（summary'));
  assert.ok(text.includes('empty content'));
});

test('usage_stats tool is master-only and returns the formatted report', async () => {
  const { chatToolSchemas, executeChatTool } = await import('../src/chat/tools.js');
  const masterTools = chatToolSchemas('private:241898129', true).map((t) => t.name);
  const childTools = chatToolSchemas('group:1', false).map((t) => t.name);
  assert.ok(masterTools.includes('usage_stats'), '主AI有');
  assert.ok(!childTools.includes('usage_stats'), '子AI没有');

  usageStats.reset();
  usageStats.recordSuccess('main', 'a/m1', { text: '好', toolCalls: [], usage: { inputTokens: 500, outputTokens: 20 } });
  try {
    const result = await executeChatTool(
      { name: 'usage_stats', arguments: {} },
      { bot: {} as never, executedTools: [] as string[] } as never,
    );
    assert.ok((result as { output: string }).output.includes('合计：1 次 · 输入 500 · 输出 20'));
  } finally {
    usageStats.reset();
  }
});
