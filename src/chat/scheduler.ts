/**
 * Task scheduler — persisted alarms + recurring tasks. Ported from the legacy
 * task subsystem's set_alarm / recurring halves:
 *
 *  - every task lives in ONE JSON file (atomic tmp+rename), so alarms survive
 *    restarts; on boot, overdue tasks fire immediately (catch-up) and future
 *    tasks are re-armed;
 *  - recurring tasks re-arm themselves after each successful delivery;
 *  - the scheduler never touches chat plumbing itself: firing calls the
 *    `fire` callback, the orchestrator injects an internal trigger message
 *    into the origin scope's actor.
 *
 * Timers are an implementation detail — the JSON file is the source of truth.
 * Delivery is at-least-once at this boundary: a callback exception/false keeps
 * the task pending and retries with backoff instead of silently losing it.
 */

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

export type TaskKind = 'set_alarm' | 'recurring' | 'send_image' | 'watchdog';
export type TaskStatus = 'pending' | 'done' | 'cancelled';

export interface ScheduledTask {
  taskId: string;
  kind: TaskKind;
  originScope: string;
  /** Alarm note / recurring instruction shown to the AI on fire. */
  note: string;
  /** Next fire time (epoch seconds). */
  runAt: number;
  /** watchdog only: 同 scope 同 tag 的新保底会顶掉旧的。 */
  tag?: string;
  /** recurring only: seconds between fires. */
  intervalSeconds?: number;
  /** send_image only: original URL key into the durable inbound image cache. */
  imageUrl?: string;
  status: TaskStatus;
  createdAt: number;
  lastFiredAt?: number;
}

interface TaskFile {
  tasks: ScheduledTask[];
}

/** setTimeout clamps at ~24.8 days; re-arm past that. */
const MAX_TIMER_MS = 2 ** 31 - 1;
const DELIVERY_RETRY_BASE_MS = 1_000;
const DELIVERY_RETRY_MAX_MS = 60_000;
const MAX_DELIVERY_BACKOFF_EXPONENT = 6;

export class TaskScheduler {
  private tasks = new Map<string, ScheduledTask>();
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly deliveryFailures = new Map<string, number>();
  private loaded = false;
  private running = false;

  constructor(
    private readonly path: string,
    private readonly fire: (task: ScheduledTask) => unknown,
  ) {}

  /** Load persisted tasks and arm timers; overdue pending tasks fire at once. */
  start(): void {
    if (this.running) return;
    this.load();
    this.running = true;
    const now = Date.now() / 1000;
    for (const task of this.tasks.values()) {
      if (task.status !== 'pending') continue;
      this.arm(task, now);
    }
  }

  /** Clear timers; persisted tasks survive for the next start(). */
  stop(): void {
    this.running = false;
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    this.deliveryFailures.clear();
  }

  scheduleAlarm(originScope: string, at: number, note: string): string {
    if (!Number.isFinite(at) || at < 0) throw new Error('闹钟时间必须是有限的 Unix 秒。');
    const task: ScheduledTask = {
      taskId: `alarm_${randomUUID().slice(0, 8)}`,
      kind: 'set_alarm',
      originScope,
      note: note.slice(0, 200),
      runAt: at,
      status: 'pending',
      createdAt: Date.now() / 1000,
    };
    this.add(task);
    return task.taskId;
  }

  scheduleImage(originScope: string, at: number, imageUrl: string): string {
    if (!Number.isFinite(at) || at < 0) throw new Error('定时发图时间必须是有限的 Unix 秒。');
    if (!/^https?:\/\//i.test(imageUrl)) throw new Error('定时发图需要有效的 http(s) 原图地址。');
    const task: ScheduledTask = {
      taskId: `image_${randomUUID().slice(0, 8)}`,
      kind: 'send_image',
      originScope,
      note: '到点原样发送指定图片',
      imageUrl,
      runAt: at,
      status: 'pending',
      createdAt: Date.now() / 1000,
    };
    this.add(task);
    return task.taskId;
  }

  /**
   * 回执超时保底：到点重新唤醒发起 scope 的 AI 自查。同 scope 同 tag 的
   * 旧保底自动作废（重新委派/续期时不会越积越多），不传 tag 用 "default"。
   */
  scheduleWatchdog(originScope: string, at: number, note: string, tag?: string): string {
    if (!Number.isFinite(at) || at < 0) throw new Error('保底时间必须是有限的 Unix 秒。');
    const effectiveTag = (tag ?? 'default').slice(0, 40);
    for (const existing of this.list(originScope)) {
      if (existing.kind === 'watchdog' && existing.tag === effectiveTag) this.cancel(existing.taskId);
    }
    const task: ScheduledTask = {
      taskId: `watch_${randomUUID().slice(0, 8)}`,
      kind: 'watchdog',
      originScope,
      note: note.slice(0, 300),
      runAt: at,
      tag: effectiveTag,
      status: 'pending',
      createdAt: Date.now() / 1000,
    };
    this.add(task);
    return task.taskId;
  }

  scheduleRecurring(originScope: string, intervalSeconds: number, instruction: string): string {
    if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0) {
      throw new Error('周期间隔必须是有限的正数秒。');
    }
    const now = Date.now() / 1000;
    const task: ScheduledTask = {
      taskId: `recur_${randomUUID().slice(0, 8)}`,
      kind: 'recurring',
      originScope,
      note: instruction.slice(0, 400),
      runAt: now + intervalSeconds,
      intervalSeconds,
      status: 'pending',
      createdAt: now,
    };
    this.add(task);
    return task.taskId;
  }

  cancel(taskId: string): boolean {
    this.load();
    const task = this.tasks.get(taskId);
    if (!task || task.status !== 'pending') return false;
    task.status = 'cancelled';
    const timer = this.timers.get(taskId);
    if (timer) clearTimeout(timer);
    this.timers.delete(taskId);
    this.deliveryFailures.delete(taskId);
    this.persist();
    return true;
  }

  /** Pending tasks, optionally filtered to one scope. */
  list(scope?: string): ScheduledTask[] {
    this.load();
    return [...this.tasks.values()]
      .filter((task) => task.status === 'pending')
      .filter((task) => scope === undefined || task.originScope === scope)
      .sort((a, b) => a.runAt - b.runAt);
  }

  find(taskId: string): ScheduledTask | null {
    this.load();
    return this.tasks.get(taskId) ?? null;
  }

  // ── internals ─────────────────────────────────────────────────────────────

  private add(task: ScheduledTask): void {
    this.load();
    this.tasks.set(task.taskId, task);
    this.persist();
    if (this.running) this.arm(task, Date.now() / 1000);
  }

  private arm(task: ScheduledTask, now: number): void {
    const delayMs = Math.max(0, (task.runAt - now) * 1000);
    this.armDelay(task.taskId, Math.min(delayMs, MAX_TIMER_MS));
  }

  private armDelay(taskId: string, delayMs: number): void {
    const previous = this.timers.get(taskId);
    if (previous) clearTimeout(previous);
    const timer = setTimeout(() => void this.onTimer(taskId), delayMs);
    timer.unref?.();
    this.timers.set(taskId, timer);
  }

  private async onTimer(taskId: string): Promise<void> {
    this.timers.delete(taskId);
    if (!this.running) return;
    const task = this.tasks.get(taskId);
    if (!task || task.status !== 'pending') return;
    const now = Date.now() / 1000;
    // A timer can wake early (and the wall clock can move backwards). Never
    // deliver before runAt; this also covers long-haul timer checkpoints.
    if (task.runAt > now) {
      this.arm(task, now);
      return;
    }

    let accepted: unknown;
    try {
      accepted = await this.fire(task);
    } catch (error) {
      this.scheduleDeliveryRetry(task, error);
      return;
    }
    if (accepted === false) {
      this.scheduleDeliveryRetry(task, new Error('fire callback declined delivery'));
      return;
    }

    this.deliveryFailures.delete(taskId);
    task.lastFiredAt = now;
    if (task.kind === 'recurring' && task.intervalSeconds !== undefined) {
      task.runAt = now + task.intervalSeconds;
      this.persist();
      if (this.running) this.arm(task, Date.now() / 1000);
    } else {
      task.status = 'done';
      this.persist();
    }
  }

  private scheduleDeliveryRetry(task: ScheduledTask, error: unknown): void {
    if (!this.running || task.status !== 'pending') return;
    const failures = (this.deliveryFailures.get(task.taskId) ?? 0) + 1;
    this.deliveryFailures.set(task.taskId, failures);
    const delayMs = Math.min(
      DELIVERY_RETRY_MAX_MS,
      DELIVERY_RETRY_BASE_MS * 2 ** Math.min(failures - 1, MAX_DELIVERY_BACKOFF_EXPONENT),
    );
    console.warn(
      `[scheduler] delivery failed task=${task.taskId} attempt=${failures}; retry in ${delayMs}ms:`,
      error,
    );
    // Keep runAt/status unchanged: a restart still sees an overdue pending
    // task and retries immediately rather than losing the reminder.
    this.armDelay(task.taskId, delayMs);
  }

  private load(): void {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const raw = JSON.parse(readFileSync(this.path, 'utf-8')) as Partial<TaskFile>;
      if (!Array.isArray(raw.tasks)) throw new Error('tasks.json: tasks 必须是数组');
      let invalid = 0;
      for (const candidate of raw.tasks) {
        const task = normalizeTask(candidate);
        if (task === null) {
          invalid += 1;
          continue;
        }
        this.tasks.set(task.taskId, task);
      }
      if (invalid > 0) console.warn(`[scheduler] ignored ${invalid} invalid task record(s) in ${this.path}`);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
      // Never silently overwrite a malformed file on the next schedule call.
      console.error('[scheduler] task file unreadable; preserving a backup:', error);
      try {
        renameSync(this.path, `${this.path}.bak-${Date.now()}`);
      } catch (backupError) {
        console.error('[scheduler] task file backup failed:', backupError);
      }
    }
  }

  private persist(): void {
    // Keep every pending task. The old unbounded slice could evict a pending
    // alarm when >200 historical records existed, leaving its timer orphaned.
    const all = [...this.tasks.values()].sort((a, b) => a.createdAt - b.createdAt);
    const history = all.filter((task) => task.status !== 'pending').slice(-200);
    const pending = all.filter((task) => task.status === 'pending');
    const kept = [...history, ...pending].sort((a, b) => a.createdAt - b.createdAt);
    this.tasks = new Map(kept.map((task) => [task.taskId, task]));
    let tmp = '';
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      tmp = `${this.path}.${randomUUID().slice(0, 8)}.tmp`;
      writeFileSync(tmp, JSON.stringify({ tasks: kept } satisfies TaskFile, null, 2), 'utf-8');
      renameSync(tmp, this.path);
    } catch (error) {
      console.error('[scheduler] persist failed:', error);
    } finally {
      if (tmp) rmSync(tmp, { force: true });
    }
  }
}

function normalizeTask(value: unknown): ScheduledTask | null {
  if (typeof value !== 'object' || value === null) return null;
  const raw = value as Record<string, unknown>;
  const taskId = String(raw.taskId ?? '');
  const kind = raw.kind;
  const originScope = String(raw.originScope ?? '');
  const note = String(raw.note ?? '');
  const runAt = Number(raw.runAt);
  const createdAt = Number(raw.createdAt);
  const status = raw.status;
  if (
    !taskId || !originScope.includes(':') || !note ||
    (kind !== 'set_alarm' && kind !== 'recurring' && kind !== 'send_image' && kind !== 'watchdog') ||
    !Number.isFinite(runAt) || runAt < 0 ||
    !Number.isFinite(createdAt) ||
    (status !== 'pending' && status !== 'done' && status !== 'cancelled')
  ) return null;
  const task: ScheduledTask = {
    taskId,
    kind,
    originScope,
    note,
    runAt,
    status,
    createdAt,
  };
  if (kind === 'recurring') {
    const intervalSeconds = Number(raw.intervalSeconds);
    if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0) return null;
    task.intervalSeconds = intervalSeconds;
  }
  if (kind === 'send_image') {
    const imageUrl = String(raw.imageUrl ?? '');
    if (!/^https?:\/\//i.test(imageUrl)) return null;
    task.imageUrl = imageUrl;
  }
  if (kind === 'watchdog' && raw.tag !== undefined) {
    const tag = String(raw.tag);
    if (tag) task.tag = tag.slice(0, 40);
  }
  if (raw.lastFiredAt !== undefined) {
    const lastFiredAt = Number(raw.lastFiredAt);
    if (Number.isFinite(lastFiredAt)) task.lastFiredAt = lastFiredAt;
  }
  return task;
}
