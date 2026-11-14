/**
 * 私有进服适配器（join hook）。
 *
 * 开源核心只认识一个稳定接口：每次连接/重连之前，加载仓库外的一个 ES module，
 * 把「只有某个部署才需要的进服前动作」交给它——比如某些服要求每次连接都出示
 * 一次性进服码、动态令牌、内网跳板身份等。核心不 import 任何私有实现，也不
 * 把服务器地址/账号写进代码；部署方只需要在 config.json 里写 `joinHook`。
 *
 * hook 契约：
 *   export default async function prepareJoin(ctx) { return overrides | null }
 *   ctx = { phase: 'join'|'reconnect', host, port, profile, info, log }
 *   返回（全部可选）：{ username, password, register, version, auth, backend, note }
 *
 * 约束：
 * - 返回值只在本次连接生效，**不会写进 server-profiles.json**（一次性码存了也会失效）。
 * - `note` 会作为一条 system 事件给 Agent 看，别放密码/token。
 * - hook 抛错或超时会中止这次连接，不会静默用旧身份硬连。
 */

import { existsSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const FIELDS = {
  username: 16,
  password: 64,
  register: 64,
  version: 32,
  auth: 16,
  backend: 16,
}

let cachedPath = ''
let cachedFn = null

/** 环境变量优先于 config.json：同一份开源代码换部署只改环境也能切适配器。 */
export function resolveHookPath(config = {}, env = process.env) {
  const fromEnv = String(env.MCBOT_JOIN_HOOK ?? '').trim()
  return fromEnv || String(config.joinHook ?? '').trim()
}

export async function loadJoinHook(hookPath) {
  if (!hookPath) return null
  if (cachedFn && cachedPath === hookPath) return cachedFn
  if (!existsSync(hookPath)) throw new Error(`进服适配器文件不存在：${hookPath}`)
  const mod = await import(pathToFileURL(hookPath).href)
  const fn = mod.default ?? mod.prepareJoin ?? mod.join
  if (typeof fn !== 'function') {
    throw new Error(`进服适配器必须默认导出 async function(ctx)：${hookPath}`)
  }
  cachedPath = hookPath
  cachedFn = fn
  return fn
}

/** 只收白名单字段；类型不对就报错，免得把畸形值带进连接层。 */
export function sanitizeOverrides(raw) {
  if (raw === null || raw === undefined) return { overrides: {}, note: '' }
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('进服适配器返回值必须是对象')
  }
  const overrides = {}
  for (const [field, maxLen] of Object.entries(FIELDS)) {
    const value = raw[field]
    if (value === undefined || value === null) continue
    if (typeof value !== 'string' && typeof value !== 'number') {
      throw new Error(`进服适配器字段 ${field} 必须是字符串`)
    }
    const text = String(value).trim()
    if (!text) continue
    if (/\s/.test(text)) throw new Error(`进服适配器字段 ${field} 不能含空白`)
    // 超长直接报错而不是截断：截断一次性码/token 会变成另一个合法但错误的身份。
    if (text.length > maxLen) throw new Error(`进服适配器字段 ${field} 超过 ${maxLen} 字符`)
    overrides[field] = text
  }
  const note = typeof raw.note === 'string'
    ? raw.note.replace(/[\r\n\t]+/g, ' ').trim().slice(0, 300)
    : ''
  return { overrides, note }
}

function withTimeout(promise, ms, label) {
  let timer = null
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${Math.round(ms / 1000)}s）`)), ms)
  })
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer))
}

/**
 * 调一次 hook。没配置 hook 时返回空覆盖，普通服务器行为完全不变。
 * 失败信息统一加「进服适配器」前缀，Agent 一眼能看出是部署侧而不是 MC 侧。
 */
export async function prepareJoin({
  hookPath,
  phase = 'join',
  host,
  port,
  profile = null,
  info = null,
  timeoutMs = 20_000,
  log = () => {},
} = {}) {
  const fn = await loadJoinHook(hookPath)
  if (!fn) return { overrides: {}, note: '' }
  const ctx = {
    phase,
    host,
    port,
    profile,
    info,
    log: (message) => log(`[join-hook] ${message}`),
  }
  let raw
  try {
    raw = await withTimeout(Promise.resolve(fn(ctx)), timeoutMs, '进服适配器')
  } catch (error) {
    throw new Error(`进服适配器失败：${error.message}`)
  }
  return sanitizeOverrides(raw)
}

/** 测试用：换 hook 文件时清掉模块级缓存。 */
export function resetJoinHookCache() {
  cachedPath = ''
  cachedFn = null
}
