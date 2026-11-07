/** QQ composite tools: schema exposure, role gates, request flow, raw safety. */
import test from 'node:test';
import assert from 'node:assert/strict';

import { chatToolSchemas, executeChatTool, type ToolContext } from '../src/chat/tools.js';
import { formatQqRequestEvent } from '../src/chat/orchestrator.js';
import type { NapcatBot } from '../src/bot/napcat.js';

interface ApiCall { action: string; params: Record<string, unknown>; timeout?: number }

function harness(options: {
  scopeType?: string;
  scopeId?: string;
  isMaster?: boolean;
  botRole?: 'owner' | 'admin' | 'member';
  requests?: Record<string, unknown>[];
} = {}) {
  const calls: ApiCall[] = [];
  const consumed: string[] = [];
  const requests = options.requests ?? [];
  const bot = {
    selfId: 10001,
    async callApi(action: string, params: Record<string, unknown>, timeout?: number) {
      calls.push({ action, params, timeout });
      if (action === 'send_qzone_msg') return { tid: 'tid-abc' };
      if (action === 'get_friend_list') return [{ user_id: 20002, nickname: '好友甲', remark: '甲' }];
      if (action === 'get_group_list') return [{ group_id: 30003, group_name: '测试群' }];
      if (action === 'get_group_info') return { group_id: params.group_id, group_name: '测试群' };
      return {};
    },
    async getGroupMemberList() {
      return [
        { user_id: 111, role: 'owner', nickname: '群主' },
        { user_id: 222, role: 'admin', nickname: '管理' },
        { user_id: 10001, role: options.botRole ?? 'admin', nickname: '机器人' },
      ];
    },
    cachedRequests(type: 'friend' | 'group') {
      return requests.filter((event) => event.request_type === type);
    },
    consumeRequest(key: string) {
      consumed.push(key);
      return undefined;
    },
  } as unknown as NapcatBot;
  const roleInfo = {
    owner: 111,
    admins: [222],
    botRole: options.botRole ?? 'admin',
    names: { 111: '群主', 222: '管理' },
    fetchedAt: Date.now(),
  } as const;
  const context = {
    bot,
    masterQq: 999000,
    isMaster: options.isMaster ?? false,
    scopeType: options.scopeType ?? 'group',
    scopeId: options.scopeId ?? '30003',
    executedTools: [],
    getGroupRoles: async () => ({ ...roleInfo, admins: [...roleInfo.admins], names: { ...roleInfo.names } }),
  } as unknown as ToolContext;
  return { context, calls, consumed };
}

test('QQ 工具面：群子AI只有当前群/互动，Onebot 与主AI有账号、空间和 raw，MC 中枢没有', () => {
  const group = chatToolSchemas('group:30003', false).map((schema) => schema.name);
  assert.ok(group.includes('group_manage') && group.includes('qq_interact'));
  assert.ok(!group.includes('qq_account') && !group.includes('napcat_call'));
  const onebot = chatToolSchemas('domain:onebot', false).map((schema) => schema.name);
  for (const name of ['qq_account', 'qzone', 'napcat_call', 'group_manage']) assert.ok(onebot.includes(name), name);
  const mc = chatToolSchemas('domain:mc', false).map((schema) => schema.name);
  assert.ok(!mc.includes('qq_account') && !mc.includes('napcat_call'));
  const master = chatToolSchemas('private:999000', true).map((schema) => schema.name);
  assert.ok(master.includes('qq_account') && master.includes('qzone') && master.includes('napcat_call'));
});

test('group_manage：批量踢人、权限矩阵与号主保护', async () => {
  const admin = harness();
  const kicked = await executeChatTool({ name: 'group_manage', arguments: { action: 'kick', qq: '444,555', reject_add_request: true } }, admin.context);
  assert.ok(kicked.ok, kicked.output);
  assert.deepEqual(admin.calls[0], {
    action: 'set_group_kick_members',
    params: { group_id: 30003, user_id: ['444', '555'], reject_add_request: true },
    timeout: undefined,
  });
  for (const qq of ['111', '999000', '10001']) {
    const denied = await executeChatTool({ name: 'group_manage', arguments: { action: 'kick', qq } }, admin.context);
    assert.equal(denied.ok, false, qq);
  }
  assert.equal(admin.calls.length, 1, '拒绝操作不能到 NapCat');

  const cross = await executeChatTool({ name: 'group_manage', arguments: { action: 'whole_mute', group_id: '888', enable: true } }, admin.context);
  assert.equal(cross.ok, false);
  const member = harness({ botRole: 'member' });
  const muted = await executeChatTool({ name: 'group_manage', arguments: { action: 'whole_mute', enable: true } }, member.context);
  assert.equal(muted.ok, false);
  assert.equal(member.calls.length, 0);
});

test('group_manage：群主设管理、公告/改名/头衔与自助名片', async () => {
  const owner = harness({ botRole: 'owner' });
  const admin = await executeChatTool({ name: 'group_manage', arguments: { action: 'set_admin', qq: '444', enable: true } }, owner.context);
  assert.ok(admin.ok, admin.output);
  assert.deepEqual(owner.calls[0]?.params, { group_id: 30003, user_id: 444, enable: true });
  await executeChatTool({ name: 'group_manage', arguments: { action: 'notice', content: '今晚活动', pinned: true } }, owner.context);
  assert.equal(owner.calls[1]?.action, '_send_group_notice');
  assert.equal(owner.calls[1]?.params.pinned, 1);
  await executeChatTool({ name: 'group_manage', arguments: { action: 'rename', name: '新群名' } }, owner.context);
  assert.equal(owner.calls[2]?.action, 'set_group_name');
  await executeChatTool({ name: 'group_manage', arguments: { action: 'set_title', qq: '444', title: '大佬' } }, owner.context);
  assert.equal(owner.calls[3]?.action, 'set_group_special_title');

  const member = harness({ botRole: 'member' });
  const card = await executeChatTool({ name: 'group_manage', arguments: { action: 'set_card', qq: '10001', card: '小助手' } }, member.context);
  assert.ok(card.ok, card.output);
  assert.equal(member.calls[0]?.action, 'set_group_card');
});

test('qq_interact：群戳一戳与资料卡点赞', async () => {
  const h = harness();
  const poke = await executeChatTool({ name: 'qq_interact', arguments: { action: 'poke', qq: '444' } }, h.context);
  assert.ok(poke.ok, poke.output);
  assert.deepEqual(h.calls[0]?.params, { user_id: 444, target_id: 444, group_id: 30003 });
  await executeChatTool({ name: 'qq_interact', arguments: { action: 'like', qq: '444', times: 99 } }, h.context);
  assert.deepEqual(h.calls[1]?.params, { user_id: 444, times: 10 });
});

test('qq_account：好友/入群申请批复后消费缓存，邀请入群保留 sub_type', async () => {
  const requests = [
    { request_type: 'friend', user_id: 20002, flag: 'friend-flag', comment: '我是甲' },
    { request_type: 'group', sub_type: 'invite', user_id: 20002, group_id: 40004, flag: 'group-flag', comment: '来玩' },
  ];
  const h = harness({ scopeType: 'domain', scopeId: 'onebot', requests });
  const list = await executeChatTool({ name: 'qq_account', arguments: { action: 'friend_requests' } }, h.context);
  assert.ok(list.ok && list.output.includes('friend-flag'));
  const approved = await executeChatTool({ name: 'qq_account', arguments: { action: 'approve_friend', flag: 'friend-flag', remark: '甲' } }, h.context);
  assert.ok(approved.ok, approved.output);
  assert.deepEqual(h.calls[0]?.params, { flag: 'friend-flag', approve: true, remark: '甲' });
  assert.deepEqual(h.consumed, ['friend:20002:friend-flag']);
  await executeChatTool({ name: 'qq_account', arguments: { action: 'approve_group', flag: 'group-flag' } }, h.context);
  assert.deepEqual(h.calls[1]?.params, { flag: 'group-flag', sub_type: 'invite', approve: true });
  const missing = await executeChatTool({ name: 'qq_account', arguments: { action: 'approve_friend', flag: 'invented' } }, h.context);
  assert.equal(missing.ok, false, '不允许猜 flag');

  const child = harness({ requests });
  const denied = await executeChatTool({ name: 'qq_account', arguments: { action: 'friend_requests' } }, child.context);
  assert.equal(denied.ok, false);
});

test('qq_account：退群绝不带解散，删号主被硬拒，通讯录可读', async () => {
  const h = harness({ scopeType: 'private', scopeId: '999000', isMaster: true });
  const leave = await executeChatTool({ name: 'qq_account', arguments: { action: 'leave_group', group_id: '30003' } }, h.context);
  assert.ok(leave.ok, leave.output);
  assert.deepEqual(h.calls[0]?.params, { group_id: 30003, is_dismiss: false });
  const denied = await executeChatTool({ name: 'qq_account', arguments: { action: 'delete_friend', qq: '999000' } }, h.context);
  assert.equal(denied.ok, false);
  const friends = await executeChatTool({ name: 'qq_account', arguments: { action: 'list_friends' } }, h.context);
  assert.ok(friends.output.includes('20002') && friends.output.includes('甲'));
  const groups = await executeChatTool({ name: 'qq_account', arguments: { action: 'list_groups' } }, h.context);
  assert.ok(groups.output.includes('30003') && groups.output.includes('测试群'));
});

test('qzone：发布带权限与图片、返回 tid，非法本地图片与权限组合被拦', async () => {
  const h = harness({ scopeType: 'domain', scopeId: 'onebot' });
  const published = await executeChatTool({
    name: 'qzone',
    arguments: { action: 'publish', content: '今天也不错', images: ['https://example.com/a.jpg'], ugc_right: 16, target_uins: ['20002'] },
  }, h.context);
  assert.ok(published.ok && published.output.includes('tid-abc'), published.output);
  assert.deepEqual(h.calls[0]?.params, {
    content: '今天也不错', images: ['https://example.com/a.jpg'], ugc_right: 16, target_uins: [20002],
  });
  const local = await executeChatTool({ name: 'qzone', arguments: { action: 'publish', content: 'x', images: ['/root/secret'] } }, h.context);
  assert.equal(local.ok, false);
  const badVisibility = await executeChatTool({ name: 'qzone', arguments: { action: 'publish', content: 'x', ugc_right: 16 } }, h.context);
  assert.equal(badVisibility.ok, false);
  await executeChatTool({ name: 'qzone', arguments: { action: 'delete', tid: 'tid-abc' } }, h.context);
  assert.equal(h.calls.at(-1)?.action, 'delete_qzone_msg');
});

test('napcat_call：help 文档、JSON 直调与本地文件/底层包/解散群防护', async () => {
  const h = harness({ scopeType: 'private', scopeId: '999000', isMaster: true });
  const help = await executeChatTool({ name: 'napcat_call', arguments: { action: 'help' } }, h.context);
  assert.ok(help.ok && help.output.includes('get_group_member_info') && help.output.includes('get_qzone_public_feeds'));
  const raw = await executeChatTool({ name: 'napcat_call', arguments: { action: 'get_group_info', params: { group_id: 30003, no_cache: true } } }, h.context);
  assert.ok(raw.ok && raw.output.includes('测试群'), raw.output);
  assert.deepEqual(h.calls[0]?.params, { group_id: 30003, no_cache: true });
  for (const args of [
    { action: 'send_packet', params: { cmd: 'x' } },
    { action: 'upload_group_file', params: { group_id: 1, file: '/root/.ssh/id_rsa' } },
    { action: 'send_qzone_msg', params: { content: 'x', images: ['/root/private.jpg'] } },
    { action: 'set_group_leave', params: { group_id: 1, is_dismiss: true } },
    { action: 'delete_friend', params: { friend_id: 999000 } },
    // CQ 码绕过：危险字符串藏在 message/text 这类普通键里也要拦
    { action: 'send_private_msg', params: { user_id: 999000, message: '[CQ:image,file=file:///etc/passwd]' } },
    { action: 'send_group_msg', params: { group_id: 30003, message: '[CQ:record,file=/root/.ssh/id_rsa]' } },
    { action: 'send_msg', params: { message: [{ type: 'image', data: { file: 'file:///etc/shadow' } }] } },
    { action: 'send_group_msg', params: { group_id: 30003, message: '看这个 [CQ:image,file=C:\\Windows\\win.ini]' } },
  ]) {
    const denied = await executeChatTool({ name: 'napcat_call', arguments: args }, h.context);
    assert.equal(denied.ok, false, JSON.stringify(args));
  }
  // 合法的 CQ 码（http/文件ID）不能被误伤
  const legit = await executeChatTool({
    name: 'napcat_call',
    arguments: { action: 'send_group_msg', params: { group_id: 30003, message: '[CQ:image,file=https://x.example/a.png]' } },
  }, h.context);
  assert.ok(legit.ok, legit.output);
  assert.equal(h.calls.length, 2, '被拦请求不能到 NapCat');
});

test('QQ 申请事件文案带真实 flag，好友与邀请入群语义不混', () => {
  const friend = formatQqRequestEvent({ request_type: 'friend', user_id: 20002, flag: 'f1', comment: '你好' });
  assert.ok(friend.includes('好友申请') && friend.includes('flag=f1'));
  const invite = formatQqRequestEvent({ request_type: 'group', sub_type: 'invite', user_id: 20002, group_id: 30003, flag: 'g1' });
  assert.ok(invite.includes('入群邀请') && invite.includes('同意即加群') && invite.includes('flag=g1'));
});
