/**
 * Image pipeline tests: CQ/segment ref extraction, the view_image tool
 * (turn stash + message_ref lookup + vision describe), send_image, and the
 * task tools (create_task set_alarm/recurring, list/cancel, scope gating).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { extractImageRefs } from '../src/chat/images.js';
import { executeChatTool, chatToolSchemas, type ToolContext } from '../src/chat/tools.js';
import { TaskScheduler } from '../src/chat/scheduler.js';
import type { ChatMessage } from '../src/chat/types.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import { Workspace } from '../src/chat/workspace.js';
import { configureImageCache, ensureImageCached } from '../src/chat/image-cache.js';
import { setDnsResolverForTests } from '../src/chat/net.js';

function message(rawMessage: string, rawData: Record<string, unknown> = {}): ChatMessage {
  return {
    chatType: 'group',
    chatId: 777,
    userId: 100,
    text: '',
    rawMessage,
    sender: { nickname: 'tester', user_id: 100 },
    messageId: 1,
    mentionsSelf: false,
    timestamp: Date.now() / 1000,
    rawData,
  };
}

// ── extraction ──────────────────────────────────────────────────────────────

test('extractImageRefs parses CQ:image codes (url param preferred)', () => {
  const refs = extractImageRefs(
    message('看看这个 [CQ:image,file=abc.jpg,url=https://img.example.com/a.jpg] 怎么样'),
  );
  assert.deepEqual(refs, ['https://img.example.com/a.jpg']);
});

test('extractImageRefs falls back to http file= param (go-cqhttp style)', () => {
  const refs = extractImageRefs(
    message('[CQ:image,file=https://gchat.qpic.cn/download?xxx,summary=[图片]]'),
  );
  assert.deepEqual(refs, ['https://gchat.qpic.cn/download?xxx']);
});

test('extractImageRefs reads OneBot array segments', () => {
  const refs = extractImageRefs(
    message('', {
      message: [
        { type: 'text', data: { text: '看图' } },
        { type: 'image', data: { url: 'https://img.example.com/seg.png', file: 'seg.png' } },
      ],
    }),
  );
  assert.deepEqual(refs, ['https://img.example.com/seg.png']);
});

test('extractImageRefs dedupes and ignores non-http files and plain text', () => {
  assert.deepEqual(extractImageRefs(message('[CQ:image,file=/local/path/x.jpg]')), []);
  assert.deepEqual(extractImageRefs(message('没有图片')), []);
  const dup = extractImageRefs(
    message('[CQ:image,url=https://a.cn/1.jpg][CQ:image,url=https://a.cn/1.jpg][CQ:image,url=https://a.cn/2.jpg]'),
  );
  assert.deepEqual(dup, ['https://a.cn/1.jpg', 'https://a.cn/2.jpg']);
});

test('extractImageRefs undoes CQ escaping inside params', () => {
  const refs = extractImageRefs(
    message('[CQ:image,url=https://a.cn/x?a&#44;b&amp;c]'),
  );
  assert.deepEqual(refs, ['https://a.cn/x?a,b&c']);
});

// ── tool harness ────────────────────────────────────────────────────────────

interface ToolHarness {
  context: ToolContext;
  described: { url: string; question: string }[];
  sentImages: { targetId: number; file: string }[];
  getImageCalls: string[];
  cleanup: () => Promise<void>;
}

async function makeToolHarness(options: {
  triggerImageRefs?: string[];
  triggerVideoRefs?: string[];
  historyEntry?: { message_ref: string; image_refs: string[]; raw_message?: string };
  otherHistoryEntry?: { message_ref: string; image_refs: string[]; raw_message?: string };
  napcatImageFile?: string;
  sendImageFailures?: number;
  isMaster?: boolean;
} = {}): Promise<ToolHarness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-images-'));
  const described: { url: string; question: string }[] = [];
  const sentImages: { targetId: number; file: string }[] = [];
  const getImageCalls: string[] = [];
  let sendImageFailures = options.sendImageFailures ?? 0;
  const appended: Record<string, unknown>[] = [];

  const fakeBot = {
    selfId: 999,
    async getImage(file: string) {
      getImageCalls.push(file);
      return { file: options.napcatImageFile ?? '' };
    },
    async sendImage(_chatType: string, targetId: number, file: string) {
      if (sendImageFailures > 0) {
        sendImageFailures -= 1;
        throw new Error('temporary NapCat failure');
      }
      sentImages.push({ targetId, file });
      return { message_id: 4242 };
    },
  } as unknown as NapcatBot;

  const fakeRepo = {
    resolveRef: () => null,
    registerMessageRef: () => 'B2C3',
    appendMessage: (_t: string, _i: string, entry: Record<string, unknown>) => {
      appended.push(entry);
      return false;
    },
    recentNotes: () => [],
    addNote: () => {},
    recentImageRefs: (_t: string, _i: string, limit: number) =>
      options.historyEntry && limit > 0
        ? [
            { ref: options.historyEntry.message_ref, ageMinutes: 1 },
            ...(options.otherHistoryEntry && limit > 1
              ? [{ ref: options.otherHistoryEntry.message_ref, ageMinutes: 2 }]
              : []),
          ]
        : [],
    findEntryByRef: (_t: string, _i: string, ref: string) => {
      if (options.historyEntry && ref === options.historyEntry.message_ref) return options.historyEntry;
      if (options.otherHistoryEntry && ref === options.otherHistoryEntry.message_ref) return options.otherHistoryEntry;
      return null;
    },
    lastAppended: appended,
  };

  return {
    context: {
      bot: fakeBot,
      repo: fakeRepo as never,
      archive: {} as never,
      relations: {} as never,
      tasks: new TaskScheduler(join(dir, 'tasks.json'), () => {}),
      masterQq: 1,
      isMaster: options.isMaster ?? false,
      notifyMaster: () => {},
      delegateToChild: () => 't',
      sendToScope: async () => 'ok',
      peerInject: () => undefined,
      triggerImageRefs: options.triggerImageRefs ?? [],
          triggerVideoRefs: options.triggerVideoRefs ?? [],
      describeImage: async (url, question) => {
        described.push({ url, question });
        return `描述结果：${url} 是一只猫`;
      },
      scopeType: 'group',
      scopeId: '777',
      executedTools: [],
      workspace: new Workspace('/tmp/liveai-test-workspace'),
      networkBudget: { used: 0 },
    },
    described,
    sentImages,
    getImageCalls,
    cleanup: async () => rm(dir, { recursive: true, force: true }),
  };
}

// ── view_image ──────────────────────────────────────────────────────────────

test('view_image views the current turn image by default (index from 1)', async () => {
  const harness = await makeToolHarness({ triggerImageRefs: ['https://a.cn/1.jpg', 'https://a.cn/2.jpg'] });
  try {
    const result = await executeChatTool(
      { name: 'view_image', arguments: {} },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.ok(result.output.includes('1/2'));
    assert.deepEqual(harness.described[0].url, 'https://a.cn/1.jpg');

    const second = await executeChatTool(
      { name: 'view_image', arguments: { index: 2, question: '图里有什么文字？' } },
      harness.context,
    );
    assert.equal(second.ok, true);
    assert.deepEqual(harness.described[1], { url: 'https://a.cn/2.jpg', question: '图里有什么文字？' });

    const overflow = await executeChatTool(
      { name: 'view_image', arguments: { index: 3 } },
      harness.context,
    );
    assert.equal(overflow.ok, false);
    assert.ok(overflow.output.includes('超出范围'));
  } finally {
    await harness.cleanup();
  }
});

test('view_image resolves message_ref against history entries', async () => {
  const harness = await makeToolHarness({
    historyEntry: { message_ref: 'A1B2', image_refs: ['https://a.cn/old.jpg'] },
  });
  try {
    const result = await executeChatTool(
      { name: 'view_image', arguments: { message_ref: '[#A1B2]' } },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.ok(result.output.includes('消息 [#A1B2]'));
    assert.deepEqual(harness.described[0].url, 'https://a.cn/old.jpg');

    const missing = await executeChatTool(
      { name: 'view_image', arguments: { message_ref: 'ZZZZ' } },
      harness.context,
    );
    assert.equal(missing.ok, false);
    assert.ok(missing.output.includes('没有找到'));
  } finally {
    await harness.cleanup();
  }
});

test('view_image without any image explains what to do', async () => {
  const harness = await makeToolHarness();
  try {
    const result = await executeChatTool({ name: 'view_image', arguments: {} }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('没有图片'));
  } finally {
    await harness.cleanup();
  }
});

test('view_image_raw reports failure instead of claiming an unavailable image was shown', async () => {
  const harness = await makeToolHarness({ triggerImageRefs: ['https://img.example.com/missing.png'] });
  const originalFetch = globalThis.fetch;
  configureImageCache('');
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  globalThis.fetch = (async () => { throw new Error('expired URL'); }) as typeof fetch;
  try {
    const result = await executeChatTool({ name: 'view_image_raw', arguments: {} }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('没有把图片呈现给你'));
    assert.equal(result.inlineImageUrls, undefined);
  } finally {
    globalThis.fetch = originalFetch;
    setDnsResolverForTests(null);
    await harness.cleanup();
  }
});

// ── send_image ──────────────────────────────────────────────────────────────

test('send_image passes the file through to the bot and persists an entry', async () => {
  const harness = await makeToolHarness();
  try {
    const result = await executeChatTool(
      { name: 'send_image', arguments: { file: 'https://a.cn/meme.jpg' } },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.ok(result.output.includes('短ID B2C3'));
    assert.deepEqual(harness.sentImages, [{ targetId: 777, file: 'https://a.cn/meme.jpg' }]);

    const rejected = await executeChatTool(
      { name: 'send_image', arguments: { file: 'not-a-url' } },
      harness.context,
    );
    assert.equal(rejected.ok, false);
    assert.equal(harness.sentImages.length, 1, 'invalid file never reaches the bot');
  } finally {
    await harness.cleanup();
  }
});

test('send_image without file returns the current or latest historical image', async () => {
  const current = await makeToolHarness({ triggerImageRefs: ['https://a.cn/current.jpg'] });
  const historical = await makeToolHarness({
    historyEntry: { message_ref: 'A1B2', image_refs: ['https://a.cn/old-1.jpg', 'https://a.cn/old-2.jpg'] },
  });
  try {
    const currentResult = await executeChatTool({ name: 'send_image', arguments: {} }, current.context);
    assert.equal(currentResult.ok, true);
    assert.deepEqual(current.sentImages, [{ targetId: 777, file: 'https://a.cn/current.jpg' }]);

    // 「再发给我」通常是没有图片的新回合，应自动回看最近带图消息。
    const latestResult = await executeChatTool({ name: 'send_image', arguments: {} }, historical.context);
    assert.equal(latestResult.ok, true);
    assert.deepEqual(historical.sentImages, [{ targetId: 777, file: 'https://a.cn/old-1.jpg' }]);

    const selectedResult = await executeChatTool(
      { name: 'send_image', arguments: { message_ref: '[#A1B2]', index: 2 } },
      historical.context,
    );
    assert.equal(selectedResult.ok, true);
    assert.equal(historical.sentImages[1].file, 'https://a.cn/old-2.jpg');
  } finally {
    await current.cleanup();
    await historical.cleanup();
  }
});

test('send_image prefers an explicit current-turn image even with multiple historical candidates', async () => {
  const harness = await makeToolHarness({
    triggerImageRefs: ['https://a.cn/current.jpg'],
    historyEntry: { message_ref: 'ZX42', image_refs: ['https://a.cn/background.jpg'] },
    otherHistoryEntry: { message_ref: 'PTSR', image_refs: ['https://a.cn/blue-maid.jpg'] },
  });
  try {
    const result = await executeChatTool({ name: 'send_image', arguments: {} }, harness.context);
    assert.equal(result.ok, true);
    assert.ok(result.output.includes('本轮消息'));
    assert.deepEqual(harness.sentImages, [{ targetId: 777, file: 'https://a.cn/current.jpg' }]);
  } finally {
    await harness.cleanup();
  }
});

test('send_image allows retry after failure, then blocks only a successful duplicate', async () => {
  const harness = await makeToolHarness({ sendImageFailures: 1 });
  try {
    const call = { name: 'send_image', arguments: { file: 'https://a.cn/retry.jpg' } };
    const failed = await executeChatTool(call, harness.context);
    assert.equal(failed.ok, false);
    const retried = await executeChatTool(call, harness.context);
    assert.equal(retried.ok, true, 'a failed send must not be recorded as delivered');
    const duplicate = await executeChatTool(call, harness.context);
    assert.equal(duplicate.ok, false);
    assert.ok(duplicate.output.includes('已经'));
    assert.equal(harness.sentImages.length, 1);
  } finally {
    await harness.cleanup();
  }
});

test('send_image NapCat fallback matches the file ID by URL for multi-image messages', async () => {
  const harness = await makeToolHarness({
    historyEntry: {
      message_ref: 'MULT',
      image_refs: ['https://a.cn/first.jpg', 'https://a.cn/second.jpg'],
      raw_message: '[CQ:image,file=first-id.jpg,url=https://a.cn/first.jpg][CQ:image,file=second-id.jpg,url=https://a.cn/second.jpg]',
    },
    napcatImageFile: '/napcat/cache/second.jpg',
  });
  try {
    configureImageCache('');
    const result = await executeChatTool(
      { name: 'send_image', arguments: { message_ref: 'MULT', index: 2 } },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.deepEqual(harness.getImageCalls, ['second-id.jpg']);
  } finally {
    await harness.cleanup();
  }
});

test('send_image refuses ambiguous no-arg selection and sends the explicitly referenced image', async () => {
  const harness = await makeToolHarness({
    historyEntry: { message_ref: 'ZX42', image_refs: ['https://a.cn/background.jpg'] },
    otherHistoryEntry: { message_ref: 'PTSR', image_refs: ['https://a.cn/blue-maid.jpg'] },
  });
  try {
    const ambiguous = await executeChatTool({ name: 'send_image', arguments: {} }, harness.context);
    assert.equal(ambiguous.ok, false);
    assert.ok(ambiguous.output.includes('ZX42'));
    assert.ok(ambiguous.output.includes('PTSR'));
    assert.equal(harness.sentImages.length, 0, 'ambiguous selection must not silently return the latest image');

    const selected = await executeChatTool(
      { name: 'send_image', arguments: { message_ref: '#PTSR' } },
      harness.context,
    );
    assert.equal(selected.ok, true);
    assert.deepEqual(harness.sentImages, [{ targetId: 777, file: 'https://a.cn/blue-maid.jpg' }]);
  } finally {
    await harness.cleanup();
  }
});

test('send_image falls back to the NapCat file id when an old URL and cache are unavailable', async () => {
  const harness = await makeToolHarness({
    historyEntry: {
      message_ref: 'A1B2',
      image_refs: ['https://multimedia.nt.qq.com.cn/expired'],
      raw_message: '[CQ:image,file=77A4F54C8D6F9EDCA870BEC7F6158DA2.png,url=https://multimedia.nt.qq.com.cn/expired]',
    },
    napcatImageFile: '/napcat/cache/original.png',
  });
  try {
    configureImageCache('');
    const result = await executeChatTool({ name: 'send_image', arguments: {} }, harness.context);
    assert.equal(result.ok, true);
    assert.deepEqual(harness.getImageCalls, ['77A4F54C8D6F9EDCA870BEC7F6158DA2.png']);
    assert.deepEqual(harness.sentImages, [{ targetId: 777, file: 'file:///napcat/cache/original.png' }]);
  } finally {
    await harness.cleanup();
  }
});

test('send_image without file or any available image fails honestly', async () => {
  const harness = await makeToolHarness();
  try {
    const result = await executeChatTool({ name: 'send_image', arguments: {} }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('没有图片'));
    assert.equal(harness.sentImages.length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('send_image sends cached inbound bytes instead of making NapCat download the QQ URL again', async () => {
  const harness = await makeToolHarness();
  const cacheDir = await mkdtemp(join(tmpdir(), 'liveai-send-cache-'));
  const originalFetch = globalThis.fetch;
  const url = 'https://img.example.com/original.png';
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC',
    'base64',
  );
  configureImageCache(cacheDir);
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  globalThis.fetch = (async () => new Response(png, {
    status: 200,
    headers: { 'content-type': 'image/png' },
  })) as typeof fetch;
  try {
    assert.equal(await ensureImageCached(url), true);
    globalThis.fetch = (async () => {
      throw new Error('send_image must not download the cached URL again');
    }) as typeof fetch;
    const result = await executeChatTool(
      { name: 'send_image', arguments: { file: url } },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.equal(harness.sentImages.length, 1);
    assert.ok(harness.sentImages[0].file.startsWith('base64://'));
    assert.equal(
      Buffer.from(harness.sentImages[0].file.slice('base64://'.length), 'base64').equals(png),
      true,
    );
  } finally {
    globalThis.fetch = originalFetch;
    setDnsResolverForTests(null);
    configureImageCache('');
    await rm(cacheDir, { recursive: true, force: true });
    await harness.cleanup();
  }
});

// ── task tools ──────────────────────────────────────────────────────────────

test('schedule_image rejects the exact 24h boundary that would outlive its cache', async () => {
  const harness = await makeToolHarness({ triggerImageRefs: ['https://a.cn/current.jpg'] });
  try {
    const result = await executeChatTool(
      { name: 'schedule_image', arguments: { at: '+1d' } },
      harness.context,
    );
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('23 小时 59 分钟'));
    assert.equal(harness.context.tasks.list().length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('image tools fail cleanly in virtual scopes instead of sending to NaN', async () => {
  const harness = await makeToolHarness();
  harness.context.scopeType = 'domain';
  harness.context.scopeId = 'mc';
  try {
    const sent = await executeChatTool(
      { name: 'send_image', arguments: { file: 'https://a.cn/image.jpg' } },
      harness.context,
    );
    const scheduled = await executeChatTool(
      { name: 'schedule_image', arguments: { at: '+10m' } },
      harness.context,
    );
    assert.equal(sent.ok, false);
    assert.equal(scheduled.ok, false);
    assert.equal(harness.sentImages.length, 0);
  } finally {
    await harness.cleanup();
  }
});

// ── task tools ──────────────────────────────────────────────────────────────

test('create_task set_alarm persists through the scheduler', async () => {
  const harness = await makeToolHarness();
  try {
    const result = await executeChatTool(
      { name: 'create_task', arguments: { kind: 'set_alarm', at: '+5m', note: '提醒他吃药' } },
      harness.context,
    );
    assert.equal(result.ok, true);
    assert.ok(/task alarm_/.test(result.output));
    const pending = harness.context.tasks.list('group:777');
    assert.equal(pending.length, 1);
    assert.equal(pending[0].note, '提醒他吃药');

    const bad = await executeChatTool(
      { name: 'create_task', arguments: { kind: 'set_alarm', at: '昨天', note: 'x' } },
      harness.context,
    );
    assert.equal(bad.ok, false);
  } finally {
    harness.context.tasks.stop();
    await harness.cleanup();
  }
});

test('create_task recurring_task + list_tasks + cancel_task', async () => {
  const harness = await makeToolHarness();
  try {
    const created = await executeChatTool(
      { name: 'create_task', arguments: { kind: 'recurring_task', every: '+6h', note: '每6小时看看群里有没人@我' } },
      harness.context,
    );
    assert.equal(created.ok, true);
    const taskId = /task (recur_\w+)/.exec(created.output)![1];
    assert.ok(created.output.includes('6小时'));

    const tooShort = await executeChatTool(
      { name: 'create_task', arguments: { kind: 'recurring_task', every: '+10s', note: 'x' } },
      harness.context,
    );
    assert.equal(tooShort.ok, false, 'recurring interval below 1 minute rejected');

    const listed = await executeChatTool({ name: 'list_tasks', arguments: {} }, harness.context);
    assert.equal(listed.ok, true);
    assert.ok(listed.output.includes(taskId));
    assert.ok(listed.output.includes('周期'));

    const cancelled = await executeChatTool(
      { name: 'cancel_task', arguments: { task_id: taskId } },
      harness.context,
    );
    assert.equal(cancelled.ok, true);
    assert.equal(harness.context.tasks.list().length, 0);

    const gone = await executeChatTool(
      { name: 'cancel_task', arguments: { task_id: taskId } },
      harness.context,
    );
    assert.equal(gone.ok, false);
  } finally {
    harness.context.tasks.stop();
    await harness.cleanup();
  }
});

test('child cannot cancel another scope\'s task; master can', async () => {
  const harness = await makeToolHarness();
  const master = await makeToolHarness({ isMaster: true });
  try {
    // Task owned by group:999, but the child context is group:777.
    const foreignId = harness.context.tasks.scheduleAlarm('group:999', Date.now() / 1000 + 600, '别scope的闹钟');
    const denied = await executeChatTool(
      { name: 'cancel_task', arguments: { task_id: foreignId } },
      harness.context,
    );
    assert.equal(denied.ok, false);
    assert.ok(denied.output.includes('不属于本会话'));

    // Master sees and cancels tasks from every scope (same scheduler instance).
    const masterContext = { ...master.context, tasks: harness.context.tasks };
    const masterList = await executeChatTool({ name: 'list_tasks', arguments: {} }, masterContext);
    assert.ok(masterList.output.includes('group:999'), 'master list spans scopes');
    const masterCancel = await executeChatTool(
      { name: 'cancel_task', arguments: { task_id: foreignId } },
      masterContext,
    );
    assert.equal(masterCancel.ok, true);
  } finally {
    harness.context.tasks.stop();
    master.context.tasks.stop();
    await harness.cleanup();
    await master.cleanup();
  }
});

test('tool schemas include the image + task tools for both roles', () => {
  for (const isMaster of [false, true]) {
    const names = chatToolSchemas('private:123456789', isMaster).map((schema) => schema.name);
    for (const expected of ['view_image', 'send_image', 'schedule_image', 'create_task', 'list_tasks', 'cancel_task']) {
      assert.ok(names.includes(expected), `${expected} present (isMaster=${isMaster})`);
    }
  }
});
