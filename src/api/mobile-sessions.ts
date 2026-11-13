/** Dedicated user-facing sessions. Store only hashes; never reuse panel authority. */
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { HistoryEntry } from '../chat/types.js';

interface Session { id: string; tokenHash: string; createdAt: number }
export class MobileSessions {
  private readonly sessions = new Map<string, Session>();
  constructor(private readonly path: string) {
    try {
      const data: unknown = JSON.parse(readFileSync(path, 'utf8'));
      if (!Array.isArray(data)) throw new Error('Invalid mobile session store');
      for (const row of data as Session[]) {
        if (!/^[a-f0-9-]{36}$/.test(row.id) || !/^[a-f0-9]{64}$/.test(row.tokenHash)) {
          throw new Error('Invalid mobile session record');
        }
        this.sessions.set(row.tokenHash, row);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
  }
  create(): { sessionId: string; token: string } {
    if (this.sessions.size >= 200) throw new Error('Mobile session limit reached');
    const token = `dlapp_${randomBytes(32).toString('base64url')}`;
    const tokenHash = this.hash(token);
    const row = { id: randomUUID(), tokenHash, createdAt: Date.now() };
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const next = [...this.sessions.values(), row];
    writeFileSync(`${this.path}.tmp`, JSON.stringify(next), { mode: 0o600 });
    renameSync(`${this.path}.tmp`, this.path);
    this.sessions.set(tokenHash, row);
    return { sessionId: row.id, token };
  }
  resolve(token: string): string | null {
    if (!/^dlapp_[A-Za-z0-9_-]{43}$/.test(token)) return null;
    return this.sessions.get(this.hash(token))?.id ?? null;
  }
  private hash(token: string): string { return createHash('sha256').update(token).digest('hex'); }
}

/** Never expose reports, master relay-backs, tool state or system messages. */
export function mobileVisibleHistory(entries: HistoryEntry[]) {
  return entries.filter((entry) => entry.source_kind === 'mobile_app' || entry.raw_source === 'app_ai')
    .slice(-100).map((entry) => ({
      role: entry.source_kind === 'mobile_app' ? 'me' : 'ai',
      name: entry.source_kind === 'mobile_app' ? '我' : 'AI',
      text: entry.text,
      ts: entry.timestamp,
    }));
}
