/**
 * Scope send ledger (legacy core/ai_runtime.py ScopeSendLedger): structured
 * idempotency for send_message. Once a visible payload has really gone out,
 * the exact same send_message in the same scope is refused for a TTL window —
 * otherwise post-send reruns, re-delivered events or duplicate triggers turn
 * the model's repeated decision into a real double send.
 *
 * Keyed per scope (not per call chain) so it survives across turns; the TTL
 * keeps normal repetition possible later (复读 shouldn't be banned forever).
 */

import { createHash } from 'node:crypto';

const DEFAULT_TTL_MS = 5 * 60 * 1000;

/**
 * Action key for one send_message: reply target + normalized visible lines.
 * Empty string = nothing to dedupe (empty payload).
 */
export function sendActionKey(lines: string[], replyToMessageId: unknown): string {
  const canonical = lines.map((line) => line.trim()).filter((line) => line !== '').join('\n');
  if (canonical === '') return '';
  const replyMarker = replyToMessageId === null || replyToMessageId === undefined || replyToMessageId === '' ? '' : String(replyToMessageId);
  return createHash('sha1').update(`${replyMarker}\n${canonical}`, 'utf8').digest('hex');
}

export class ScopeSendLedger {
  /** scopeKey → (actionKey → expiresAt). */
  private readonly scopes = new Map<string, Map<string, number>>();

  constructor(
    private readonly ttlMs = DEFAULT_TTL_MS,
    private readonly now: () => number = Date.now,
  ) {}

  has(scopeKey: string, key: string): boolean {
    const entries = this.purge(scopeKey);
    return entries !== undefined && entries.has(key);
  }

  add(scopeKey: string, key: string): void {
    let entries = this.purge(scopeKey);
    if (entries === undefined) {
      entries = new Map();
      this.scopes.set(scopeKey, entries);
    }
    entries.set(key, this.now() + this.ttlMs);
  }

  discard(scopeKey: string, key: string): void {
    this.scopes.get(scopeKey)?.delete(key);
  }

  private purge(scopeKey: string): Map<string, number> | undefined {
    const entries = this.scopes.get(scopeKey);
    if (entries === undefined) return undefined;
    const now = this.now();
    for (const [key, expiresAt] of entries) {
      if (expiresAt <= now) entries.delete(key);
    }
    if (entries.size === 0) {
      this.scopes.delete(scopeKey);
      return undefined;
    }
    return entries;
  }
}
