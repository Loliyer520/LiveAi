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

import { mkdir, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { readFileSync, readdirSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import type { HistoryEntry } from '../chat/types.js';
import type { MessageArchive } from './archive.js';
import { normalizeMessageRef } from '../message-ref.js';

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
  /** 子AI 总开关：false 时该会话任何消息都不触发生成（历史照记）。 */
  enabled: boolean;
  displayName: string;
  createdAt: number;
  /** Rolling overall summary of the whole scope (compaction primer). */
  overallSummary: string;
  overallSummaryBuiltAt: number;
  /** 最近一次重建整体梗概时最新摘要的分段下标（index 单调递增，不受 20 条上限影响）。 */
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
/**
 * 历史窗口按 20 条整块淘汰，与 assemble 的缓存网格断点对齐：逐条淘汰会让
 * 窗口每条消息都前移一格，整个前缀字节级变动，上游提示词缓存（apipp 是
 * 自动前缀缓存）永远命不中历史部分。整块淘汰后窗口起点每 20 条才跳一次，
 * 跳动之间前缀完全稳定，跨回合缓存命中率从 ~10% 提到 ~97%。
 */
const HISTORY_EVICT_CHUNK = 20;
/**
 * 短ID映射（message_id ↔ 4位 ref）只对仍可能被引用的消息有意义：活跃窗口 +
 * 未归档日记段 + 少量近期归档。发版实测单个会话堆到 99,817 条、状态文件
 * 7.26MB——每来一条消息全量 stringify 一次，纯属白白拖慢回合。保留最近
 * MAX_MESSAGE_REFS 条即可，活跃窗口内的永远保留。
 */
const MAX_MESSAGE_REFS = 5_000;

export class ScopeRepository {
  private readonly cache = new Map<string, ScopeState>();

  constructor(
    private readonly stateDir = join(PROJECT_ROOT, 'data', 'state'),
    private readonly historyLimit = 500,
    private readonly archive: MessageArchive | null = null,
    private readonly defaultTriggerRate = 0.01,
  ) {}

  // ── agents ────────────────────────────────────────────────────────────────

  getOrCreateAgent(scopeType: string, scopeId: string, persona = ''): AgentRecord {
    const state = this.loadScope(scopeType, scopeId);
    // Honor the documented seeding parameter: an explicitly supplied persona
    // fills an agent that has none (never overwrites an existing one).
    if (persona.trim() && !state.agent.persona.trim()) {
      state.agent.persona = persona;
      this.persist(state);
    }
    return state.agent;
  }

  updateAgent(scopeType: string, scopeId: string, patch: Partial<AgentRecord>): void {
    const state = this.loadScope(scopeType, scopeId);
    state.agent = { ...state.agent, ...patch };
    this.persist(state);
  }

  /** Every scope that has a state file on disk (group_<id>.json / private_<id>.json). */
  listAgents(): AgentRecord[] {
    let files: string[] = [];
    try {
      files = readdirSync(this.stateDir);
    } catch {
      return [];
    }
    const agents: AgentRecord[] = [];
    for (const file of files) {
      const match = /^(group|private)_(\d+)\.json$/.exec(file);
      if (!match) continue;
      agents.push(this.getOrCreateAgent(match[1]!, match[2]!));
    }
    return agents.sort((a, b) =>
      a.scopeType === b.scopeType ? Number(a.scopeId) - Number(b.scopeId) : a.scopeType.localeCompare(b.scopeType),
    );
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
    if (state.messages.length > this.historyLimit + HISTORY_EVICT_CHUNK) {
      state.messages.splice(0, HISTORY_EVICT_CHUNK);
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
    this.pruneMessageRefs(state);
    this.persist(state);
    return needsSummarize;
  }

  /** Segments filled but not yet summarized — the summarizer's work queue. */
  getUnsummarizedSegments(scopeType: string, scopeId: string): DiarySegment[] {
    return [...this.loadScope(scopeType, scopeId).diarySegments];
  }

  /** 启动清扫：tmp+rename 写到一半被 kill 会留下孤儿 .tmp，攒多了占盘还碍眼。 */
  async sweepOrphanTemps(): Promise<number> {
    let names: string[] = [];
    try {
      names = await readdir(this.stateDir);
    } catch {
      return 0;
    }
    let swept = 0;
    for (const name of names) {
      if (!/\.tmp$/.test(name)) continue;
      try {
        await rm(join(this.stateDir, name), { force: true });
        swept += 1;
      } catch { /* best-effort */ }
    }
    return swept;
  }

  /** Scope keys ("type:id") of every persisted state file — recovery scans. */
  async listPersistedScopes(): Promise<string[]> {
    let names: string[] = [];
    try {
      names = (await readdir(this.stateDir)).filter((name) => /^(group|private|domain)_[^/]+\.json$/.test(name));
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
    return state.refToMessageId[normalizeMessageRef(ref)] ?? null;
  }

  /** History entry carrying a short ID — view_image looks up images this way. */
  findEntryByRef(scopeType: string, scopeId: string, ref: string): HistoryEntry | null {
    const normalized = normalizeMessageRef(ref);
    if (!normalized) return null;
    const state = this.loadScope(scopeType, scopeId);
    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
      if (String(state.messages[i].message_ref ?? '') === normalized) return state.messages[i];
    }
    return null;
  }

  /** 最近的带图消息（新→旧，最多 limit 条）：取图失败时提示模型真实可用的短ID。 */
  recentImageRefs(scopeType: string, scopeId: string, limit = 5): { ref: string; ageMinutes: number }[] {
    const state = this.loadScope(scopeType, scopeId);
    const now = Date.now() / 1000; // entry.timestamp 是 epoch 秒，别拿毫秒减
    const out: { ref: string; ageMinutes: number }[] = [];
    for (let i = state.messages.length - 1; i >= 0 && out.length < limit; i -= 1) {
      const entry = state.messages[i];
      const ref = String(entry.message_ref ?? '');
      if (ref === '' || !Array.isArray(entry.image_refs) || entry.image_refs.length === 0) continue;
      const timestamp = Number(entry.timestamp);
      const ageMinutes = Number.isFinite(timestamp) ? Math.max(0, Math.round((now - timestamp) / 60)) : 0;
      out.push({ ref, ageMinutes });
    }
    return out;
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
    // Same 20-segment prompt window as storeDiarySummary: fallback summaries
    // must not grow the list past the window the prompt actually reads.
    if (state.diarySummaries.length > 20) {
      state.diarySummaries.splice(0, state.diarySummaries.length - 20);
    }
  }

  /**
   * Bound the short-ID maps. Refs still reachable from the active history
   * window / unsummarized diary segments are always kept; the rest degrade
   * to "newest MAX_MESSAGE_REFS" (numeric message ids are chronological).
   */
  private pruneMessageRefs(state: ScopeState): void {
    const total = Object.keys(state.messageRefs).length;
    if (total <= MAX_MESSAGE_REFS) return;
    const liveIds = new Set<string>();
    const collect = (entries: HistoryEntry[] | undefined): void => {
      for (const entry of entries ?? []) {
        const id = String(entry.message_id ?? '');
        if (id) liveIds.add(id);
      }
    };
    collect(state.messages);
    collect(state.pendingDiary?.messages);
    for (const segment of state.diarySegments) collect(segment.messages);

    const keep = new Set<string>();
    for (const id of liveIds) if (state.messageRefs[id] !== undefined) keep.add(id);
    if (keep.size >= MAX_MESSAGE_REFS) {
      // Pathological active window larger than the cap: keep only live refs.
    } else {
      const rest = Object.keys(state.messageRefs)
        .filter((id) => !keep.has(id))
        .sort((a, b) => {
          const na = Number(a);
          const nb = Number(b);
          return Number.isFinite(na) && Number.isFinite(nb) ? nb - na : b.localeCompare(a);
        });
      for (const id of rest) {
        if (keep.size >= MAX_MESSAGE_REFS) break;
        keep.add(id);
      }
    }
    for (const id of Object.keys(state.messageRefs)) {
      if (keep.has(id)) continue;
      const ref = state.messageRefs[id];
      delete state.messageRefs[id];
      if (ref !== undefined) delete state.refToMessageId[ref];
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
        agent: { ...defaultAgent(scopeType, scopeId, this.defaultTriggerRate), ...raw.agent },
        messages: raw.messages ?? [],
        diarySegments: raw.diarySegments ?? [],
        diarySummaries: raw.diarySummaries ?? [],
        pendingDiary: raw.pendingDiary ?? null,
        notes: raw.notes ?? [],
        messageRefs: raw.messageRefs ?? {},
        refToMessageId: raw.refToMessageId ?? {},
      };
    } catch (error) {
      // 文件不存在 = 新会话，正常；但 JSON 损坏绝不能静默覆盖——
      // 先留 .bak 再用全新状态启动，否则记忆丢了连线索都没有。
      if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') {
        const broken = this.scopePath(scopeType, scopeId);
        const backup = `${broken}.bak-${Date.now()}`;
        try {
          renameSync(broken, backup);
          console.error(`[repository] 会话状态文件损坏，已备份到 ${backup}:`, error);
        } catch {
          console.error(`[repository] 会话状态文件损坏且备份失败:`, error);
        }
      }
      state = null;
    }
    state ??= {
      agent: defaultAgent(scopeType, scopeId, this.defaultTriggerRate),
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
  /** 每文件写链：同一 scope 的 persist 必须按调用顺序落盘——
   *  否则并发的 tmp+rename 可能让旧状态后落地，磁盘回退。 */
  private readonly writeQueues = new Map<string, Promise<void>>();

  private persist(state: ScopeState): void {
    const key = `${state.agent.scopeType}:${state.agent.scopeId}`;
    const path = this.scopePath(state.agent.scopeType, state.agent.scopeId);
    const snapshot = JSON.stringify(state, null, 2);
    const write = (this.writeQueues.get(path) ?? Promise.resolve())
      .then(async () => {
        await mkdir(dirname(path), { recursive: true });
        const tmp = `${path}.${randomUUID().slice(0, 8)}.tmp`;
        await writeFile(tmp, snapshot, 'utf-8');
        await rename(tmp, path);
      })
      .catch(() => {
        // Write failures drop the cache so the next touch re-hydrates from disk.
        this.cache.delete(key);
      });
    this.writeQueues.set(path, write);
    this.pendingWrites.add(write);
    void write.then(() => this.pendingWrites.delete(write));
  }

  async flush(): Promise<void> {
    while (this.pendingWrites.size > 0) {
      await Promise.allSettled([...this.pendingWrites]);
    }
  }
}

function defaultAgent(scopeType: string, scopeId: string, triggerRate = 0.01): AgentRecord {
  return {
    agentId: `agent_${randomUUID().slice(0, 8)}`,
    scopeType,
    scopeId,
    persona: '',
    impression: '',
    triggerWords: [], // 默认只认 @；触发词由各会话自行配置（trigger_config / scope_manage）
    triggerRate,
    enabled: true,
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
