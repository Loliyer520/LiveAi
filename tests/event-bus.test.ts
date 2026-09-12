import test from 'node:test';
import assert from 'node:assert/strict';
import { EventBus } from '../src/events/event-bus.js';

test('EventBus publishes to subscribers and supports unsubscribe', async () => {
  const bus = new EventBus<{ message: { text: string } }>();
  const received: string[] = [];
  const unsubscribe = bus.subscribe('message', (event) => {
    received.push(event.text);
  });

  await bus.publish('message', { text: 'first' });
  unsubscribe();
  await bus.publish('message', { text: 'second' });

  assert.deepEqual(received, ['first']);
});

test('EventBus isolates synchronous and asynchronous subscriber failures', async () => {
  const bus = new EventBus<{ message: string }>();
  const received: string[] = [];
  bus.subscribe('message', () => {
    throw new Error('sync failure');
  });
  bus.subscribe('message', async () => {
    throw new Error('async failure');
  });
  bus.subscribe('message', (event) => {
    received.push(event);
  });

  await assert.doesNotReject(() => bus.publish('message', 'delivered'));
  assert.deepEqual(received, ['delivered']);
});
