import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MobileSessions } from '../src/api/mobile-sessions.js';

test('mobile session credentials persist hashed and survive restart without sharing authority', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-mobile-'));
  try {
    const path = join(dir, 'sessions.json');
    const store = new MobileSessions(path);
    const a = store.create(); const b = store.create();
    assert.notEqual(a.token, b.token);
    assert.equal(store.resolve(a.token), a.sessionId);
    assert.equal(store.resolve('admin-secret'), null);
    assert.ok(!(await readFile(path, 'utf8')).includes(a.token));
    assert.equal(new MobileSessions(path).resolve(b.token), b.sessionId);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
