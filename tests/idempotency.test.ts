import test from 'node:test';
import assert from 'node:assert/strict';
import { ScopeSendLedger, sendActionKey } from '../src/bot/idempotency.js';

test('sendActionKey normalizes whitespace and keys on the reply target', () => {
  assert.equal(sendActionKey(['  你好 ', '', '在吗'], null), sendActionKey(['你好', '在吗'], undefined));
  assert.notEqual(sendActionKey(['你好'], null), sendActionKey(['你好'], 42));
  assert.equal(sendActionKey(['', '  '], null), '');
});

test('ScopeSendLedger blocks the same key within TTL, per scope', () => {
  let now = 1_000;
  const ledger = new ScopeSendLedger(5_000, () => now);
  const key = sendActionKey(['hello'], null);
  assert.equal(ledger.has('group:1', key), false);
  ledger.add('group:1', key);
  assert.equal(ledger.has('group:1', key), true);
  assert.equal(ledger.has('group:2', key), false, 'scopes are independent');
  now += 5_001;
  assert.equal(ledger.has('group:1', key), false, 'expired after TTL');
});

test('ScopeSendLedger discard releases a reserved key', () => {
  const ledger = new ScopeSendLedger();
  ledger.add('private:9', 'k');
  ledger.discard('private:9', 'k');
  assert.equal(ledger.has('private:9', 'k'), false);
});
