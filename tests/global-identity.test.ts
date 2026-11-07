/** 全局共同体记忆：订阅制动态频道 + 静态行 + 事件留痕。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  addIdentityLine,
  configureGlobalIdentity,
  globalIdentityCard,
  identityChannelList,
  identityEvents,
  recordIdentityEvent,
  registerIdentityChannel,
  removeIdentityLine,
  setSubscription,
  subscriptionsFor,
} from '../src/store/global-identity.js';

test('global-identity: 未订阅不注入、订阅后注入、退订后消失', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'global-identity-'));
  try {
    configureGlobalIdentity(join(dir, 'global_identity.json'));
    registerIdentityChannel({ name: 'mc2d', description: 'MC 分身在场动态', status: () => 'MC 分身在线' });

    // 未订阅：动态频道完全不进上下文（绘画 AI 不用惦记 MC）
    assert.equal(await globalIdentityCard('group:1'), '');

    assert.ok((await setSubscription('group:1', 'mc2d', true)).includes('已订阅'));
    assert.ok((await globalIdentityCard('group:1')).includes('MC 分身在线'));
    // 别的会话不受影响
    assert.equal(await globalIdentityCard('private:2'), '');
    assert.deepEqual(await subscriptionsFor('group:1'), ['mc2d']);

    assert.ok((await setSubscription('group:1', 'mc2d', false)).includes('已退订'));
    assert.equal(await globalIdentityCard('group:1'), '');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('global-identity: 静态行人人可见，事件留痕可查', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'global-identity-'));
  try {
    configureGlobalIdentity(join(dir, 'global_identity.json'));
    registerIdentityChannel({ name: 'mc2d', description: 'MC 分身在场动态', status: () => 'MC 分身离线' });
    const line = await addIdentityLine('我有一个 B 站分身，负责投稿与数据', 'test');
    assert.ok((await globalIdentityCard('group:9')).includes('B 站分身'), '静态行不分订阅');
    assert.ok(await removeIdentityLine(line.id));

    recordIdentityEvent('mc2d', 'MC 分身进服上线');
    recordIdentityEvent('mc2d', '');
    recordIdentityEvent('bili', '投稿审核通过');
    const events = identityEvents('mc2d');
    assert.equal(events.length, 1);
    assert.equal(events[0]!.text, 'MC 分身进服上线');
    assert.equal(identityEvents().length, 2, '不带 channel 查全部');
    const list = identityChannelList();
    assert.equal(list[0]!.name, 'mc2d');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('global-identity: 未知频道拒绝、空内容拒绝', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'global-identity-'));
  try {
    configureGlobalIdentity(join(dir, 'global_identity.json'));
    assert.ok((await setSubscription('group:1', 'nope', true)).includes('没有 nope 频道'));
    await assert.rejects(() => addIdentityLine('   ', 'test'), /内容为空/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
