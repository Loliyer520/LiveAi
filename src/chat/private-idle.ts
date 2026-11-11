import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export interface PrivateIdleState {
  mode: 'idle' | 'sleep' | 'forever' | 'deciding';
  lastActivityAt: number;
  wakeAt: number | null;
  revision: number;
  reason: string;
}

const MAX_TIMER_MS = 2 ** 31 - 1;

/** Durable per-private-chat quiet windows; never sends a message itself. */
export class PrivateIdle {
  private readonly states = new Map<string, PrivateIdleState>();
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>();
  private running = false;

  constructor(
    private readonly path: string,
    private readonly wake: (scopeKey: string, revision: number) => boolean,
    private readonly idleMs = 120_000,
  ) {
    try {
      const raw = JSON.parse(readFileSync(path, 'utf8')) as { scopes: Record<string, PrivateIdleState> };
      for (const [key, value] of Object.entries(raw.scopes ?? {})) {
        if (!/^private:\d+$/.test(key) || !value || !['idle', 'sleep', 'forever', 'deciding'].includes(value.mode)
          || !Number.isFinite(value.lastActivityAt) || !Number.isSafeInteger(value.revision)
          || (value.mode === 'forever' ? value.wakeAt !== null : !Number.isFinite(value.wakeAt))) continue;
        this.states.set(key, value);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
  }

  start(): void {
    this.running = true;
    for (const [key, value] of this.states) {
      // A process died during a decision: resume it once, never leave it stuck.
      if (value.mode === 'deciding') {
        value.mode = 'idle';
        value.wakeAt = Date.now();
        value.revision += 1;
      }
      this.arm(key);
    }
    if (this.states.size) this.persist();
  }

  stop(): void {
    this.running = false;
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
  }

  /** Existing chats get a startup grace window, without overriding saved sleep. */
  enroll(scopeKey: string): void {
    if (!this.states.has(scopeKey)) this.activity(scopeKey, true);
  }

  activity(scopeKey: string, incoming: boolean): void {
    if (this.idleMs <= 0 || !/^private:\d+$/.test(scopeKey)) return;
    const previous = this.states.get(scopeKey);
    // Late self-echoes must not undo a sleep chosen just after sending.
    if (!incoming && previous && (previous.mode === 'sleep' || previous.mode === 'forever')) return;
    this.states.set(scopeKey, {
      mode: 'idle', lastActivityAt: Date.now(), wakeAt: Date.now() + this.idleMs,
      revision: (previous?.revision ?? 0) + 1, reason: '',
    });
    try { this.persist(); } finally { this.arm(scopeKey); }
  }

  sleep(scopeKey: string, wakeAt: number | null, reason: string): void {
    if (!/^private:\d+$/.test(scopeKey)) throw new Error('sleep 仅限私聊');
    if (wakeAt !== null && (!Number.isFinite(wakeAt) || wakeAt <= Date.now())) throw new Error('唤醒时间必须在未来');
    const previous = this.states.get(scopeKey);
    this.states.set(scopeKey, {
      mode: wakeAt === null ? 'forever' : 'sleep', wakeAt,
      lastActivityAt: previous?.lastActivityAt ?? Date.now(),
      revision: (previous?.revision ?? 0) + 1, reason: reason.slice(0, 300),
    });
    try { this.persist(); } catch (error) {
      if (previous) this.states.set(scopeKey, previous);
      else this.states.delete(scopeKey);
      throw error;
    }
    this.arm(scopeKey);
  }

  get(scopeKey: string): PrivateIdleState | undefined {
    const value = this.states.get(scopeKey);
    return value ? { ...value } : undefined;
  }

  isCurrent(scopeKey: string, revision: number): boolean {
    return this.states.get(scopeKey)?.revision === revision;
  }

  finish(scopeKey: string, revision: number, acted: boolean): void {
    const state = this.states.get(scopeKey);
    if (!state || state.revision !== revision || state.mode !== 'deciding') return;
    // Protocol failure must not cause an endless two-minute model loop.
    if (!acted) this.sleep(scopeKey, Date.now() + 3_600_000, '防死寂决策未选择行动或 sleep，故障退避一小时');
    else this.activity(scopeKey, false);
  }

  private arm(scopeKey: string, retryMs?: number): void {
    const previous = this.timers.get(scopeKey);
    if (previous) clearTimeout(previous);
    this.timers.delete(scopeKey);
    const state = this.states.get(scopeKey);
    if (!this.running || this.idleMs <= 0 || !state || state.wakeAt === null || state.mode === 'deciding') return;
    const revision = state.revision;
    const timer = setTimeout(() => {
      this.timers.delete(scopeKey);
      const current = this.states.get(scopeKey);
      if (!this.running || !current || current.revision !== revision) return;
      if (current.wakeAt! > Date.now()) { this.arm(scopeKey); return; }
      const oldMode = current.mode;
      current.mode = 'deciding';
      try {
        this.persist();
        if (this.wake(scopeKey, revision)) return;
      } catch (error) { console.error('[private-idle] wake failed:', error); }
      current.mode = oldMode;
      this.arm(scopeKey, 5_000);
    }, retryMs ?? Math.min(MAX_TIMER_MS, Math.max(0, state.wakeAt - Date.now())));
    timer.unref?.();
    this.timers.set(scopeKey, timer);
  }

  private persist(): void {
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify({ scopes: Object.fromEntries(this.states) }, null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

/** Relative intervals or ISO-8601 instants with an explicit timezone. */
export function parseSleepUntil(value: string, now = Date.now()): number | null | undefined {
  const text = value.trim().toLowerCase();
  if (text === 'forever') return null;
  const relative = /^\+?(\d+(?:\.\d+)?)\s*(s|m|h|d)$/.exec(text);
  let at: number;
  if (relative) {
    at = now + Number(relative[1]) * ({ s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 }[relative[2]]!);
  } else if (/^\d{10}(?:\.\d+)?$/.test(text)) at = Number(text) * 1000;
  else if (/^\d{4}-\d{2}-\d{2}t.*(?:z|[+-]\d{2}:\d{2})$/.test(text)) at = Date.parse(value.trim());
  else return undefined;
  return Number.isFinite(at) && at <= 8.64e15 && at >= now + 1000 ? at : undefined;
}
