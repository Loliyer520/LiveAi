import test from 'node:test';
import assert from 'node:assert/strict';
import { shouldTrigger, GroupReplyWindows, buildDebounceTriggerMessage } from '../src/chat/trigger.js';
import { sourceKindOf, cleanText, markMentionsSelf } from '../src/chat/source.js';
import type { ChatMessage } from '../src/chat/types.js';
import { Workspace } from '../src/chat/workspace.js';

function groupMessage(text: string, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    chatType: 'group',
    chatId: 777,
    userId: 100,
    text,
    rawMessage: text,
    sender: { nickname: 'user100', user_id: 100 },
    messageId: 1,
    mentionsSelf: false,
    timestamp: Date.now() / 1000,
    rawData: {},
    ...overrides,
  };
}

const AGENT = { triggerWords: ['冰糖', 'bingtang'], triggerRate: 0 };

test('private messages always trigger', () => {
  const message: ChatMessage = {
    ...groupMessage('hi', { chatType: 'private' as const, rawData: {} }),
  };
  assert.equal(shouldTrigger(message, 'hi', AGENT), true);
});

test('group messages trigger on mention, word, and rate; otherwise not', () => {
  assert.equal(shouldTrigger(groupMessage('随便聊聊', { mentionsSelf: true }), '随便聊聊', AGENT), true);
  assert.equal(shouldTrigger(groupMessage('今天冰糖真可爱'), '今天冰糖真可爱', AGENT), true);
  assert.equal(shouldTrigger(groupMessage('bingtang 在吗'), 'bingtang 在吗', AGENT), true);
  assert.equal(shouldTrigger(groupMessage('无关内容'), '无关内容', AGENT), false);
});

test('system private sources never trigger', () => {
  const message = { ...groupMessage('系统消息', { chatType: 'private' as const }), userId: 10000, sender: { nickname: '系统消息', user_id: 10000 } };
  assert.equal(sourceKindOf(message), 'system_private');
  assert.equal(shouldTrigger(message, '系统消息', AGENT), false);
});

test('sourceKindOf classifies internal and self-device sources', () => {
  const agentMsg = groupMessage('report', { userId: 0, rawData: { source: 'agent_message' } });
  assert.equal(sourceKindOf(agentMsg), 'internal_task');
  const selfDevice = groupMessage('我自己说的', { rawData: { source: 'self_other_device' } });
  assert.equal(sourceKindOf(selfDevice), 'self_other_device');
});

test('cleanText strips CQ:at of the bot and trims', () => {
  const message = groupMessage(`[CQ:at,qq=3592217365] 你好呀`);
  assert.equal(cleanText(message, 3592217365), '你好呀');
});

test('markMentionsSelf prefixes the marker for group mentions', () => {
  const message = groupMessage('你好', { mentionsSelf: true });
  assert.equal(markMentionsSelf(message, '你好'), '[被@] 你好');
  assert.equal(markMentionsSelf(groupMessage('hi'), 'hi'), 'hi');
});

test('debounce window fires only after 5s silence, once, and only when idle', async () => {
  const fired: { scopeKey: string; scopeId: string }[] = [];
  const windows = new GroupReplyWindows({
    isEpochStale: () => false,
    isScopeBusy: () => false,
    fireTrigger: (scopeKey, scopeId) => fired.push({ scopeKey, scopeId }),
  });
  windows.touch('group:1', '1', 5);
  windows.touch('group:1', '1', 5); // refresh, no re-arm
  // Window expires at 60s; silence fires at 5s after last touch — neither reached
  // within this short wait:
  await sleep(1500);
  assert.equal(fired.length, 0);
  windows.cancelAll();
});

test('debounce suppresses fire when scope is busy', async () => {
  const fired: string[] = [];
  const windows = new GroupReplyWindows({
    isEpochStale: () => false,
    isScopeBusy: () => true,
    fireTrigger: (scopeKey) => fired.push(scopeKey),
  });
  // Simulate a window whose last message is already 6s old: use private access
  // via a short-circuit — construct by touching then aging the record.
  windows.touch('group:2', '2', 7);
  const record = (windows as unknown as { windows: Map<string, { lastMessageTime: number; armedAt: number }> }).windows;
  const entry = record.get('group:2')!;
  entry.lastMessageTime = Date.now() - 6000;
  entry.armedAt = Date.now() - 7000;
  await sleep(1500);
  assert.equal(fired.length, 0); // busy → suppressed and disposed
  windows.cancelAll();
});

test('buildDebounceTriggerMessage is a mentions-self synthetic with unique id', () => {
  const one = buildDebounceTriggerMessage('777', 3, 3592217365);
  const two = buildDebounceTriggerMessage('777', 3, 3592217365);
  assert.equal(one.chatType, 'group');
  assert.equal(one.chatId, 777);
  assert.equal(one.mentionsSelf, true);
  assert.notEqual(one.messageId, two.messageId);
});

// ── trigger_config tool: the model tunes its own rate / words ───────────────

import { executeChatTool, type ToolContext } from '../src/chat/tools.js';
import type { AgentRecord } from '../src/store/repository.js';

function triggerToolContext(): { context: ToolContext; agent: AgentRecord } {
  const agent = {
    agentId: 'agent_t',
    scopeType: 'group',
    scopeId: '777',
    persona: '',
    impression: '',
    triggerWords: ['冰糖', 'bingtang'],
    triggerRate: 0.01,
    displayName: '',
    createdAt: 0,
    overallSummary: '',
    overallSummaryBuiltAt: 0,
    overallSummaryAtSegments: 0,
  } satisfies AgentRecord;
  const repo = {
    getOrCreateAgent: () => agent,
    updateAgent: (_t: string, _i: string, patch: Partial<AgentRecord>) => Object.assign(agent, patch),
  };
  const context = {
    bot: {} as never,
    repo: repo as never,
    archive: {} as never,
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
  } satisfies ToolContext;
  return { context, agent };
}

test('trigger_config set_rate clamps to 0~1 and persists on the agent', async () => {
  const { context, agent } = triggerToolContext();
  const result = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'set_rate', rate: 0.2 } },
    context,
  );
  assert.equal(result.ok, true);
  assert.equal(agent.triggerRate, 0.2);
  assert.ok(result.output.includes('20.0%'));

  const bad = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'set_rate', rate: 1.5 } },
    context,
  );
  assert.equal(bad.ok, false);
  assert.equal(agent.triggerRate, 0.2, 'invalid rate leaves the setting alone');
});

test('trigger_config add_word / remove_word manage the word list', async () => {
  const { context, agent } = triggerToolContext();
  const added = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'add_word', word: '小糖' } },
    context,
  );
  assert.equal(added.ok, true);
  assert.deepEqual(agent.triggerWords, ['冰糖', 'bingtang', '小糖']);
  // adding makes the word actually trigger
  assert.equal(shouldTrigger(groupMessage('小糖在吗'), '小糖在吗', agent), true);

  const dupe = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'add_word', word: '小糖' } },
    context,
  );
  assert.equal(dupe.ok, true);
  assert.equal(agent.triggerWords.length, 3, 'duplicate add is a no-op');

  const removed = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'remove_word', word: 'bingtang' } },
    context,
  );
  assert.equal(removed.ok, true);
  assert.deepEqual(agent.triggerWords, ['冰糖', '小糖']);

  const missing = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'remove_word', word: '不存在' } },
    context,
  );
  assert.equal(missing.ok, false);
});

test('trigger_config list reports current settings', async () => {
  const { context } = triggerToolContext();
  const result = await executeChatTool(
    { name: 'trigger_config', arguments: { action: 'list' } },
    context,
  );
  assert.equal(result.ok, true);
  assert.ok(result.output.includes('1.0%'));
  assert.ok(result.output.includes('冰糖'));
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
