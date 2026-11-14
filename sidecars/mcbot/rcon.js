/**
 * 极简 RCON 客户端：读 mc-vanilla 的 server.properties 拿端口/密码，
 * 长连接 + 串行队列（26.2 的原版 RCON 不支持真并发，逐条排队最稳）。
 * 密码只在本模块内部使用，绝不出日志/接口。
 */

import net from 'node:net'
import { existsSync, readFileSync } from 'node:fs'

const PROPS_PATH = process.env.MC_SERVER_PROPERTIES ?? ''

function readProps() {
  if (!existsSync(PROPS_PATH)) return null
  const text = readFileSync(PROPS_PATH, 'utf8')
  const get = (key) => {
    const m = text.match(new RegExp(`^${key}=(.*)$`, 'm'))
    return m ? m[1].trim() : ''
  }
  if (!/^true$/i.test(get('enable-rcon'))) return null
  const port = Number(get('rcon.port'))
  const password = get('rcon.password')
  if (!Number.isInteger(port) || port <= 0 || !password) return null
  return { host: '127.0.0.1', port, password }
}

let config = readProps()
let socket = null
let connected = false
let nextId = 100
const pending = new Map()
let chain = Promise.resolve()
let buffer = null

function auth() {
  return new Promise((resolve, reject) => {
    const id = nextId++
    const payload = Buffer.from(config.password, 'utf8')
    const frame = packet(id, 3, payload)
    const timer = setTimeout(() => {
      pending.delete(id)
      reject(new Error('rcon auth 超时'))
    }, 5000)
    pending.set(id, { resolve: () => { clearTimeout(timer); resolve() }, reject: (e) => { clearTimeout(timer); reject(e) } })
    socket.write(frame)
  })
}

function packet(id, type, payload) {
  const frame = Buffer.alloc(14 + payload.length) // len(4)+id(4)+type(4)+payload+2×NUL
  frame.writeInt32LE(payload.length + 10, 0)
  frame.writeInt32LE(id, 4)
  frame.writeInt32LE(type, 8)
  payload.copy(frame, 12)
  return frame
}

function onFrame(id, body) {
  const entry = pending.get(id)
  if (id === -1 || !entry) return  // auth 失败回 -1；未知的丢弃
  pending.delete(id)
  entry.resolve(body.toString('utf8'))
}

function pump() {
  if (!socket) return
  while (buffer && buffer.length >= 4) {
    const len = buffer.readInt32LE(0)
    if (buffer.length < 4 + len) return
    const id = buffer.readInt32LE(4)
    const body = buffer.subarray(12, 4 + len - 2)
    onFrame(id, body)
    buffer = buffer.subarray(4 + len)
  }
}

async function ensureConnected() {
  if (connected && socket && !socket.destroyed) return
  if (!config) {
    config = readProps()
    if (!config) throw new Error('mc-vanilla 未开 RCON（server.properties enable-rcon）')
  }
  if (socket) { socket.destroy(); socket = null }
  connected = false
  await new Promise((resolve, reject) => {
    socket = net.connect(config.port, config.host)
    socket.once('connect', resolve)
    socket.once('error', (e) => reject(e))
  })
  socket.on('data', (chunk) => {
    buffer = buffer ? Buffer.concat([buffer, chunk]) : chunk
    pump()
  })
  socket.on('error', () => { connected = false })
  socket.on('close', () => {
    connected = false
    for (const [, entry] of pending) entry.reject(new Error('rcon 连接断开'))
    pending.clear()
  })
  socket.setNoDelay(true)
  try {
    await auth()
    connected = true
  } catch (error) {
    socket.destroy()
    socket = null
    throw error
  }
}

/** 串行队列：所有命令逐条排队跑（RCON 无并发语义），失败也不断链。 */
function enqueue(fn) {
  const run = chain.then(fn, fn)
  chain = run.catch(() => {})
  return run
}

async function runCommand(command) {
  try {
    await ensureConnected()
    const id = nextId++
    const payload = Buffer.from(command, 'utf8')
    const frame = packet(id, 2, payload)
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id)
        socket?.destroy()
        reject(new Error(`rcon 命令超时：${command.slice(0, 60)}`))
      }, 10000)
      pending.set(id, { resolve: (text) => { clearTimeout(timer); resolve(text) }, reject: (e) => { clearTimeout(timer); reject(e) } })
      socket.write(frame)
    })
  } catch (error) {
    // 断线/重启场景：重置连接后在同一队列槽里重试一次
    connected = false
    socket?.destroy()
    socket = null
    await ensureConnected()
    throw error
  }
}

/** 跑一条命令，返回服务端回包文本；连接问题自动重连重试一次。 */
export function rconCommand(command) {
  return enqueue(() => runCommand(command).catch((error) => {
    if (!/超时|断开/.test(String(error.message))) throw error
    return runCommand(command)
  }))
}

/** execute if block … 之类的布尔命令：passed = true。 */
export async function rconTest(command) {
  return /passed/i.test(await rconCommand(command))
}

export function rconReady() {
  return config !== null
}
