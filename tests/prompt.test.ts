import test from 'node:test';
import assert from 'node:assert/strict';
import {
  sanitizeBlockBody,
  xmlAttrEscape,
  normalizeRef,
  wrapUserMsgBlock,
  wrapUserInvisibleGroup,
  renderPendingBlocks,
  buildRoleBasedHistoryMessages,
  buildTriggerUserMessage,
} from '../src/prompt/render.js';
import { buildChildMessages, buildChildBackgroundPrompt } from '../src/prompt/assemble.js';
import type { AssembleInput } from '../src/prompt/assemble.js';
import { stripThinking } from '../src/chat/thinking.js';
import type { HistoryEntry, TriggerEntry, ChatMessage } from '../src/chat/types.js';

const BOT_ID = '999';

function userEntry(text: string, userId = 100, ref = ''): HistoryEntry {
  return {
    user_id: userId,
    nickname: `user${userId}`,
    text,
    raw_message: text,
    message_id: null,
    message_ref: ref,
    timestamp: 1_700_000_000,
    source_kind: 'group',
    source_label: 'QQ群消息',
  };
}

function botEntry(text: string): HistoryEntry {
  return { user_id: Number(BOT_ID), nickname: '冰糖', text, timestamp: 1_700_000_000 };
}

function internalEntry(text: string): HistoryEntry {
  return { user_id: 0, nickname: '后台任务', text, timestamp: 1_700_000_000, source_kind: 'internal_task' };
}

test('sanitizeBlockBody neutralizes colluding wrapping tags only', () => {
  assert.equal(sanitizeBlockBody('<user_msg>hi</user_msg>'), '＜user_msg>hi＜/user_msg>');
  assert.equal(sanitizeBlockBody('<b>bold</b>'), '<b>bold</b>');
});

test('xmlAttrEscape escapes attribute-hostile characters', () => {
  assert.equal(xmlAttrEscape('a"b&c<d'), 'a&quot;b&amp;c&lt;d');
});

test('normalizeRef strips bracket decorations', () => {
  assert.equal(normalizeRef('[#A1B2]'), 'A1B2');
  assert.equal(normalizeRef('A1B2'), 'A1B2');
});

test('wrapUserMsgBlock groups consecutive user entries with short IDs', () => {
  const block = wrapUserMsgBlock([userEntry('hello', 100, 'A1B2'), userEntry('world', 101)]);
  assert.ok(block.startsWith('<user_msg from="user100"'));
  assert.ok(block.includes('[#A1B2]'));
  assert.ok(block.includes('user101'));
  assert.ok(block.endsWith('</user_msg>'));
});

test('renderPendingBlocks interleaves user and invisible runs', () => {
  const blocks = renderPendingBlocks([
    userEntry('a'),
    userEntry('b'),
    internalEntry('task done'),
    internalEntry('alarm fired'),
    userEntry('c'),
  ]);
  assert.equal(blocks.length, 3);
  assert.ok(blocks[0].startsWith('<user_msg'));
  assert.ok(blocks[1].startsWith('<user_invisible><tool_report'));
  assert.ok(blocks[1].includes('<tool_report'));
  assert.ok(blocks[2].startsWith('<user_msg'));
});

test('buildRoleBasedHistoryMessages collapses consecutive bot entries', () => {
  const messages = buildRoleBasedHistoryMessages([
    userEntry('q1'),
    botEntry('a1'),
    botEntry('a2'),
    userEntry('q2'),
    internalEntry('report'),
  ], BOT_ID);
  const roles = messages.map((message) => message.role);
  assert.deepEqual(roles, ['user', 'assistant', 'user']);
  const assistant = messages[1];
  assert.ok(assistant.role === 'assistant');
  assert.equal((assistant as { content: string }).content, 'a1\na2');
});

test('buildTriggerUserMessage wraps internal reports as invisible with note', () => {
  const triggers: TriggerEntry[] = [
    { user_id: 0, nickname: '后台任务', text: '闹钟响了', timestamp: 1, source_kind: 'internal_task' },
  ];
  const content = buildTriggerUserMessage(triggers);
  assert.ok(content.includes('<user_invisible>'));
  assert.ok(content.includes('闹钟响了'));
  assert.ok(content.includes('系统内部异步结果'));
});

test('buildTriggerUserMessage handles empty input', () => {
  assert.equal(buildTriggerUserMessage([]), '暂无新消息');
});

test('stripThinking removes closed and unclosed thinking blocks', () => {
  assert.equal(stripThinking('<thinking>hidden</thinking>visible'), 'visible');
  assert.equal(stripThinking('visible<thinking>unclosed'), 'visible');
  assert.equal(stripThinking('plain'), 'plain');
});

// ── assemble ─────────────────────────────────────────────────────────────────

const BUNDLE = {
  char: '你是冰糖，一个内向的高中生。',
  charPrefill: '好的，我会按照人设行动。',
  chatFocus: '【聊天职责】观察群聊。',
  chatStyle: '【说话检查】短句。',
  childRules: '1. 规则一\n2. 规则二\n23. 规则二十三\n24. 规则二十四\n25. 规则二十五',
  main: '你是主AI。',
  agent: '你是后台agent。',
  staffSystem: 'staff system block',
};

function makeMessage(text = '你好'): ChatMessage {
  return {
    chatType: 'private',
    chatId: 123,
    userId: 456,
    text,
    rawMessage: text,
    sender: { nickname: 'tester', user_id: 456 },
    messageId: 1,
    mentionsSelf: false,
    timestamp: 1_700_000_000,
    rawData: {},
  };
}

function assembleInput(triggerText: string): AssembleInput {
  const trigger: TriggerEntry = {
    user_id: 456,
    nickname: 'tester',
    text: triggerText,
    timestamp: 1_700_000_000,
    source_kind: 'friend_private',
    source_label: 'QQ好友私聊',
  };
  return {
    bundle: BUNDLE,
    message: makeMessage(triggerText),
    persona: BUNDLE.char,
    impression: '喜欢聊技术',
    historyBeforeTrigger: [userEntry('earlier')],
    triggerMessages: [trigger],
    background: {
      impression: '喜欢聊技术',
      globalIdentityContext: '',
      groupContext: '',
      triggerInfo: '',
      senderCard: '',
      scopeRelationCard: '',
      relationOverview: '',
      imageHint: '',
      deferredCount: 0,
      displayName: '',
      overallSummary: '这是从会话开始到现在的整体梗概。',
      diarySummaries: [{ index: 0, text: '旧摘要' }],
      knowledgeLines: [],
      mountedKnowledgeLines: [],
      recentThinkNotes: ['注意他最近在考试'],
      isMasterMessage: false,
      isAdminMessage: false,
      botSelfId: '3592217365',
      masterQq: '241898129',
      nowText: '2026-09-28 星期一 12:00',
    },
    injectPersona: true,
    chatMode: true,
    modeHint: '',
  };
}

test('buildChildMessages orders system blocks and stamps cache on head', () => {
  const { system, messages } = buildChildMessages(assembleInput('在吗'));
  assert.equal(system.length, 3);
  assert.ok(system[0].text.includes('staff system block'));
  assert.ok(system[0].text.includes('规则二十五'));
  // chat mode drops tooling-only rules 2/23/24
  assert.ok(!system[0].text.includes('规则二十三'));
  assert.ok(system[0].cacheControl?.type === 'ephemeral');
  assert.ok(system[1].text.includes('2026-09-28'));
  assert.ok(system[1].text.includes('旧摘要'));
  assert.ok(system[2].text.includes('冰糖'));
  assert.ok(system[2].text.includes('send_message'));

  const roles = messages.map((entry) => entry.role);
  // char prefill pair + tool prefill pair + history + trigger
  assert.deepEqual(roles, ['user', 'assistant', 'user', 'assistant', 'user', 'user']);
  const trigger = messages[messages.length - 1];
  assert.ok(trigger.role === 'user');
  assert.ok(String((trigger as { content: string }).content).includes('在吗'));
});

test('background prompt carries deferred-count 补审提醒', () => {
  const input = assembleInput('在吗');
  input.background.deferredCount = 3;
  const text = buildChildBackgroundPrompt(input);
  assert.ok(text.includes('补审提醒'));
  assert.ok(text.includes('3 条消息'));
});

test('background prompt shows overall summary, last-3 segments and search hint', () => {
  const input = assembleInput('在吗');
  input.background.overallSummary = '整体梗概：一起做了项目。';
  input.background.diarySummaries = [0, 1, 2, 3].map((index) => ({ index, text: `第${index}段摘要` }));
  const text = buildChildBackgroundPrompt(input);
  assert.ok(text.includes('会话整体梗概'));
  assert.ok(text.includes('一起做了项目'));
  assert.ok(text.includes('【第4段】第3段摘要'), 'latest segments rendered');
  assert.ok(!text.includes('【第1段】第0段摘要'), 'older segments folded into the primer');
  assert.ok(text.includes('memory_search'));
});

test('master mode: coordination prefill, unfiltered rules, relation overview', () => {
  const input = assembleInput('[子AI上报 from group:1 type=intel]\n小明说他在准备考研');
  input.masterMode = true;
  input.persona = BUNDLE.main;
  input.background.relationOverview = '人物（按情报量排序）:\n- 小明(456)：3 条情报';
  const { system, messages } = buildChildMessages(input);

  // rules NOT filtered in master mode (chat-mode filter drops 2/23/24)
  assert.ok(system[0].text.includes('规则二十三'), 'master keeps tooling rules');
  // no chat focus/style, no chat-persona prefill acknowledgement
  const personaTail = system[system.length - 1].text;
  assert.ok(!personaTail.includes('【聊天职责】'));
  assert.ok(personaTail.includes('主AI职责'));
  assert.ok(system[1].text.includes('全局关系网络概览'));
  assert.ok(system[1].text.includes('小明(456)：3 条情报'));

  const roles = messages.map((entry) => entry.role);
  assert.deepEqual(roles.slice(0, 2), ['user', 'assistant']);
  const ack = messages[1];
  assert.ok(ack.role === 'assistant');
  assert.ok(String((ack as { content: string }).content).includes('主AI'), 'master acknowledgement, not chat persona');
  assert.ok(String((messages[0] as { content: string }).content).includes('职责定位'));
});

test('background prompt renders sender relation card with confidentiality note', () => {
  const input = assembleInput('在吗');
  input.background.senderCard = '小明(456)，出现于 3 个会话\n- [preference][9-28] 喜欢猫';
  input.background.scopeRelationCard = '常聊话题: 游戏、考试';
  const text = buildChildBackgroundPrompt(input);
  assert.ok(text.includes('发送者档案（关系网络'));
  assert.ok(text.includes('喜欢猫'));
  assert.ok(text.includes('不要说出"我在别的群看到你'), 'cross-scope confidentiality rule');
  assert.ok(text.includes('本会话关系网络概况'));
  assert.ok(text.includes('游戏、考试'));
  const bare = buildChildBackgroundPrompt(assembleInput('在吗'));
  assert.ok(!bare.includes('发送者档案'), 'no card section when empty');
});

test('background prompt marks master identity', () => {
  const input = assembleInput('主人在吗');
  input.background.isMasterMessage = true;
  const text = buildChildBackgroundPrompt(input);
  assert.ok(text.includes('发送者是你的主人'));
});
