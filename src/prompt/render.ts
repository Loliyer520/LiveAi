/**
 * Chat history → model messages rendering, with the block protocol the legacy
 * prompts rely on (ported from core/ai_runtime.py rendering helpers):
 *
 *   <user_msg from=".." time="..">HH:MM 昵称(uid): 内容</user_msg>
 *   <user_invisible><tool_report source=".." time="..">..</tool_report></user_invisible>
 *
 * Real user messages merge into consecutive <user_msg> blocks; internal
 * triggers (agent reports / master-AI relays / alarms) merge into
 * <user_invisible> so the model knows the user cannot see them.
 */

import type { HistoryEntry, ModelMessage, TriggerEntry } from '../chat/types.js';
import { isInternalReportEntry } from '../chat/types.js';

const COLLIDING_TOKENS = [
  '</user_msg>',
  '</tool_report>',
  '</user_invisible>',
  '</user_visible>',
  '<user_msg',
  '<tool_report',
  '<user_invisible',
  '<user_visible',
];

/** Only neutralize the wrapping tags themselves; keep code readable. */
export function sanitizeBlockBody(text: string | null | undefined): string {
  let value = String(text ?? '');
  for (const token of COLLIDING_TOKENS) {
    value = value.replaceAll(token, '＜' + token.slice(1));
  }
  return value;
}

export function xmlAttrEscape(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

export function formatClock(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function formatHistoryItem(entry: HistoryEntry): string {
  const timePrefix = formatClock(Number(entry.timestamp ?? 0));
  const userId = String(entry.user_id ?? '').trim();
  const speaker = String(entry.nickname ?? entry.user_id ?? '未知');
  const sourceLabel = String(entry.source_label ?? '').trim();
  const ref = normalizeRef(entry.message_ref);
  const prefix = ref ? `[#${ref}] ` : '';
  if (sourceLabel) {
    return `${prefix}${timePrefix} [${sourceLabel}] ${speaker}(${userId || '未知'}): ${String(entry.text ?? '')}`.trim();
  }
  return `${prefix}${timePrefix} ${speaker}(${userId || '未知'}): ${String(entry.text ?? '')}`.trim();
}

export function normalizeRef(ref: unknown): string {
  const value = String(ref ?? '').trim().replace(/^\[#?|\]$/g, '');
  return value;
}

export function wrapUserMsgBlock(items: HistoryEntry[]): string {
  if (items.length === 0) return '';
  const first = items[0];
  const nickname = String(first.nickname ?? first.user_id ?? '');
  const time = formatClock(Number(first.timestamp ?? 0));
  const inner = items.map((item) => sanitizeBlockBody(formatHistoryItem(item))).join('\n');
  return `<user_msg from="${xmlAttrEscape(nickname)}" time="${xmlAttrEscape(time)}">${inner}</user_msg>`;
}

export function wrapToolReportBlock(item: HistoryEntry, note = ''): string {
  const source = String(item.nickname ?? item.user_id ?? '');
  const time = formatClock(Number(item.timestamp ?? 0));
  const text = sanitizeBlockBody(String(item.text ?? ''));
  const noteAttr = note ? ` note="${xmlAttrEscape(note)}"` : '';
  return `<tool_report source="${xmlAttrEscape(source)}" time="${xmlAttrEscape(time)}"${noteAttr}>${text}</tool_report>`;
}

export function wrapUserInvisibleGroup(items: HistoryEntry[], note = ''): string {
  if (items.length === 0) return '';
  const inner = items.map((item) => wrapToolReportBlock(item, note)).join('\n');
  return `<user_invisible>${inner}</user_invisible>`;
}

/** Render entries in original order, user/invisible runs interleaved. */
export function renderPendingBlocks(items: HistoryEntry[], internalNote = ''): string[] {
  const blocks: string[] = [];
  let userRun: HistoryEntry[] = [];
  let invisibleRun: HistoryEntry[] = [];

  const flushUser = () => {
    if (userRun.length > 0) {
      blocks.push(wrapUserMsgBlock(userRun));
      userRun = [];
    }
  };
  const flushInvisible = () => {
    if (invisibleRun.length > 0) {
      blocks.push(wrapUserInvisibleGroup(invisibleRun, internalNote));
      invisibleRun = [];
    }
  };
  for (const item of items) {
    if (isInternalReportEntry(item)) {
      flushUser();
      invisibleRun.push(item);
    } else {
      flushInvisible();
      userRun.push(item);
    }
  }
  flushUser();
  flushInvisible();
  return blocks;
}

/**
 * Flatten flat history into role-based model messages. Consecutive bot entries
 * collapse into one assistant run (with the latest tool_context kept), user
 * entries merge into block segments.
 */
export function buildRoleBasedHistoryMessages(history: HistoryEntry[], botUserId: string): ModelMessage[] {
  const messages: ModelMessage[] = [];
  let pending: HistoryEntry[] = [];

  const flushPending = () => {
    if (pending.length > 0) {
      messages.push({ role: 'user', content: renderPendingBlocks(pending).join('\n') });
      pending = [];
    }
  };

  let index = 0;
  while (index < history.length) {
    const item = history[index];
    const userId = String(item.user_id ?? '').trim();
    const text = String(item.text ?? '').trim();
    if (userId && userId === botUserId) {
      flushPending();
      const assistantTexts: string[] = [];
      let toolContext: ModelMessage[] = [];
      while (index < history.length) {
        const current = history[index];
        if (String(current.user_id ?? '').trim() !== botUserId) break;
        const context = normalizeToolContextMessages(current.tool_context_messages);
        if (context.length > 0) toolContext = context;
        const currentText = String(current.text ?? '').trim();
        if (currentText) assistantTexts.push(currentText);
        index += 1;
      }
      messages.push(...toolContext);
      const mergedAssistant = assistantTexts.join('\n');
      if (mergedAssistant.trim()) {
        messages.push({ role: 'assistant', content: mergedAssistant });
      }
      continue;
    }
    if (text) pending.push(item);
    index += 1;
  }
  flushPending();
  return messages;
}

export function normalizeToolContextMessages(value: unknown): ModelMessage[] {
  if (!Array.isArray(value)) return [];
  const result: ModelMessage[] = [];
  for (const raw of value) {
    if (typeof raw !== 'object' || raw === null) continue;
    const entry = raw as Record<string, unknown>;
    const role = String(entry.role ?? '');
    if (role === 'user' || role === 'assistant') {
      result.push({ role, content: String(entry.content ?? '') } as ModelMessage);
    } else if (role === 'tool_result') {
      result.push({
        role: 'tool_result',
        toolCallId: String(entry.toolCallId ?? entry.tool_call_id ?? ''),
        content: String(entry.content ?? ''),
      });
    }
  }
  return result;
}

/** The trigger user message for this turn (latest inputs as blocks). */
export function buildTriggerUserMessage(triggerMessages: TriggerEntry[]): string {
  const items = triggerMessages.filter((entry) => String(entry.text ?? '').trim() !== '');
  if (items.length === 0) return '暂无新消息';
  const blocks = renderPendingBlocks(items as HistoryEntry[], '这是系统内部异步结果，默认只更新记忆、消化即可，非必要不要调用 send_message 对外发送');
  return blocks.length > 0 ? blocks.join('\n') : '暂无新消息';
}
