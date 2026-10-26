/** send_tts：满穗 TTS（GPT-SoVITS，frps 8020）→ QQ 语音。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { existsSync } from 'node:fs';
import { executeChatTool, type ToolContext } from '../src/chat/tools.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import { Workspace } from '../src/chat/workspace.js';
import { TaskScheduler } from '../src/chat/scheduler.js';

/** 44 字节头的最小合法 WAV（16bit 单声道 32kHz）。 */
function wavBytes(dataBytes = 3200): Buffer {
  const buffer = Buffer.alloc(44 + dataBytes);
  buffer.write('RIFF', 0, 'latin1');
  buffer.writeUInt32LE(36 + dataBytes, 4);
  buffer.write('WAVE', 8, 'latin1');
  buffer.write('fmt ', 12, 'latin1');
  buffer.writeUInt32LE(16, 16); // fmt chunk size
  buffer.writeUInt16LE(1, 20); // PCM
  buffer.writeUInt16LE(1, 22); // mono
  buffer.writeUInt32LE(32000, 24); // sample rate
  buffer.writeUInt32LE(64000, 28); // byte rate
  buffer.writeUInt16LE(2, 32); // block align
  buffer.writeUInt16LE(16, 34); // bits per sample
  buffer.write('data', 36, 'latin1');
  buffer.writeUInt32LE(dataBytes, 40);
  return buffer;
}

interface Harness {
  context: ToolContext;
  sentRecords: { targetId: number; file: string }[];
  requestedUrls: string[];
  cleanup: () => Promise<void>;
}

async function makeHarness(body: () => Buffer | string): Promise<Harness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-tts-'));
  const sentRecords: { targetId: number; file: string }[] = [];
  const requestedUrls: string[] = [];
  const appended: Record<string, unknown>[] = [];

  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0]) => {
    requestedUrls.push(String(input instanceof Request ? input.url : input));
    const payload = body();
    return new Response(typeof payload === 'string' ? payload : new Uint8Array(payload), {
      status: 200,
      headers: { 'content-type': typeof payload === 'string' ? 'application/json' : 'audio/wav' },
    });
  }) as typeof fetch;

  const fakeBot = {
    selfId: 999,
    selfNickname: '测试冰',
    async sendRecord(_chatType: string, targetId: number, file: string) {
      sentRecords.push({ targetId, file });
      return { message_id: 7777 };
    },
  } as unknown as NapcatBot;

  const context: ToolContext = {
    bot: fakeBot,
    repo: {
      registerMessageRef: () => 'T9T9',
      appendMessage: (_t: string, _i: string, entry: Record<string, unknown>) => {
        appended.push(entry);
        return false;
      },
    } as never,
    archive: null as never,
    relations: null as never,
    tasks: new TaskScheduler(join(dir, 'tasks.json'), () => {}),
    masterQq: 1,
    isMaster: false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    peerInject: () => undefined,
    workspace: new Workspace(dir),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
    describeImage: async () => '',
    scopeType: 'private',
    scopeId: '3319085316',
    executedTools: [],
  };
  return {
    context,
    sentRecords,
    requestedUrls,
    cleanup: async () => {
      globalThis.fetch = originalFetch;
      await rm(dir, { recursive: true, force: true });
    },
  };
}

test('send_tts synthesizes WAV, sends it as record, and logs [语音] into history', async () => {
  // 128000B 数据 @32000Hz·16bit ≈ 2 秒，足够让时长断言有区分度
  const harness = await makeHarness(() => wavBytes(128_000));
  try {
    const ok = await executeChatTool({ name: 'send_tts', arguments: { text: '主人你好呀', emotion: 'narration', speed: 1.2, pause: 0.8, temperature: 1.1 } }, harness.context);
    assert.equal(ok.ok, true, ok.output);
    assert.equal(ok.sentToUser, true);
    // 请求带上了编码后的文本 + 情感 + 语速 + 停顿 + 表现力
    assert.ok(harness.requestedUrls.at(-1)?.includes('text='), harness.requestedUrls.at(-1));
    assert.ok(harness.requestedUrls.at(-1)?.includes('emotion=narration'));
    assert.ok(harness.requestedUrls.at(-1)?.includes('speed=1.2'));
    assert.ok(harness.requestedUrls.at(-1)?.includes('fragment_interval=0.8'));
    assert.ok(harness.requestedUrls.at(-1)?.includes('temperature=1.1'));
    // WAV 落进了会话工作区，并按 record 段发出
    assert.equal(harness.sentRecords.length, 1);
    assert.equal(harness.sentRecords[0].targetId, 3319085316);
    const sent = harness.sentRecords[0].file;
    assert.ok(sent.startsWith('file://'), sent);
    assert.ok(sent.endsWith('.wav'));
    assert.ok(existsSync(sent.slice('file://'.length)));
    assert.ok(ok.output.includes('短ID T9T9'));
    // 时长按 WAV 头算（128000B 数据 @32000Hz·16bit ≈ 2 秒）
    assert.ok(ok.output.includes('约 2 秒'), ok.output);
  } finally {
    await harness.cleanup();
  }
});

test('send_tts rejects >300 chars and unknown emotions before any network call', async () => {
  const harness = await makeHarness(() => wavBytes());
  try {
    const tooLong = await executeChatTool({ name: 'send_tts', arguments: { text: '长'.repeat(301) } }, harness.context);
    assert.equal(tooLong.ok, false);
    assert.ok(tooLong.output.includes('300'));

    const badEmotion = await executeChatTool({ name: 'send_tts', arguments: { text: '你好', emotion: '开心点' } }, harness.context);
    assert.equal(badEmotion.ok, false);
    assert.ok(badEmotion.output.includes('narration'));

    assert.equal(harness.requestedUrls.length, 0, 'rejected before fetching');
    assert.equal(harness.sentRecords.length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('send_tts surfaces a JSON error body instead of sending garbage as voice', async () => {
  const harness = await makeHarness(() => JSON.stringify({ detail: 'gpt-sovits offline' }));
  try {
    const result = await executeChatTool({ name: 'send_tts', arguments: { text: '你好' } }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('gpt-sovits offline'), result.output);
    assert.ok(result.output.includes('不是音频'));
    assert.equal(harness.sentRecords.length, 0);
    // 不传 temperature 时落主人调定的日常档 0.6
    assert.ok(harness.requestedUrls.at(-1)?.includes('temperature=0.6'), harness.requestedUrls.at(-1));
  } finally {
    await harness.cleanup();
  }
});
