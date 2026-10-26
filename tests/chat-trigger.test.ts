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
  const message = groupMessage(`[CQ:at,qq=987654322] 你好呀`);
  assert.equal(cleanText(message, 987654322), '你好呀');
});

test('markMentionsSelf prefixes the marker for group mentions', () => {
  const message = groupMessage('你好', { mentionsSelf: true });
  assert.equal(markMentionsSelf(message, '你好'), '[被@] 你好');
  assert.equal(markMentionsSelf(groupMessage('hi'), 'hi'), 'hi');
});

test('markMentionsSelf skips the marker for the debounce synthetic', () => {
  // Regression 2026-09-29: rendering the debounce trigger as [被@] pressured
  // the model into re-answering its own last reply verbatim.
  const synthetic = buildDebounceTriggerMessage('1', 5, 987654322);
  assert.equal(markMentionsSelf(synthetic, synthetic.text), synthetic.text);
  assert.ok(synthetic.text.includes('stay_silent'), 'debounce text guides silence-when-answered');
});

test('cancel() drops an armed window so it never fires', async () => {
  const fired: string[] = [];
  const windows = new GroupReplyWindows({
    isEpochStale: () => false,
    isScopeBusy: () => false,
    fireTrigger: (scopeKey) => fired.push(scopeKey),
  });
  windows.touch('group:9', '9', 5);
  const record = (windows as unknown as { windows: Map<string, { lastMessageTime: number; armedAt: number }> }).windows;
  const entry = record.get('group:9')!;
  entry.lastMessageTime = Date.now() - 6000;
  entry.armedAt = Date.now() - 7000;
  windows.cancel('group:9'); // bot just spoke — window must die
  await sleep(1500);
  assert.equal(fired.length, 0);
  assert.equal(record.has('group:9'), false);
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
  const one = buildDebounceTriggerMessage('777', 3, 987654322);
  const two = buildDebounceTriggerMessage('777', 3, 987654322);
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
    enabled: true,
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
    peerInject: () => undefined,
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

// ── scope_manage (主AI 管控工具) ────────────────────────────────────────────

function makeAgent(scopeType: string, scopeId: string): AgentRecord {
  return {
    agentId: `agent_${scopeId}`,
    scopeType,
    scopeId,
    persona: '',
    impression: '',
    triggerWords: ['冰糖', 'bingtang'],
    triggerRate: 0.01,
    enabled: true,
    displayName: '',
    createdAt: 0,
    overallSummary: '',
    overallSummaryBuiltAt: 0,
    overallSummaryAtSegments: 0,
  };
}

function scopeManageContext(): { context: ToolContext; agents: Map<string, AgentRecord> } {
  const agents = new Map<string, AgentRecord>();
  agents.set('group:777', makeAgent('group', '777'));
  agents.set('group:888', makeAgent('group', '888'));
  agents.set('private:123456789', makeAgent('private', '123456789'));
  const repo = {
    getOrCreateAgent: (t: string, i: string) => {
      const key = `${t}:${i}`;
      if (!agents.has(key)) agents.set(key, makeAgent(t, i));
      return agents.get(key)!;
    },
    updateAgent: (t: string, i: string, patch: Partial<AgentRecord>) => {
      const key = `${t}:${i}`;
      const agent = agents.get(key) ?? makeAgent(t, i);
      Object.assign(agent, patch);
      agents.set(key, agent);
    },
    listAgents: () => [...agents.values()],
  };
  const context = {
    bot: {} as never,
    repo: repo as never,
    archive: {} as never,
    relations: {} as never,
    tasks: {} as never,
    masterQq: 123456789,
    isMaster: true,
    notifyMaster: () => {},
    delegateToChild: () => 't',
    sendToScope: async () => 'ok',
    peerInject: () => undefined,
    triggerImageRefs: [],
    describeImage: async () => '',
    scopeType: 'private',
    scopeId: '123456789',
    executedTools: [],
    workspace: new Workspace('/tmp/liveai-test-workspace'),
    networkBudget: { used: 0 },
  } satisfies ToolContext;
  return { context, agents };
}

test('scope_manage refuses non-master callers', async () => {
  const { context } = scopeManageContext();
  const result = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'list' } },
    { ...context, isMaster: false },
  );
  assert.equal(result.ok, false);
  assert.ok(result.output.includes('仅限主AI'));
});

test('scope_manage list and get report scope configs', async () => {
  const { context } = scopeManageContext();
  const list = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'list' } },
    context,
  );
  assert.equal(list.ok, true);
  assert.ok(list.output.includes('group:777'));
  assert.ok(list.output.includes('private:123456789'));

  const get = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'get', target_scope_type: 'group', target_scope_id: '888' } },
    context,
  );
  assert.equal(get.ok, true);
  assert.ok(get.output.includes('group:888'));
  assert.ok(get.output.includes('冰糖'));
});

test('scope_manage set_enabled gates triggering but not history', async () => {
  const { context, agents } = scopeManageContext();
  const off = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'set_enabled', target_scope_type: 'group', target_scope_id: '777', enabled: false } },
    context,
  );
  assert.equal(off.ok, true);
  assert.equal(agents.get('group:777')!.enabled, false);
  // @bot and trigger words no longer fire while disabled
  const msg = { ...groupMessage('冰糖在吗'), mentionsSelf: true };
  assert.equal(shouldTrigger(msg, '冰糖在吗', agents.get('group:777')!), false);

  const on = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'set_enabled', target_scope_type: 'group', target_scope_id: '777', enabled: true } },
    context,
  );
  assert.equal(on.ok, true);
  assert.equal(agents.get('group:777')!.enabled, true);
});

test('scope_manage never disables the master scope', async () => {
  const { context, agents } = scopeManageContext();
  const result = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'set_enabled', target_scope_type: 'private', target_scope_id: '123456789', enabled: false } },
    context,
  );
  assert.equal(result.ok, false);
  assert.equal(agents.get('private:123456789')!.enabled, true, 'master scope stays enabled');
});

test('scope_manage set_trigger_rate validates 0~1', async () => {
  const { context, agents } = scopeManageContext();
  const ok = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'set_trigger_rate', target_scope_type: 'group', target_scope_id: '777', rate: 0.3 } },
    context,
  );
  assert.equal(ok.ok, true);
  assert.equal(agents.get('group:777')!.triggerRate, 0.3);

  const bad = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'set_trigger_rate', target_scope_type: 'group', target_scope_id: '777', rate: 1.5 } },
    context,
  );
  assert.equal(bad.ok, false);
  assert.equal(agents.get('group:777')!.triggerRate, 0.3, 'invalid rate leaves the setting alone');
});

test('scope_manage trigger words add/remove with dedupe and non-empty guard', async () => {
  const { context, agents } = scopeManageContext();
  const added = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'add_trigger_words', target_scope_type: 'group', target_scope_id: '777', words: ['小糖', ' 冰糖 '] } },
    context,
  );
  assert.equal(added.ok, true);
  assert.deepEqual(agents.get('group:777')!.triggerWords, ['冰糖', 'bingtang', '小糖'], 'trims and dedupes');

  const removed = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'remove_trigger_words', target_scope_type: 'group', target_scope_id: '777', words: ['bingtang', '小糖'] } },
    context,
  );
  assert.equal(removed.ok, true);
  assert.deepEqual(agents.get('group:777')!.triggerWords, ['冰糖']);

  const empty = await executeChatTool(
    { name: 'scope_manage', arguments: { action: 'remove_trigger_words', target_scope_type: 'group', target_scope_id: '777', words: ['冰糖'] } },
    context,
  );
  assert.equal(empty.ok, false, 'refuses to empty the word list');
  assert.deepEqual(agents.get('group:777')!.triggerWords, ['冰糖']);
});
