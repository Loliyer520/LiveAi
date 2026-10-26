/** Group roles cache + admin tools (recall others / group_mute) gating. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { GroupRolesCache, parseMemberRoles } from '../src/chat/group-roles.js';
import { executeChatTool, type ToolContext } from '../src/chat/tools.js';
import { ScopeRepository } from '../src/store/repository.js';
import type { NapcatBot } from '../src/bot/napcat.js';

const BOT_ID = 987654322;

const MEMBER_LIST = [
  { user_id: 111, role: 'owner', card: '', nickname: '群主大大' },
  { user_id: 222, role: 'admin', card: '管理小明', nickname: '小明' },
  { user_id: 333, role: 'admin', card: '', nickname: '管理小红' },
  { user_id: 444, role: 'member', card: '', nickname: '路人甲' },
  { user_id: BOT_ID, role: 'admin', card: '', nickname: '冰糖' },
];

test('parseMemberRoles classifies owner/admins/botRole, card beats nickname', () => {
  const info = parseMemberRoles(MEMBER_LIST, BOT_ID);
  assert.equal(info.owner, 111);
  assert.deepEqual(info.admins, [222, 333]);
  assert.equal(info.botRole, 'admin');
  assert.equal(info.names[111], '群主大大');
  assert.equal(info.names[222], '管理小明'); // card preferred
  assert.equal(info.names[444], undefined); // members not tracked

  const degraded = parseMemberRoles(MEMBER_LIST, 999); // bot not in list
  assert.equal(degraded.botRole, 'member');
  assert.equal(parseMemberRoles('garbage', BOT_ID).botRole, 'member');
});

test('GroupRolesCache caches within TTL and degrades to member on failure', async () => {
  let calls = 0;
  let failing = false;
  const bot = {
    selfId: BOT_ID,
    async getGroupMemberList() {
      calls += 1;
      if (failing) throw new Error('napcat down');
      return MEMBER_LIST;
    },
  } as unknown as NapcatBot;
  const cache = new GroupRolesCache(bot, 60_000);

  const first = await cache.get(555);
  const second = await cache.get(555);
  assert.equal(calls, 1, 'second read served from cache');
  assert.equal(first.botRole, 'admin');
  assert.equal(second.owner, 111);

  // fetch failure with a warm cache → stale data still served
  failing = true;
  const stale = await cache.get(555);
  assert.equal(stale.botRole, 'admin');
  // failure with a cold cache → locked-down default
  const cold = await cache.get(666);
  assert.equal(cold.botRole, 'member');
  assert.equal(cold.owner, null);

  cache.invalidate(555);
  failing = false;
  const refetched = await cache.get(555);
  assert.equal(refetched.botRole, 'admin');
  assert.equal(calls, 3);
});

// ── tool gating ─────────────────────────────────────────────────────────────

interface BanCall { group_id: number; user_id: number; duration: number }

async function makeContext(options: {
  scopeType?: 'group' | 'private';
  scopeId?: string;
  botRole?: 'owner' | 'admin' | 'member';
  withMessage?: { userId: number; messageId: number; ref: string };
}) {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-roles-'));
  const repo = new ScopeRepository(join(dir, 'state'), 500);
  const scopeType = options.scopeType ?? 'group';
  const scopeId = options.scopeId ?? '555';
  if (options.withMessage) {
    const ref = repo.registerMessageRef(scopeType, scopeId, options.withMessage.messageId);
    repo.appendMessage(scopeType, scopeId, {
      user_id: options.withMessage.userId,
      nickname: '某人',
      text: '一条消息',
      raw_message: '',
      message_id: options.withMessage.messageId,
      message_ref: ref,
      timestamp: Date.now() / 1000,
      image_refs: [],
      file_refs: [],
    });
    options.withMessage.ref = ref;
  }
  const recalled: (number | string)[] = [];
  const bans: BanCall[] = [];
  const bot = {
    selfId: BOT_ID,
    async recallMessage(id: number | string) {
      recalled.push(id);
      return {};
    },
    async setGroupBan(groupId: number, userId: number, duration: number) {
      bans.push({ group_id: groupId, user_id: userId, duration });
    },
  } as unknown as NapcatBot;
  const roleInfo = {
    owner: 111,
    admins: [222, 333],
    botRole: options.botRole ?? 'admin',
    names: { 111: '群主大大', 222: '管理小明', 333: '管理小红' },
    fetchedAt: Date.now(),
  } as const;
  const context: ToolContext = {
    bot,
    repo,
    archive: null as never,
    relations: null as never,
    tasks: null as never,
    masterQq: 123456789,
    isMaster: false,
    notifyMaster: () => undefined,
    workspace: null as never,
    networkBudget: { used: 0 },
    delegateToChild: () => '',
    sendToScope: async () => '',
    peerInject: () => undefined,
    triggerImageRefs: [],
    describeImage: async () => '',
    getGroupRoles: async () => ({ ...roleInfo, names: { ...roleInfo.names }, admins: [...roleInfo.admins] }),
    scopeType,
    scopeId,
    executedTools: [],
  };
  return { context, recalled, bans, cleanup: async () => {
    await repo.flush();
    await rm(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  } };
}

test('recall_message: member bot can still retract its OWN message', async () => {
  const own = await makeContext({ botRole: 'member', withMessage: { userId: BOT_ID, messageId: 9001, ref: '' } });
  try {
    const entries = own.context.repo.getMessages('group', '555');
    const realRef = entries[0]!.message_ref!;
    const result = await executeChatTool({ name: 'recall_message', arguments: { message_ref: realRef } }, own.context);
    assert.ok(result.ok, result.output);
    assert.deepEqual(own.recalled, [9001]);
  } finally {
    await own.cleanup();
  }
});

test('recall_message as admin can retract others; as member cannot', async () => {
  const asAdmin = await makeContext({ botRole: 'admin', withMessage: { userId: 444, messageId: 9002, ref: '' } });
  try {
    const entries = asAdmin.context.repo.getMessages('group', '555');
    const realRef = entries[0]!.message_ref!;
    const result = await executeChatTool({ name: 'recall_message', arguments: { message_ref: realRef } }, asAdmin.context);
    assert.ok(result.ok, result.output);
    assert.deepEqual(asAdmin.recalled, [9002]);
  } finally {
    await asAdmin.cleanup();
  }

  const asMember = await makeContext({ botRole: 'member', withMessage: { userId: 444, messageId: 9003, ref: '' } });
  try {
    const entries = asMember.context.repo.getMessages('group', '555');
    const realRef = entries[0]!.message_ref!;
    const result = await executeChatTool({ name: 'recall_message', arguments: { message_ref: realRef } }, asMember.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('管理员'));
    assert.equal(asMember.recalled.length, 0);
  } finally {
    await asMember.cleanup();
  }

  // private scope: recalling the peer's message is refused regardless
  const priv = await makeContext({ scopeType: 'private', botRole: 'admin', withMessage: { userId: 444, messageId: 9004, ref: '' } });
  try {
    const entries = priv.context.repo.getMessages('private', '555');
    const realRef = entries[0]!.message_ref!;
    const result = await executeChatTool({ name: 'recall_message', arguments: { message_ref: realRef } }, priv.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('私聊'));
  } finally {
    await priv.cleanup();
  }
});

test('group_mute: duration conversion, unmute, and refusal matrix', async () => {
  const admin = await makeContext({ botRole: 'admin' });
  try {
    // normal mute: minutes → seconds
    const mute = await executeChatTool({ name: 'group_mute', arguments: { qq: '444', minutes: 10 } }, admin.context);
    assert.ok(mute.ok, mute.output);
    assert.deepEqual(admin.bans, [{ group_id: 555, user_id: 444, duration: 600 }]);

    // unmute
    const unmute = await executeChatTool({ name: 'group_mute', arguments: { qq: '444', minutes: 0 } }, admin.context);
    assert.ok(unmute.ok);
    assert.equal(admin.bans[1]!.duration, 0);

    // refusal: owner / master / self / fellow admin (bot is only admin)
    for (const [qq, why] of [['111', '群主'], ['123456789', '号主'], [String(BOT_ID), '我自己'], ['222', '管理员']] as const) {
      const refused = await executeChatTool({ name: 'group_mute', arguments: { qq, minutes: 5 } }, admin.context);
      assert.equal(refused.ok, false, qq);
      assert.ok(refused.output.includes(why), `${qq}: ${refused.output}`);
    }
    assert.equal(admin.bans.length, 2, 'refusals never reach napcat');
  } finally {
    await admin.cleanup();
  }

  // owner-role bot CAN mute an admin; member bot cannot mute anyone
  const owner = await makeContext({ botRole: 'owner' });
  try {
    const ok = await executeChatTool({ name: 'group_mute', arguments: { qq: '222', minutes: 1 } }, owner.context);
    assert.ok(ok.ok, ok.output);
    assert.deepEqual(owner.bans, [{ group_id: 555, user_id: 222, duration: 60 }]);
  } finally {
    await owner.cleanup();
  }
  const member = await makeContext({ botRole: 'member' });
  try {
    const refused = await executeChatTool({ name: 'group_mute', arguments: { qq: '444', minutes: 1 } }, member.context);
    assert.equal(refused.ok, false);
    assert.equal(member.bans.length, 0);
  } finally {
    await member.cleanup();
  }

  // private scope refuses outright
  const priv = await makeContext({ scopeType: 'private' });
  try {
    const refused = await executeChatTool({ name: 'group_mute', arguments: { qq: '444', minutes: 1 } }, priv.context);
    assert.equal(refused.ok, false);
    assert.ok(refused.output.includes('群聊'));
  } finally {
    await priv.cleanup();
  }
});

test('send_message to the bot\'s own account is refused', async () => {
  const selfChat = await makeContext({ scopeType: 'private', scopeId: String(BOT_ID) });
  try {
    const result = await executeChatTool({ name: 'send_message', arguments: { content: '喂？' } }, selfChat.context);
    assert.equal(result.ok, false);
    assert.ok(result.output.includes('自己的账号'));
  } finally {
    await selfChat.cleanup();
  }
});
