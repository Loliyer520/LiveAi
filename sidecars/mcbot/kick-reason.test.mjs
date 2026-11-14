import test from 'node:test'
import assert from 'node:assert/strict'
import { classifyKick, isTerminalKick } from './kick-reason.js'

test('premium / session failures stop retrying', () => {
  assert.equal(classifyKick('Failed to verify username!'), 'premium')
  assert.equal(classifyKick('Invalid session'), 'premium')
  assert.equal(isTerminalKick('premium'), true)
})

test('version mismatch stops with advice', () => {
  assert.equal(classifyKick('Outdated client! Please use 1.20.4'), 'version')
  assert.equal(classifyKick('客户端版本不匹配'), 'version')
  assert.equal(isTerminalKick('version'), true)
})

test('bans / whitelist / blacklist are terminal', () => {
  assert.equal(classifyKick('You are banned from this server'), 'terminal')
  assert.equal(classifyKick('你已被封禁'), 'terminal')
  assert.equal(classifyKick('You are not whitelisted on this server'), 'terminal')
  assert.equal(isTerminalKick('terminal'), true)
})

test('full / restart / duplicate login reset backoff', () => {
  assert.equal(classifyKick('The server is full'), 'transient')
  assert.equal(classifyKick('服务器已满'), 'transient')
  assert.equal(classifyKick('Server restarting'), 'transient')
  assert.equal(classifyKick('You are already logged in'), 'transient')
  assert.equal(isTerminalKick('transient'), false)
})

test('graceful close / unknown reasons keep normal backoff', () => {
  assert.equal(classifyKick('Server closed'), 'unknown')
  assert.equal(classifyKick('{\"type\":\"string\",\"value\":\"Server closed\"}'), 'unknown')
  assert.equal(classifyKick(''), 'unknown')
  assert.equal(classifyKick(null), 'unknown')
  assert.equal(isTerminalKick('unknown'), false)
})
