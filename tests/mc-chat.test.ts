import test from 'node:test';
import assert from 'node:assert/strict';
import { McChatBridge, parseMcChat, mcChatTriggerRate } from '../src/chat/mc-chat.js';
import { shouldTrigger } from '../src/chat/trigger.js';
import { sourceKindOf } from '../src/chat/source.js';
import type { ChatMessage } from '../src/chat/types.js';
import { configureMcBot, sendMinecraftText } from '../src/chat/tools.js';

const online = '在线：DeepLive @ 127.0.0.1:25565（26.2 / 协议 776，Rust 后端，离线）';
const status = { ok: true, message: `${online}\n玩家列表（2）：DeepLive, player` };
const event = (next: number, text: string) => ({ ok: true, next, message: `[13:12:57] chat: ${text}\nnext=${next}` });

test('only actual speech and explicit whispers parse; system/joins/tool echoes do not', () => {
  assert.deepEqual(parseMcChat('[13:00:00] chat: <player> 在吗\n[13:00:01] chat: player joined the game\n[13:00:02] system: 我发送：你好\n[13:00:03] whisper: <player> 私聊: hi'), [
    { username: 'player', text: '在吗', whisper: false },
    { username: 'player', text: 'hi', whisper: true },
  ]);
});

test('bridge primes cursor, delivers once, filters self and keeps game identity separate', async () => {
  const messages: ChatMessage[] = [];
  const urls: string[] = [];
  let batch = event(10, '<player> old');
  const bridge = new McChatBridge('http://local', 'test', (m) => messages.push(m), (async (url, init) => {
    urls.push(String(url));
    assert.equal((init?.headers as Record<string, string>).authorization, 'Bearer test');
    return Response.json(String(url).endsWith('/status') ? status : batch);
  }) as typeof fetch);
  await bridge.pollOnce(); assert.equal(messages.length, 0);
  batch = event(11, '<player> DeepLive 在吗');
  await bridge.pollOnce(); await bridge.pollOnce();
  assert.equal(messages.length, 1);
  assert.equal(messages[0]!.mentionsSelf, true);
  assert.equal(messages[0]!.chatType, 'domain');
  assert.equal(messages[0]!.chatId, 'mc');
  assert.equal(sourceKindOf(messages[0]!), 'minecraft_chat');
  assert.ok(messages[0]!.userId < 0);
  assert.equal(messages[0]!.rawData.mc_solo, true);
  assert.ok(urls.some((u) => u.includes('since=10')));
  batch = event(12, '<DeepLive> 在'); await bridge.pollOnce();
  assert.equal(messages.length, 1);
  batch = event(13, '<player> 那一起走'); await bridge.pollOnce();
  assert.equal(messages[1]!.rawData.mc_followup, true);
  // Sidecar cursor reset must not replay retained old speech.
  batch = event(1, '<player> old after restart'); await bridge.pollOnce();
  assert.equal(messages.length, 2);
  bridge.stop(); batch = event(2, '<player> after stop'); await bridge.pollOnce();
  assert.equal(messages.length, 2);
});

test('failed fetch does not advance the cursor; poll retries the same events', async () => {
  let fail = false; let next = 1; const messages: ChatMessage[] = [];
  const bridge = new McChatBridge('http://local', '', (m) => messages.push(m), (async (url) => {
    if (String(url).endsWith('/status')) return Response.json(status);
    if (fail) return new Response('', { status: 503 });
    return Response.json(event(next, '<player> hi'));
  }) as typeof fetch);
  await bridge.pollOnce(); next = 2; fail = true; await bridge.pollOnce();
  fail = false; await bridge.pollOnce(); assert.equal(messages.length, 1); bridge.stop();
});

test('stop during fetch never delivers a late message', async () => {
  let batch = event(1, '<player> old'); let pause: (() => void) | undefined;
  let block = false; const messages: ChatMessage[] = [];
  const bridge = new McChatBridge('http://local', '', (m) => messages.push(m), (async (url) => {
    if (String(url).endsWith('/status')) return Response.json(status);
    if (block) await new Promise<void>((resolve) => { pause = resolve; });
    return Response.json(batch);
  }) as typeof fetch);
  await bridge.pollOnce(); block = true; batch = event(2, '<player> new');
  const pending = bridge.pollOnce();
  while (!pause) await new Promise((resolve) => setTimeout(resolve, 1));
  bridge.stop(); pause(); await pending; assert.equal(messages.length, 0);
});

test('adaptive gate prioritizes direct speech and solo questions, reduces busy-room chance', () => {
  const m: ChatMessage = { chatType: 'domain', chatId: 'mc', userId: -2, text: '怎么走？', rawMessage: '怎么走？',
    sender: { nickname: 'Minecraft:player' }, messageId: 'test', timestamp: Date.now() / 1000,
    mentionsSelf: false, rawData: { source: 'minecraft_chat', mc_solo: true } };
  const agent = { enabled: true, triggerRate: 0.01, triggerWords: [] };
  assert.equal(mcChatTriggerRate(m, 0.01), 1);
  assert.equal(shouldTrigger(m, m.text, agent), true);
  m.rawData.mc_solo = false; m.rawData.mc_recent_count = 40;
  assert.equal(mcChatTriggerRate(m, 0.01), 0.01);
  m.mentionsSelf = true; assert.equal(shouldTrigger(m, m.text, { ...agent, triggerRate: 0 }), true);
  assert.equal(shouldTrigger(m, m.text, { ...agent, enabled: false }), false);
  m.mentionsSelf = false; assert.equal(shouldTrigger(m, m.text, { ...agent, triggerRate: 0 }), false);
  assert.equal(shouldTrigger(m, m.text, { ...agent, triggerWords: ['怎么'] }), true);
});

test('game send_message uses sidecar, validates connection and prevents command injection', async () => {
  const original = globalThis.fetch; const urls: string[] = [];
  globalThis.fetch = (async (url) => {
    urls.push(String(url)); return Response.json(String(url).endsWith('/status') ? status : { ok: true, message: 'sent' });
  }) as typeof fetch;
  configureMcBot('http://mc-test', 'test');
  try {
    await sendMinecraftText('在，一起走吧', online);
    assert.ok(urls[1]!.startsWith('http://mc-test/chat?message='));
    await sendMinecraftText('私聊回复', online, 'player');
    assert.match(decodeURIComponent(urls.at(-1)!), /command=\/tell player 私聊回复/);
    await assert.rejects(sendMinecraftText('hello', 'different server'), /连接已变化/);
    await assert.rejects(sendMinecraftText('/op player', online), /普通聊天/);
    await assert.rejects(sendMinecraftText('a'.repeat(241), online), /240/);
  } finally { globalThis.fetch = original; configureMcBot('http://127.0.0.1:7660', ''); }
});
