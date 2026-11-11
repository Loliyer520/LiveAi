/**
 * Append-only message archive — the openclaw-style "raw history is sacred"
 * layer. Every message ever seen (inbound, outbound, internal reports) is
 * appended to a per-scope JSONL file and never rewritten, so precise search
 * can always reach the original wording even after the active 500-message
 * window and diary summaries roll over.
 *
 * Search uses a two-pass strategy:
 *
 *  1. BM25 scoring: all entries are ranked by the standard Okapi-BM25
 *     formula (k1=1.5, b=0.75). A partial-word match scores ≥1 point but
 *     a full-word boundary match gets a 1.5× bonus, making precise queries
 *     surface better than noisy ones.
 *
 *  2. Quoted phrase filtering: each "quoted phrase" in the query must appear
 *     verbatim (substring) — they are removed from the scoring terms but
 *     applied as a hard filter afterwards.
 *
 * Together these replace the original AND-only filter, which silently returned
 * nothing when a term was slightly misspelled or appeared in a synonym.
 * No embedding or external model is needed — the archive is local JSONL.
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
  /** Free-form: whitespace splits into scored BM25 terms; "quoted" = exact phrase hard-filter. */
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
  /** Scored BM25 terms (case-insensitive). */
  terms: string[];
  /** Hard-filter phrases (must appear verbatim, case-insensitive). */
  phrases: string[];
}

export function parseSearchQuery(query: string): ParsedQuery {
  const phrases: string[] = [];
  let rest = String(query ?? '');
  rest = rest.replace(/"([^"]+)"/g, (_match, phrase: string) => {
    const trimmed = String(phrase).trim();
    if (trimmed) phrases.push(trimmed.toLowerCase());
    return ' ';
  });
  const words = rest
    .split(/[\s,，、;；]+/)
    .map((word) => word.trim())
    .filter((word) => word.length > 0);
  const terms = words.map((term) => term.toLowerCase());
  return { terms: [...new Set(terms)], phrases: [...new Set(phrases)] };
}

// ── BM25 helpers ─────────────────────────────────────────────────────────────

const BM25_K1 = 1.5;
const BM25_B = 0.75;
/** Approx chars-per-token for CJK-heavy text; only needed for avgdl estimate. */
const CHARS_PER_TOKEN = 1.5;

function tokenize(text: string): string[] {
  // For CJK text every character is its own token; Latin words split on non-
  // alphanumeric. This produces reasonable token overlap with the query terms.
  return text
    .toLowerCase()
    .split(/([a-z0-9]+)/g)
    .flatMap((chunk) => (chunk.trim() ? (/^[a-z0-9]+$/.test(chunk) ? [chunk] : [...chunk]) : []))
    .filter((token) => token.trim().length > 0);
}

function bm25Score(haystack: string[], terms: string[], avgdl: number): number {
  if (terms.length === 0) return 0;
  const dl = haystack.length;
  const tf = new Map<string, number>();
  for (const token of haystack) {
    tf.set(token, (tf.get(token) ?? 0) + 1);
  }
  const haystackText = haystack.join('');
  let score = 0;
  for (const term of terms) {
    const freq = tf.get(term) ?? 0;
    if (freq === 0) {
      // Partial substring bonus: term may span multiple "tokens" (e.g. 冰糖
      // is two chars that are both their own tokens, but the compound won't
      // appear in tf). Give a fractional score for substring presence.
      if (haystackText.includes(term)) score += 0.6;
      continue;
    }
    const tfScore = (freq * (BM25_K1 + 1)) / (freq + BM25_K1 * (1 - BM25_B + BM25_B * (dl / avgdl)));
    // Word-boundary bonus: term appears as a standalone token.
    score += tfScore * 1.5;
  }
  return score;
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
  /** 每 scope 追加代际：loadLines 读盘期间发生 append 时禁止回填旧缓存。 */
  private readonly versions = new Map<string, number>();
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
        this.versions.set(scopeKey, (this.versions.get(scopeKey) ?? 0) + 1);
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
    const { terms, phrases } = parseSearchQuery(query.query);
    if (terms.length === 0 && phrases.length === 0) return [];
    const limit = Math.max(1, Math.min(50, query.limit ?? 12));
    const scopeFiles = await this.resolveScopeFiles(query.scopeKeys ?? []);

    // Pass 1: load and pre-filter by metadata (scope/user/time); collect all
    // candidate entries across scopes for BM25 avgdl calculation.
    interface Candidate {
      scopeKey: string;
      entries: HistoryEntry[];
      index: number;
    }
    const candidates: Candidate[] = [];
    let totalEntries = 0;
    let totalLength = 0;
    for (const { scopeKey, path } of scopeFiles) {
      const entries = await this.loadLines(scopeKey, path);
      totalEntries += entries.length;
      for (let index = 0; index < entries.length; index += 1) {
        const entry = entries[index];
        totalLength += String(entry.text ?? '').length / CHARS_PER_TOKEN;
        if (query.userId !== undefined && Number(entry.user_id) !== query.userId) continue;
        const timestamp = Number(entry.timestamp ?? 0);
        if (query.from !== undefined && timestamp < query.from) continue;
        if (query.to !== undefined && timestamp > query.to) continue;
        candidates.push({ scopeKey, entries, index });
      }
    }
    if (candidates.length === 0) return [];

    // Compute over the full corpus without spreading a potentially huge
    // archive into function arguments (V8's argument limit causes RangeError).
    const avdl = totalEntries === 0 ? 100 : Math.max(1, totalLength / totalEntries);

    // Pass 2: score each candidate with BM25; hard-filter on phrases.
    interface Scored {
      scopeKey: string;
      entries: HistoryEntry[];
      index: number;
      score: number;
    }
    const scored: Scored[] = [];
    for (const { scopeKey, entries, index } of candidates) {
      const entry = entries[index];
      const text = `${String(entry.text ?? '')} ${String(entry.nickname ?? '')} ${String(entry.raw_message ?? '')}`;
      // Hard phrase filter first (cheap).
      if (phrases.length > 0) {
        const lower = text.toLowerCase();
        if (!phrases.every((phrase) => lower.includes(phrase))) continue;
      }
      const tokens = tokenize(text);
      // Merge phrase words into the scoring terms so phrases contribute score.
      const allTerms = [...terms, ...phrases.flatMap((p) => p.split(/\s+/))];
      const score = bm25Score(tokens, allTerms, avdl);
      if (score > 0) {
        scored.push({ scopeKey, entries, index, score });
      }
    }

    // Sort descending by BM25 score, then most-recent timestamp.
    scored.sort((a, b) => {
      const scoreDiff = b.score - a.score;
      if (Math.abs(scoreDiff) > 0.01) return scoreDiff;
      return Number(b.entries[b.index]?.timestamp ?? 0) - Number(a.entries[a.index]?.timestamp ?? 0);
    });

    // Pass 3: build ArchiveHit objects for the top-N results.
    return scored.slice(0, limit).map(({ scopeKey, entries, index }) => {
      const entry = entries[index];
      return {
        scopeKey,
        lineIndex: index,
        entry,
        line: formatArchiveLine(scopeKey, entry),
        contextBefore: index > 0 ? [formatArchiveLine(scopeKey, entries[index - 1])] : [],
        contextAfter: index < entries.length - 1 ? [formatArchiveLine(scopeKey, entries[index + 1])] : [],
      };
    });
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
    const versionBefore = this.versions.get(scopeKey) ?? 0;
    let entries: HistoryEntry[] = [];
    try {
      const raw = await readFile(path, 'utf-8');
      // A single truncated/corrupt line (crash mid-append) must not blank the
      // whole scope's searchable archive — skip bad lines, keep the rest.
      let skipped = 0;
      for (const line of raw.split('\n')) {
        const trimmed = line.trim();
        if (trimmed.length === 0) continue;
        try {
          entries.push(JSON.parse(trimmed) as HistoryEntry);
        } catch {
          skipped += 1;
        }
      }
      if (skipped > 0) console.warn(`[archive] skipped ${skipped} corrupt line(s) in ${path}`);
    } catch {
      entries = [];
    }
    // 读盘期间有 append 完成的话这份内容已经过时，不能进缓存。
    if ((this.versions.get(scopeKey) ?? 0) === versionBefore) {
      this.cache.set(scopeKey, entries);
    }
    return entries;
  }
}
