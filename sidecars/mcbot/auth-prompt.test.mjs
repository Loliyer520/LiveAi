import test from 'node:test'
import assert from 'node:assert/strict'
import { classifyAuthPrompt } from './auth-prompt.js'

test('Chinese AuthMe prompts', () => {
  assert.equal(classifyAuthPrompt('请先注册！使用 /register <密码> <密码>'), 'register')
  assert.equal(classifyAuthPrompt('你还没有注册，请输入 /reg <密码> <密码>'), 'register')
  assert.equal(classifyAuthPrompt('请先登录！使用 /login <密码>'), 'login')
  assert.equal(classifyAuthPrompt('§c该用户名已被注册'), 'already-registered')
  assert.equal(classifyAuthPrompt('密码错误，请重试'), 'wrong-password')
  assert.equal(classifyAuthPrompt('登录成功！欢迎回来'), 'success')
  assert.equal(classifyAuthPrompt('请完成验证码验证'), 'captcha')
})

test('English AuthMe prompts', () => {
  assert.equal(classifyAuthPrompt('Please register with /register <password> <password>'), 'register')
  assert.equal(classifyAuthPrompt('Please login with /login <password>'), 'login')
  assert.equal(classifyAuthPrompt('This username is already registered'), 'already-registered')
  assert.equal(classifyAuthPrompt('Wrong password'), 'wrong-password')
  assert.equal(classifyAuthPrompt('Successfully logged in!'), 'success')
})

test('ordinary chat is not an auth prompt', () => {
  assert.equal(classifyAuthPrompt('<Steve> hello'), '')
  assert.equal(classifyAuthPrompt('Steve joined the game'), '')
  assert.equal(classifyAuthPrompt(''), '')
  assert.equal(classifyAuthPrompt(null), '')
})

test('color codes are stripped before matching', () => {
  assert.equal(classifyAuthPrompt('§6§lPlease login§r with /login <password>'), 'login')
})
