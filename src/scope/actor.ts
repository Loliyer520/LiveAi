/**
 * Scope actor dispatcher — one single-consumer async actor per scope key.
 * Ported from legacy core/scope_actor_dispatcher.py + scope_actor_registry.py.
 *
 * This is what guarantees the README's "同一账户、同一会话内的消息严格按顺序
 * 处理，不同会话之间可以并发" contract: each scope runs its own loop, popping
 * mailbox entries one at a time; new arrivals only wake the loop, they never
 * run concurrently inside the same scope.
 */

import { InMemoryEventMailbox, type MailboxEntry } from './mailbox.js';
import { EventEnvelope } from './envelope.js';
import { CharacterSessionRegistry } from './session.js';
import type { TurnItem } from './turn.js';

export type ItemConsumer = (scopeKey: string, item: TurnItem) => Promise<void>;
export type IdleCallback = (scopeKey: string) => void;
export type StalePredicate = (item: TurnItem) => boolean;

interface ActorRecord {
  wakeup: PromiseWithResolvers<void>;
  closed: boolean;
  task: Promise<void>;
}

export class ScopeActorDispatcher {
  readonly mailbox: InMemoryEventMailbox;
  readonly sessions: CharacterSessionRegistry;
  private readonly consume: ItemConsumer;
  private readonly isStale: StalePredicate;
  private readonly onIdle: IdleCallback | null;
  private readonly actors = new Map<string, ActorRecord>();

  constructor(options: {
    mailbox: InMemoryEventMailbox;
    sessions: CharacterSessionRegistry;
    consume: ItemConsumer;
    isStale?: StalePredicate;
    onIdle?: IdleCallback | null;
  }) {
    this.mailbox = options.mailbox;
    this.sessions = options.sessions;
    this.consume = options.consume;
    this.isStale = options.isStale ?? (() => false);
    this.onIdle = options.onIdle ?? null;
  }

  submitEvent(envelope: EventEnvelope, transient: TurnItem): void {
    this.mailbox.append(envelope, transient);
    this.ensureActor(envelope.scopeKey);
    this.wake(envelope.scopeKey);
  }

  submitTask(scopeKey: string, item: TurnItem): void {
    this.sessions.appendPendingTask(scopeKey, {
      kind: 'task',
      taskId: String(item.taskId ?? ''),
      messageEpoch: item.messageEpoch,
      turnItem: item,
    });
    this.ensureActor(scopeKey);
    this.wake(scopeKey);
  }

  wake(scopeKey: string): void {
    const actor = this.actors.get(scopeKey);
    if (actor && !actor.closed) actor.wakeup.resolve();
  }

  activeActorCount(): number {
    return this.actors.size;
  }

  actorKeys(): string[] {
    return [...this.actors.keys()];
  }

  clearRuntimeState(): void {
    this.mailbox.clear();
    this.sessions.clearPendingTasks();
    this.sessions.clearActive();
  }

  async close(): Promise<void> {
    const actors = [...this.actors.values()];
    for (const actor of actors) {
      actor.closed = true;
      actor.wakeup.resolve();
    }
    await Promise.allSettled(actors.map((actor) => actor.task));
    this.actors.clear();
    this.clearRuntimeState();
  }

  private ensureActor(scopeKey: string): void {
    if (this.actors.has(scopeKey)) return;
    const wakeup = Promise.withResolvers<void>();
    const record: ActorRecord = {
      wakeup,
      closed: false,
      task: Promise.resolve(),
    };
    record.task = this.runActor(scopeKey, record);
    this.actors.set(scopeKey, record);
    // Self-cleanup: drop the actor record once its loop exits while idle.
    void record.task.then(() => {
      if (this.actors.get(scopeKey) === record) this.actors.delete(scopeKey);
    });
  }

  private async runActor(scopeKey: string, record: ActorRecord): Promise<void> {
    const [scopeType, scopeId] = splitKey(scopeKey);
    const session = this.sessions.getOrCreate(scopeType, scopeId);
    try {
      for (;;) {
        await record.wakeup.promise;
        if (record.closed) return;
        record.wakeup = Promise.withResolvers<void>();
        for (;;) {
          const item = this.nextItem(scopeKey);
          if (item === null) {
            session.deactivate();
            this.onIdle?.(scopeKey);
            break;
          }
          if (this.isStale(item)) continue;
          if (!session.isActive()) session.activate();
          await this.consume(scopeKey, item);
          if (record.closed) return;
        }
      }
    } finally {
      session.deactivate();
    }
  }

  private nextItem(scopeKey: string): TurnItem | null {
    const entry: MailboxEntry | null = this.mailbox.popScopeEntry(scopeKey);
    if (entry !== null) {
      const item = entry.transient as TurnItem;
      if (item === null || typeof item !== 'object') {
        throw new Error(`mailbox transient must be a TurnItem: ${scopeKey}`);
      }
      item.mailboxEventIds = [entry.envelope.eventId];
      item.mailboxSequences = [entry.envelope.mailboxSequence ?? 0];
      return item;
    }
    const task = this.sessions.promotePendingTaskIfMailboxEmpty(scopeKey);
    if (task === null) return null;
    return task.turnItem as TurnItem;
  }
}

function splitKey(scopeKey: string): [string, string] {
  const index = scopeKey.indexOf(':');
  return [scopeKey.slice(0, index), scopeKey.slice(index + 1)];
}
