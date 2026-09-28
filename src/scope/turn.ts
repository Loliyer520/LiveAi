/**
 * Scope turn item — the unit a scope actor consumes per turn. Ported from the
 * legacy turn-item dict contract (core/event_adapters.py).
 *
 * One TurnItem may bundle several queued messages (batch merge after a busy
 * turn): they surface as `triggerMessages` in FIFO order while the last live
 * entry becomes the representative message.
 */

import { EventEnvelope, EventType } from './envelope.js';
import type { EventBatch } from './mailbox.js';
import type { ChatMessage, HistoryEntry, TriggerEntry } from '../chat/types.js';

export type TurnKind = 'message' | 'task' | 'report';

export interface TurnItem {
  kind: TurnKind;
  message: ChatMessage | null;
  cleaned: string;
  agentId: string;
  scopeKey: string;
  deferredCount: number;
  triggerMessages: TriggerEntry[];
  messageEpoch: number | null;
  historySeed: HistoryEntry[] | null;
  silentEvent: boolean;
  batchItems?: TurnItem[];
  mailboxEventIds?: string[];
  mailboxSequences?: number[];
  turnMetadata?: Record<string, unknown>;
  batchMetadata?: Record<string, unknown>;
  taskId?: string;
  [key: string]: unknown;
}

export function envelopeFromTurnItem(item: TurnItem): EventEnvelope {
  const kind = item.kind;
  const message = item.message;
  if (message !== null) {
    const systemEvent = String((message.rawData as Record<string, unknown>).system_event ?? '');
    const eventType: EventType = (
      {
        agent_message: 'agent_report',
        alarm: 'alarm',
        recurring_task: 'recurring_task',
        main_ai_message: 'main_ai_message',
      } as Record<string, EventType>
    )[systemEvent] ?? (kind === 'message' ? 'message' : kind === 'task' ? 'recurring_task' : 'agent_report');
    const payload = {
      message: { ...message },
      kind,
      cleaned: item.cleaned,
      agent_id: item.agentId,
      deferred_count: item.deferredCount,
      trigger_messages: item.triggerMessages,
      message_epoch: item.messageEpoch,
      history_seed: item.historySeed,
      silent_event: item.silentEvent,
    };
    const raw = message.rawData as Record<string, unknown>;
    const source = String(raw.source ?? raw.system_event ?? kind);
    const messageId = message.messageId;
    const eventId =
      messageId !== null && messageId !== undefined && messageId !== ''
        ? String(messageId)
        : String(raw.event_id ?? '') || `${message.chatType}:${message.chatId}:${message.userId}:${message.timestamp}`;
    return new EventEnvelope({
      eventType,
      scopeType: message.chatType,
      scopeId: String(message.chatId),
      payload,
      source,
      eventId,
      occurredAt: message.timestamp,
    });
  }
  if (kind === 'message') throw new Error('message scope turn item must include message');
  const payload = {
    kind,
    item: {
      cleaned: item.cleaned,
      agent_id: item.agentId,
      deferred_count: item.deferredCount,
      trigger_messages: item.triggerMessages,
      message_epoch: item.messageEpoch,
      history_seed: item.historySeed,
      silent_event: item.silentEvent,
      task_id: item.taskId,
    },
  };
  return new EventEnvelope({
    eventType: kind === 'task' ? 'recurring_task' : 'agent_report',
    scopeType: item.scopeKey.split(':', 2)[0],
    scopeId: item.scopeKey.split(':', 2)[1] ?? '',
    payload,
    source: item.taskId ? `task:${item.taskId}` : kind,
    eventId: item.taskId ?? `${kind}:${item.scopeKey}`,
  });
}

export function turnItemFromEnvelope(event: EventEnvelope): TurnItem {
  const data = event.payload as Record<string, unknown>;
  const kind = String(data.kind) as TurnKind;
  const messageData = data.message as Partial<ChatMessage> | undefined;
  if (messageData === undefined) {
    const itemData = (data.item ?? {}) as Record<string, unknown>;
    return {
      kind,
      message: null,
      cleaned: String(itemData.cleaned ?? ''),
      agentId: String(itemData.agent_id ?? ''),
      scopeKey: event.scopeKey,
      deferredCount: Number(itemData.deferred_count ?? 0),
      triggerMessages: (itemData.trigger_messages as TriggerEntry[]) ?? [],
      messageEpoch: (itemData.message_epoch as number | null) ?? null,
      historySeed: (itemData.history_seed as HistoryEntry[]) ?? null,
      silentEvent: Boolean(itemData.silent_event),
      taskId: itemData.task_id ? String(itemData.task_id) : undefined,
      mailboxEventIds: [event.eventId],
      mailboxSequences: [event.mailboxSequence ?? 0],
    };
  }
  const message = normalizeStoredMessage(messageData);
  return {
    kind,
    message,
    cleaned: String(data.cleaned ?? ''),
    agentId: String(data.agent_id ?? ''),
    scopeKey: event.scopeKey,
    deferredCount: Number(data.deferred_count ?? 0),
    triggerMessages: (data.trigger_messages as TriggerEntry[]) ?? [],
    messageEpoch: (data.message_epoch as number | null) ?? null,
    historySeed: (data.history_seed as HistoryEntry[]) ?? null,
    silentEvent: Boolean(data.silent_event),
    mailboxEventIds: [event.eventId],
    mailboxSequences: [event.mailboxSequence ?? 0],
  };
}

/**
 * Merge one drained batch into a single follow-up turn item.
 * Representative = last live entry; trigger entries accumulate FIFO;
 * deferredCount sums; first non-null historySeed wins.
 */
export function turnItemFromBatch(batch: EventBatch): TurnItem {
  const items = batch.events.map((event) => turnItemFromEnvelope(event));
  const result: TurnItem = { ...structuredClone(items[items.length - 1]) };
  const triggerMessages: TriggerEntry[] = [];
  let deferredCount = 0;
  let historySeed: HistoryEntry[] | null = null;
  for (const item of items) {
    if (item.triggerMessages.length > 0) {
      triggerMessages.push(...structuredClone(item.triggerMessages));
    } else if (item.message !== null) {
      triggerMessages.push({
        user_id: item.message.userId,
        nickname: String(item.message.sender?.nickname || item.message.sender?.card || item.message.userId),
        text: item.cleaned || item.message.text,
        raw_message: item.message.rawMessage,
        message_id: item.message.messageId,
        timestamp: item.message.timestamp,
      });
    }
    deferredCount += Math.max(1, item.deferredCount);
    if (historySeed === null && item.historySeed !== null) {
      historySeed = structuredClone(item.historySeed);
    }
  }
  result.deferredCount = deferredCount;
  result.triggerMessages = triggerMessages;
  result.historySeed = historySeed;
  result.scopeKey = batch.scopeKey;
  result.mailboxEventIds = batch.events.map((event) => event.eventId);
  result.mailboxSequences = batch.events.map((event) => event.mailboxSequence ?? 0);
  result.batchItems = items;
  return result;
}

function normalizeStoredMessage(data: Partial<ChatMessage>): ChatMessage {
  return {
    chatType: (data.chatType ?? 'private') as ChatMessage['chatType'],
    chatId: Number(data.chatId ?? 0),
    userId: Number(data.userId ?? 0),
    text: String(data.text ?? ''),
    rawMessage: String(data.rawMessage ?? ''),
    sender: (data.sender as ChatMessage['sender']) ?? {},
    messageId: data.messageId ?? null,
    mentionsSelf: Boolean(data.mentionsSelf),
    timestamp: Number(data.timestamp ?? 0),
    rawData: (data.rawData as Record<string, unknown>) ?? {},
  };
}
