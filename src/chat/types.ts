/**
 * Shared chat data types — ported from legacy core/events.py and the turn-item
 * dict contract in core/ai_runtime.py / core/event_adapters.py.
 */

export interface ChatMessage {
  /** 'master' = 主AI（最高级中枢）的虚拟 scope（固定 master:core）：上级 agent，
   * 不是任何 QQ 号，没有 QQ 窗口——只经面板/数据线/下级上报注入；
   * 'domain' = 领域主级 AI 的虚拟 scope（无 QQ 窗口，只走内部注入）；
   * 'bili'/'mcclient' = 领域下级 AI 的虚拟 scope（每视频/每 MC client 一个，同样无 QQ 窗口）。 */
  chatType: 'group' | 'private' | 'master' | 'domain' | 'bili' | 'mcclient' | 'app';
  /** domain/bili/mcclient scope 用字符串 id（如 'mc'、'BV1xx'），QQ scope 是数字号。 */
  chatId: number | string;
  userId: number;
  text: string;
  rawMessage: string;
  sender: { nickname?: string; card?: string; user_id?: number; [key: string]: unknown };
  messageId: number | string | null;
  mentionsSelf: boolean;
  timestamp: number; // epoch seconds
  rawData: Record<string, unknown>;
}

export function messageNickname(message: ChatMessage): string {
  return String(message.sender?.nickname || message.sender?.card || message.userId);
}

export type SourceKind =
  | 'minecraft_chat'
  | 'mobile_app'
  | 'admin_webui'
  | 'internal_task'
  | 'self_other_device'
  | 'dataline'
  | 'group'
  | 'friend_private'
  | 'group_temp_private'
  | 'system_private'
  | 'other_private';

const SOURCE_LABELS: Record<SourceKind, string> = {
  minecraft_chat: 'Minecraft 玩家聊天（游戏名不代表 QQ 或管理员身份）',
  admin_webui: '系统管理员（后台控制台）',
  mobile_app: '手机 App 独立会话用户（非 QQ 身份）',
  internal_task: '后台任务',
  self_other_device: '本账号其他设备',
  dataline: '主人-手机数据线',
  group: 'QQ群消息',
  friend_private: 'QQ好友私聊',
  group_temp_private: '群临时会话',
  system_private: '系统或官方来源',
  other_private: '非好友或其他私聊来源',
};

export function sourceLabel(kind: SourceKind): string {
  return SOURCE_LABELS[kind] ?? '未知来源';
}

/** A persisted / rendered chat history entry (flat dict in legacy). */
export interface HistoryEntry {
  user_id: number;
  nickname: string;
  text: string;
  raw_message?: string;
  message_id?: number | string | null;
  message_ref?: string;
  timestamp: number;
  source_kind?: SourceKind;
  source_label?: string;
  /** Image URLs carried by this message (parsed from CQ:image / segments). */
  image_refs?: string[];
  tool_context_messages?: ModelMessage[];
  [key: string]: unknown;
}

/** One trigger message entry (what the model sees as "the new input"). */
export interface TriggerEntry {
  user_id: number;
  nickname: string;
  text: string;
  raw_message?: string;
  message_id?: number | string | null;
  message_ref?: string;
  timestamp: number;
  source_label?: string;
  source_kind?: SourceKind;
  raw_source?: string;
  /** Image URLs carried by this message (parsed from CQ:image / segments). */
  image_refs?: string[];
  [key: string]: unknown;
}

/** Role-based model message (protocol-neutral; rendered per provider later). */
export type ModelMessage =
  | { role: 'user'; content: string | ContentBlock[] }
  | { role: 'assistant'; content: string | ContentBlock[]; toolCalls?: ToolCall[] }
  | { role: 'tool_result'; toolCallId: string; content: string | ContentBlock[] };

export type ContentBlock =
  | { type: 'text'; text: string; cacheControl?: { type: 'ephemeral' } }
  | { type: 'image'; url: string };

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export function isInternalReportEntry(entry: { source_kind?: string; raw_source?: unknown; user_id?: number }): boolean {
  if (String(entry.source_kind ?? '') === 'internal_task') return true;
  const rawSource = String(entry.raw_source ?? '');
  if (rawSource === 'dev_agent_task_report' || rawSource === 'agent_message') return true;
  const userId = Number(entry.user_id);
  return Number.isFinite(userId) && userId === 0;
}

/**
 * 主AI 的 scope：上级 agent 只此一个，与号主 QQ（ai.master_qq，只是「号主身份」）
 * 完全脱钩——号主私聊 private:<号主QQ> 是普通下级会话。
 */
export const MASTER_SCOPE_TYPE = 'master';
export const MASTER_SCOPE_ID = 'core';
export const MASTER_SCOPE_KEY = `${MASTER_SCOPE_TYPE}:${MASTER_SCOPE_ID}`;

export function isMasterScope(scopeType: string, scopeId: string | number): boolean {
  return scopeType === MASTER_SCOPE_TYPE && String(scopeId) === MASTER_SCOPE_ID;
}

export function scopeKeyOf(chatType: string, chatId: string | number): string {
  return `${chatType}:${chatId}`;
}
