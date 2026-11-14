/**
 * 每个服务器一份的进服档案：用户名 / 密码 / 认证方式。
 *
 * 为什么单独一个文件：`mc-accounts.json` 是早期单服时代留下的扁平记录，没有任何
 * 代码读它；「随便进服」必须按 host:port 记，否则换服就会拿 A 服密码去登 B 服。
 *
 * 隐私：密码是离线服 AuthMe 用的，文件 0600；日志/事件里绝不回显密码原文。
 */

import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import crypto from 'node:crypto'
import path from 'node:path'

const PROFILE_PATH = process.env.MCBOT_PROFILES ?? path.join(path.dirname(process.env.MCBOT_CONFIG ?? path.join(import.meta.dirname, 'config.json')), 'server-profiles.json')
const PASSWORD_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
const PASSWORD_LEN = 12

export function profileKey(host, port) {
  const h = String(host ?? '').trim().toLowerCase().replace(/^\/+|\/+$/g, '')
  const p = Number(port)
  return `${h}:${Number.isFinite(p) && p > 0 ? Math.trunc(p) : 25565}`
}

function readAll() {
  if (!existsSync(PROFILE_PATH)) return {}
  try {
    const data = JSON.parse(readFileSync(PROFILE_PATH, 'utf8'))
    return data && typeof data === 'object' && !Array.isArray(data) ? data : {}
  } catch {
    return {}
  }
}

function writeAll(data) {
  const tmp = `${PROFILE_PATH}.tmp-${process.pid}`
  writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 })
  renameSync(tmp, PROFILE_PATH)
}

export function readProfile(host, port) {
  const entry = readAll()[profileKey(host, port)]
  return entry && typeof entry === 'object' ? { ...entry } : null
}

export function writeProfile(host, port, patch) {
  if (!patch || typeof patch !== 'object') return null
  const all = readAll()
  const key = profileKey(host, port)
  const next = { ...(all[key] ?? {}) }
  for (const [field, value] of Object.entries(patch)) {
    if (value === undefined) continue
    next[field] = value
  }
  next.updatedAt = Date.now()
  all[key] = next
  try {
    writeAll(all)
  } catch {
    // 写档案失败不该挡住进服；下次再存。
    return next
  }
  return next
}

export function generatePassword() {
  const bytes = crypto.randomBytes(PASSWORD_LEN)
  let out = ''
  for (const byte of bytes) out += PASSWORD_ALPHABET[byte % PASSWORD_ALPHABET.length]
  return out
}

/** 已有就返回已有密码，没有就生成并立刻落盘（先进服、后补存都不丢）。 */
export function ensurePassword(host, port) {
  const existing = readProfile(host, port)?.password
  if (existing) return existing
  const password = generatePassword()
  writeProfile(host, port, { password })
  return password
}
