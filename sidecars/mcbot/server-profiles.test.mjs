import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

const dir = mkdtempSync(path.join(tmpdir(), 'mcbot-profiles-'))
const file = path.join(dir, 'profiles.json')
process.env.MCBOT_PROFILES = file
const mod = await import(`./server-profiles.js?test=${Date.now()}`)
const { ensurePassword, generatePassword, profileKey, readProfile, writeProfile } = mod

test.after(() => rmSync(dir, { recursive: true, force: true }))

test('profileKey normalizes host case and default port', () => {
  assert.equal(profileKey('Example.COM', 25565), 'example.com:25565')
  assert.equal(profileKey('Example.COM', undefined), 'example.com:25565')
  assert.equal(profileKey('  play.test.cn  ', 25570), 'play.test.cn:25570')
})

test('generated passwords are 12 safe characters and unique enough', () => {
  const a = generatePassword()
  const b = generatePassword()
  assert.match(a, /^[A-Za-z0-9]{12}$/)
  assert.notEqual(a, b)
})

test('ensurePassword persists once and reuses per server', () => {
  const first = ensurePassword('one.example.com', 25565)
  assert.equal(ensurePassword('one.example.com', 25565), first)
  assert.equal(readProfile('one.example.com', 25565).password, first)
  // 不同服务器不能共用密码
  const second = ensurePassword('two.example.com', 25565)
  assert.notEqual(second, first)
  const onDisk = JSON.parse(readFileSync(file, 'utf8'))
  assert.equal(onDisk['one.example.com:25565'].password, first)
})

test('writeProfile merges fields and keeps password', () => {
  writeProfile('merge.example.com', 25565, { username: 'BingTang', auth: 'offline' })
  writeProfile('merge.example.com', 25565, { autoReconnect: false })
  const profile = readProfile('merge.example.com', 25565)
  assert.equal(profile.username, 'BingTang')
  assert.equal(profile.auth, 'offline')
  assert.equal(profile.autoReconnect, false)
  assert.ok(profile.updatedAt > 0)
})
