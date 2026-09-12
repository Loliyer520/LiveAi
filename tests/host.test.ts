import test from 'node:test';
import assert from 'node:assert/strict';
import { Host } from '../src/host/host.js';
import type { LiveAiModule } from '../src/host/types.js';

function moduleOf(name: string, events: string[], failure?: 'start' | 'stop'): LiveAiModule {
  return {
    name,
    async start() {
      events.push(`start:${name}`);
      if (failure === 'start') {
        throw new Error(`failed to start ${name}`);
      }
    },
    async stop() {
      events.push(`stop:${name}`);
      if (failure === 'stop') {
        throw new Error(`failed to stop ${name}`);
      }
    },
  };
}

test('Host starts in order and stops in reverse order', async () => {
  const events: string[] = [];
  const host = new Host([
    moduleOf('config', events),
    moduleOf('api', events),
  ]);

  await host.start();
  assert.deepEqual(host.snapshot(), {
    status: 'ready',
    startedModules: ['config', 'api'],
  });
  await host.stop();
  assert.deepEqual(events, ['start:config', 'start:api', 'stop:api', 'stop:config']);
});

test('Host rolls back started modules when startup fails', async () => {
  const events: string[] = [];
  const host = new Host([
    moduleOf('config', events),
    moduleOf('api', events, 'start'),
  ]);

  await assert.rejects(() => host.start(), /failed to start api/);
  assert.deepEqual(events, ['start:config', 'start:api', 'stop:config']);
  assert.equal(host.snapshot().status, 'failed');
});

test('Host stop is idempotent', async () => {
  const events: string[] = [];
  const host = new Host([moduleOf('api', events)]);

  await host.start();
  await Promise.all([host.stop(), host.stop()]);
  assert.deepEqual(events, ['start:api', 'stop:api']);
});

test('Host reports stop failures after attempting every module', async () => {
  const events: string[] = [];
  const host = new Host([
    moduleOf('first', events),
    moduleOf('second', events, 'stop'),
  ]);

  await host.start();
  await assert.rejects(() => host.stop(), AggregateError);
  assert.deepEqual(events, ['start:first', 'start:second', 'stop:second', 'stop:first']);
});

test('Host can be stopped before it starts', async () => {
  const host = new Host([]);

  await host.stop();
  assert.equal(host.snapshot().status, 'stopped');
});
