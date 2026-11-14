import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { RustBackend, shouldUseRust } from './rust-backend.js'

function fixture(t, source = 'fake-rust.mjs') {
  const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-rust-test-'))
  const binary = path.join(dir, 'fake-rust')
  writeFileSync(binary, `#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(path.join(import.meta.dirname, 'test-fixtures', source))} "$@"\n`)
  chmodSync(binary, 0o700)
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return { rustBinary: binary, rustWorkDir: path.join(dir, 'work') }
}

test('routes protocol 776 and explicit 26.2 only', () => {
  assert.equal(shouldUseRust('', 776), true)
  assert.equal(shouldUseRust('26.2', undefined), true)
  assert.equal(shouldUseRust('1.20.4', 765), false)
  assert.equal(shouldUseRust('', 765), false)
})

test('owns the Rust process and exchanges bounded NDJSON requests', async (t) => {
  const files = fixture(t)
  const events = []
  const backend = new RustBackend({
    config: files,
    addEvent: (type, text) => events.push({ type, text }),
    log: () => {},
  })
  t.after(() => backend.stop())
  const snapshot = await backend.start({ host: '127.0.0.1', port: 25565, username: 'BingTang' })
  assert.equal(snapshot.protocol, 776)
  assert.equal(backend.active, true)
  assert.deepEqual((await backend.request('chat', { message: 'hello' })).sent, true)
  assert.equal((await backend.request('status')).chat_sequence, 1)
  assert.deepEqual((await backend.request('move', { buttons: 17 })).accepted, true)
  assert.equal(events.some((event) => event.type === 'spawn'), true)
  await backend.stop()
  assert.equal(backend.active, false)
  assert.equal(backend.pending.size, 0)
})

test('fails promptly when the Rust executable cannot start', async (t) => {
  const backend = new RustBackend({
    config: { rustBinary: '/definitely/missing/mc-native', rustWorkDir: fixture(t).rustWorkDir },
    addEvent: () => {},
    log: () => {},
  })
  await assert.rejects(
    backend.start({ host: '127.0.0.1', port: 25565, username: 'BingTang' }),
    /启动失败/,
  )
  assert.equal(backend.active, false)
})
