/**
 * Intel collector — asynchronous, cross-scope intelligence distillation.
 *
 * Every N real user messages in a scope, a background job replays the last
 * window of transcript through a cheap model pass and extracts structured
 * observations into the shared relation graph: durable facts about people
 * (identity / preference / relationship / …) plus scope topic tags. Child
 * AIs never wait on this — it runs out-of-band, and a failed pass just
 * re-arms earlier so the next window retries with more data.
 *
 * Combined with the manual `intel_report` tool this gives every child AI
 * (any group, any private chat) both an automatic and a deliberate path into
 * the same graph, and `relation_query` / person cards read it back out.
 */

import type { RelationGraph, FactCategory } from './graph.js';
import { FACT_CATEGORIES } from './graph.js';
import type { ScopeRepository } from '../store/repository.js';
import type { ModelManager } from '../models/manager.js';
import { completeChat } from '../models/protocol.js';
import { isInternalReportEntry } from '../chat/types.js';
import type { HistoryEntry } from '../chat/types.js';

const EXTRACT_EVERY_MESSAGES = 20;
const TRANSCRIPT_WINDOW = 24;
const MAX_FACTS_PER_PASS = 8;
const RETRY_AFTER_MESSAGES = 10;

interface Observation {
  subject_id?: number;
  category?: string;
  text?: string;
  confidence?: number;
}

interface ExtractionResult {
  facts: Observation[];
  topics: string[];
  /** Refreshed 会话印象 (purpose/atmosphere/key people), '' = unchanged. */
  scopeImpression: string;
}

export class IntelCollector {
  private counters = new Map<string, number>();
  private queue: Promise<void> = Promise.resolve();
  private readonly running = new Set<string>();

  constructor(
    private readonly graph: RelationGraph,
    private readonly repo: ScopeRepository,
    private readonly models: ModelManager,
    /** Live bot self-id (may be adopted from events after startup). */
    private readonly selfIdOf: () => number,
    private readonly enabled = true,
  ) {}

  /** Count one real user message; fires an extraction when the window fills. */
  notify(scopeType: string, scopeId: string): void {
    if (!this.enabled) return;
    const scopeKey = `${scopeType}:${scopeId}`;
    const count = (this.counters.get(scopeKey) ?? 0) + 1;
    if (count < EXTRACT_EVERY_MESSAGES) {
      this.counters.set(scopeKey, count);
      return;
    }
    this.counters.set(scopeKey, 0);
    this.enqueue(scopeType, scopeId, scopeKey);
  }

  async drain(): Promise<void> {
    await this.queue;
  }

  private enqueue(scopeType: string, scopeId: string, scopeKey: string): void {
    if (this.running.has(scopeKey)) return; // one job per scope at a time
    this.running.add(scopeKey);
    this.queue = this.queue
      .then(() => this.extract(scopeType, scopeId, scopeKey))
      .catch((error) => {
        console.error(`[intel] extraction failed scope=${scopeKey}:`, error);
        // Retry sooner: re-arm the counter so RETRY_AFTER_MESSAGES more
        // messages trigger a fresh pass with a bigger window.
        this.counters.set(scopeKey, EXTRACT_EVERY_MESSAGES - RETRY_AFTER_MESSAGES);
      })
      .finally(() => {
        this.running.delete(scopeKey);
      });
  }

  private async extract(scopeType: string, scopeId: string, scopeKey: string): Promise<void> {
    if (!this.enabled) return;
    const selfId = this.selfIdOf();
    const history = this.repo
      .getMessages(scopeType, scopeId)
      .filter((entry) => !isInternalReportEntry(entry) && Number(entry.user_id) !== selfId && Number(entry.user_id) > 0)
      .slice(-TRANSCRIPT_WINDOW);
    if (history.length < TRANSCRIPT_WINDOW / 2) return;

    const result = await this.extractFromHistory(history);
    let stored = 0;
    for (const fact of result.facts.slice(0, MAX_FACTS_PER_PASS)) {
      const subject = Number(fact.subject_id);
      const text = String(fact.text ?? '').trim();
      if (!Number.isFinite(subject) || subject <= 0 || !text) continue;
      const added = this.graph.addFact({
        subject,
        category: normalizeCategory(fact.category),
        text,
        sourceScope: scopeKey,
        sourceTool: 'auto_extract',
        confidence: Number.isFinite(Number(fact.confidence)) ? Number(fact.confidence) : undefined,
      });
      if (added !== null) stored += 1;
    }
    if (result.topics.length > 0) this.graph.upsertScopeTopics(scopeKey, result.topics);
    if (result.scopeImpression) this.graph.setScopeImpression(scopeKey, result.scopeImpression);
    if (stored > 0 || result.topics.length > 0 || result.scopeImpression) {
      console.info(`[intel] scope=${scopeKey} stored ${stored} facts, ${result.topics.length} topics${result.scopeImpression ? ', impression refreshed' : ''}`);
    }
  }

  private async extractFromHistory(history: HistoryEntry[]): Promise<ExtractionResult> {
    const participants = new Map<number, string>();
    for (const entry of history) {
      if (!participants.has(Number(entry.user_id))) {
        participants.set(Number(entry.user_id), String(entry.nickname ?? entry.user_id));
      }
    }
    const roster = [...participants.entries()]
      .map(([id, name]) => `${id}=${name}`)
      .join('，');
    const transcript = history
      .map((entry) => `${entry.nickname}(${entry.user_id}): ${String(entry.text ?? '').trim()}`)
      .join('\n')
      .slice(0, 16_000);

    const reply = await this.callModel([
      '你是聊天情报分析器。分析下面的聊天记录，提取值得长期记住的观察，输出纯 JSON（不要 markdown 代码块、不要解释）。',
      '格式：{"facts":[{"subject_id":123,"category":"preference","text":"…","confidence":0.8}],"topics":["话题1","话题2"],"scope_impression":"…"}',
      '规则：',
      `- subject_id 必须是参与者的 QQ 号。参与者名单：${roster}`,
      `- category 只能是：${FACT_CATEGORIES.join('/')}`,
      '- text 是一句话客观陈述，中文，不超过 60 字，包含具体锚点（名字/数字/时间），不要主观臆测',
      '- 只提取稳定、有长期价值的：身份信息、喜好偏好、人物关系、重要事件、情绪倾向；闲聊水话、一次性的玩笑、你自己都不确定的一律不要',
      '- confidence 0~1：直接说出口的 0.9，行为推断的 0.6~0.8，猜测低于 0.5 的直接丢弃',
      '- topics 是本会话最近常聊话题，2~4 个词',
      '- scope_impression 是对这个会话本身的整体印象（用途/氛围/关键人物/说话风格），一句话 60 字内；只有当你能写出比"普通闲聊"更具体的判断时才输出，否则输出空字符串',
      '- 没有值得提取的就输出 {"facts":[],"topics":[],"scope_impression":""}',
      '',
      '聊天记录：',
      transcript,
    ].join('\n'));
    return parseExtraction(reply);
  }

  private async callModel(prompt: string): Promise<string> {
    const model = this.models.getModelForRole('summary');
    if (model === null) throw new Error('没有可用的情报模型（roles.summary 未配置且无法回退到 main）');
    const reply = await completeChat(model, {
      system: [{ text: '你是严谨的信息抽取工具，只输出 JSON，不输出任何其他内容。' }],
      messages: [{ role: 'user', content: prompt }],
      tools: [],
      temperature: 0.2,
      maxTokens: 1024,
      thinkingLevel: 'off',
    });
    const text = reply.text.trim();
    if (!text) throw new Error('情报模型返回空内容');
    return text;
  }
}

function normalizeCategory(value: unknown): FactCategory {
  const name = String(value ?? '').trim().toLowerCase();
  return (FACT_CATEGORIES as readonly string[]).includes(name) ? (name as FactCategory) : 'other';
}

/** Lenient JSON extraction: strips code fences, finds the outermost object. */
export function parseExtraction(raw: string): ExtractionResult {
  let text = String(raw ?? '').trim();
  text = text.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/i, '');
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) return { facts: [], topics: [], scopeImpression: '' };
  try {
    const parsed = JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>;
    const facts = Array.isArray(parsed.facts) ? (parsed.facts as Observation[]) : [];
    const topics = Array.isArray(parsed.topics)
      ? (parsed.topics as unknown[]).map((topic) => String(topic)).filter((topic) => topic.trim() !== '')
      : [];
    const scopeImpression = String(parsed.scope_impression ?? '').trim().slice(0, 300);
    return { facts, topics: topics.slice(0, 6), scopeImpression };
  } catch {
    return { facts: [], topics: [], scopeImpression: '' };
  }
}
