/**
 * Task scheduler — persisted alarms + recurring tasks. Ported from the legacy
 * task subsystem's set_alarm / recurring halves:
 *
 *  - every task lives in ONE JSON file (atomic tmp+rename), so alarms survive
 *    restarts; on boot, overdue tasks fire immediately (catch-up) and future
 *    tasks are re-armed;
 *  - recurring tasks re-arm themselves after each fire (runAt += interval);
 *  - the scheduler never touches chat plumbing itself: firing calls the
 *    `fire` callback, the orchestrator injects an internal trigger message
 *    into the origin scope's actor.
 *
 * Timers are an implementation detail — the JSON file is the source of truth.
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';

export type TaskKind = 'set_alarm' | 'recurring';
export type TaskStatus = 'pending' | 'done' | 'cancelled';

export interface ScheduledTask {
  taskId: string;
  kind: TaskKind;
  originScope: string;
  /** Alarm note / recurring instruction shown to the AI on fire. */
  note: string;
  /** Next fire time (epoch seconds). */
  runAt: number;
  /** recurring only: seconds between fires. */
  intervalSeconds?: number;
  status: TaskStatus;
  createdAt: number;
  lastFiredAt?: number;
}

interface TaskFile {
  tasks: ScheduledTask[];
}

/** setTimeout clamps at ~24.8 days; re-arm past that. */
const MAX_TIMER_MS = 2 ** 31 - 1;

export class TaskScheduler {
  private tasks = new Map<string, ScheduledTask>();
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>();
  private loaded = false;
  private running = false;

  constructor(
    private readonly path: string,
    private readonly fire: (task: ScheduledTask) => void,
  ) {}

  /** Load persisted tasks and arm timers; overdue pending tasks fire at once. */
  start(): void {
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
  }

  scheduleAlarm(originScope: string, at: number, note: string): string {
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

  scheduleRecurring(originScope: string, intervalSeconds: number, instruction: string): string {
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
    const timer = setTimeout(() => this.onTimer(task.taskId), Math.min(delayMs, MAX_TIMER_MS));
    timer.unref?.();
    this.timers.set(task.taskId, timer);
  }

  private onTimer(taskId: string): void {
    this.timers.delete(taskId);
    if (!this.running) return;
    const task = this.tasks.get(taskId);
    if (!task || task.status !== 'pending') return;
    const now = Date.now() / 1000;
    if (task.runAt - now > MAX_TIMER_MS / 1000) {
      this.arm(task, now); // long-haul timer checkpoint, not due yet
      return;
    }
    task.lastFiredAt = now;
    if (task.kind === 'recurring' && task.intervalSeconds !== undefined) {
      task.runAt = now + task.intervalSeconds;
      this.persist();
      this.fire(task);
      if (this.running) this.arm(task, Date.now() / 1000);
    } else {
      task.status = 'done';
      this.persist();
      this.fire(task);
    }
  }

  private load(): void {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const raw = JSON.parse(readFileSync(this.path, 'utf-8')) as Partial<TaskFile>;
      for (const task of raw.tasks ?? []) {
        this.tasks.set(task.taskId, task);
      }
    } catch {
      // missing/corrupt file starts empty
    }
  }

  private persist(): void {
    // Keep done/cancelled tasks for audit, bounded.
    const all = [...this.tasks.values()].sort((a, b) => a.createdAt - b.createdAt);
    const kept = all.slice(-200);
    this.tasks = new Map(kept.map((task) => [task.taskId, task]));
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      const tmp = `${this.path}.${randomUUID().slice(0, 8)}.tmp`;
      writeFileSync(tmp, JSON.stringify({ tasks: kept } satisfies TaskFile, null, 2), 'utf-8');
      renameSync(tmp, this.path);
    } catch (error) {
      console.error('[scheduler] persist failed:', error);
    }
  }
}
