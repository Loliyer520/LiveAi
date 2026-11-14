/**
 * 通用任务调度系统——支持多行为并行、优先级仲裁、动态创建/取消任务。
 *
 * 核心模型：
 * - Task：一个可执行的行为实例（优先级、状态、每 tick 推进一步的 fn）
 * - Scheduler：维护任务队列，每 tick 按优先级从高到低依次尝试执行；
 *   谁先「占用身体」谁执行，返回 { idle: true } 的任务让位给下一个——
 *   这样高优先级任务「没活干」的那一拍，低优先级任务能补上（跟随+采集并行）。
 *
 * 优先级分层（越高越先尝试）：
 *   100+ : 紧急（反击/脱危）
 *   50-99: 重要（进食/防卫）
 *   10-49: 主任务（跟随/采集/建筑）
 *   1-9  : 辅助（自动捡拾、整理背包）
 *
 * Task.fn 返回约定：
 *   { done: true }            任务完成，本 tick 后销毁
 *   { idle: true }            本 tick 无活可干（不占用身体），调度器继续尝试下一个任务
 *   { claimed: true } / 无返回  本 tick 占用了身体，后续低优先级任务本 tick 不再执行
 *   { priority: n }           动态调整自身优先级（下一 tick 生效）
 *   抛异常                     任务失败，记录 error 后销毁
 */

class Task {
  constructor(id, type, priority, fn, params = {}) {
    this.id = id
    this.type = type
    this.priority = priority
    this.basePriority = priority   // 动态优先级调整的回归基线
    this.fn = fn
    this.params = params
    this.status = 'running'   // running | paused | done | failed
    this.createdAt = Date.now()
    this.lastTick = 0
    this.error = null
    this.note = ''            // 结束说明（done/failed 时写）
    this.state = params.state ?? {}  // 任务自己维护的运行时状态（对外可查）
    this.timeoutAt = params.timeoutMs ? Date.now() + params.timeoutMs : 0
    // 约束：{ check: async (task, scheduler, me) => null | { blocked: true, reason } }
    // 约束不满足时本拍让位（不占身体），下一拍再试——低优先级任务可趁隙执行。
    this.constraints = params.constraints ?? null
    this.blocked = null       // 当前阻塞原因（constraints 不满足时）
  }

  /** 执行一帧。返回 { status, claimed }。 */
  async tick(scheduler, me, tick) {
    if (this.status !== 'running') return { status: this.status, claimed: false }
    if (this.timeoutAt && Date.now() > this.timeoutAt) {
      this.status = 'done'
      this.note = this.note || `超时（${Math.round((Date.now() - this.createdAt) / 1000)}s）`
      return { status: 'done', claimed: false }
    }
    // 约束检查：超距/依赖未就绪等 → 本拍让位（不占身体、不调 fn）
    if (this.constraints?.check) {
      let verdict = null
      try {
        verdict = await this.constraints.check(this, scheduler, me)
      } catch (err) {
        verdict = { blocked: true, reason: `约束检查出错：${err?.message ?? err}` }
      }
      if (verdict?.blocked) {
        this.blocked = verdict.reason || '约束未满足'
        this.state.blocked = this.blocked
        return { status: 'running', claimed: false }
      }
      if (this.blocked) {
        this.blocked = null
        delete this.state.blocked
      }
    }
    let r
    try {
      r = await this.fn(scheduler, me, tick, this)
    } catch (err) {
      this.status = 'failed'
      this.error = err?.message ?? String(err)
      return { status: 'failed', claimed: false }
    }
    this.lastTick = tick
    if (r && typeof r === 'object') {
      if (Number.isFinite(r.priority)) this.priority = r.priority
      if (r.note) this.note = r.note
      if (r.state) Object.assign(this.state, r.state)
      if (r.done) {
        this.status = 'done'
        return { status: 'done', claimed: false }
      }
      if (r.idle) return { status: 'running', claimed: false }
    }
    return { status: 'running', claimed: true }
  }
}

export class Scheduler {
  constructor() {
    this.tasks = new Map()     // id -> Task
    this.tick = 0
    this.executing = null      // 本 tick 实际占用身体的任务 ID（无则 null）
    this.lastResults = []      // 上一 tick 每个任务的执行结果（排障/状态查询用）
    this.history = []          // 已结束任务（done/failed）的归档，cap 40——Agent 要能查到「结果如何」
  }

  /** 创建任务。返回 taskId。 */
  create(type, priority, fn, id = null, params = {}) {
    const taskId = id || `${type}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`
    const task = new Task(taskId, type, priority, fn, params)
    this.tasks.set(taskId, task)
    return taskId
  }

  /** 取消任务（status=failed + note=原因，随后销毁）。 */
  cancel(taskId, reason = 'cancelled') {
    const task = this.tasks.get(taskId)
    if (!task) return false
    task.status = 'failed'
    task.error = reason
    task.note = reason
    return true
  }

  /** 查询单个任务（含已结束但未被清理的）。 */
  get(taskId) {
    return this.tasks.get(taskId) ?? null
  }

  /** 列出活跃任务（按优先级降序）。 */
  list() {
    return Array.from(this.tasks.values())
      .filter((t) => t.status === 'running')
      .sort((a, b) => b.priority - a.priority)
  }

  /** 清理所有已结束的任务，归档进 history，返回被清理的 Task 数组。 */
  reap() {
    const dead = []
    for (const [id, task] of this.tasks.entries()) {
      if (task.status === 'done' || task.status === 'failed') {
        dead.push(task)
        this.tasks.delete(id)
        this.history.push({
          id: task.id,
          type: task.type,
          priority: task.priority,
          status: task.status,
          state: { ...task.state },
          note: task.note,
          error: task.error,
          created: task.createdAt,
          ended: Date.now(),
        })
      }
    }
    if (this.history.length > 40) this.history.splice(0, this.history.length - 40)
    return dead
  }

  /** 已结束任务历史（最新在后）。 */
  historyList(limit = 10) {
    return this.history.slice(-limit)
  }

  /**
   * 调度主循环（每 tick 调一次）：
   * 1. 清理已结束任务
   * 2. 按优先级从高到低依次尝试；第一个「占用身体」的任务执行完即止
   * 3. 全都没活干（全 idle）→ executing = null
   */
  async step(me) {
    this.tick += 1
    this.reap()

    if (this.tasks.size === 0) {
      this.executing = null
      this.lastResults = []
      return { executed: null, running: 0, paused: 0, results: [] }
    }

    const sorted = this.list()
    const results = []
    let executed = null

    for (const task of sorted) {
      const r = await task.tick(this, me, this.tick)
      results.push({ id: task.id, type: task.type, priority: task.priority, status: r.status, claimed: r.claimed })
      if (r.status === 'done' || r.status === 'failed') continue
      if (r.claimed) {
        executed = { id: task.id, type: task.type, priority: task.priority }
        break
      }
    }

    this.executing = executed?.id ?? null
    this.lastResults = results
    return {
      executed,
      running: this.tasks.size,
      paused: 0,          // 暂停模型已改为「按 tick 让位」，无持久暂停态
      results,
    }
  }

  /** 当前身体所有者（本 tick 执行的任务）。 */
  currentOwner() {
    return this.executing ? this.tasks.get(this.executing) : null
  }

  /** 身体是否被优先级高于 targetPriority 的任务占用。 */
  isBodyBusy(targetPriority) {
    const owner = this.currentOwner()
    return Boolean(owner && owner.priority > targetPriority)
  }
}

export default Scheduler
