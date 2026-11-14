/**
 * 服务器登录插件（AuthMe 等）的自动注册/登录状态机。
 *
 * 独立于连接层：server.js 只注入「怎么发命令 / 怎么读写档案 / 怎么记事件」，
 * 因此这里可以被单测完整驱动（见 auth-flow.test.mjs），也能同时服务
 * Mineflayer 与 Rust 两种后端。
 */

import { classifyAuthPrompt } from './auth-prompt.js'

const EMPTY = () => ({
  active: false,      // 见过注册/登录提示
  done: false,        // 已确认登录成功
  gaveUp: false,      // 密码错/验证码等，停止自动尝试
  prompt: '',         // 最近识别的提示类型
  attempts: 0,
  wrongCount: 0,
  lastSent: '',
  lastSentAt: 0,
  timer: null,
})

export function createAuthFlow({
  addEvent = () => {},
  sendCommand,
  loadProfile = () => null,
  saveProfile = () => {},
  ensurePassword = () => '',
  delayMs = 900,
  dedupeMs = 4000,
  maxAttempts = 4,
} = {}) {
  let state = EMPTY()
  let info = null
  let password = ''
  let register = ''

  function clearTimer() {
    if (state.timer) clearTimeout(state.timer)
    state.timer = null
  }

  function reset() {
    clearTimer()
    state = EMPTY()
    info = null
    password = ''
    register = ''
  }

  function attach(connInfo, credentials = {}) {
    reset()
    info = connInfo
    password = String(credentials.password ?? '')
    register = String(credentials.register ?? '')
  }

  function passwordValue() {
    return password || register || ''
  }

  function profileOf() {
    return info ? loadProfile(info.host, info.port) : null
  }

  function giveUp(reason) {
    if (state.gaveUp) return
    state.gaveUp = true
    clearTimer()
    addEvent('error', reason)
  }

  function isDuplicate(command) {
    return command === state.lastSent && Date.now() - state.lastSentAt < dedupeMs
  }

  function schedule(command, label) {
    if (isDuplicate(command)) return false
    state.lastSent = command
    state.lastSentAt = Date.now()
    clearTimer()
    const owner = info
    state.timer = setTimeout(async () => {
      state.timer = null
      if (info !== owner || state.done || state.gaveUp) return
      try {
        await sendCommand(command)
        addEvent('system', `自动${label}（密码已隐藏）`)
      } catch (error) {
        addEvent('error', `自动${label}失败：${error.message}`)
      }
    }, delayMs)
    state.timer.unref?.()
    return true
  }

  function sendRegister() {
    if (!info) return
    const value = passwordValue() || ensurePassword(info.host, info.port)
    if (!value) return giveUp('服务器要求注册，但生成密码失败；请用 mc_join 的 password 参数指定。')
    if (!password) {
      password = value
      info.password = value
      saveProfile(info.host, info.port, { password: value })
    }
    const command = `/register ${value} ${value}`
    if (isDuplicate(command)) return
    state.attempts += 1
    if (state.attempts > maxAttempts) {
      return giveUp(`自动注册/登录尝试超过 ${maxAttempts} 次，停止；请检查 mc_events 里的服务器提示。`)
    }
    schedule(command, '注册')
  }

  function sendLogin() {
    if (!info) return
    const value = passwordValue() || profileOf()?.password || ''
    if (!value) {
      return giveUp('服务器要求登录，但没有这个服的密码；用 mc_join 的 password 参数指定一次，之后会自动记住。')
    }
    if (!password) {
      password = value
      info.password = value
    }
    const command = `/login ${value}`
    if (isDuplicate(command)) return
    state.attempts += 1
    if (state.attempts > maxAttempts) {
      return giveUp(`自动注册/登录尝试超过 ${maxAttempts} 次，停止；请检查 mc_events 里的服务器提示。`)
    }
    schedule(command, '登录')
  }

  function markAuthRequired() {
    if (!info) return
    saveProfile(info.host, info.port, { authRequired: true })
  }

  function handleText(raw) {
    const kind = classifyAuthPrompt(raw)
    if (!kind) return
    if (kind === 'success' || kind === 'already-logged-in') {
      if (!state.done && (state.active || state.attempts > 0)) addEvent('system', '服务器登录：已通过验证')
      state.done = true
      state.gaveUp = false
      clearTimer()
      markAuthRequired()
      return
    }
    if (kind === 'captcha') {
      return giveUp('服务器要求验证码/邮箱验证，自动进服接不了，需要人工处理一次。')
    }
    state.active = true
    markAuthRequired()
    if (state.gaveUp) return
    if (kind === 'wrong-password') {
      // 同一串密码重试没有意义，直接停并让人工给正确密码，避免把账号刷到封禁。
      state.wrongCount += 1
      return giveUp('密码错误。档案里这个服的密码不对，用 mc_join 的 password 参数指定正确密码。')
    }
    if (kind === 'already-registered') {
      state.prompt = 'login'
      addEvent('system', '这个用户名已注册过，改用登录')
      return sendLogin()
    }
    if (kind === 'register') {
      state.prompt = 'register'
      return sendRegister()
    }
    if (kind === 'login') {
      state.prompt = 'login'
      return sendLogin()
    }
  }

  /** 老服提示可能只发一次且很晚：档案确认要认证时，进服后由调用方延时补发一次。 */
  function fallbackLogin() {
    if (!info) return false
    const profile = profileOf()
    if (!profile?.authRequired) return false
    if (state.done || state.active || state.gaveUp) return false
    if (!password && profile.password) password = profile.password
    if (!passwordValue()) return false
    addEvent('system', '没有等到登录提示，主动补发一次登录')
    sendLogin()
    return true
  }

  function statusLine() {
    if (!state.active) return ''
    const detail = state.done ? '已登录' : state.gaveUp ? '自动登录失败（看 mc_events）' : '进行中'
    return `服务器登录：${detail}`
  }

  return {
    attach,
    reset,
    handleText,
    fallbackLogin,
    statusLine,
    password: passwordValue,
    isActive: () => state.active,
    isDone: () => state.done,
    isGaveUp: () => state.gaveUp,
    isSettled: () => !state.active || state.done || state.gaveUp,
    state: () => ({ ...state, timer: null }),
  }
}
