/** send_sticker：号主收藏表情库（data/stickers/index.json）→ QQ 表情气泡；自动同步打标。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { executeChatTool, configureStickers, parseStickerCaption, refreshStickerIndex, type ToolContext } from '../src/chat/tools.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import { Workspace } from '../src/chat/workspace.js';
import { TaskScheduler } from '../src/chat/scheduler.js';

// URL 内嵌 MD5 形态与线上收藏表情一致（…_0_0_0_<32HEX>_0_0/0）
const md5Url = (md5: string): string => `https://p.qpic.cn/qq_expression/9/9_0_0_0_${md5}_0_0/0`;

const STICKERS = [
  { md5: 'A'.repeat(32), url: md5Url('A'.repeat(32)), desc: '一只翻白眼的橘猫', tags: ['无语', '猫猫', '白眼'] },
  { md5: 'B'.repeat(32), url: md5Url('B'.repeat(32)), desc: '小女孩开心鼓掌', tags: ['开心', '鼓掌', '加油'] },
  { md5: 'C'.repeat(32), url: md5Url('C'.repeat(32)), desc: '流泪的卡通脸', tags: ['哭', '难过', '委屈'] },
];

interface HarnessOpts {
  indexEntries?: typeof STICKERS | { md5: string; url: string; desc: string; tags: string[] }[];
  fetchCustomFace?: string[];
  describeImage?: (url: string, question: string) => Promise<string>;
  isMaster?: boolean;
}

interface Harness {
  context: ToolContext;
  dir: string;
  sentStickers: { chatType: string; targetId: number; file: string }[];
  appended: Record<string, unknown>[];
  cleanup: () => Promise<void>;
}

async function makeHarness(opts: HarnessOpts = {}): Promise<Harness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sticker-'));
  if (opts.indexEntries) {
    await writeFile(join(dir, 'index.json'), JSON.stringify(opts.indexEntries), 'utf-8');
  }
  configureStickers(join(dir, 'index.json'));

  const sentStickers: { chatType: string; targetId: number; file: string }[] = [];
  const appended: Record<string, unknown>[] = [];
  const fakeBot = {
    selfId: 999,
    selfNickname: '测试冰',
    async sendSticker(chatType: string, targetId: number, file: string) {
      sentStickers.push({ chatType, targetId, file });
      return { message_id: 8888 };
    },
    fetchCustomFace: async () => opts.fetchCustomFace ?? [],
  } as unknown as NapcatBot;

  const context: ToolContext = {
    bot: fakeBot,
    repo: {
      registerMessageRef: () => 'S7S7',
      appendMessage: (_t: string, _i: string, entry: Record<string, unknown>) => {
        appended.push(entry);
        return false;
      },
    } as never,
    archive: null as never,
    relations: null as never,
    tasks: new TaskScheduler(join(dir, 'tasks.json'), () => {}),
    masterQq: 1,
    isMaster: opts.isMaster ?? false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    peerInject: () => undefined,
    workspace: new Workspace(dir),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
    describeImage: opts.describeImage ?? (async () => ''),
    scopeType: 'private',
    scopeId: '3319085316',
    executedTools: [],
  };
  return {
    context,
    dir,
    sentStickers,
    appended,
    cleanup: async () => {
      await rm(dir, { recursive: true, force: true });
    },
  };
}

test('send_sticker matches by tag, sends the sticker URL, and logs [表情] into history', async () => {
  const harness = await makeHarness({ indexEntries: STICKERS });
  try {
    const result = await executeChatTool({ name: 'send_sticker', arguments: { query: '无语' } }, harness.context);
    assert.equal(result.ok, true, result.output);
    assert.equal(result.sentToUser, true);
    assert.equal(harness.sentStickers.length, 1);
    assert.equal(harness.sentStickers[0].chatType, 'private');
    assert.equal(harness.sentStickers[0].targetId, 3319085316);
    assert.equal(harness.sentStickers[0].file, STICKERS[0].url);
    assert.ok(result.output.includes('翻白眼'), result.output);
    assert.ok(result.output.includes('短ID S7S7'));
    assert.equal(harness.appended.length, 1);
    assert.equal(harness.appended[0].text, '[表情]');
    assert.ok(String(harness.appended[0].raw_message).includes('subType=1'));
  } finally {
    await harness.cleanup();
  }
});

test('send_sticker with no match returns the tag catalog and sends nothing', async () => {
  const harness = await makeHarness({ indexEntries: STICKERS });
  try {
    const result = await executeChatTool({ name: 'send_sticker', arguments: { query: '火箭发射' } }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('没有和「火箭发射」对得上的'), result.output);
    assert.ok(result.output.includes('无语'), result.output);
    assert.equal(harness.sentStickers.length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('send_sticker 随机 picks from the whole library', async () => {
  const harness = await makeHarness({ indexEntries: STICKERS });
  try {
    const result = await executeChatTool({ name: 'send_sticker', arguments: { query: '随机' } }, harness.context);
    assert.equal(result.ok, true, result.output);
    assert.equal(harness.sentStickers.length, 1);
    const urls = STICKERS.map((entry) => entry.url);
    assert.ok(urls.includes(harness.sentStickers[0].file));
  } finally {
    await harness.cleanup();
  }
});

test('send_sticker with a missing index reports a readable error instead of sending', async () => {
  const harness = await makeHarness(); // 不写 index.json
  try {
    const result = await executeChatTool({ name: 'send_sticker', arguments: { query: '开心' } }, harness.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('还没建好'), result.output);
    assert.equal(harness.sentStickers.length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('refreshStickerIndex 按 MD5 增量：新表情视觉打标、旧的只挪 URL、取消收藏的移除', async () => {
  const seen: { url: string; question: string }[] = [];
  const newMd5 = 'D'.repeat(32);
  const harness = await makeHarness({
    indexEntries: [STICKERS[0], STICKERS[2]], // A（无语猫）+ C（哭），B 不在收藏里了
    fetchCustomFace: [STICKERS[0].url, md5Url(newMd5)],
    describeImage: async (url, question) => {
      seen.push({ url, question });
      return '{"desc":"歪头小猫听不懂","tags":["歪头","猫猫","疑惑"]}';
    },
  });
  try {
    const result = await refreshStickerIndex(harness.context.bot, harness.context.describeImage);
    assert.equal(result.ok, true, result.message);
    assert.equal(result.added, 1);
    assert.equal(result.kept, 1);
    assert.equal(result.removed, 1);
    assert.equal(seen.length, 1, '只应看一张新图');
    assert.equal(seen[0].url, md5Url(newMd5));
    assert.ok(seen[0].question.includes('JSON'), '打标 prompt 应要求 JSON 输出');
    // 落盘核对：A 保留 + 新条目（解析出的 desc/tags）
    const onDisk = JSON.parse(await readFile(join(harness.dir, 'index.json'), 'utf-8')) as {
      md5: string;
      url: string;
      desc: string;
      tags: string[];
    }[];
    assert.equal(onDisk.length, 2);
    const added = onDisk.find((entry) => entry.md5 === newMd5);
    assert.ok(added, '新条目应写入');
    assert.equal(added.desc, '歪头小猫听不懂');
    assert.deepEqual(added.tags, ['歪头', '猫猫', '疑惑']);
    assert.ok(onDisk.some((entry) => entry.md5 === STICKERS[0].md5));
    assert.ok(!onDisk.some((entry) => entry.md5 === STICKERS[2].md5), '取消收藏的应被移除');
    // 缓存已重置：send_sticker 立刻能搜到新打的标签
    const send = await executeChatTool({ name: 'send_sticker', arguments: { query: '疑惑' } }, harness.context);
    assert.equal(send.ok, true, send.output);
    assert.equal(harness.sentStickers[0].file, md5Url(newMd5));
  } finally {
    await harness.cleanup();
  }
});

test('refreshStickerIndex 打标失败不占坑，清单为空不动索引', async () => {
  const harness = await makeHarness({
    indexEntries: STICKERS,
    fetchCustomFace: [md5Url('E'.repeat(32))],
    describeImage: async () => {
      throw new Error('视觉模型超时');
    },
  });
  try {
    const failed = await refreshStickerIndex(harness.context.bot, harness.context.describeImage);
    assert.equal(failed.ok, true, failed.message);
    assert.equal(failed.failed, 1);
    assert.equal(failed.added, 0);
    const kept = JSON.parse(await readFile(join(harness.dir, 'index.json'), 'utf-8')) as unknown[];
    assert.equal(kept.length, 0, '没有成功打标的新条目，旧索引全被移除（收藏清单确实只剩这张）');

    // 清单拉空 = 接口异常，跳过同步
    const empty = await makeHarness({
      indexEntries: STICKERS,
      fetchCustomFace: [],
      describeImage: async () => {
        throw new Error('不该看到图');
      },
    });
    try {
      const result = await refreshStickerIndex(empty.context.bot, empty.context.describeImage);
      assert.equal(result.ok, false);
      assert.ok(result.message.includes('为空'), result.message);
      const onDisk = JSON.parse(await readFile(join(empty.dir, 'index.json'), 'utf-8')) as unknown[];
      assert.equal(onDisk.length, STICKERS.length, '索引应原样保留');
    } finally {
      await empty.cleanup();
    }
  } finally {
    await harness.cleanup();
  }
});

test('sticker_refresh 仅主AI可用；主AI调用走完整刷新', async () => {
  const seen: string[] = [];
  const newMd5 = 'F'.repeat(32);
  const asChild = await makeHarness({
    fetchCustomFace: [md5Url(newMd5)],
    describeImage: async (url) => {
      seen.push(url);
      return '{"desc":"x","tags":["y"]}';
    },
  });
  try {
    const denied = await executeChatTool({ name: 'sticker_refresh', arguments: {} }, asChild.context);
    assert.equal(denied.ok, false);
    assert.ok(denied.output.includes('仅主AI'), denied.output);
    assert.equal(seen.length, 0, '被拒时不应触发视觉调用');
  } finally {
    await asChild.cleanup();
  }

  const asMaster = await makeHarness({
    isMaster: true,
    fetchCustomFace: [md5Url(newMd5)],
    describeImage: async () => '{"desc":"大哭的猫","tags":["哭","猫猫"]}',
  });
  try {
    const result = await executeChatTool({ name: 'sticker_refresh', arguments: {} }, asMaster.context);
    assert.equal(result.ok, true, result.output);
    assert.ok(result.output.includes('新打标 1'), result.output);
  } finally {
    await asMaster.cleanup();
  }
});

test('parseStickerCaption 兼容 JSON / 代码块 / 字段正则 / 两行 / 单行合并等形态', () => {
  assert.deepEqual(
    parseStickerCaption('{"desc":"猫猫歪头","tags":["歪头","猫猫"]}'),
    { desc: '猫猫歪头', tags: ['歪头', '猫猫'] },
  );
  assert.deepEqual(
    parseStickerCaption('```json\n{"desc":"猫猫歪头","tags":["歪头"]}\n```'),
    { desc: '猫猫歪头', tags: ['歪头'] },
  );
  const loose = parseStickerCaption('打标如下："desc": "大哭的猫", "tags": ["哭", "难过", "崩溃"]');
  assert.equal(loose.desc, '大哭的猫');
  assert.deepEqual(loose.tags, ['哭', '难过', '崩溃']);
  assert.deepEqual(
    parseStickerCaption('一只猫瘫在桌上\n瘫，猫猫，无语'),
    { desc: '一只猫瘫在桌上', tags: ['瘫', '猫猫', '无语'] },
  );
  const merged = parseStickerCaption('粉发女孩开心拍手，可爱，粉发，拍手，得意，Q版');
  assert.ok(merged.desc.startsWith('粉发女孩'), merged.desc);
  assert.ok(merged.tags.includes('拍手'));
  assert.equal(parseStickerCaption('   \n ').desc, '');
});
