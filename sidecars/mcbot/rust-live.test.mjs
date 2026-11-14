import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createWriteStream, existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import net from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { RustBackend } from './rust-backend.js'

const ROOT = path.resolve(import.meta.dirname)
const JAVA = process.env.MCBOT_JAVA ?? path.join(ROOT, 'runtime/jdk25/bin/java')
const SERVER_JAR = process.env.MCBOT_26_2_SERVER ?? path.join(ROOT, 'test-server-26.2/server.jar')
const PORT = Number(process.env.MCBOT_TEST_PORT ?? 25598)

function portInUse(port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: '127.0.0.1', port })
    socket.once('connect', () => { socket.destroy(); resolve(true) })
    socket.once('error', () => resolve(false))
    socket.setTimeout(1000, () => { socket.destroy(); reject(new Error('测试端口探测超时')) })
  })
}

async function waitFor(check, description, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await check()
    if (value) return value
    await delay(200)
  }
  throw new Error(`等待${description}超时`)
}

test('real official 26.2 server: join, status, chat, movement and leave', { timeout: 180_000 }, async (t) => {
  assert.equal(await portInUse(PORT), false, `测试端口 ${PORT} 已被占用`)
  const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-26.2-live-'))
  symlinkSync(SERVER_JAR, path.join(dir, 'server.jar'))
  writeFileSync(path.join(dir, 'eula.txt'), 'eula=true\n')
  writeFileSync(path.join(dir, 'server.properties'), [
    'server-ip=127.0.0.1',
    `server-port=${PORT}`,
    'online-mode=false',
    'view-distance=4',
    'simulation-distance=4',
    'spawn-protection=0',
    'max-players=2',
    'level-seed=2602001',
    'level-type=minecraft\\:flat',
    'motd=DeepLive isolated integration test',
    '',
  ].join('\n'))
  const logPath = path.join(dir, 'server.log')
  const server = spawn(JAVA, ['-Xms256M', '-Xmx768M', '-jar', 'server.jar', 'nogui'], {
    cwd: dir,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const serverOutput = createWriteStream(logPath)
  server.stdout.pipe(serverOutput)
  server.stderr.pipe(serverOutput)
  let backend
  const events = []
  t.after(async () => {
    if (backend) await backend.stop()
    if (server.exitCode === null) {
      if (server.stdin.writable) server.stdin.write('stop\n')
      await Promise.race([
        new Promise((resolve) => server.once('close', resolve)),
        delay(20_000).then(() => { if (server.exitCode === null) server.kill('SIGKILL') }),
      ])
    }
    rmSync(dir, { recursive: true, force: true })
  })

  await waitFor(() => {
    const log = existsSync(logPath) ? readFileSync(logPath, 'utf8') : ''
    if (server.exitCode !== null) throw new Error(`测试服提前退出：${log.slice(-2000)}`)
    return log.includes('Done (')
  }, '26.2 测试服启动')

  backend = new RustBackend({
    config: { rustWorkDir: path.join(dir, 'bot') },
    addEvent: (type, text) => events.push({ type, text }),
    log: () => {},
  })
  const initial = await backend.start({ host: '127.0.0.1', port: PORT, username: 'BingTang' })
  assert.equal(initial.protocol, 776)
  assert.equal(initial.level_loading, false)
  assert.equal(initial.position.length, 3)
  assert.ok(initial.players.includes('BingTang'))

  const marker = `deeplive-${Date.now()}`
  assert.equal((await backend.request('chat', { message: marker })).sent, true)
  assert.equal((await backend.request('command', { message: `/say ${marker}` })).sent, true)
  await waitFor(async () => {
    await backend.poll()
    return events.some((event) => event.type === 'chat' && event.text.includes(marker))
  }, '聊天事件', 10_000)

  const lookBefore = (await backend.request('status')).yaw
  await backend.request('move', { buttons: 0, look: [1200, 0] })
  await delay(300)
  const lookAfter = (await backend.request('status')).yaw
  const lookDelta = Math.atan2(Math.sin(lookAfter - lookBefore), Math.cos(lookAfter - lookBefore))
  assert.ok(Math.abs(Math.abs(lookDelta) - Math.PI) < 0.15,
    `大角度视角输入未按 0.15°/单位生效：${JSON.stringify({ lookBefore, lookAfter, lookDelta })}`)

  const before = (await backend.request('status')).position
  await backend.request('move', { buttons: 1 })
  await delay(900)
  await backend.request('stop')
  const after = (await backend.request('status')).position
  const distance = Math.hypot(after[0] - before[0], after[2] - before[2])
  assert.ok(distance > 0.2, `前进没有产生位移：${JSON.stringify({ before, after, distance })}`)

  await backend.request('select', { slot: 0 })
  const final = await backend.request('status')
  assert.equal(final.selected, 0)
  assert.ok(Array.isArray(final.inventory))
  await backend.stop()
  assert.equal(backend.active, false)
  assert.equal(backend.pending.size, 0)
  console.log(JSON.stringify({ official_server: '26.2', protocol: 776, chat: true, large_look: true, distance, leave: true }))
})
