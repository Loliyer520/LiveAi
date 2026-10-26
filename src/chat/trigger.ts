/**
 * Trigger decision + group-reply debounce windows.
 * Ported from legacy _should_trigger / _arm_group_reply_window /
 * _group_reply_debounce_runner.
 *
 * Trigger order (first hit wins):
 *   1. private chat → always reply (system sources filtered upstream)
 *   2. @bot mention → always reply
 *   3. agent trigger words → always reply
 *   4. random sample < agent.triggerRate (default 0.01, self-adjustable
 *      via the trigger_config tool)
 *
 * Debounce: when a group message does NOT trigger, a 60s listen window arms
 * (or refreshes). If the group keeps chatting and then goes silent for 5s
 * while the scope is idle, a synthetic "please continue naturally" message is
 * submitted — that's what made the bot feel alive in groups without @ing.
 */

import type { ChatMessage, SourceKind } from '../chat/types.js';
import { sourceKindOf } from './source.js';

export interface TriggerAgentConfig {
  triggerWords: string[];
  triggerRate: number;
  /** 子AI 总开关：false 时任何消息（含 @ 和触发词）都不触发生成。 */
  enabled?: boolean;
}

export interface GroupReplyWindow {
  armedAt: number;
  lastMessageTime: number;
  epoch: number;
  scopeId: string;
  timer: ReturnType<typeof setTimeout> | null;
}

export interface DebounceHooks {
  isEpochStale(epoch: number): boolean;
  isScopeBusy(scopeKey: string): boolean;
  fireTrigger(scopeKey: string, scopeId: string, epoch: number): void;
}

const LISTEN_WINDOW_MS = 60_000;
const DEBOUNCE_SILENCE_MS = 5_000;
const POLL_INTERVAL_MS = 1_000;

export class GroupReplyWindows {
  private readonly windows = new Map<string, GroupReplyWindow>();

  constructor(private readonly hooks: DebounceHooks) {}

  /** Refresh/create the listen window for a group scope after any message. */
  touch(scopeKey: string, scopeId: string, epoch: number): void {
    const existing = this.windows.get(scopeKey);
    if (existing) {
      existing.lastMessageTime = Date.now();
      return;
    }
    this.arm(scopeKey, scopeId, epoch);
  }

  private arm(scopeKey: string, scopeId: string, epoch: number): void {
    const now = Date.now();
    const window: GroupReplyWindow = {
      armedAt: now,
      lastMessageTime: now,
      epoch,
      scopeId,
      timer: null,
    };
    window.timer = setInterval(() => this.tick(scopeKey, window), POLL_INTERVAL_MS);
    // Do not keep the process alive just for debounce polling.
    window.timer.unref?.();
    this.windows.set(scopeKey, window);
  }

  private tick(scopeKey: string, window: GroupReplyWindow): void {
    if (this.hooks.isEpochStale(window.epoch) || this.windows.get(scopeKey) !== window) {
      this.dispose(scopeKey, window);
      return;
    }
    const now = Date.now();
    if (now - window.armedAt >= LISTEN_WINDOW_MS) {
      this.dispose(scopeKey, window);
      return;
    }
    if (
      window.lastMessageTime > window.armedAt &&
      now - window.lastMessageTime >= DEBOUNCE_SILENCE_MS
    ) {
      if (this.hooks.isEpochStale(window.epoch)) {
        this.dispose(scopeKey, window);
        return;
      }
      this.dispose(scopeKey, window);
      if (!this.hooks.isScopeBusy(scopeKey)) {
        this.hooks.fireTrigger(scopeKey, window.scopeId, window.epoch);
      }
    }
  }

  private dispose(scopeKey: string, window: GroupReplyWindow): void {
    if (window.timer !== null) clearInterval(window.timer);
    if (this.windows.get(scopeKey) === window) this.windows.delete(scopeKey);
  }

  /** Drop a scope's window (e.g. right after the bot spoke — no instant re-trigger). */
  cancel(scopeKey: string): void {
    const window = this.windows.get(scopeKey);
    if (window) this.dispose(scopeKey, window);
  }

  cancelAll(): void {
    for (const [key, window] of this.windows) this.dispose(key, window);
  }
}

export function shouldTrigger(
  message: ChatMessage,
  cleaned: string,
  agent: TriggerAgentConfig,
  log: (line: string) => void = () => {},
): boolean {
  if (agent.enabled === false) return false;
  const kind: SourceKind = sourceKindOf(message);
  // 内部任务（闹钟/周期任务/主AI委派/请求事件）在任何会话都必须触发——
  // 否则群里的闹钟和委派 99% 被随机率拦掉，任务形同虚设。
  if (kind === 'internal_task') return true;
  if (message.chatType === 'private') {
    if (kind === 'system_private') return false;
    return true;
  }
  if (message.mentionsSelf) {
    log(`[trigger] mentions_self scope=${message.chatType}:${message.chatId}`);
    return true;
  }
  const lowered = cleaned.toLowerCase();
  const word = agent.triggerWords.find((candidate) => candidate.toLowerCase().trim() && lowered.includes(candidate.toLowerCase()));
  if (word !== undefined) {
    log(`[trigger] trigger word "${word}" scope=${message.chatType}:${message.chatId}`);
    return true;
  }
  const hit = Math.random() < agent.triggerRate;
  if (hit) log(`[trigger] random rate=${agent.triggerRate.toFixed(2)} scope=${message.chatType}:${message.chatId}`);
  return hit;
}

/** Synthetic continuation message for the debounce path. */
export function buildDebounceTriggerMessage(scopeId: string, epoch: number, selfId: number): ChatMessage {
  const text =
    '（连续对话触发：群里沉默了5秒。先看看最后的话题——如果你刚刚已经回复过、没有新内容可接，直接 stay_silent；'
    + '有别人抛出了新话题或你确实有想补充的，再用 send_message 自然接一句。不要重复自己已经说过的话。）';
  return {
    chatType: 'group',
    chatId: Number(scopeId),
    userId: 0,
    text,
    rawMessage: '',
    sender: { nickname: '系统', user_id: 0 },
    messageId: `sys_debounce_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    mentionsSelf: true,
    timestamp: Date.now() / 1000,
    rawData: { source: 'group_reply_debounce', _epoch: epoch, _selfId: selfId },
  };
}
