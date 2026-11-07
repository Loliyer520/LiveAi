/** 真实 MC 在场情报：边车 /status 文本 → 低频摘要。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPresenceSnapshot } from '../src/chat/mc-presence.js';

test('mc-presence: 26.2 Rust 后端在线文本只保留服务器与玩家名单', () => {
  const message = [
    '在线：BingTang @ 127.0.0.1:25565（26.2 / 协议 776，Rust 后端，离线）',
    '位置：x=0.5 y=86.0 z=8.5',
    '生命 20/20｜饥饿 20/20｜等级 0',
    '玩家列表（2）：BingTang, MC3DPen',
    '手持：空手｜背包 0 格 / 0 件',
  ].join('\n');
  const snap = buildPresenceSnapshot(message);
  assert.equal(
    snap.text,
    '真实 MC 服务器：BingTang @ 127.0.0.1:25565（26.2 / 协议 776，Rust 后端，离线）\n在线玩家：BingTang, MC3DPen',
  );
  assert.match(snap.key, /^online\|/);
  assert.equal(snap.dead, false);
  assert.ok(!snap.text.includes('位置'), '高频坐标不能进低频缓存');
});

test('mc-presence: 生命 0/20 标记死亡，供自动重生与诚实回答', () => {
  const snap = buildPresenceSnapshot([
    '在线：BingTang @ 127.0.0.1:25565（26.2 / 协议 776，Rust 后端，离线）',
    '位置：x=-3.4 y=72.0 z=4.4',
    '生命 0/20｜饥饿 20/20｜等级 0',
    '玩家列表（1）：BingTang',
  ].join('\n'));
  assert.equal(snap.dead, true);
  assert.match(snap.key, /dead=1/);
  assert.match(snap.text, /已死亡/);
  assert.ok(!snap.text.includes('位置'));
});

test('mc-presence: Mineflayer 旧版的「玩家在线」行同样识别', () => {
  const snap = buildPresenceSnapshot([
    '在线：BingTang @ play.example.com:25565（1.20.4，离线）',
    '位置：x=1 y=2 z=3｜维度 minecraft:overworld',
    '玩家在线（1）：BingTang',
  ].join('\n'));
  assert.equal(snap.text.split('\n')[1], '在线玩家：BingTang');
});

test('mc-presence: 未连接与连接中不冒充在线', () => {
  const offline = buildPresenceSnapshot([
    '未连接。用 mc_join 进服（host 必填，port 默认 25565，离线服 auth=offline）。',
    '上次错误：connection refused',
  ].join('\n'));
  assert.equal(offline.key, 'offline');
  assert.match(offline.text, /未连接/);
  assert.match(offline.text, /connection refused/);

  const connecting = buildPresenceSnapshot('正在连接 127.0.0.1:25565（BingTang）……');
  assert.equal(connecting.key, 'connecting');
  assert.match(connecting.text, /正在连接/);
});
