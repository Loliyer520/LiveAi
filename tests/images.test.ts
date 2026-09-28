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
  cleanup: () => Promise<void>;
}

async function makeToolHarness(options: {
  triggerImageRefs?: string[];
  historyEntry?: { message_ref: string; image_refs: string[] };
  isMaster?: boolean;
} = {}): Promise<ToolHarness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-images-'));
  const described: { url: string; question: string }[] = [];
  const sentImages: { targetId: number; file: string }[] = [];
  const appended: Record<string, unknown>[] = [];

  const fakeBot = {
    selfId: 999,
    async sendImage(_chatType: string, targetId: number, file: string) {
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
    findEntryByRef: (_t: string, _i: string, ref: string) =>
      options.historyEntry && ref === options.historyEntry.message_ref
        ? options.historyEntry
        : null,
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
      triggerImageRefs: options.triggerImageRefs ?? [],
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
    const names = chatToolSchemas(isMaster).map((schema) => schema.name);
    for (const expected of ['view_image', 'send_image', 'create_task', 'list_tasks', 'cancel_task']) {
      assert.ok(names.includes(expected), `${expected} present (isMaster=${isMaster})`);
    }
  }
});
