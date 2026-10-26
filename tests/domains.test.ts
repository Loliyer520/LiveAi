/**
 * 领域主级 AI（domain scopes）：注册表、工具可见性、委派校验、插件 scopes 白名单。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { domainDefs, domainDefForScope, domainScopeHasBuiltin, hubForScope, loadDomains } from '../src/chat/domains.js';
import { chatToolSchemas, executeChatTool, type ToolContext } from '../src/chat/tools.js';
import { PluginRegistry } from '../src/chat/plugins.js';
import type { NapcatBot } from '../src/bot/napcat.js';

test('domain 注册表：domain:mc 已注册，查询与工具判定正确', () => {
  assert.ok(domainDefs().some((def) => def.id === 'mc'));
  assert.ok(domainDefs().some((def) => def.id === 'onebot'));
  assert.ok(domainDefs().some((def) => def.id === 'bili'));
  assert.equal(domainDefForScope('domain:mc')?.title, 'Minecraft 中枢');
  assert.equal(domainDefForScope('domain:onebot')?.title, 'Onebot 中枢');
  assert.equal(domainDefForScope('group:1'), null);
  assert.equal(domainDefForScope('private:241898129'), null);
  assert.equal(domainDefForScope('domain:unknown'), null);
  assert.equal(domainScopeHasBuiltin('domain:mc', 'mc_view'), true);
  assert.equal(domainScopeHasBuiltin('domain:mc', 'relation_write'), false);
  assert.equal(domainScopeHasBuiltin('group:1', 'mc_view'), false);
});

test('汇报路由表：QQ 下级→Onebot 中枢，mcclient→MC 中枢，bili→哔哩中枢，domain/主AI 直报', () => {
  const master = 'private:241898129';
  assert.equal(hubForScope('group:10001', master), 'domain:onebot');
  assert.equal(hubForScope('private:3319085316', master), 'domain:onebot');
  assert.equal(hubForScope('mcclient:explorer', master), 'domain:mc');
  assert.equal(hubForScope('bili:BV1xxxxx', master), 'domain:bili');
  assert.equal(hubForScope('domain:mc', master), null, '领域中枢直报最高级中枢');
  assert.equal(hubForScope(master, master), null, '主AI 自己不汇报');
  assert.equal(hubForScope('unknown:x', master), null);
});

test('工具面：MC 中枢拿 CHILD 集+mc_view+domain_message，不拿主AI专属，普通子AI无 mc_view/domain_message', () => {
  const mc = chatToolSchemas('domain:mc', false).map((schema) => schema.name);
  assert.ok(mc.includes('mc_view'), 'MC 中枢缺 mc_view');
  assert.ok(mc.includes('notify_master'), 'MC 中枢应有 notify_master 上报通道');
  assert.ok(mc.includes('kb_list') && mc.includes('kb_append'), 'MC 中枢应有知识库读/报备');
  assert.ok(mc.includes('domain_message'), '领域中枢应有主级互聊工具');
  assert.ok(!mc.includes('relation_write'), '主AI专属工具不应给领域中枢');
  assert.ok(!mc.includes('delegate_to_child'), '委派是主AI的权力');
  assert.ok(!mc.includes('kb_review'), '复核权留在中枢');
  const group = chatToolSchemas('group:1', false).map((schema) => schema.name);
  assert.ok(!group.includes('mc_view'));
  assert.ok(!group.includes('domain_message'), '下级子AI不应有主级互聊工具');
  const videoSub = chatToolSchemas('bili:BV1xxxxx', false).map((schema) => schema.name);
  assert.ok(!videoSub.includes('domain_message'), '视频下级不应有主级互聊工具');
  const master = chatToolSchemas('private:241898129', true).map((schema) => schema.name);
  assert.ok(master.includes('domain_message'), '主AI 应有主级互聊工具');
});

test('delegate_to_child：主AI 可委派 domain:mc / bili / mcclient，未知领域/子AI僭越都被拒', async () => {
  const master = {
    isMaster: true,
    scopeType: 'private',
    scopeId: '241898129',
    delegateToChild: (type: string, id: string) => `delegate_${type}_${id}`,
    executedTools: [],
  } as unknown as ToolContext;
  const okResult = await executeChatTool(
    { name: 'delegate_to_child', arguments: { target_scope_type: 'domain', target_scope_id: 'mc', instruction: '进服看看情况' } },
    master,
  );
  assert.equal(okResult.ok, true);
  assert.match(okResult.output, /domain:mc/);

  // bili 视频下级 / mcclient 下级：id 即建即用（bvid / client 名），无需注册表
  const biliSub = await executeChatTool(
    { name: 'delegate_to_child', arguments: { target_scope_type: 'bili', target_scope_id: 'BV1xxxxx', instruction: '细看这个视频' } },
    master,
  );
  assert.equal(biliSub.ok, true);
  const mcSub = await executeChatTool(
    { name: 'delegate_to_child', arguments: { target_scope_type: 'mcclient', target_scope_id: 'explorer', instruction: '往北探路' } },
    master,
  );
  assert.equal(mcSub.ok, true);

  const unknown = await executeChatTool(
    { name: 'delegate_to_child', arguments: { target_scope_type: 'domain', target_scope_id: 'quake3', instruction: 'x' } },
    master,
  );
  assert.equal(unknown.ok, false);
  assert.match(unknown.output, /没有注册/);

  const child = {
    isMaster: false,
    scopeType: 'group',
    scopeId: '1',
    executedTools: [],
  } as unknown as ToolContext;
  const hijack = await executeChatTool(
    { name: 'delegate_to_child', arguments: { target_scope_type: 'domain', target_scope_id: 'mc', instruction: 'x' } },
    child,
  );
  assert.equal(hijack.ok, false);
});

test('domain_message 主级互聊：主AI/领域中枢可用，注入目标领域；下级与未知领域被拒', async () => {
  const injected: { scope: string; text: string }[] = [];
  let notified = '';
  const mk = (scopeType: string, scopeId: string, isMaster: boolean) => ({
    isMaster,
    scopeType,
    scopeId,
    peerInject: (scope: string, text: string) => injected.push({ scope, text }),
    notifyMaster: (payload: { text: string }) => { notified = payload.text; },
    executedTools: [],
  }) as unknown as ToolContext;

  // 主AI → MC 中枢
  const fromMaster = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'mc', content: '主人在线，可以开始干活', request_type: 'coord' } },
    mk('private', '241898129', true),
  );
  assert.equal(fromMaster.ok, true);
  assert.equal(injected.length, 1);
  assert.equal(injected[0].scope, 'domain:mc');
  assert.match(injected[0].text, /\[主级互聊 from private:241898129 type=coord\]/);
  assert.match(injected[0].text, /可以开始干活/);

  // 领域中枢 → 领域中枢（横向通道）
  const fromHub = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'onebot', content: '帮我约小源周末上线' } },
    mk('domain', 'mc', false),
  );
  assert.equal(fromHub.ok, true);
  assert.equal(injected[1].scope, 'domain:onebot');
  assert.match(injected[1].text, /\[主级互聊 from domain:mc\]/);

  // target=master → 走中枢直发通道
  const toMaster = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'master', content: '急需批复' } },
    mk('domain', 'bili', false),
  );
  assert.equal(toMaster.ok, true);
  assert.equal(notified, '急需批复');

  // 下级子AI 僭越被拒
  const childDenied = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'mc', content: 'x' } },
    mk('group', '1', false),
  );
  assert.equal(childDenied.ok, false);
  assert.match(childDenied.output, /仅限主AI与领域中枢/);

  // 未知领域被拒 / 自己发自己被拒
  const unknown = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'quake3', content: 'x' } },
    mk('private', '241898129', true),
  );
  assert.equal(unknown.ok, false);
  assert.match(unknown.output, /没有注册/);
  const self = await executeChatTool(
    { name: 'domain_message', arguments: { target: 'mc', content: 'x' } },
    mk('domain', 'mc', false),
  );
  assert.equal(self.ok, false);
});

test('mc_view 执行层硬拦截：只有 domain:mc 能看，主AI/群会话被拒并引导委派', async () => {
  const mk = (scopeType: string, scopeId: string, isMaster: boolean) => ({
    isMaster,
    scopeType,
    scopeId,
    describeImage: async () => '',
    executedTools: [],
  }) as unknown as ToolContext;
  const denied = await executeChatTool({ name: 'mc_view', arguments: {} }, mk('private', '241898129', true));
  assert.equal(denied.ok, false);
  assert.match(denied.output, /delegate_to_child/);
  const groupDenied = await executeChatTool({ name: 'mc_view', arguments: {} }, mk('group', '1', false));
  assert.equal(groupDenied.ok, false);
});

const SCOPED_PLUGIN = JSON.stringify({
  name: 'mcside',
  description: 'MC 边车',
  tools: [
    {
      name: 'mc_status',
      description: '状态',
      scopes: ['domain:mc'],
      allow_internal: true,
      parameters: { type: 'object', properties: {} },
      request: { url: 'http://127.0.0.1:7660/status' },
    },
  ],
});

test('插件 scopes 白名单：mc_* 只挂 domain:mc，主AI/群会话不可见且执行被硬拒', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-domain-plugins-'));
  try {
    const pluginDir = join(dir, 'mcside');
    await mkdir(pluginDir, { recursive: true });
    await writeFile(join(pluginDir, 'plugin.json'), SCOPED_PLUGIN, 'utf-8');
    const registry = new PluginRegistry(dir);
    registry.load();
    const reserved = new Set<string>();
    const mcNames = registry.toolSchemas('domain:mc', false, reserved).map((schema) => schema.name);
    const masterNames = registry.toolSchemas('private:241898129', true, reserved).map((schema) => schema.name);
    const groupNames = registry.toolSchemas('group:1', false, reserved).map((schema) => schema.name);
    assert.deepEqual(mcNames, ['mc_status']);
    assert.ok(!masterNames.includes('mc_status'), '主AI 收敛后不再持有 mc 工具');
    assert.ok(!groupNames.includes('mc_status'));

    // 可见性之外还有执行层硬检查：模型在别的会话猜出工具名也执行不了。
    const foreignContext = {
      isMaster: true,
      scopeType: 'private',
      scopeId: '241898129',
      networkBudget: { used: 0 },
      executedTools: [],
    } as unknown as ToolContext;
    const blocked = await registry.execute('mc_status', {}, foreignContext);
    assert.ok(blocked !== null, '应拦截而不是落入未知工具');
    assert.equal(blocked!.ok, false);
    assert.match(blocked!.output, /未挂载/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

const BARE_TYPE_PLUGIN = JSON.stringify({
  name: 'biliside',
  description: 'B 站域',
  tools: [
    {
      name: 'bili_search',
      description: '搜索',
      scopes: ['domain:bili', 'bili'],
      parameters: { type: 'object', properties: {} },
      request: { url: 'http://127.0.0.1:9/nope' },
    },
  ],
});

test('插件 scopes 裸类型名：挂 "bili" = 哔哩中枢+全部视频下级可见，其余会话不可见且硬拒', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-baretype-plugins-'));
  try {
    const pluginDir = join(dir, 'biliside');
    await mkdir(pluginDir, { recursive: true });
    await writeFile(join(pluginDir, 'plugin.json'), BARE_TYPE_PLUGIN, 'utf-8');
    const registry = new PluginRegistry(dir);
    registry.load();
    const reserved = new Set<string>();
    assert.deepEqual(registry.toolSchemas('domain:bili', false, reserved).map((schema) => schema.name), ['bili_search']);
    assert.deepEqual(registry.toolSchemas('bili:BV1xxxxx', false, reserved).map((schema) => schema.name), ['bili_search']);
    assert.equal(registry.toolSchemas('group:1', false, reserved).length, 0);
    assert.equal(registry.toolSchemas('mcclient:x', false, reserved).length, 0);
    assert.equal(registry.toolSchemas('private:241898129', true, reserved).length, 0, '主AI 收敛后不再持有 bili 工具');

    // 执行层硬检查：视频下级能执行，QQ 会话猜出工具名也执行不了。
    const videoContext = {
      isMaster: false,
      scopeType: 'bili',
      scopeId: 'BV1xxxxx',
      networkBudget: { used: 0 },
      executedTools: [],
    } as unknown as ToolContext;
    const allowed = await registry.execute('bili_search', {}, videoContext);
    assert.ok(allowed !== null, '应路由到插件而不是落入未知工具');
    const groupContext = {
      isMaster: false,
      scopeType: 'group',
      scopeId: '1',
      networkBudget: { used: 0 },
      executedTools: [],
    } as unknown as ToolContext;
    const blocked = await registry.execute('bili_search', {}, groupContext);
    assert.ok(blocked !== null);
    assert.equal(blocked!.ok, false);
    assert.match(blocked!.output, /未挂载/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
