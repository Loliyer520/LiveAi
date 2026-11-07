/** 小说阅读指令机：整句指令直接应答（不走模型），数据走 fq-novel 服务。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { AddressInfo } from 'node:net';
import { NovelCommandHandler, chunkChapterText, parseChineseNumber } from '../src/chat/novel-cmd.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import type { ScopeRepository } from '../src/store/repository.js';
import type { ChatMessage } from '../src/chat/types.js';

const CATALOG = [
  { item_id: 'c1', title: '第1章 开始' },
  { item_id: 'c2', title: '第2章 继续' },
  { item_id: 'c3', title: '第3章 深入' },
];

function fakeFqServer(): Promise<{ server: Server; baseUrl: string }> {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://x');
    const send = (payload: unknown): void => {
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify(payload));
    };
    if (url.pathname === '/search') {
      if (url.searchParams.get('q') === '无此书') return send({ code: 0, data: { books: [] } });
      return send({
        code: 0,
        data: {
          books: [
            { book_id: 'B1', title: '书一', author: '甲', score: '9.1', word_count: '100万' },
            { book_id: 'B2', title: '书二', author: '乙' },
          ],
        },
      });
    }
    if (url.pathname === '/book/B1/catalog') {
      return send({ code: 0, data: { volume_list: [{ chapter_list: CATALOG }] } });
    }
    if (url.pathname === '/book/B2/catalog') {
      return send({ code: -1, message: '书不存在' });
    }
    const chapterMatch = url.pathname.match(/^\/chapter\/(c\d)$/);
    if (chapterMatch !== null) {
      const idx = Number(chapterMatch[1].slice(1));
      return send({ code: 0, data: { title: CATALOG[idx - 1].title, text: `第${idx}章正文第一段。\n第${idx}章第二段。` } });
    }
    res.statusCode = 404;
    return send({ code: -1, message: 'not found' });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as AddressInfo).port;
      resolve({ server, baseUrl: `http://127.0.0.1:${port}` });
    });
  });
}

interface Harness {
  handler: NovelCommandHandler;
  sent: string[];
  history: Record<string, unknown>[];
  server: Server;
  cleanup: () => Promise<void>;
}

async function makeHarness(scopes: string[] = ['private:3319085316']): Promise<Harness> {
  const { server, baseUrl } = await fakeFqServer();
  const dir = await mkdtemp(join(tmpdir(), 'liveai-novelcmd-'));
  const sent: string[] = [];
  const history: Record<string, unknown>[] = [];
  const fakeBot = {
    selfId: 999,
    selfNickname: '测试冰',
    async sendText(_chatType: string, _targetId: number, text: string) {
      sent.push(text);
      return { message_id: 7777 };
    },
  } as unknown as NapcatBot;
  const fakeRepo = {
    appendMessage: (_t: string, _i: string, entry: Record<string, unknown>) => {
      history.push(entry);
      return true;
    },
  } as unknown as ScopeRepository;
  const handler = new NovelCommandHandler({
    scopes,
    baseUrl,
    fallbackBaseUrls: [],
    statePath: join(dir, 'novel-cmd.json'),
    bot: fakeBot,
    repo: fakeRepo,
  });
  return {
    handler,
    sent,
    history,
    server,
    cleanup: async () => {
      server.close();
      await rm(dir, { recursive: true, force: true });
    },
  };
}

function msg(text: string): ChatMessage {
  return {
    chatType: 'private',
    chatId: 3319085316,
    userId: 3319085316,
    text,
    rawMessage: text,
    sender: {},
    messageId: 1,
    mentionsSelf: false,
    timestamp: Date.now() / 1000,
    rawData: {},
  } as ChatMessage;
}

test('parseChineseNumber 覆盖 十/百/千/万 与 零', () => {
  assert.equal(parseChineseNumber('一'), 1);
  assert.equal(parseChineseNumber('十'), 10);
  assert.equal(parseChineseNumber('十二'), 12);
  assert.equal(parseChineseNumber('二十'), 20);
  assert.equal(parseChineseNumber('二十三'), 23);
  assert.equal(parseChineseNumber('一百零五'), 105);
  assert.equal(parseChineseNumber('两百三十'), 230);
  assert.equal(parseChineseNumber('三千零二十一'), 3021);
  assert.equal(parseChineseNumber('一万零一'), 10001);
  assert.equal(parseChineseNumber('abc'), null);
  assert.equal(parseChineseNumber(''), null);
});

test('chunkChapterText 按段落切块，超长段硬切', () => {
  const chunks = chunkChapterText('短段一\n短段二\n短段三', 100);
  assert.equal(chunks.length, 1);
  const text = Array.from({ length: 30 }, (_, i) => `段落${i}内容内容内容内容`).join('\n');
  const parts = chunkChapterText(text, 80);
  assert.ok(parts.length > 2, '应切多段');
  for (const part of parts) assert.ok(part.length <= 80, `单块超限: ${part.length}`);
  assert.equal(parts.join('\n').replace(/\n/g, ''), text.replace(/\n/g, ''), '拼接应还原正文');
  const hard = chunkChapterText('x'.repeat(250), 100);
  assert.deepEqual(hard.map((p) => p.length), [100, 100, 50]);
});

test('非指令消息不拦截', async () => {
  const h = await makeHarness();
  try {
    assert.equal(await h.handler.tryHandle(msg('今天天气真好'), '今天天气真好'), false);
    assert.equal(await h.handler.tryHandle(msg('目录里有什么好吃的'), '目录里有什么好吃的'), false);
    assert.equal(h.sent.length, 0);
    assert.equal(h.handler.handles('private:3319085316'), true);
    assert.equal(h.handler.handles('private:111'), false);
  } finally {
    await h.cleanup();
  }
});

test('读书帮助', async () => {
  const h = await makeHarness();
  try {
    assert.equal(await h.handler.tryHandle(msg('读书'), '读书'), true);
    assert.ok(h.sent[0].includes('搜书'), h.sent[0]);
    assert.equal(h.history.length, 1);
    assert.equal(h.history[0].source_label, '小说指令');
  } finally {
    await h.cleanup();
  }
});

test('搜书 → 选 → 目录 → 看 → 下一章 全流程', async () => {
  const h = await makeHarness();
  try {
    assert.equal(await h.handler.tryHandle(msg('搜书 芙莉莲'), '搜书 芙莉莲'), true);
    assert.ok(h.sent[0].includes('《书一》'), h.sent[0]);
    assert.ok(h.sent[0].includes('甲'), h.sent[0]);

    assert.equal(await h.handler.tryHandle(msg('选 1'), '选 1'), true);
    assert.ok(h.sent[1].includes('已选《书一》'), h.sent[1]);
    assert.ok(h.sent[1].includes('共 3 章'), h.sent[1]);

    assert.equal(await h.handler.tryHandle(msg('目录'), '目录'), true);
    assert.ok(h.sent[2].includes('第1章 开始'), h.sent[2]);
    assert.ok(h.sent[2].includes('1/1页'), h.sent[2]);

    assert.equal(await h.handler.tryHandle(msg('看 2'), '看 2'), true);
    // 标题行 + 正文块
    assert.ok(h.sent.some((t) => t.includes('《书一》第2章 继续')), JSON.stringify(h.sent));
    assert.ok(h.sent.some((t) => t.includes('第2章正文第一段')), JSON.stringify(h.sent));
    const last = h.history[h.history.length - 1];
    assert.ok(String(last.text).includes('[小说指令]'), String(last.text));
    assert.ok(String(last.text).includes('第2章 继续'), String(last.text));

    assert.equal(await h.handler.tryHandle(msg('下一章'), '下一章'), true);
    assert.ok(h.sent.some((t) => t.includes('第3章 深入')), JSON.stringify(h.sent));

    // 中文数字章节号
    assert.equal(await h.handler.tryHandle(msg('第一章'), '第一章'), true);
    assert.ok(h.sent.some((t) => t.includes('第1章 开始')), JSON.stringify(h.sent));

    // 上一章到头
    assert.equal(await h.handler.tryHandle(msg('上一章'), '上一章'), true);
    assert.ok(h.sent[h.sent.length - 1].includes('已经是第一章'), h.sent[h.sent.length - 1]);
  } finally {
    await h.cleanup();
  }
});

test('没选书就 看/目录 → 引导先搜书', async () => {
  const h = await makeHarness();
  try {
    await h.handler.tryHandle(msg('看 1'), '看 1');
    assert.ok(h.sent[0].includes('还没选书'), h.sent[0]);
    await h.handler.tryHandle(msg('目录'), '目录');
    assert.ok(h.sent[1].includes('还没选书'), h.sent[1]);
    await h.handler.tryHandle(msg('选 1'), '选 1');
    assert.ok(h.sent[2].includes('还没有可选的搜索结果'), h.sent[2]);
  } finally {
    await h.cleanup();
  }
});

test('搜不到 / 选书目录失败 → 可读错误；章节超界提示', async () => {
  const h = await makeHarness();
  try {
    await h.handler.tryHandle(msg('搜书 无此书'), '搜书 无此书');
    assert.ok(h.sent[0].includes('没搜到'), h.sent[0]);

    await h.handler.tryHandle(msg('搜书 芙莉莲'), '搜书 芙莉莲');
    await h.handler.tryHandle(msg('选 2'), '选 2');
    assert.ok(h.sent[h.sent.length - 1].includes('书不存在'), h.sent[h.sent.length - 1]);

    await h.handler.tryHandle(msg('选 1'), '选 1');
    await h.handler.tryHandle(msg('看 99'), '看 99');
    assert.ok(h.sent[h.sent.length - 1].includes('超出范围'), h.sent[h.sent.length - 1]);
    await h.handler.tryHandle(msg('目录 5'), '目录 5');
    assert.ok(h.sent[h.sent.length - 1].includes('页码超出范围'), h.sent[h.sent.length - 1]);
  } finally {
    await h.cleanup();
  }
});

test('双机互备：主线路挂了改走 fallback 链，全挂才报错', async () => {
  const { server, baseUrl } = await fakeFqServer();
  const dir = await mkdtemp(join(tmpdir(), 'liveai-novelcmd-fb-'));
  const sent: string[] = [];
  const fakeBot = {
    selfId: 999,
    selfNickname: '测试冰',
    async sendText(_c: string, _t: number, text: string) {
      sent.push(text);
      return { message_id: 7777 };
    },
  } as unknown as NapcatBot;
  const fakeRepo = {
    appendMessage: () => true,
  } as unknown as ScopeRepository;
  // 主线路指向必然拒绝的端口（连接层失败），fallback 指向活服务
  const handler = new NovelCommandHandler({
    scopes: ['private:3319085316'],
    baseUrl: 'http://127.0.0.1:1',
    fallbackBaseUrls: [baseUrl],
    statePath: join(dir, 'novel-cmd.json'),
    bot: fakeBot,
    repo: fakeRepo,
  });
  try {
    await handler.tryHandle(msg('搜书 芙莉莲'), '搜书 芙莉莲');
    assert.ok(sent[0].includes('书一'), `fallback 应答: ${sent[0]}`);
    await handler.tryHandle(msg('选 1'), '选 1');
    await handler.tryHandle(msg('看 1'), '看 1');
    assert.ok(sent.some((s) => s.includes('第1章正文第一段')), '整链应从 fallback 出正文');

    // 全链挂死 → 报错而非静默
    const dead = new NovelCommandHandler({
      scopes: ['private:3319085316'],
      baseUrl: 'http://127.0.0.1:1',
      fallbackBaseUrls: ['http://127.0.0.1:2'],
      statePath: join(dir, 'novel-cmd.json'),
      bot: fakeBot,
      repo: fakeRepo,
    });
    sent.length = 0;
    await dead.tryHandle(msg('搜书 芙莉莲'), '搜书 芙莉莲');
    assert.ok(sent[0].includes('搜索失败'), `应报失败: ${sent[0]}`);
  } finally {
    server.close();
    await rm(dir, { recursive: true, force: true });
  }
});
