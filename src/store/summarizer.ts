/**
 * Background diary summarizer — the openclaw compaction counterpart.
 *
 * Two layers:
 *  1. segment summaries: every filled 50-message diary segment is condensed
 *     by the model into a short objective summary (retry-safe: the raw
 *     segment stays queued until its summary lands);
 *  2. overall summary: a rolling "会话整体梗概" rebuilt from the previous
 *     primer + new segment summaries every time ≥3 fresh segments exist,
 *     so the background prompt always carries long-term coherence without
 *     stuffing every segment into the context.
 *
 * Runs out-of-band: never blocks a chat turn, one job at a time, failures
 * leave the queue intact for the next trigger (and the segment bound in the
 * repository guarantees a mechanical fallback if the model stays down).
 */

import type { ScopeRepository } from './repository.js';
import type { ModelManager } from '../models/manager.js';
import { completeChat } from '../models/protocol.js';
import { formatArchiveLine } from './archive.js';
import type { HistoryEntry } from '../chat/types.js';

const OVERALL_REFRESH_SEGMENTS = 3;
const SUMMARY_MAX_CHARS = 360;

export class DiarySummarizer {
  private queue: Promise<void> = Promise.resolve();
  private readonly scopesSeen = new Set<string>();

  constructor(
    private readonly repo: ScopeRepository,
    private readonly models: ModelManager,
    private readonly enabled = true,
  ) {}

  /** Called after appendMessage rolled a segment; also a fine retry hook. */
  notify(scopeType: string, scopeId: string): void {
    if (!this.enabled) return;
    this.enqueue(scopeType, scopeId);
  }

  /** Startup recovery: re-attempt scopes with unsummarized segments. */
  async drainKnownScopes(): Promise<void> {
    for (const scopeKey of this.scopesSeen) {
      const separator = scopeKey.indexOf(':');
      this.enqueue(scopeKey.slice(0, separator), scopeKey.slice(separator + 1));
    }
    await this.queue;
  }

  private enqueue(scopeType: string, scopeId: string): void {
    this.scopesSeen.add(`${scopeType}:${scopeId}`);
    this.queue = this.queue
      .then(() => this.run(scopeType, scopeId))
      .catch((error) => {
        console.error('[summarizer] job failed:', error);
      });
  }

  private async run(scopeType: string, scopeId: string): Promise<void> {
    if (!this.enabled) return;
    // 1) summarize every queued raw segment, oldest first
    const segments = this.repo.getUnsummarizedSegments(scopeType, scopeId);
    for (const segment of segments) {
      const text = await this.summarizeSegment(segment.messages);
      this.repo.storeDiarySummary(scopeType, scopeId, segment.index, text);
    }
    // 2) refresh the overall primer when enough new summaries landed
    const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
    const summaries = this.repo.getDiarySummaries(scopeType, scopeId);
    const fresh = summaries.length - agent.overallSummaryAtSegments;
    if (summaries.length > 0 && (agent.overallSummary === '' || fresh >= OVERALL_REFRESH_SEGMENTS)) {
      const overall = await this.summarizeOverall(agent.overallSummary, summaries.map((item) => item.text));
      this.repo.updateAgent(scopeType, scopeId, {
        overallSummary: overall,
        overallSummaryBuiltAt: Date.now() / 1000,
        overallSummaryAtSegments: summaries.length,
      });
    }
  }

  private async summarizeSegment(messages: HistoryEntry[]): Promise<string> {
    const transcript = messages
      .filter((entry) => String(entry.text ?? '').trim() !== '')
      .map((entry) => formatArchiveLine('', entry).replace(/^\[\] /, ''))
      .join('\n');
    const reply = await this.callModel(
      [
        '你是聊天记录摘要器。把下面这段聊天记录浓缩成一段客观摘要，供 AI 角色日后回忆这段过往使用。',
        '要求：',
        '- 中文，150~300 字，第三人称，一段话',
        '- 保留关键事件、约定、承诺、人物关系与情绪基调',
        '- 保留可检索的细节锚点：具体时间点、数字、名字、达成的结论',
        '- 不要流水账逐条复述，不要对话原文',
        '',
        '聊天记录：',
        transcript.slice(0, 24_000),
      ].join('\n'),
    );
    return reply.slice(0, SUMMARY_MAX_CHARS);
  }

  private async summarizeOverall(previous: string, segmentSummaries: string[]): Promise<string> {
    const reply = await this.callModel(
      [
        '你是聊天记忆整理器。现有一份旧的"会话整体梗概"和若干新增的分段摘要，请合并成一份新的整体梗概。',
        '要求：',
        '- 中文，200~400 字，第三人称',
        '- 旧梗概里仍然重要的信息要保留；新发生的事件、约定、关系变化要并入',
        '- 冲突时以新的为准；过时且不再相关的细节可以舍弃',
        '- 这份梗概会直接放进 AI 的提示词作为长期记忆，写成连贯叙述，不要列表',
        '',
        previous.trim()
          ? `旧整体梗概：\n${previous}`
          : '旧整体梗概：（暂无，这是第一次生成）',
        '',
        '分段摘要（从旧到新）：',
        segmentSummaries.map((text, index) => `【段${index + 1}】${text}`).join('\n').slice(0, 20_000),
      ].join('\n'),
    );
    return reply.slice(0, SUMMARY_MAX_CHARS * 1.5);
  }

  private async callModel(prompt: string): Promise<string> {
    const model = this.models.getModelForRole('summary');
    if (model === null) throw new Error('没有可用的摘要模型（roles.summary 未配置且无法回退到 main）');
    const reply = await completeChat(model, {
      system: [{ text: '你是一个严谨的聊天记录摘要工具，只输出摘要正文，不要任何前言、后缀或解释。' }],
      messages: [{ role: 'user', content: prompt }],
      tools: [],
      temperature: 0.3,
      maxTokens: 1024,
      thinkingLevel: 'off',
    });
    const text = reply.text.trim();
    if (!text) throw new Error('摘要模型返回空内容');
    return text;
  }
}
