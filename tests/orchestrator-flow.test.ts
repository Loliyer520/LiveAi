/**
 * End-to-end orchestrator flow tests with a stubbed model upstream and a fake
 * NapCat bot. Verifies the preemption semantics ported from the legacy
 * runtime: mid-turn pickup, post-turn single follow-up merge, send_message
 * persistence, and the crash-guard interrupt note.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AiOrchestrator } from '../src/chat/orchestrator.js';
import { ScopeRepository } from '../src/store/repository.js';
import { MessageArchive } from '../src/store/archive.js';
import { DiarySummarizer } from '../src/store/summarizer.js';
import { PromptStore } from '../src/prompt/store.js';
import { ModelManager } from '../src/models/manager.js';
import { RelationGraph } from '../src/relations/graph.js';
import { KnowledgeBase } from '../src/knowledge/kb.js';
import { IntelCollector } from '../src/relations/collector.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import type { ChatMessage } from '../src/chat/types.js';

interface Harness {
  orchestrator: AiOrchestrator;
  repo: ScopeRepository;
  sent: { chatType: string; targetId: number; text: string }[];
  datalineSent: { text: string }[];
  modelRequests: Record<string, unknown>[];
  modelsJson: string;
  cleanup: () => Promise<void>;
}

function makeMessage(chatId: number, text: string, userId = 100, messageId: number): ChatMessage {
  return {
    chatType: 'private',
    chatId,
    userId,
    text,
    rawMessage: text,
    sender: { nickname: `user${userId}`, user_id: userId },
    messageId,
    mentionsSelf: false,
    timestamp: Date.now() / 1000,
    rawData: {},
  };
}

/** Scripted anthropic-style replies; one entry per expected model call. */
type ScriptedReply =
  | { text: string }
  | { tool: string; input: Record<string, unknown> };

function anthropicResponse(script: ScriptedReply): Record<string, unknown> {
  if ('tool' in script) {
    return {
      content: [{ type: 'tool_use', id: 'call_1', name: script.tool, input: script.input }],
      usage: { input_tokens: 10, output_tokens: 10 },
    };
  }
  return {
    content: [{ type: 'text', text: script.text }],
    usage: { input_tokens: 10, output_tokens: 10 },
  };
}

async function makeHarness(replies: ScriptedReply[], onFirstModelCall?: () => void): Promise<Harness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-test-'));
  const stateDir = join(dir, 'state');
  const modelsJson = join(dir, 'models_config.json');
  await writeFile(modelsJson, JSON.stringify({
    upstreams: [{ name: 'stub', base_url: 'http://model.test', api_key: 'sk-test', protocol: 'anthropic' }],
    channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'stub', model_id: 'test-model' }] }],
    roles: { main: 'main' },
  }), 'utf-8');

  // One archive instance shared by repo and orchestrator: stop() flushes the
  // archive the orchestrator holds, and that must be the one actually writing.
  const archive = new MessageArchive(join(dir, 'archive'));
  const repo = new ScopeRepository(stateDir, 500, archive);
  const prompts = new PromptStore(); // falls back to bundled defaults if data/prompt missing
  const models = new ModelManager(modelsJson);
  await models.load();
  const summarizer = new DiarySummarizer(repo, models, false); // disabled: memory tests cover it
  const relations = new RelationGraph(join(dir, 'relations.json'));
  const intel = new IntelCollector(relations, repo, models, () => 987654322, false); // disabled: relations tests cover it

  const sent: { chatType: string; targetId: number; text: string }[] = [];
  const datalineSent: { text: string }[] = [];
  const modelRequests: Record<string, unknown>[] = [];
  let callIndex = 0;

  const fakeBot = {
    selfId: 987654322,
    legacySelfIds: [] as number[],
    isSelfId(id: unknown) {
      return String(id ?? '') === '987654322';
    },
    async sendText(chatType: string, targetId: number, text: string) {
      sent.push({ chatType, targetId, text });
      return { message_id: 9000 + sent.length };
    },
    async sendDatalineText(text: string) {
      datalineSent.push({ text });
      return { message_id: 8000 + datalineSent.length };
    },
    async sendReplyText(message: ChatMessage, content: string) {
      sent.push({ chatType: message.chatType, targetId: Number(message.chatId), text: content });
      return { message_id: 9000 + sent.length };
    },
    async recallMessage() {
      return {};
    },
    async getGroupInfo() {
      return {};
    },
    async getStrangerInfo() {
      return {};
    },
  } as unknown as NapcatBot;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    const url = String(input instanceof Request ? input.url : input);
    assert.ok(url.includes('model.test'), `unexpected fetch: ${url}`);
    if (callIndex === 0) onFirstModelCall?.();
    const rawBody = input instanceof Request ? await input.clone().text() : String(init?.body ?? '{}');
    const body = JSON.parse(rawBody) as Record<string, unknown>;
    modelRequests.push(body);
    const script = replies[Math.min(callIndex, replies.length - 1)];
    callIndex += 1;
    return new Response(JSON.stringify(anthropicResponse(script)), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;

  const orchestrator = new AiOrchestrator({
    bot: fakeBot,
    repo,
    promptStore: prompts,
    models,
    config: {
      masterQq: 123456789,
      adminQq: 123456789,
      globalTriggerRate: 0,
      staleMessageMaxAgeSeconds: 300,
      historyLimit: 500,
      requestTimeoutMs: 10000,
      summaryEnabled: false,
      intelEnabled: false,
    },
    archive,
    summarizer,
    relations,
    intel,
    kb: new KnowledgeBase(join(dir, 'knowledge'), 'private:123456789'),
    tasksPath: join(dir, 'tasks.json'),
  });
  orchestrator.start();

  return {
    orchestrator,
    repo,
    sent,
    datalineSent,
    modelRequests,
    modelsJson,
    cleanup: async () => {
      globalThis.fetch = originalFetch;
      await orchestrator.stop();
      await rm(dir, { recursive: true, force: true });
    },
  };
}

test('simple private reply: one model call, send_message sends and persists', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '你好呀，我在。' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '在吗？', 100, 1));
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '你好呀，我在。');
    assert.equal(harness.sent[0].chatType, 'private');

    const history = harness.repo.getMessages('private', '123');
    const outbound = history.find((entry) => entry.user_id === 987654322 && entry.text === '你好呀，我在。');
    assert.ok(outbound !== undefined, 'outbound send persisted to history');
    assert.ok(outbound.message_ref !== undefined);
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('thinking is stripped from send_message output', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '<thinking>先想想</thinking>正文来了' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(124, '说点什么', 100, 2));
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '正文来了');
  } finally {
    await harness.cleanup();
  }
});

test('send_message splits multi-line content into separate QQ messages', async () => {
  const harness = await makeHarness([
    {
      tool: 'send_message',
      input: { content: '第一条\n\n[[内心：这句是旁白，不该发出去]]\n第二条' },
    },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(128, '分条试试', 100, 8));
    await waitFor(() => harness.sent.length === 2);

    assert.equal(harness.sent[0].text, '第一条');
    assert.equal(harness.sent[1].text, '第二条');
    for (const entry of harness.sent) {
      assert.equal(entry.chatType, 'private');
      assert.equal(entry.targetId, 128);
    }

    // each delivered line persists as its own history entry
    const history = harness.repo.getMessages('private', '128');
    for (const text of ['第一条', '第二条']) {
      const outbound = history.find((entry) => entry.user_id === 987654322 && entry.text === text);
      assert.ok(outbound !== undefined, `outbound "${text}" persisted to history`);
      assert.ok(outbound.message_ref !== undefined);
    }
    assert.ok(!history.some((entry) => entry.text.includes('旁白')), '[[...]] markers stripped, never sent');
  } finally {
    await harness.cleanup();
  }
});

test('mid-turn pickup: message arriving during the tool loop joins the same turn', async () => {
  const harness = await makeHarness(
    [
      { tool: 'send_message', input: { content: '第一条回复' } },
      { tool: 'stay_silent', input: {} },
    ],
    () => {
      // Arrives while the first model call is in flight: scope is busy, so it queues.
      harness.orchestrator.handleMessage(makeMessage(125, '插队消息', 100, 3));
    },
  );
  try {
    harness.orchestrator.handleMessage(makeMessage(125, '第一条', 100, 2));
    await waitFor(() => harness.modelRequests.length >= 2);
    await sleep(150);

    assert.equal(harness.sent.length, 1); // first reply sent; second round chose silence
    const secondRequest = harness.modelRequests[1];
    const flattened = JSON.stringify(secondRequest.messages);
    assert.ok(flattened.includes('插队消息'), 'queued message became round-2 trigger');
    assert.ok(flattened.includes('第一条回复'), 'round-1 outbound visible as history continuation');
    assert.ok(flattened.includes('补审提醒'), 'deferred-count reminder injected');
  } finally {
    await harness.cleanup();
  }
});

test('post-turn merge: messages queued during a full turn become ONE follow-up call', async () => {
  const harness = await makeHarness(
    [
      { tool: 'stay_silent', input: {} },
      { tool: 'send_message', input: { content: '补一条回复' } },
      { tool: 'stay_silent', input: {} },
    ],
    () => {
      harness.orchestrator.handleMessage(makeMessage(126, '排队甲', 100, 4));
      harness.orchestrator.handleMessage(makeMessage(126, '排队乙', 100, 5));
    },
  );
  try {
    harness.orchestrator.handleMessage(makeMessage(126, '开场', 100, 6));
    await waitFor(() => harness.modelRequests.length >= 2 && harness.sent.length === 1);
    await sleep(150);

    const secondRequest = harness.modelRequests[1];
    const flattened = JSON.stringify(secondRequest.messages);
    assert.ok(flattened.includes('排队甲'));
    assert.ok(flattened.includes('排队乙'));
    // both queued messages appear in a single trigger block — one merged turn
    const triggerMessage = (secondRequest.messages as { role: string; content: unknown }[])
      .filter((entry) => entry.role === 'user').pop();
    const triggerText = JSON.stringify(triggerMessage?.content ?? '');
    assert.ok(triggerText.includes('排队甲') && triggerText.includes('排队乙'), 'both queued messages merged into one trigger');
  } finally {
    await harness.cleanup();
  }
});

test('crash guard: failed model call persists an interrupt note', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }]);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    return new Response('upstream exploded', { status: 500 });
  }) as typeof fetch;
  try {
    harness.orchestrator.handleMessage(makeMessage(127, '触发错误', 100, 7));
    await waitFor(() => {
      const history = harness.repo.getMessages('private', '127');
      return history.some((entry) => String(entry.text).includes('上轮 AI 处理异常中断'));
    }, 200);
    const history = harness.repo.getMessages('private', '127');
    const note = history.find((entry) => String(entry.text).includes('上轮 AI 处理异常中断'));
    assert.ok(note !== undefined, 'interrupt note persisted');
  } finally {
    globalThis.fetch = originalFetch;
    await harness.cleanup();
  }
});

test('report routing: QQ child report lands at Onebot hub, not master', async () => {
  const harness = await makeHarness([
    { tool: 'notify_master', input: { text: '小明说他在准备考研', request_type: 'intel' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '跟主AI说一下我在准备考研', 100, 1));
    // QQ 下级（group/private）的汇报经 HUB_ROUTING 送进 Onebot 中枢消化
    await waitFor(() => {
      const hubHistory = harness.repo.getMessages('domain', 'onebot');
      return hubHistory.some((entry) => String(entry.text).includes('[子AI上报 from private:123'));
    });
    const hubHistory = harness.repo.getMessages('domain', 'onebot');
    const relay = hubHistory.find((entry) => String(entry.text).includes('[子AI上报 from private:123'));
    assert.ok(String(relay!.text).includes('准备考研'));
    const masterHistory = harness.repo.getMessages('private', '123456789');
    assert.ok(
      !masterHistory.some((entry) => String(entry.text).includes('准备考研')),
      'QQ 下级上报不应绕过 Onebot 中枢直送主AI',
    );
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('Onebot hub: digest relays to master; hub tool set carries domain_message, not relation_write', async () => {
  // 直接注入 onebot 会话（与下级汇报路由落地同形），避开共享剧本交错。
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '聚合完成：考研情报已沉淀入库。' } },
    // onebot 回合与中枢收尾回合共享剧本：重复 send，任一消费顺序都能把
    // 摘要送到中枢（若中枢收尾吃掉一条，会走 QQ 私聊发主人，断言容忍）。
    { tool: 'send_message', input: { content: '聚合完成：考研情报已沉淀入库。' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const injected: ChatMessage = {
      chatType: 'domain',
      chatId: 'onebot',
      userId: 0,
      text: '[子AI上报 from private:123 type=intel]\n小明说他在准备考研',
      rawMessage: '[子AI上报 from private:123 type=intel]\n小明说他在准备考研',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_onebot_1',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', relayed_from: 'private:123' },
    };
    harness.orchestrator.handleMessage(injected);
    // 1) onebot 回合跑起来：主级工具面带 domain_message（主级互聊），不带主AI专属工具
    await waitFor(() => harness.modelRequests.length >= 1);
    const hubRequest = harness.modelRequests.find((request) =>
      Array.isArray(request.tools) && (request.tools as { name: string }[]).some((tool) => tool.name === 'domain_message'),
    );
    assert.ok(hubRequest, 'Onebot 中枢请求应带 domain_message');
    const hubTools = (hubRequest!.tools as { name: string }[]).map((tool) => tool.name);
    assert.ok(!hubTools.includes('relation_write'), '领域中枢不应有 relation_write');
    assert.ok(hubTools.includes('notify_master'), '领域中枢应有 notify_master 上报通道');
    // 2) onebot 的 send_message 改道直报最高级中枢（domain → 主AI）
    await waitFor(() => {
      const masterHistory = harness.repo.getMessages('private', '123456789');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:onebot'));
    });
    const masterHistory = harness.repo.getMessages('private', '123456789');
    const relay = masterHistory.find((entry) => String(entry.text).includes('[子AI上报 from domain:onebot'));
    assert.ok(String(relay!.text).includes('考研情报已沉淀'));
    for (const entry of harness.sent) {
      assert.equal(entry.chatType, 'private');
      assert.equal(entry.targetId, 123456789);
    }
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('master conclusion relays back to the reporting hub scope', async () => {
  const harness = await makeHarness([{ text: '已收到，情报已归档。' }]);
  try {
    const injected: ChatMessage = {
      chatType: 'private',
      chatId: 123456789,
      userId: 0,
      text: '[子AI上报 from domain:onebot type=intel]\n聚合完成：考研情报已沉淀入库。',
      rawMessage: '[子AI上报 from domain:onebot type=intel]\n聚合完成：考研情报已沉淀入库。',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_master_relay_1',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', relayed_from: 'domain:onebot' },
    };
    harness.orchestrator.handleMessage(injected);
    // 主AI纯文本结论经 [主AI回传] 回到发起汇报的领域中枢
    await waitFor(() => {
      const hubHistory = harness.repo.getMessages('domain', 'onebot');
      return hubHistory.some((entry) => String(entry.text).includes('[主AI回传]'));
    });
    const hubHistory = harness.repo.getMessages('domain', 'onebot');
    const callback = hubHistory.find((entry) => String(entry.text).includes('[主AI回传]'));
    assert.ok(String(callback!.text).includes('已收到，情报已归档'));
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('bili video sub-AI: report routes to bili hub, hub digest reaches master', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '视频要点：UP 主讲了三种挖矿路线。' } },
    // 视频下级、哔哩中枢、中枢收尾三方共享剧本：重复 send 保任一消费顺序都通。
    { tool: 'send_message', input: { content: '视频要点：UP 主讲了三种挖矿路线。' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const injected: ChatMessage = {
      chatType: 'bili',
      chatId: 'BV1xxxxx',
      userId: 0,
      text: '[主AI委派] 细看这个视频，整理要点回报',
      rawMessage: '[主AI委派] 细看这个视频，整理要点回报',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_bili_1',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', delegate_task: 'delegate_bili_1' },
    };
    harness.orchestrator.handleMessage(injected);
    // 1) 委派落进 bili:<bvid> 视频下级 scope
    await waitFor(() => {
      const videoHistory = harness.repo.getMessages('bili', 'BV1xxxxx');
      return videoHistory.some((entry) => String(entry.text).includes('[主AI委派]'));
    });
    // 2) 视频下级的 send_message 改道报给哔哩中枢（[子AI上报 from bili:BV1xxxxx]）
    await waitFor(() => {
      const hubHistory = harness.repo.getMessages('domain', 'bili');
      return hubHistory.some((entry) => String(entry.text).includes('[子AI上报 from bili:BV1xxxxx'));
    });
    const hubHistory = harness.repo.getMessages('domain', 'bili');
    const relay = hubHistory.find((entry) => String(entry.text).includes('[子AI上报 from bili:BV1xxxxx'));
    assert.ok(String(relay!.text).includes('视频要点'));
    // 3) 哔哩中枢消化后直报最高级中枢（domain → 主AI）
    await waitFor(() => {
      const masterHistory = harness.repo.getMessages('private', '123456789');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:bili'));
    });
    const masterHistory = harness.repo.getMessages('private', '123456789');
    const digest = masterHistory.find((entry) => String(entry.text).includes('[子AI上报 from domain:bili'));
    assert.ok(String(digest!.text).includes('视频要点'));
    // 4) 全程零 QQ 发送（虚拟 scope 无窗口）
    assert.equal(harness.sent.length, 0);
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('master delegate_to_child injects an instruction into the target scope', async () => {  const harness = await makeHarness([
    { tool: 'delegate_to_child', input: { target_scope_type: 'private', target_scope_id: '777', instruction: '转告对方周末聚会改到下午三点' } },
    // The delegated scope's turn races the master's close-out round for the
    // shared scripted replies — duplicate the send so either fetch order
    // delivers it to the 777 scope.
    { tool: 'send_message', input: { content: '好的，已转达。' } },
    { tool: 'send_message', input: { content: '好的，已转达。' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    // 主人直接在主AI会话里下达跨会话指令
    harness.orchestrator.handleMessage(makeMessage(123456789, '帮我跟 777 说一下聚会改时间', 123456789, 10));
    // target scope received the delegation as an internal instruction…
    await waitFor(() => {
      const targetHistory = harness.repo.getMessages('private', '777');
      return targetHistory.some((entry) => String(entry.text).includes('[主AI委派]'));
    });
    // …and its child AI acted on it (send_message to the user there)
    await waitFor(() => harness.sent.some((entry) => entry.chatType === 'private' && entry.targetId === 777));
    assert.equal(harness.sent.find((entry) => entry.targetId === 777)!.text, '好的，已转达。');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('domain hub: domain:mc turn runs with domain tools; send_message relays back to master', async () => {
  // 直接以内部注入形式驱动 domain:mc（与 delegate_to_child 落地的消息同形），
  // 避开主AI收尾回合与领域回合共享剧本的交错不确定性（委派落地本身已有专测）。
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '已到服，出生点周围安全。' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const injected: ChatMessage = {
      chatType: 'domain',
      chatId: 'mc',
      userId: 0,
      text: '[主AI委派] 进服看一眼出生点周围',
      rawMessage: '[主AI委派] 进服看一眼出生点周围',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_domain_1',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', delegate_task: 'delegate_test_1' },
    };
    harness.orchestrator.handleMessage(injected);
    // 1) 委派消息落进 domain:mc scope 历史
    await waitFor(() => {
      const hubHistory = harness.repo.getMessages('domain', 'mc');
      return hubHistory.some((entry) => String(entry.text).includes('[主AI委派]'));
    });
    // 2) domain:mc 的模型请求带领域工具 mc_view，不带主AI专属工具
    await waitFor(() => harness.modelRequests.length >= 1);
    const hubRequest = harness.modelRequests.find((request) =>
      Array.isArray(request.tools) && (request.tools as { name: string }[]).some((tool) => tool.name === 'mc_view'),
    );
    assert.ok(hubRequest, 'domain:mc 请求应带 mc_view');
    const hubTools = (hubRequest!.tools as { name: string }[]).map((tool) => tool.name);
    assert.ok(!hubTools.includes('relation_write'), '领域中枢不应有 relation_write');
    assert.ok(hubTools.includes('notify_master'), '领域中枢应有 notify_master 上报通道');
    // 3) domain:mc 的 send_message 改道 relay 回中枢（[子AI上报 from domain:mc]），不碰任何 QQ 窗口
    await waitFor(() => {
      const masterHistory = harness.repo.getMessages('private', '123456789');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:mc'));
    });
    const masterHistory = harness.repo.getMessages('private', '123456789');
    const relay = masterHistory.find((entry) => String(entry.text).includes('[子AI上报 from domain:mc'));
    assert.ok(String(relay!.text).includes('出生点周围安全'));
    assert.equal(harness.sent.length, 0, '领域发言不应发到 QQ 窗口');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('persisted alarm fires back into the origin scope as an internal turn', async () => {
  const harness = await makeHarness([
    { tool: 'create_task', input: { kind: 'set_alarm', at: '+1s', note: '提醒主人喝水' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'send_message', input: { content: '喝水时间到！' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '1秒后提醒我喝水', 100, 1));
    // turn 1 creates the alarm through the persisted scheduler…
    await waitFor(() => harness.modelRequests.length >= 1);
    // …and when it fires, the scope wakes with [闹钟触发] and replies
    await waitFor(() => harness.sent.some((entry) => entry.text === '喝水时间到！'), 8000);
    const history = harness.repo.getMessages('private', '123');
    assert.ok(history.some((entry) => String(entry.text).includes('[闹钟触发] 提醒主人喝水')));
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('image messages surface the view_image hint and keep refs on the entry', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }]);
  try {
    const imageMessage = makeMessage(128, '', 100, 8);
    imageMessage.rawMessage = '[CQ:image,file=x.jpg,url=https://img.example.com/cat.jpg]';
    harness.orchestrator.handleMessage(imageMessage);
    await waitFor(() => harness.modelRequests.length >= 1);

    const flattened = JSON.stringify(harness.modelRequests[0].messages);
    assert.ok(flattened.includes('本次消息包含 1 张图片'), 'image hint in background (trigger tail)');
    assert.ok(flattened.includes('view_image'), 'hint points at the tool');

    const history = harness.repo.getMessages('private', '128');
    const entry = history.find((item) => Array.isArray(item.image_refs) && item.image_refs.length > 0);
    assert.ok(entry !== undefined, 'history entry carries image_refs');
    assert.deepEqual(entry!.image_refs, ['https://img.example.com/cat.jpg']);
    assert.ok(String(entry!.text).includes('[图片×1]'), 'text marker replaces dropped CQ code');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('group scope background carries trigger settings and 少说话 guidance', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }]);
  try {
    const groupMsg = { ...makeMessage(555, '随便聊聊', 100, 9), chatType: 'group' as const, mentionsSelf: true };
    harness.orchestrator.handleMessage(groupMsg);
    await waitFor(() => harness.modelRequests.length >= 1);
    const background = JSON.stringify(harness.modelRequests[0].messages);
    assert.ok(background.includes('群聊参与度'), 'trigger info section present in group scopes');
    assert.ok(background.includes('1.0%'), 'default trigger rate 0.01 shown');
    assert.ok(background.includes('尽量少说话'), '没融入少说话 guidance present');
    assert.ok(background.includes('trigger_config'), 'points at the self-tuning tool');
  } finally {
    await harness.cleanup();
  }
});

test('view_image result feeds back into the next round and the turn completes', async () => {
  const harness = await makeHarness([
    { tool: 'view_image', input: { index: 1, question: '这是什么' } },
    { text: '图片内容：一张物理试卷' }, // vision describe call
    { tool: 'send_message', input: { content: '我看到了，是物理卷子' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const imageMessage = makeMessage(130, '', 100, 10);
    imageMessage.rawMessage = '[CQ:image,file=x.jpg,url=https://img.example.com/physics.jpg]';
    harness.orchestrator.handleMessage(imageMessage);
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '我看到了，是物理卷子');
    await sleep(150);

    // 4 fetches: main round 1 → vision describe → main round 2 (feedback) → close-out
    assert.equal(harness.modelRequests.length, 4);
    const round2 = JSON.stringify(harness.modelRequests[2]);
    assert.ok(round2.includes('"tool_use"'), 'assistant tool_use replayed');
    assert.ok(round2.includes('view_image'), 'view_image call replayed');
    assert.ok(round2.includes('"tool_result"'), 'tool result block present');
    assert.ok(round2.includes('物理试卷'), 'image description reached the model');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('plain-text reply without send_message gets one re-prompt, not silence', async () => {
  const harness = await makeHarness([
    { text: '我直接用文字回答了你' }, // no tool call: invisible to the user
    { tool: 'send_message', input: { content: '重新用工具发的回答' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(131, '在不', 100, 11));
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '重新用工具发的回答');
    assert.equal(harness.sent.length, 1, 'the invisible plain text was never sent');

    const round2 = JSON.stringify(harness.modelRequests[1]);
    assert.ok(round2.includes('并没有被发送'), 're-prompt explains the send_message contract');
    assert.ok(round2.includes('我直接用文字回答了你'), 'model sees its own unsent text');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

function sleep(ms: number): Promise<void> {  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(25);
  }
  assert.fail('waitFor timed out');
}

test('self-chat messages are dropped before enqueue', async () => {
  const harness = await makeHarness([{ text: '不该出现' }]);
  try {
    // QQ self-chat normally arrives with user_id == chatId == the bot account
    harness.orchestrator.handleMessage(makeMessage(987654322, '自己的会话', 987654322, 500));
    // chatId guard on its own (defensive: sender differs but target is self)
    harness.orchestrator.handleMessage(makeMessage(987654322, '还是自己的会话', 999, 501));
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(harness.modelRequests.length, 0);
    assert.equal(harness.sent.length, 0);
  } finally {
    await harness.cleanup();
  }
});

test('self messages from other devices enter history but never trigger a turn', async () => {
  const harness = await makeHarness([{ text: '不该出现' }]);
  try {
    // 主人在手机上给好友 123 发了消息（同账号同步）
    harness.orchestrator.handleSelfMessage(makeMessage(123, '手机上发的', 987654322, 502));
    // 自我会话（发给自己）连历史都不进
    harness.orchestrator.handleSelfMessage(makeMessage(987654322, '自我会话', 987654322, 505));
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(harness.modelRequests.length, 0, 'silent event must not invoke the model');
    assert.equal(harness.sent.length, 0);

    // 但已经写进该会话的历史，标签 本账号其他设备
    const history = harness.repo.getMessages('private', '123');
    const entry = history.find((item) => item.text === '手机上发的');
    assert.ok(entry !== undefined, 'self message persisted to scope history');
    assert.equal(entry.source_label, '本账号其他设备');
    assert.equal(entry.user_id, 987654322);
    assert.equal(harness.repo.getMessages('private', '987654322').length, 0, 'self-chat never persisted');
  } finally {
    await harness.cleanup();
  }
});

test('self message arriving mid-turn folds into the running turn context', async () => {
  const harness = await makeHarness(
    [
      { tool: 'send_message', input: { content: '收到' } },
      { tool: 'stay_silent', input: {} },
    ],
    () => {
      // 第一次模型调用在飞：此刻手机上的发言应并入本轮触发上下文
      harness.orchestrator.handleSelfMessage(makeMessage(125, '手机上补充了一句', 987654322, 503));
    },
  );
  try {
    harness.orchestrator.handleMessage(makeMessage(125, '你好', 100, 504));
    await waitFor(() => harness.modelRequests.length >= 2);
    await sleep(100);
    const flattened = JSON.stringify(harness.modelRequests[1].messages);
    assert.ok(flattened.includes('手机上补充了一句'), 'silent self message visible to the running turn');
  } finally {
    await harness.cleanup();
  }
});

test('dataline message joins the master scope and the reply goes back over dataline', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '主AI收到，手机通道已接通。' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    // Inbound dataline: chatId/userId are the bot itself (same-account phone sync)
    const inbound = makeMessage(987654322, '手机数据线来的', 987654322, 600);
    inbound.rawData = { sub_type: 'dataline' };
    harness.orchestrator.handleDatalineMessage(inbound);
    await waitFor(() => harness.datalineSent.length === 1);
    assert.equal(harness.datalineSent[0].text, '主AI收到，手机通道已接通。');
    assert.equal(harness.sent.length, 0, 'nothing went out over QQ');

    // landed in the master scope history with the dataline source label
    const history = harness.repo.getMessages('private', '123456789');
    const entry = history.find((item) => item.text === '手机数据线来的');
    assert.ok(entry !== undefined, 'dataline message persisted in the master scope');
    assert.equal(entry.user_id, 123456789);
    assert.equal(entry.source_label, '主人-手机数据线');
  } finally {
    await harness.cleanup();
  }
});

test('master channel follows the latest ingress: QQ private flips replies back to QQ', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '走数据线' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'send_message', input: { content: '走QQ' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const inbound = makeMessage(987654322, '手机来的', 987654322, 610);
    inbound.rawData = { sub_type: 'dataline' };
    harness.orchestrator.handleDatalineMessage(inbound);
    await waitFor(() => harness.datalineSent.length === 1);

    // master then writes from their real QQ → replies return to QQ private chat
    harness.orchestrator.handleMessage(makeMessage(123456789, '回到QQ上说', 123456789, 611));
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '走QQ');
    assert.equal(harness.sent[0].chatType, 'private');
    assert.equal(harness.sent[0].targetId, 123456789);
    assert.equal(harness.datalineSent.length, 1, 'dataline untouched after the flip');
  } finally {
    await harness.cleanup();
  }
});
