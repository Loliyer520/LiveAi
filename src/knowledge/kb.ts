/**
 * 知识库（Knowledge Base）——DeepLive 记忆体系的结构化知识层。
 *
 * 存储：data/knowledge/kb.json（条目+挂载）+ data/knowledge/pending.json
 * （下级追加进主级待审核队列）。
 *
 * 层级：中枢（master scope）管全局标签体系和裁判权；主级按标签分得知识包；
 * 主级挂载给下级时带 ACL。权限位在工具层硬检查：
 *   read   = 查询、读入上下文
 *   append = 新增条目，进主级待审核队列，复核后才生效
 *   write  = 追加 + 修订挂载范围内既有条目
 */

import { readFileSync } from 'node:fs';
import { mkdir, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';

export type KbPermission = 'read' | 'append' | 'write';

export interface KbEntry {
  id: string;
  subject: string;
  content: string;
  tags: string[];
  source: string;
  confidence: number;
  createdAt: number;
  updatedAt: number;
}

export interface KbMount {
  /** 挂载目标：scope key（type:id）或 'all'。master scope 在 admins 里天然全权。 */
  target: string;
  permission: KbPermission;
  /** 按标签过滤的挂载范围；空 = 该 target 可见的全部条目。 */
  tags?: string[];
  grantedAt: number;
}

export interface KbPendingItem {
  id: string;
  entry: Pick<KbEntry, 'subject' | 'content' | 'tags' | 'source' | 'confidence'>;
  proposedBy: string;
  proposedAt: number;
}

export interface KbState {
  entries: Record<string, KbEntry>;
  mounts: KbMount[];
  updatedAt: number;
}

const MAX_SUBJECT = 80;
const MAX_CONTENT = 2000;
const MAX_TAG = 32;

function now(): number {
  return Date.now() / 1000;
}

function cleanTags(tags: unknown): string[] {
  if (!Array.isArray(tags)) return [];
  const out: string[] = [];
  for (const raw of tags) {
    const tag = String(raw ?? '').trim().toLowerCase().replace(/\s+/g, '-').slice(0, MAX_TAG);
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out;
}

const PERMISSION_RANK: Record<KbPermission, number> = { read: 1, append: 2, write: 3 };

export class KnowledgeBase {
  private state: KbState | null = null;
  private pendingState: KbPendingItem[] | null = null;
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(
    private readonly dir: string,
    /** 中枢 scope key：master scope 拥有裁判权与全局可见权。 */
    private readonly masterScopeKey: string,
  ) {}

  private get kbPath(): string {
    return join(this.dir, 'kb.json');
  }

  private get pendingPath(): string {
    return join(this.dir, 'pending.json');
  }

  private isAdmin(scopeKey: string): boolean {
    return scopeKey === this.masterScopeKey || scopeKey === 'master';
  }

  // ── 查询 ─────────────────────────────────────────────────────────────────

  list(query: { tags?: string[]; q?: string; limit?: number }): KbEntry[] {
    const state = this.load();
    const tagFilter = cleanTags(query.tags);
    const text = (query.q ?? '').trim().toLowerCase();
    let out = Object.values(state.entries);
    if (tagFilter.length > 0) {
      out = out.filter((entry) => tagFilter.every((tag) => entry.tags.includes(tag)));
    }
    if (text) {
      out = out.filter(
        (entry) => entry.subject.toLowerCase().includes(text) || entry.content.toLowerCase().includes(text),
      );
    }
    out.sort((a, b) => b.updatedAt - a.updatedAt);
    return out.slice(0, Math.max(1, Math.min(100, query.limit ?? 20)));
  }

  get(id: string): KbEntry | null {
    return this.load().entries[id] ?? null;
  }

  // ── 写入（中枢/主级直写；replace 取代 add 的旧条目语义由调用方决定） ────────

  add(input: {
    subject: string;
    content: string;
    tags?: string[];
    source: string;
    confidence?: number;
  }): KbEntry {
    const state = this.load();
    const entry: KbEntry = {
      id: randomUUID().slice(0, 8),
      subject: input.subject.trim().slice(0, MAX_SUBJECT),
      content: input.content.trim().slice(0, MAX_CONTENT),
      tags: cleanTags(input.tags),
      source: input.source.slice(0, 64),
      confidence: Math.max(0, Math.min(1, input.confidence ?? 0.8)),
      createdAt: now(),
      updatedAt: now(),
    };
    if (!entry.subject) throw new Error('subject 为空');
    if (!entry.content) throw new Error('content 为空');
    state.entries[entry.id] = entry;
    this.persist();
    return entry;
  }

  /** supersede 语义：在原地修订条目（保留 id 供挂载继续生效），来源与时间戳更新。 */
  revise(
    entryId: string,
    input: { subject?: string; content?: string; tags?: string[]; source: string; confidence?: number },
  ): KbEntry {
    const state = this.load();
    const old = state.entries[entryId];
    if (!old) throw new Error(`没有知识条目 ${entryId}`);
    const next: KbEntry = {
      ...old,
      subject: input.subject !== undefined ? input.subject.trim().slice(0, MAX_SUBJECT) : old.subject,
      content: input.content !== undefined ? input.content.trim().slice(0, MAX_CONTENT) : old.content,
      tags: input.tags !== undefined ? cleanTags(input.tags) : old.tags,
      confidence: input.confidence !== undefined ? Math.max(0, Math.min(1, input.confidence)) : old.confidence,
      source: input.source.slice(0, 64),
      updatedAt: now(),
    };
    if (!next.subject) throw new Error('subject 为空');
    if (!next.content) throw new Error('content 为空');
    state.entries[entryId] = next;
    this.persist();
    return next;
  }

  remove(entryId: string): boolean {
    const state = this.load();
    if (!state.entries[entryId]) return false;
    delete state.entries[entryId];
    this.persist();
    return true;
  }

  // ── 挂载 ─────────────────────────────────────────────────────────────────

  grantMount(target: string, permission: KbPermission, tags?: string[]): KbMount {
    const state = this.load();
    const mount: KbMount = {
      target,
      permission,
      tags: tags === undefined ? undefined : cleanTags(tags),
      grantedAt: now(),
    };
    const key = (m: KbMount) => `${m.target}|${JSON.stringify(m.tags ?? null)}`;
    state.mounts = state.mounts.filter((m) => key(m) !== key(mount));
    state.mounts.push(mount);
    this.persist();
    return mount;
  }

  revokeMount(target: string, tags?: string[]): boolean {
    const state = this.load();
    const before = state.mounts.length;
    const norm = tags === undefined ? null : JSON.stringify(cleanTags(tags));
    state.mounts = state.mounts.filter((m) => {
      if (m.target !== target) return true;
      if (norm === null) return false;
      return JSON.stringify(m.tags ?? null) !== norm;
    });
    if (state.mounts.length === before) return false;
    this.persist();
    return true;
  }

  /** 某 scope 的全部挂载（含 'all' 通配）。 */
  mountsFor(scopeKey: string): KbMount[] {
    const state = this.load();
    return state.mounts.filter((m) => m.target === scopeKey || m.target === 'all');
  }

  /** 某 scope 的可见条目（并集去重，按更新时间倒序）。 */
  visibleFor(scopeKey: string): KbEntry[] {
    if (this.isAdmin(scopeKey)) {
      return Object.values(this.load().entries).sort((a, b) => b.updatedAt - a.createdAt);
    }
    const mounts = this.mountsFor(scopeKey);
    if (mounts.length === 0) return [];
    const seen = new Map<string, KbEntry>();
    const state = this.load();
    for (const mount of mounts) {
      for (const entry of Object.values(state.entries)) {
        if (mount.tags && mount.tags.length > 0 && !mount.tags.some((tag) => entry.tags.includes(tag))) continue;
        if (!seen.has(entry.id)) seen.set(entry.id, entry);
      }
    }
    return [...seen.values()].sort((a, b) => b.updatedAt - a.updatedAt);
  }

  /** 某 scope 对某条目的最高权限；无挂载=不可见。 */
  permissionFor(scopeKey: string, entry: KbEntry): KbPermission | null {
    if (this.isAdmin(scopeKey)) return 'write';
    let best: KbPermission | null = null;
    for (const mount of this.mountsFor(scopeKey)) {
      if (mount.tags && mount.tags.length > 0 && !mount.tags.some((tag) => entry.tags.includes(tag))) continue;
      if (best === null || PERMISSION_RANK[mount.permission] > PERMISSION_RANK[best]) best = mount.permission;
    }
    return best;
  }

  /** 该 scope 是否对某标签拥有 ≥ 某权限（用于追加判定，无需具体条目）。 */
  hasPermissionOnTags(scopeKey: string, permission: KbPermission, tags: string[]): boolean {
    if (this.isAdmin(scopeKey)) return true;
    const need = PERMISSION_RANK[permission];
    for (const mount of this.mountsFor(scopeKey)) {
      if (PERMISSION_RANK[mount.permission] < need) continue;
      if (!mount.tags || mount.tags.length === 0) return true;
      if (tags.some((tag) => mount.tags!.includes(tag))) return true;
    }
    return false;
  }

  // ── 下级追加 → 主级待审核队列 ──────────────────────────────────────────

  propose(scopeKey: string, input: { subject: string; content: string; tags?: string[]; confidence?: number }): string {
    const tags = cleanTags(input.tags);
    if (!this.hasPermissionOnTags(scopeKey, 'append', tags)) {
      throw new Error('没有追加权限：需挂载到本 scope 且带 append/write 权限（mount 范围要覆盖这些标签）');
    }
    const item: KbPendingItem = {
      id: randomUUID().slice(0, 8),
      entry: {
        subject: input.subject.trim().slice(0, MAX_SUBJECT),
        content: input.content.trim().slice(0, MAX_CONTENT),
        tags,
        source: `proposed:${scopeKey}`,
        confidence: Math.max(0, Math.min(1, input.confidence ?? 0.5)),
      },
      proposedBy: scopeKey,
      proposedAt: now(),
    };
    if (!item.entry.subject) throw new Error('subject 为空');
    if (!item.entry.content) throw new Error('content 为空');
    this.pending().push(item);
    this.persistPending();
    return item.id;
  }

  pendingFor(reviewerScope: string, proposedBy?: string): KbPendingItem[] {
    const items = this.pending();
    const filtered = this.isAdmin(reviewerScope)
      ? items
      : items.filter((item) => item.proposedBy === reviewerScope);
    if (!proposedBy) return [...filtered];
    return filtered.filter((item) => item.proposedBy === proposedBy);
  }

  review(pendingId: string, action: 'approve' | 'reject', reviewerScope: string): KbEntry | null {
    const items = this.pending();
    const index = items.findIndex((item) => item.id === pendingId);
    if (index < 0) throw new Error(`没有待审项 ${pendingId}`);
    const item = items[index];
    if (!this.isAdmin(reviewerScope) && item.proposedBy !== reviewerScope) {
      throw new Error('只能复核提出者是本 scope 或中枢身份的待审项');
    }
    items.splice(index, 1);
    this.persistPending();
    if (action === 'reject') return null;
    return this.add({
      ...item.entry,
      source: item.entry.source.replace(/^proposed:/, 'approved:'),
    });
  }

  // ── 持久化 ───────────────────────────────────────────────────────────────

  private load(): KbState {
    if (this.state !== null) return this.state;
    try {
      // 同步读，避免与工具调用链互相等待；写走串行队列（见 persist）。
      const raw = JSON.parse(readFileSync(this.kbPath, 'utf-8')) as Partial<KbState>;
      this.state = {
        entries: raw.entries ?? {},
        mounts: Array.isArray(raw.mounts) ? raw.mounts : [],
        updatedAt: raw.updatedAt ?? 0,
      };
    } catch {
      this.state = { entries: {}, mounts: [], updatedAt: 0 };
    }
    return this.state;
  }

  private pending(): KbPendingItem[] {
    if (this.pendingState !== null) return this.pendingState;
    try {
      const raw = JSON.parse(readFileSync(this.pendingPath, 'utf-8')) as KbPendingItem[] | null;
      this.pendingState = Array.isArray(raw) ? raw : [];
    } catch {
      this.pendingState = [];
    }
    return this.pendingState;
  }

  private persist(): void {
    const state = this.load();
    state.updatedAt = now();
    this.enqueueWrite(this.kbPath, JSON.stringify(state, null, 2));
  }

  private persistPending(): void {
    this.enqueueWrite(this.pendingPath, JSON.stringify(this.pending(), null, 2));
  }

  /** Serialized atomic writes: tmp + rename，队列串行化避免交错写。 */
  private enqueueWrite(path: string, data: string): void {
    this.writeQueue = this.writeQueue
      .then(async () => {
        await mkdir(dirname(path), { recursive: true });
        const tmp = `${path}.${randomUUID().slice(0, 8)}.tmp`;
        await writeFile(tmp, data, 'utf-8');
        await rename(tmp, path);
      })
      .catch((error) => {
        console.error('[kb] persist failed:', error);
      });
  }

  async flush(): Promise<void> {
    await this.writeQueue;
  }
}
