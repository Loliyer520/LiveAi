/**
 * TaskScheduler unit tests: persisted alarms + recurring tasks, restart
 * recovery (overdue catch-up), cancellation, and re-arming.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, readdir, writeFile } from 'node:fs/promises';
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

test('scheduled image keeps its original URL across persistence and reload', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const path = join(dir, 'tasks.json');
  try {
    const first = new TaskScheduler(path, () => {});
    first.scheduleImage('private:9', Date.now() / 1000 + 600, 'https://img.example.com/original.png');
    first.stop();
    const second = new TaskScheduler(path, () => {});
    const task = second.list()[0];
    assert.equal(task.kind, 'send_image');
    assert.equal(task.imageUrl, 'https://img.example.com/original.png');
    second.stop();
  } finally {
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

test('delivery exception keeps an alarm pending and retries with backoff', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: string[] = [];
  let attempts = 0;
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), async (task) => {
    attempts += 1;
    await sleep(10);
    if (attempts === 1) throw new Error('temporary mailbox failure');
    fired.push(task.taskId);
  });
  try {
    scheduler.start();
    const taskId = scheduler.scheduleAlarm('private:6', Date.now() / 1000 + 0.05, '故障后仍要提醒');
    await sleep(180);
    assert.equal(attempts, 1);
    assert.equal(scheduler.list().length, 1, 'failed delivery is not marked done');
    await sleep(1_100);
    assert.deepEqual(fired, [taskId]);
    assert.equal(scheduler.list().length, 0);
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('recurring task survives a delivery exception and is re-armed', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  let attempts = 0;
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), () => {
    attempts += 1;
    if (attempts === 1) throw new Error('temporary fire failure');
  });
  try {
    scheduler.start();
    scheduler.scheduleRecurring('group:8', 0.1, '异常后继续巡检');
    await sleep(180);
    assert.equal(attempts, 1);
    assert.equal(scheduler.list().length, 1);
    await sleep(1_250);
    assert.ok(attempts >= 3, `expected retry plus a later interval, got ${attempts}`);
    assert.equal(scheduler.list().length, 1);
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('start is idempotent and never installs duplicate recurring timers', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: string[] = [];
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), (task) => fired.push(task.taskId));
  try {
    scheduler.scheduleRecurring('private:7', 0.2, '只触发一份');
    scheduler.start();
    scheduler.start();
    await sleep(450);
    assert.equal(fired.length, 2);
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('corrupt task file is backed up instead of silently overwritten', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const path = join(dir, 'tasks.json');
  try {
    await writeFile(path, '{not-json', 'utf-8');
    const scheduler = new TaskScheduler(path, () => {});
    scheduler.start();
    assert.equal(scheduler.list().length, 0);
    const backups = (await readdir(dir)).filter((name) => name.startsWith('tasks.json.bak-'));
    assert.equal(backups.length, 1);
    scheduler.scheduleAlarm('private:1', Date.now() / 1000 + 100, '新任务');
    assert.equal(JSON.parse(await readFile(path, 'utf-8')).tasks.length, 1);
    scheduler.stop();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('history retention never evicts pending tasks', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const scheduler = new TaskScheduler(join(dir, 'tasks.json'), () => {});
  try {
    for (let index = 0; index < 205; index += 1) {
      scheduler.scheduleAlarm('private:1', Date.now() / 1000 + 3600 + index, `pending-${index}`);
    }
    assert.equal(scheduler.list().length, 205);
  } finally {
    scheduler.stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('watchdog：同 tag 顶替旧保底，不同 tag 共存，重启可恢复', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const path = join(dir, 'tasks.json');
  const fired: { taskId: string; note: string }[] = [];
  try {
    const scheduler = new TaskScheduler(path, (task) => { fired.push({ taskId: task.taskId, note: task.note }); });
    scheduler.start();
    const first = scheduler.scheduleWatchdog('private:9', Date.now() / 1000 + 3600, '等 MC 中枢回报', 'mc');
    // 重委派：同 tag 新保底顶掉旧的，不积压
    const second = scheduler.scheduleWatchdog('private:9', Date.now() / 1000 + 3600, '等 MC 中枢回报（续）', 'mc');
    assert.notEqual(first, second);
    const mine = scheduler.list('private:9').filter((task) => task.kind === 'watchdog');
    assert.equal(mine.length, 1);
    assert.equal(mine[0].taskId, second);
    // 不同 tag 各自独立
    scheduler.scheduleWatchdog('private:9', Date.now() / 1000 + 3600, '等下载', 'download');
    assert.equal(scheduler.list('private:9').filter((task) => task.kind === 'watchdog').length, 2);
    // 别的 scope 不受影响也查不到
    assert.equal(scheduler.list('group:8').filter((task) => task.kind === 'watchdog').length, 0);
    scheduler.stop();

    // 重启：tag 随任务持久化恢复
    const revived = new TaskScheduler(path, () => {});
    revived.start();
    const restored = revived.list('private:9').filter((task) => task.kind === 'watchdog');
    assert.equal(restored.length, 2);
    assert.ok(restored.every((task) => typeof task.tag === 'string' && task.tag.length > 0));
    revived.stop();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('watchdog：到点触发且一次性（触发后不再重复）', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sched-'));
  const fired: string[] = [];
  try {
    const scheduler = new TaskScheduler(join(dir, 'tasks.json'), (task) => { fired.push(task.taskId); return true; });
    scheduler.start();
    scheduler.scheduleWatchdog('private:10', Date.now() / 1000 + 0.1, '催办', 'mc');
    await sleep(450);
    assert.equal(fired.length, 1);
    await sleep(250);
    assert.equal(fired.length, 1); // 一次性，不重复
    scheduler.stop();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
