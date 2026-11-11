import test from 'node:test';
import assert from 'node:assert/strict';
import { MessageDebounce } from '../src/chat/message-debounce.js';

test('new messages reset the full 2s quiet interval independently per scope', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const debounce = new MessageDebounce();
  debounce.touch('private:1');
  let ready = false;
  const waiting = debounce.wait('private:1')!.then(() => { ready = true; });
  t.mock.timers.tick(1500);
  debounce.touch('private:1');
  debounce.touch('group:2');
  t.mock.timers.tick(1500);
  await Promise.resolve();
  assert.equal(ready, false, 'the first message deadline must not fire');
  debounce.touch('group:2');
  t.mock.timers.tick(500);
  await waiting;
  assert.equal(ready, true);
  assert.equal(debounce.wait('private:1'), null);
  assert.notEqual(debounce.wait('group:2'), null, 'another scope has its own deadline');
  debounce.clear();
});

test('clearing on shutdown releases waiters and cancels deadlines', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const debounce = new MessageDebounce();
  debounce.touch('master:core');
  const waiting = debounce.wait('master:core');
  debounce.clear();
  await waiting;
  assert.equal(debounce.wait('master:core'), null);
});
