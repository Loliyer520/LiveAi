import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TurnCheckpointStore, type TurnCheckpoint } from '../src/store/checkpoint.js';

function sample(scopeKey: string, at: number): TurnCheckpoint {
  const [scopeType, scopeId] = scopeKey.split(':');
  return { scopeKey, scopeType, scopeId, at, executedTools: ['send_message', 'memory_write'], toolRound: 1, triggerPreview: 'hi' };
}

test('TurnCheckpointStore writes, reads and removes per scope', () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  try {
    const store = new TurnCheckpointStore(join(dir, 'cp'));
    assert.equal(store.read('group:1'), null, 'missing dir/file reads as null');
    store.write(sample('group:1', Date.now()));
    assert.deepEqual(store.read('group:1')?.executedTools, ['send_message', 'memory_write']);
    store.remove('group:1');
    assert.equal(store.read('group:1'), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('TurnCheckpointStore.takeAll returns recent checkpoints, drops stale/corrupt, empties the dir', () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  try {
    const now = 10 * 24 * 60 * 60 * 1000;
    const store = new TurnCheckpointStore(join(dir, 'cp'), () => now);
    store.write(sample('group:1', now - 1000));
    store.write(sample('private:2', now - 2 * 24 * 60 * 60 * 1000)); // too old
    writeFileSync(join(dir, 'cp', 'broken.json'), '{not json', 'utf8');
    const recovered = store.takeAll();
    assert.deepEqual(recovered.map((c) => c.scopeKey), ['group:1']);
    assert.deepEqual(store.takeAll(), [], 'everything consumed');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('TurnCheckpointStore.takeAll tolerates a missing directory', () => {
  const dir = mkdtempSync(join(tmpdir(), 'checkpoint-'));
  try {
    mkdirSync(join(dir, 'other'));
    assert.deepEqual(new TurnCheckpointStore(join(dir, 'cp')).takeAll(), []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
