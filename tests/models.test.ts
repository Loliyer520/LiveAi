/** Model manager / protocol: upstream flags, request body shaping. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ModelManager } from '../src/models/manager.js';
import { completeChat } from '../src/models/protocol.js';

async function makeManager(thinkingDisabled: boolean): Promise<{ manager: ModelManager; cleanup: () => Promise<void> }> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-models-'));
  const configPath = join(dir, 'models.json');
  await writeFile(configPath, JSON.stringify({
    upstreams: [{
      name: 'glm',
      base_url: 'https://8.8.8.8',
      api_key: 'sk-test',
      protocol: 'anthropic',
      thinking_disabled: thinkingDisabled,
    }],
    channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'glm', model_id: 'glm-5.3' }] }],
    roles: { main: 'main' },
  }), 'utf-8');
  const manager = new ModelManager(configPath);
  await manager.load();
  return { manager, cleanup: () => rm(dir, { recursive: true, force: true }) };
}

function stubAnthropicFetch(capture: { body?: Record<string, unknown> }): () => void {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    capture.body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    return new Response(JSON.stringify({
      content: [{ type: 'text', text: '好的' }],
      usage: { input_tokens: 10, output_tokens: 2 },
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  return () => {
    globalThis.fetch = originalFetch;
  };
}

const BASE_REQUEST = {
  system: [],
  messages: [{ role: 'user' as const, content: '你好' }],
  tools: [],
  temperature: 0.7,
  maxTokens: 1024,
  thinkingLevel: 'off' as const,
};

test('thinking_disabled upstream sends thinking:{type:disabled} when level is off', async () => {
  const { manager, cleanup } = await makeManager(true);
  const capture: { body?: Record<string, unknown> } = {};
  const restore = stubAnthropicFetch(capture);
  try {
    const model = manager.getModelForRole('main');
    assert.ok(model);
    assert.equal(model.thinkingDisabled, true);
    const reply = await completeChat(model, BASE_REQUEST);
    assert.equal(reply.text, '好的');
    assert.deepEqual(capture.body?.thinking, { type: 'disabled' });
  } finally {
    restore();
    await cleanup();
  }
});

test('upstream without the flag omits the thinking param entirely', async () => {
  const { manager, cleanup } = await makeManager(false);
  const capture: { body?: Record<string, unknown> } = {};
  const restore = stubAnthropicFetch(capture);
  try {
    const model = manager.getModelForRole('main');
    assert.ok(model);
    assert.equal(model.thinkingDisabled, false);
    await completeChat(model, BASE_REQUEST);
    assert.equal('thinking' in (capture.body ?? {}), false);
  } finally {
    restore();
    await cleanup();
  }
});

test('drop_temperature upstream omits the temperature field entirely', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-models-'));
  try {
    const configPath = join(dir, 'models.json');
    await writeFile(configPath, JSON.stringify({
      upstreams: [{
        name: 'relay',
        base_url: 'https://8.8.8.8',
        api_key: 'sk-test',
        protocol: 'anthropic',
        drop_temperature: true,
      }],
      channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'relay', model_id: 'm-strict' }] }],
      roles: { main: 'main' },
    }), 'utf-8');
    const manager = new ModelManager(configPath);
    await manager.load();
    const capture: { body?: Record<string, unknown> } = {};
    const restore = stubAnthropicFetch(capture);
    try {
      const model = manager.getModelForRole('main');
      assert.ok(model);
      assert.equal(model.dropTemperature, true);
      await completeChat(model, BASE_REQUEST);
      assert.equal('temperature' in (capture.body ?? {}), false, '整个 temperature 字段不应出现在请求体里');
    } finally {
      restore();
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('completions protocol also honors thinking_disabled (zhipu paas 端点)', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-models-'));
  try {
    const configPath = join(dir, 'models.json');
    await writeFile(configPath, JSON.stringify({
      upstreams: [{
        name: 'glm-vision',
        base_url: 'https://8.8.8.8',
        api_key: 'sk-test',
        protocol: 'completions',
        thinking_disabled: true,
      }],
      channels: [{ name: 'vision', strategy: 'fallback', models: [{ upstream: 'glm-vision', model_id: 'glm-4.5v' }] }],
      roles: { vision: 'vision' },
    }), 'utf-8');
    const manager = new ModelManager(configPath);
    await manager.load();
    const capture: { body?: Record<string, unknown> } = {};
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async (_input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      capture.body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
      return new Response(JSON.stringify({
        choices: [{ message: { content: '一只猫' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as typeof fetch;
    try {
      const model = manager.getModelForRole('vision');
      assert.ok(model);
      const reply = await completeChat(model, BASE_REQUEST);
      assert.equal(reply.text, '一只猫');
      assert.deepEqual(capture.body?.thinking, { type: 'disabled' });
      assert.equal('reasoning_effort' in (capture.body ?? {}), false);
    } finally {
      globalThis.fetch = originalFetch;
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('fallback_reset: notifyFailure advances, beginRequest returns to primary', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-models-'));
  try {
    const configPath = join(dir, 'models.json');
    await writeFile(configPath, JSON.stringify({
      upstreams: [
        { name: 'a', base_url: 'https://a.test', api_key: 'sk-a', protocol: 'anthropic' },
        { name: 'b', base_url: 'https://b.test', api_key: 'sk-b', protocol: 'anthropic' },
      ],
      channels: [{
        name: 'main',
        strategy: 'fallback_reset',
        models: [{ upstream: 'a', model_id: 'm1' }, { upstream: 'b', model_id: 'm2' }],
      }],
      roles: { main: 'main' },
    }), 'utf-8');
    const manager = new ModelManager(configPath);
    await manager.load();

    assert.equal(manager.getModelForRole('main')?.displayName, 'a/m1');
    manager.notifyFailure('main');
    assert.equal(manager.getModelForRole('main')?.displayName, 'b/m2', '失败后本轮内推进到下一个');
    manager.beginRequest('main');
    assert.equal(manager.getModelForRole('main')?.displayName, 'a/m1', '新请求必须回到首选模型');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('thinking budget is clamped strictly below max_tokens', async () => {
  const { manager, cleanup } = await makeManager(false);
  const restore = stubAnthropicFetch({});
  const capture: { body?: Record<string, unknown> } = {};
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    capture.body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    return new Response(JSON.stringify({
      content: [{ type: 'text', text: '好' }],
      usage: { input_tokens: 1, output_tokens: 1 },
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  try {
    const model = manager.getModelForRole('main');
    assert.ok(model);
    await completeChat(model, { ...BASE_REQUEST, maxTokens: 2048, thinkingLevel: 'high' });
    const thinking = capture.body?.thinking as { budget_tokens?: number } | undefined;
    assert.equal(thinking?.budget_tokens, 2047, 'budget 必须严格小于 max_tokens，否则上游 400');
  } finally {
    globalThis.fetch = originalFetch;
    restore();
    await cleanup();
  }
});
