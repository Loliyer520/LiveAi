import test from 'node:test';
import assert from 'node:assert/strict';
import { IdempotencyLedger } from '../src/bot/idempotency.js';

test('IdempotencyLedger: detects duplicate sends within TTL', () => {
  const ledger = new IdempotencyLedger();
  assert.equal(ledger.isDuplicate('group', 123, 'text', 'hello'), false);
  ledger.record('group', 123, 'text', 'hello');
  assert.equal(ledger.isDuplicate('group', 123, 'text', 'hello'), true);
  ledger.destroy();
});

test('IdempotencyLedger: different targets are independent', () => {
  const ledger = new IdempotencyLedger();
  ledger.record('group', 123, 'text', 'hello');
  assert.equal(ledger.isDuplicate('group', 456, 'text', 'hello'), false);
  assert.equal(ledger.isDuplicate('private', 123, 'text', 'hello'), false);
  ledger.destroy();
});

test('IdempotencyLedger: different types are independent', () => {
  const ledger = new IdempotencyLedger();
  ledger.record('group', 123, 'text', 'hello');
  assert.equal(ledger.isDuplicate('group', 123, 'image', 'hello'), false);
  ledger.destroy();
});
