/**
 * TaskScheduler unit tests: persisted alarms + recurring tasks, restart
 * recovery (overdue catch-up), cancellation, and re-arming.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TaskScheduler, type ScheduledTask } from '../src/chat/scheduler.js';

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test('alarm fires once at the scheduled time', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: ScheduledTask[] = [];
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), (task) => fired.push(task));
  try {
    scheduler.start();
    const taskId = scheduler.scheduleAlarm('private:1', Date.now() / 1000 + 0.15, '喝水提醒');
    assert.ok(taskId.startsWith('alarm_'));
    await sleep(400);
    assert.equal(fired.length, 1);
    assert.equal(fired[0].note, '喝水提醒');
    assert.equal(fired[0].originScope, 'private:1');
    assert.equal(scheduler.list().length, 0, 'fired alarm leaves the pending list');
    await sleep(200);
    assert.equal(fired.length, 1, 'one-shot alarm does not fire twice');
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('recurring task re-arms itself after each fire', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: ScheduledTask[] = [];
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), (task) => fired.push(task));
  try {
    scheduler.start();
    const taskId = scheduler.scheduleRecurring('group:9', 0.12, '每12秒检查一次群公告');
    await sleep(450);
    assert.ok(fired.length >= 2, `expected ≥2 fires, got ${fired.length}`);
    assert.ok(fired.every((task) => task.taskId === taskId));
    const pending = scheduler.list();
    assert.equal(pending.length, 1, 'recurring task stays pending');
    assert.equal(pending[0].kind, 'recurring');
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('tasks persist to disk and a fresh scheduler recovers them', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const path = join(dir, 'tasks.json');
  const first = new TaskScheduler(path, () => {});
  try {
    const at = Date.now() / 1000 + 3600;
    first.scheduleAlarm('private:2', at, '重启后还应存在的闹钟');
    first.scheduleRecurring('group:3', 600, '周期巡检');

    const raw = JSON.parse(await readFile(path, 'utf-8')) as { tasks: ScheduledTask[] };
    assert.equal(raw.tasks.length, 2, 'tasks.json written on schedule');

    const second = new TaskScheduler(path, () => {});
    const recovered = second.list();
    assert.equal(recovered.length, 2);
    assert.ok(recovered.some((task) => task.note === '重启后还应存在的闹钟'));
    assert.ok(recovered.some((task) => task.kind === 'recurring' && task.intervalSeconds === 600));
  } finally {
    first.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('overdue tasks fire immediately on start (restart catch-up)', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const path = join(dir, 'tasks.json');
  const fired: string[] = [];
  try {
    // Simulate a task that was due while the process was down.
    const planter = new TaskScheduler(path, () => {});
    const taskId = planter.scheduleAlarm('private:4', Date.now() / 1000 + 0.05, '补发的闹钟');
    planter.stop(); // stops timers; task stays pending on disk
    await sleep(120);

    const recovered = new TaskScheduler(path, (task) => fired.push(task.taskId));
    recovered.start();
    await sleep(200);
    assert.deepEqual(fired, [taskId], 'overdue pending task fired at boot');
    recovered.stop();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('cancel removes a pending task and stops its timer', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: string[] = [];
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), (task) => fired.push(task.taskId));
  try {
    scheduler.start();
    const taskId = scheduler.scheduleAlarm('private:5', Date.now() / 1000 + 0.1, '会被取消');
    assert.equal(scheduler.list().length, 1);
    assert.equal(scheduler.cancel(taskId), true);
    assert.equal(scheduler.list().length, 0);
    await sleep(250);
    assert.deepEqual(fired, [], 'cancelled task never fires');
    assert.equal(scheduler.cancel(taskId), false, 'double cancel reports false');
    assert.equal(scheduler.cancel('alarm_nope'), false, 'unknown id reports false');
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('list filters by scope', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), () => {});
  try {
    scheduler.scheduleAlarm('private:1', Date.now() / 1000 + 100, 'a');
    scheduler.scheduleAlarm('group:2', Date.now() / 1000 + 200, 'b');
    assert.equal(scheduler.list().length, 2);
    assert.equal(scheduler.list('private:1').length, 1);
    assert.equal(scheduler.list('group:2')[0].note, 'b');
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});
