/**
 * Group role introspection — who owns/admins a group and what the bot itself
 * is. Drives two things:
 *
 *  - the group prompt's 群信息 block (she knows who the owner/admins are and
 *    whether she herself has admin powers);
 *  - runtime gating of admin-only tools (recall others' messages, group_mute).
 *
 * Member lists are heavy on big groups, so results are cached for a few
 * minutes; on fetch failure we degrade to 'member' (tools stay locked).
 */

import type { NapcatBot } from '../bot/napcat.js';

export type GroupRole = 'owner' | 'admin' | 'member';

export interface GroupRolesInfo {
  /** Owner QQ (null when unknown). */
  owner: number | null;
  /** Admin QQs, owner NOT included. */
  admins: number[];
  botRole: GroupRole;
  /** Display names (card preferred over nickname) for owner + admins. */
  names: Record<number, string>;
  fetchedAt: number;
}

/** Parse OneBot get_group_member_list output into roles. Exported for tests. */
export function parseMemberRoles(members: unknown, botId: number): Omit<GroupRolesInfo, 'fetchedAt'> {
  const owner: number[] = [];
  const admins: number[] = [];
  const names: Record<number, string> = {};
  let botRole: GroupRole = 'member';
  if (Array.isArray(members)) {
    for (const raw of members) {
      if (typeof raw !== 'object' || raw === null) continue;
      const member = raw as { user_id?: unknown; role?: unknown; card?: unknown; nickname?: unknown };
      const userId = Number(member.user_id);
      if (!Number.isFinite(userId) || userId <= 0) continue;
      const role = String(member.role ?? 'member');
      if (role === 'owner' || role === 'admin') {
        const card = String(member.card ?? '').trim();
        const nickname = String(member.nickname ?? '').trim();
        names[userId] = card || nickname || String(userId);
        // the bot herself is reported separately via botRole, not listed
        if (userId !== Number(botId)) {
          if (role === 'owner') owner.push(userId);
          else admins.push(userId);
        }
      }
      if (userId === Number(botId)) botRole = role === 'owner' ? 'owner' : role === 'admin' ? 'admin' : 'member';
    }
  }
  return { owner: owner[0] ?? null, admins, botRole, names };
}

export class GroupRolesCache {
  private readonly cache = new Map<string, GroupRolesInfo>();

  constructor(
    private readonly bot: NapcatBot,
    private readonly ttlMs = 10 * 60 * 1000,
  ) {}

  /** Fresh-enough roles for a group; degraded info on fetch failure. */
  async get(groupId: string | number): Promise<GroupRolesInfo> {
    const key = String(groupId);
    const cached = this.cache.get(key);
    if (cached && Date.now() - cached.fetchedAt < this.ttlMs) return cached;
    try {
      const members = await this.bot.getGroupMemberList(Number(groupId));
      const info: GroupRolesInfo = { ...parseMemberRoles(members, this.bot.selfId), fetchedAt: Date.now() };
      this.cache.set(key, info);
      return info;
    } catch {
      // Keep serving stale data if we have it; otherwise locked-down default.
      return cached ?? { owner: null, admins: [], botRole: 'member', names: {}, fetchedAt: 0 };
    }
  }

  invalidate(groupId: string | number): void {
    this.cache.delete(String(groupId));
  }
}
