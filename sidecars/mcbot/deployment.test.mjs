import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync, statSync } from 'node:fs'
import { once } from 'node:events'
import { tmpdir } from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { setTimeout as delay } from 'node:timers/promises'

test('account store follows the configured installation; RCON is opt-in', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-portable-'))
  const env = { ...process.env, MCBOT_CONFIG: path.join(dir, 'config.json') }
  delete env.MCBOT_PROFILES
  delete env.MC_SERVER_PROPERTIES
  const child = spawn(process.execPath, ['--input-type=module', '-e', `
    import {writeProfile} from ${JSON.stringify(new URL('./server-profiles.js', import.meta.url).href)};
    import {rconReady} from ${JSON.stringify(new URL('./rcon.js', import.meta.url).href)};
    if (rconReady()) process.exit(2);
    writeProfile('test.invalid',25565,{username:'PortableTest',auth:'offline'});
  `], { env, stdio: 'pipe' })
  try {
    const [code] = await once(child, 'close')
    assert.equal(code, 0)
    const profile = path.join(dir, 'server-profiles.json')
    assert.equal(JSON.parse(readFileSync(profile, 'utf8'))['test.invalid:25565'].username, 'PortableTest')
    if (process.platform !== 'win32') assert.equal(statSync(profile).mode & 0o777, 0o600)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('fresh portable sidecar starts, requires token and remains disconnected until join', { timeout: 20_000 }, async (t) => {
  const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-start-'))
  const probe = net.createServer()
  probe.listen(0, '127.0.0.1'); await once(probe, 'listening')
  const port = probe.address().port
  await new Promise((resolve) => probe.close(resolve))
  const config = path.join(dir, 'config.json')
  writeFileSync(config, JSON.stringify({ host: '127.0.0.1', port, token: 'isolated-test-token' }), { mode: 0o600 })
  const env = { ...process.env, MCBOT_CONFIG: config }
  delete env.MCBOT_TOKEN; delete env.MCBOT_JOIN_HOOK; delete env.MC_SERVER_PROPERTIES
  const child = spawn(process.execPath, [new URL('./server.js', import.meta.url).pathname], { cwd: dir, env, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  child.stdout.on('data', (b) => { output += b })
  child.stderr.on('data', (b) => { output += b })
  t.after(async () => {
    if (child.exitCode === null) { child.kill('SIGTERM'); await once(child, 'close') }
    rmSync(dir, { recursive: true, force: true })
  })
  const base = `http://127.0.0.1:${port}`
  let ready = false
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) assert.fail(`Sidecar exited: ${output}`)
    try { ready = (await fetch(`${base}/health`)).ok } catch { /* wait for listener */ }
    if (ready) break
    await delay(100)
  }
  assert.ok(ready, output)
  assert.equal((await fetch(`${base}/status`)).status, 401)
  const response = await fetch(`${base}/status`, { headers: { authorization: 'Bearer isolated-test-token' } })
  assert.equal(response.status, 200)
  assert.match((await response.json()).message, /未连接/)
  assert.equal(existsSync(path.join(dir, 'server-profiles.json')), false, 'no implicit server login or account creation')
})
