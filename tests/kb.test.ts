/**
 * 知识库单元测试：条目 CRUD、挂载 ACL（read/append/write 分级与取最高）、
 * 下级追加待审核队列、越权硬拦截、持久化重载。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { KnowledgeBase } from '../src/knowledge/kb.js';
import { executeChatTool, type ToolContext } from '../src/chat/tools.js';

const MASTER = 'private:241898129';

function makeKb(): { kb: KnowledgeBase; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), 'kb-test-'));
  return { kb: new KnowledgeBase(dir, MASTER), dir };
}

/** 只填 kb_* 工具真正读到的字段，其余 as 断言补齐。 */
function kbContext(kb: KnowledgeBase, scopeType: string, scopeId: string, isMaster: boolean): ToolContext {
  return {
    kb,
    isMaster,
    scopeType,
    scopeId,
    executedTools: [],
  } as unknown as ToolContext;
}

test('工具层硬拦截：下级 kb_append 走待审、read-only 被拒、主AI直录', async () => {
  const { kb, dir } = makeKb();
  const master = kbContext(kb, 'private', '241898129', true);
  const childRead = kbContext(kb, 'group', '1', false);
  const childAppend = kbContext(kb, 'group', '7', false);

  // 主AI直录立即生效
  const direct = await executeChatTool(
    { name: 'kb_append', arguments: { subject: '直录', content: '主AI写的', tags: ['mc'] } },
    master,
  );
  assert.equal(direct.ok, true);
  assert.match(direct.output, /主AI直录/);
  assert.equal(kb.list({}).length, 1);

  // 挂载：group:1 只读，group:7 可追加
  await executeChatTool(
    { name: 'kb_mount', arguments: { target: 'group:1', permission: 'read', tags: ['mc'] } },
    master,
  );
  await executeChatTool(
    { name: 'kb_mount', arguments: { target: 'group:7', permission: 'append', tags: ['mc'] } },
    master,
  );

  // 下级可见性
  const list1 = await executeChatTool({ name: 'kb_list', arguments: {} }, childRead);
  assert.match(list1.output, /直录/);
  const list9 = await executeChatTool({ name: 'kb_list', arguments: {} }, kbContext(kb, 'group', '9', false));
  assert.match(list9.output, /没有挂载/);

  // read-only 追加被硬拒（工具层，不靠提示词）
  const blocked = await executeChatTool(
    { name: 'kb_append', arguments: { subject: '偷渡', content: '不该进', tags: ['mc'] } },
    childRead,
  );
  assert.equal(blocked.ok, false);
  assert.match(blocked.output, /追加权限/);
  assert.equal(kb.pendingFor(MASTER).length, 0);

  // 追加权限的下级 → 进待审队列，未复核前不可见
  const proposed = await executeChatTool(
    { name: 'kb_append', arguments: { subject: '报备', content: '下级发现', tags: ['mc'] } },
    childAppend,
  );
  assert.equal(proposed.ok, true);
  assert.match(proposed.output, /待审项/);
  assert.equal(kb.pendingFor(MASTER).length, 1);
  assert.equal(kb.visibleFor('group:7').length, 1);

  // 下级不能复核/挂载/删改
  for (const name of ['kb_review', 'kb_mount', 'kb_delete', 'kb_edit', 'kb_pending']) {
    const result = await executeChatTool({ name, arguments: { id: 'x', action: 'approve', target: 'a', permission: 'read' } }, childAppend);
    assert.equal(result.ok, false, `${name} 应被主AI守卫拦截`);
  }

  // 主AI复核通过 → 挂载方立即可见
  const pendingId = kb.pendingFor(MASTER)[0].id;
  const reviewed = await executeChatTool(
    { name: 'kb_review', arguments: { id: pendingId, action: 'approve' } },
    master,
  );
  assert.equal(reviewed.ok, true);
  assert.equal(kb.visibleFor('group:7').length, 2);
  rmSync(dir, { recursive: true, force: true });
});

test('add/list/get/revise/remove 条目生命周期', async () => {
  const { kb, dir } = makeKb();
  const entry = kb.add({ subject: '主人常玩的 MC 服', content: '地址 mc.example.com', tags: ['mc', 'server'], source: 'test' });
  assert.ok(entry.id);
  assert.equal(entry.confidence, 0.8);

  const listed = kb.list({ tags: ['mc'] });
  assert.equal(listed.length, 1);
  assert.equal(listed[0].subject, '主人常玩的 MC 服');

  const got = kb.get(entry.id);
  assert.ok(got);
  assert.equal(got.content, '地址 mc.example.com');

  // revise 保留 id（supersede 语义：挂载继续生效）
  const revised = kb.revise(entry.id, { content: '地址 mc.example.com:25565', source: 'test2' });
  assert.equal(revised.id, entry.id);
  assert.equal(revised.content, '地址 mc.example.com:25565');
  assert.equal(revised.subject, '主人常玩的 MC 服');
  assert.ok(revised.updatedAt >= entry.updatedAt);

  assert.equal(kb.remove(entry.id), true);
  assert.equal(kb.remove(entry.id), false);
  assert.equal(kb.get(entry.id), null);
  await kb.flush();
  rmSync(dir, { recursive: true, force: true });
});

test('cleanTags 规整：小写、去空格、去重、截断', () => {
  const { kb, dir } = makeKb();
  const entry = kb.add({ subject: 's', content: 'c', tags: [' MC ', 'MC', 'Server Info', ''], source: 't' });
  assert.deepEqual(entry.tags, ['mc', 'server-info']);
  rmSync(dir, { recursive: true, force: true });
});

test('挂载：visibleFor/permissionFor 按标签范围与最高权限', () => {
  const { kb, dir } = makeKb();
  const mcEntry = kb.add({ subject: 'MC 情报', content: '…', tags: ['mc'], source: 't' });
  const biliEntry = kb.add({ subject: 'B 站情报', content: '…', tags: ['bili'], source: 't' });

  // 无挂载 = 不可见
  assert.deepEqual(kb.visibleFor('group:1'), []);
  assert.equal(kb.permissionFor('group:1', mcEntry), null);

  // 只读挂载 mc 标签
  kb.grantMount('group:1', 'read', ['mc']);
  assert.deepEqual(kb.visibleFor('group:1').map((e) => e.id), [mcEntry.id]);
  assert.equal(kb.permissionFor('group:1', mcEntry), 'read');
  assert.equal(kb.permissionFor('group:1', biliEntry), null);

  // 再叠加 write 挂载（无标签范围=全部）→ 最高权限 write 胜出
  kb.grantMount('group:1', 'write');
  assert.equal(kb.permissionFor('group:1', mcEntry), 'write');
  assert.equal(kb.permissionFor('group:1', biliEntry), 'write');
  assert.equal(kb.visibleFor('group:1').length, 2);

  // all 通配挂载
  kb.grantMount('all', 'read');
  assert.equal(kb.permissionFor('private:999', biliEntry), 'read');

  // revoke 带标签范围只撤那一条
  assert.equal(kb.revokeMount('group:1', ['mc']), true);
  assert.equal(kb.permissionFor('group:1', mcEntry), 'write'); // 无范围 write 挂载仍在
  assert.equal(kb.revokeMount('group:1'), true);
  assert.equal(kb.permissionFor('group:1', mcEntry), 'read'); // 只剩 all 通配
  rmSync(dir, { recursive: true, force: true });
});

test('中枢天然全权，无需挂载', () => {
  const { kb, dir } = makeKb();
  const entry = kb.add({ subject: 's', content: 'c', tags: ['x'], source: 't' });
  assert.equal(kb.permissionFor(MASTER, entry), 'write');
  assert.equal(kb.permissionFor('master', entry), 'write');
  assert.equal(kb.visibleFor(MASTER).length, 1);
  rmSync(dir, { recursive: true, force: true });
});

test('下级追加：propose 进待审队列，主级 approve 入库 / reject 丢弃', () => {
  const { kb, dir } = makeKb();
  kb.grantMount('group:7', 'append', ['mc']);

  const pendingId = kb.propose('group:7', { subject: '新矿点', content: '出生点北边有铁矿', tags: ['mc'] });
  assert.equal(kb.pendingFor(MASTER).length, 1);
  assert.equal(kb.pendingFor(MASTER)[0].id, pendingId);
  assert.equal(kb.pendingFor(MASTER)[0].entry.source, 'proposed:group:7');

  // 待审未过：普通 scope 看不到，中枢可见（入库前条目不存在于 list）
  assert.equal(kb.visibleFor('group:7').length, 0);

  // 非提出者/非中枢不能复核
  assert.throws(() => kb.review(pendingId, 'approve', 'group:8'));

  const approved = kb.review(pendingId, 'approve', MASTER);
  assert.ok(approved);
  assert.equal(approved.subject, '新矿点');
  assert.match(approved.source, /^approved:/);
  assert.equal(kb.pendingFor(MASTER).length, 0);
  assert.equal(kb.visibleFor('group:7').length, 1); // 按 mc 标签挂载立即可见

  // reject 路径
  const pid2 = kb.propose('group:7', { subject: '垃圾情报', content: 'x', tags: ['mc'] });
  assert.equal(kb.review(pid2, 'reject', MASTER), null);
  assert.equal(kb.pendingFor(MASTER).length, 0);
  rmSync(dir, { recursive: true, force: true });
});

test('越权硬拦截：read-only 不能 append，无挂载不能 propose', () => {
  const { kb, dir } = makeKb();
  kb.add({ subject: 's', content: 'c', tags: ['mc'], source: 't' });
  kb.grantMount('group:2', 'read', ['mc']);
  assert.throws(() => kb.propose('group:2', { subject: 'x', content: 'y', tags: ['mc'] }), /追加权限/);
  assert.throws(() => kb.propose('group:3', { subject: 'x', content: 'y', tags: ['mc'] }), /追加权限/);
  // append 权限但标签不在挂载范围
  kb.grantMount('group:4', 'append', ['bili']);
  assert.throws(() => kb.propose('group:4', { subject: 'x', content: 'y', tags: ['mc'] }), /追加权限/);
  rmSync(dir, { recursive: true, force: true });
});

test('持久化：新实例从磁盘重载条目与挂载', async () => {
  const { kb, dir } = makeKb();
  const entry = kb.add({ subject: '持久', content: '落盘验证', tags: ['persistence'], source: 't' });
  kb.grantMount('group:5', 'write', ['persistence']);
  await kb.flush();

  const kb2 = new KnowledgeBase(dir, MASTER);
  assert.equal(kb2.get(entry.id)?.content, '落盘验证');
  assert.equal(kb2.permissionFor('group:5', entry), 'write');

  await kb2.flush();
  rmSync(dir, { recursive: true, force: true });
});
