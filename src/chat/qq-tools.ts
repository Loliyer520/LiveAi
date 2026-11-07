/** QQ account/group tools. Composite actions keep the model's tool list small. */

import type { ToolSchema } from '../models/types.js';
import { parseMemberRoles, type GroupRolesInfo } from './group-roles.js';
import { NAPCAT_API_GUIDE } from './napcat-api-guide.js';
import type { ToolContext, ToolResult } from './tools.js';

const QQ_ID = { type: 'string', description: '对方 QQ 号（数字字符串）' };
const GROUP_ID = { type: 'string', description: '群号；群子AI留空=当前群，主AI/Onebot 中枢跨群时必填' };

export const QQ_CHAT_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'group_manage',
    description:
      '群管理复合工具。action=kick 踢人（qq 可逗号分隔批量，reject_add_request=true 拒绝其再申请）；'
      + 'whole_mute 全员禁言/解除（enable）；set_admin 设/撤管理员（enable，需我是群主）；'
      + 'set_card 改群名片（card，空=清除）；set_title 设专属头衔（title，需我是群主）；'
      + 'notice 发群公告（content，可选 image/pinned/confirm_required）；rename 改群名（name）；sign 群打卡。'
      + '普通群子AI只能管当前群；主AI/Onebot 中枢可传 group_id 管其他群。退群用 qq_account，解散群不支持。'
      + '踢人/禁言只用于明确违规或用户明确要求，别拿管理权限玩闹。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'kick / whole_mute / set_admin / set_card / set_title / notice / rename / sign' },
        group_id: GROUP_ID,
        qq: { type: 'string', description: '目标 QQ；kick 可用逗号分隔多个，其他动作只传一个' },
        enable: { type: 'boolean', description: 'whole_mute/set_admin：true=开启或设置，false=解除或撤销' },
        reject_add_request: { type: 'boolean', description: 'kick：是否拒绝对方再次加群，默认 false' },
        card: { type: 'string', description: 'set_card：新群名片，空串清除' },
        title: { type: 'string', description: 'set_title：专属头衔，空串清除' },
        content: { type: 'string', description: 'notice：公告正文' },
        image: { type: 'string', description: 'notice：可选公告图，http(s):// 或 base64://' },
        pinned: { type: 'boolean', description: 'notice：是否置顶，默认 false' },
        confirm_required: { type: 'boolean', description: 'notice：是否要求群员确认，默认 false' },
        name: { type: 'string', description: 'rename：新群名' },
      },
      required: ['action'],
    },
  },
  {
    name: 'qq_interact',
    description: 'QQ 轻互动。action=poke 戳一戳（群里 qq 必填；私聊留空默认当前对方）；action=like 给资料卡点赞（times 1~10，默认 1）。别连续刷。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'poke / like' },
        qq: QQ_ID,
        group_id: GROUP_ID,
        times: { type: 'number', description: 'like：点赞次数 1~10，默认 1' },
      },
      required: ['action'],
    },
  },
];

export const QQ_MAIN_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'qq_account',
    description:
      '【主AI/Onebot 中枢】QQ 账号与会话资产。action=friend_requests/group_requests 列出收到的好友/入群申请；'
      + 'approve_friend/reject_friend、approve_group/reject_group 用列表里的 flag 批复（入群申请含别人邀请我入群，approve_group 即接受邀请加群）；'
      + 'delete_friend 删除好友（qq，可选 both=true 双向删除）；leave_group 退出群（group_id，只退群绝不解散）；'
      + 'list_friends/list_groups 查通讯录。主动加陌生好友/主动申请任意群不是 OneBot 标准能力，需对方发申请或邀请。'
      + '拒绝可用 reason；同意好友可带 remark。删好友/退群前确认用户意图，不能对号主做删除/踢禁操作。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'friend_requests / group_requests / approve_friend / reject_friend / approve_group / reject_group / delete_friend / leave_group / list_friends / list_groups' },
        flag: { type: 'string', description: '申请列表里的 flag（原样传，不要猜）' },
        qq: QQ_ID,
        group_id: { type: 'string', description: 'leave_group：要退出的群号' },
        remark: { type: 'string', description: 'approve_friend：添加后的好友备注' },
        reason: { type: 'string', description: '拒绝申请时的理由' },
        both: { type: 'boolean', description: 'delete_friend：是否双向删除，默认 false' },
      },
      required: ['action'],
    },
  },
  {
    name: 'qzone',
    description:
      '【主AI/Onebot 中枢】QQ 空间。action=publish 发说说：content 正文（可空但必须有图），images 最多 9 个 http(s)/base64 图片；'
      + 'ugc_right=1 所有人、4 好友、16 仅 target_uins 可见、64 仅自己、128 target_uins 不可见。返回 tid。'
      + 'action=delete 按 tid 删除自己的说说。评论/点赞公开动态等低频能力用 napcat_call(action="help") 查。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'publish / delete' },
        content: { type: 'string', description: 'publish：说说正文' },
        images: { type: 'array', items: { type: 'string' }, description: 'publish：图片 URL 或 base64://，最多 9 张' },
        ugc_right: { type: 'number', description: 'publish：1/4/16/64/128，默认 1' },
        target_uins: { type: 'array', items: { type: 'string' }, description: 'publish：ugc_right=16/128 时作用 QQ 号列表' },
        tid: { type: 'string', description: 'delete：发说说时返回的 tid' },
      },
      required: ['action'],
    },
  },
  {
    name: 'napcat_call',
    description:
      '【主AI/Onebot 中枢】低频 NapCat/OneBot 接口的受控 JSON 直调。action 传接口名，params 直接写请求体 JSON 对象；'
      + '先调 {action:"help",params:{}} 获取接口文档和示例。专用工具已覆盖的功能优先用专用工具。'
      + '禁止底层 send_packet、进程/账号资料高风险动作、任意本地文件读取；不要用它解散群。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'NapCat action 名；help=查看低频接口文档' },
        params: { type: 'object', description: '直接传给 NapCat 的 JSON 请求体，省略={}' },
        timeout_ms: { type: 'number', description: '可选超时，1000~180000ms，默认 30000' },
      },
      required: ['action'],
    },
  },
];

export async function executeQqTool(
  call: { name: string; arguments: Record<string, unknown> },
  context: ToolContext,
): Promise<ToolResult | null> {
  const args = call.arguments ?? {};
  try {
    switch (call.name) {
      case 'group_manage': return await groupManage(args, context);
      case 'qq_interact': return await qqInteract(args, context);
      case 'qq_account': return await qqAccount(args, context);
      case 'qzone': return await qzone(args, context);
      case 'napcat_call': return await napcatCall(args, context);
      default: return null;
    }
  } catch (error) {
    return { ok: false, output: `${call.name} 失败: ${error instanceof Error ? error.message : String(error)}` };
  }
}

function isQqMain(context: ToolContext): boolean {
  return context.isMaster || (context.scopeType === 'domain' && context.scopeId === 'onebot');
}

function requireQqMain(context: ToolContext, tool: string): ToolResult | null {
  return isQqMain(context) ? null : { ok: false, output: `${tool} 仅主AI和 Onebot 中枢可用。` };
}

function qqId(value: unknown): number | null {
  const id = Number(String(value ?? '').trim());
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function requireQq(value: unknown, label = 'qq'): number {
  const id = qqId(value);
  if (id === null) throw new Error(`${label} 不是有效 QQ/群号。`);
  return id;
}

function bool(value: unknown, fallback = false): boolean {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'string') return value.toLowerCase() !== 'false' && value !== '0';
  return Boolean(value);
}

async function groupManage(args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  const action = String(args.action ?? '').trim();
  const currentGroup = context.scopeType === 'group' ? requireQq(context.scopeId, '当前群号') : null;
  const explicitGroup = String(args.group_id ?? '').trim();
  // 非主AI/非 Onebot 中枢只能管自己所在的群：群子AI不许换群，私聊/虚拟下级
  // 没有「自己的群」，显式 group_id 一律拒绝（旧逻辑只拦了群聊里的跨群）。
  if (!isQqMain(context) && explicitGroup) {
    if (currentGroup === null) {
      return { ok: false, output: '跨群管理仅主AI可用，请上报主AI处理。' };
    }
    if (Number(explicitGroup) !== currentGroup) {
      return { ok: false, output: '群子AI只能管理当前群，跨群操作请上报主AI。' };
    }
  }
  const groupId = explicitGroup ? requireQq(explicitGroup, 'group_id') : currentGroup;
  if (groupId === null) return { ok: false, output: '这里不是群聊，跨群管理必须传 group_id。' };
  if (action === 'sign') {
    await context.bot.callApi('set_group_sign', { group_id: groupId });
    return { ok: true, output: `已在群 ${groupId} 打卡。` };
  }

  const roles = await rolesForGroup(groupId, context);
  if (roles.botRole === 'member' && !(action === 'set_card' && String(args.qq ?? '') === String(context.bot.selfId))) {
    return { ok: false, output: `我在群 ${groupId} 不是管理员，无法执行 ${action}。` };
  }
  const adminOnly = new Set(['kick', 'whole_mute', 'set_admin', 'set_card', 'set_title', 'notice', 'rename']);
  if (!adminOnly.has(action)) return { ok: false, output: `未知 group_manage action: ${action || '(空)'}` };

  if (action === 'whole_mute') {
    const enable = bool(args.enable, true);
    await context.bot.callApi('set_group_whole_ban', { group_id: groupId, enable });
    return { ok: true, output: `已${enable ? '开启' : '解除'}群 ${groupId} 的全员禁言。` };
  }
  if (action === 'notice') {
    const content = String(args.content ?? '').trim();
    if (!content) return { ok: false, output: '公告 content 不能为空。' };
    if (content.length > 2000) return { ok: false, output: '公告最长 2000 字。' };
    const image = String(args.image ?? '').trim();
    if (image) assertRemoteResource(image, 'image');
    await context.bot.callApi('_send_group_notice', {
      group_id: groupId,
      content,
      ...(image ? { image } : {}),
      pinned: bool(args.pinned) ? 1 : 0,
      confirm_required: bool(args.confirm_required) ? 1 : 0,
      type: 1,
      is_show_edit_card: 0,
      tip_window_type: 0,
    });
    return { ok: true, output: `已在群 ${groupId} 发布公告。` };
  }
  if (action === 'rename') {
    const name = String(args.name ?? '').trim();
    if (!name || name.length > 48) return { ok: false, output: '群名 name 需为 1~48 字。' };
    await context.bot.callApi('set_group_name', { group_id: groupId, group_name: name });
    return { ok: true, output: `已将群 ${groupId} 改名为「${name}」。` };
  }

  const targets = String(args.qq ?? '').split(/[\s,，]+/).map((part) => part.trim()).filter(Boolean).map((part) => requireQq(part));
  if (targets.length === 0) return { ok: false, output: `${action} 需要 qq。` };
  if (action !== 'kick' && targets.length !== 1) return { ok: false, output: `${action} 一次只能操作一个 QQ。` };
  if (targets.length > 20) return { ok: false, output: '批量踢人一次最多 20 人。' };
  for (const target of targets) {
    if (target === Number(context.masterQq) && (action === 'kick' || (action === 'set_admin' && !bool(args.enable, true)))) {
      return { ok: false, output: '不能踢出或撤销号主的管理员。' };
    }
    if (roles.owner === target && action !== 'set_card' && action !== 'set_title') return { ok: false, output: '不能对群主执行这个操作。' };
    if (roles.botRole === 'admin' && roles.admins.includes(target) && (action === 'kick' || action === 'set_card')) {
      return { ok: false, output: '我只是管理员，不能对同级管理员执行这个操作。' };
    }
  }
  const target = targets[0]!;
  if (action === 'kick') {
    if (targets.includes(Number(context.bot.selfId))) return { ok: false, output: '不能用 kick 踢自己；退群请用 qq_account。' };
    const reject = bool(args.reject_add_request);
    if (targets.length === 1) await context.bot.callApi('set_group_kick', { group_id: groupId, user_id: target, reject_add_request: reject });
    else await context.bot.callApi('set_group_kick_members', { group_id: groupId, user_id: targets.map(String), reject_add_request: reject });
    return { ok: true, output: `已将 ${targets.join('、')} 移出群 ${groupId}${reject ? '，并拒绝再次加群' : ''}。` };
  }
  if (action === 'set_admin') {
    if (roles.botRole !== 'owner') return { ok: false, output: '设/撤管理员需要我是群主。' };
    if (target === Number(context.bot.selfId)) return { ok: false, output: '不能修改自己的管理员身份。' };
    const enable = bool(args.enable, true);
    await context.bot.callApi('set_group_admin', { group_id: groupId, user_id: target, enable });
    return { ok: true, output: `已${enable ? '设置' : '撤销'} ${target} 的群管理员。` };
  }
  if (action === 'set_card') {
    const card = String(args.card ?? '').trim();
    if (card.length > 60) return { ok: false, output: '群名片最长 60 字。' };
    await context.bot.callApi('set_group_card', { group_id: groupId, user_id: target, card });
    return { ok: true, output: `已${card ? `将 ${target} 的群名片改为「${card}」` : `清除 ${target} 的群名片`}。` };
  }
  if (roles.botRole !== 'owner') return { ok: false, output: '专属头衔需要我是群主。' };
  const title = String(args.title ?? '').trim();
  if (title.length > 18) return { ok: false, output: '专属头衔最长 18 字。' };
  await context.bot.callApi('set_group_special_title', { group_id: groupId, user_id: target, special_title: title });
  return { ok: true, output: `已${title ? `给 ${target} 设置头衔「${title}」` : `清除 ${target} 的专属头衔`}。` };
}

async function rolesForGroup(groupId: number, context: ToolContext): Promise<GroupRolesInfo> {
  if (context.scopeType === 'group' && Number(context.scopeId) === groupId && context.getGroupRoles) {
    const cached = await context.getGroupRoles();
    if (cached.botRole !== 'member') return cached;
  }
  try {
    const members = await context.bot.getGroupMemberList(groupId);
    return { ...parseMemberRoles(members, context.bot.selfId), fetchedAt: Date.now() };
  } catch {
    return { owner: null, admins: [], botRole: 'member', names: {}, fetchedAt: 0 };
  }
}

async function qqInteract(args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  const action = String(args.action ?? '').trim();
  const fallback = context.scopeType === 'private' ? context.scopeId : '';
  const target = requireQq(args.qq || fallback);
  if (action === 'like') {
    const times = Math.min(10, Math.max(1, Math.trunc(Number(args.times ?? 1) || 1)));
    await context.bot.callApi('send_like', { user_id: target, times });
    return { ok: true, output: `已给 ${target} 的资料卡点赞 ${times} 次。` };
  }
  if (action !== 'poke') return { ok: false, output: `未知 qq_interact action: ${action || '(空)'}` };
  const groupId = args.group_id || (context.scopeType === 'group' ? context.scopeId : undefined);
  // 同 group_manage：非主AI只能在当前群戳人，私聊/虚拟下级不能借 group_id 跨群。
  if (groupId !== undefined && !isQqMain(context)) {
    if (context.scopeType !== 'group' || Number(groupId) !== Number(context.scopeId)) {
      return { ok: false, output: '群子AI只能在当前群戳人。' };
    }
  }
  await context.bot.callApi('send_poke', {
    user_id: target,
    target_id: target,
    ...(groupId !== undefined ? { group_id: requireQq(groupId, 'group_id') } : {}),
  });
  return { ok: true, output: `已戳了 ${target} 一下。` };
}

async function qqAccount(args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  const denied = requireQqMain(context, 'qq_account');
  if (denied) return denied;
  const action = String(args.action ?? '').trim();
  if (action === 'friend_requests' || action === 'group_requests') {
    const type = action === 'friend_requests' ? 'friend' : 'group';
    const events = context.bot.cachedRequests(type);
    if (events.length === 0) return { ok: true, output: type === 'friend' ? '（没有待处理好友申请）' : '（没有待处理入群申请/邀请）' };
    return { ok: true, output: `待处理 ${events.length} 条：\n${events.slice(0, 30).map(formatRequest).join('\n')}` };
  }
  if (action === 'list_friends' || action === 'list_groups') {
    const data = await context.bot.callApi(action === 'list_friends' ? 'get_friend_list' : 'get_group_list', {}, 60000);
    const list = Array.isArray(data) ? data : [];
    const lines = list.slice(0, 200).map((item) => {
      const row = item as Record<string, unknown>;
      return action === 'list_friends'
        ? `${String(row.user_id ?? '')} ${String(row.remark || row.nickname || '')}`
        : `${String(row.group_id ?? '')} ${String(row.group_name ?? '')}`;
    });
    return { ok: true, output: `共 ${list.length} 条${list.length > 200 ? '，只显示前 200' : ''}：\n${lines.join('\n')}` };
  }
  if (action === 'delete_friend') {
    const target = requireQq(args.qq);
    if (target === Number(context.masterQq)) return { ok: false, output: '不能删除号主好友。' };
    const both = bool(args.both);
    await context.bot.callApi('delete_friend', { friend_id: target, temp_both_del: both });
    return { ok: true, output: `已删除好友 ${target}${both ? '（双向删除）' : ''}。` };
  }
  if (action === 'leave_group') {
    const groupId = requireQq(args.group_id, 'group_id');
    await context.bot.callApi('set_group_leave', { group_id: groupId, is_dismiss: false });
    return { ok: true, output: `已退出群 ${groupId}（未解散群）。` };
  }
  const match = /^(approve|reject)_(friend|group)$/.exec(action);
  if (!match) return { ok: false, output: `未知 qq_account action: ${action || '(空)'}` };
  const approve = match[1] === 'approve';
  const type = match[2] as 'friend' | 'group';
  const flag = String(args.flag ?? '').trim();
  if (!flag) return { ok: false, output: '批复申请必须传列表里的 flag。' };
  const event = context.bot.cachedRequests(type).find((entry) => String(entry.flag ?? '') === flag);
  if (!event) return { ok: false, output: `没有找到${type === 'friend' ? '好友' : '入群'}申请 flag=${flag}；先刷新申请列表，勿猜 flag。` };
  if (type === 'friend') {
    await context.bot.callApi('set_friend_add_request', {
      flag,
      approve,
      ...(approve && String(args.remark ?? '').trim() ? { remark: String(args.remark).trim() } : {}),
    });
  } else {
    await context.bot.callApi('set_group_add_request', {
      flag,
      sub_type: String(event.sub_type ?? 'add'),
      approve,
      ...(!approve && String(args.reason ?? '').trim() ? { reason: String(args.reason).trim() } : {}),
    });
  }
  context.bot.consumeRequest(`${type}:${String(event.user_id ?? '')}:${flag}`);
  const kind = type === 'friend' ? '好友申请' : String(event.sub_type) === 'invite' ? '入群邀请' : '入群申请';
  return { ok: true, output: `已${approve ? '同意' : '拒绝'} ${String(event.user_id ?? '?')} 的${kind}。` };
}

function formatRequest(event: Record<string, unknown>): string {
  const type = String(event.request_type) === 'friend' ? '好友申请' : String(event.sub_type) === 'invite' ? '邀请我入群' : '入群申请';
  const group = event.group_id ? ` 群=${String(event.group_id)}` : '';
  return `- flag=${String(event.flag ?? '')} ${type} 申请人=${String(event.user_id ?? '?')}${group} 验证=${JSON.stringify(String(event.comment ?? '')).slice(0, 300)}`;
}

async function qzone(args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  const denied = requireQqMain(context, 'qzone');
  if (denied) return denied;
  const action = String(args.action ?? '').trim();
  if (action === 'delete') {
    const tid = String(args.tid ?? '').trim();
    if (!tid) return { ok: false, output: '删除说说必须传 tid。' };
    await context.bot.callApi('delete_qzone_msg', { tid });
    return { ok: true, output: `已删除说说 ${tid}。` };
  }
  if (action !== 'publish') return { ok: false, output: `未知 qzone action: ${action || '(空)'}` };
  const content = String(args.content ?? '').trim();
  const images = Array.isArray(args.images) ? args.images.map(String).map((value) => value.trim()).filter(Boolean) : [];
  if (!content && images.length === 0) return { ok: false, output: '说说正文和图片不能都为空。' };
  if (content.length > 2000) return { ok: false, output: '说说正文最长 2000 字。' };
  if (images.length > 9) return { ok: false, output: '一条说说最多 9 张图。' };
  images.forEach((image) => assertRemoteResource(image, 'images'));
  const ugcRight = Math.trunc(Number(args.ugc_right ?? 1));
  if (![1, 4, 16, 64, 128].includes(ugcRight)) return { ok: false, output: 'ugc_right 只能是 1/4/16/64/128。' };
  const targetUins = Array.isArray(args.target_uins) ? args.target_uins.map((value) => requireQq(value, 'target_uins')) : [];
  if ((ugcRight === 16 || ugcRight === 128) && targetUins.length === 0) return { ok: false, output: 'ugc_right=16/128 时 target_uins 不能为空。' };
  const result = await context.bot.callApi('send_qzone_msg', {
    content,
    ...(images.length ? { images } : {}),
    ugc_right: ugcRight,
    ...(targetUins.length ? { target_uins: targetUins } : {}),
  }, 180000);
  return { ok: true, output: `说说已发布，tid=${String(result.tid ?? '(未返回)')}。删除时用这个 tid。` };
}

const RAW_BLOCKED_ACTIONS = new Set([
  'send_packet', 'bot_exit', 'set_restart', 'set_qq_profile', 'set_qq_avatar',
  'set_online_status', 'set_diy_online_status', 'set_long_nick', 'set_group_portrait',
]);

async function napcatCall(args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  const denied = requireQqMain(context, 'napcat_call');
  if (denied) return denied;
  const action = String(args.action ?? '').trim();
  if (action === 'help') return { ok: true, output: NAPCAT_API_GUIDE };
  if (!/^[a-z][a-z0-9_]{1,63}$/.test(action)) return { ok: false, output: 'action 不是合法 NapCat 接口名；action="help" 可查文档。' };
  if (RAW_BLOCKED_ACTIONS.has(action)) return { ok: false, output: `接口 ${action} 不在 raw 允许范围。` };
  const params = args.params ?? {};
  if (typeof params !== 'object' || params === null || Array.isArray(params)) return { ok: false, output: 'params 必须是 JSON 对象。' };
  const body = params as Record<string, unknown>;
  const serialized = JSON.stringify(body);
  if (serialized.length > 65536) return { ok: false, output: '请求体超过 64KB；大文件请走既有媒体工具。' };
  assertSafeRawParams(body, '$');
  if (action === 'set_group_leave' && bool(body.is_dismiss)) return { ok: false, output: 'raw 不允许解散群；set_group_leave 仅允许 is_dismiss=false。' };
  if (action === 'delete_friend' && Number(body.friend_id ?? body.user_id) === Number(context.masterQq)) return { ok: false, output: '不能删除号主好友。' };
  if (['set_group_kick', 'set_group_ban'].includes(action) && Number(body.user_id) === Number(context.masterQq)) return { ok: false, output: '不能踢出或禁言号主。' };
  if (action === 'set_group_kick_members' && Array.isArray(body.user_id) && body.user_id.some((id) => Number(id) === Number(context.masterQq))) return { ok: false, output: '不能踢出号主。' };
  const timeout = Math.min(180000, Math.max(1000, Math.trunc(Number(args.timeout_ms ?? 30000) || 30000)));
  const result = await context.bot.callApi(action, body, timeout);
  return { ok: true, output: `${action} 成功：\n${JSON.stringify(result, null, 2).slice(0, 24000)}` };
}

function assertRemoteResource(value: string, field: string): void {
  if (!/^(https?:\/\/|base64:\/\/)/i.test(value)) throw new Error(`${field} 只允许 http(s):// 或 base64://，禁止本地文件路径。`);
  // 媒体由 NapCat 所在机器去拉，内网地址等于拿 NapCat 主机的网络当跳板。
  // 这里只拦字面量/常见内网名；域名解析结果由 NapCat 侧负责，主AI可用 raw 接口。
  let host = '';
  try {
    host = new URL(value).hostname.toLowerCase().replace(/^\[|\]$/g, '');
  } catch {
    throw new Error(`${field} 不是合法 URL。`);
  }
  if (
    host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.internal') || host.endsWith('.local')
    || host === '::1' || host === '::' || host === '0.0.0.0'
    || /^127\./.test(host)
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^169\.254\./.test(host)
    || /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)
  ) {
    throw new Error(`${field} 不允许内网/本机地址。`);
  }
}

function assertSafeRawParams(value: unknown, path: string): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSafeRawParams(item, `${path}[${index}]`));
    return;
  }
  // 字符串字段全量扫：防护不能只认 file/image 结尾的键——message 里塞一段
  // [CQ:image,file=file:///etc/passwd] 照样会让 NapCat 读本地文件外发。
  if (typeof value === 'string') {
    if (value.startsWith('file://')) {
      throw new Error(`${path} 禁止 file:// 本地路径；请用 http(s)/base64 或文件 ID。`);
    }
    if (/\[CQ:[a-z_]+(?:,[^\]]*?)?\bfile\s*=\s*(?:file:\/\/|\/|~\/|[a-z]:[\\/])/i.test(value)) {
      throw new Error(`${path} 的 CQ 码引用了本地文件（file= 本地路径），已拦截；请用 http(s)/base64 或文件 ID。`);
    }
    return;
  }
  if (typeof value !== 'object' || value === null) return;
  for (const [key, child] of Object.entries(value)) {
    if (/(^|_)(files?|paths?|images?|avatars?|srcs?)$/i.test(key)) {
      assertNoLocalResource(child, `${path}.${key}`);
    }
    assertSafeRawParams(child, `${path}.${key}`);
  }
}

function assertNoLocalResource(value: unknown, path: string): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoLocalResource(item, `${path}[${index}]`));
    return;
  }
  if (typeof value === 'string' && (value.startsWith('/') || value.startsWith('file://') || /^[a-z]:[\\/]/i.test(value))) {
    throw new Error(`${path} 禁止本地文件路径；请用 http(s)/base64 或文件 ID。`);
  }
}

