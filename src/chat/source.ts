/**
 * Message source classification. Ported from legacy _message_source_kind /
 * _message_source_label: admin console, internal tasks, self-other-device,
 * group, friend private, group-temp private, system private.
 */

import type { ChatMessage, SourceKind } from './types.js';
import { sourceLabel } from './types.js';

export function sourceKindOf(message: ChatMessage): SourceKind {
  const raw = message.rawData as Record<string, unknown>;
  if (message.userId === 0 && raw.source === 'admin_webui') return 'admin_webui';
  if (message.userId === 0 && raw.source === 'dev_agent_task_report') return 'internal_task';
  if (message.userId === 0 && raw.source === 'agent_message') return 'internal_task';
  if (message.userId === 0 && raw.source === 'qq_request_event') return 'internal_task';
  if (raw.source === 'self_other_device') return 'self_other_device';
  if (message.chatType === 'group') return 'group';
  const subType = String(raw.sub_type ?? '').trim().toLowerCase();
  const nickname = String(message.sender?.nickname ?? '');
  if (nickname.includes('系统') || message.userId === 10000) return 'system_private';
  if (subType === 'group' || subType === 'group_self') return 'group_temp_private';
  return 'friend_private';
}

export function sourceLabelOf(message: ChatMessage): string {
  return sourceLabel(sourceKindOf(message));
}

export function shouldIgnoreMessage(message: ChatMessage): boolean {
  return sourceKindOf(message) === 'system_private';
}

/** Strip the CQ:at mentioning ourselves (used for command parsing / display). */
export function cleanText(message: ChatMessage, selfId: number | string, eventSelfId?: unknown): string {
  const selfIds = new Set([String(selfId)]);
  if (eventSelfId !== undefined && eventSelfId !== null && String(eventSelfId) !== '') {
    selfIds.add(String(eventSelfId));
  }
  let text = message.text;
  for (const id of selfIds) {
    text = text.replaceAll(new RegExp(`\\[CQ:at,qq=${escapeRegExp(id)}(?:,[^\\]]*)?\\]\\s*`, 'g'), '');
  }
  return text.trim();
}

/** Re-mark @bot visibility into the model-facing text (bot can't see CQ codes). */
export const MENTION_SELF_MARK = '[被@] ';

export function markMentionsSelf(message: ChatMessage, text: string): string {
  if (message.chatType !== 'group' || !message.mentionsSelf) return text;
  if (text.startsWith(MENTION_SELF_MARK)) return text;
  return `${MENTION_SELF_MARK}${text}`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
