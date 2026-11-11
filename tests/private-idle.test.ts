import test, { type TestContext } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PrivateIdle, parseSleepUntil } from '../src/chat/private-idle.js';
import { chatToolSchemas } from '../src/chat/tools.js';

function setup(t: TestContext) {
  const dir = mkdtempSync(join(tmpdir(), 'private-idle-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1_800_000_000_000 });
  return join(dir, 'idle.json');
}

test('two quiet minutes after the latest inbound or outbound, one decision per wake', (t) => {
  const path = setup(t);
  const wakes: string[] = [];
  const idle = new PrivateIdle(path, (key) => { wakes.push(key); return true; });
  idle.start();
  idle.activity('private:1', true);
  t.mock.timers.tick(90_000);
  idle.activity('private:1', false);
  t.mock.timers.tick(119_999);
  assert.deepEqual(wakes, []);
  t.mock.timers.tick(1);
  assert.deepEqual(wakes, ['private:1']);
  t.mock.timers.tick(600_000);
  assert.equal(wakes.length, 1, 'a pending decision must never pile up duplicates');
  idle.stop();
});

test('sleep survives restarts; outgoing echoes preserve it; new incoming cancels it', (t) => {
  const path = setup(t);
  const wakes: string[] = [];
  let idle = new PrivateIdle(path, (key) => { wakes.push(key); return true; });
  idle.start();
  idle.activity('private:1', true);
  const oldRevision = idle.get('private:1')!.revision;
  idle.sleep('private:1', Date.now() + 3_600_000, '一小时后再考虑');
  idle.activity('private:1', false);
  assert.equal(idle.get('private:1')?.mode, 'sleep');
  assert.equal(idle.isCurrent('private:1', oldRevision), false);
  idle.stop();
  idle = new PrivateIdle(path, (key) => { wakes.push(key); return true; });
  idle.start();
  idle.enroll('private:1');
  t.mock.timers.tick(3_599_999);
  assert.equal(wakes.length, 0);
  t.mock.timers.tick(1);
  assert.deepEqual(wakes, ['private:1']);
  idle.sleep('private:1', null, '明确不再主动联系');
  idle.stop();
  idle = new PrivateIdle(path, (key) => { wakes.push(key); return true; });
  idle.start();
  idle.enroll('private:1');
  t.mock.timers.tick(86_400_000);
  assert.equal(wakes.length, 1);
  idle.activity('private:1', true);
  t.mock.timers.tick(120_000);
  assert.equal(wakes.length, 2);
  idle.stop();
});

test('busy/offline delivery retries without losing the wake; scopes are independent', (t) => {
  const path = setup(t);
  let available = false;
  const wakes: string[] = [];
  const idle = new PrivateIdle(path, (key) => { if (!available) return false; wakes.push(key); return true; });
  idle.start();
  idle.activity('private:1', true);
  idle.activity('private:2', true);
  idle.sleep('private:2', null, '不主动联系');
  t.mock.timers.tick(120_000);
  assert.deepEqual(wakes, []);
  available = true;
  t.mock.timers.tick(5_000);
  assert.deepEqual(wakes, ['private:1']);
  idle.stop();
});

test('long sleeps exceed the Node timer limit without waking early', (t) => {
  const path = setup(t);
  let count = 0;
  const idle = new PrivateIdle(path, () => { count++; return true; });
  idle.start();
  idle.sleep('private:1', Date.now() + 40 * 86_400_000, '下个月');
  t.mock.timers.tick(2 ** 31 - 1);
  assert.equal(count, 0);
  t.mock.timers.tick(40 * 86_400_000 - (2 ** 31 - 1));
  assert.equal(count, 1);
  idle.stop();
});

test('unfinished crash decision recovers; a no-action result backs off one hour', (t) => {
  const path = setup(t);
  let count = 0;
  let idle = new PrivateIdle(path, () => { count++; return true; });
  idle.start();
  idle.activity('private:1', true);
  t.mock.timers.tick(120_000);
  idle.stop();
  idle = new PrivateIdle(path, () => { count++; return true; });
  idle.start();
  t.mock.timers.tick(0);
  assert.equal(count, 2);
  idle.finish('private:1', idle.get('private:1')!.revision, false);
  assert.equal(idle.get('private:1')?.mode, 'sleep');
  assert.equal(idle.get('private:1')?.wakeAt, Date.now() + 3_600_000);
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).scopes['private:1'].mode, 'sleep');
  idle.stop();
});

test('sleep accepts durations, Unix seconds and timezone-aware instants, rejects past/ambiguous times', () => {
  const now = Date.parse('2026-11-09T12:00:00Z');
  assert.equal(parseSleepUntil('+1h', now), now + 3_600_000);
  assert.equal(parseSleepUntil('1d', now), now + 86_400_000);
  assert.equal(parseSleepUntil('2026-11-10T09:00:00+08:00', now), Date.parse('2026-11-10T01:00:00Z'));
  assert.equal(parseSleepUntil(String(now / 1000 + 60), now), now + 60_000);
  assert.equal(parseSleepUntil('forever', now), null);
  for (const value of ['+0s', '-1h', 'tomorrow', '2026-11-10T09:00:00', '2020-01-01T00:00:00Z']) {
    assert.equal(parseSleepUntil(value, now), undefined);
  }
});

test('sleep is available to private chats only', () => {
  assert.ok(chatToolSchemas('private:123', false).some((tool) => tool.name === 'sleep'));
  for (const key of ['group:123', 'domain:mc', 'master:core']) {
    assert.ok(!chatToolSchemas(key, key === 'master:core').some((tool) => tool.name === 'sleep'));
  }
});

test('a stale decision cannot replace activity from a new user message', (t) => {
  const path = setup(t);
  const idle = new PrivateIdle(path, () => true);
  idle.start();
  idle.activity('private:1', true);
  t.mock.timers.tick(120_000);
  const revision = idle.get('private:1')!.revision;
  idle.activity('private:1', true);
  idle.finish('private:1', revision, false);
  assert.equal(idle.get('private:1')?.mode, 'idle');
  assert.equal(idle.get('private:1')?.wakeAt, Date.now() + 120_000);
  assert.throws(() => idle.sleep('private:1', Date.now() - 1, '过去时间'));
  assert.equal(idle.get('private:1')?.mode, 'idle');
  idle.stop();
});
