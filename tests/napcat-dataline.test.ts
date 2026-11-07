/** QQ 数据线（我的电脑/我的手机）事件在 napcat 适配层的分流与回声去重。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { NapcatBot } from '../src/bot/napcat.js';

const SELF_ID = 987654321;

function datalineEvent(text: string, messageId: number): Record<string, unknown> {
  return {
    post_type: 'message_sent',
    message_type: 'private',
    sub_type: 'dataline',
    self_id: SELF_ID,
    user_id: SELF_ID,
    time: Math.floor(Date.now() / 1000),
    message_id: messageId,
    raw_message: text,
    message: [{ type: 'text', data: { text } }],
    sender: { user_id: SELF_ID, nickname: '我' },
  };
}

test('dataline events route to onDatalineMessage, never the self/normal paths', () => {
  const dataline: string[] = [];
  const self: string[] = [];
  const normal: string[] = [];
  const bot = new NapcatBot({
    wsUrl: 'ws://127.0.0.1:1',
    httpUrl: 'http://127.0.0.1:1',
    selfId: SELF_ID,
    handlers: {
      onDatalineMessage: (m) => dataline.push(m.text),
      onSelfMessage: (m) => self.push(m.text),
      onPrivateMessage: (m) => normal.push(m.text),
    },
  });
  // message_sent (selfSent=true) is how the bridge reports dataline traffic —
  // sender is always our own account on this channel.
  (bot as never as { handleIncomingMessage(d: Record<string, unknown>, s: boolean): void })
    .handleIncomingMessage(datalineEvent('手机来的', 12345), true);
  assert.deepEqual(dataline, ['手机来的']);
  assert.deepEqual(self, []);
  assert.deepEqual(normal, []);
});

test('echo of our own sendDatalineText is swallowed by the pending dedup', async () => {
  const dataline: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 777 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })) as typeof fetch;
  try {
    const bot = new NapcatBot({
      wsUrl: 'ws://127.0.0.1:1',
      httpUrl: 'http://127.0.0.1:1',
      selfId: SELF_ID,
      handlers: { onDatalineMessage: (m) => dataline.push(m.text) },
    });
    await bot.sendDatalineText('机器人回的');
    (bot as never as { handleIncomingMessage(d: Record<string, unknown>, s: boolean): void })
      .handleIncomingMessage(datalineEvent('机器人回的', 777), true);
    assert.deepEqual(dataline, [], 'own send echo must not re-enter as master input');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('private echo dedup keys on target_id (user_id is the bot itself in echoes)', async () => {
  const self: string[] = [];
  const normal: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 888 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })) as typeof fetch;
  try {
    const bot = new NapcatBot({
      wsUrl: 'ws://127.0.0.1:1',
      httpUrl: 'http://127.0.0.1:1',
      selfId: SELF_ID,
      handlers: {
        onSelfMessage: (m) => self.push(m.text),
        onPrivateMessage: (m) => normal.push(m.text),
      },
    });
    await bot.sendText('private', 555, '给私聊的回复');
    // message_sent 私聊回声：user_id=机器人自己，对方 QQ 在 target_id
    (bot as never as { handleIncomingMessage(d: Record<string, unknown>, s: boolean): void })
      .handleIncomingMessage({
        post_type: 'message_sent',
        message_type: 'private',
        sub_type: 'friend',
        self_id: SELF_ID,
        user_id: SELF_ID,
        target_id: 555,
        time: Math.floor(Date.now() / 1000),
        message_id: 999, // 故意不用发送回执的 888：只有 pending 内容键能拦住它
        raw_message: '给私聊的回复',
        message: [{ type: 'text', data: { text: '给私聊的回复' } }],
        sender: { user_id: SELF_ID, nickname: '我' },
      }, true);
    assert.deepEqual(self, [], '回声必须被 pending 去重吞掉');
    assert.deepEqual(normal, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
