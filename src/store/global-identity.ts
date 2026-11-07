/**
 * 全局共同体记忆：跨会话共享的「自我情报」——订阅制。
 *
 * 设计原则（主人定的）：各会话上下文独立、情报高度共享；但共享要照顾两件事：
 *   1. 缓存命中：拼进 prompt 的动态行必须低频变化（只随上下线这种事件变），
 *      坐标/血量这类每回合都变的状态一律走工具按需查，不打爆前缀缓存。
 *   2. 按需推送：每个 AI 自己订阅/退订频道。没订阅的会话上下文里完全不出现
 *      该频道（绘画 AI 不用惦记 MC）；被提起了再订阅 + 查事件留痕补课。
 *
 * 数据落 data/state/global_identity.json：
 *   lines        静态行（主AI 经 identity_write 维护，所有会话恒可见，低频变化）
 *   subscriptions 各会话订阅的频道（scopeKey → channel set）
 * events 只留内存环形缓冲（重启清零）——它是「最近推送记录」，不是账本。
 */

import { readFile, writeFile, rename } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';

export interface IdentityLine {
  id: string;
  text: string;
  source: string;
  updatedAt: number;
}

export interface IdentityEvent {
  at: number;
  channel: string;
  text: string;
}

interface IdentityFile {
  lines: IdentityLine[];
  subscriptions: Record<string, string[]>;
}

/** 频道注册项：当前状态（订阅者注入用，须低频变化）+ 人类可读说明。 */
export interface IdentityChannel {
  name: string;
  description: string;
  status: () => string;
}

const MAX_LINES = 40;
const MAX_TEXT = 500;
const MAX_SUBSCRIPTIONS = 500;
const MAX_EVENTS = 200;

let identityPath: string | null = null;
let cache: IdentityFile | null = null;
/** 读多写少；事件环形缓冲仅内存。 */
let eventLog: IdentityEvent[] = [];
const channels = new Map<string, IdentityChannel>();

/** chat-module 启动时指定落盘位置；测试可指向临时目录。 */
export function configureGlobalIdentity(path: string): void {
  identityPath = path;
  cache = null;
}

/** 注册频道（如 mc）。status() 返回的文本只应随低频事件变化（缓存命中）。 */
export function registerIdentityChannel(channel: IdentityChannel): void {
  channels.set(channel.name, channel);
}

export function identityChannelList(): { name: string; description: string; status: () => string }[] {
  return [...channels.values()].map(({ name, description, status }) => ({ name, description, status }));
}

async function load(): Promise<IdentityFile> {
  if (cache !== null) return cache;
  if (identityPath === null) return { lines: [], subscriptions: {} };
  try {
    const raw = JSON.parse(await readFile(identityPath, 'utf-8')) as Partial<IdentityFile>;
    cache = {
      lines: Array.isArray(raw.lines)
        ? raw.lines.filter((l): l is IdentityLine => typeof l?.text === 'string' && l.text.trim() !== '')
        : [],
      subscriptions: raw.subscriptions && typeof raw.subscriptions === 'object'
        ? Object.fromEntries(
            Object.entries(raw.subscriptions)
              .map(([k, v]) => [k, Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []] as const),
          )
        : {},
    };
  } catch {
    cache = { lines: [], subscriptions: {} };
  }
  return cache;
}

async function persist(data: IdentityFile): Promise<void> {
  cache = data;
  if (identityPath === null) return;
  const tmp = join(dirname(identityPath), `.global_identity.${randomUUID()}.tmp`);
  await writeFile(tmp, JSON.stringify(data, null, 1), 'utf-8');
  await rename(tmp, identityPath);
}

// ── 静态行 ─────────────────────────────────────────────────────────────────

export async function listIdentityLines(): Promise<IdentityLine[]> {
  return [...(await load()).lines];
}

export async function addIdentityLine(text: string, source: string): Promise<IdentityLine> {
  const trimmed = text.trim().slice(0, MAX_TEXT);
  if (trimmed === '') throw new Error('内容为空');
  const data = await load();
  if (data.lines.length >= MAX_LINES) throw new Error(`已满 ${MAX_LINES} 条，先删几条再加`);
  const line: IdentityLine = { id: `gi_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`, text: trimmed, source, updatedAt: Date.now() };
  await persist({ ...data, lines: [...data.lines, line] });
  return line;
}

export async function removeIdentityLine(id: string): Promise<boolean> {
  const data = await load();
  const next = data.lines.filter((l) => l.id !== id);
  if (next.length === data.lines.length) return false;
  await persist({ ...data, lines: next });
  return true;
}

// ── 订阅 ───────────────────────────────────────────────────────────────────

export async function subscriptionsFor(scopeKey: string): Promise<string[]> {
  return [...((await load()).subscriptions[scopeKey] ?? [])];
}

export async function setSubscription(scopeKey: string, channel: string, on: boolean): Promise<string> {
  if (!channels.has(channel)) {
    return `没有 ${channel} 频道。可用频道：${[...channels.keys()].join('、') || '（无）'}。`;
  }
  const data = await load();
  const current = new Set(data.subscriptions[scopeKey] ?? []);
  if (on) current.add(channel);
  else current.delete(channel);
  const subscriptions = { ...data.subscriptions, [scopeKey]: [...current] };
  if (subscriptions[scopeKey]!.length === 0) delete subscriptions[scopeKey];
  if (Object.keys(subscriptions).length > MAX_SUBSCRIPTIONS) {
    // 防脏数据无限膨胀：最早的会话订阅整体丢弃。
    const keys = Object.keys(data.subscriptions);
    for (const k of keys.slice(0, Object.keys(subscriptions).length - MAX_SUBSCRIPTIONS)) delete subscriptions[k];
  }
  await persist({ ...data, subscriptions });
  return on ? `已订阅 ${channel}：从下一轮起，你的上下文会持续带上它的当前状态。` : `已退订 ${channel}。`;
}

// ── 事件留痕（推送记录） ────────────────────────────────────────────────────

/** 频道状态发生值得记住的变化时调（上下线、重要节点）。 */
export function recordIdentityEvent(channel: string, text: string): void {
  const trimmed = text.trim();
  if (trimmed === '') return;
  eventLog.push({ at: Date.now(), channel, text: trimmed.slice(0, MAX_TEXT) });
  if (eventLog.length > MAX_EVENTS) eventLog = eventLog.slice(-MAX_EVENTS);
}

/** 查某频道最近的推送记录；不带 channel=全部。 */
export function identityEvents(channel?: string, limit = 20): IdentityEvent[] {
  const capped = Math.min(Math.max(limit, 1), 100);
  return eventLog.filter((e) => channel === undefined || e.channel === channel).slice(-capped).reverse();
}

// ── 拼卡 ───────────────────────────────────────────────────────────────────

/**
 * 拼出写进该会话 prompt 的「全局共同体记忆」段。
 * 静态行人人可见；动态行只给订阅了对应频道的会话——没订阅就完全不注入，
 * 既省 token 也保缓存。status() 低频变化，订阅者的缓存不受高频状态影响。
 */
export async function globalIdentityCard(scopeKey: string): Promise<string> {
  const data = await load();
  const subscribed = new Set(data.subscriptions[scopeKey] ?? []);
  const parts: string[] = [];
  for (const channel of channels.values()) {
    if (!subscribed.has(channel.name)) continue;
    const text = channel.status().trim();
    if (text !== '') parts.push(text);
  }
  parts.push(...data.lines.map((l) => l.text));
  if (parts.length === 0) return '';
  return parts.join('\n');
}
