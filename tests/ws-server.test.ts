import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WsServer, type WsServerConnection } from '../src/bot/ws-server.js';
import { NapcatBot } from '../src/bot/napcat.js';
import type { ChatMessage } from '../src/chat/types.js';

const HOST = '127.0.0.1';

async function startOnEphemeralPort(handlers: ConstructorParameters<typeof WsServer>[0]): Promise<{ server: WsServer; port: number }> {
  const server = new WsServer(handlers);
  await server.start(HOST, 0);
  return { server, port: server.port as number };
}

function connect(port: number, path = '/?access_token=t0k'): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://${HOST}:${port}${path}`);
    ws.onopen = () => resolve(ws);
    ws.addEventListener('error', (ev) => reject(new Error(String((ev as unknown as { message?: string }).message ?? '') || 'connect failed')));
  });
}

const nextMessage = (ws: WebSocket): Promise<string> =>
  new Promise((resolve) => {
    ws.addEventListener('message', (ev) => resolve(String(ev.data)), { once: true });
  });

test('反向 WS：token 校验 + 双工收发 + 大消息', async () => {
  const conns: WsServerConnection[] = [];
  const texts: string[] = [];
  const { server, port } = await startOnEphemeralPort({
    authorize: (token) => token === 't0k',
    onConnection: (conn) => conns.push(conn),
    onText: (_conn, data) => texts.push(data),
    onClose: () => {},
  });
  try {
    // 错 token 被拒（连接建立失败；undici 不透传 401 原因，只验拒绝）
    await assert.rejects(connect(port, '/?access_token=wrong'));
    // 正确 token 接入
    const ws = await connect(port);
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(conns.length, 1);

    // 客户端 → 服务端
    ws.send(JSON.stringify({ post_type: 'message', message_type: 'private', user_id: 123, message: 'hi' }));
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(texts.length, 1);

    // 服务端 → 客户端
    conns[0].send('server-hello');
    assert.equal(await nextMessage(ws), 'server-hello');

    // 大消息（>64KB，触发 16bit 长度编码）
    const big = 'x'.repeat(80_000);
    conns[0].send(big);
    const gotBig = await nextMessage(ws);
    assert.equal(gotBig.length, 80_000);
    ws.close();
  } finally {
    await server.stop();
  }
});

test('NapcatBot 反向模式：事件进 handler，sendText 走 ws 动作并收响应', async () => {
  const received: ChatMessage[] = [];
  const bot = new NapcatBot({
    wsUrl: 'ws://127.0.0.1:1/unused',
    httpUrl: 'http://127.0.0.1:1/unused',
    selfId: 1059681169,
    accessToken: 't0k',
    handlers: { onPrivateMessage: (m) => received.push(m) },
    reverseWsPort: 0,
    reverseWsHost: HOST,
  });
  bot.start();
  await new Promise((r) => setTimeout(r, 50));
  const port = (bot as unknown as { wsServer: WsServer | null }).wsServer?.port;
  assert.ok(typeof port === 'number' && port > 0, 'reverse ws server should be started');

  const ws = await connect(port);
  ws.send(JSON.stringify({
    post_type: 'message',
    message_type: 'private',
    user_id: 3319085316,
    self_id: 1059681169,
    message_id: 77,
    message: [{ type: 'text', data: { text: '读书' } }],
    raw_message: '读书',
    time: Math.floor(Date.now() / 1000),
  }));
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(received.length, 1);
  assert.equal(received[0].userId, 3319085316);

  // sendText → 客户端收到 action 请求 → 回响应 → promise 兑现
  const sendPromise = bot.sendText('private', 3319085316, '好的');
  const request = JSON.parse(await nextMessage(ws)) as { action: string; params: { user_id: number }; echo: string };
  assert.equal(request.action, 'send_private_msg');
  assert.equal(request.params.user_id, 3319085316);
  ws.send(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 88 }, echo: request.echo }));
  const result = (await sendPromise) as { message_id: number };
  assert.equal(result.message_id, 88);

  // 动作失败路径
  const failPromise = bot.sendText('private', 1, 'x');
  const req2 = JSON.parse(await nextMessage(ws)) as { echo: string };
  ws.send(JSON.stringify({ status: 'failed', retcode: 200, data: null, message: 'BRIDGE_NOT_READY', echo: req2.echo }));
  await assert.rejects(failPromise, /BRIDGE_NOT_READY/);

  bot.stop();
  ws.close();
});

test('NapcatBot 反向模式未连接时动作直接失败', async () => {
  const bot = new NapcatBot({
    wsUrl: 'ws://127.0.0.1:1/unused',
    httpUrl: 'http://127.0.0.1:1/unused',
    selfId: 0,
    handlers: {},
    reverseWsPort: 0,
    reverseWsHost: HOST,
  });
  await assert.rejects(bot.sendText('private', 123, 'x'), /REVERSE_WS_NOT_CONNECTED/);
  bot.stop();
});

test('畸形 access_token 不崩服务端，后续正常连接不受影响', async () => {
  const texts: string[] = [];
  const { server, port } = await startOnEphemeralPort({
    authorize: (token) => token === 't0k',
    onConnection: () => {},
    onText: (_conn, data) => texts.push(data),
    onClose: () => {},
  });
  try {
    // %E0%A4%A 是非法 UTF-8 百分号序列：decodeURIComponent 会抛 URIError。
    await assert.rejects(connect(port, '/?access_token=%E0%A4%A'));
    const ws = await connect(port);
    ws.send('still-alive');
    await new Promise((r) => setTimeout(r, 30));
    assert.deepEqual(texts, ['still-alive']);
    ws.close();
  } finally {
    await server.stop();
  }
});

test('超大帧按 1009 关闭连接', async () => {
  const closed: number[] = [];
  const { server, port } = await startOnEphemeralPort({
    onConnection: () => {},
    onText: () => {},
    onClose: () => {},
  });
  try {
    const ws = await connect(port, '/');
    const code = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('server did not close oversized frame')), 5000);
      ws.addEventListener('close', (ev) => {
        clearTimeout(timer);
        resolve(ev.code);
      });
      // 单帧上限 8MB，发 9MB 必被拒。
      ws.send('x'.repeat(9 * 1024 * 1024));
    });
    closed.push(code);
    assert.equal(closed[0], 1009);
  } finally {
    await server.stop();
  }
});

test('NapcatBot 反向模式未配 token：本机仍可连入', async () => {
  const bot = new NapcatBot({
    wsUrl: 'ws://127.0.0.1:1/unused',
    httpUrl: 'http://127.0.0.1:1/unused',
    selfId: 0,
    handlers: {},
    reverseWsPort: 0,
    reverseWsHost: HOST,
  });
  bot.start();
  await new Promise((r) => setTimeout(r, 50));
  const port = (bot as unknown as { wsServer: WsServer | null }).wsServer?.port;
  assert.ok(typeof port === 'number' && port > 0);
  const ws = await connect(port as number, '/');
  ws.close();
  bot.stop();
});

test('NapcatBot 反向模式：白名单外账号不许接管连接', async () => {
  const bot = new NapcatBot({
    wsUrl: 'ws://127.0.0.1:1/unused',
    httpUrl: 'http://127.0.0.1:1/unused',
    selfId: 1059681169,
    allowedSelfIds: [1059681169],
    accessToken: 't0k',
    handlers: {},
    reverseWsPort: 0,
    reverseWsHost: HOST,
  });
  bot.start();
  await new Promise((r) => setTimeout(r, 50));
  const port = (bot as unknown as { wsServer: WsServer | null }).wsServer?.port as number;

  // 连接到 bot 的假 NapCat：自动回 get_login_info（带上自己的账号）和其余动作。
  const open = (identity: number): Promise<{ ws: WebSocket; closed: Promise<number> }> =>
    new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://${HOST}:${port}/?access_token=t0k`);
      const closed = new Promise<number>((res) => {
        ws.addEventListener('close', (ev) => res(ev.code), { once: true });
      });
      ws.addEventListener('message', (ev) => {
        const req = JSON.parse(String(ev.data)) as { action?: string; echo?: string };
        if (typeof req.echo !== 'string') return;
        const data = req.action === 'get_login_info' ? { user_id: identity, nickname: `号${identity}` } : { message_id: 1 };
        ws.send(JSON.stringify({ status: 'ok', retcode: 0, echo: req.echo, data }));
      });
      ws.onopen = () => resolve({ ws, closed });
      ws.addEventListener('error', () => reject(new Error('connect failed')));
    });

  const good = await open(1059681169);
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(bot.selfId, 1059681169);

  // 换号前旧账号的 NapCat 若还在跑，自报白名单外账号：直接拒绝，
  // 现役连接和 selfId 都不能变。
  const retired = await open(3592217365);
  assert.equal(await retired.closed, 1000);
  assert.equal(bot.selfId, 1059681169);
  assert.equal(bot.connected, true);

  // 白名单外账号抢先连（无现役连接时）：先接管再补学身份，发现不在名单
  // 也必须立刻断开、不学它的号。
  good.ws.close();
  await good.closed;
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(bot.connected, false);
  const stranger = await open(2440000000);
  await stranger.closed;
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(bot.selfId, 1059681169);
  assert.equal(bot.connected, false);

  // 现役账号重连（旧连接可能半死）仍正常接管。
  const again = await open(1059681169);
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(bot.selfId, 1059681169);
  assert.equal(bot.connected, true);

  bot.stop();
  again.ws.close();
});
