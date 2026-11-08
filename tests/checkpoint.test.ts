import test from 'node:test';
import assert from 'node:assert/strict';
import { TurnCheckpointStore } from '../src/store/checkpoint.js';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { HistoryEntry, ModelMessage } from '../src/chat/types.js';

test('TurnCheckpointStore: writes and reads checkpoint', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  const store = new TurnCheckpointStore(dir);

  const checkpoint = {
    scopeKey: 'group:123',
    at: Date.now(),
    historyBeforeTrigger: [
      { message_id: 1, user_id: 100, text: 'hello', timestamp: Date.now() / 1000 } as HistoryEntry,
    ] as HistoryEntry[],
    extension: [
      { role: 'assistant' as const, content: 'I understand' },
      { role: 'user' as const, content: 'thanks' },
    ] as ModelMessage[],
    executedTools: ['send_message', 'memory_write'],
    toolRound: 2,
  };

  await store.write(checkpoint);
  const read = await store.read('group:123');

  assert.ok(read !== null);
  assert.equal(read.scopeKey, 'group:123');
  assert.equal(read.toolRound, 2);
  assert.deepEqual(read.executedTools, ['send_message', 'memory_write']);
  assert.equal(read.extension.length, 2);

  rmSync(dir, { recursive: true });
});

test('TurnCheckpointStore: returns null for missing checkpoint', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  const store = new TurnCheckpointStore(dir);

  const read = await store.read('nonexistent:999');
  assert.equal(read, null);

  rmSync(dir, { recursive: true });
});

test('TurnCheckpointStore: removes checkpoint', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  const store = new TurnCheckpointStore(dir);

  const checkpoint = {
    scopeKey: 'private:456',
    at: Date.now(),
    historyBeforeTrigger: [] as HistoryEntry[],
    extension: [] as ModelMessage[],
    executedTools: [],
    toolRound: 0,
  };

  await store.write(checkpoint);
  let read = await store.read('private:456');
  assert.ok(read !== null);

  await store.remove('private:456');
  read = await store.read('private:456');
  assert.equal(read, null);

  rmSync(dir, { recursive: true });
});

test('TurnCheckpointStore: prunes stale checkpoints', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  const store = new TurnCheckpointStore(dir);

  // Write an old checkpoint (pretend it's from 15 minutes ago)
  const oldTime = Date.now() - 15 * 60 * 1000;
  const checkpoint = {
    scopeKey: 'group:789',
    at: oldTime,
    historyBeforeTrigger: [] as HistoryEntry[],
    extension: [] as ModelMessage[],
    executedTools: [],
    toolRound: 0,
  };

  await store.write(checkpoint);
  await store.pruneStale();

  // After pruning, old checkpoint should be gone
  const read = await store.read('group:789');
  assert.equal(read, null);

  rmSync(dir, { recursive: true });
});
