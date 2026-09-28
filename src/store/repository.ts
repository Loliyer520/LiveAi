/**
 * JSON persistence for per-scope chat state. Ported essentials of legacy
 * core/ai_repository.py:
 *
 *  - active message window (historyLimit, default 500)
 *  - diary segmentation: every 50 messages rolls into a pending diary segment;
 *    a summarizer task later condenses it into `diarySummaries`
 *  - AI notes (备注) — short-term scratch memory surfaced in the prompt
 *  - persistent 4-char short IDs (message_ref) so reply/recall survive restarts
 *  - agents (per-scope persona/impression/trigger config)
 *  - tasks (set_alarm / recurring)
 *
 * One JSON file per scope, atomic write via tmp+rename.
 */

import { mkdir, readdir, rename, writeFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import type { HistoryEntry } from '../chat/types.js';
import type { MessageArchive } from './archive.js';

const PROJECT_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

export interface DiarySegment {
  index: number;
  messages: HistoryEntry[];
}

export interface DiarySummary {
  index: number;
  text: string;
}

export interface AgentRecord {
  agentId: string;
  scopeType: string;
  scopeId: string;
  persona: string;
  impression: string; // DEPRECATED: 会话印象已并入关系网（relations 的 scope 节点），此字段仅用于一次性迁移
  triggerWords: string[];
  triggerRate: number;
  displayName: string;
  createdAt: number;
  /** Rolling overall summary of the whole scope (compaction primer). */
  overallSummary: string;
  overallSummaryBuiltAt: number;
  /** diarySummaries.length when the overall summary was last built. */
  overallSummaryAtSegments: number;
}

export interface TaskRecord {
  taskId: string;
  kind: string;
  agentId: string;
  originScope: string;
  payload: Record<string, unknown>;
  status: 'pending' | 'done' | 'failed';
  result: string;
  runAt: number;
  createdAt: number;
  cron?: string;
}

interface ScopeState {
  agent: AgentRecord;
  messages: HistoryEntry[];
  diarySegments: DiarySegment[];
  diarySummaries: DiarySummary[];
  pendingDiary: DiarySegment | null;
  notes: { text: string; timestamp: number }[];
  messageRefs: Record<string, string>; // message_id -> short ref
  refToMessageId: Record<string, string>; // short ref -> message_id
}

const REF_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const DIARY_SEGMENT_SIZE = 50;
/** Raw unsummarized segments kept for retry; overflow gets a fallback summary. */
const MAX_UNSUMMARIZED_SEGMENTS = 10;

export class ScopeRepository {
  private readonly cache = new Map<string, ScopeState>();

  constructor(
    private readonly stateDir = join(PROJECT_ROOT, 'data', 'state'),
    private readonly historyLimit = 500,
    private readonly archive: MessageArchive | null = null,
  ) {}

  // ── agents ────────────────────────────────────────────────────────────────

  getOrCreateAgent(scopeType: string, scopeId: string, persona = ''): AgentRecord {
    const state = this.loadScope(scopeType, scopeId);
    return state.agent;
  }

  updateAgent(scopeType: string, scopeId: string, patch: Partial<AgentRecord>): void {
    const state = this.loadScope(scopeType, scopeId);
    state.agent = { ...state.agent, ...patch };
    this.persist(state);
  }

  // ── messages ──────────────────────────────────────────────────────────────

  /**
   * Append one entry. Mirrors it into the append-only archive and rolls the
   * diary segment when full. Returns true when a segment became ready for
   * summarization.
   */
  appendMessage(scopeType: string, scopeId: string, entry: HistoryEntry): boolean {
    const state = this.loadScope(scopeType, scopeId);
    state.messages.push(entry);
    if (state.messages.length > this.historyLimit) {
      state.messages.splice(0, state.messages.length - this.historyLimit);
    }
    this.archive?.append(scopeType, scopeId, entry);
    let needsSummarize = false;
    if (state.pendingDiary === null) {
      state.pendingDiary = { index: this.nextSegmentIndex(state), messages: [] };
    }
    state.pendingDiary.messages.push(entry);
    if (state.pendingDiary.messages.length >= DIARY_SEGMENT_SIZE) {
      state.diarySegments.push(state.pendingDiary);
      state.pendingDiary = null;
      needsSummarize = true;
      this.enforceSegmentBound(state);
    }
    this.persist(state);
    return needsSummarize;
  }

  /** Segments filled but not yet summarized — the summarizer's work queue. */
  getUnsummarizedSegments(scopeType: string, scopeId: string): DiarySegment[] {
    return [...this.loadScope(scopeType, scopeId).diarySegments];
  }

  /** Scope keys ("type:id") of every persisted state file — recovery scans. */
  async listPersistedScopes(): Promise<string[]> {
    let names: string[] = [];
    try {
      names = (await readdir(this.stateDir)).filter((name) => name.endsWith('.json'));
    } catch {
      return [];
    }
    return names.map((name) => name.replace(/\.json$/, '').replace('_', ':'));
  }

  getMessages(scopeType: string, scopeId: string): HistoryEntry[] {
    return [...this.loadScope(scopeType, scopeId).messages];
  }

  getPendingDiary(scopeType: string, scopeId: string): DiarySegment | null {
    return this.loadScope(scopeType, scopeId).pendingDiary;
  }

  /** Store a segment summary and retire the raw segment it came from. */
  storeDiarySummary(scopeType: string, scopeId: string, index: number, text: string): void {
    const state = this.loadScope(scopeType, scopeId);
    state.diarySegments = state.diarySegments.filter((segment) => segment.index !== index);
    state.diarySummaries = state.diarySummaries.filter((item) => item.index !== index);
    state.diarySummaries.push({ index, text });
    state.diarySummaries.sort((a, b) => a.index - b.index);
    // Keep at most 20 summary segments in the prompt-able window.
    if (state.diarySummaries.length > 20) {
      state.diarySummaries.splice(0, state.diarySummaries.length - 20);
    }
    this.persist(state);
  }

  getDiarySummaries(scopeType: string, scopeId: string): DiarySummary[] {
    return [...this.loadScope(scopeType, scopeId).diarySummaries];
  }

  clearMessages(scopeType: string, scopeId: string): void {
    const state = this.loadScope(scopeType, scopeId);
    state.messages = [];
    state.pendingDiary = null;
    this.persist(state);
  }

  // ── notes ─────────────────────────────────────────────────────────────────

  addNote(scopeType: string, scopeId: string, text: string): void {
    const state = this.loadScope(scopeType, scopeId);
    state.notes.push({ text: text.slice(0, 260), timestamp: Date.now() / 1000 });
    if (state.notes.length > 20) state.notes.splice(0, state.notes.length - 20);
    this.persist(state);
  }

  recentNotes(scopeType: string, scopeId: string, limit = 5): string[] {
    return this.loadScope(scopeType, scopeId)
      .notes.slice(-limit)
      .map((note) => note.text);
  }

  clearNotes(scopeType: string, scopeId: string): void {
    const state = this.loadScope(scopeType, scopeId);
    state.notes = [];
    this.persist(state);
  }

  // ── short IDs ─────────────────────────────────────────────────────────────

  registerMessageRef(scopeType: string, scopeId: string, messageId: number | string | null): string {
    const state = this.loadScope(scopeType, scopeId);
    const idKey = String(messageId ?? '');
    if (idKey && state.messageRefs[idKey]) return state.messageRefs[idKey];
    let ref = '';
    do {
      ref = Array.from({ length: 4 }, () => REF_ALPHABET[Math.floor(Math.random() * REF_ALPHABET.length)]).join('');
    } while (state.refToMessageId[ref] !== undefined);
    if (idKey) {
      state.messageRefs[idKey] = ref;
      state.refToMessageId[ref] = idKey;
      this.persist(state);
    }
    return ref;
  }

  resolveRef(scopeType: string, scopeId: string, ref: string): string | null {
    const state = this.loadScope(scopeType, scopeId);
    return state.refToMessageId[String(ref).trim().replace(/^\[#?|\]$/g, '')] ?? null;
  }

  /** History entry carrying a short ID — view_image looks up images this way. */
  findEntryByRef(scopeType: string, scopeId: string, ref: string): HistoryEntry | null {
    const normalized = String(ref).trim().replace(/^\[#?|\]$/g, '');
    if (!normalized) return null;
    const state = this.loadScope(scopeType, scopeId);
    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
      if (String(state.messages[i].message_ref ?? '') === normalized) return state.messages[i];
    }
    return null;
  }

  // ── scope state file management ───────────────────────────────────────────

  private nextSegmentIndex(state: ScopeState): number {
    const maxSummary = state.diarySummaries.length > 0
      ? state.diarySummaries[state.diarySummaries.length - 1].index
      : -1;
    const maxSegment = state.diarySegments.length > 0
      ? state.diarySegments[state.diarySegments.length - 1].index
      : -1;
    return Math.max(maxSummary, maxSegment) + 1;
  }

  /**
   * Bound the raw unsummarized queue: if the summarizer falls too far behind,
   * the oldest segments get a mechanical fallback summary instead of growing
   * state forever (their raw text stays in the append-only archive).
   */
  private enforceSegmentBound(state: ScopeState): void {
    while (state.diarySegments.length > MAX_UNSUMMARIZED_SEGMENTS) {
      const overflow = state.diarySegments.shift()!;
      state.diarySummaries.push({
        index: overflow.index,
        text: fallbackSegmentSummary(overflow),
      });
      state.diarySummaries.sort((a, b) => a.index - b.index);
    }
  }

  private scopePath(scopeType: string, scopeId: string): string {
    return join(this.stateDir, `${scopeType}_${scopeId}.json`);
  }

  private loadScope(scopeType: string, scopeId: string): ScopeState {
    const key = `${scopeType}:${scopeId}`;
    const cached = this.cache.get(key);
    if (cached) return cached;
    let state: ScopeState | null = null;
    try {
      // First touch per process reads synchronously so no call site can race a
      // later hydrate into persisting defaults over disk state.
      const raw = JSON.parse(readFileSync(this.scopePath(scopeType, scopeId), 'utf-8')) as Partial<ScopeState>;
      state = {
        agent: { ...defaultAgent(scopeType, scopeId), ...raw.agent },
        messages: raw.messages ?? [],
        diarySegments: raw.diarySegments ?? [],
        diarySummaries: raw.diarySummaries ?? [],
        pendingDiary: raw.pendingDiary ?? null,
        notes: raw.notes ?? [],
        messageRefs: raw.messageRefs ?? {},
        refToMessageId: raw.refToMessageId ?? {},
      };
    } catch {
      state = null;
    }
    state ??= {
      agent: defaultAgent(scopeType, scopeId),
      messages: [],
      diarySegments: [],
      diarySummaries: [],
      pendingDiary: null,
      notes: [],
      messageRefs: {},
      refToMessageId: {},
    };
    this.cache.set(key, state);
    return state;
  }

  /** NOTE: write-through is fire-and-forget; flush() awaits pending writes. */
  private pendingWrites = new Set<Promise<void>>();

  private persist(state: ScopeState): void {
    const key = `${state.agent.scopeType}:${state.agent.scopeId}`;
    const path = this.scopePath(state.agent.scopeType, state.agent.scopeId);
    const write = (async () => {
      await mkdir(dirname(path), { recursive: true });
      const tmp = `${path}.${randomUUID().slice(0, 8)}.tmp`;
      await writeFile(tmp, JSON.stringify(state, null, 2), 'utf-8');
      await rename(tmp, path);
    })().catch(() => {
      // Write failures drop the cache so the next touch re-hydrates from disk.
      this.cache.delete(key);
    });
    this.pendingWrites.add(write);
    void write.then(() => this.pendingWrites.delete(write));
  }

  async flush(): Promise<void> {
    while (this.pendingWrites.size > 0) {
      await Promise.allSettled([...this.pendingWrites]);
    }
  }
}

function defaultAgent(scopeType: string, scopeId: string): AgentRecord {
  return {
    agentId: `agent_${randomUUID().slice(0, 8)}`,
    scopeType,
    scopeId,
    persona: '',
    impression: '',
    triggerWords: ['冰糖', 'bingtang'],
    triggerRate: 0.01,
    displayName: '',
    createdAt: Date.now() / 1000,
    overallSummary: '',
    overallSummaryBuiltAt: 0,
    overallSummaryAtSegments: 0,
  };
}

function fallbackSegmentSummary(segment: DiarySegment): string {
  const messages = segment.messages;
  const count = messages.length;
  const first = messages[0];
  const last = messages[messages.length - 1];
  const time = (entry: HistoryEntry) => {
    const date = new Date(Number(entry.timestamp ?? 0) * 1000);
    const pad = (value: number) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  const speakers = [...new Set(messages.map((entry) => `${entry.nickname}(${entry.user_id})`))].slice(0, 6);
  const samples = messages
    .filter((entry) => String(entry.text ?? '').trim() !== '')
    .slice(0, 2)
    .concat(messages.filter((entry) => String(entry.text ?? '').trim() !== '').slice(-2))
    .map((entry) => `${entry.nickname}: ${String(entry.text ?? '').trim().slice(0, 60)}`);
  return [
    `（摘要生成失败，机械兜底）${count} 条消息，${time(first)} ~ ${time(last)}，参与：${speakers.join('、')}`,
    ...[...new Set(samples)],
  ].join('\n');
}
