/**
 * 统一探针框架（ROADMAP P0）——把 MC3D 验证过的「分段计时 + 慢帧现场」
 * 探针模式抽象成框架能力：任何代码路径都能 record/withProbe 打点，
 * 面板（性能页）统一看慢调用 TOP 榜和最近事件。
 *
 * 用法：
 *   import { withProbe, probeRecord } from '../observability/probe.js';
 *   const result = await withProbe('chat.turn', turn, { scope: scopeKey });
 *   probeRecord('llm.call', ms, { model });
 *
 * 设计约束：
 *  - 零分配热路径之外不阻塞：纯内存环形缓冲，无 I/O；
 *  - 不抛错：探针自己出问题绝不影响被测路径（MC3D 教训——诊断探针
 *    也会抢定时器/拖慢宿主）；
 *  - 聚合按 name：count/avg/max/最近一次；慢事件（> SLOW_MS）单独留现场。
 */

export interface ProbeSpan {
  name: string;
  ms: number;
  at: number;
  meta?: Record<string, string | number>;
}

export interface ProbeAggregate {
  name: string;
  count: number;
  totalMs: number;
  avgMs: number;
  maxMs: number;
  lastAt: number;
}

export interface ProbeSnapshot {
  aggregates: ProbeAggregate[];
  slow: ProbeSpan[];
  recent: ProbeSpan[];
  slowThresholdMs: number;
  startedAt: number;
}

const MAX_SLOW = 200;
const MAX_RECENT = 300;
/** 超过该耗时进入慢事件现场（可按需调）。 */
const SLOW_MS = 1500;

const aggregates = new Map<string, { count: number; totalMs: number; maxMs: number; lastAt: number }>();
const slowSpans: ProbeSpan[] = [];
const recentSpans: ProbeSpan[] = [];
const startedAt = Date.now();

export function probeRecord(name: string, ms: number, meta?: Record<string, string | number>): void {
  try {
    const at = Date.now();
    const agg = aggregates.get(name) ?? { count: 0, totalMs: 0, maxMs: 0, lastAt: at };
    agg.count += 1;
    agg.totalMs += ms;
    if (ms > agg.maxMs) agg.maxMs = ms;
    agg.lastAt = at;
    aggregates.set(name, agg);
    const span: ProbeSpan = { name, ms, at, ...(meta !== undefined ? { meta } : {}) };
    recentSpans.push(span);
    if (recentSpans.length > MAX_RECENT) recentSpans.splice(0, recentSpans.length - MAX_RECENT);
    if (ms >= SLOW_MS) {
      slowSpans.push(span);
      if (slowSpans.length > MAX_SLOW) slowSpans.splice(0, slowSpans.length - MAX_SLOW);
    }
  } catch {
    // 探针永不影响业务路径
  }
}

/** 计时包装：fn 的耗时自动打点；fn 抛错也记录后再抛。 */
export async function withProbe<T>(name: string, fn: () => Promise<T>, meta?: Record<string, string | number>): Promise<T> {
  const started = Date.now();
  try {
    return await fn();
  } finally {
    probeRecord(name, Date.now() - started, meta);
  }
}

export function probeSnapshot(): ProbeSnapshot {
  const list: ProbeAggregate[] = [...aggregates.entries()].map(([name, agg]) => ({
    name,
    count: agg.count,
    totalMs: Math.round(agg.totalMs),
    avgMs: Math.round((agg.totalMs / agg.count) * 10) / 10,
    maxMs: agg.maxMs,
    lastAt: agg.lastAt,
  }));
  list.sort((a, b) => b.maxMs - a.maxMs);
  return {
    aggregates: list.slice(0, 30),
    slow: slowSpans.slice(-50).reverse(),
    recent: recentSpans.slice(-50).reverse(),
    slowThresholdMs: SLOW_MS,
    startedAt,
  };
}

export function probeReset(): void {
  aggregates.clear();
  slowSpans.length = 0;
  recentSpans.length = 0;
}
