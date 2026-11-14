/**
 * DeepLive Minecraft sidecar — Minecraft Java Edition bot (mineflayer) behind a
 * loopback HTTP API. Kept out of the zero-dependency DeepLive process on purpose:
 * mineflayer + pathfinder pull in a real dependency tree, while the bot only
 * needs declarative HTTP tools (`data/plugins/mc/plugin.json`).
 *
 * Security: binds 127.0.0.1 only and requires `Authorization: Bearer <token>`.
 * The plugin tools that call it are master_only.
 */

import http from 'node:http'
import crypto from 'node:crypto'
import path from 'node:path'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import mineflayer from 'mineflayer'
import minecraftProtocol from 'minecraft-protocol'
import vec3 from 'vec3'
import pathfinderPkg from 'mineflayer-pathfinder'
import collectBlockPkg from 'mineflayer-collectblock'
import { renderView } from './render.js'
import { RustBackend, shouldUseRust } from './rust-backend.js'
import { rconCommand, rconReady } from './rcon.js'
import { createBehaviorEngine } from './behaviors.js'
import { decodeGrid } from './nav.js'
import { ensurePassword, readProfile, writeProfile } from './server-profiles.js'
import { createAuthFlow } from './auth-flow.js'
import { classifyKick } from './kick-reason.js'
import { prepareJoin, resolveHookPath } from './join-hook.js'

const { pathfinder, Movements, goals } = pathfinderPkg
const collectBlockPlugin = collectBlockPkg.plugin

const CONFIG_PATH = process.env.MCBOT_CONFIG ?? path.join(import.meta.dirname, 'config.json')
const MAX_EVENTS = 300
const MAX_EVENT_LEN = 500

function log(...args) {
  console.log(new Date().toISOString(), ...args)
}

function loadConfig() {
  let cfg = {}
  if (existsSync(CONFIG_PATH)) {
    try {
      cfg = JSON.parse(readFileSync(CONFIG_PATH, 'utf8'))
    } catch (error) {
      log('[config] invalid JSON, using defaults:', error.message)
    }
  }
  if (!cfg.token) cfg.token = crypto.randomBytes(24).toString('hex')
  if (!cfg.host) cfg.host = '127.0.0.1'
  if (!cfg.port) cfg.port = 7660
  try {
    writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2) + '\n', { mode: 0o600 })
  } catch (error) {
    log('[config] cannot persist:', error.message)
  }
  return cfg
}

const CONFIG = loadConfig()
const TOKEN = String(process.env.MCBOT_TOKEN ?? CONFIG.token)

// 私有进服适配器：核心只存路径，实现放在开源仓库之外（见 join-hook.js）。
// 相对路径按 config.json 所在目录解析，避免 systemd 的 cwd 影响。
const JOIN_HOOK = (() => {
  const raw = resolveHookPath(CONFIG)
  if (!raw) return ''
  return path.isAbsolute(raw) ? raw : path.resolve(path.dirname(CONFIG_PATH), raw)
})()

const DEFAULT_USERNAME = 'DeepLive'

/** Connection + event state. */
const state = {
  bot: null,
  joined: false,
  connecting: false,
  info: null, // { host, port, username, version, auth, autoReconnect }
  events: [],
  seq: 0,
  lastError: '',
  task: '',
  follow: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  reconnectingAttempt: false,
  manualLeave: false,
  kickTerminal: false,
  // hook 覆盖前的稳定身份；重连时以它为底，避免复用上一次的一次性码。
  preHookInfo: null,
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

function fmtPos(p) {
  return `${p.x.toFixed(1)},${p.y.toFixed(1)},${p.z.toFixed(1)}`
}

function clampInt(value, fallback, min, max) {
  const n = Number.parseInt(String(value ?? ''), 10)
  const v = Number.isFinite(n) ? n : fallback
  return Math.min(max, Math.max(min, v))
}

function num(value, fallback) {
  // 空串必须算「没给」：Number('')===0 是有限数，插件模板 y={{y}} 留空渲染成 y=
  // 会被当成 y=0——bot 在 y=72 永远满足不了 |py-0|<1.6 的到点硬条件，
  // 在目标点上永远判不到，前后左右来回抽搐到超时（实机踩过）。
  if (value === null || value === undefined || String(value).trim() === '') return fallback
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function bool(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback
  return /^(1|true|yes|on|是)$/i.test(String(value))
}

function cleanText(value, maxLen) {
  return String(value ?? '').replace(/[\r\n\t]+/g, ' ').trim().slice(0, maxLen)
}

function withTimeout(promise, ms, label) {
  let timer = null
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${Math.round(ms / 1000)}s）`)), ms)
  })
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer))
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 自己把自己挖进 1×1 坑里时，寻路器不会主动跳出来（实测会一直超时）。 */
function stuckInPit(bot) {
  const base = bot.entity.position.floored()
  const below = bot.blockAt(base.offset(0, -1, 0))
  if (!below || below.boundingBox !== 'block') return false
  const head = bot.blockAt(base.offset(0, 1, 0))
  if (head && head.boundingBox === 'block') return false
  let walls = 0
  for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    const block = bot.blockAt(base.offset(dx, 0, dz))
    if (block && block.boundingBox === 'block') walls += 1
  }
  return walls >= 3
}

/** 四个方向轮流「跳 + 前进」，脱困后恢复视线。成功返回 true。 */
async function attemptUnstuck(bot) {
  const start = bot.entity.position.clone()
  const yaw0 = bot.entity.yaw
  const pitch0 = bot.entity.pitch
  for (let i = 0; i < 4; i++) {
    try {
      await bot.look(yaw0 + (i * Math.PI) / 2, 0, true)
    } catch {}
    bot.setControlState('jump', true)
    bot.setControlState('forward', true)
    await sleep(550)
    bot.setControlState('forward', false)
    bot.setControlState('jump', false)
    await sleep(150)
    const pos = bot.entity.position
    if (pos.y > start.y + 0.6 || pos.distanceTo(start) > 1.2) {
      try {
        await bot.look(yaw0, pitch0, true)
      } catch {}
      return true
    }
  }
  return false
}

function addEvent(type, text) {
  state.seq += 1
  state.events.push({
    seq: state.seq,
    at: new Date().toISOString(),
    type,
    text: String(text ?? '').replace(/[\r\n]+/g, ' ').slice(0, MAX_EVENT_LEN),
  })
  if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS)
}

function requireBot() {
  if (!state.bot || !state.joined) throw new Error('还没连上服务器，先用 mc_join 进服。')
  return state.bot
}

// ---------------------------------------------------------------------------
// 进服目标解析 / 服务器登录插件（AuthMe 等）
// ---------------------------------------------------------------------------

/** "play.example.com" / "play.example.com:25570" / "minecraft://host:port/" 都接受。 */
function parseTarget(rawHost, rawPort) {
  let host = cleanText(rawHost, 300)
  let port = Number(rawPort) || 0
  if (!host) return { host: '', port: port || 25565 }
  // 允许模型把协议和路径一起抄进来
  host = host.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/\/.*$/, '')
  const v6 = host.match(/^\[([^\]]+)\](?::(\d+))?$/)
  if (v6) {
    host = v6[1]
    if (!port && v6[2]) port = Number(v6[2])
  } else {
    const parts = host.split(':')
    if (parts.length === 2 && /^\d+$/.test(parts[1])) {
      host = parts[0]
      if (!port) port = Number(parts[1])
    }
  }
  host = host.trim()
  if (/\s/.test(host)) return { host: '', port: port || 25565 }
  return { host, port: clampInt(port || rawPort || 25565, 25565, 1, 65535) }
}


const rustBackend = new RustBackend({
  addEvent,
  log,
  config: CONFIG,
  onChat: (text) => authFlow.handleText(text),
  onUnexpectedEnd: () => {
    if (state.manualLeave || !state.info?.autoReconnect) return
    addEvent('system', '26.2 客户端异常退出，准备自动重连')
    scheduleReconnect()
  },
})

// 注册/登录状态机：连接层只提供「发命令」和「档案读写」，逻辑本身可单测。
const authFlow = createAuthFlow({
  addEvent,
  loadProfile: (host, port) => readProfile(host, port),
  saveProfile: (host, port, patch) => writeProfile(host, port, patch),
  ensurePassword: (host, port) => ensurePassword(host, port),
  delayMs: Number(CONFIG.authDelayMs ?? 900),
  sendCommand: async (command) => {
    if (state.info?.backend === 'rust') {
      if (!rustBackend.active) throw new Error('Rust 后端未运行')
      await rustBackend.request('command', { message: command })
      return
    }
    const bot = state.bot
    if (!bot?.chat) throw new Error('还没连上服务器')
    // 提示可能比 spawn 早到（登录阶段），等进世界再发，避免 chat 抛 "not spawned"。
    if (!state.joined) await waitForSpawn(bot, 5_000).catch(() => {})
    if (!state.joined) throw new Error('等待进入世界超时')
    bot.chat(command)
  },
})

/** 等自动注册/登录落定，最多 maxMs；没触发认证的服立即返回，不拖慢普通进服。 */
async function settleAuth(maxMs) {
  const deadline = Date.now() + maxMs
  while (Date.now() < deadline) {
    if (authFlow.isSettled()) return
    await sleep(100)
  }
}

/** 老服可能只发一次提示且发得很晚：进服 2.5s 后补一次登录（仅在档案确认过要认证时）。 */
function scheduleAuthFallback(bot) {
  const timer = setTimeout(() => {
    if (state.bot !== bot) return
    authFlow.fallbackLogin()
  }, 2500)
  timer.unref?.()
}

function authJoinNote() {
  if (authFlow.isDone()) return '｜自动登录成功'
  if (authFlow.isGaveUp()) return '｜自动登录失败，看 mc_events'
  return authFlow.isActive() ? '｜登录处理中，mc_events 看进展' : ''
}

/**
 * 部署侧的进服前适配（见 join-hook.js）。每次首次连接和自动重连都调一次，
 * 因为一次性进服码/动态令牌用一次就失效，不能缓存到下一次连接。
 * 返回新 info；没配 hook 时原样返回，开源核心的普通进服路径零变化。
 */
async function applyJoinHook(info, phase) {
  if (!JOIN_HOOK) return info
  const profile = readProfile(info.host, info.port)
  const { overrides, note } = await prepareJoin({
    hookPath: JOIN_HOOK,
    phase,
    host: info.host,
    port: info.port,
    profile,
    info: {
      username: info.username,
      version: info.version,
      auth: info.auth,
      backend: info.backend,
    },
    timeoutMs: Number(CONFIG.joinHookTimeoutMs ?? 20_000),
    log,
  })
  const merged = { ...info }
  for (const [field, value] of Object.entries(overrides)) {
    merged[field] = value
    if (field === 'password' && !overrides.register) merged.register = value
    if (field === 'register' && !overrides.password) merged.password = value
  }
  if (!/^[A-Za-z0-9_]{3,16}$/.test(String(merged.username ?? ''))) {
    throw new Error('进服适配器返回的 username 不合法（只允许 3-16 位字母/数字/下划线）')
  }
  if (!['offline', 'microsoft'].includes(String(merged.auth ?? '').toLowerCase())) {
    throw new Error('进服适配器返回的 auth 只能是 offline 或 microsoft')
  }
  merged.auth = String(merged.auth).toLowerCase()
  if (merged.backend && !['auto', 'rust', 'mineflayer'].includes(merged.backend)) {
    throw new Error('进服适配器返回的 backend 只能是 auto / rust / mineflayer')
  }
  if (note) addEvent('system', `进服适配：${note}`)
  log(
    '[join-hook]', phase, `${info.host}:${info.port}`,
    'overrides:', Object.keys(overrides).join(',') || '(none)',
  )
  return merged
}

function itemLabel(item) {
  return `${item.name}×${item.count}`
}

const ITEM_ALIASES = {
  泥土: 'dirt',
  草方块: 'grass_block',
  石头: 'stone',
  圆石: 'cobblestone',
  原木: 'oak_log',
  木头: 'oak_log',
  木板: 'oak_planks',
  沙子: 'sand',
  沙砾: 'gravel',
  煤: 'coal',
  煤炭: 'coal',
  铁: 'iron_ingot',
  铁锭: 'iron_ingot',
  金锭: 'gold_ingot',
  钻石: 'diamond',
  红石: 'redstone',
  火把: 'torch',
  工作台: 'crafting_table',
  熔炉: 'furnace',
  箱子: 'chest',
  床: 'red_bed',
  苹果: 'apple',
  面包: 'bread',
  熟猪排: 'cooked_porkchop',
}

function normalizeItem(name) {
  // 剥 minecraft: 前缀：mc_inventory 展示全名，模型照抄传回来必须仍能匹配
  // （曾致「背包里没有 minecraft:dirt」而包里明明有，2026-11-05）。
  const raw = cleanText(name, 64).toLowerCase().replace(/\s+/g, '_').replace(/^minecraft:/, '')
  return ITEM_ALIASES[raw] ?? raw
}

// ---------------------------------------------------------------------------
// mineflayer connection
// ---------------------------------------------------------------------------

function attachEvents(bot) {
  bot.on('spawn', () => {
    state.joined = true
    state.connecting = false
    state.reconnectingAttempt = false
    state.reconnectAttempts = 0
    state.task = ''
    state.lastError = ''
    try {
      const movements = new Movements(bot)
      bot.pathfinder.setMovements(movements)
    } catch (error) {
      log('[pathfinder] movements init failed:', error.message)
    }
    addEvent('spawn', `${bot.username} 已进入服务器`)
    // 注册/登录不再用写死的定时器：由 messagestr 识别服务器提示后按需发送，
    // 老服提示晚到时再由 scheduleAuthFallback 补一次。
    scheduleAuthFallback(bot)
    log('[mc] spawned as', bot.username, 'at', fmtPos(bot.entity.position))
  })

  bot.on('end', (reason) => {
    const wasJoined = state.joined
    if (state.bot === bot) {
      state.bot = null
      state.joined = false
      state.connecting = false
    }
    state.task = ''
    state.follow = null
    addEvent('end', `连接结束：${reason || '(无原因)'}`)
    log('[mc] connection ended:', reason)
    // wasJoined=false 还可能是「重连本身失败」：createBot 的错误是异步的，
    // 不在这里续排，退避链会在第一次重连失败后断掉（2026-11-08 实测）。
    if (!state.manualLeave && state.info?.autoReconnect && (wasJoined || state.reconnectingAttempt)) {
      scheduleReconnect()
    }
  })

  bot.on('kicked', (reason) => {
    const text = typeof reason === 'string' ? reason : JSON.stringify(reason)
    state.lastError = `被服务器踢出：${text}`.slice(0, 500)
    addEvent('kicked', state.lastError)
    const kick = classifyKick(text)
    if (kick === 'premium') {
      state.kickTerminal = true
      if (state.info) state.info.autoReconnect = false
      addEvent('error', '服务器要求正版验证（online-mode=true），离线登录进不去；换离线服，或旧版服改用 auth=microsoft。')
    } else if (kick === 'version') {
      state.kickTerminal = true
      if (state.info) state.info.autoReconnect = false
      addEvent('error', '客户端/服务器版本不匹配，已停止重连；用 mc_join 的 version 参数指定服务器版本。')
    } else if (kick === 'terminal') {
      // 封禁/白名单这类重连一万次也没用，直接停；下次 mc_join 会重置。
      state.kickTerminal = true
      if (state.info) state.info.autoReconnect = false
      addEvent('error', '踢出原因是封禁/白名单/黑名单，停止自动重连。')
    } else if (kick === 'transient') {
      // 满员/重复登录/重启：服务器恢复后马上能进，不该把退避次数耗尽。
      state.reconnectAttempts = 0
      addEvent('system', '踢出原因是临时性的（满员/重复登录/重启），恢复后会重试。')
    }
    log('[mc] kicked:', state.lastError)
  })

  bot.on('error', (error) => {
    state.lastError = String(error?.message ?? error).slice(0, 500)
    addEvent('error', state.lastError)
    log('[mc] error:', state.lastError)
  })

  bot.on('chat', (username, message) => {
    if (username === bot.username) return
    addEvent('chat', `<${username}> ${message}`)
  })

  bot.on('whisper', (username, message) => {
    addEvent('whisper', `<${username}> 私聊: ${message}`)
  })

  bot.on('messagestr', (message, position) => {
    const text = String(message ?? '').trim()
    if (!text) return
    // 登录提示有的服走 system，有的走 chat/game_info，识别不受 position 限制；
    // 事件流仍只收 system/game_info，避免把普通聊天记两遍。
    authFlow.handleText(text)
    if (position !== 'system' && position !== 'game_info') return
    addEvent('system', text)
  })

  bot.on('playerJoined', (player) => {
    if (player.username !== bot.username) addEvent('join', `${player.username} 加入服务器`)
  })

  bot.on('playerLeft', (player) => {
    if (player.username !== bot.username) addEvent('leave', `${player.username} 离开服务器`)
  })

  bot.on('death', () => {
    addEvent('death', `${bot.username} 死了，位置 ${fmtPos(bot.entity.position)}`)
  })

  bot.on('entityHurt', (entity) => {
    if (entity === bot.entity) addEvent('hurt', `受伤，生命 ${bot.health.toFixed(0)}/20`)
  })

  bot.on('rain', () => {
    if (bot.isRaining) addEvent('weather', '开始下雨')
  })
}

function scheduleReconnect() {
  if (state.reconnectTimer || !state.info) return
  if (state.kickTerminal) return
  if (state.reconnectAttempts >= 8) {
    addEvent('error', '自动重连已达 8 次，停止重试；需要时再 mc_join。')
    return
  }
  const delay = Math.min(60_000, 2_000 * 2 ** state.reconnectAttempts)
  state.reconnectAttempts += 1
  // 以 hook 覆盖前的稳定身份为底，重新进服时再由 hook 取新的一次性码/令牌。
  const info = { ...(state.preHookInfo ?? state.info) }
  addEvent('system', `将在 ${Math.round(delay / 1000)}s 后自动重连（第 ${state.reconnectAttempts} 次）`)
  state.reconnectTimer = setTimeout(async () => {
    state.reconnectTimer = null
    if (state.manualLeave || state.bot || rustBackend.active) return
    // 重连也要重新取部署侧的身份（一次性进服码 5 分钟失效、用一次即废）。
    let next = info
    try {
      next = await applyJoinHook(info, 'reconnect')
    } catch (error) {
      state.lastError = error.message
      addEvent('error', `自动重连前的进服适配失败：${error.message}`)
      scheduleReconnect()
      return
    }
    if (state.manualLeave || state.bot || rustBackend.active) return
    log('[mc] reconnecting to', next.host, next.port)
    if (next.backend === 'rust') {
      state.manualLeave = false
      state.info = next
      authFlow.attach(next, { password: next.password, register: next.register })
      rustBackend.start(next).then(() => {
        state.reconnectAttempts = 0
        addEvent('system', '26.2 自动重连成功')
        setTimeout(() => { if (rustBackend.active) authFlow.fallbackLogin() }, 2500).unref?.()
      }).catch((error) => {
        state.lastError = error.message
        addEvent('error', `26.2 自动重连失败：${error.message}`)
        scheduleReconnect()
      })
      return
    }
    try {
      connect(next, { reconnecting: true })
    } catch (error) {
      state.lastError = error.message
      scheduleReconnect()
    }
  }, delay)
  state.reconnectTimer.unref()
}

function connect(info, { reconnecting = false } = {}) {
  const options = {
    host: info.host,
    port: info.port,
    username: info.username,
    auth: info.auth === 'microsoft' ? 'microsoft' : 'offline',
    version: info.version || false,
    checkTimeoutInterval: 60_000,
    // 断线后不让 mineflayer 自己刷屏，由上面的退避逻辑接管。
    hideErrors: false,
  }
  log('[mc] connecting', options.host + ':' + options.port, 'as', options.username, 'version', options.version || 'auto')
  const bot = mineflayer.createBot(options)
  state.bot = bot
  state.joined = false
  state.connecting = true
  state.reconnectingAttempt = reconnecting
  state.info = info
  state.manualLeave = false
  authFlow.attach(info, { password: info.password ?? info.register, register: info.register ?? info.password })
  bot.loadPlugin(pathfinder)
  bot.loadPlugin(collectBlockPlugin)
  attachEvents(bot)
  return bot
}

async function waitForSpawn(bot, timeoutMs) {
  if (state.joined && state.bot === bot) return
  await new Promise((resolve, reject) => {
    let timer = null
    const cleanup = () => {
      clearTimeout(timer)
      bot.removeListener('spawn', onSpawn)
      bot.removeListener('end', onEnd)
      bot.removeListener('kicked', onKick)
      bot.removeListener('error', onError)
    }
    const onSpawn = () => {
      cleanup()
      resolve()
    }
    const onEnd = (reason) => {
      cleanup()
      reject(new Error(`连接被关闭：${reason || '(无原因)'}`))
    }
    const onKick = (reason) => {
      cleanup()
      const text = typeof reason === 'string' ? reason : JSON.stringify(reason)
      reject(new Error(`被服务器踢出：${text}`))
    }
    const onError = (error) => {
      cleanup()
      reject(new Error(`连接失败：${error?.message ?? error}`))
    }
    timer = setTimeout(() => {
      cleanup()
      reject(new Error(`连接超时（${Math.round(timeoutMs / 1000)}s）：服务器无响应或版本不匹配`))
    }, timeoutMs)
    bot.once('spawn', onSpawn)
    bot.once('end', onEnd)
    bot.once('kicked', onKick)
    bot.once('error', onError)
  })
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------

let actionQueue = Promise.resolve()

function action(name, fn) {
  const run = actionQueue.then(async () => {
    state.task = name
    try {
      return await fn()
    } finally {
      state.task = ''
    }
  })
  actionQueue = run.catch(() => {})
  return run
}

async function approach(bot, target, range = 2, timeoutMs = 20_000) {
  const GoalNear = goals.GoalNear
  if (bot.entity.position.distanceTo(target) <= range) return
  state.task = `走近 ${fmtPos(target)}`
  try {
    await withTimeout(
      bot.pathfinder.goto(new GoalNear(target.x, target.y, target.z, range)),
      timeoutMs,
      '寻路',
    )
  } catch (error) {
    try {
      bot.pathfinder.setGoal(null)
      bot.clearControlStates()
    } catch {}
    throw error
  } finally {
    state.task = ''
  }
}

function inventorySummary(bot) {
  const items = bot.inventory.items()
  const counts = new Map()
  for (const item of items) counts.set(item.name, (counts.get(item.name) ?? 0) + item.count)
  const total = items.reduce((sum, item) => sum + item.count, 0)
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1])
  return {
    slots: items.length,
    total,
    text: sorted.map(([name, count]) => `${name}×${count}`).join(', '),
  }
}

const HOSTILE = /zombie|skeleton|creeper|spider|witch|slime|phantom|pillager|vindicator|ravager|blaze|ghast|magma|husk|stray|drowned|wither|ender_dragon|guardian|shulker|hoglin|zoglin|piglin|brute|evoker|vex|silverfish|endermite|warden|breeze|bogged/i

function findEntity(bot, name) {
  const needle = cleanText(name, 64).toLowerCase()
  let best = null
  let bestDist = Infinity
  for (const entity of Object.values(bot.entities)) {
    if (entity === bot.entity) continue
    const hay = `${entity.name ?? ''} ${entity.username ?? ''} ${entity.displayName ?? ''}`.toLowerCase()
    if (needle && !hay.includes(needle)) continue
    const d = entity.position.distanceTo(bot.entity.position)
    if (d < bestDist) {
      best = entity
      bestDist = d
    }
  }
  return best
}

function nearestTarget(bot, name) {
  if (name) return findEntity(bot, name)
  const players = Object.values(bot.entities).filter((e) => e !== bot.entity && e.type === 'player')
  void players
  const mobs = Object.values(bot.entities)
    .filter((e) => e !== bot.entity && e.type === 'mob')
    .sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position))
  return mobs.find((e) => HOSTILE.test(e.name ?? '')) ?? mobs[0] ?? null
}

function entityText(entity, bot) {
  const d = entity.position.distanceTo(bot.entity.position)
  const kind = entity.type === 'player' ? `玩家 ${entity.username ?? entity.name}` : `生物 ${entity.name ?? '?'}`
  const hp = typeof entity.health === 'number' ? ` 生命 ${entity.health.toFixed(0)}` : ''
  return `${kind}｜距离 ${d.toFixed(1)}｜${fmtPos(entity.position)}${hp}`
}

function rustStatusText() {
  const info = rustBackend.info
  const snapshot = rustBackend.snapshot
  if (!info || !snapshot) return `正在连接 26.2 服务器（Rust 后端）……`
  const [x, y, z] = snapshot.position ?? [0, 0, 0]
  const held = snapshot.held ? `${snapshot.held.name}×${snapshot.held.count}` : '空手'
  const carried = snapshot.carried ? `${snapshot.carried.name}×${snapshot.carried.count}` : ''
  const players = snapshot.players ?? []
  const items = snapshot.inventory ?? []
  const lines = [
    `在线：${info.username} @ ${info.host}:${info.port}（26.2 / 协议 ${snapshot.protocol}，Rust 后端，离线）`,
    `位置：x=${x.toFixed(1)} y=${y.toFixed(1)} z=${z.toFixed(1)}`,
    `视角：yaw=${String(snapshot.yaw)} pitch=${String(snapshot.pitch)}`,
    `生命 ${Number(snapshot.health).toFixed(0)}/20｜饥饿 ${snapshot.food}/20｜等级 ${snapshot.experience_level}`,
    `玩家列表（${players.length}）：${players.join(', ') || '无'}`,
    authFlow.statusLine(),
    `手持：${held}｜背包 ${items.length} 格 / ${items.reduce((sum, item) => sum + item.count, 0)} 件${carried ? `｜光标挂着：${carried}` : ''}`,
    items.length ? `背包内容：${items.map((item) => `${item.name}×${item.count}`).join(', ')}` : '',
    '后端能力：聊天/命令/视角/基础移动/寻路（A* 体素网格，会绕路·挖隧道·搭桥）/挖掘/放置/方块查询/实体扫描（RCON）/抽样扫描/攻击/跟随/捡拾/吃东西/快捷栏/背包点击·搬移·丢弃·主副手交换/容器（开箱·整组存取·关箱）/服务端辅助合成/重生。',
  ].filter(Boolean)
  return lines.join('\n')
}

/** 容器菜单里箱子区的大小：箱子类 rows*9；无 rows 的菜单（熔炉等）从最大槽号倒推。 */
function rustContainerSize(container) {
  if (!container) return 0
  if (container.rows > 0) return container.rows * 9
  const maxSlot = (container.slots ?? []).reduce((m, e) => Math.max(m, Number(e.slot) || 0), -1)
  return Math.max(0, maxSlot + 1 - 36)
}

function rustContainerText(snapshot) {
  const c = snapshot.container
  if (!c) return ''
  const size = rustContainerSize(c)
  const lines = [`打开的容器：「${c.title || '?'}」（id=${c.id}，菜单 ${c.menu}，箱子区 ${size} 格，菜单槽快照 ${(c.slots ?? []).length} 条）`]
  const chestItems = (c.slots ?? []).filter((e) => e.slot < size)
  const playerItems = (c.slots ?? []).filter((e) => e.slot >= size)
  if (chestItems.length) {
    lines.push(`箱内：${chestItems.map((e) => `${invName(e)}×${e.count}(槽${e.slot})`).join(', ')}`)
  } else {
    lines.push('箱内：（空）')
  }
  if (playerItems.length) {
    lines.push(`容器内背包区：${playerItems.map((e) => `${invName(e)}×${e.count}(槽${e.slot})`).join(', ')}`)
  }
  return lines.join('\n')
}

function rustInventoryText() {
  const snapshot = rustBackend.snapshot
  if (!snapshot) throw new Error('26.2 世界尚未加载完成')
  const held = snapshot.held ? `${snapshot.held.name}×${snapshot.held.count}` : '空手'
  const lines = [`手持：${held}`]
  if (snapshot.carried) lines.push(`光标挂着：${snapshot.carried.name}×${snapshot.carried.count}（点空槽放下）`)
  lines.push(`背包（${snapshot.inventory.length} 格）：`)
  for (const item of [...snapshot.inventory].sort((a, b) => a.name.localeCompare(b.name))) {
    lines.push(`- ${item.name} ×${item.count}（槽 ${item.slot}）`)
  }
  if (snapshot.inventory.length === 0) lines.push('（空）')
  const containerText = rustContainerText(snapshot)
  if (containerText) lines.push(containerText)
  return lines.join('\n')
}

async function rustLook(yaw, pitch) {
  const snapshot = await rustBackend.request('status')
  rustBackend.snapshot = snapshot
  if (snapshot.yaw === null || snapshot.pitch === null) throw new Error('还读不到当前视角')
  // native status 的 yaw/pitch 直接是角度（实测 18.04°/-295.8°），别再乘 180/π。
  const currentYaw = Number(snapshot.yaw)
  const currentPitch = Number(snapshot.pitch)
  const targetYaw = yaw * 180 / Math.PI
  const targetPitch = pitch * 180 / Math.PI
  // 任意大小差值都能安全归一到 ±180：((x%360)+540)%360-180。
  const deltaYaw = (((targetYaw - currentYaw) % 360 + 540) % 360) - 180
  const deltaPitch = Math.max(-90, Math.min(90, targetPitch)) - currentPitch
  const lookUnits = [deltaYaw / 0.15, deltaPitch / 0.15]
  if (!Number.isFinite(lookUnits[0]) || !Number.isFinite(lookUnits[1])
    || Math.abs(lookUnits[0]) > 2400 || Math.abs(lookUnits[1]) > 2400) {
    throw new Error(`视角增量超限：yaw=${String(snapshot.yaw)} pitch=${String(snapshot.pitch)} → look=[${lookUnits.map((v) => v.toFixed(0)).join(', ')}]`)
  }
  await rustBackend.request('move', { buttons: 0, look: lookUnits })
}

// ---------------------------------------------------------------------------
// RCON 世界查询（26.2 Rust 后端只有键鼠级原语，方块/实体数据走 mc-vanilla RCON）
// ---------------------------------------------------------------------------

/** `execute if block x y z <name>`：name 支持 #tag。 */
async function rustBlockIs(x, y, z, name) {
  return /passed/i.test(await rconCommand(`execute if block ${x} ${y} ${z} ${name}`))
}

async function rustBlockIsAir(x, y, z) {
  return (await rustBlockIs(x, y, z, 'minecraft:air'))
    || (await rustBlockIs(x, y, z, 'minecraft:cave_air'))
    || (await rustBlockIs(x, y, z, 'minecraft:void_air'))
}

/** 玩家/任意具名实体坐标（原版 data get entity <name> Pos）。 */
async function rustEntityPos(name) {
  const out = await rconCommand(`data get entity ${name} Pos`)
  const m = out.match(/\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]/)
  if (!m) return null
  return [Number(m[1]), Number(m[2]), Number(m[3])]
}

async function rustOnlinePlayers() {
  const out = await rconCommand('list')
  const m = out.match(/online:\s*(.*)$/i)
  if (!m || !m[1].trim()) return []
  return m[1].split(',').map((s) => s.trim()).filter(Boolean)
}

/**
 * 附近实体（Rust 客户端内存快照，毫秒级）：[{id, kind, name, position, distance}]。
 * name 是 mob 类型（zombie/…）或 "player"；不再靠 RCON 逐类型打点。
 */
async function rustNearbyEntities(radius = 48) {
  const res = await rustBackend.request('entities', { radius })
  return res?.entities ?? []
}

/** 战斗前换武器：优先最好的剑，没有就保持当前手持（空手也能打）。返回武器名或 null。 */
const WEAPON_ORDER = ['netherite_sword', 'diamond_sword', 'iron_sword', 'stone_sword', 'wooden_sword', 'golden_sword']
async function rustEquipWeapon() {
  rustBackend.snapshot = await rustBackend.request('status')
  const have = new Set((rustBackend.snapshot.inventory ?? []).map((e) => invName(e)))
  const want = WEAPON_ORDER.find((name) => have.has(name))
  if (!want) return null
  const slot = await rustEnsureHotbar(want)
  await rustBackend.request('select', { slot })
  return want
}

/** 垫脚/搭桥用方块：优先随便一块能放的，背包没有就返回 null（不许挖地形换方块）。 */
const BRIDGE_ORDER = ['dirt', 'cobblestone', 'stone', 'oak_planks', 'oak_log', 'gravel', 'sand', 'andesite', 'diorite', 'granite']
async function rustBridgeItem() {
  const snap = await rustBackend.request('status')
  const have = new Set((snap.inventory ?? []).map((e) => invName(e)))
  return BRIDGE_ORDER.find((name) => have.has(name)) ?? null
}

/**
 * 直发攻击实体（无需瞄准/转视角）。entity id 来自 rustNearbyEntities。
 * 返回后端是否接受（实体不存在时后端仍会发，服务端忽略）。
 */
async function rustAttackEntity(entityId) {
  return rustBackend.request('attack_entity', { entity: entityId })
}

/**
 * 实体快照里找目标：先用类型名（zombie/cow/…）；找不到再看是不是玩家名，
 * 用 RCON 坐标匹配最近的 player 实体（快照里的玩家没有名字）。
 */
async function rustFindTarget(name, radius = 48) {
  const want = normalizeItem(name)
  const list = await rustNearbyEntities(radius)
  const direct = list.find((e) => e.name === want)
  if (direct) return direct
  const p = await rustEntityPos(name).catch(() => null)
  if (!p) return null
  const players = list.filter((e) => e.name === 'player')
  players.sort((a, b) =>
    Math.hypot(a.position[0] - p[0], a.position[2] - p[2])
    - Math.hypot(b.position[0] - p[0], b.position[2] - p[2]))
  return players[0] ?? null
}

/** 最近一次 nav 体素网格（几秒内复用，放置时不用反复取）。 */
let _navGridCache = null
let _navGridAt = 0
async function navGrid(maxAgeMs = 4000) {
  const now = Date.now()
  if (_navGridCache && now - _navGridAt < maxAgeMs) return _navGridCache
  const res = await rustBackend.request('nav', { radius: 12, dy_min: -10, dy_max: 8 })
  if (!res || !Array.isArray(res.runs)) throw new Error('nav 返回缺 runs')
  _navGridCache = decodeGrid(res)
  _navGridAt = now
  return _navGridCache
}

/** 两点间三维距离（位置数组）。 */
function dist3(a, b) {
  return Math.hypot((a?.[0] ?? 0) - b[0], (a?.[1] ?? 0) - b[1], (a?.[2] ?? 0) - b[2])
}

/** travel 的返回值是不是「真到了」：null/非字符串（A* 没路线）一律算没到。 */
function travelArrived(leg) {
  if (typeof leg !== 'string') return false
  return !/遇阻|被挡死|超时|中断|缺镐|没找到路|高度不符/.test(leg)
}

/**
 * 靠近 (tx,tz) 到 wantDist 内：让 A* travel 走到目标周围一圈的落点（禁挖，
 * 只走现有地形），走不通再退回直线贴脸。比盲目直线走靠谱得多。
 * 目标格本身可能被实体/方块占着，所以不是直接走到目标格，而是依次试外圈候选点。
 */
async function rustApproachViaTravel(tx, tz, wantDist, timeoutMs = 12_000, ty = null) {
  // ty 给了才有「上行模式」（挖楼梯/垫块直拔）；目标是天上的掉落物/方块时必须传，
  // 否则 A* 只在脚下这层规划，永远不上崖。
  const targetY = Number.isFinite(ty) ? ty : null
  const snap = await rustBackend.request('status')
  const [px, py, pz] = snap.position ?? []
  // ty 给了时高度也要算进去：水平贴近但人在崖下不算到（否则 collect 会在崖底空转）
  const near = (ax, ay, az) => Math.hypot(tx - ax, tz - az) <= wantDist
    && (targetY === null || Math.abs(targetY - ay) <= 1.6)
  if ([px, pz].every(Number.isFinite) && near(px, py, pz)) return true
  const deadline = Date.now() + timeoutMs
  const r = Math.max(1.5, wantDist - 0.5)
  const ring = [
    [r, 0], [-r, 0], [0, r], [0, -r],
    [r * 0.7, r * 0.7], [-r * 0.7, r * 0.7], [r * 0.7, -r * 0.7], [-r * 0.7, -r * 0.7],
  ]
  // 目标明显在头顶：先试「垫块搭桥/跳起上拔」——A* 爬不上多格高差，
  // 走别的 pass 只会贴着墙原地跳，把时限烧光。其余情况先禁挖、再挖、最后垫块。
  const bridgeItem = await rustBridgeItem().catch(() => null)
  const wantUp = targetY !== null && targetY > py + 1.2
  const passes = wantUp && bridgeItem
    ? [{ dig: true, bridge: bridgeItem }, { dig: false, bridge: null }]
    : [{ dig: false, bridge: null }, { dig: true, bridge: null }]
  if (!wantUp && bridgeItem) passes.push({ dig: true, bridge: bridgeItem })
  for (const { dig, bridge } of passes) {
    for (const [dx, dz] of ring) {
      const left = deadline - Date.now()
      if (left < 500) break
      try {
        const leg = await rustBehavior.travel({
          x: tx + dx, z: tz + dz, y: targetY, timeoutMs: Math.min(6_000, left), dig, bridge,
        })
        if (travelArrived(leg)) return true
      } catch {}
      const now = await rustBackend.request('status')
      const [ax, ay, az] = now.position ?? []
      if ([ax, az].every(Number.isFinite) && near(ax, ay, az)) return true
    }
  }
  // A* 走不通（隔墙/隔水）：最后退回直线贴脸，能贴多近算多近
  await rustApproach(tx, tz, wantDist, Math.min(5_000, Math.max(1_000, deadline - Date.now())))
  return false
}

/** 追击移动目标：A* 抄近路，走不通退回直线贴脸。 */
async function rustChase(tx, tz, wantDist, timeoutMs = 10_000, ty = null) {
  await rustApproachViaTravel(tx, tz, wantDist, timeoutMs, ty)
}

/** 挖掘/攻击前先走到目标附近（直线 + 遇阻跳，复用 goto 的走法）。 */
async function rustApproach(tx, tz, wantDist, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs
  let lastDist = Infinity
  let stuckRounds = 0
  for (;;) {
    const snapshot = await rustBackend.request('status')
    const [px, , pz] = snapshot.position ?? []
    if (![px, pz].every(Number.isFinite)) throw new Error('还读不到当前位置')
    const dist = Math.hypot(tx - px, tz - pz)
    if (dist <= wantDist) return dist
    if (Date.now() > deadline) return dist
    const blocked = lastDist - dist < 0.05
    stuckRounds = blocked ? stuckRounds + 1 : 0
    const jump = stuckRounds >= 2 ? 16 : 0
    await rustLook(Math.atan2(-(tx - px), tz - pz), 0)
    await rustBackend.request('move', { buttons: 1 | jump, look: [0, 0] })
    lastDist = dist
    await sleep(450)
  }
}

/** 面向方块中心看齐（挖掘/放置共用）。 */
async function rustLookAtBlock(x, y, z) {
  await rustLookAtPoint(x + 0.5, y + 0.5, z + 0.5)
}

/**
 * 挖掉一格（调用方保证已靠近）：直发 dig_at——Rust 侧构造原版 dig 包
 * （START → 停留 hold_ms → STOP），不经过鼠标瞄准/按住攻击。
 * 挖不穿就把 hold_ms 翻倍重试（黑曜石等硬方块）。
 * 返回 true=挖掉了；false=超时没挖穿。
 */
async function rustDigBlock(x, y, z, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs
  let hold = 1_200
  while (Date.now() < deadline) {
    await rustBackend.request('dig_at', { pos: [x, y, z], hold_ms: hold })
    const until = Math.min(Date.now() + hold + 1_500, deadline)
    while (Date.now() < until) {
      await sleep(250)
      if (await rustBlockIsAir(x, y, z)) return true
    }
    hold = Math.min(hold * 2, 8_000)
  }
  return false
}

/**
 * 找 (x,y,z) 可依附的支撑面：底面优先，其次四侧。
 * 返回 {pos:[sx,sy,sz], face}，face 是「从支撑块看向目标格」的命中面。
 */
async function rustPlaceSupport(x, y, z) {
  if (!await rustBlockIsAir(x, y - 1, z)) return { pos: [x, y - 1, z], face: 1 }
  const sides = [
    [[x - 1, y, z], 5], // 西侧邻居 → 打它的东面
    [[x + 1, y, z], 4], // 东侧邻居 → 打它的西面
    [[x, y, z - 1], 3], // 北侧邻居 → 打它的南面
    [[x, y, z + 1], 2], // 南侧邻居 → 打它的北面
  ]
  for (const [p, face] of sides) {
    if (!await rustBlockIsAir(...p)) return { pos: p, face }
  }
  return null
}

/**
 * 在 (x,y,z) 放一格 itemName（调用方保证目标格是空气）：
 * 找支撑面 → 快捷栏备物 → 走近 → 直发 use_at（Rust 侧构造原版 use_on 包，
 * 带精确坐标/命中面，不经过鼠标瞄准——服务端只校验距离，不校验视线）。
 * 返回 true=放下了。
 */
async function rustPlaceBlock(x, y, z, itemName, { approach = true } = {}) {
  if (!await rustBlockIsAir(x, y, z)) throw new Error(`(${x},${y},${z}) 不是空气，放不进去`)

  const support = await rustPlaceSupport(x, y, z)
  if (!support) {
    throw new Error(`(${x},${y},${z}) 下方和同层四周都是空气，没有可依附的方块，放不上`)
  }

  const slot = await rustEnsureHotbar(itemName)
  if (approach) {
    // 服务端按「眼睛 → 命中点」直线距离校验，水平贴近就够（无需视线）；
    // 只有目标在头顶时才要求爬上去，同层/脚下的放置点不传 y（免得下坡找高度）。
    rustBackend.snapshot = await rustBackend.request('status')
    const myY = rustBackend.snapshot.position?.[1]
    await rustApproachViaTravel(x + 0.5, z + 0.5, 3.0, 15_000, Number.isFinite(myY) && y > myY + 1.2 ? y : null)
  }
  // 刚进服/刚切栏时偶发第一包不生效（服务端状态没对齐），重试一次比直接报错好
  for (let attempt = 0; attempt < 2; attempt++) {
    await rustBackend.request('stop')
    await rustBackend.request('select', { slot })
    await rustBackend.request('use_at', { pos: support.pos, face: support.face })

    // 服务端处理有延迟：轮询到方块出现为止，别一次没读到就判死
    const deadline = Date.now() + 1_500
    while (Date.now() < deadline) {
      await sleep(150)
      if (!await rustBlockIsAir(x, y, z)) return true
    }
    await sleep(250)
  }
  throw new Error(
    `use_at 没放上（重试 2 次；支撑 ${support.pos.join(',')} 面 ${support.face}，` +
    `物品 ${itemName}，槽 ${slot}）`,
  )
}

/** native status 里物品名带 minecraft: 前缀，比对前剥掉。 */
function invName(entry) {
  return String(entry?.name ?? '').replace(/^minecraft:/, '')
}

/** storage 槽 27–35 是快捷栏（0–8）；held=slots[selected] 实测与 27 号槽一致。 */
function isHotbarSlot(slot) {
  return slot >= 27 && slot <= 35
}

function hotbarIndexOf(entry) {
  return entry.slot - 27
}

/** 玩家背包窗口（无容器打开时）菜单槽 = 存储槽 + 9（窗口 0 布局：0-4 合成/5-8 盔甲/9-35 主背包/36-44 快捷栏）。 */
function storageToMenuSlot(slot) {
  return slot + 9
}

/**
 * 确保某物品在快捷栏（0-8）：不在就用原生「数字键交换」（等价悬停按 1-9）换进来，
 * 优先换进空位，全满则与当前主手交换。返回它的快捷栏槽位；挪不动抛错。
 */
async function rustEnsureHotbar(itemName) {
  rustBackend.snapshot = await rustBackend.request('status')
  // 优先用已经在快捷栏里的那份——背包里可能有多份同物品，
  // 挑到非快捷栏的那份会白折腾一次换栏（甚至换失败）。
  const all = (rustBackend.snapshot.inventory ?? []).filter((e) => invName(e) === itemName)
  let entry = all.find((e) => isHotbarSlot(e.slot)) ?? all[0]
  if (!entry) throw new Error(`背包里没有 ${itemName}（mc_inventory 可查）`)
  if (isHotbarSlot(entry.slot)) return hotbarIndexOf(entry)
  const occupied = new Set(
    (rustBackend.snapshot.inventory ?? []).filter((e) => isHotbarSlot(e.slot)).map(hotbarIndexOf),
  )
  let target = -1
  for (let i = 0; i < 9; i++) if (!occupied.has(i)) { target = i; break }
  if (target < 0) target = clampInt(rustBackend.snapshot.selected, 0, 0, 8)
  await rustBackend.request('inv_swap', { inv_slot: storageToMenuSlot(entry.slot), button: target })
  await sleep(350)
  rustBackend.snapshot = await rustBackend.request('status')
  entry = (rustBackend.snapshot.inventory ?? []).find((e) => invName(e) === itemName && isHotbarSlot(e.slot))
  if (!entry || !isHotbarSlot(entry.slot)) throw new Error(`${itemName} 换不进快捷栏（交换后仍在 ${entry?.slot} 槽）`)
  addEvent('system', `背包操作：把 ${itemName} 换进快捷栏 ${target + 1} 号位`)
  return hotbarIndexOf(entry)
}

async function rustLookAtPoint(x, y, z) {
  const snapshot = await rustBackend.request('status')
  const [px, py, pz] = snapshot.position ?? []
  if (![px, py, pz].every(Number.isFinite)) throw new Error('还读不到当前位置')
  const dx = x - px
  const dy = y - (py + 1.62)
  const dz = z - pz
  await rustLook(Math.atan2(-dx, dz), -Math.atan2(dy, Math.hypot(dx, dz)))
}

/**
 * 贴底面放置专用视角：几乎垂直向下看方块中心。
 * 贴底面命中判定比贴侧面严得多——斜着看中心常常打不中底面，
 * 放置会静默失败（方块没出来）。所以这里把 pitch 压到接近 -90°。
 */
/**
 * 贴底面放置专用视角：让射线垂直向下命中支撑方块的顶面。
 *
 * 关键：贴底面放置只有「射线穿过目标格、且命中的是那一格的底面」才成立。
 * 斜着看方块中心，射线往往先撞上侧面/自己脚下的方块 → 物品被消耗但方块没出现。
 * 所以这里把 pitch 直接压到 -90°（正下方），yaw 对准目标格中心。
 */
async function rustLookAtBottom(x, y, z) {
  // 瞄点 = 支撑方块的顶面中心（x+0.5, y, z+0.5；y 是目标格底面 = 支撑块顶面高度）。
  //
  // 俯角必须「刚好落在那个顶面方格内」：
  //  - 太陡（>60°）→ 射线在越过目标格之前就从旁边穿出去，打在空气里，物品被消耗但方块没出来；
  //  - 太平 → 会被中间挡路的方块截住，打到别的面上。
  // 所以按几何精确瞄顶面中心：pitch = atan2(眼高 - 顶面高度, 水平距离)。
  const snapshot = await rustBackend.request('status')
  const [px, py, pz] = snapshot.position ?? []
  if (![px, py, pz].every(Number.isFinite)) throw new Error('还读不到当前位置')
  const dx = x + 0.5 - px
  const dz = z + 0.5 - pz
  const dy = (py + 1.62) - y            // 眼高比顶面高多少
  const horiz = Math.hypot(dx, dz)
  const pitch = horiz < 0.30 ? 1.45 : Math.min(1.45, Math.max(0.05, Math.atan2(dy, horiz)))
  await rustLook(Math.atan2(-dx, dz), pitch)
}

const RUST_FOODS = [
  'bread', 'cooked_beef', 'cooked_porkchop', 'cooked_chicken', 'cooked_mutton',
  'cooked_cod', 'cooked_salmon', 'baked_potato', 'apple', 'golden_apple',
  'carrot', 'beetroot', 'melon_slice', 'sweet_berries', 'cooked_rabbit',
]

/** 吃一口背包里找到的食物，返回汇报文本（/eat 路由与行为层 auto_eat 共用）。 */
async function rustEatFood() {
  rustBackend.snapshot = await rustBackend.request('status')
  const food0 = num(rustBackend.snapshot.food, NaN)
  const foodEntry = (rustBackend.snapshot.inventory ?? []).find((e) => RUST_FOODS.includes(invName(e)))
  if (!foodEntry) throw new Error(`快捷栏/背包里没找到能吃的（认这些：${RUST_FOODS.slice(0, 6).join('、')} 等）`)
  const slot = await rustEnsureHotbar(invName(foodEntry))
  await rustBackend.request('select', { slot })
  await rustBackend.request('use', { down: true })
  await sleep(1_900)  // 一口面包 1.6s+
  await rustBackend.request('use', { down: false })
  await sleep(400)
  rustBackend.snapshot = await rustBackend.request('status')
  const food1 = num(rustBackend.snapshot.food, NaN)
  const delta = Number.isFinite(food0) && Number.isFinite(food1) ? `（饥饿 ${food0} → ${food1}）` : ''
  return `吃了 ${invName(foodEntry)}×1${delta}。`
}

// ---------------------------------------------------------------------------
// 行为层（travel 任务 + 常驻行为 + 触发器），动作层原语见上方 rust* 函数
// ---------------------------------------------------------------------------

const rustBehavior = createBehaviorEngine({
  rust: rustBackend,
  rcon: rconCommand,
  // 本地世界扫描：客户端区块缓存在内存里，整盒查毫秒级零网络。
  // 替代 RCON 一格一探（每格 ~10ms，半径 12 三层扫要两分钟——「扫一下要半天」的根因）。
  nativeScan: async ({ names, radius, dyMin, dyMax, limit }) => {
    const res = await rustBackend.request('scan', {
      names, radius, dy_min: dyMin, dy_max: dyMax, limit,
    })
    if (Array.isArray(res?.unknown) && res.unknown.length) {
      log('[scan] 本地扫描跳过未知名:', res.unknown.join(','))
    }
    return res
  },
  // 体素导航：一次取整盒带方块名+碰撞顶高的格子（RLE），JS 侧跑 A*。
  // 有了它 travel 才不用每拍 RCON 盲探前方一格（「走两步停一下」的根因）。
  nav: async ({ radius = 16, dyMin = -8, dyMax = 8 } = {}) => {
    const res = await rustBackend.request('nav', {
      radius, dy_min: dyMin, dy_max: dyMax,
    })
    if (!res || !Array.isArray(res.runs)) throw new Error('nav 返回缺 runs')
    return res
  },
  addEvent,
  log,
  blockIs: rustBlockIs,
  blockIsAir: rustBlockIsAir,
  look: rustLook,
  lookAtPoint: rustLookAtPoint,
  digBlock: rustDigBlock,
  placeBlock: rustPlaceBlock,
  nearbyEntities: rustNearbyEntities,
  attackEntity: rustAttackEntity,
  eatFood: rustEatFood,
  ensureHotbar: rustEnsureHotbar,
  normalizeItem,
  entityPos: rustEntityPos,
  // 背包里有哪些工具（镐/斧/锹…）：A* 规划挖隧道前先问一句「有工具挖得动吗」，
  // 没有就别规划挖石头——那会两轮 12s 挖不破、白烧 24 秒还卡在原地（实机 11-07）。
  toolsInInventory: async () => {
    rustBackend.snapshot = await rustBackend.request('status')
    return (rustBackend.snapshot.inventory ?? [])
      .map((e) => invName(e))
      .filter((n) => /_(pickaxe|axe|shovel|hoe|sword)$/.test(n))
  },
  // 背包里某物品的总数（备料建房先清点库存再决定采多少）
  inventoryCount: async (itemName) => {
    rustBackend.snapshot = await rustBackend.request('status')
    const want = normalizeItem(itemName)
    return (rustBackend.snapshot.inventory ?? [])
      .filter((e) => invName(e) === want)
      .reduce((sum, e) => sum + (e.count ?? 1), 0)
  },
  // 服务端辅助合成：备料建房时「原木→木板」这类兑换走同一套 /craft 逻辑
  craftItem: async (result, times) => (await rustCraft(result, times)).message,
  statePath: path.join(path.dirname(CONFIG_PATH), 'behaviors.json'),
})

/** 会占用身体的旧式长动作：执行期间行为层只感知不动作，且打断进行中的 travel。 */
const RUST_BODY_PATHS = new Set([
  '/goto', '/dig', '/place', '/attack', '/follow', '/collect', '/unstuck',
  '/eat', '/look', '/look_at', '/use', '/equip', '/craft',
  '/container_take', '/container_put', '/container_close',
  '/inv_click', '/inv_drop', '/swap_hands',
])

/** 内置配方表（服务端辅助合成用）：产物 → [{ing, n}…]。 */
const RUST_RECIPES = {
  oak_planks: [{ ing: 'oak_log', n: 1, out: 4 }],
  spruce_planks: [{ ing: 'spruce_log', n: 1, out: 4 }],
  birch_planks: [{ ing: 'birch_log', n: 1, out: 4 }],
  stick: [{ ing: '#planks', n: 2, out: 4 }],
  crafting_table: [{ ing: '#planks', n: 4, out: 1 }],
  torch: [{ ing: 'stick', n: 1, out: 4 }, { ing: 'coal', n: 1, out: 4 }],
  furnace: [{ ing: 'cobblestone', n: 8, out: 1 }],
  chest: [{ ing: '#planks', n: 8, out: 1 }],
  bread: [{ ing: 'wheat', n: 3, out: 1 }],
  cookie: [{ ing: 'wheat', n: 2, out: 8 }, { ing: 'cocoa_beans', n: 1, out: 8 }],
  stone_pickaxe: [{ ing: 'cobblestone', n: 3, out: 1 }, { ing: 'stick', n: 2, out: 1 }],
  stone_axe: [{ ing: 'cobblestone', n: 3, out: 1 }, { ing: 'stick', n: 2, out: 1 }],
  stone_shovel: [{ ing: 'cobblestone', n: 1, out: 1 }, { ing: 'stick', n: 2, out: 1 }],
  stone_sword: [{ ing: 'cobblestone', n: 2, out: 1 }, { ing: 'stick', n: 1, out: 1 }],
  wooden_pickaxe: [{ ing: '#planks', n: 3, out: 1 }, { ing: 'stick', n: 2, out: 1 }],
  wooden_axe: [{ ing: '#planks', n: 3, out: 1 }, { ing: 'stick', n: 2, out: 1 }],
  wooden_sword: [{ ing: '#planks', n: 2, out: 1 }, { ing: 'stick', n: 1, out: 1 }],
  iron_ingot: [{ ing: 'raw_iron', n: 1, out: 1 }],
  gold_ingot: [{ ing: 'raw_gold', n: 1, out: 1 }],
  copper_ingot: [{ ing: 'raw_copper', n: 1, out: 1 }]
}

/**
 * 服务端辅助合成：clear 扣原料 + give 发产物（26.2 Rust 客户端没有工作台窗口
 * 协议，这是等价兑换，每次调用都如实报告）。/craft 端点与调度任务共用。
 */
async function rustCraft(result, times = 1) {
  if (!rconReady()) throw new Error('mc-vanilla 未开 RCON')
  const item = normalizeItem(result)
  const n = clampInt(times, 1, 1, 16)
  if (!item) throw new Error('result 必填（要合成的物品名）')
  const recipe = RUST_RECIPES[item]
  if (!recipe) {
    return { ok: true, message: `没有内置「${item}」的配方。已收录：${Object.keys(RUST_RECIPES).join(', ')}。` }
  }
  // 按一次合成的用量 × 次数核对背包
  rustBackend.snapshot = await rustBackend.request('status')
  const inv = rustBackend.snapshot.inventory ?? []
  const need = []
  for (const r of recipe) {
    const total = r.n * n
    if (String(r.ing).startsWith('#')) {
      // tag 原料：找一种「任意木板」类的具体物品当代表
      const base = r.ing.slice(1).replace(/s$/, '')
      const have = inv.find((e) => invName(e) === base || invName(e).endsWith(`_${base}`))
      if (!have || (have.count ?? 1) < total) {
        throw new Error(`#${r.ing.slice(1)} 需要 ${total} 个，背包${have ? `只有 ${have.count}` : '没有'}`)
      }
      need.push({ ...r, total, clear: invName(have) })
      continue
    }
    const have = inv.find((e) => invName(e) === r.ing)
    if (!have || (have.count ?? 1) < total) {
      throw new Error(`${r.ing} 需要 ${total} 个，背包${have ? `只有 ${have.count}` : '没有'}`)
    }
    need.push({ ...r, total, clear: r.ing })
  }
  const logs = []
  for (const r of need) {
    const out = await rconCommand(`clear ${DEFAULT_USERNAME} minecraft:${r.clear} ${r.total}`)
    logs.push(`扣 ${r.clear}×${r.total}：${/removed/i.test(out) ? 'ok' : cleanText(out, 80)}`)
  }
  const giveOut = (recipe.find((r) => r.out > 1)?.out ?? 1) * n
  const give = await rconCommand(`give ${DEFAULT_USERNAME} minecraft:${item} ${giveOut}`)
  logs.push(`发 ${item}×${giveOut}：${/gave/i.test(give) ? 'ok' : cleanText(give, 80)}`)
  addEvent('system', `服务端辅助合成 ${item}×${giveOut}`)
  return { ok: true, message: `合成 ${item}×${giveOut}（服务端辅助：clear 扣原料 + give 发产物）。\n${logs.join('\n')}` }
}

/** 后台长动作（跟随/捡拾）的取消旗，/stop 置位。 */
const rustLongTask = { cancel: false, name: '' }

/**
 * 通用任务调度端点（/schedule/*）：与后端无关，Rust/Mineflayer 两条路径共用。
 * 未连服时给出明确错误而不是「未知接口」。
 */
async function handleSchedule(path, query) {
  if (!rustBackend.active) {
    throw new Error('还没进服：先 mc_join 再创建调度任务（/schedule/* 只在 Rust 后端在线时可用）')
  }
  switch (path) {
    case '/schedule/create': {
      // 创建任务：type + priority + params(JSON)。复合类型可一次创建多个任务。
      const type = cleanText(query.get('type'), 40).trim()
      if (!type) throw new Error('type 必填（follow / mine / build / build_with_materials / collect / attack / eat / retaliate / follow_and_mine）')
      const priority = clampInt(query.get('priority'), 10, 1, 999)
      let params = {}
      const raw = cleanText(query.get('params'), 4000)
      if (raw) {
        try {
          params = JSON.parse(raw)
        } catch {
          throw new Error('params 必须是 JSON 对象')
        }
      }
      const timeoutSec = num(query.get('timeout'), NaN)
      if (Number.isFinite(timeoutSec) && timeoutSec > 0) params.timeoutMs = timeoutSec * 1000
      const { taskIds, message } = rustBehavior.createTaskFromRequest(type, priority, params)
      return { ok: true, taskIds, taskId: taskIds[0], message }
    }
    case '/schedule/tasks': {
      const id = cleanText(query.get('id'), 80)
      const limit = clampInt(query.get('limit'), 5, 1, 40)
      const finished = rustBehavior.snapshotHistory(limit)
      if (id) {
        const task = rustBehavior.getTask(id)
        if (task) {
          return { ok: true, tasks: [rustBehavior.taskSnapshot(task)], finished, message: `任务 ${id}：${task.type}（${task.status}）` }
        }
        const done = finished.find((t) => t.id === id)
        if (!done) throw new Error(`没有任务 ${id}（活跃和历史里都没有）`)
        return { ok: true, tasks: [], finished, message: `任务 ${id} 已结束：${done.status}${done.note ? `｜${done.note}` : ''}${done.error ? `｜${done.error}` : ''}` }
      }
      const tasks = rustBehavior.snapshotTasks()
      const lines = tasks.length
        ? tasks.map((t) => `• ${t.id}｜${t.type} p=${t.priority}｜${t.status}${t.state?.blocked ? `｜阻塞：${t.state.blocked}` : ''}${t.note ? `｜${t.note}` : ''}`)
        : ['（没有活跃任务）']
      const doneLines = finished.map((t) => `· 已结束 ${t.id}｜${t.type}｜${t.status}${t.note ? `｜${t.note}` : ''}${t.error ? `｜${t.error}` : ''}`)
      return { ok: true, tasks, finished, message: [...lines, ...(doneLines.length ? ['最近结束：', ...doneLines] : [])].join('\n') }
    }
    case '/schedule/cancel': {
      const taskId = cleanText(query.get('taskId') ?? query.get('id'), 80)
      if (!taskId) throw new Error('taskId 必填')
      const reason = cleanText(query.get('reason'), 120) || '手动取消'
      const ok = rustBehavior.cancelTask(taskId, reason)
      if (!ok) throw new Error(`没有任务 ${taskId}`)
      return { ok: true, message: `已取消任务 ${taskId}（${reason}）` }
    }
    case '/schedule/status': {
      const owner = rustBehavior.scheduler.currentOwner()
      const tasks = rustBehavior.snapshotTasks()
      const busy = Boolean(owner)
      const lastResults = rustBehavior.scheduler.lastResults ?? []
      const msg = tasks.length
        ? `调度器：${busy ? `正在执行 ${owner.type}（${owner.id}，p=${owner.priority}）` : '空闲'}，共 ${tasks.length} 个活跃任务。`
        : '调度器：没有活跃任务。'
      return {
        ok: true,
        executing: owner ? rustBehavior.taskSnapshot(owner) : null,
        running: tasks.length,
        tasks,
        lastTick: lastResults,
        message: msg,
      }
    }
    default:
      throw new Error(`未知调度端点 ${path}`)
  }
}

async function handleRust(path, query) {
  switch (path) {
    case '/leave':
      state.manualLeave = true
      state.info = null
      state.preHookInfo = null
      state.reconnectAttempts = 0
      state.kickTerminal = false
      authFlow.reset()
      rustBehavior.cancelAll('离开服务器')
      rustBehavior.cancelAllTasks('离开服务器')
      await rustBackend.stop()
      addEvent('system', '已主动离开 26.2 服务器')
      return { ok: true, message: '已离开服务器。' }
    case '/status':
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: rustStatusText() }
    case '/events':
      return {
        ok: true,
        message: eventsText(query.get('since'), clampInt(query.get('limit'), 25, 1, 100)),
        next: state.seq,
      }
    case '/inventory': {
      rustBackend.snapshot = await rustBackend.request('status')
      const inv = rustBackend.snapshot.inventory ?? []
      const summary = {}
      for (const entry of inv) {
        const name = invName(entry)
        summary[name] = (summary[name] ?? 0) + (entry.count ?? 1)
      }
      return { ok: true, inventory: summary, slots_used: inv.length, message: rustInventoryText() }
    }
    case '/chat': {
      const message = cleanText(query.get('message'), 240)
      if (!message) throw new Error('message 必填')
      await rustBackend.request('chat', { message })
      addEvent('system', `我发送：${message}`)
      return { ok: true, message: `已发送：${message}` }
    }
    case '/command': {
      let command = cleanText(query.get('command'), 200)
      if (!command.startsWith('/')) command = `/${command}`
      await rustBackend.request('command', { message: command })
      addEvent('system', `我执行命令：${command}`)
      return { ok: true, message: `已执行：${command}（需要 op 权限的命令只有 op 后可用）` }
    }
    case '/stop': {
      rustLongTask.cancel = true
      rustBehavior.cancelAll('手动停止')
      const cancelled = rustBehavior.cancelAllTasks('手动停止')
      await rustBackend.request('stop')
      return {
        ok: true,
        message: `已停止移动和持续操作（跟随/捡拾/travel 等长动作也已取消）${cancelled.length ? `，调度任务取消 ${cancelled.length} 个` : ''}。`,
      }
    }
    case '/look': {
      const yaw = num(query.get('yaw'), NaN)
      const pitch = num(query.get('pitch'), NaN)
      if (!Number.isFinite(yaw) || !Number.isFinite(pitch)) throw new Error('yaw、pitch 必填（弧度）')
      await rustLook(yaw, pitch)
      return { ok: true, message: `视角已转向 yaw=${yaw.toFixed(2)} pitch=${pitch.toFixed(2)}。` }
    }
    case '/look_at': {
      const x = num(query.get('x'), NaN)
      const y = num(query.get('y'), NaN)
      const z = num(query.get('z'), NaN)
      if (![x, y, z].every(Number.isFinite)) throw new Error('x、y、z 必填')
      const snapshot = await rustBackend.request('status')
      const [px, py, pz] = snapshot.position ?? []
      if (![px, py, pz].every(Number.isFinite)) throw new Error('还读不到当前位置')
      const dx = x - px
      const dy = y - (py + 1.62)
      const dz = z - pz
      await rustLook(Math.atan2(-dx, dz), -Math.atan2(dy, Math.hypot(dx, dz)))
      return { ok: true, message: `已看向 (${x},${y},${z})。` }
    }
    case '/equip': {
      const itemName = normalizeItem(cleanText(query.get('item'), 64))
      const destination = cleanText(query.get('destination'), 16) || 'hand'
      if (destination !== 'hand') throw new Error('26.2 Rust 后端目前只支持装备到 hand')
      rustBackend.snapshot = await rustBackend.request('status')
      const item = (rustBackend.snapshot.inventory ?? []).find((entry) => invName(entry) === itemName)
      if (!item) throw new Error(`背包里没有 ${itemName}`)
      const slot = isHotbarSlot(item.slot) ? hotbarIndexOf(item) : await rustEnsureHotbar(itemName)
      await rustBackend.request('select', { slot })
      return { ok: true, message: `已把 ${itemName} 拿到主手。` }
    }
    // ---- 背包/容器（原生窗口协议；槽号=界面槽号，mc_container 可查当前布局）----
    case '/container': {
      rustBackend.snapshot = await rustBackend.request('status')
      const text = rustContainerText(rustBackend.snapshot)
      if (!text) return { ok: true, message: '当前没有打开容器。对着箱子/熔炉 mc_use 打开后再看。' }
      return { ok: true, message: text }
    }
    case '/container_close':
      await rustBackend.request('container_close')
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: '已关闭容器。' }
    case '/container_take':
    case '/container_put': {
      const take = path === '/container_take'
      const itemName = normalizeItem(cleanText(query.get('item'), 64))
      if (!itemName) throw new Error('item 必填（物品名，或 all=全部）')
      const stacks = clampInt(query.get('stacks'), 36, 1, 36)
      rustBackend.snapshot = await rustBackend.request('status')
      const c = rustBackend.snapshot.container
      if (!c) throw new Error('当前没有打开的容器——先对着箱子 mc_use 打开')
      const size = c.rows > 0 ? c.rows * 9 : rustContainerSize(c)
      const targets = (c.slots ?? [])
        .filter((e) => (itemName === 'all' || invName(e) === itemName) && e.slot < size)
      let playerRegion = []
      if (!take) {
        // 放入：从背包快照按存储槽找，菜单槽 = 箱子区大小 + 存储槽
        playerRegion = (rustBackend.snapshot.inventory ?? [])
          .filter((e) => itemName === 'all' || invName(e) === itemName)
          .map((e) => ({ ...e, slot: size + e.slot }))
      }
      const list = take ? targets : playerRegion
      if (!list.length) throw new Error(take ? `容器里没有 ${itemName}` : `背包里没有 ${itemName}`)
      let moved = 0
      let clicks = 0
      for (const entry of list.slice(0, stacks)) {
        await rustBackend.request('inv_quick_move', { inv_slot: entry.slot, button: 0 })
        moved += entry.count ?? 1
        clicks += 1
        await sleep(250)
      }
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: take
        ? `从「${c.title || '容器'}」拿了 ${clicks} 组（约 ${moved} 个）${itemName === 'all' ? '物品' : itemName} 到背包。`
        : `往「${c.title || '容器'}」放了 ${clicks} 组（约 ${moved} 个）${itemName === 'all' ? '物品' : itemName}。` }
    }
    case '/inv_drop': {
      const itemName = normalizeItem(cleanText(query.get('item'), 64))
      if (!itemName) throw new Error('item 必填（要丢的物品名）')
      const whole = bool(query.get('whole'), true)
      rustBackend.snapshot = await rustBackend.request('status')
      const entry = (rustBackend.snapshot.inventory ?? []).find((e) => invName(e) === itemName)
      if (!entry) throw new Error(`背包里没有 ${itemName}`)
      if (isHotbarSlot(entry.slot) && hotbarIndexOf(entry) === Number(rustBackend.snapshot.selected)) {
        await rustBackend.request('drop_hand', { whole })
      } else {
        await rustBackend.request('inv_throw', { inv_slot: storageToMenuSlot(entry.slot), whole })
      }
      await sleep(300)
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: `已丢出 ${itemName}${whole ? '（整组）' : '×1'}。` }
    }
    case '/inv_click': {
      const slot = clampInt(query.get('slot'), NaN, -999, 999)
      if (!Number.isFinite(slot)) throw new Error('slot 必填（界面槽号）')
      const button = clampInt(query.get('button'), 0, 0, 8)
      await rustBackend.request('inv_click', { inv_slot: slot, button })
      await sleep(250)
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: `已点击槽 ${slot}（${button === 0 ? '左键' : '右键'}）。当前背包/容器见 mc_inventory。` }
    }
    case '/swap_hands':
      await rustBackend.request('swap_hands')
      await sleep(250)
      rustBackend.snapshot = await rustBackend.request('status')
      return { ok: true, message: '已交换主副手。' }
    case '/use':
      await rustBackend.request('use', { down: true })
      await sleep(300)
      await rustBackend.request('use', { down: false })
      return { ok: true, message: '已使用主手物品。' }
    case '/respawn':
      await rustBackend.request('respawn')
      return { ok: true, message: '已请求重生。' }
    case '/unstuck': {
      const snapshot = await rustBackend.request('status')
      const yaw = Number(snapshot.yaw ?? 0)
      for (let i = 0; i < 4; i++) {
        await rustLook(yaw + i * Math.PI / 2, 0)
        await rustBackend.request('move', { buttons: 1 | 16, look: [0, 0] })
        await sleep(550)
        await rustBackend.request('stop')
        await sleep(150)
      }
      await rustLook(yaw, Number(snapshot.pitch ?? 0))
      return { ok: true, message: '已执行四向跳跃移动脱困；请用 mc_status 检查新位置。' }
    }
    case '/goto': {
      // 26.2 简化寻路：朝目标持续前进，原地卡住（水平距离不缩）就跳一跳；
      // 不做 A*——遇高楼/岩浆绕行这类场景仍需人工，能走到大多数平视目标。
      const x = num(query.get('x'), NaN)
      const y = num(query.get('y'), NaN)
      const z = num(query.get('z'), NaN)
      if (![x, y, z].every(Number.isFinite)) throw new Error('x、y、z 必填')
      const timeoutMs = clampInt(query.get('timeout'), 60_000, 5_000, 180_000)
      const deadline = Date.now() + timeoutMs
      const start = Date.now()
      let lastDist = Infinity
      let stuckRounds = 0
      for (;;) {
        const snapshot = await rustBackend.request('status')
        const [px, , pz] = snapshot.position ?? []
        if (![px, pz].every(Number.isFinite)) throw new Error('还读不到当前位置')
        const dist = Math.hypot(x - px, z - pz)
        if (dist < 1.2) {
          await rustBackend.request('stop')
          const secs = ((Date.now() - start) / 1000).toFixed(1)
          return { ok: true, message: `已走到 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标 ${dist.toFixed(1)} 格（${secs}s）。` }
        }
        if (Date.now() > deadline) {
          await rustBackend.request('stop')
          return { ok: true, message: `超时停在 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标还剩 ${dist.toFixed(1)} 格——路线可能被挡，试试分段走或 /unstuck。` }
        }
        // 距离两轮不缩 → 判定卡住，用前进+跳顶一下。
        const blocked = lastDist - dist < 0.05
        stuckRounds = blocked ? stuckRounds + 1 : 0
        const jump = stuckRounds >= 2 ? 16 : 0
        const yawToTarget = Math.atan2(-(x - px), z - pz)
        await rustLook(yawToTarget, 0)
        await rustBackend.request('move', { buttons: 1 | jump, look: [0, 0] })
        lastDist = dist
        await sleep(500)
      }
    }
    case '/dig': {
      // RCON 验方块 → 靠近 → rustDigBlock（对准 → 按住攻击 → 轮询消失）
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，26.2 挖掘验证做不了')
      const x = clampInt(query.get('x'), NaN, -3e7, 3e7)
      const y = clampInt(query.get('y'), NaN, -500, 500)
      const z = clampInt(query.get('z'), NaN, -3e7, 3e7)
      if (![x, y, z].every(Number.isFinite)) throw new Error('x、y、z 必填')
      if (await rustBlockIsAir(x, y, z)) return { ok: true, message: `(${x},${y},${z}) 本来就是空气，不用挖。` }
      // 只有目标在头顶时才把高度交给寻路（要爬崖）；脚下/同层的块站在边上就能挖，
      // 传 y 反而会让它下坡去找「高度一致」的点，白烧时限。
      rustBackend.snapshot = await rustBackend.request('status')
      const myY = rustBackend.snapshot.position?.[1]
      await rustApproachViaTravel(x + 0.5, z + 0.5, 3.2, 20_000, Number.isFinite(myY) && y > myY + 1.2 ? y : null)
      await rustBackend.request('stop')
      const timeoutMs = clampInt(query.get('timeout'), 30_000, 5_000, 120_000)
      if (await rustDigBlock(x, y, z, timeoutMs)) {
        return { ok: true, message: `已挖掉 (${x},${y},${z}) 的方块。` }
      }
      rustBackend.snapshot = await rustBackend.request('status')
      const hp = rustBackend.snapshot.health
      if (num(hp, 20) <= 0) return { ok: true, message: `挖掘中断：我在挖的时候被打死了（生命 ${hp}），mc_respawn 后再来。` }
      return { ok: true, message: `挖了 ${timeoutMs / 1000}s 还没挖穿 (${x},${y},${z})——可能太远（要站在 3 格内）或工具不对。` }
    }
    case '/place': {
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，26.2 放置验证做不了')
      const x = clampInt(query.get('x'), NaN, -3e7, 3e7)
      const y = clampInt(query.get('y'), NaN, -500, 500)
      const z = clampInt(query.get('z'), NaN, -3e7, 3e7)
      if (![x, y, z].every(Number.isFinite)) throw new Error('x、y、z 必填')
      const itemName = normalizeItem(query.get('item'))
      if (!itemName) throw new Error('item 必填（要放的物品名）')
      // 失败细节（没支撑面/背包没物品/use_at 没生效）由 rustPlaceBlock 抛出
      await rustPlaceBlock(x, y, z, itemName)
      return { ok: true, message: `已在 (${x},${y},${z}) 放下 ${itemName}（消耗 1 个）。` }
    }
    case '/block': {
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON')
      const x = clampInt(query.get('x'), NaN, -3e7, 3e7)
      const y = clampInt(query.get('y'), NaN, -500, 500)
      const z = clampInt(query.get('z'), NaN, -3e7, 3e7)
      if (![x, y, z].every(Number.isFinite)) throw new Error('x、y、z 必填')
      const guess = cleanText(query.get('name'), 80)
      const lines = [`${guess ? `(${x},${y},${z}) 是否 ${guess}：` : `(${x},${y},${z})：`}`]
      if (guess) {
        lines.push(await rustBlockIs(x, y, z, guess.startsWith('#') ? `minecraft:${guess}` : guess) ? '✔ 是' : '✘ 不是')
      } else {
        lines.push(await rustBlockIsAir(x, y, z) ? '空气（空格）' : '非空气（有方块）')
      }
      const data = await rconCommand(`data get block ${x} ${y} ${z}`)
      if (!/not a block entity/i.test(data)) lines.push(`方块实体数据：${cleanText(data, 300)}`)
      else lines.push('（原版命令查不到普通方块的类型名；带 name= 可验证是不是某方块，支持 #tag）')
      return { ok: true, message: lines.join('\n') }
    }
    case '/entities': {
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON')
      const radius = clampInt(query.get('radius'), 24, 4, 64)
      rustBackend.snapshot = await rustBackend.request('status')
      const me = rustBackend.snapshot.position ?? []
      if (!me.every(Number.isFinite)) throw new Error('还读不到当前位置')
      const lines = [`附近 ${radius} 格实体（我 @ ${me.map((v) => v.toFixed(1)).join(',')}）：`]
      for (const name of await rustOnlinePlayers()) {
        if (name === DEFAULT_USERNAME) continue
        const p = await rustEntityPos(name)
        if (!p) continue
        const d = Math.hypot(p[0] - me[0], p[1] - me[1], p[2] - me[2])
        if (d <= radius) lines.push(`- 玩家 ${name}（${d.toFixed(1)} 格）@ ${p.map((v) => v.toFixed(1)).join(',')}`)
      }
      // 实体快照直接来自 Rust 客户端内存（毫秒级），不再逐类型 RCON 打点。
      const list = await rustNearbyEntities(radius)
      const label = {
        item: '掉落物', xp_orb: '经验球', experience_orb: '经验球', falling_block: '下落方块',
        armor_stand: '盔甲架', player: '玩家',
      }
      for (const e of list.slice(0, 30)) {
        const type = e.name || `kind#${e.kind}`
        const d = Number.isFinite(e.distance) ? `（${e.distance.toFixed(1)} 格）` : ''
        // 生物才报血；掉落物/经验球的初值没意义
        const hp = Number.isFinite(e.health) && Number.isFinite(e.max_health) && e.max_health > 0
          && !label[type] ? `，♥ ${e.health.toFixed(0)}/${e.max_health.toFixed(0)}` : ''
        lines.push(`- ${label[type] ?? type}${d}${hp} @ ${e.position.map((v) => v.toFixed(1)).join(',')}`)
      }
      if (lines.length === 1) lines.push('（没扫到玩家和常见生物）')
      return { ok: true, message: lines.join('\n') }
    }
    case '/scan': {
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON')
      const radius = clampInt(query.get('radius'), 2, 1, 2)
      rustBackend.snapshot = await rustBackend.request('status')
      const me = rustBackend.snapshot.position ?? []
      const cx = Math.floor(me[0] ?? NaN)
      const cy = Math.floor(me[1] ?? NaN)
      const cz = Math.floor(me[2] ?? NaN)
      if (![cx, cy, cz].every(Number.isFinite)) throw new Error('还读不到当前位置')
      // 原版 RCON 拿不到方块名，只能对候选清单逐格 if block——真·抽样扫描
      const candidates = [
        '#minecraft:logs', 'minecraft:chest', 'minecraft:crafting_table',
        'minecraft:furnace', 'minecraft:blast_furnace', 'minecraft:spawner',
        'minecraft:torch', 'minecraft:lava', 'minecraft:water',
        'minecraft:diamond_ore', 'minecraft:deepslate_diamond_ore',
        'minecraft:iron_ore', 'minecraft:deepslate_iron_ore',
        'minecraft:gold_ore', 'minecraft:deepslate_gold_ore',
        'minecraft:coal_ore', 'minecraft:deepslate_coal_ore',
        'minecraft:copper_ore', 'minecraft:redstone_ore', 'minecraft:lapis_ore',
        'minecraft:emerald_ore', 'minecraft:ancient_debris', 'minecraft:tnt',
        'minecraft:diamond_block', 'minecraft:gold_block', 'minecraft:iron_block',
      ]
      const hits = []
      for (let dx = -radius; dx <= radius; dx++) {
        for (let dz = -radius; dz <= radius; dz++) {
          for (let dy = -3; dy <= 3; dy++) {
            const x = cx + dx, y = cy + dy, z = cz + dz
            for (const c of candidates) {
              if (await rustBlockIs(x, y, z, c)) {
                hits.push(`${c.replace('minecraft:', '')} (${x},${y},${z})`)
                break
              }
            }
          }
        }
      }
      const lines = [`以 (${cx},${cy},${cz}) 为中心 ±${radius}×±3 格抽样扫描（只认清单里的目标方块）：`]
      if (hits.length) lines.push(hits.slice(0, 40).map((h) => `- ${h}`).join('\n'))
      else lines.push('（清单方块一个都没扫到；普通石土按设计不报）')
      return { ok: true, message: lines.join('\n') }
    }
    case '/nav': {
      // 本地体素盒：客户端内存里一次导出周围碰撞网格（RLE），供 JS 侧 A*。
      // 这是「走路一顿一顿」的根治手段——原来每拍 2~3 次 execute if block。
      const radius = clampInt(query.get('radius'), 16, 1, 32)
      const dyMin = clampInt(query.get('dy_min'), -8, -32, 32)
      const dyMax = clampInt(query.get('dy_max'), 8, -32, 32)
      const t0 = Date.now()
      const res = await rustBackend.request('nav', { radius, dy_min: dyMin, dy_max: dyMax })
      const cells = (res?.size?.[0] ?? 0) * (res?.size?.[1] ?? 0) * (res?.size?.[2] ?? 0)
      const runs = res?.runs?.length ?? 0
      return {
        ok: true,
        ...res,
        message: `导航网格 ${res?.size?.join('×')}＝${cells} 格，调色板 ${res?.palette?.length ?? 0} 种、${runs} 段 RLE`
          + `，未加载 ${res?.unloaded ?? 0} 格，${Date.now() - t0}ms`,
      }
    }
    case '/attack': {
      const name = cleanText(query.get('name'), 64)
      if (!name) throw new Error('name 必填（玩家名或生物类型，如 zombie）')
      const count = clampInt(query.get('count'), 3, 1, 20)

      // 目标解析：实体快照按类型找；玩家用 data get 的坐标找最近的 player 实体。
      let target = await rustFindTarget(name, 48)
      if (!target) throw new Error(`48 格内没找到 ${name}（mc_entities 可看清单）`)

      const lines = []
      let hitRounds = 0
      const hpStart = Number.isFinite(target.health) ? target.health : null
      const weapon = await rustEquipWeapon().catch(() => null)
      for (let i = 0; i < count && target; i++) {
        rustBackend.snapshot = await rustBackend.request('status')
        const me = rustBackend.snapshot.position ?? []
        if (dist3(me, target.position) > 3.2) {
          await rustChase(target.position[0], target.position[2], 2.2, 10_000, target.position[1])
          await rustBackend.request('stop')
          rustBackend.snapshot = await rustBackend.request('status')
          const d = dist3(rustBackend.snapshot.position ?? [], target.position)
          if (d > 3.6) {
            lines.push(`追不上 ${name}：还差 ${d.toFixed(1)} 格（在 ${target.position.map((v) => v.toFixed(1)).join(',')}），这一刀没挥。`)
            break
          }
        }
        // 直发攻击：不需要把视角转到目标身上
        await rustAttackEntity(target.id)
        hitRounds += 1
        await sleep(650) // 攻击冷却 0.6s
        const latest = await rustNearbyEntities(48)
        target = latest.find((e) => e.id === target.id) ?? null
      }
      if (target) {
        const hp = Number.isFinite(target.health) && target.max_health > 0
          ? `，♥ ${target.health.toFixed(0)}/${target.max_health.toFixed(0)}` : ''
        const dmg = hpStart != null && Number.isFinite(target.health) ? Math.max(0, hpStart - target.health) : 0
        const dealt = dmg > 0 ? `，总共掉了 ${dmg.toFixed(0)} 血` : ''
        lines.push(`对 ${name} 挥了 ${hitRounds} 轮刀（手持 ${weapon ?? '空手'}），目标还在（@ ${target.position.map((v) => v.toFixed(1)).join(',')}${hp}${dealt}）。`)
      } else {
        lines.push(`对 ${name} 挥了 ${hitRounds} 轮刀，目标没了——击杀或它跑/消失了。`)
      }
      rustBackend.snapshot = await rustBackend.request('status')
      lines.push(`我的生命 ${rustBackend.snapshot.health ?? '?'}/20。`)
      return { ok: true, message: lines.join('\n') }
    }
    case '/follow': {
      const name = cleanText(query.get('name'), 64)
      if (!name) throw new Error('name 必填（玩家名或生物类型）')
      const wantDist = clampInt(query.get('distance'), 3, 2, 16)
      const minutes = clampInt(query.get('minutes'), 3, 1, 10)
      rustLongTask.cancel = false
      rustLongTask.name = name
      const deadline = Date.now() + minutes * 60_000
      let seen = 0
      let lost = 0
      while (!rustLongTask.cancel && Date.now() < deadline) {
        rustBackend.snapshot = await rustBackend.request('status')
        const me = rustBackend.snapshot.position ?? []
        // 实体快照直读（毫秒级），玩家名靠 RCON 坐标匹配
        const target = await rustFindTarget(name, 48).catch(() => null)
        if (!target) {
          lost += 1
          if (lost >= 4) break
          await sleep(1_500)
          continue
        }
        lost = 0
        seen += 1
        const p = target.position
        const d = dist3(me, p)
        if (d > wantDist) {
          // A* 追人（禁挖，绕现有地形），而不是直线怼墙
          await rustApproachViaTravel(p[0], p[2], Math.max(2, wantDist - 1), 8_000, p[1])
        } else {
          await rustBackend.request('stop')
          await rustLookAtPoint(p[0], p[1] + 1.6, p[2])
        }
        await sleep(800)
      }
      await rustBackend.request('stop')
      const why = rustLongTask.cancel ? '被叫停' : Date.now() >= deadline ? '时间到' : '目标跟丢了'
      return { ok: true, message: `跟随 ${name}（保持 ${wantDist} 格）结束：${why}，共跟了 ${seen} 拍。` }
    }
    case '/collect': {
      const timeoutMs = clampInt(query.get('timeout'), 60_000, 10_000, 180_000)
      rustLongTask.cancel = false
      const deadline = Date.now() + timeoutMs
      const picked = []
      while (!rustLongTask.cancel && Date.now() < deadline) {
        rustBackend.snapshot = await rustBackend.request('status')
        const me = rustBackend.snapshot.position ?? []
        // 实体快照直接可读（毫秒级），不再 RCON 逐类型盲扫
        const items = (await rustNearbyEntities(24)).filter((e) => e.name === 'item')
        if (!items.length) break
        const item = items[0]
        if (dist3(me, item.position) > 1.4) {
          // 直接踩到掉落物那一格（掉落物不是实体碰撞，能站进去）；崖上/坑里时
          // 外圈候选点会落在崖下同一水平面，travel 以为到了但还差一格高，捡不到。
          const bridge = await rustBridgeItem().catch(() => null)
          const direct = await rustBehavior.travel({
            x: item.position[0], z: item.position[2], y: item.position[1],
            timeoutMs: Math.min(15_000, Math.max(3_000, deadline - Date.now())), dig: true, bridge,
          }).catch(() => null)
          if (!travelArrived(direct)) {
            await rustApproachViaTravel(item.position[0], item.position[2], 1.2, Math.max(3_000, deadline - Date.now()), item.position[1])
          }
          await rustBackend.request('stop')
        }
        // 走到跟前原地等 1s，让服务器判拾取
        await sleep(1_000)
        const rest = (await rustNearbyEntities(8)).filter((e) => e.name === 'item')
        if (!rest.some((e) => e.id === item.id)) {
          picked.push(item.position.map((v) => v.toFixed(0)).join(','))
        }
      }
      await rustBackend.request('stop')
      return { ok: true, message: picked.length
        ? `捡拾了 ${picked.length} 处掉落物（${picked.join(' / ')}）。`
        : '没捡到掉落物（可能已经没了或走不过去）。' }
    }
    case '/eat': {
      const message = await rustEatFood()
      return { ok: true, message }
    }
    // ---- 行为层 ----
    case '/travel': {
      // 任务行为：走到 (x,z)，内部自动完成寻路/跳顶/挖洞/搭路；规范条件经参数传入
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，travel 的挖/探做不了')
      const x = num(query.get('x'), NaN)
      const z = num(query.get('z'), NaN)
      if (![x, z].every(Number.isFinite)) throw new Error('x、z 必填')
      const y = num(query.get('y'), NaN)
      const digRaw = cleanText(query.get('dig'), 200)
      const dig = digRaw === '' || /^(1|true|yes|on)$/i.test(digRaw) ? true
        : /^(0|false|no|off)$/i.test(digRaw) ? false
        : digRaw.split(',').map((s) => normalizeItem(s)).filter(Boolean)
      const bridgeRaw = cleanText(query.get('bridge'), 64)
      const bridge = !bridgeRaw || /^(0|false|no|off)$/i.test(bridgeRaw) ? null
        : /^(1|true|yes|on)$/i.test(bridgeRaw) ? true : bridgeRaw
      const tool = cleanText(query.get('tool'), 64) || null
      const timeoutMs = clampInt(query.get('timeout'), 120, 10, 300) * 1000  // 插件契约是秒
      const message = await rustBehavior.travel({
        x, z, y: Number.isFinite(y) ? y : null, timeoutMs, dig, bridge, tool,
      })
      return { ok: true, message }
    }
    case '/behavior': {
      // 常驻行为开关与阈值；无参 = 查状态
      const name = cleanText(query.get('name'), 32)
      if (name) rustBehavior.setToggle(name, bool(query.get('on'), true))
      for (const key of ['radius', 'eat_below', 'guard_below']) {
        const v = query.get(key)
        if (v !== null && v !== '') rustBehavior.setThreshold(key, clampInt(v, 6, 1, 64))
      }
      return { ok: true, message: rustBehavior.describe() }
    }
    case '/follow_player': {
      // 行为层跟随：寻路跟玩家（挖墙/搭桥照规范），不是旧 /follow 的直线莽
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，follow_player 的定位做不了')
      const name = cleanText(query.get('name'), 64)
      if (!name) throw new Error('name 必填（玩家名）')
      const distance = clampInt(query.get('distance'), 3, 2, 16)
      const digRaw = cleanText(query.get('dig'), 200)
      const dig = digRaw === '' || /^(1|true|yes|on)$/i.test(digRaw) ? true
        : /^(0|false|no|off)$/i.test(digRaw) ? false
        : digRaw.split(',').map((s) => normalizeItem(s)).filter(Boolean)
      const bridgeRaw = cleanText(query.get('bridge'), 64)
      const bridge = !bridgeRaw || /^(0|false|no|off)$/i.test(bridgeRaw) ? null
        : /^(1|true|yes|on)$/i.test(bridgeRaw) ? true : bridgeRaw
      const tool = cleanText(query.get('tool'), 64) || null
      const timeoutMs = clampInt(query.get('timeout'), 180, 10, 600) * 1000  // 插件契约是秒
      const message = await rustBehavior.followPlayer({ name, distance, timeoutMs, dig, bridge, tool })
      return { ok: true, message }
    }
    case '/mine': {
      // 行为层定向采集：环带外扩扫目标方块，travel 带路逐块挖，可指定工具/数量/范围/超时，back=true 返程
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，mine 的探/验做不了')
      const block = normalizeItem(query.get('block'))
      if (!block) throw new Error('block 必填（要挖的方块 id，如 oak_log / dirt / coal_ore）')
      const count = clampInt(query.get('count'), 1, 1, 64)
      const range = clampInt(query.get('range'), 12, 2, 48)
      const tool = cleanText(query.get('tool'), 64) || null
      const digRaw = cleanText(query.get('dig'), 200)
      const dig = digRaw === '' || /^(1|true|yes|on)$/i.test(digRaw) ? true
        : /^(0|false|no|off)$/i.test(digRaw) ? false
        : digRaw.split(',').map((s) => normalizeItem(s)).filter(Boolean)
      const bridgeRaw = cleanText(query.get('bridge'), 64)
      const bridge = !bridgeRaw || /^(0|false|no|off)$/i.test(bridgeRaw) ? null
        : /^(1|true|yes|on)$/i.test(bridgeRaw) ? true : bridgeRaw
      const back = bool(query.get('back'), false)
      const timeoutMs = clampInt(query.get('timeout'), 180, 10, 600) * 1000  // 插件契约是秒
      const message = await rustBehavior.mine({ block, count, tool, range, timeoutMs, dig, bridge, back })
      return { ok: true, message }
    }
    case '/find': {
      // 行为层勘察：mine 同款分层螺旋扫描但只报坐标不动手——「附近有没有 X」走这里，
      // 别用 /scan（半径钳在 ±2 只是贴脸探针，找树永远报空——实机 11-05 踩过）
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，find 的探做不了')
      const block = cleanText(query.get('block'), 200)
      if (!block) throw new Error('block 必填（要找的方块 id，如 oak_log / coal_ore；找任意树用 log）')
      const range = clampInt(query.get('range'), 12, 2, 48)
      const limit = clampInt(query.get('limit'), 10, 1, 40)
      const timeoutMs = clampInt(query.get('timeout'), 60, 10, 300) * 1000  // 插件契约是秒
      const message = await rustBehavior.find({ block, range, limit, timeoutMs })
      return { ok: true, message }
    }
    case '/presets': {
      // 查询可用建筑预设及其材料需求：为 AI 决策提供选项
      // 格式：{ preset_name: { description, material_options: [...], size: {w,h,d}, cost: {...} } }
      const presets = {
        house: {
          description: '小屋：5×5 地板 + 四面墙（留前门） + 屋顶',
          material_options: ['oak_log', 'stone', 'cobblestone', 'oak_planks', 'dirt'],
          size: { w: 5, h: 4, d: 5 },
          cost: { blocks: 100 },  // 近似方块数
        },
        tower: {
          description: '塔：3×3 柱子高 12 格',
          material_options: ['stone', 'cobblestone', 'oak_log', 'oak_planks', 'dirt'],
          size: { w: 3, h: 12, d: 3 },
          cost: { blocks: 84 },
        },
        platform: {
          description: '平台：7×7 单层地板',
          material_options: ['stone', 'cobblestone', 'oak_planks', 'dirt', 'oak_log'],
          size: { w: 7, h: 1, d: 7 },
          cost: { blocks: 49 },
        },
        pillar: {
          description: '柱子：1×1 高 8 格（常用于过河/爬升）',
          material_options: ['dirt', 'oak_log', 'stone', 'cobblestone'],
          size: { w: 1, h: 8, d: 1 },
          cost: { blocks: 8 },
        },
        wall: {
          description: '墙：9×1×5 面墙（留两个窗洞）',
          material_options: ['stone', 'oak_log', 'cobblestone'],
          size: { w: 9, h: 5, d: 1 },
          cost: { blocks: 43 },
        },
        arch: {
          description: '拱门：两根柱 + 一道横梁（装饰/通道）',
          material_options: ['stone', 'oak_log', 'cobblestone'],
          size: { w: 5, h: 4, d: 1 },
          cost: { blocks: 10 },
        },
      }
      const lines = Object.entries(presets).map(([name, info]) =>
        `• ${name}: ${info.description}（${info.size.w}×${info.size.h}×${info.size.d}，需约${info.cost.blocks}块）`
      )
      return { ok: true, presets, message: `可用预设（用 /build shape=<name> 执行）：\n${lines.join('\n')}` }
    }
    case '/plan': {
      // AI 决策规划端点：输入高层目标（"Build a small house"），返回完整规划方案
      // 包括选定的建筑、材料、采集清单、预计时间等
      const goal = cleanText(query.get('goal'), 200)
      if (!goal) throw new Error('goal 必填（想要做什么，例如：Build a small house at x=100,z=200）')

      // 简单规则：基于 goal 关键词推荐预设和材料
      // 实际上这里应该调 LLM，但目前只能用规则
      let presetName = 'platform'
      let material = 'stone'
      let reason = ''

      if (/house|小屋|房|建筑/.test(goal)) {
        presetName = 'house'
        material = 'oak_log'  // 优先用木头，景观好
        reason = '目标是建房，选小屋预设，用 oak_log 材料'
      } else if (/tower|塔|柱|高/.test(goal)) {
        presetName = 'tower'
        material = 'stone'
        reason = '目标是建塔，选塔预设，用 stone'
      } else if (/bridge|桥|跨/.test(goal)) {
        presetName = 'platform'
        material = 'dirt'
        reason = '目标是建桥/平台，选平台预设，用 dirt'
      }

      // 查背包看现有材料
      rustBackend.snapshot = await rustBackend.request('status')
      const inv = rustBackend.snapshot.inventory ?? []
      const have = {}
      for (const entry of inv) {
        const name = invName(entry)
        have[name] = (have[name] ?? 0) + (entry.count ?? 1)
      }

      const presets = {
        house: { blocks: 100 },
        tower: { blocks: 84 },
        platform: { blocks: 49 },
        pillar: { blocks: 8 },
        wall: { blocks: 43 },
        arch: { blocks: 10 },
      }

      const needed = presets[presetName]?.blocks ?? 50
      const haveCount = have[material] ?? 0
      const needMine = Math.max(0, needed - haveCount)

      const plan = {
        preset: presetName,
        material,
        total_blocks: needed,
        have: haveCount,
        need_to_mine: needMine,
        reason,
        next_step: needMine > 0
          ? `采集 ${needMine} 个 ${material}（当前有 ${haveCount} 个）`
          : `背包已足够，可直接建筑`,
      }

      return { ok: true, plan, message: `规划完成：${reason}\n• 预设：${presetName}\n• 材料：${material}\n• 总需：${needed} 块\n• 现有：${haveCount} 块\n• 待采：${needMine} 块\n• 下一步：${plan.next_step}` }
    }
    // ---- 通用任务调度（多行为并行/优先级仲裁）----
    case '/schedule/create':
    case '/schedule/tasks':
    case '/schedule/cancel':
    case '/schedule/status':
      return await handleSchedule(path, query)
    case '/build': {
      // 3D 打印建筑：按蓝图逐块摆放（先地板后墙再顶），已有方块跳过，缺材料如实报
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，build 的放置做不了')
      const blueprint = cleanText(query.get('blueprint'), 32) || null
      const blocks = cleanText(query.get('blocks'), 4000) || null
      const shape = cleanText(query.get('shape'), 32) || null
      if (!blueprint && !blocks && !shape) throw new Error('build 必填 blueprint / blocks / shape 之一')
      const material = cleanText(query.get('material'), 64) || 'stone'
      const w = clampInt(query.get('w'), 5, 1, 32)
      const h = clampInt(query.get('h'), 4, 1, 32)
      const d = clampInt(query.get('d'), 5, 1, 32)
      const radius = clampInt(query.get('radius'), 24, 2, 48)
      let at = null
      const atRaw = cleanText(query.get('at'), 64)
      if (atRaw) {
        // 插件契约 "x,z"（高度按走到后脚下算）或 "x,y,z"
        const parts = atRaw.split(',').map((v) => Number(v.trim()))
        if (parts.length >= 2 && parts.slice(0, 3).every(Number.isFinite)) {
          at = parts.length >= 3 ? [parts[0], parts[1], parts[2]] : [parts[0], null, parts[1]]
        }
      }
      const timeoutMs = clampInt(query.get('timeout'), 300, 10, 900) * 1000
      const message = await rustBehavior.build({ blueprint, blocks, shape, material, w, h, d, at, radius, timeoutMs })
      return { ok: true, message }
    }
    case '/survey': {
      // 体素网格勘察：一次取盒扫全场，比 find 的 RCON 逐格快一个量级
      if (!rconReady()) throw new Error('mc-vanilla 未开 RCON，survey 的探做不了')
      const block = cleanText(query.get('block'), 200)
      if (!block) throw new Error('block 必填（要找的方块 id，如 oak_log / coal_ore）')
      const radius = clampInt(query.get('radius'), 16, 2, 32)
      const limit = clampInt(query.get('limit'), 20, 1, 100)
      const timeoutMs = clampInt(query.get('timeout'), 60, 10, 300) * 1000
      const message = await rustBehavior.survey({ block, radius, limit, timeoutMs })
      return { ok: true, message }
    }
    case '/craft': {
      const result = normalizeItem(query.get('result'))
      const times = clampInt(query.get('count'), 1, 1, 16)
      if (!result) throw new Error('result 必填（要合成的物品名）')
      return await rustCraft(result, times)
    }
    default:
      throw new Error(`26.2 Rust 后端暂不支持 ${path}；已支持聊天、命令、视角、寻路（A* 体素网格 goto/nav）、网格勘察 survey、3D 打印建筑 build、挖掘、放置、方块/实体查询、抽样扫描、攻击、跟随、捡拾、吃东西、背包操作（equip/inv_click/inv_drop/swap_hands）、容器（container/container_take/container_put/container_close）、服务端辅助合成、停止和重生。旧版服务器的完整能力仍走 Mineflayer。`)
  }
}

// ---------------------------------------------------------------------------
// HTTP handlers
// ---------------------------------------------------------------------------

function statusText() {
  const bot = state.bot
  if (!bot || !state.joined) {
    const conn = state.info
    if (state.connecting && conn) {
      return `正在连接 ${conn.host}:${conn.port}（${conn.username}）……`
    }
    const why = state.lastError ? `\n上次错误：${state.lastError}` : ''
    return `未连接。用 mc_join 进服（host 必填，port 默认 25565，离线服 auth=offline）。${why}`
  }
  const pos = bot.entity.position
  const timeOfDay = bot.time?.timeOfDay ?? 0
  const phase = timeOfDay < 12000 ? '白天' : timeOfDay < 13800 ? '黄昏' : timeOfDay < 22200 ? '夜晚' : '黎明'
  const players = Object.keys(bot.players ?? {})
  const nearby = Object.values(bot.entities).filter((e) => e !== bot.entity && e.position.distanceTo(pos) <= 16)
  const nearbyText = nearby
    .slice(0, 12)
    .map((e) => (e.type === 'player' ? e.username : e.name))
    .filter(Boolean)
  const inv = inventorySummary(bot)
  const held = bot.heldItem ? bot.heldItem.name : '空手'
  const info = state.info ?? {}
  const lines = [
    `在线：${bot.username} @ ${info.host}:${info.port}（${bot.version}，${info.auth === 'microsoft' ? '微软正版' : '离线'}）`,
    `位置：x=${pos.x.toFixed(1)} y=${pos.y.toFixed(1)} z=${pos.z.toFixed(1)}｜维度 ${bot.game?.dimension ?? '?'}`,
    `生命 ${bot.health.toFixed(0)}/20｜饥饿 ${bot.food.toFixed(0)}/20｜时间 ${timeOfDay}（${phase}）${bot.isRaining ? '｜下雨' : ''}`,
    `玩家在线（${players.length}）：${players.join(', ')}`,
    authFlow.statusLine(),
    `附近 16 格实体（${nearby.length}）：${nearbyText.length ? nearbyText.join(', ') : '无'}`,
    `手持：${held}｜背包 ${inv.slots} 格 / ${inv.total} 件`,
    inv.text ? `背包内容：${inv.text}` : '',
    state.task ? `当前动作：${state.task}` : '',
    state.follow ? `跟随中：${state.follow}` : '',
  ].filter(Boolean)
  return lines.join('\n')
}

function eventsText(since, limit) {
  let events
  if (since === undefined || since === null || since === '') {
    events = state.events.slice(-limit)
  } else {
    const from = clampInt(since, 0, 0, Number.MAX_SAFE_INTEGER)
    events = state.events.filter((e) => e.seq > from).slice(-limit)
  }
  if (events.length === 0) return `（没有新事件）\nnext=${state.seq}`
  const lines = events.map((e) => {
    const time = e.at.slice(11, 19)
    return `[${time}] ${e.type}: ${e.text}`
  })
  return `${lines.join('\n')}\nnext=${state.seq}`
}

function inventoryText(bot) {
  const inv = inventorySummary(bot)
  const lines = []
  lines.push(`手持：${bot.heldItem ? itemLabel(bot.heldItem) : '空手'}`)
  const armor = ['head', 'torso', 'legs', 'feet']
    .map((slot) => {
      const item = bot.inventory.slots[bot.getEquipmentDestSlot?.(slot) ?? -1]
      return item ? `${slot}=${item.name}` : null
    })
    .filter(Boolean)
  if (armor.length) lines.push(`装备：${armor.join(', ')}`)
  lines.push(`背包（${inv.slots} 格 / ${inv.total} 件）：`)
  const items = bot.inventory.items().sort((a, b) => a.name.localeCompare(b.name))
  for (const item of items) lines.push(`- ${item.name} ×${item.count}（槽 ${item.slot}）`)
  if (items.length === 0) lines.push('（空）')
  return lines.join('\n')
}

function entitiesText(bot, radius) {
  const pos = bot.entity.position
  const entities = Object.values(bot.entities)
    .filter((e) => e !== bot.entity && e.position.distanceTo(pos) <= radius)
    .sort((a, b) => a.position.distanceTo(pos) - b.position.distanceTo(pos))
    .slice(0, 30)
  if (entities.length === 0) return `附近 ${radius} 格内没有实体。`
  return `附近 ${radius} 格内 ${entities.length} 个实体：\n` + entities.map((e) => `- ${entityText(e, bot)}`).join('\n')
}

function scanText(bot, radius) {
  const center = bot.entity.position.floored()
  const counts = new Map()
  const interesting = []
  const interestingNames = /ore|log|water|lava|chest|crafting_table|furnace|bed|torch|spawner|portal/i
  for (let dx = -radius; dx <= radius; dx++) {
    for (let dz = -radius; dz <= radius; dz++) {
      for (let dy = -4; dy <= 5; dy++) {
        const block = bot.blockAt(center.offset(dx, dy, dz))
        if (!block || block.name === 'air' || block.name === 'cave_air') continue
        counts.set(block.name, (counts.get(block.name) ?? 0) + 1)
        if (interestingNames.test(block.name) && interesting.length < 25) {
          interesting.push(`${block.name} (${block.position.x},${block.position.y},${block.position.z})`)
        }
      }
    }
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20)
  const lines = [`以 ${fmtPos(bot.entity.position)} 为中心，±${radius} 格内方块统计：`]
  lines.push(top.map(([name, count]) => `${name}×${count}`).join(', ') || '（全是空气）')
  if (interesting.length) lines.push(`值得注意：\n- ${interesting.join('\n- ')}`)
  return lines.join('\n')
}

async function handle(path, query) {
  // 调度端点与后端解耦：未进服时也给出明确提示（而不是「未知接口」）
  if (path.startsWith('/schedule/')) return await handleSchedule(path, query)
  if (rustBackend.active && !['/health', '/ping', '/join'].includes(path)) {
    // 占用身体的旧式长动作：行为层期间只感知不动作，并打断进行中的 travel（新指令优先）
    if (RUST_BODY_PATHS.has(path)) {
      rustBehavior.holdBegin()
      try {
        return await handleRust(path, query)
      } finally {
        rustBehavior.holdEnd()
      }
    }
    return await handleRust(path, query)
  }
  switch (path) {
    case '/health':
      return { ok: true, message: 'ok' }

    case '/ping': {
      const { host, port } = parseTarget(query.get('host'), query.get('port'))
      if (!host) throw new Error('host 必填（支持 host 或 host:port）')
      const version = cleanText(query.get('version'), 32)
      const data = await withTimeout(
        minecraftProtocol.ping({ host, port, ...(version && version !== 'auto' ? { version } : {}) }),
        10_000,
        '服务器状态查询',
      )
      const desc = typeof data.description === 'string'
        ? data.description
        : (data.description?.text ?? JSON.stringify(data.description ?? ''))
      const players = data.players ?? {}
      return {
        ok: true,
        message: `${host}:${port} 在线｜版本 ${data.version?.name ?? '?'}（协议 ${data.version?.protocol ?? '?'}）`
          + `｜玩家 ${players.online ?? 0}/${players.max ?? 0}｜延迟 ${data.latency ?? '?'}ms\n`
          + `MOTD: ${String(desc).replace(/§./g, '').slice(0, 300)}`,
      }
    }

    case '/join': {
      if (state.bot || rustBackend.active) throw new Error('已经连接了服务器，先 mc_leave 再进新服。')
      const { host, port } = parseTarget(query.get('host'), query.get('port'))
      if (!host) throw new Error('host 必填且不能含空格（支持 host 或 host:port）')
      const profile = readProfile(host, port)
      const username = cleanText(query.get('username'), 16) || profile?.username || DEFAULT_USERNAME
      if (!/^[A-Za-z0-9_]{3,16}$/.test(username)) {
        throw new Error('username 只能是 3-16 位字母/数字/下划线（Minecraft 用户名限制）')
      }
      const versionRaw = cleanText(query.get('version'), 32)
      const version = versionRaw && versionRaw !== 'auto' ? versionRaw : ''
      const auth = (cleanText(query.get('auth'), 16) || profile?.auth || 'offline').toLowerCase()
      if (!['offline', 'microsoft'].includes(auth)) throw new Error('auth 只能是 offline 或 microsoft')
      const password = cleanText(query.get('password'), 64)
        || cleanText(query.get('register'), 64)
        || profile?.password
        || ''
      if (/\s/.test(password)) throw new Error('password 不能含空格（AuthMe 等登录插件通常不允许）')
      const backendRaw = cleanText(query.get('backend'), 16).toLowerCase()
      if (backendRaw && !['auto', 'rust', 'mineflayer'].includes(backendRaw)) {
        throw new Error('backend 只能是 auto / rust / mineflayer')
      }
      let info = {
        host,
        port,
        username,
        version,
        auth,
        password,
        register: password,
        autoReconnect: bool(query.get('auto_reconnect'), profile?.autoReconnect ?? true),
      }
      state.reconnectAttempts = 0
      state.kickTerminal = false
      state.manualLeave = false

      // 部署侧适配可能改 username/password/version/backend（如一次性进服码）。
      const preHookInfo = { ...info }
      info = await applyJoinHook(info, 'join')
      const effectiveBackend = ['auto', 'rust', 'mineflayer'].includes(info.backend)
        ? info.backend
        : backendRaw

      // 选后端：显式指定 > 26.2 版本字符串 > SLP 探测。探测失败不能挡进服——
      // 不少服关了/改了状态查询，但正常登录协议是通的，退回 Mineflayer 自动版本即可。
      let useRust = info.version === '26.2'
      let probe = null
      let probeNote = ''
      if (effectiveBackend === 'rust') {
        useRust = true
      } else if (effectiveBackend === 'mineflayer') {
        useRust = false
      } else if (!info.version) {
        try {
          probe = await withTimeout(minecraftProtocol.ping({ host, port }), 8_000, '服务器版本探测')
          useRust = shouldUseRust('', probe.version?.protocol)
        } catch (error) {
          probeNote = `服务器状态查询失败（${error.message}），已改用 Mineflayer 自动版本尝试连接。`
          useRust = false
        }
      }
      info.backend = useRust ? 'rust' : 'mineflayer'
      if (useRust && info.auth === 'microsoft') {
        if (effectiveBackend === 'rust') throw new Error('26.2 Rust 后端正版会话尚未接入；目前只支持 offline 离线服')
        info.backend = 'mineflayer'
      }
      // 记录 hook 之前的稳定身份（用户名/密码），重连时用它做底再重新取码。
      state.preHookInfo = {
        ...preHookInfo,
        version: info.version,
        auth: info.auth,
        backend: info.backend,
      }
      // hook 给的身份（一次性码/动态令牌）只用于本次连接，不落盘；
      // 档案继续记用户在 query/profile 里提供的稳定用户名与密码。
      writeProfile(host, port, {
        username,
        auth: info.auth,
        autoReconnect: info.autoReconnect,
        lastBackend: info.backend,
        ...(password ? { password } : {}),
      })
      const flavor = probe
        ? `${probe.version?.name ?? '?'}（协议 ${probe.version?.protocol ?? '?'}）`
        : (info.version || '自动版本')

      if (info.backend === 'rust') {
        authFlow.attach(info, { password: info.password, register: info.register })
        state.info = info
        try {
          await rustBackend.start(info)
        } catch (error) {
          state.manualLeave = true
          state.info = null
          state.preHookInfo = null
          throw error
        }
        setTimeout(() => { if (rustBackend.active) authFlow.fallbackLogin() }, 2500).unref?.()
        await settleAuth(4000)
        const authNote = authJoinNote()
        writeProfile(host, port, { lastJoinedAt: Date.now() })
        return {
          ok: true,
          message: `已进入 ${host}:${port}（${info.username}，${flavor}，Rust 后端${authNote}）`
            + `${probeNote ? `\n${probeNote}` : ''}\n${rustStatusText()}`,
        }
      }

      const bot = connect(info)
      try {
        await waitForSpawn(bot, info.auth === 'microsoft' ? 180_000 : 20_000)
        await settleAuth(4000)
      } catch (error) {
        state.manualLeave = true
        state.info = null
        state.preHookInfo = null
        try {
          bot.quit('join failed')
        } catch {}
        throw error
      }
      writeProfile(host, port, { lastJoinedAt: Date.now() })
      const authNote = authJoinNote()
      return {
        ok: true,
        message: `已进入 ${host}:${port}（${info.username}，版本 ${flavor}，Mineflayer 后端${authNote}）`
          + `${probeNote ? `\n${probeNote}` : ''}\n${statusText()}`,
      }
    }

    case '/leave': {
      if (!state.bot) return { ok: true, message: '本来就没连接。' }
      state.manualLeave = true
      if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer)
        state.reconnectTimer = null
      }
      const bot = state.bot
      try {
        bot.quit('主人让我下线')
      } catch {}
      state.bot = null
      state.joined = false
      state.connecting = false
      state.reconnectingAttempt = false
      state.info = null
      state.preHookInfo = null
      state.follow = null
      state.task = ''
      state.kickTerminal = false
      authFlow.reset()
      addEvent('system', '已主动离开服务器')
      return { ok: true, message: '已离开服务器。' }
    }

    case '/status':
      return { ok: true, message: statusText() }

    case '/events':
      return {
        ok: true,
        message: eventsText(query.get('since'), clampInt(query.get('limit'), 25, 1, 100)),
        next: state.seq,
      }

    case '/chat': {
      const bot = requireBot()
      const message = cleanText(query.get('message'), 240)
      if (!message) throw new Error('message 必填')
      bot.chat(message)
      addEvent('system', `我发送：${message}`)
      return { ok: true, message: `已发送：${message}` }
    }

    case '/command': {
      const bot = requireBot()
      let command = cleanText(query.get('command'), 200)
      if (!command.startsWith('/')) command = '/' + command
      bot.chat(command)
      addEvent('system', `我执行命令：${command}`)
      return { ok: true, message: `已执行：${command}（需要 op 权限的命令只有 op 后可用）` }
    }

    case '/goto': {
      return await action('goto', async () => {
        const bot = requireBot()
        const x = num(query.get('x'), NaN)
        const z = num(query.get('z'), NaN)
        if (!Number.isFinite(x) || !Number.isFinite(z)) throw new Error('x、z 必填')
        const yRaw = query.get('y')
        const y = yRaw === null || yRaw === '' ? null : num(yRaw, NaN)
        if (y !== null && !Number.isFinite(y)) throw new Error('y 不是有效数字')
        const range = clampInt(query.get('range'), 1, 0, 8)
        const timeoutMs = clampInt(query.get('timeout'), 45, 3, 180) * 1000
        const goal = y === null
          ? new goals.GoalXZ(Math.round(x), Math.round(z))
          : new goals.GoalNear(Math.round(x), Math.round(y), Math.round(z), range)
        state.task = '寻路'
        try {
          await withTimeout(bot.pathfinder.goto(goal), timeoutMs, '寻路')
        } catch (error) {
          try {
            bot.pathfinder.setGoal(null)
            bot.clearControlStates()
          } catch {}
          // 常见死局：掉进自己挖的 1×1 坑，寻路器不跳，直接超时。
          const escaped = await attemptUnstuck(bot)
          if (!escaped) throw error
          await withTimeout(bot.pathfinder.goto(goal), 15_000, '脱困后寻路')
        }
        const pos = bot.entity.position
        const remain = Math.hypot(pos.x - x, pos.z - z)
        return {
          ok: true,
          message: `寻路结束，位置 ${fmtPos(pos)}，距目标水平 ${remain.toFixed(1)} 格。`
            + (remain > range + 2 ? '（没完全走到，可能被地形挡住）' : ''),
        }
      })
    }

    case '/dig': {
      return await action('dig', async () => {
        const bot = requireBot()
        const x = clampInt(query.get('x'), NaN, -30000000, 30000000)
        const y = clampInt(query.get('y'), NaN, -64, 320)
        const z = clampInt(query.get('z'), NaN, -30000000, 30000000)
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) throw new Error('x、y、z 必填')
        let block = bot.blockAt(vec3(x, y, z))
        if (!block || block.name === 'air' || block.name === 'cave_air') throw new Error(`(${x},${y},${z}) 是空气`)
        if (block.diggable === false) throw new Error(`${block.name} 挖不动`)
        if (bool(query.get('approach'), true) && bot.entity.position.distanceTo(block.position) > 3.5) {
          await approach(bot, block.position, 2, 25_000)
          block = bot.blockAt(vec3(x, y, z))
          if (!block || block.name === 'air') return { ok: true, message: '走近时方块已经没了。' }
        }
        try {
          const tool = bot.pathfinder.bestHarvestTool(block)
          if (tool && bot.heldItem?.name !== tool.name) await bot.equip(tool, 'hand')
        } catch {}
        const name = block.name
        await withTimeout(bot.dig(block, true), 30_000, '挖掘')
        await new Promise((r) => setTimeout(r, 500))
        let note = ''
        if (stuckInPit(bot)) {
          note = (await attemptUnstuck(bot)) ? '（挖穿脚下掉进坑里，已自己跳出来）' : '（掉进坑里了，可以用 mc_unstuck 脱困）'
        }
        const pos = bot.entity.position
        return { ok: true, message: `挖掉了 ${name}（${x},${y},${z}）。当前位置 ${fmtPos(pos)}。${note}` }
      })
    }

    case '/place': {
      return await action('place', async () => {
        const bot = requireBot()
        const x = clampInt(query.get('x'), NaN, -30000000, 30000000)
        const y = clampInt(query.get('y'), NaN, -64, 320)
        const z = clampInt(query.get('z'), NaN, -30000000, 30000000)
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) throw new Error('x、y、z 必填')
        const target = vec3(x, y, z)
        if (bot.entity.position.floored().equals(target)) {
          throw new Error('目标位置就是你脚下的位置，先走开一格再放')
        }
        const current = bot.blockAt(target)
        if (!current) throw new Error('目标位置读不到方块')
        const replaceable = current.replaceable === true
          || ['air', 'cave_air', 'water', 'short_grass', 'tall_grass', 'fern', 'dead_bush', 'snow', 'vine'].includes(current.name)
        if (!replaceable) throw new Error(`目标位置被 ${current.name} 占着，放不了`)

        let itemName = cleanText(query.get('item'), 64)
        if (itemName) itemName = normalizeItem(itemName)
        let item = itemName ? bot.inventory.items().find((i) => i.name === itemName) : bot.heldItem
        if (!item) {
          const blockItems = bot.inventory.items().filter((i) => bot.registry.itemsByName[i.name]?.blockId)
          item = blockItems[0]
        }
        if (!item) throw new Error(itemName ? `背包里没有 ${itemName}` : '背包里没有可放置的方块')
        if (bot.heldItem?.name !== item.name) await bot.equip(item, 'hand')

        const offsets = [
          [0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1],
        ]
        let ref = null
        let face = null
        for (const [dx, dy, dz] of offsets) {
          const neighbour = bot.blockAt(target.offset(dx, dy, dz))
          if (neighbour && neighbour.boundingBox === 'block') {
            ref = neighbour
            face = vec3(-dx, -dy, -dz)
            break
          }
        }
        if (!ref) throw new Error('目标位置周围没有可依附的实体方块')
        if (bot.entity.position.distanceTo(target) > 4) await approach(bot, target, 2, 25_000)
        await withTimeout(bot.placeBlock(ref, face), 15_000, '放置')
        await new Promise((r) => setTimeout(r, 250))
        const after = bot.blockAt(target)
        return { ok: true, message: `放了 ${after?.name ?? item.name}（${x},${y},${z}）。` }
      })
    }

    case '/attack': {
      return await action('attack', async () => {
        const bot = requireBot()
        const name = cleanText(query.get('name'), 64)
        const times = clampInt(query.get('count'), 1, 1, 20)
        const target = nearestTarget(bot, name)
        if (!target) throw new Error(name ? `附近没有叫 ${name} 的实体` : '附近没有可攻击的目标')
        if (bot.entity.position.distanceTo(target.position) > 3.5) {
          await approach(bot, target.position, 2, 20_000)
        }
        let hits = 0
        for (let i = 0; i < times; i++) {
          if (!target.isValid || target.health <= 0) break
          try {
            await bot.lookAt(target.position.offset(0, target.height ? target.height * 0.5 : 0.5, 0), true)
          } catch {}
          bot.attack(target, true)
          hits += 1
          if (i < times - 1) await new Promise((r) => setTimeout(r, 600))
        }
        const hp = typeof target.health === 'number' ? target.health.toFixed(1) : '?'
        return { ok: true, message: `攻击 ${entityText(target, bot)}，出手 ${hits} 次${target.isValid ? `，目标生命 ${hp}` : '，目标已消失'}。` }
      })
    }

    case '/follow': {
      return await action('follow', async () => {
        const bot = requireBot()
        const name = cleanText(query.get('name'), 32)
        if (!name) throw new Error('name 必填（玩家名）')
        const player = bot.players[name]
        if (!player?.entity) throw new Error(`看不到玩家 ${name}（不在线或太远）`)
        const distance = clampInt(query.get('distance'), 3, 1, 8)
        bot.pathfinder.setGoal(new goals.GoalFollow(player.entity, distance), true)
        state.follow = name
        return { ok: true, message: `开始跟随 ${name}，保持 ${distance} 格。用 mc_stop 停止。` }
      })
    }

    case '/stop': {
      return await action('stop', async () => {
        const bot = requireBot()
        bot.pathfinder.setGoal(null)
        bot.clearControlStates()
        state.follow = null
        bot.setControlState('forward', false)
        bot.setControlState('jump', false)
        bot.setControlState('sprint', false)
        return { ok: true, message: '已停止移动/跟随。' }
      })
    }

    case '/collect': {
      return await action('collect', async () => {
        const bot = requireBot()
        const blockName = normalizeItem(cleanText(query.get('block'), 64))
        if (!blockName) throw new Error('block 必填（方块 id，如 oak_log / coal_ore）')
        const radius = clampInt(query.get('radius'), 32, 4, 64)
        const want = clampInt(query.get('count'), 1, 1, 16)
        const center = bot.entity.position
        let done = 0
        const failures = []
        for (let i = 0; i < want; i++) {
          const candidates = bot.findBlocks({
            matching: (block) => block.name === blockName,
            maxDistance: radius,
            count: 8,
          })
          if (candidates.length === 0) break
          let collected = false
          for (const position of candidates) {
            const block = bot.blockAt(position)
            if (!block) continue
            try {
              await withTimeout(bot.collectBlock.collect(block), 30_000, '采集')
              collected = true
              done += 1
              break
            } catch (error) {
              failures.push(error.message)
            }
          }
          if (!collected) break
        }
        if (done === 0) throw new Error(`附近 ${radius} 格没有采到 ${blockName}${failures.length ? `：${failures[0]}` : ''}`)
        return { ok: true, message: `采到 ${blockName} ×${done}（目标 ${want}）。` }
      })
    }

    case '/block': {
      const bot = requireBot()
      const x = clampInt(query.get('x'), NaN, -30000000, 30000000)
      const y = clampInt(query.get('y'), NaN, -64, 320)
      const z = clampInt(query.get('z'), NaN, -30000000, 30000000)
      if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) throw new Error('x、y、z 必填')
      const target = vec3(x, y, z)
      const block = bot.blockAt(target)
      if (!block) throw new Error('这个位置读不到方块（区块没加载？）')
      const neighbours = [
        [0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1],
      ].map(([dx, dy, dz]) => {
        const b = bot.blockAt(target.offset(dx, dy, dz))
        return `${dx},${dy},${dz}=${b?.name ?? '?'}`
      })
      return {
        ok: true,
        message: `(${x},${y},${z}) = ${block.name}｜diggable=${block.diggable}｜replaceable=${block.replaceable}｜bbox=${block.boundingBox}\n邻居：${neighbours.join(' ')}`,
      }
    }

    case '/inventory': {
      const bot = requireBot()
      return { ok: true, message: inventoryText(bot) }
    }

    case '/entities': {
      const bot = requireBot()
      return { ok: true, message: entitiesText(bot, clampInt(query.get('radius'), 16, 2, 64)) }
    }

    case '/scan': {
      const bot = requireBot()
      return { ok: true, message: scanText(bot, clampInt(query.get('radius'), 8, 2, 16)) }
    }

    case '/view': {
      const bot = requireBot()
      const radius = clampInt(query.get('radius'), 20, 8, 32)
      const { png, message } = renderView(bot, { radius })
      return { ok: true, message, png_base64: png.toString('base64') }
    }

    case '/equip': {
      return await action('equip', async () => {
        const bot = requireBot()
        const itemName = normalizeItem(cleanText(query.get('item'), 64))
        const destination = cleanText(query.get('destination'), 16) || 'hand'
        if (!itemName) throw new Error('item 必填')
        if (!['hand', 'head', 'torso', 'legs', 'feet', 'off-hand'].includes(destination)) {
          throw new Error('destination 只能是 hand/head/torso/legs/feet/off-hand')
        }
        const item = bot.inventory.items().find((i) => i.name === itemName)
        if (!item) throw new Error(`背包里没有 ${itemName}`)
        await bot.equip(item, destination)
        return { ok: true, message: `已把 ${itemName} 装到 ${destination}。` }
      })
    }

    case '/use': {
      return await action('use', async () => {
        const bot = requireBot()
        if (!bot.heldItem) throw new Error('手上没有东西')
        bot.activateItem()
        setTimeout(() => {
          try {
            bot.deactivateItem()
          } catch {}
        }, 300)
        return { ok: true, message: `使用了 ${bot.heldItem.name}。` }
      })
    }

    case '/eat': {
      return await action('eat', async () => {
        const bot = requireBot()
        const foods = bot.inventory.items().filter((i) => /cooked|bread|apple|carrot|potato|beef|pork|chicken|mutton|rabbit|fish|stew|pie|cookie|melon|berries|golden_apple/i.test(i.name))
        if (foods.length === 0) throw new Error('背包里没有食物')
        const food = foods[0]
        await bot.equip(food, 'hand')
        await withTimeout(bot.consume(), 10_000, '进食')
        return { ok: true, message: `吃了 ${food.name}，饥饿 ${bot.food.toFixed(0)}/20。` }
      })
    }

    case '/look': {
      return await action('look', async () => {
        const bot = requireBot()
        const yaw = num(query.get('yaw'), NaN)
        const pitch = num(query.get('pitch'), NaN)
        if (!Number.isFinite(yaw) || !Number.isFinite(pitch)) throw new Error('yaw、pitch 必填（弧度）')
        await bot.look(yaw, pitch, true)
        return { ok: true, message: `视角已转向 yaw=${yaw.toFixed(2)} pitch=${pitch.toFixed(2)}。` }
      })
    }

    case '/look_at': {
      return await action('look_at', async () => {
        const bot = requireBot()
        const x = num(query.get('x'), NaN)
        const y = num(query.get('y'), NaN)
        const z = num(query.get('z'), NaN)
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) throw new Error('x、y、z 必填')
        await bot.lookAt(vec3(x, y, z), true)
        return { ok: true, message: `已看向 (${x},${y},${z})。` }
      })
    }

    case '/unstuck': {
      return await action('unstuck', async () => {
        const bot = requireBot()
        const escaped = await attemptUnstuck(bot)
        const pos = bot.entity.position
        return {
          ok: true,
          message: escaped
            ? `脱困成功，现在在 ${fmtPos(pos)}。`
            : `四个方向都没挤出去，仍在 ${fmtPos(pos)}。可以试试挖开旁边的方块，或 mc_goto 到开阔位置。`,
        }
      })
    }

    case '/respawn': {
      const bot = requireBot()
      bot.respawn()
      return { ok: true, message: '已请求重生。' }
    }

    default:
      throw new Error(`未知接口 ${path}`)
  }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

function tokenMatches(header) {
  const expected = `Bearer ${TOKEN}`
  if (typeof header !== 'string' || header.length !== expected.length) return false
  try {
    return crypto.timingSafeEqual(Buffer.from(header), Buffer.from(expected))
  } catch {
    return false
  }
}

const server = http.createServer(async (req, res) => {
  const send = (status, payload) => {
    const body = JSON.stringify(payload)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(body),
      'cache-control': 'no-store',
    })
    res.end(body)
  }

  if (req.method !== 'GET' && req.method !== 'POST') {
    send(405, { ok: false, message: '只支持 GET/POST' })
    return
  }
  let url
  try {
    url = new URL(req.url, 'http://127.0.0.1')
  } catch {
    send(400, { ok: false, message: 'URL 不合法' })
    return
  }
  if (url.pathname === '/health') {
    send(200, { ok: true, message: 'ok' })
    return
  }
  if (!tokenMatches(req.headers.authorization)) {
    send(401, { ok: false, message: 'unauthorized' })
    return
  }
  try {
    const result = await handle(url.pathname, url.searchParams)
    send(200, result)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    log('[http]', url.pathname, 'error:', message)
    send(200, { ok: false, message })
  }
})

server.listen(CONFIG.port, CONFIG.host, () => {
  log(`[http] listening on ${CONFIG.host}:${CONFIG.port}`)
})
rustBehavior.start()

function shutdown(signal) {
  log('[mc] shutting down:', signal)
  state.manualLeave = true
  rustBehavior.stop()
  void rustBackend.stop()
  if (state.reconnectTimer) clearTimeout(state.reconnectTimer)
  try {
    state.bot?.quit('sidecar shutdown')
  } catch {}
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(0), 2000).unref()
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))

process.on('unhandledRejection', (reason) => {
  log('[process] unhandled rejection:', reason)
})
