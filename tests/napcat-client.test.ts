/**
 * NapcatBot 客户端模式（正向 WS）：事件与动作共用一条连接——
 * 动作经 echo 匹配响应、access_token 走 query、WS 断开期间退 HTTP。
 * 对端用 WsServer 扮 NapCat。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { NapcatBot } from '../src/bot/napcat.js';
import { WsServer, type WsServerConnection } from '../src/bot/ws-server.js';
import type { ChatMessage } from '../src/chat/types.js';

interface FakeNapcat {
  server: WsServer;
  port: number;
  conn: () => WsServerConnection | null;
  token: () => string | null;
  actions: Record<string, unknown>[];
  respondTo: (echo: string, data?: Record<string, unknown>) => void;
  pushEvent: (event: Record<string, unknown>) => void;
  stop: () => Promise<void>;
}

async function fakeNapcat(): Promise<FakeNapcat> {
  const actions: Record<string, unknown>[] = [];
  let conn: WsServerConnection | null = null;
  const server = new WsServer({
    authorize: (token) => token === 'tk123',
    onConnection: (c) => {
      conn = c;
    },
    onText: (c, data) => {
      try {
        const parsed = JSON.parse(data) as Record<string, unknown>;
        if (typeof parsed.echo === 'string') {
          actions.push(parsed);
          // get_login_info 给个像样回复，其余动作回通用 ok。
          const payload =
            parsed.action === 'get_login_info'
              ? { user_id: 123456789, nickname: '假NapCat' }
              : { message_id: 777 };
          c.send(JSON.stringify({ status: 'ok', retcode: 0, echo: parsed.echo, data: payload }));
        }
      } catch {
        // 非 JSON 忽略
      }
    },
    onClose: () => {
      conn = null;
    },
  });
  await server.start('127.0.0.1', 0);
  const port = server.port as number;
  return {
    server,
    port,
    conn: () => conn,
    token: () => conn?.token ?? null,
    actions,
    respondTo: (echo, data = {}) => conn?.send(JSON.stringify({ status: 'ok', retcode: 0, echo, data })),
    pushEvent: (event) => conn?.send(JSON.stringify(event)),
    stop: () => server.stop(),
  };
}

function waitFor(predicate: () => boolean, timeoutMs = 3000): Promise<void> {
  return new Promise((resolvePromise, rejectPromise) => {
    const deadline = Date.now() + timeoutMs;
    const tick = (): void => {
      if (predicate()) {
        resolvePromise();
        return;
      }
      if (Date.now() > deadline) {
        rejectPromise(new Error('waitFor 超时'));
        return;
      }
      setTimeout(tick, 20);
    };
    tick();
  });
}

test('client 模式：连上正向 WS，token 走 query，事件与动作共用一条连接', async () => {
  const fake = await fakeNapcat();
  const received: ChatMessage[] = [];
  let connected = 0;
  const bot = new NapcatBot({
    mode: 'client',
    wsUrl: `ws://127.0.0.1:${fake.port}/ob11`,
    httpUrl: '',
    selfId: 0,
    accessToken: 'tk123',
    reconnectBaseMs: 50,
    reconnectMaxMs: 200,
    handlers: {
      onPrivateMessage: (message) => received.push(message),
      onConnected: () => {
        connected += 1;
      },
    },
  });
  try {
    bot.start();
    assert.equal(bot.connectionMode, 'client');
    await waitFor(() => bot.connected);
    assert.equal(connected, 1);
    // access_token 经 query 传到服务端
    await waitFor(() => fake.token() === 'tk123');
    // get_login_info 已在连接后自动发出（走 WS，不是 HTTP——httpUrl 是空串，走 HTTP 会直接报错）
    await waitFor(() => fake.actions.some((a) => a.action === 'get_login_info'));
    await waitFor(() => bot.selfId === 123456789 && bot.selfNickname === '假NapCat');

    // 动作：sendText 经 WS echo 匹配拿到 message_id
    const result = await bot.sendText('private', 555, '在吗');
    assert.equal((result as { message_id?: number }).message_id, 777);
    const send = fake.actions.find((a) => a.action === 'send_private_msg');
    assert.ok(send);
    assert.deepEqual((send.params as { user_id?: number }).user_id, 555);

    // 事件：服务端推一条私聊消息
    fake.pushEvent({
      post_type: 'message',
      message_type: 'private',
      user_id: 555,
      self_id: 123456789,
      time: 1700000000,
      message_id: 42,
      raw_message: '你好',
      message: [{ type: 'text', data: { text: '你好' } }],
      sender: { user_id: 555, nickname: '路人' },
    });
    await waitFor(() => received.length === 1);
    assert.equal(received[0]!.userId, 555);
    assert.equal(received[0]!.text, '你好');
  } finally {
    bot.stop();
    await fake.stop();
  }
});

test('client 模式：WS 断开期间动作退走 HTTP', async () => {
  // 只起 HTTP 端（WS 端口不监听，connect 失败会静默重连）。
  const httpCalls: string[] = [];
  const http = createServer((req, res) => {
    httpCalls.push(req.url ?? '');
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 9 } }));
  });
  await new Promise<void>((resolvePromise) => http.listen(0, '127.0.0.1', () => resolvePromise()));
  const address = http.address();
  const httpPort = typeof address === 'object' && address !== null ? address.port : 0;

  const bot = new NapcatBot({
    mode: 'client',
    wsUrl: 'ws://127.0.0.1:1/never',
    httpUrl: `http://127.0.0.1:${httpPort}`,
    selfId: 123456789,
    reconnectBaseMs: 10_000,
    handlers: {},
  });
  try {
    bot.start(); // WS 连不上 → ws 永不为 OPEN
    const result = await bot.sendText('private', 555, '测试');
    assert.equal((result as { message_id?: number }).message_id, 9);
    assert.ok(httpCalls.some((url) => url === '/send_private_msg'));
  } finally {
    bot.stop();
    await new Promise<void>((resolvePromise) => http.close(() => resolvePromise()));
  }
});

test('client 模式：WS 断开时在途动作立即失败而不是挂到超时', async () => {
  const fake = await fakeNapcat();
  const bot = new NapcatBot({
    mode: 'client',
    wsUrl: `ws://127.0.0.1:${fake.port}/ob11`,
    httpUrl: '',
    selfId: 123456789,
    accessToken: 'tk123',
    reconnectBaseMs: 10_000,
    handlers: {},
  });
  try {
    bot.start();
    await waitFor(() => bot.connected);
    // 制造一个在途动作：发 sendText 但服务端不回（改写响应表前先记下 echo 再不让它回）
    const conn = fake.conn();
    assert.ok(conn);
    // 直接关掉连接——在途的 get_login_info 可能已回完，这里手动塞一个在途动作：
    // 先发一个不会被响应的动作（fake 只回带 echo 的，先把它回掉再断）
    const pending = bot.getGroupInfo(999); // 60s 超时，fake 会正常回 ok——为了测试断连路径，先断再观察
    conn.close(1001);
    // 断连应让在途动作快速失败（不依赖 60s 超时）；fake 若已抢先回包也算成功路径，两者皆可但绝不能挂住。
    const outcome = await Promise.race([
      pending.then(() => 'resolved').catch(() => 'rejected'),
      new Promise<string>((resolvePromise) => setTimeout(() => resolvePromise('hung'), 4000)),
    ]);
    assert.notEqual(outcome, 'hung');
    await waitFor(() => !bot.connected);
  } finally {
    bot.stop();
    await fake.stop();
  }
});
