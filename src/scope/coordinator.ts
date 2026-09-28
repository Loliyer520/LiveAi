/**
 * Atomic turn batch coordinator. Ported from legacy
 * core/event_batch_coordinator.py.
 *
 * After a completed turn (or mid-turn, at tool-loop boundaries) this drains one
 * scope snapshot atomically, filters stale entries and merges everything into
 * a single follow-up turn item — so a burst of messages that arrived while the
 * AI was busy triggers exactly one follow-up model call, not N.
 */

import type { InMemoryEventMailbox, EventBatch } from './mailbox.js';
import type { EventEnvelope } from './envelope.js';
import { turnItemFromBatch, type TurnItem } from './turn.js';
import type { HistoryEntry } from '../chat/types.js';

export interface CompletedTurn {
  scopeKey: string;
  historySeed: HistoryEntry[];
  metadata: Record<string, unknown> | null;
}

export interface CoordinatedBatch {
  snapshot: EventBatch;
  entries: { envelope: EventEnvelope; transient: TurnItem }[];
  representative: TurnItem | null;
  turnItem: TurnItem;
}

export class AtomicTurnBatchCoordinator {
  constructor(private readonly mailbox: InMemoryEventMailbox) {}

  drainAfterCompletedTurn(
    completedTurn: CompletedTurn,
    isStale: (item: TurnItem) => boolean,
  ): CoordinatedBatch | null {
    return this.drainScopeFollowup(completedTurn.scopeKey, completedTurn.historySeed, completedTurn.metadata, isStale);
  }

  drainScopeFollowup(
    scopeKey: string,
    historySeed: HistoryEntry[] = [],
    metadata: Record<string, unknown> | null = null,
    isStale: (item: TurnItem) => boolean = () => false,
  ): CoordinatedBatch | null {
    const snapshot = this.mailbox.drainScope(scopeKey);
    if (snapshot === null) return null;

    const liveEntries = snapshot.transients
      .map((transient, index) => ({ transient, envelope: snapshot.events[index] }))
      .filter((entry) => {
        const item = entry.transient as TurnItem | null;
        return item !== null && !isStale(item);
      })
      .map((entry) => ({
        envelope: entry.envelope,
        transient: entry.transient as TurnItem,
      }));
    if (liveEntries.length === 0) return null;

    const liveBatch: EventBatch = {
      scopeKey: snapshot.scopeKey,
      events: liveEntries.map((entry) => entry.envelope),
      transients: liveEntries.map((entry) => entry.transient),
    };
    const turnItem = turnItemFromBatch(liveBatch);

    // Representative = latest live FIFO entry (already is, via turnItemFromBatch).
    const representative = liveEntries[liveEntries.length - 1].transient;
    if (representative.message !== null) {
      turnItem.message = representative.message;
      turnItem.cleaned = representative.cleaned;
      turnItem.agentId = representative.agentId;
      turnItem.kind = representative.kind;
    }
    if (historySeed.length > 0) {
      turnItem.historySeed = historySeed.map((entry) => ({ ...entry }));
    }
    if (metadata !== null) {
      turnItem.turnMetadata = { ...metadata };
    }
    return { snapshot, entries: liveEntries, representative, turnItem };
  }

  /** Tool-loop contract: pop one raw FIFO identity (mid-turn pickup). */
  popToolRaw(scopeKey: string): TurnItem | null {
    const entry = this.mailbox.popScopeEntry(scopeKey);
    if (entry === null) return null;
    return (entry.transient as TurnItem) ?? null;
  }
}

/**
 * Merge several follow-up segments (current live item + drained batches) into
 * one representative turn item. Port of legacy _merge_followup_items.
 */
export function mergeFollowupItems(scopeKey: string, ...segments: (TurnItem | null | undefined)[]): TurnItem | null {
  const mergedItems: TurnItem[] = [];
  let historySeed: HistoryEntry[] | null = null;
  let turnMetadata: Record<string, unknown> | null = null;
  for (const segment of segments) {
    if (segment === null || segment === undefined) continue;
    if (Array.isArray(segment.batchItems) && segment.batchItems.length > 0) {
      mergedItems.push(...segment.batchItems.map((entry) => structuredClone(entry)));
    } else {
      mergedItems.push(structuredClone(segment));
    }
    if (historySeed === null && segment.historySeed !== null) {
      historySeed = structuredClone(segment.historySeed);
    }
    if (turnMetadata === null && segment.turnMetadata !== undefined) {
      turnMetadata = { ...segment.turnMetadata };
    }
  }
  if (mergedItems.length === 0) return null;

  const representative = structuredClone(mergedItems[mergedItems.length - 1]);
  const triggerMessages: TurnItem['triggerMessages'] = [];
  let deferredCount = 0;
  const mailboxEventIds: string[] = [];
  const mailboxSequences: number[] = [];
  for (const entry of mergedItems) {
    if (entry.triggerMessages.length > 0) {
      triggerMessages.push(...structuredClone(entry.triggerMessages));
    } else if (entry.message !== null) {
      triggerMessages.push({
        user_id: entry.message.userId,
        nickname: String(entry.message.sender?.nickname || entry.message.sender?.card || entry.message.userId),
        text: entry.cleaned || entry.message.text,
        raw_message: entry.message.rawMessage,
        message_id: entry.message.messageId,
        timestamp: entry.message.timestamp,
      });
    }
    deferredCount += Math.max(1, entry.deferredCount);
    for (const eventId of entry.mailboxEventIds ?? []) {
      if (String(eventId).trim()) mailboxEventIds.push(String(eventId));
    }
    for (const sequence of entry.mailboxSequences ?? []) {
      if (Number.isInteger(sequence)) mailboxSequences.push(sequence);
    }
  }

  representative.scopeKey = scopeKey;
  representative.deferredCount = deferredCount;
  representative.triggerMessages = dedupeTriggerEntries(triggerMessages);
  representative.batchItems = mergedItems;
  representative.mailboxEventIds = mailboxEventIds;
  representative.mailboxSequences = mailboxSequences;
  if (historySeed !== null) representative.historySeed = historySeed;
  if (turnMetadata !== null) representative.turnMetadata = turnMetadata;
  representative.batchMetadata = {
    event_count: mergedItems.length,
    event_ids: mailboxEventIds,
    sequences: mailboxSequences,
    first_sequence: mailboxSequences[0] ?? null,
    last_sequence: mailboxSequences[mailboxSequences.length - 1] ?? null,
  };
  return representative;
}

function dedupeTriggerEntries(entries: TurnItem['triggerMessages']): TurnItem['triggerMessages'] {
  const seen = new Set<string>();
  const result: TurnItem['triggerMessages'] = [];
  for (const entry of entries) {
    const key = [
      String(entry.message_id ?? ''),
      String(entry.message_ref ?? ''),
      String(entry.raw_message ?? ''),
      String(entry.text ?? ''),
      String(entry.timestamp ?? ''),
      String(entry.source_label ?? ''),
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(entry);
  }
  return result;
}
