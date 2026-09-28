/**
 * Append-only message archive — the openclaw-style "raw history is sacred"
 * layer. Every message ever seen (inbound, outbound, internal reports) is
 * appended to a per-scope JSONL file and never rewritten, so precise search
 * can always reach the original wording even after the active 500-message
 * window and diary summaries roll over.
 *
 * Search is substring-precise (no fuzzy), multi-keyword AND, with quoted
 * exact phrases, plus scope/speaker/time filters — the bot-side equivalent of
 * openclaw's memory_search over its session files.
 */

import { appendFile, mkdir, readdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type { HistoryEntry } from '../chat/types.js';

export interface ArchiveHit {
  scopeKey: string;
  lineIndex: number;
  entry: HistoryEntry;
  /** Formatted display line. */
  line: string;
  contextBefore: string[];
  contextAfter: string[];
}

export interface SearchQuery {
  /** Free-form: whitespace splits into AND keywords; "quoted" = exact phrase. */
  query: string;
  /** Restrict to these scope keys; empty = all scopes. */
  scopeKeys?: string[];
  userId?: number;
  /** Epoch seconds lower bound (inclusive). */
  from?: number;
  /** Epoch seconds upper bound (inclusive). */
  to?: number;
  limit?: number;
}

export interface ParsedQuery {
  /** Every term must appear (case-insensitive substring). */
  terms: string[];
}

export function parseSearchQuery(query: string): ParsedQuery {
  const phrases: string[] = [];
  let rest = String(query ?? '');
  rest = rest.replace(/"([^"]+)"/g, (_match, phrase: string) => {
    const trimmed = String(phrase).trim();
    if (trimmed) phrases.push(trimmed);
    return ' ';
  });
  const words = rest
    .split(/[\s,，、;；]+/)
    .map((word) => word.trim())
    .filter((word) => word.length > 0);
  const terms = [...phrases, ...words].map((term) => term.toLowerCase());
  return { terms: [...new Set(terms)] };
}

export function entryMatches(entry: HistoryEntry, terms: string[]): boolean {
  if (terms.length === 0) return false;
  const haystack = `${String(entry.text ?? '')}\n${String(entry.nickname ?? '')}\n${String(entry.raw_message ?? '')}`.toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

export function formatArchiveLine(scopeKey: string, entry: HistoryEntry): string {
  const date = new Date(Number(entry.timestamp ?? 0) * 1000);
  const pad = (value: number) => String(value).padStart(2, '0');
  const clock = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const ref = String(entry.message_ref ?? '').trim();
  const refSuffix = ref ? ` [#${ref}]` : '';
  const label = String(entry.source_label ?? '').trim();
  const labelPrefix = label ? `[${label}] ` : '';
  return `[${scopeKey}] ${clock} ${labelPrefix}${String(entry.nickname ?? entry.user_id ?? '未知')}(${entry.user_id}): ${String(entry.text ?? '').trim()}${refSuffix}`;
}

export class MessageArchive {
  private readonly archiveDir: string;
  /** line cache per scope file: { lines, loaded } — refreshed on demand. */
  private readonly cache = new Map<string, HistoryEntry[]>();
  /** Per-file append chain: JSONL line order must match call order. */
  private readonly appendQueues = new Map<string, Promise<void>>();

  constructor(archiveDir: string) {
    this.archiveDir = archiveDir;
  }

  /** Fire-and-forget append; never throws into the chat path. */
  append(scopeType: string, scopeId: string | number, entry: HistoryEntry): void {
    const scopeKey = `${scopeType}:${scopeId}`;
    const path = this.scopePath(scopeType, String(scopeId));
    const write = (this.appendQueues.get(path) ?? Promise.resolve())
      .then(async () => {
        await mkdir(this.archiveDir, { recursive: true });
        await appendFile(path, JSON.stringify(entry) + '\n', 'utf-8');
        // Best-effort cache invalidation so searches see fresh appends.
        this.cache.delete(scopeKey);
      })
      .catch((error) => {
        console.error('[archive] append failed:', error);
      });
    this.appendQueues.set(path, write);
  }

  /** Await every in-flight append (tests, shutdown). */
  async flush(): Promise<void> {
    await Promise.allSettled([...this.appendQueues.values()]);
  }

  async search(query: SearchQuery): Promise<ArchiveHit[]> {
    const { terms } = parseSearchQuery(query.query);
    if (terms.length === 0) return [];
    const limit = Math.max(1, Math.min(50, query.limit ?? 12));
    const scopeFiles = await this.resolveScopeFiles(query.scopeKeys ?? []);
    const hits: ArchiveHit[] = [];
    for (const { scopeKey, path } of scopeFiles) {
      const entries = await this.loadLines(scopeKey, path);
      for (let index = 0; index < entries.length; index += 1) {
        const entry = entries[index];
        if (query.userId !== undefined && Number(entry.user_id) !== query.userId) continue;
        const timestamp = Number(entry.timestamp ?? 0);
        if (query.from !== undefined && timestamp < query.from) continue;
        if (query.to !== undefined && timestamp > query.to) continue;
        if (!entryMatches(entry, terms)) continue;
        hits.push({
          scopeKey,
          lineIndex: index,
          entry,
          line: formatArchiveLine(scopeKey, entry),
          contextBefore: index > 0 ? [formatArchiveLine(scopeKey, entries[index - 1])] : [],
          contextAfter: index < entries.length - 1 ? [formatArchiveLine(scopeKey, entries[index + 1])] : [],
        });
        if (hits.length >= limit) return hits;
      }
    }
    return hits;
  }

  /** Drop cached lines (used after external writes / in tests). */
  invalidate(scopeKey?: string): void {
    if (scopeKey === undefined) this.cache.clear();
    else this.cache.delete(scopeKey);
  }

  private scopePath(scopeType: string, scopeId: string): string {
    return join(this.archiveDir, `${scopeType}_${String(scopeId)}.jsonl`);
  }

  private async resolveScopeFiles(scopeKeys: string[]): Promise<{ scopeKey: string; path: string }[]> {
    if (scopeKeys.length > 0) {
      return scopeKeys.map((scopeKey) => {
        const separator = scopeKey.indexOf(':');
        return {
          scopeKey,
          path: this.scopePath(scopeKey.slice(0, separator), scopeKey.slice(separator + 1)),
        };
      });
    }
    if (!existsSync(this.archiveDir)) return [];
    const names = (await readdir(this.archiveDir)).filter((name) => name.endsWith('.jsonl')).sort();
    return names.map((name) => ({
      scopeKey: name.replace(/\.jsonl$/, '').replace('_', ':'),
      path: join(this.archiveDir, name),
    }));
  }

  private async loadLines(scopeKey: string, path: string): Promise<HistoryEntry[]> {
    const cached = this.cache.get(scopeKey);
    if (cached) return cached;
    let entries: HistoryEntry[] = [];
    try {
      const raw = await readFile(path, 'utf-8');
      entries = raw
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line.length > 0)
        .map((line) => JSON.parse(line) as HistoryEntry);
    } catch {
      entries = [];
    }
    this.cache.set(scopeKey, entries);
    return entries;
  }
}
