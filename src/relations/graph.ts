/**
 * Relation graph — the shared, cross-scope intelligence layer. Ported from
 * the legacy main-AI relation model, redesigned so every child AI (any group,
 * any private chat) reads from and writes into ONE graph:
 *
 *  - people: userId → names seen per scope, scopes they appear in, facts
 *  - facts: subject + category + text + confidence + source scope/tool,
 *    supersede/retract instead of delete (audit trail survives)
 *  - scopes: display name, sampled members, topic tags
 *
 * The graph is global by design (one JSON file, atomic writes) — that is what
 * lets a fact learned in group A surface in the person card of a private chat
 * with the same user. Prompt-side sections carry an explicit "don't leak
 * across scopes" rule so sharing happens in knowledge, not in speech.
 */

import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

export const FACT_CATEGORIES = ['identity', 'preference', 'event', 'relationship', 'emotion', 'other'] as const;
export type FactCategory = (typeof FACT_CATEGORIES)[number];

export type FactStatus = 'active' | 'superseded' | 'retracted';

export interface RelationFact {
  id: string;
  subject: number;
  category: FactCategory;
  text: string;
  sourceScope: string;
  sourceTool: 'intel_report' | 'auto_extract' | 'master';
  confidence: number;
  status: FactStatus;
  createdAt: number;
  supersededBy?: string;
}

export interface PersonRecord {
  userId: number;
  /** Latest nickname seen. */
  canonicalName: string;
  /** scopeKey → nickname seen there (alias map). */
  aliases: Record<string, string>;
  scopes: string[];
  /** Free-text overall impression of this person (master/curated). */
  impression: string;
  facts: RelationFact[];
  firstSeenAt: number;
  lastSeenAt: number;
}

export interface ScopeRecord {
  scopeKey: string;
  displayName: string;
  /** 会话印象: purpose / atmosphere / key people, auto- and tool-maintained. */
  impression: string;
  memberIds: number[];
  topicTags: string[];
  lastActiveAt: number;
}

interface GraphState {
  people: Record<string, PersonRecord>;
  scopes: Record<string, ScopeRecord>;
  updatedAt: number;
}

const MAX_FACTS_PER_PERSON = 60;
const MAX_MEMBERS_SAMPLED = 40;
const MAX_TOPIC_TAGS = 12;
const MAX_FACT_TEXT = 200;

export interface AddFactInput {
  subject: number;
  category: FactCategory;
  text: string;
  sourceScope: string;
  sourceTool: RelationFact['sourceTool'];
  confidence?: number;
}

export class RelationGraph {
  private state: GraphState | null = null;
  /** Serialized writes: flush() on the tail covers every earlier persist. */
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(private readonly path: string) {}

  // ── observation (cheap, every message) ────────────────────────────────────

  touchMember(scopeKey: string, scopeDisplayName: string, userId: number, nickname: string, timestamp = Date.now() / 1000): void {
    if (!Number.isFinite(userId) || userId <= 0) return;
    const state = this.load();
    const person = this.ensurePerson(state, userId);
    const name = nickname.trim();
    if (name) {
      person.canonicalName = name;
      person.aliases[scopeKey] = name;
    }
    if (!person.scopes.includes(scopeKey)) person.scopes.push(scopeKey);
    person.lastSeenAt = Math.max(person.lastSeenAt, timestamp);

    const scope = this.scope(state, scopeKey);
    if (scopeDisplayName.trim()) scope.displayName = scopeDisplayName.trim();
    if (!scope.memberIds.includes(userId)) {
      scope.memberIds.push(userId);
      if (scope.memberIds.length > MAX_MEMBERS_SAMPLED) {
        scope.memberIds.splice(0, scope.memberIds.length - MAX_MEMBERS_SAMPLED);
      }
    }
    scope.lastActiveAt = Math.max(scope.lastActiveAt, timestamp);
    this.persist();
  }

  upsertScopeTopics(scopeKey: string, tags: string[]): void {
    const state = this.load();
    const scope = this.scope(state, scopeKey);
    const cleaned = tags.map((tag) => tag.trim().slice(0, 24)).filter((tag) => tag.length > 0);
    if (cleaned.length === 0) return;
    scope.topicTags = [...new Set([...cleaned, ...scope.topicTags])].slice(0, MAX_TOPIC_TAGS);
    this.persist();
  }

  // ── impressions (会话印象/人物印象, merged into the graph) ────────────────

  scopeImpression(scopeKey: string): string {
    return this.load().scopes[scopeKey]?.impression ?? '';
  }

  setScopeImpression(scopeKey: string, text: string): void {
    const cleaned = String(text ?? '').trim().slice(0, 300);
    if (!cleaned) return;
    const scope = this.scope(this.load(), scopeKey);
    if (scope.impression === cleaned) return;
    scope.impression = cleaned;
    this.persist();
  }

  setPersonImpression(userId: number, text: string): void {
    const cleaned = String(text ?? '').trim().slice(0, 300);
    if (!cleaned || !Number.isFinite(userId) || userId <= 0) return;
    const person = this.ensurePerson(this.load(), userId);
    if (person.impression === cleaned) return;
    person.impression = cleaned;
    this.persist();
  }

  // ── facts ─────────────────────────────────────────────────────────────────

  addFact(input: AddFactInput): RelationFact | null {
    const text = String(input.text ?? '').trim().slice(0, MAX_FACT_TEXT);
    if (!text || !Number.isFinite(input.subject) || input.subject <= 0) return null;
    const category = (FACT_CATEGORIES as readonly string[]).includes(input.category) ? input.category : 'other';
    const state = this.load();
    const person = this.ensurePerson(state, input.subject);
    const normalized = text.toLowerCase();
    const duplicate = person.facts.find(
      (fact) => fact.status === 'active' && fact.text.trim().toLowerCase() === normalized,
    );
    if (duplicate) {
      // Refresh recency/confidence instead of duplicating.
      duplicate.createdAt = Date.now() / 1000;
      duplicate.confidence = Math.max(duplicate.confidence, clampConfidence(input.confidence ?? defaultConfidence(input.sourceTool)));
      this.persist();
      return duplicate;
    }
    const fact: RelationFact = {
      id: `f_${randomUUID().slice(0, 8)}`,
      subject: input.subject,
      category,
      text,
      sourceScope: input.sourceScope,
      sourceTool: input.sourceTool,
      confidence: clampConfidence(input.confidence ?? defaultConfidence(input.sourceTool)),
      status: 'active',
      createdAt: Date.now() / 1000,
    };
    person.facts.push(fact);
    // Retire oldest active facts beyond the cap (retracted first).
    const active = person.facts.filter((entry) => entry.status === 'active');
    if (active.length > MAX_FACTS_PER_PERSON) {
      const overflow = active.length - MAX_FACTS_PER_PERSON;
      let retired = 0;
      for (const entry of person.facts) {
        if (retired >= overflow) break;
        if (entry.status === 'active') {
          entry.status = 'retracted';
          retired += 1;
        }
      }
    }
    this.persist();
    return fact;
  }

  retractFact(factId: string): boolean {
    const fact = this.findFact(factId);
    if (!fact) return false;
    fact.status = 'retracted';
    this.persist();
    return true;
  }

  supersedeFact(factId: string, newText: string, sourceTool: RelationFact['sourceTool'] = 'master'): RelationFact | null {
    const old = this.findFact(factId);
    if (!old) return null;
    const next = this.addFact({
      subject: old.subject,
      category: old.category,
      text: newText,
      sourceScope: old.sourceScope,
      sourceTool,
      confidence: 1.0,
    });
    if (next !== null && next.id !== old.id) {
      old.status = 'superseded';
      old.supersededBy = next.id;
      this.persist();
    }
    return next;
  }

  findFact(factId: string): RelationFact | null {
    const state = this.load();
    for (const person of Object.values(state.people)) {
      const hit = person.facts.find((fact) => fact.id === factId);
      if (hit) return hit;
    }
    return null;
  }

  searchFacts(query: string, limit = 10): RelationFact[] {
    const terms = String(query ?? '').toLowerCase().split(/[\s,，、]+/).filter((term) => term.length > 0);
    if (terms.length === 0) return [];
    const state = this.load();
    const hits: RelationFact[] = [];
    for (const person of Object.values(state.people)) {
      for (const fact of person.facts) {
        if (fact.status !== 'active') continue;
        const haystack = `${fact.text}\n${person.canonicalName}`.toLowerCase();
        if (terms.every((term) => haystack.includes(term))) hits.push(fact);
        if (hits.length >= limit) return hits;
      }
    }
    return hits;
  }

  // ── cards (prompt-facing) ─────────────────────────────────────────────────

  person(userId: number): PersonRecord | null {
    return this.load().people[String(userId)] ?? null;
  }

  /** Compact one-paragraph dossier for the prompt. */
  personCard(userId: number, maxFacts = 8): string {
    const person = this.person(userId);
    if (!person) return '';
    const activeFacts = person.facts
      .filter((fact) => fact.status === 'active')
      .sort((a, b) => b.confidence - a.confidence || b.createdAt - a.createdAt)
      .slice(0, maxFacts);
    const aliasScopes = person.scopes
      .map((scopeKey) => ({ scopeKey, name: person.aliases[scopeKey] }))
      .filter((entry) => entry.name && entry.name !== person.canonicalName)
      .slice(0, 3);
    const lines: string[] = [];
    const seen = person.scopes.length;
    lines.push(`${person.canonicalName}(${userId})，出现于 ${seen} 个会话${aliasScopes.length > 0 ? `，又名：${aliasScopes.map((entry) => entry.name).join('/')}` : ''}`);
    if (person.impression) lines.push(`印象：${person.impression}`);
    for (const fact of activeFacts) {
      const date = new Date(fact.createdAt * 1000);
      const day = `${date.getMonth() + 1}-${date.getDate()}`;
      const lowConfidence = fact.confidence < 0.7 ? '（不太确定）' : '';
      lines.push(`- [${fact.category}][${day}][${fact.id}]${lowConfidence} ${fact.text}`);
    }
    return lines.join('\n');
  }

  scopeCard(scopeKey: string): string {
    const scope = this.load().scopes[scopeKey];
    if (!scope) return '';
    const parts: string[] = [];
    if (scope.impression) parts.push(`印象: ${scope.impression}`);
    if (scope.topicTags.length > 0) parts.push(`常聊话题: ${scope.topicTags.join('、')}`);
    if (scope.memberIds.length > 0) {
      const names = scope.memberIds
        .slice(-8)
        .map((id) => {
          const person = this.load().people[String(id)];
          return person ? `${person.canonicalName}(${id})` : String(id);
        });
      parts.push(`已知成员: ${names.join('、')}`);
    }
    return parts.join('\n');
  }

  /** People in this scope, with card text, for bulk prompt context. */
  knownMembers(scopeKey: string): PersonRecord[] {
    const scope = this.load().scopes[scopeKey];
    if (!scope) return [];
    const state = this.load();
    return scope.memberIds
      .map((id) => state.people[String(id)])
      .filter((person): person is PersonRecord => person !== undefined);
  }

  /** Master-scope wake overview: richest dossiers + active scopes, one line each. */
  overviewCard(maxPeople = 10, maxScopes = 8): string {
    const state = this.load();
    const lines: string[] = [];
    const people = Object.values(state.people)
      .map((person) => ({
        person,
        active: person.facts.filter((fact) => fact.status === 'active').length,
      }))
      .filter((entry) => entry.active > 0 || entry.person.impression !== '')
      .sort((a, b) => b.active - a.active || b.person.lastSeenAt - a.person.lastSeenAt)
      .slice(0, maxPeople);
    if (people.length > 0) {
      lines.push('人物（按情报量排序）:');
      for (const { person, active } of people) {
        lines.push(`- ${person.canonicalName || person.userId}(${person.userId})：${active} 条情报${person.impression ? `，印象「${person.impression.slice(0, 40)}」` : ''}`);
      }
    }
    const scopes = Object.values(state.scopes)
      .filter((scope) => scope.impression !== '' || scope.topicTags.length > 0)
      .sort((a, b) => b.lastActiveAt - a.lastActiveAt)
      .slice(0, maxScopes);
    if (scopes.length > 0) {
      lines.push('会话（按活跃排序）:');
      for (const scope of scopes) {
        const label = scope.displayName || scope.scopeKey;
        lines.push(`- ${label}（${scope.scopeKey}）${scope.impression ? `：${scope.impression.slice(0, 50)}` : scope.topicTags.length > 0 ? `：${scope.topicTags.slice(0, 4).join('、')}` : ''}`);
      }
    }
    return lines.join('\n');
  }

  stats(): { people: number; facts: number; scopes: number } {
    const state = this.load();
    const people = Object.values(state.people);
    return {
      people: people.length,
      facts: people.reduce((sum, person) => sum + person.facts.filter((fact) => fact.status === 'active').length, 0),
      scopes: Object.keys(state.scopes).length,
    };
  }

  async flush(): Promise<void> {
    await this.writeQueue;
  }

  // ── persistence ───────────────────────────────────────────────────────────

  private load(): GraphState {
    if (this.state !== null) return this.state;
    try {
      const raw = JSON.parse(readFileSync(this.path, 'utf-8')) as Partial<GraphState>;
      this.state = {
        people: raw.people ?? {},
        scopes: raw.scopes ?? {},
        updatedAt: raw.updatedAt ?? 0,
      };
    } catch {
      this.state = { people: {}, scopes: {}, updatedAt: 0 };
    }
    // Records persisted before impressions existed default to ''.
    for (const person of Object.values(this.state.people)) person.impression ??= '';
    for (const scope of Object.values(this.state.scopes)) scope.impression ??= '';
    return this.state;
  }

  private ensurePerson(state: GraphState, userId: number): PersonRecord {
    const key = String(userId);
    let person = state.people[key];
    if (!person) {
      person = {
        userId,
        canonicalName: '',
        aliases: {},
        scopes: [],
        impression: '',
        facts: [],
        firstSeenAt: Date.now() / 1000,
        lastSeenAt: Date.now() / 1000,
      };
      state.people[key] = person;
    }
    return person;
  }

  private scope(state: GraphState, scopeKey: string): ScopeRecord {
    let scope = state.scopes[scopeKey];
    if (!scope) {
      scope = { scopeKey, displayName: '', impression: '', memberIds: [], topicTags: [], lastActiveAt: Date.now() / 1000 };
      state.scopes[scopeKey] = scope;
    }
    scope.impression ??= '';
    return scope;
  }

  private persist(): void {
    const state = this.load();
    state.updatedAt = Date.now() / 1000;
    const path = this.path;
    const snapshot = JSON.stringify(state, null, 2);
    this.writeQueue = this.writeQueue
      .then(async () => {
        await mkdir(dirname(path), { recursive: true });
        const tmp = `${path}.${randomUUID().slice(0, 8)}.tmp`;
        await writeFile(tmp, snapshot, 'utf-8');
        await rename(tmp, path);
      })
      .catch((error) => {
        console.error('[relations] persist failed:', error);
      });
  }

  /** Test/debug helper: replace storage path read cache. */
  invalidate(): void {
    this.state = null;
  }

  /** Structural async load (unused fast path kept for API symmetry). */
  async loadAsync(): Promise<void> {
    if (this.state !== null) return;
    try {
      const raw = JSON.parse(await readFile(this.path, 'utf-8')) as Partial<GraphState>;
      this.state = { people: raw.people ?? {}, scopes: raw.scopes ?? {}, updatedAt: raw.updatedAt ?? 0 };
    } catch {
      this.state = { people: {}, scopes: {}, updatedAt: 0 };
    }
    for (const person of Object.values(this.state.people)) person.impression ??= '';
    for (const scope of Object.values(this.state.scopes)) scope.impression ??= '';
  }
}

function defaultConfidence(sourceTool: RelationFact['sourceTool']): number {
  if (sourceTool === 'master') return 1.0;
  if (sourceTool === 'intel_report') return 0.8;
  return 0.6;
}

function clampConfidence(value: number): number {
  if (!Number.isFinite(value)) return 0.6;
  return Math.min(1, Math.max(0, value));
}
