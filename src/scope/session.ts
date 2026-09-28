/**
 * Character session — single-scope ownership boundary. Ported from legacy
 * core/character_session.py. Owns no model behavior; centralizes the active
 * bit, the scope's mailbox partition and message-before-task promotion.
 */

import type { EventBatch } from './mailbox.js';

export interface SessionSnapshot {
  scopeType: string;
  scopeId: string;
  scopeKey: string;
  active: boolean;
  pendingEventCount: number;
  pendingTaskCount: number;
  retired: boolean;
  busy: boolean;
}

export interface PendingTask {
  kind: 'task';
  taskId: string;
  messageEpoch: number | null;
  turnItem?: unknown;
  [key: string]: unknown;
}

export class CharacterSession {
  readonly scopeType: string;
  readonly scopeId: string;
  private readonly mailbox: { pendingCount(scopeKey: string): number };
  private readonly drain: (scopeKey: string) => EventBatch | null;
  private active = false;
  private retired = false;
  private pendingTasks: PendingTask[] = [];

  constructor(
    scopeType: string,
    scopeId: string,
    mailbox: {
      pendingCount(scopeKey: string): number;
      drainScope(scopeKey: string): EventBatch | null;
    },
  ) {
    this.scopeType = scopeType;
    this.scopeId = scopeId;
    this.mailbox = mailbox;
    this.drain = mailbox.drainScope.bind(mailbox);
  }

  get scopeKey(): string {
    return `${this.scopeType}:${this.scopeId}`;
  }

  snapshot(): SessionSnapshot {
    const pendingEventCount = this.mailbox.pendingCount(this.scopeKey);
    return {
      scopeType: this.scopeType,
      scopeId: this.scopeId,
      scopeKey: this.scopeKey,
      active: this.active,
      pendingEventCount,
      pendingTaskCount: this.pendingTasks.length,
      retired: this.retired,
      busy: this.active || pendingEventCount > 0,
    };
  }

  isActive(): boolean {
    return this.active;
  }

  isBusy(): boolean {
    return this.active || this.mailbox.pendingCount(this.scopeKey) > 0;
  }

  activate(): boolean {
    if (this.retired) throw new Error('character session is retired');
    if (this.active) return false;
    this.active = true;
    return true;
  }

  deactivate(): void {
    this.active = false;
  }

  appendTask(task: PendingTask): number {
    if (this.retired) throw new Error('character session is retired');
    const countBefore = this.pendingTasks.length;
    this.pendingTasks.push(task);
    return countBefore;
  }

  pendingTaskCount(): number {
    return this.pendingTasks.length;
  }

  /** Tasks only promote when the mailbox partition is empty (messages win). */
  promoteTaskIfMailboxEmpty(): PendingTask | null {
    if (this.mailbox.pendingCount(this.scopeKey) > 0) return null;
    return this.pendingTasks.shift() ?? null;
  }

  clearPendingTasks(): void {
    if (this.retired) throw new Error('character session is retired');
    this.pendingTasks = [];
  }

  retireIfIdle(): boolean {
    if (this.active || this.pendingTasks.length > 0 || this.mailbox.pendingCount(this.scopeKey) > 0) {
      return false;
    }
    this.retired = true;
    return true;
  }

  isRetired(): boolean {
    return this.retired;
  }

  clearRuntimeState(): void {
    this.drain(this.scopeKey);
    this.active = false;
    this.pendingTasks = [];
  }
}

export interface MailboxView {
  pendingCount(scopeKey: string): number;
  drainScope(scopeKey: string): EventBatch | null;
}

export class CharacterSessionRegistry {
  readonly mailbox: MailboxView;
  private readonly sessions = new Map<string, CharacterSession>();

  constructor(mailbox: MailboxView) {
    this.mailbox = mailbox;
  }

  getOrCreate(scopeType: string, scopeId: string): CharacterSession {
    const scopeKey = `${scopeType}:${scopeId}`;
    let session = this.sessions.get(scopeKey);
    if (!session) {
      session = new CharacterSession(scopeType, scopeId, this.mailbox);
      this.sessions.set(scopeKey, session);
    }
    return session;
  }

  get(scopeKey: string): CharacterSession | undefined {
    return this.sessions.get(scopeKey);
  }

  listScopeKeys(): string[] {
    return [...this.sessions.keys()].sort();
  }

  snapshots(): SessionSnapshot[] {
    return this.listScopeKeys().map((key) => this.sessions.get(key)!.snapshot());
  }

  isActive(scopeKey: string): boolean {
    return this.sessions.get(scopeKey)?.isActive() ?? false;
  }

  appendPendingTask(scopeKey: string, task: PendingTask): number {
    const [scopeType, scopeId] = splitScopeKey(scopeKey);
    return this.getOrCreate(scopeType, scopeId).appendTask(task);
  }

  promotePendingTaskIfMailboxEmpty(scopeKey: string): PendingTask | null {
    return this.sessions.get(scopeKey)?.promoteTaskIfMailboxEmpty() ?? null;
  }

  clearPendingTasks(scopeKey?: string): void {
    if (scopeKey !== undefined) {
      this.sessions.get(scopeKey)?.clearPendingTasks();
      return;
    }
    for (const key of this.listScopeKeys()) this.clearPendingTasks(key);
  }

  clearActive(): void {
    for (const session of this.sessions.values()) session.deactivate();
  }

  clearRuntimeState(): void {
    for (const session of this.sessions.values()) session.clearRuntimeState();
    this.sessions.clear();
  }
}

export function splitScopeKey(scopeKey: string): [string, string] {
  const separatorIndex = scopeKey.indexOf(':');
  if (separatorIndex <= 0 || separatorIndex === scopeKey.length - 1) {
    throw new Error(`scopeKey must be <scope_type>:<scope_id>, got: ${scopeKey}`);
  }
  return [scopeKey.slice(0, separatorIndex), scopeKey.slice(separatorIndex + 1)];
}
