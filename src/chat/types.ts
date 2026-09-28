/**
 * Shared chat data types — ported from legacy core/events.py and the turn-item
 * dict contract in core/ai_runtime.py / core/event_adapters.py.
 */

export interface ChatMessage {
  chatType: 'group' | 'private';
  chatId: number;
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
  | 'admin_webui'
  | 'internal_task'
  | 'self_other_device'
  | 'group'
  | 'friend_private'
  | 'group_temp_private'
  | 'system_private'
  | 'other_private';

const SOURCE_LABELS: Record<SourceKind, string> = {
  admin_webui: '系统管理员（后台控制台）',
  internal_task: '后台任务',
  self_other_device: '本人-其他设备',
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

export function scopeKeyOf(chatType: string, chatId: string | number): string {
  return `${chatType}:${chatId}`;
}
