import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TurnLogStore } from '../src/store/turn-log.js';

test('turn log store appends tool and turn logs per scope', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-turnlog-'));
  const store = new TurnLogStore(dir);
  try {
    store.appendToolLog('group', '777', {
      at: 1000,
      name: 'web_search',
      argsPreview: '{"q":"北京"}',
      resultPreview: '三条结果',
      ok: true,
      durationMs: 120,
    });
    store.appendToolLog('group', '777', {
      at: 2000,
      name: 'send_message',
      argsPreview: '{"text":"你好"}',
      resultPreview: '已发送',
      ok: false,
      durationMs: 30,
    });
    store.appendTurnLog('group', '777', {
      at: 2500,
      scopeKey: 'group:777',
      triggerPreview: '帮我搜下北京天气',
      toolsUsed: ['web_search', 'send_message'],
      sent: true,
      durationMs: 1500,
    });
    await store.flush();

    const tools = await store.readToolLogs('group', '777');
    assert.equal(tools.length, 2);
    assert.equal(tools[0]!.name, 'web_search');
    assert.equal(tools[1]!.ok, false);

    const turns = await store.readTurnLogs('group', '777');
    assert.equal(turns.length, 1);
    assert.equal(turns[0]!.triggerPreview, '帮我搜下北京天气');
    assert.deepEqual(turns[0]!.toolsUsed, ['web_search', 'send_message']);
    assert.equal(turns[0]!.sent, true);

    // 不同 scope 互相隔离
    const other = await store.readToolLogs('group', '888');
    assert.equal(other.length, 0);
    const otherTurns = await store.readTurnLogs('private', '888');
    assert.equal(otherTurns.length, 0);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('tool log file is capped at 500 lines, keeping the newest', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-turnlog-cap-'));
  const store = new TurnLogStore(dir);
  try {
    for (let i = 0; i < 520; i += 1) {
      store.appendToolLog('group', '1', {
        at: i,
        name: `tool_${i}`,
        argsPreview: '{}',
        resultPreview: '',
        ok: true,
        durationMs: 1,
      });
    }
    await store.flush();
    const logs = await store.readToolLogs('group', '1', 600);
    assert.equal(logs.length, 500);
    // 最新的在最前（保留的是末尾 500 条）
    assert.equal(logs[logs.length - 1]!.name, 'tool_519');
    assert.ok(logs.some((entry) => entry.name === 'tool_20'));
    assert.ok(!logs.some((entry) => entry.name === 'tool_19'));
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('turn logs are capped at 20 per scope', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-turnlog-turn-'));
  const store = new TurnLogStore(dir);
  try {
    for (let i = 0; i < 30; i += 1) {
      store.appendTurnLog('private', '9', {
        at: i,
        scopeKey: 'private:9',
        triggerPreview: `第 ${i} 回合`,
        toolsUsed: [],
        sent: false,
        durationMs: 10,
      });
    }
    await store.flush();
    const turns = await store.readTurnLogs('private', '9', 50);
    assert.equal(turns.length, 20);
    assert.equal(turns[turns.length - 1]!.triggerPreview, '第 29 回合');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('reading a missing log file returns an empty array instead of throwing', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-turnlog-empty-'));
  const store = new TurnLogStore(dir);
  try {
    assert.deepEqual(await store.readToolLogs('group', 'nope'), []);
    assert.deepEqual(await store.readTurnLogs('group', 'nope'), []);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
