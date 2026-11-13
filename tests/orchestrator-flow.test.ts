/**
 * End-to-end orchestrator flow tests with a stubbed model upstream and a fake
 * NapCat bot. Verifies the preemption semantics ported from the legacy
 * runtime: mid-turn pickup, post-turn single follow-up merge, send_message
 * persistence, and the crash-guard interrupt note.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { AiOrchestrator } from '../src/chat/orchestrator.js';
import { setDnsResolverForTests } from '../src/chat/net.js';
import { RelationGraph } from '../src/relations/graph.js';
import { ScopeRepository } from '../src/store/repository.js';
import { MessageArchive } from '../src/store/archive.js';
import { DiarySummarizer } from '../src/store/summarizer.js';
import { PromptStore } from '../src/prompt/store.js';
import { ModelManager } from '../src/models/manager.js';
import { KnowledgeBase } from '../src/knowledge/kb.js';
import { IntelCollector } from '../src/relations/collector.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import type { ChatMessage } from '../src/chat/types.js';
import { configureImageCache } from '../src/chat/image-cache.js';
import { configureMcBot } from '../src/chat/tools.js';

test('Minecraft speech wakes MC domain and send_message replies in game without QQ or master relay', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '在，一起玩吧' } },
    { tool: 'stay_silent', input: {} },
  ]);
  const modelFetch = globalThis.fetch;
  const gameSent: string[] = [];
  const online = '在线：DeepLive @ 127.0.0.1:25565（26.2 / 协议 776，Rust 后端，离线）';
  configureMcBot('http://mc-sidecar.test', 'test');
  globalThis.fetch = (async (url, init) => {
    if (String(url).startsWith('http://mc-sidecar.test')) {
      const target = new URL(String(url));
      if (target.pathname === '/status') return Response.json({ ok: true, message: online });
      assert.equal(target.pathname, '/chat'); gameSent.push(target.searchParams.get('message')!);
      return Response.json({ ok: true, message: 'sent' });
    }
    return modelFetch(url, init);
  }) as typeof fetch;
  try {
    harness.orchestrator.handleMessage({
      chatType: 'domain', chatId: 'mc', userId: -123, text: 'DeepLive 在吗', rawMessage: 'DeepLive 在吗',
      sender: { nickname: 'Minecraft:player' }, messageId: 'mc:test:1', mentionsSelf: true,
      timestamp: Date.now() / 1000, rawData: { source: 'minecraft_chat', mc_connection: online, mc_username: 'player' },
    });
    await waitFor(() => gameSent.length === 1);
    assert.deepEqual(gameSent, ['在，一起玩吧']);
    assert.equal(harness.sent.length, 0); assert.equal(harness.datalineSent.length, 0);
    assert.ok(harness.repo.getMessages('domain', 'mc').some((entry) => entry.source_kind === 'minecraft_chat'));
    assert.ok(!harness.repo.getMessages('master', 'core').some((entry) => entry.text.includes('在，一起玩吧')));
  } finally {
    await harness.cleanup(); configureMcBot('http://127.0.0.1:7660', '');
  }
});

interface Harness {
  orchestrator: AiOrchestrator;
  repo: ScopeRepository;
  sent: { chatType: string; targetId: number; text: string }[];
  sentImages: { chatType: string; targetId: number; file: string }[];
  datalineSent: { text: string }[];
  modelRequests: Record<string, unknown>[];
  modelsJson: string;
  relations: RelationGraph;
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
  | { tool: string; input: Record<string, unknown> }
  | { error: { status: number; body?: string } };

function anthropicResponse(script: ScriptedReply): Record<string, unknown> {
  if ('tool' in script) {
    return {
      content: [{ type: 'tool_use', id: 'call_1', name: script.tool, input: script.input }],
      usage: { input_tokens: 10, output_tokens: 10 },
    };
  }
  if ('error' in script) throw new Error('error script must be handled by the fetch stub');
  return {
    content: [{ type: 'text', text: script.text }],
    usage: { input_tokens: 10, output_tokens: 10 },
  };
}

async function makeHarness(
  replies: ScriptedReply[],
  onFirstModelCall?: () => void,
  /** Runs against the temp dir before the orchestrator starts (seed state). */
  beforeStart?: (dir: string) => Promise<void>,
  messageDebounceMs = 0,
  privateIdleMs = 0,
): Promise<Harness> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-test-'));
  await beforeStart?.(dir);
  const stateDir = join(dir, 'state');
  configureImageCache(join(stateDir, 'imgcache'));
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
  const sentImages: { chatType: string; targetId: number; file: string }[] = [];
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
    async sendImage(chatType: string, targetId: number, file: string) {
      sentImages.push({ chatType, targetId, file });
      return { message_id: 7000 + sentImages.length };
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
    // view_image_raw 的原图代取：返回一张 1x1 PNG（base64）。
    if (url.includes('img.example.com')) {
      const png = Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC',
        'base64',
      );
      return new Response(png, { status: 200, headers: { 'content-type': 'image/png' } });
    }
    assert.ok(url.includes('model.test'), `unexpected fetch: ${url}`);
    if (callIndex === 0) await onFirstModelCall?.();
    const rawBody = input instanceof Request ? await input.clone().text() : String(init?.body ?? '{}');
    const body = JSON.parse(rawBody) as Record<string, unknown>;
    modelRequests.push(body);
    const script = replies[Math.min(callIndex, replies.length - 1)];
    callIndex += 1;
    if ('error' in script) {
      return new Response(script.error.body ?? 'temporary upstream failure', {
        status: script.error.status,
        headers: { 'content-type': 'text/plain' },
      });
    }
    return new Response(JSON.stringify(anthropicResponse(script)), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;

  const orchestrator = new AiOrchestrator({
    messageDebounceMs,
    privateIdleMs,
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
    kb: new KnowledgeBase(join(dir, 'knowledge'), 'master:core'),
    tasksPath: join(dir, 'tasks.json'),
  });
  orchestrator.start();

  return {
    orchestrator,
    repo,
    sent,
    sentImages,
    datalineSent,
    modelRequests,
    modelsJson,
    relations,
    cleanup: async () => {
      globalThis.fetch = originalFetch;
      await orchestrator.stop();
      configureImageCache('');
      await rm(dir, { recursive: true, force: true });
    },
  };
}

test('user burst waits for quiet then reaches the model as one batch', async () => {
  const harness = await makeHarness([
    { tool: 'stay_silent', input: {} },
  ], undefined, undefined, 100);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '第一段转发', 100, 801));
    await sleep(60);
    assert.equal(harness.modelRequests.length, 0);
    harness.orchestrator.handleMessage(makeMessage(123, '第二段转发', 100, 802));
    await sleep(60);
    assert.equal(harness.modelRequests.length, 0, 'new message resets the deadline');
    await waitFor(() => harness.modelRequests.length === 1);
    const request = JSON.stringify(harness.modelRequests[0]);
    assert.ok(request.includes('第一段转发'));
    assert.ok(request.includes('第二段转发'));
    await sleep(150);
    assert.equal(harness.modelRequests.length, 1, 'one burst starts one model turn');
  } finally {
    await harness.cleanup();
  }
});

test('private quiet wake asks for a decision, rejects bare silence, persists sleep', async () => {
  const harness = await makeHarness([
    { tool: 'stay_silent', input: {} },
    { tool: 'stay_silent', input: {} },
    { tool: 'sleep', input: { until: '+1h', reason: '没有需要处理的事项，一小时后再考虑' } },
  ], undefined, undefined, 0, 80);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '暂时没事', 100, 901));
    await waitFor(() => harness.modelRequests.length === 3);
    const wake = JSON.stringify(harness.modelRequests[1]);
    assert.ok(wake.includes('防死寂决策'));
    await waitFor(() => {
      const path = join(dirname(harness.modelsJson), 'private-idle.json');
      return existsSync(path) && JSON.parse(readFileSync(path, 'utf8')).scopes['private:123'].mode === 'sleep';
    });
    await sleep(180);
    assert.equal(harness.modelRequests.length, 3);
    assert.equal(harness.sent.length, 0, 'decision trigger is internal, not a forced message');
  } finally { await harness.cleanup(); }
});

test('forever sleep stops autonomous contact, real incoming messages still wake the model', async () => {
  const harness = await makeHarness([
    { tool: 'sleep', input: { until: 'forever', reason: '用户明确不再主动联系' } },
    { tool: 'sleep', input: { until: '+1d', reason: '新消息已处理，明天再考虑' } },
  ], undefined, undefined, 0, 80);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '不要主动联系我', 100, 911));
    await waitFor(() => harness.modelRequests.length === 1);
    await sleep(180);
    assert.equal(harness.modelRequests.length, 1);
    harness.orchestrator.handleMessage(makeMessage(123, '我回来问一下', 100, 912));
    await waitFor(() => harness.modelRequests.length === 2);
    await sleep(180);
    assert.equal(harness.modelRequests.length, 2);
  } finally { await harness.cleanup(); }
});

test('non-triggering group messages extend an already triggered quiet window', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }], undefined, undefined, 100);
  try {
    harness.orchestrator.handleMessage({
      ...makeMessage(555, '先看这些', 100, 811), chatType: 'group', mentionsSelf: true,
    });
    await sleep(60);
    harness.orchestrator.handleMessage({
      ...makeMessage(555, '补充材料', 100, 812), chatType: 'group',
    });
    await sleep(60);
    assert.equal(harness.modelRequests.length, 0);
    await waitFor(() => harness.modelRequests.length === 1);
    assert.ok(JSON.stringify(harness.modelRequests[0]).includes('补充材料'));
  } finally {
    await harness.cleanup();
  }
});

test('internal request events do not wait for the user-message quiet interval', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }], undefined, undefined, 10_000);
  try {
    harness.orchestrator.handleQqRequestEvent({ request_type: 'friend', user_id: 100, comment: '申请' });
    await waitFor(() => harness.modelRequests.length === 1, 1000);
  } finally {
    await harness.cleanup();
  }
});

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

test('send_message result explains turn completion without blocking intentional follow-ups', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '第一条' } },
    { tool: 'send_message', input: { content: '确实还要补充一条' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, 'ok了', 100, 31));
    await waitFor(() => harness.modelRequests.length >= 3);
    assert.deepEqual(harness.sent.map((entry) => entry.text), ['第一条', '确实还要补充一条']);
    const requests = JSON.stringify(harness.modelRequests);
    assert.ok(requests.includes('如果本次回应已经完成，请调用 stay_silent 结束本轮'));
    assert.ok(!requests.includes('本次追加消息已拦截'));
  } finally {
    await harness.cleanup();
  }
});

test('progress message does not block the result message after a real tool runs', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '我去看一下' } },
    { tool: 'memory_list', input: {} },
    { tool: 'send_message', input: { content: '看完啦' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '帮我看看', 100, 32));
    await waitFor(() => harness.sent.length === 2);
    assert.deepEqual(harness.sent.map((entry) => entry.text), ['我去看一下', '看完啦']);
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
      // 发送拦截：第一轮的发送被「插队消息」拦下，模型看完新消息后重调才发出
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

    assert.equal(harness.sent.length, 1); // 被拦一次 + 重调放行，最终只发一条
    const secondRequest = harness.modelRequests[1];
    const flattened = JSON.stringify(secondRequest.messages);
    assert.ok(flattened.includes('插队消息'), 'queued message became round-2 trigger');
    assert.ok(flattened.includes('发送前暂停'), 'send interlock notice present in round 2 context');
    assert.ok(flattened.includes('补审提醒'), 'deferred-count reminder injected');
  } finally {
    await harness.cleanup();
  }
});

test('主级回话在用户回合进行中到达：折入同一回合并提示主动转告', async () => {
  const harness = await makeHarness(
    [
      { tool: 'memory_write', input: { text: '等待主级结论' } },
      { tool: 'send_message', input: { content: '主级结论到了，现在开不了。' } },
      { tool: 'stay_silent', input: {} },
    ],
    () => {
      // 实机故障形态：子AI的用户回合尚未结束，主级回话先进入 mailbox。
      harness.orchestrator.handleMessage({
        ...makeMessage(125, '[主级回话 from domain:onebot]\n结论：现在开不了。', 0, 56),
        rawData: { source: 'agent_message', peer_chat: 'domain:onebot' },
      });
    },
  );
  try {
    harness.orchestrator.handleMessage(makeMessage(125, '帮我问问能不能开', 100, 55));
    await waitFor(() => harness.sent.some((entry) => entry.text.includes('主级结论到了')));
    const callbackRound = JSON.stringify(harness.modelRequests[1]?.messages ?? '');
    assert.ok(callbackRound.includes('主级回话 from domain:onebot'), '主级回话应折入进行中的回合');
    assert.ok(callbackRound.includes('本轮包含主AI/领域中枢对先前请示的正式回话'), '折入后也应带主动转告提示');
  } finally {
    await harness.cleanup();
  }
});

test('发送拦截：思考期间进来的新消息会暂停第一次发送，模型看完重调才发出', async () => {
  const harness = await makeHarness(
    [
      { tool: 'send_message', input: { content: '先说这个' } },
      { tool: 'send_message', input: { content: '先说这个' } },
      { tool: 'stay_silent', input: {} },
    ],
    () => {
      // 第一轮模型调用进行中，用户插了一句——send_message 执行时必须拦下
      harness.orchestrator.handleMessage(makeMessage(126, '插一句', 100, 55));
    },
  );
  try {
    harness.orchestrator.handleMessage(makeMessage(126, '开场', 100, 54));
    await waitFor(() => harness.sent.length === 1, 3000);
    await sleep(150);
    assert.equal(harness.sent.filter((entry) => entry.text === '先说这个').length, 1, '重调后只真正发出一次');
    assert.equal(harness.sent[0].text, '先说这个');
    // 第二轮请求里必须同时带着拦截提示和新消息本身（fold 折入）
    const round2 = JSON.stringify(harness.modelRequests[1]?.messages ?? []);
    assert.ok(round2.includes('发送前暂停'), '拦截提示进了上下文');
    assert.ok(round2.includes('插一句'), '新消息被 fold 进下一轮触发');
    assert.ok(round2.includes('没有发出去'), '提示里明确说明未发送');
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

test('transient 500 on a user message turn is retried and recovers (no message lost)', async () => {
  // 第一次模型调用 500（瞬时）→ 协议层内部重试耗尽后回合级重试 → 成功发出去。
  // 500 重复 3 次：completeChat 自己对瞬时错误重试 3 次，都用 500 脚本，第 4 次调用才放行。
  const harness = await makeHarness([
    { error: { status: 500 } },
    { error: { status: 500 } },
    { error: { status: 500 } },
    { tool: 'send_message', input: { content: '恢复了' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(127, '别丢我', 100, 77));
    await waitFor(() => harness.sent.some((entry) => entry.text === '恢复了'), 6000);
    assert.equal(harness.sent.filter((entry) => entry.text === '恢复了').length, 1, '重试成功只发一次');
    const history = harness.repo.getMessages('private', '127');
    assert.ok(!history.some((entry) => String(entry.text).includes('上轮 AI 处理异常中断')), '重试成功就不该落中断便签');
  } finally {
    await harness.cleanup();
  }
});

test('crash guard: failed model call persists an interrupt note', async () => {  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }]);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    return new Response('upstream exploded', { status: 500 });
  }) as typeof fetch;
  try {
    harness.orchestrator.handleMessage(makeMessage(127, '触发错误', 100, 7));
    await waitFor(() => {
      const history = harness.repo.getMessages('private', '127');
      return history.some((entry) => String(entry.text).includes('上轮 AI 处理异常中断'));
    }, 8000); // 瞬时故障现在有 2 次有界重试（1s+2s 退避），便签要等重试耗尽才落
    const history = harness.repo.getMessages('private', '127');
    const note = history.find((entry) => String(entry.text).includes('上轮 AI 处理异常中断'));
    assert.ok(note !== undefined, 'interrupt note persisted');
  } finally {
    globalThis.fetch = originalFetch;
    await harness.cleanup();
  }
});

test('crash recovery: a checkpoint left by a dead process becomes an interrupt note on boot', async () => {
  const harness = await makeHarness([{ tool: 'stay_silent', input: {} }], undefined, async (dir) => {
    await mkdir(join(dir, '.checkpoint'), { recursive: true });
    await writeFile(join(dir, '.checkpoint', `${encodeURIComponent('private:131')}.json`), JSON.stringify({
      scopeKey: 'private:131', scopeType: 'private', scopeId: '131', at: Date.now(),
      executedTools: ['send_message'], toolRound: 0, triggerPreview: '在吗',
    }), 'utf-8');
  });
  try {
    const history = harness.repo.getMessages('private', '131');
    const note = history.find((entry) => String(entry.text).includes('上轮 AI 处理异常中断'));
    assert.ok(note !== undefined, 'interrupt note written at start()');
    assert.ok(String(note.text).includes('send_message'), 'note lists the side effects that happened');
  } finally {
    await harness.cleanup();
  }
});

test('checkpoint is cleared when a turn completes normally', async () => {
  let checkpointDir = '';
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '收到' } },
    { tool: 'stay_silent', input: {} },
  ], undefined, async (dir) => {
    checkpointDir = join(dir, '.checkpoint');
  });
  try {
    harness.orchestrator.handleMessage(makeMessage(132, '在吗', 100, 41));
    await waitFor(() => harness.modelRequests.length >= 2);
    await waitFor(() => !existsSync(checkpointDir) || readdirSync(checkpointDir).length === 0);
  } finally {
    await harness.cleanup();
  }
});

test('send ledger: identical send_message within TTL is refused, not double-sent', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '同一句话' } },
    { tool: 'send_message', input: { content: '同一句话' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(133, '在吗', 100, 51));
    await waitFor(() => harness.modelRequests.length >= 3);
    assert.deepEqual(harness.sent.map((entry) => entry.text), ['同一句话']);
    assert.ok(JSON.stringify(harness.modelRequests).includes('系统拦截了本次重复发送'));
  } finally {
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
    const masterHistory = harness.repo.getMessages('master', 'core');
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
    // 摘要送到中枢（若中枢收尾吃掉一条，只落主AI会话，不发 QQ）。
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
      const masterHistory = harness.repo.getMessages('master', 'core');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:onebot'));
    });
    const masterHistory = harness.repo.getMessages('master', 'core');
    const relay = masterHistory.find((entry) => String(entry.text).includes('[子AI上报 from domain:onebot'));
    assert.ok(String(relay!.text).includes('考研情报已沉淀'));
    // 主AI 没有 QQ 窗口：中枢收尾即便吃到 send_message 也只落主AI会话
    assert.equal(harness.sent.length, 0, '主AI/中枢发言不应发到任何 QQ 窗口');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('master conclusion relays back to the reporting hub scope', async () => {
  const harness = await makeHarness([{ text: '已收到，情报已归档。' }]);
  try {
    const injected: ChatMessage = {
      chatType: 'master',
      chatId: 'core',
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
      const masterHistory = harness.repo.getMessages('master', 'core');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:bili'));
    });
    const masterHistory = harness.repo.getMessages('master', 'core');
    const digest = masterHistory.find((entry) => String(entry.text).includes('[子AI上报 from domain:bili'));
    assert.ok(String(digest!.text).includes('视频要点'));
    // 4) 全程零 QQ 发送（虚拟 scope 无窗口）
    assert.equal(harness.sent.length, 0);
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('master delegate_to_child injects an instruction into the target scope', async () => {
  const harness = await makeHarness([
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
    // 面板控制台直接在主AI会话（master:core）里下达跨会话指令
    harness.orchestrator.handleAdminWebuiMessage('帮我跟 777 说一下聚会改时间');
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
      const masterHistory = harness.repo.getMessages('master', 'core');
      return masterHistory.some((entry) => String(entry.text).includes('[子AI上报 from domain:mc'));
    });
    const masterHistory = harness.repo.getMessages('master', 'core');
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

test('send_image duplicate guard spans model rounds and does not send the same image twice', async () => {
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  const harness = await makeHarness([
    { tool: 'send_image', input: { file: 'https://img.example.com/same.png' } },
    { tool: 'send_image', input: { file: 'https://img.example.com/same.png' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '发图', 100, 14));
    await waitFor(() => harness.modelRequests.length >= 3);
    assert.equal(harness.sentImages.length, 1);
    assert.ok(JSON.stringify(harness.modelRequests[2]).includes('已经把这张图发出去了'));
  } finally {
    setDnsResolverForTests(null);
    await harness.cleanup();
  }
});

test('schedule_image caches the inbound original and sends it directly without another model turn', async () => {
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  const harness = await makeHarness([
    { tool: 'schedule_image', input: { at: '+1s' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const imageMessage = makeMessage(123, '10分钟后把这张照片再发给我', 100, 3);
    imageMessage.rawMessage = '[CQ:image,file=cat.jpg,url=https://img.example.com/cat.jpg]';
    harness.orchestrator.handleMessage(imageMessage);
    await waitFor(() => harness.sentImages.length === 1, 8000);
    assert.equal(harness.sentImages[0].targetId, 123);
    assert.ok(harness.sentImages[0].file.startsWith('base64://'), 'sends the durable cached bytes, not the expiring QQ URL');
    assert.equal(harness.modelRequests.length, 2, 'only schedule + stay_silent; firing needs no model call');
    const history = harness.repo.getMessages('private', '123');
    assert.ok(history.some((entry) => entry.user_id === 987654322 && entry.text === '[图片]'));
    await harness.repo.flush();
  } finally {
    setDnsResolverForTests(null);
    await harness.cleanup();
  }
});

test('scheduled image with an unrecoverable original fails once instead of retrying a dead URL forever', async () => {
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  const harness = await makeHarness([
    { tool: 'schedule_image', input: { at: '+2s' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const imageMessage = makeMessage(123, '稍后发图', 100, 15);
    imageMessage.rawMessage = '[CQ:image,file=gone.jpg,url=https://img.example.com/gone.jpg]';
    harness.orchestrator.handleMessage(imageMessage);
    await waitFor(() => harness.modelRequests.length >= 2);
    configureImageCache('');
    const harnessFetch = globalThis.fetch;
    globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.includes('img.example.com')) throw new Error('expired original');
      return harnessFetch(input, init);
    }) as typeof fetch;

    await waitFor(() => harness.sent.some((entry) => entry.text.includes('定时图片发送失败')), 8000);
    assert.equal(harness.sentImages.length, 0);
    await sleep(1200);
    assert.equal(harness.sent.filter((entry) => entry.text.includes('定时图片发送失败')).length, 1);
    assert.equal(harness.modelRequests.length, 2, 'failure is deterministic and does not wake the model');
  } finally {
    setDnsResolverForTests(null);
    await harness.cleanup();
  }
});

test('scheduled turn retries exhausted transient model failures before any tool side effect', async () => {
  const harness = await makeHarness([
    { tool: 'create_task', input: { kind: 'set_alarm', at: '+1s', note: '故障后提醒' } },
    { tool: 'stay_silent', input: {} },
    { error: { status: 503 } },
    { error: { status: 503 } },
    { error: { status: 503 } },
    { tool: 'send_message', input: { content: '恢复后提醒到了' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.orchestrator.handleMessage(makeMessage(123, '设置一个闹钟', 100, 2));
    await waitFor(() => harness.sent.some((entry) => entry.text === '恢复后提醒到了'), 10000);
    assert.equal(harness.sent.filter((entry) => entry.text === '恢复后提醒到了').length, 1);
    assert.ok(harness.modelRequests.length >= 6, 'three protocol attempts precede the scheduled-turn retry');
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

test('view_image_raw shows the image once, then expires in later rounds', async () => {
  // img.example.com 现实里解析不到：桩掉 DNS，让 SSRF 守卫放行。
  setDnsResolverForTests(async () => [{ address: '93.184.216.34', family: 4 }]);
  const harness = await makeHarness([
    { tool: 'view_image_raw', input: { index: 1 } },
    { tool: 'send_message', input: { content: '看到了原图' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const imageMessage = makeMessage(132, '', 100, 12);
    imageMessage.rawMessage = '[CQ:image,file=x.jpg,url=https://img.example.com/raw.jpg]';
    harness.orchestrator.handleMessage(imageMessage);
    await waitFor(() => harness.sent.length === 1);
    await sleep(150);

    // 3 model calls: round 1 (tool call) → round 2 (sees the image) → close-out.
    assert.equal(harness.modelRequests.length, 3);
    const round2 = JSON.stringify(harness.modelRequests[1]);
    assert.ok(round2.includes('"type":"base64"'), 'raw image inlined for the round right after the call');
    assert.ok(round2.includes('"media_type":"image/png"'), 'png media type on the inline block');
    const round3 = JSON.stringify(harness.modelRequests[2]);
    assert.ok(round3.includes('【原图已过期，如需查看，请再次调用 view_image_raw 工具获取原图】'), 'later rounds see the expired marker');
    assert.ok(!round3.includes('"type":"base64"'), 'the image itself is gone after one round');
    await harness.repo.flush();
  } finally {
    setDnsResolverForTests(null);
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
    const history = harness.repo.getMessages('master', 'core');
    const entry = history.find((item) => item.text === '手机数据线来的');
    assert.ok(entry !== undefined, 'dataline message persisted in the master scope');
    assert.equal(entry.user_id, 123456789);
    assert.equal(entry.source_label, '主人-手机数据线');
  } finally {
    await harness.cleanup();
  }
});

test('master scope has no QQ window: owner private chat is an ordinary child, panel ingress replies stay off QQ', async () => {
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '走数据线' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'send_message', input: { content: '号主私聊子AI回话' } },
    { tool: 'stay_silent', input: {} },
    { tool: 'send_message', input: { content: '面板回话' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const inbound = makeMessage(987654322, '手机来的', 987654322, 610);
    inbound.rawData = { sub_type: 'dataline' };
    harness.orchestrator.handleDatalineMessage(inbound);
    await waitFor(() => harness.datalineSent.length === 1);

    // 号主从真 QQ 私聊过来 → 普通下级会话 private:<号主>，不进主AI会话
    harness.orchestrator.handleMessage(makeMessage(123456789, '私聊说一句', 123456789, 611));
    await waitFor(() => harness.sent.length === 1);
    assert.equal(harness.sent[0].text, '号主私聊子AI回话');
    assert.equal(harness.sent[0].chatType, 'private');
    assert.equal(harness.sent[0].targetId, 123456789);
    assert.ok(!harness.repo.getMessages('master', 'core').some((e) => e.text === '私聊说一句'), 'owner QQ chat stays out of the master scope');

    // 面板控制台进主AI会话 → 回复只落主AI会话：不发 QQ，也不再走数据线
    harness.orchestrator.handleAdminWebuiMessage('面板来的');
    await waitFor(() => harness.repo.getMessages('master', 'core').some((e) => e.text === '面板回话'));
    assert.equal(harness.sent.length, 1, 'master reply never goes out over QQ');
    assert.equal(harness.datalineSent.length, 1, 'panel ingress flips the master channel off dataline');
  } finally {
    await harness.cleanup();
  }
});

test('请示回执：带 need_reply 的上报未获回话，回合结束被系统追问一次', async () => {
  const harness = await makeHarness([
    { tool: 'stay_silent', input: {} }, // hub 第一回合：静默结案（违规）
    { tool: 'stay_silent', input: {} }, // 回执提醒回合：仍静默（提醒不连环自激）
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const injected: ChatMessage = {
      chatType: 'domain',
      chatId: 'onebot',
      userId: 0,
      text: '[子AI上报 from private:1059681169 type=system_operation need_reply=1]\n用户请求打开 mc2d 总开关，请确认是否授权。',
      rawMessage: '[子AI上报 from private:1059681169 type=system_operation need_reply=1]\n用户请求打开 mc2d 总开关，请确认是否授权。',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_ack_1',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', relayed_from: 'private:1059681169', ack_required: 'private:1059681169' },
    };
    harness.orchestrator.handleMessage(injected);
    // 静默结案 → 系统注入 [回执提醒]，提醒里列出来源会话与摘要
    await waitFor(() => {
      const hubHistory = harness.repo.getMessages('domain', 'onebot');
      return hubHistory.some((entry) => String(entry.text).includes('[回执提醒]'));
    });
    const hubHistory = harness.repo.getMessages('domain', 'onebot');
    const reminder = hubHistory.find((entry) => String(entry.text).includes('[回执提醒]'));
    assert.ok(String(reminder!.text).includes('private:1059681169'), '提醒应列出来源会话');
    assert.ok(String(reminder!.text).includes('mc2d'), '提醒应带上报摘要');
    // 提醒触发第二轮模型调用，请求里应含回执提醒内容
    await waitFor(() => harness.modelRequests.length >= 2);
    const reminderRequest = harness.modelRequests[1];
    assert.ok(JSON.stringify(reminderRequest).includes('回执提醒'), '第二轮请求应携带回执提醒');
    // 提醒自身不带 ack 标记：不会有第三次追问
    await new Promise((resolve) => setTimeout(resolve, 400));
    const reminders = harness.repo.getMessages('domain', 'onebot')
      .filter((entry) => String(entry.text).includes('[回执提醒]'));
    assert.equal(reminders.length, 1, '提醒最多一次，不得连环自激');
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});

test('请示回执：domain_message 直回来源会话即算回执，子AI收到结论后主动转告', async () => {
  const harness = await makeHarness([
    { tool: 'domain_message', input: { target: 'private:1059681169', content: '驳回：mc2d 总开关只有号主本人能开。' } },
    { tool: 'stay_silent', input: {} }, // hub 收尾
    { tool: 'send_message', input: { content: '问到了，mc2d 总开关只有号主本人能开。' } }, // 子AI主动转告
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    const injected: ChatMessage = {
      chatType: 'domain',
      chatId: 'onebot',
      userId: 0,
      text: '[子AI上报 from private:1059681169 type=system_operation need_reply=1]\n用户请求打开 mc2d 总开关，请确认是否授权。',
      rawMessage: '[子AI上报 from private:1059681169 type=system_operation need_reply=1]\n用户请求打开 mc2d 总开关，请确认是否授权。',
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: 'sys_test_ack_2',
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', relayed_from: 'private:1059681169', ack_required: 'private:1059681169' },
    };
    harness.orchestrator.handleMessage(injected);
    // 回话注入来源会话
    await waitFor(() => {
      const childHistory = harness.repo.getMessages('private', '1059681169');
      return childHistory.some((entry) => String(entry.text).includes('[主级回话 from domain:onebot]'));
    });
    const childHistory = harness.repo.getMessages('private', '1059681169');
    const reply = childHistory.find((entry) => String(entry.text).includes('[主级回话 from domain:onebot]'));
    assert.ok(String(reply!.text).includes('驳回'), '回话内容应送达来源会话');
    // 子AI收到回话的那一轮必须带主动转告提示，并实际发消息给用户。
    await waitFor(() => harness.sent.some((entry) => entry.chatType === 'private' && entry.targetId === 1059681169));
    assert.ok(
      harness.sent.some((entry) => entry.targetId === 1059681169 && entry.text.includes('只有号主本人能开')),
      '子AI不应把已经收到的结论静默扣下',
    );
    const childRequest = harness.modelRequests.find((request) => JSON.stringify(request).includes('本轮包含主AI/领域中枢对先前请示的正式回话'));
    assert.ok(childRequest, '主级回话回合应明确提示 send_message 主动转告');
    // 已回执：不得出现 [回执提醒]
    await new Promise((resolve) => setTimeout(resolve, 400));
    const hubHistory = harness.repo.getMessages('domain', 'onebot');
    assert.ok(
      !hubHistory.some((entry) => String(entry.text).includes('[回执提醒]')),
      '已回执的请示不应再被追问',
    );
    await harness.repo.flush();
  } finally {
    await harness.cleanup();
  }
});


test('mobile user gets a separate child context and reply transport without master or QQ output', async () => {
  const id = 'a28f7d24-155c-493b-aef9-166a91beef70';
  const harness = await makeHarness([
    { tool: 'send_message', input: { content: '独立会话的回答' } },
    { tool: 'stay_silent', input: {} },
  ]);
  try {
    harness.repo.appendMessage('master', 'core', { user_id: 0, nickname: '后台', text: '隐藏的主AI上下文', timestamp: 1 });
    harness.orchestrator.handleMobileMessage(id, '手机用户的消息');
    await waitFor(() => harness.repo.getMessages('app', id).some(e => e.raw_source === 'app_ai'));
    const history = harness.repo.getMessages('app', id);
    assert.ok(history.some(e => e.text === '手机用户的消息' && e.user_id === -1));
    assert.ok(history.some(e => e.text === '独立会话的回答' && e.raw_source === 'app_ai'));
    assert.equal(harness.sent.length, 0);
    assert.equal(harness.datalineSent.length, 0);
    assert.ok(!harness.repo.getMessages('master', 'core').some(e => e.text === '手机用户的消息'));
    const request = JSON.stringify(harness.modelRequests[0]);
    assert.ok(!request.includes('隐藏的主AI上下文'));
    assert.ok(request.includes('你是独立的用户会话 AI'));
    const tools = harness.modelRequests[0]!.tools as {name: string}[];
    assert.ok(!tools.some(t => ['delegate_to_child', 'message_scope', 'send_image'].includes(t.name)));
  } finally { await harness.cleanup(); }
});
