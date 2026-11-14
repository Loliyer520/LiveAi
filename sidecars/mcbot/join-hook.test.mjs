import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import {
  prepareJoin,
  resetJoinHookCache,
  resolveHookPath,
  sanitizeOverrides,
} from './join-hook.js'

const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-join-hook-'))
test.after(() => rmSync(dir, { recursive: true, force: true }))

function hookFile(name, source) {
  const file = path.join(dir, name)
  writeFileSync(file, source, 'utf8')
  resetJoinHookCache()
  return file
}

test('hook path prefers env over config', () => {
  assert.equal(resolveHookPath({ joinHook: '/a.mjs' }, { MCBOT_JOIN_HOOK: '/b.mjs' }), '/b.mjs')
  assert.equal(resolveHookPath({ joinHook: '/a.mjs' }, {}), '/a.mjs')
  assert.equal(resolveHookPath({}, {}), '')
})

test('no hook path is a no-op', async () => {
  const result = await prepareJoin({ hookPath: '', host: 'srv', port: 25565 })
  assert.deepEqual(result, { overrides: {}, note: '' })
})

test('sanitize keeps only whitelisted fields and trims them', () => {
  const { overrides, note } = sanitizeOverrides({
    username: ' Abc123 ',
    password: 'Pw123456',
    note: '取码成功\n第二行',
    evil: 'ignored',
    backend: 'rust',
  })
  assert.deepEqual(overrides, { username: 'Abc123', password: 'Pw123456', backend: 'rust' })
  assert.equal(note, '取码成功 第二行')
})

test('sanitize rejects whitespace and non-string fields', () => {
  assert.throws(() => sanitizeOverrides({ password: 'a b' }), /不能含空白/)
  assert.throws(() => sanitizeOverrides({ username: { x: 1 } }), /必须是字符串/)
  assert.throws(() => sanitizeOverrides('nope'), /必须是对象/)
})

test('hook receives context and can override the connection identity', async () => {
  const file = hookFile('ok.mjs', `
    export default async function (ctx) {
      ctx.log('phase=' + ctx.phase + ' host=' + ctx.host)
      if (ctx.phase === 'reconnect') return { username: 'code2', note: '重连换码' }
      return { username: 'code1', version: '26.2', backend: 'rust', note: '进服取码' }
    }
  `)
  const logs = []
  const first = await prepareJoin({
    hookPath: file, phase: 'join', host: 'srv', port: 25565, log: (m) => logs.push(m),
  })
  assert.deepEqual(first.overrides, { username: 'code1', version: '26.2', backend: 'rust' })
  assert.equal(first.note, '进服取码')
  assert.ok(logs.some((line) => line.includes('phase=join')))

  const second = await prepareJoin({ hookPath: file, phase: 'reconnect', host: 'srv', port: 25565 })
  assert.deepEqual(second.overrides, { username: 'code2' })
})

test('null return means "nothing to override"', async () => {
  const file = hookFile('null.mjs', 'export default async () => null')
  const result = await prepareJoin({ hookPath: file, host: 'other', port: 25565 })
  assert.deepEqual(result, { overrides: {}, note: '' })
})

test('missing hook file fails loudly', async () => {
  resetJoinHookCache()
  await assert.rejects(
    prepareJoin({ hookPath: path.join(dir, 'missing.mjs'), host: 'srv', port: 25565 }),
    /文件不存在/,
  )
})

test('module without a function export is rejected', async () => {
  const file = hookFile('bad.mjs', 'export const notIt = 1')
  await assert.rejects(
    prepareJoin({ hookPath: file, host: 'srv', port: 25565 }),
    /必须默认导出/,
  )
})

test('hook throwing is wrapped with a deployment-side prefix', async () => {
  const file = hookFile('throw.mjs', `export default async () => { throw new Error('auth 500') }`)
  await assert.rejects(
    prepareJoin({ hookPath: file, host: 'srv', port: 25565 }),
    /进服适配器失败：auth 500/,
  )
})

test('slow hook times out instead of blocking the join forever', async () => {
  const file = hookFile('slow.mjs', 'export default () => new Promise(() => {})')
  await assert.rejects(
    prepareJoin({ hookPath: file, host: 'srv', port: 25565, timeoutMs: 30 }),
    /超时/,
  )
})
