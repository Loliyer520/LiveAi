import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MessageArchive, parseSearchQuery, entryMatches } from '../src/store/archive.js';
import type { HistoryEntry } from '../src/chat/types.js';
import { Workspace } from '../src/chat/workspace.js';

function entry(text: string, userId = 100, timestamp = 1_700_000_000, nickname = 'user100', ref = ''): HistoryEntry {
  return { user_id: userId, nickname, text, timestamp, message_ref: ref };
}

test('parseSearchQuery splits AND keywords and keeps quoted phrases', () => {
  assert.deepEqual(parseSearchQuery('北京 见面'), { terms: ['北京', '见面'] });
  assert.deepEqual(parseSearchQuery('"上海 见面" 周末，'), { terms: ['上海 见面', '周末'] });
  assert.deepEqual(parseSearchQuery('  '), { terms: [] });
});

test('entryMatches requires every term', () => {
  const item = entry('明天去北京见面吧');
  assert.equal(entryMatches(item, ['北京']), true);
  assert.equal(entryMatches(item, ['北京', '见面']), true);
  assert.equal(entryMatches(item, ['北京', '上海']), false);
  assert.equal(entryMatches(item, []), false);
  // nickname participates
  assert.equal(entryMatches(entry('你好', 1, 1, '冰糖小号'), ['冰糖小号']), true);
});

test('archive append + search with filters, context and limit', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-archive-'));
  const archive = new MessageArchive(dir);
  try {
    archive.append('group', 777, entry('今天天气不错', 100, 1_700_000_100));
    archive.append('group', 777, entry('明天去北京见面吧', 101, 1_700_000_200, 'user101'));
    archive.append('group', 777, entry('北京烤鸭好吃', 102, 1_700_000_300));
    archive.append('private', 888, entry('北京的私聊内容', 100, 1_700_000_400));
    await archive.flush(); // appends are fire-and-forget

    const hits = await archive.search({ query: '北京', scopeKeys: ['group:777'] });
    assert.equal(hits.length, 2);
    assert.ok(hits[0].line.includes('明天去北京见面吧'));
    assert.ok(hits[0].line.includes('user101(101)'));
    // context lines surround the hit
    assert.equal(hits[0].contextAfter.length, 1);
    assert.ok(hits[0].contextAfter[0].includes('北京烤鸭好吃'));

    // AND across words
    const narrow = await archive.search({ query: '北京 见面' });
    assert.equal(narrow.length, 1);

    // user filter
    const byUser = await archive.search({ query: '北京', userId: 102 });
    assert.equal(byUser.length, 1);
    assert.ok(byUser[0].line.includes('北京烤鸭好吃'));

    // time window
    const timed = await archive.search({ query: '北京', from: 1_700_000_350 });
    assert.equal(timed.length, 1);
    assert.ok(timed[0].scopeKey.includes('888'));

    // all-scope search finds both scopes
    const all = await archive.search({ query: '北京' });
    assert.equal(all.length, 3);

    // limit
    const limited = await archive.search({ query: '北京', limit: 1 });
    assert.equal(limited.length, 1);

    // phrase
    const phrase = await archive.search({ query: '"北京烤鸭"' });
    assert.equal(phrase.length, 1);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('archive search returns [] for empty terms', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-archive-'));
  const archive = new MessageArchive(dir);
  try {
    archive.append('group', 1, entry('内容'));
    await archive.flush();
    assert.deepEqual(await archive.search({ query: '' }), []);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('memory_search tool queries the archive and formats hits with context', async () => {
  const { executeChatTool } = await import('../src/chat/tools.js');
  const dir = await mkdtemp(join(tmpdir(), 'liveai-archive-'));
  const archive = new MessageArchive(dir);
  const repo = {
    resolveRef: () => null,
    registerMessageRef: () => 'AAAA',
    appendMessage: () => false,
    recentNotes: () => [],
    addNote: () => {},
    getOrCreateAgent: () => ({}),
  };
  try {
    archive.append('group', 777, entry('上次说的北京聚会定在周六'));
    archive.append('group', 777, entry('记得带相机'));
    await archive.flush();

    const result = await executeChatTool(
      { name: 'memory_search', arguments: { query: '北京 聚会' } },
      {
        bot: {} as never,
        repo: repo as never,
        archive,
        relations: {} as never,
        tasks: {} as never,
        masterQq: 1,
        isMaster: false,
        notifyMaster: () => {},
        delegateToChild: () => 't',
        sendToScope: async () => 'ok',
        triggerImageRefs: [],
        describeImage: async () => '',
        scopeType: 'group',
        scopeId: '777',
        executedTools: [],
        workspace: new Workspace('/tmp/liveai-test-workspace'),
        networkBudget: { used: 0 },
      },
    );
    assert.equal(result.ok, true);
    assert.ok(result.output.includes('北京聚会'));
    assert.ok(result.output.includes('▶'));

    const miss = await executeChatTool(
      { name: 'memory_search', arguments: { query: '不存在的词' } },
      {
        bot: {} as never,
        repo: repo as never,
        archive,
        relations: {} as never,
        tasks: {} as never,
        masterQq: 1,
        isMaster: false,
        notifyMaster: () => {},
        delegateToChild: () => 't',
        sendToScope: async () => 'ok',
        triggerImageRefs: [],
        describeImage: async () => '',
        scopeType: 'group',
        scopeId: '777',
        executedTools: [],
        workspace: new Workspace('/tmp/liveai-test-workspace'),
        networkBudget: { used: 0 },
      },
    );
    assert.equal(miss.ok, true);
    assert.ok(miss.output.includes('没有找到'));
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
