import test from 'node:test'
import assert from 'node:assert/strict'
import { createAuthFlow } from './auth-flow.js'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function harness(initialProfile = null) {
  const sent = []
  const events = []
  const profiles = new Map()
  if (initialProfile) profiles.set('srv:25565', { ...initialProfile })
  const flow = createAuthFlow({
    sendCommand: async (command) => { sent.push(command) },
    addEvent: (type, text) => events.push({ type, text }),
    loadProfile: () => profiles.get('srv:25565') ?? null,
    saveProfile: (_h, _p, patch) => {
      profiles.set('srv:25565', { ...(profiles.get('srv:25565') ?? {}), ...patch })
    },
    ensurePassword: () => {
      const value = 'GeneratedPw1'
      profiles.set('srv:25565', { ...(profiles.get('srv:25565') ?? {}), password: value })
      return value
    },
    delayMs: 0,
  })
  flow.attach({ host: 'srv', port: 25565, backend: 'mineflayer' })
  return { flow, sent, events, profiles }
}

test('register prompt → /register with generated password → login prompt → /login', async () => {
  const { flow, sent, profiles } = harness()
  flow.handleText('请先注册！使用 /register <密码> <密码>')
  assert.equal(flow.isActive(), true)
  await sleep(10)
  assert.deepEqual(sent, ['/register GeneratedPw1 GeneratedPw1'])
  assert.equal(profiles.get('srv:25565').password, 'GeneratedPw1')
  assert.equal(profiles.get('srv:25565').authRequired, true)

  flow.handleText('注册成功，请登录')
  await sleep(10)
  assert.deepEqual(sent, ['/register GeneratedPw1 GeneratedPw1', '/login GeneratedPw1'])

  flow.handleText('登录成功！欢迎回来')
  assert.equal(flow.isDone(), true)
  assert.equal(flow.statusLine(), '服务器登录：已登录')
})

test('already-registered switches to stored profile password', async () => {
  const { flow, sent } = harness({ password: 'OldPw123456', authRequired: true })
  flow.handleText('该用户名已被注册')
  await sleep(10)
  assert.deepEqual(sent, ['/login OldPw123456'])
  assert.equal(flow.isDone(), false)
})

test('wrong password stops immediately instead of retrying the same string', async () => {
  const { flow, sent, events } = harness({ password: 'WrongPw', authRequired: true })
  flow.handleText('请先登录')
  await sleep(10)
  flow.handleText('密码错误，请重试')
  await sleep(10)
  assert.deepEqual(sent, ['/login WrongPw'])
  assert.equal(flow.isGaveUp(), true)
  assert.ok(events.some((e) => e.text.includes('密码不对')))
})

test('captcha stops automation and reports', () => {
  const { flow, events } = harness()
  flow.handleText('请输入验证码完成验证')
  assert.equal(flow.isGaveUp(), true)
  assert.ok(events.some((e) => e.text.includes('验证码')))
})

test('duplicate prompts within dedupe window send one command', async () => {
  const { flow, sent } = harness({ password: 'SamePw123456' })
  flow.handleText('请先登录')
  flow.handleText('请先登录！使用 /login <密码>')
  flow.handleText('请先登录')
  await sleep(10)
  assert.deepEqual(sent, ['/login SamePw123456'])
  assert.equal(flow.state().attempts, 1)
})

test('fallback login only when the profile says auth is required', async () => {
  const a = harness({ password: 'PwA123456' })
  assert.equal(a.flow.fallbackLogin(), false)
  assert.equal(a.sent.length, 0)

  const b = harness({ password: 'PwB123456', authRequired: true })
  assert.equal(b.flow.fallbackLogin(), true)
  await sleep(10)
  assert.deepEqual(b.sent, ['/login PwB123456'])
})

test('no password and login-only server gives up with actionable message', () => {
  const { flow, events, sent } = harness()
  flow.handleText('请先登录！使用 /login <密码>')
  assert.equal(sent.length, 0)
  assert.equal(flow.isGaveUp(), true)
  assert.ok(events.some((e) => e.text.includes('mc_join 的 password')))
})

test('repeated prompts inside the dedupe window do not spam or burn attempts', async () => {
  const { flow, sent } = harness({ password: 'LoopPw123456' })
  for (let i = 0; i < 6; i++) {
    flow.handleText('请先登录')
    await sleep(2)
  }
  assert.deepEqual(sent, ['/login LoopPw123456'])
  assert.equal(flow.state().attempts, 1)
  assert.equal(flow.isGaveUp(), false)
})
