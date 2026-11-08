/**
 * Idempotency ledger: tracks fingerprints of recently sent messages to prevent
 * duplicate sends during crash recovery or retries. Uses SHA1 hash of
 * (scope, content) with a 5-minute TTL.
 */

import { createHash } from 'node:crypto';

interface IdempotencyRecord {
  fingerprint: string;
  expiresAt: number;
}

const IDEMPOTENCY_TTL_MS = 5 * 60 * 1000; // 5 minutes

export class IdempotencyLedger {
  private readonly ledger = new Map<string, IdempotencyRecord>();
  private gcTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    // Run GC every minute to prune expired entries
    this.gcTimer = setInterval(() => this.gc(), 60_000);
  }

  /**
   * Computes a fingerprint for a send operation. Includes target (chatType + id),
   * content (text/file), and a type discriminator.
   */
  private computeFingerprint(
    chatType: 'group' | 'private',
    targetId: number,
    type: 'text' | 'image' | 'sticker' | 'video',
    payload: string,
  ): string {
    const raw = `${chatType}:${targetId}:${type}:${payload}`;
    return createHash('sha1').update(raw, 'utf8').digest('hex');
  }

  /**
   * Checks if a message is a duplicate (was sent recently). Returns true if
   * this exact message was sent within the TTL window and should be skipped.
   */
  isDuplicate(chatType: 'group' | 'private', targetId: number, type: 'text' | 'image' | 'sticker' | 'video', payload: string): boolean {
    const fingerprint = this.computeFingerprint(chatType, targetId, type, payload);
    const record = this.ledger.get(fingerprint);
    if (record === undefined) return false;
    if (Date.now() > record.expiresAt) {
      this.ledger.delete(fingerprint);
      return false;
    }
    return true;
  }

  /**
   * Records a send operation so future duplicates can be detected.
   */
  record(chatType: 'group' | 'private', targetId: number, type: 'text' | 'image' | 'sticker' | 'video', payload: string): void {
    const fingerprint = this.computeFingerprint(chatType, targetId, type, payload);
    this.ledger.set(fingerprint, { fingerprint, expiresAt: Date.now() + IDEMPOTENCY_TTL_MS });
  }

  /**
   * Prunes expired entries.
   */
  private gc(): void {
    const now = Date.now();
    for (const [key, record] of this.ledger.entries()) {
      if (now > record.expiresAt) {
        this.ledger.delete(key);
      }
    }
  }

  /**
   * Stops the GC timer.
   */
  destroy(): void {
    if (this.gcTimer !== null) {
      clearInterval(this.gcTimer);
      this.gcTimer = null;
    }
  }
}
