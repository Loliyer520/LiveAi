/**
 * 请求次数与 token 用量统计（面板「状态」卡的数据源）。
 *
 * 内存自启动累计，重启清零——要做长期账单得落盘，但面板要的是
 * 「这波跑得怎么样 / 烧了多少」，随 uptime 看正合适。
 * completeChatTracked 是 completeChat 的统计包装：成功记 token、
 * 失败记次数与最近一条错误（余额/限流类事故一眼能看到）。
 */

import type { ChatReply, ChatRequest, ResolvedModel } from './types.js';
import { completeChat } from './protocol.js';

export interface UsageBucket {
  requests: number;
  failures: number;
  inputTokens: number;
  outputTokens: number;
  /** 提示词缓存命中读到的 token（便宜的那部分）。 */
  cacheReadTokens: number;
  /** 提示词缓存写入的 token。 */
  cacheWriteTokens: number;
}

export interface UsageSnapshot {
  /** 统计起点（进程启动时刻，epoch ms）。 */
  readonly since: number;
  readonly total: UsageBucket;
  readonly byRole: Record<string, UsageBucket>;
  readonly byModel: Record<string, UsageBucket>;
  readonly lastError: { readonly at: number; readonly role: string; readonly message: string } | null;
}

function emptyBucket(): UsageBucket {
  return { requests: 0, failures: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 };
}

export class UsageTracker {
  private since = Date.now();
  private total = emptyBucket();
  private roles = new Map<string, UsageBucket>();
  private models = new Map<string, UsageBucket>();
  private lastError: UsageSnapshot['lastError'] = null;

  recordSuccess(role: string, modelName: string, reply: ChatReply): void {
    const input = reply.usage?.inputTokens ?? 0;
    const output = reply.usage?.outputTokens ?? 0;
    const cacheRead = reply.usage?.cacheReadTokens ?? 0;
    const cacheWrite = reply.usage?.cacheWriteTokens ?? 0;
    for (const bucket of this.bucketsFor(role, modelName)) {
      bucket.requests += 1;
      bucket.inputTokens += input;
      bucket.outputTokens += output;
      bucket.cacheReadTokens += cacheRead;
      bucket.cacheWriteTokens += cacheWrite;
    }
  }

  recordFailure(role: string, modelName: string, error: unknown): void {
    for (const bucket of this.bucketsFor(role, modelName)) {
      bucket.requests += 1;
      bucket.failures += 1;
    }
    const message = (error instanceof Error ? error.message : String(error)).slice(0, 300);
    this.lastError = { at: Date.now(), role, message };
  }

  snapshot(): UsageSnapshot {
    const copy = (bucket: UsageBucket): UsageBucket => ({ ...bucket });
    return {
      since: this.since,
      total: copy(this.total),
      byRole: Object.fromEntries([...this.roles].map(([k, v]) => [k, copy(v)])),
      byModel: Object.fromEntries([...this.models].map(([k, v]) => [k, copy(v)])),
      lastError: this.lastError,
    };
  }

  /** 测试用：清零重来。 */
  reset(): void {
    this.since = Date.now();
    this.total = emptyBucket();
    this.roles.clear();
    this.models.clear();
    this.lastError = null;
  }

  private bucketsFor(role: string, modelName: string): UsageBucket[] {
    let roleBucket = this.roles.get(role);
    if (roleBucket === undefined) {
      roleBucket = emptyBucket();
      this.roles.set(role, roleBucket);
    }
    let modelBucket = this.models.get(modelName);
    if (modelBucket === undefined) {
      modelBucket = emptyBucket();
      this.models.set(modelName, modelBucket);
    }
    return [this.total, roleBucket, modelBucket];
  }
}

/** 压缩成 k/M 的人类可读 token 数。 */
function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function fmtBucket(b: UsageBucket): string {
  const base = `${b.requests} 次${b.failures > 0 ? `（失败 ${b.failures}）` : ''} · 输入 ${fmtTokens(b.inputTokens)} · 输出 ${fmtTokens(b.outputTokens)}`;
  const looked = b.inputTokens + b.cacheReadTokens;
  if (b.cacheReadTokens <= 0 && b.cacheWriteTokens <= 0) return base;
  const hitRate = looked > 0 ? Math.round((b.cacheReadTokens / looked) * 100) : 0;
  return `${base} · 缓存命中 ${fmtTokens(b.cacheReadTokens)}（${hitRate}%）· 缓存写入 ${fmtTokens(b.cacheWriteTokens)}`;
}

/** 给 AI 看的纯文本用量报告（usage_stats 工具返回体）。 */
export function formatUsageText(snapshot: UsageSnapshot): string {
  const lines: string[] = [];
  const since = new Date(snapshot.since);
  const pad = (n: number): string => String(n).padStart(2, '0');
  lines.push(`统计起点 ${since.getFullYear()}-${pad(since.getMonth() + 1)}-${pad(since.getDate())} ${pad(since.getHours())}:${pad(since.getMinutes())}（本次启动至今）`);
  if (snapshot.total.requests === 0) {
    lines.push('还没有任何模型请求。');
    return lines.join('\n');
  }
  lines.push(`合计：${fmtBucket(snapshot.total)}`);
  for (const [role, bucket] of Object.entries(snapshot.byRole)) {
    lines.push(`角色 ${role}：${fmtBucket(bucket)}`);
  }
  for (const [model, bucket] of Object.entries(snapshot.byModel)) {
    lines.push(`模型 ${model}：${fmtBucket(bucket)}`);
  }
  if (snapshot.lastError !== null) {
    const at = new Date(snapshot.lastError.at);
    lines.push(`最近错误（${snapshot.lastError.role} ${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}）：${snapshot.lastError.message}`);
  }
  return lines.join('\n');
}

/** 进程级单例：各调用点与面板共享同一份计数。 */
export const usageStats = new UsageTracker();

/** completeChat 的统计包装：一次逻辑请求记一条（内部重试不重复计）。 */
export async function completeChatTracked(
  role: string,
  model: ResolvedModel,
  request: ChatRequest,
): Promise<ChatReply> {
  try {
    const reply = await completeChat(model, request);
    usageStats.recordSuccess(role, model.displayName, reply);
    return reply;
  } catch (error) {
    usageStats.recordFailure(role, model.displayName, error);
    throw error;
  }
}
