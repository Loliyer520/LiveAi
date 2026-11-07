/**
 * Transport-neutral, JSON-serializable event for one AI scope.
 *
 * Ported from legacy core/event_envelope.py. `mailboxSequence` is assigned by
 * the in-memory mailbox; it is runtime ordering metadata, not a cursor.
 */

export const EVENT_TYPES = [
  'message',
  'alarm',
  'recurring_task',
  'agent_report',
  'main_ai_message',
  'system',
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export interface EnvelopeInit {
  eventType: EventType | string;
  scopeType: string;
  scopeId: string;
  payload: Record<string, unknown>;
  source?: string;
  eventId?: string;
  occurredAt?: number;
  mailboxSequence?: number | null;
}

export class EventEnvelope {
  readonly eventType: EventType;
  readonly scopeType: string;
  readonly scopeId: string;
  readonly payload: Record<string, unknown>;
  readonly source: string;
  readonly eventId: string;
  readonly occurredAt: number;
  readonly mailboxSequence: number | null;

  constructor(init: EnvelopeInit) {
    const eventType = EventEnvelope.coerceEventType(init.eventType);
    const scopeType = String(init.scopeType ?? '').trim();
    const scopeId = String(init.scopeId ?? '').trim();
    const source = String(init.source ?? '').trim() || 'unknown';
    const eventId = String(init.eventId ?? '').trim() || randomId();
    if (!scopeType || scopeType.includes(':')) {
      throw new Error('scopeType must be non-empty and must not contain colon');
    }
    if (!scopeId) throw new Error('scopeId must be non-empty');
    const sequence = init.mailboxSequence ?? null;
    if (sequence !== null && (!Number.isInteger(sequence) || sequence < 1)) {
      throw new Error('mailboxSequence must be a positive integer or null');
    }
    this.eventType = eventType;
    this.scopeType = scopeType;
    this.scopeId = scopeId;
    this.payload = structuredClone(init.payload ?? {});
    this.source = source;
    this.eventId = eventId;
    this.occurredAt = Number(init.occurredAt ?? Date.now() / 1000);
    this.mailboxSequence = sequence;
  }

  get scopeKey(): string {
    return `${this.scopeType}:${this.scopeId}`;
  }

  /** Immutable replace: same event, new mailbox sequence (frozen-dataclass `replace`). */
  withSequence(sequence: number): EventEnvelope {
    return new EventEnvelope({
      eventType: this.eventType,
      scopeType: this.scopeType,
      scopeId: this.scopeId,
      payload: this.payload,
      source: this.source,
      eventId: this.eventId,
      occurredAt: this.occurredAt,
      mailboxSequence: sequence,
    });
  }

  toDict(): Record<string, unknown> {
    return {
      event_type: this.eventType,
      scope_type: this.scopeType,
      scope_id: this.scopeId,
      payload: this.payload,
      source: this.source,
      event_id: this.eventId,
      occurred_at: this.occurredAt,
      mailbox_sequence: this.mailboxSequence,
    };
  }

  static fromDict(data: Record<string, unknown>): EventEnvelope {
    return new EventEnvelope({
      eventType: String(data.event_type),
      scopeType: String(data.scope_type),
      scopeId: String(data.scope_id),
      payload: (data.payload ?? {}) as Record<string, unknown>,
      source: String(data.source ?? ''),
      eventId: String(data.event_id ?? ''),
      occurredAt: Number(data.occurred_at ?? 0),
      mailboxSequence: (data.mailbox_sequence as number | null) ?? null,
    });
  }

  private static coerceEventType(value: EventType | string): EventType {
    const name = String(value);
    if ((EVENT_TYPES as readonly string[]).includes(name)) return name as EventType;
    throw new Error(`unsupported eventType: ${String(value)}`);
  }
}

function randomId(): string {
  return globalThis.crypto?.randomUUID?.().replace(/-/g, '') ?? `${Date.now()}${Math.random()}`;
}
