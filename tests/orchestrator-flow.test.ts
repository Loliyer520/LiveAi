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
import { IntelCollector } from '../src/relations/collector.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import type { ChatMessage } from '../src/chat/types.js';

interface Harness {
  orchestrator: AiOrchestrator;
  repo: ScopeRepository;
  sent: { chatType: string; targetId: number; text: string }[];
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
  const intel = new IntelCollector(relations, repo, models, () => 3592217365, false); // disabled: relations tests cover it

  const sent: { chatType: string; targetId: number; text: string }[] = [];
  const modelRequests: Record<string, unknown>[] = [];
  let callIndex = 0;

  const fakeBot = {
    selfId: 3592217365,
    async sendText(chatType: string, targetId: number, text: string) {
      sent.push({ chatType, targetId, text });
      return { message_id: 9000 + sent.length };
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
      masterQq: 241898129,
      adminQq: 241898129,
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
    tasksPath: join(dir, 'tasks.json'),
  });
  orchestrator.start();

  return {
    orchestrator,
    repo,
    sent,
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
    const outbound = history.find((entry) => entry.user_id === 3592217365 && entry.text === '你好呀，我在。');
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
      const outbound = history.find((entry) => entry.user_id === 3592217365 && entry.text === text);
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
    const system = secondRequest.system as { text: string }[];
    assert.ok(system.some((block) => block.text.includes('补审提醒')), 'deferred-count reminder injected');
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

test('master loop: child report reaches master scope, conclusion relays back', async () => {
  const harness = await makeHarness([
    { tool: 'notify_master', input: { text: '小明说他在准备考研', request_type: 'intel' } },
    { tool: 'stay_silent', input: {} },
    { text: '已收到，情报已归档。' },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '跟主AI说一下我在准备考研', 100, 1));
    // 1) child report lands in the master scope as an internal relay
    await waitFor(() => {
      const masterHistory = harness.repo.getMessages('private', '241898129');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from private:123'));
    });
    // 2) master scope turn ran with the master tool set (no notify_master self-loop)
    await waitFor(() => harness.modelRequests.length >= 3);
    const masterRequest = harness.modelRequests[2];
    const masterToolNames = (masterRequest.tools as { name: string }[]).map((tool) => tool.name);
    assert.ok(masterToolNames.includes('relation_write'), 'master scope gets relation_write');
    assert.ok(!masterToolNames.includes('notify_master'), 'master never relays to itself');
    // 3) master's plain-text conclusion relays back into the reporting child scope
    await waitFor(() => {
      const childHistory = harness.repo.getMessages('private', '123');
      return childHistory.some((entry) => String(entry.text).includes('[主AI回传]'));
    });
    const childHistory = harness.repo.getMessages('private', '123');
    const callback = childHistory.find((entry) => String(entry.text).includes('[主AI回传]'));
    assert.ok(String(callback!.text).includes('已收到，情报已归档'));
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
    harness.orchestrator.handleMessage(makeMessage(241898129, '帮我跟 777 说一下聚会改时间', 241898129, 10));
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

    const system = harness.modelRequests[0].system as { text: string }[];
    const background = system.map((block) => block.text).join('\n');
    assert.ok(background.includes('本次消息包含 1 张图片'), 'image hint in background');
    assert.ok(background.includes('view_image'), 'hint points at the tool');

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
    const system = (harness.modelRequests[0].system as { text: string }[]).map((block) => block.text).join('\n');
    assert.ok(system.includes('群聊参与度'), 'trigger info section present in group scopes');
    assert.ok(system.includes('1.0%'), 'default trigger rate 0.01 shown');
    assert.ok(system.includes('尽量少说话'), '没融入少说话 guidance present');
    assert.ok(system.includes('trigger_config'), 'points at the self-tuning tool');
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
