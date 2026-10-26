import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BridgeWatchdog } from '../src/bot/bridge-watchdog.js';

const zombie = () => new Error('NapCat send_private_msg failed: failed/200 BRIDGE_NOT_READY');

test('连续 BRIDGE_NOT_READY 达到阈值才触发重启', () => {
  const calls: number[] = [];
  let t = 1_000_000;
  const wd = new BridgeWatchdog({ onRestartNeeded: (n) => calls.push(n), now: () => t });
  assert.equal(wd.recordFailure(zombie()), false);
  assert.equal(wd.recordFailure(zombie()), false);
  assert.equal(wd.recordFailure(zombie()), true);
  assert.deepEqual(calls, [3]);
});

test('成功一次即清零计数', () => {
  const calls: number[] = [];
  const wd = new BridgeWatchdog({ onRestartNeeded: (n) => calls.push(n), now: () => 0 });
  wd.recordFailure(zombie());
  wd.recordFailure(zombie());
  wd.recordSuccess();
  assert.equal(wd.consecutiveFailures, 0);
  wd.recordFailure(zombie());
  wd.recordFailure(zombie());
  assert.equal(wd.recordFailure(zombie()), true);
  assert.deepEqual(calls, [3]);
});

test('冷却期内不重复触发', () => {
  const calls: number[] = [];
  let t = 0;
  const wd = new BridgeWatchdog({ onRestartNeeded: (n) => calls.push(n), now: () => t });
  for (let i = 0; i < 3; i++) wd.recordFailure(zombie());
  assert.equal(calls.length, 1);
  // 冷却 180s 内继续失败：不触发
  t = 60_000;
  for (let i = 0; i < 5; i++) assert.equal(wd.recordFailure(zombie()), false);
  assert.equal(calls.length, 1);
  // 冷却过后再次达到条件：触发（计数一直在涨，阈值早已过）
  t = 200_000;
  assert.equal(wd.recordFailure(zombie()), true);
  assert.equal(calls.length, 2);
});

test('非 BRIDGE_NOT_READY 错误不计数', () => {
  const calls: number[] = [];
  const wd = new BridgeWatchdog({ onRestartNeeded: (n) => calls.push(n), now: () => 0 });
  assert.equal(wd.recordFailure(new Error('fetch failed')), false);
  assert.equal(wd.recordFailure(new Error('NapCat send_group_msg failed: failed/200 别的错')), false);
  assert.equal(wd.recordFailure('string error'), false);
  assert.equal(wd.consecutiveFailures, 0);
  assert.equal(calls.length, 0);
});

test('tryRestart 走同一冷却门（主动探针路径）', () => {
  const calls: number[] = [];
  let t = 0;
  const wd = new BridgeWatchdog({ onRestartNeeded: (n) => calls.push(n), now: () => t });
  assert.equal(wd.tryRestart(), true);
  assert.equal(wd.tryRestart(), false); // 冷却内
  t = 200_000;
  assert.equal(wd.tryRestart(), true);
  assert.equal(calls.length, 2);
});
