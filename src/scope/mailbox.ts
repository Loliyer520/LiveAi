/**
 * Thread-safe (single-process async), per-scope FIFO queues.
 *
 * Ported from legacy core/event_mailbox.py, keeping the two invariants that
 * made the legacy chat preemption correct:
 *  - pop/drain refuses to bypass a head entry whose retry backoff has not
 *    elapsed (FIFO order survives a failed turn);
 *  - requeueFront re-admits a failed event at the head so the retry stays
 *    ahead of anything that arrived while it was being processed.
 */

import { EventEnvelope } from './envelope.js';

export interface MailboxEntry<T = unknown> {
  envelope: EventEnvelope;
  transient: T | null;
  attempt: number;
  notBefore: number; // epoch seconds; 0 = immediately ready
}

export interface EventBatch<T = unknown> {
  scopeKey: string;
  events: EventEnvelope[];
  transients: (T | null)[];
}

export class InMemoryEventMailbox {
  private readonly scopeQueues = new Map<string, MailboxEntry[]>();
  private nextSequence = 1;

  append<T>(event: EventEnvelope, transient: T | null = null): EventEnvelope {
    return this.appendEntry(event, transient).envelope;
  }

  appendEntry<T>(event: EventEnvelope, transient: T | null = null): MailboxEntry<T> {
    const queued = event.withSequence(this.nextSequence);
    const entry: MailboxEntry<T> = { envelope: queued, transient, attempt: 0, notBefore: 0 };
    this.queueFor(queued.scopeKey).push(entry);
    this.nextSequence += 1;
    return entry;
  }

  /** Validate and append the complete list as one atomic commit. */
  appendMany(events: EventEnvelope[]): EventEnvelope[] {
    const queued = events.map((event, index) =>
      event.withSequence(this.nextSequence + index),
    );
    for (const envelope of queued) {
      this.queueFor(envelope.scopeKey).push({ envelope, transient: null, attempt: 0, notBefore: 0 });
    }
    this.nextSequence += queued.length;
    return queued;
  }

  /** Atomically remove one FIFO entry; a not-ready head parks the whole scope. */
  popScopeEntry(scopeKey: string): MailboxEntry | null {
    const queue = this.scopeQueues.get(scopeKey);
    if (!queue || queue.length === 0) {
      this.scopeQueues.delete(scopeKey);
      return null;
    }
    if (!isReady(queue[0])) return null;
    const entry = queue.shift()!;
    if (queue.length === 0) this.scopeQueues.delete(scopeKey);
    return entry;
  }

  /** Re-admit a failed event at the head of its scope, deferred by `delayMs`. */
  requeueFront(
    event: EventEnvelope,
    transient: unknown = null,
    attempt = 1,
    delayMs = 0,
  ): MailboxEntry {
    const queued = event.withSequence(this.nextSequence);
    this.nextSequence += 1;
    const entry: MailboxEntry = {
      envelope: queued,
      transient,
      attempt: Math.max(1, Math.trunc(attempt)),
      notBefore: delayMs > 0 ? Date.now() / 1000 + delayMs / 1000 : 0,
    };
    this.queueFor(queued.scopeKey).unshift(entry);
    return entry;
  }

  /** Drain the whole scope into one ordered batch (retry-not-ready parks it). */
  drainScope(scopeKey: string): EventBatch | null {
    const queue = this.scopeQueues.get(scopeKey);
    if (!queue || queue.length === 0) {
      this.scopeQueues.delete(scopeKey);
      return null;
    }
    if (!isReady(queue[0])) return null;
    this.scopeQueues.delete(scopeKey);
    return {
      scopeKey,
      events: queue.map((entry) => entry.envelope),
      transients: queue.map((entry) => entry.transient),
    };
  }

  pendingCount(scopeKey?: string): number {
    if (scopeKey === undefined) {
      let total = 0;
      for (const queue of this.scopeQueues.values()) total += queue.length;
      return total;
    }
    return this.scopeQueues.get(scopeKey)?.length ?? 0;
  }

  /** Scope keys ordered by head sequence (oldest first). */
  pendingScopes(): string[] {
    const scopes = [...this.scopeQueues.entries()]
      .filter(([, queue]) => queue.length > 0)
      .map(([key, queue]) => ({ seq: queue[0].envelope.mailboxSequence ?? 0, key }));
    scopes.sort((a, b) => a.seq - b.seq);
    return scopes.map((item) => item.key);
  }

  clear(): void {
    this.scopeQueues.clear();
  }

  isEmpty(): boolean {
    return this.pendingCount() === 0;
  }

  private queueFor(scopeKey: string): MailboxEntry[] {
    let queue = this.scopeQueues.get(scopeKey);
    if (!queue) {
      queue = [];
      this.scopeQueues.set(scopeKey, queue);
    }
    return queue;
  }
}

function isReady(entry: MailboxEntry): boolean {
  if (entry.notBefore <= 0) return true;
  return Date.now() / 1000 >= entry.notBefore;
}
