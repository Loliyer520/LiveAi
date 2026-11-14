import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const ROOT = path.resolve(import.meta.dirname)

/** Owns one mc-native bot process. stdin/stdout are NDJSON; no token or password
 * is put on its command line. The process is killed when stdin cannot close it.
 */
export class RustBackend {
  constructor({ addEvent, log, config = {}, onUnexpectedEnd = null, onChat = null }) {
    this.addEvent = addEvent
    this.log = log
    this.onUnexpectedEnd = onUnexpectedEnd
    // AuthMe 登录提示也走这条聊天流；server.js 用它在 Rust 后端上做自动注册/登录。
    this.onChat = onChat
    this.binary = config.rustBinary ?? path.join(ROOT, 'bin/mc-native')
    this.assets = config.rustAssets ?? path.join(ROOT, 'runtime/minecraft-26.2-client')
    this.runtimeData = config.rustRuntimeData ?? path.join(ROOT, 'runtime/runtime-data-26.2')
    this.workDir = config.rustWorkDir ?? path.join(ROOT, 'runtime/bot')
    this.child = null
    this.info = null
    this.snapshot = null
    this.pending = new Map()
    this.nextId = 1
    this.chatCursor = 0
    this.pollTimer = null
    this.polling = false
    this.stdoutBuffer = ''
    this.stderrBuffer = ''
    this.manualStop = false
    this.readySeen = false
    this.exited = null
  }

  get active() {
    return this.child !== null
  }

  async start(info) {
    if (this.child) throw new Error('Rust 26.2 客户端已经在运行')
    mkdirSync(this.workDir, { recursive: true, mode: 0o700 })
    const configPath = path.join(this.workDir, 'config.json')
    const config = {
      assets: this.assets,
      runtime_data: this.runtimeData,
      player_name: info.username,
      options_file: null,
      hotbar_file: path.join(this.workDir, 'hotbar.nbt'),
      servers_file: path.join(this.workDir, 'servers.dat'),
      screenshots: path.join(this.workDir, 'screenshots'),
      view_distance: 4,
      cpu_cache_mib: 256,
      transient_mib: 32,
    }
    writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 })
    this.info = info
    this.manualStop = false
    this.readySeen = false
    this.snapshot = null
    this.chatCursor = 0
    const child = spawn(this.binary, [
      'bot', '--config', configPath, '--connect', info.host, '--port', String(info.port),
    ], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, MC_AUDIO_SINK: 'off' },
    })
    this.child = child
    let exitResolve
    this.exited = new Promise((resolve) => { exitResolve = resolve })
    child.once('error', (error) => this.log('[rust] process error:', error.message))
    child.once('close', (code, signal) => {
      if (this.child === child) this.child = null
      if (this.pollTimer) clearTimeout(this.pollTimer)
      this.pollTimer = null
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer)
        reject(new Error('Rust 26.2 客户端已退出'))
      }
      this.pending.clear()
      if (!this.manualStop) {
        const detail = this.stderrBuffer.trim().split('\n').slice(-3).join(' | ').slice(0, 500)
        this.addEvent('end', `Rust 26.2 连接结束（code=${code ?? '?'} signal=${signal ?? '?'}）${detail ? `：${detail}` : ''}`)
        // 事件流被 MAX_EVENT_LEN 截断，完整 stderr 尾 12 行另投服务日志排障
        const tail = this.stderrBuffer.trim().split('\n').slice(-12).map((l) => l.slice(0, 400)).join('\n')
        this.log(`[rust] 异常退出 code=${code ?? '?'} stderr 尾:\n${tail}`)
        try {
          this.onUnexpectedEnd?.(this.info)
        } catch (error) {
          this.log('[rust] onUnexpectedEnd error:', error.message)
        }
      }
      exitResolve()
    })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.onStdout(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      this.stderrBuffer = `${this.stderrBuffer}${chunk}`.slice(-8192)
      // 容器/背包排障：这类 eprintln 直指窗口协议问题，转发到服务日志
      for (const line of String(chunk).split('\n')) {
        if (/CONTAINER|INVENTORY|MENU|CHEST|USE_|DIG_AT|ATTACK_ENTITY/i.test(line)) this.log('[rust]', line.slice(0, 300))
      }
    })
    try {
      await this.waitReady(child)
      const deadline = Date.now() + 45_000
      while (Date.now() < deadline) {
        this.snapshot = await this.request('status')
        if (!this.snapshot.level_loading && this.snapshot.position) {
          // 不要把游标跳到当前 sequence：进服后服务器发的登录/注册提示
          // 可能刚好落在这之前，跳过就永远收不到。新进程的事件流本来就是空的。
          this.addEvent('spawn', `${info.username} 已进入 26.2 服务器（Rust 后端）`)
          this.pollTimer = setInterval(() => void this.poll(), 500)
          this.pollTimer.unref()
          return this.snapshot
        }
        await delay(250)
      }
      throw new Error('Rust 26.2 已连接，但等待世界加载超时')
    } catch (error) {
      await this.stop()
      const detail = this.stderrBuffer.trim().split('\n').slice(-4).join(' | ').slice(0, 700)
      throw new Error(`${error.message}${detail ? `｜${detail}` : ''}`)
    }
  }

  onStdout(chunk) {
    this.stdoutBuffer = `${this.stdoutBuffer}${chunk}`
    for (;;) {
      const end = this.stdoutBuffer.indexOf('\n')
      if (end < 0) break
      const line = this.stdoutBuffer.slice(0, end)
      this.stdoutBuffer = this.stdoutBuffer.slice(end + 1)
      if (!line) continue
      let message
      try {
        message = JSON.parse(line)
      } catch {
        this.log('[rust] ignored non-JSON stdout')
        continue
      }
      if (message.type === 'ready') this.readySeen = true
      if (message.type === 'disconnect') this.addEvent('end', `服务器断开：${String(message.reason ?? '').slice(0, 500)}`)
      if (message.id === undefined) continue
      const key = String(message.id)
      const pending = this.pending.get(key)
      if (!pending) continue
      this.pending.delete(key)
      clearTimeout(pending.timer)
      if (message.ok) pending.resolve(message.result)
      else pending.reject(new Error(String(message.error ?? 'Rust 客户端操作失败')))
    }
  }

  waitReady(child) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup()
        reject(new Error('Rust 26.2 启动超时（60s）'))
      }, 60_000)
      const onData = () => {
        if (this.readySeen) {
          cleanup()
          resolve()
        }
      }
      const onExit = () => {
        cleanup()
        reject(new Error('Rust 26.2 启动失败'))
      }
      const cleanup = () => {
        clearTimeout(timer)
        child.stdout.off('data', onData)
        child.off('close', onExit)
      }
      child.stdout.on('data', onData)
      child.once('close', onExit)
      onData()
    })
  }

  request(op, fields = {}, timeoutMs = 10_000) {
    if (!this.child?.stdin.writable) return Promise.reject(new Error('Rust 26.2 客户端未运行'))
    const id = String(this.nextId++)
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`Rust 26.2 ${op} 超时`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.child.stdin.write(`${JSON.stringify({ id, op, ...fields })}\n`, (error) => {
        if (!error) return
        clearTimeout(timer)
        this.pending.delete(id)
        reject(error)
      })
    })
  }

  async poll() {
    if (!this.child || this.polling) return
    this.polling = true
    try {
      const [snapshot, eventBatch] = await Promise.all([
        this.request('status'),
        this.request('events', { since: this.chatCursor }),
      ])
      this.snapshot = snapshot
      this.chatCursor = Math.max(this.chatCursor, Number(eventBatch.next) || 0)
      for (const event of eventBatch.events ?? []) {
        const text = String(event.text ?? '').slice(0, 500)
        this.addEvent('chat', text)
        try {
          this.onChat?.(text)
        } catch (error) {
          this.log('[rust] onChat error:', error.message)
        }
      }
    } catch (error) {
      if (this.child) this.log('[rust] poll:', error.message)
    } finally {
      this.polling = false
    }
  }

  async stop() {
    this.manualStop = true
    if (this.pollTimer) clearTimeout(this.pollTimer)
    this.pollTimer = null
    const child = this.child
    if (!child) return
    try {
      await this.request('leave', {}, 2000)
    } catch {}
    const exited = this.exited
    await Promise.race([exited, delay(2000)])
    if (this.child === child) {
      child.kill('SIGKILL')
      await exited
    }
    this.info = null
    this.snapshot = null
  }
}

export function shouldUseRust(version, protocol) {
  return version === '26.2' || Number(protocol) === 776
}
