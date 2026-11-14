// 调度器单测：优先级仲裁 / idle 让位 / 取消 / 超时 / 异常隔离。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Scheduler } from './scheduler.js'

test('create/list：按优先级降序返回活跃任务', () => {
  const s = new Scheduler()
  s.create('low', 5, async () => ({ idle: true }))
  s.create('high', 50, async () => ({ idle: true }))
  s.create('mid', 20, async () => ({ idle: true }))
  assert.deepEqual(s.list().map((t) => t.type), ['high', 'mid', 'low'])
})

test('cancel：取消后从活跃列表消失，get 仍可取到并带原因', () => {
  const s = new Scheduler()
  const id = s.create('x', 10, async () => ({ idle: true }))
  assert.equal(s.cancel(id, '不要了'), true)
  assert.equal(s.list().length, 0)
  assert.equal(s.get(id).status, 'failed')
  assert.equal(s.get(id).note, '不要了')
  assert.equal(s.cancel('不存在'), false)
})

test('step：高优先级任务占用身体后，低优先级本 tick 不执行', async () => {
  const s = new Scheduler()
  const ran = []
  s.create('high', 50, async () => { ran.push('high'); return { claimed: true } })
  s.create('low', 5, async () => { ran.push('low'); return { claimed: true } })
  const r = await s.step([0, 70, 0])
  assert.deepEqual(ran, ['high'])
  assert.equal(r.executed.type, 'high')
})

test('step：高优先级 idle 时让位，低优先级同 tick 接手', async () => {
  const s = new Scheduler()
  const ran = []
  s.create('high', 50, async () => { ran.push('high'); return { idle: true } })
  s.create('low', 5, async () => { ran.push('low'); return { claimed: true } })
  const r = await s.step([0, 70, 0])
  assert.deepEqual(ran, ['high', 'low'])
  assert.equal(r.executed.type, 'low')
})

test('step：全部 idle → executed=null（身体空闲）', async () => {
  const s = new Scheduler()
  s.create('a', 10, async () => ({ idle: true }))
  const r = await s.step([0, 70, 0])
  assert.equal(r.executed, null)
  assert.equal(r.running, 1)
})

test('step：done 任务本 tick 后清理，不再参与调度', async () => {
  const s = new Scheduler()
  let n = 0
  s.create('once', 10, async () => { n += 1; return { done: true, note: '完成' } })
  await s.step([0, 70, 0])
  assert.equal(n, 1)
  assert.equal(s.list().length, 0)
  await s.step([0, 70, 0])
  assert.equal(n, 1, 'done 后不应再执行')
})

test('异常：任务抛错标记 failed，不影响其他任务继续调度', async () => {
  const s = new Scheduler()
  const ran = []
  const badId = s.create('bad', 100, async () => { throw new Error('炸了') })
  s.create('good', 10, async () => { ran.push('good'); return { claimed: true } })
  await s.step([0, 70, 0])
  const bad = s.get(badId)
  // 本 tick 结束后 bad 还在（下一 tick 才 reap），状态 failed
  assert.equal(bad.status, 'failed')
  assert.equal(bad.error, '炸了')
  assert.deepEqual(ran, ['good'])
})

test('超时：timeoutMs 到点后任务以 done 结束并写 note', async () => {
  const s = new Scheduler()
  const slowId = s.create('slow', 10, async () => ({ claimed: true }), null, { timeoutMs: 1 })
  await new Promise((r) => setTimeout(r, 10))
  const r = await s.step([0, 70, 0])
  assert.equal(r.executed, null)
  const t = s.get(slowId)
  assert.equal(t.status, 'done')
  assert.match(t.note, /超时/)
})

test('动态优先级：fn 返回 priority 下一 tick 生效', async () => {
  const s = new Scheduler()
  const ran = []
  s.create('a', 10, async () => { ran.push('a'); return { priority: 1, claimed: true } })
  s.create('b', 9, async () => { ran.push('b'); return { claimed: true } })
  await s.step([0, 70, 0])
  assert.deepEqual(ran, ['a'], '首 tick a 优先级最高')
  await s.step([0, 70, 0])
  assert.deepEqual(ran, ['a', 'b'], 'a 自降优先级后 b 接手')
})

test('isBodyBusy：按本 tick 执行者判断', async () => {
  const s = new Scheduler()
  s.create('big', 50, async () => ({ claimed: true }))
  await s.step([0, 70, 0])
  assert.equal(s.isBodyBusy(10), true)
  assert.equal(s.isBodyBusy(99), false)
  assert.equal(s.currentOwner()?.type, 'big')
})

test('约束：不满足时本拍让位（不调 fn），低优先级同 tick 接手', async () => {
  const s = new Scheduler()
  const ran = []
  s.create('mine', 10, async () => { ran.push('mine'); return { claimed: true } }, null, {
    constraints: { check: () => ({ blocked: true, reason: '离主人 12 格' }) },
  })
  s.create('idle-low', 5, async () => { ran.push('low'); return { claimed: true } })
  const r = await s.step([0, 70, 0])
  assert.deepEqual(ran, ['low'])
  assert.equal(r.executed.type, 'idle-low')
})

test('约束：阻塞原因写进 state.blocked，解除后恢复执行并清掉标记', async () => {
  const s = new Scheduler()
  const dist = { d: 20 }
  const ran = []
  const id = s.create('mine', 10, async () => { ran.push('mine'); return { claimed: true } }, null, {
    constraints: { check: () => (dist.d > 8 ? { blocked: true, reason: `距 ${dist.d}` } : null) },
  })
  await s.step([0, 70, 0])
  assert.equal(s.get(id).state.blocked, '距 20')
  assert.deepEqual(ran, [], '阻塞时不执行 fn')
  dist.d = 2
  const r = await s.step([0, 70, 0])
  assert.deepEqual(ran, ['mine'])
  assert.equal(r.executed.type, 'mine')
  assert.equal(s.get(id)?.state?.blocked, undefined)
})

test('约束：check 抛异常按阻塞处理，不会把任务判失败', async () => {
  const s = new Scheduler()
  const id = s.create('x', 10, async () => ({ claimed: true }), null, {
    constraints: { check: () => { throw new Error('锚点没了') } },
  })
  const r = await s.step([0, 70, 0])
  assert.equal(r.executed, null)
  assert.equal(s.get(id).status, 'running')
  assert.match(s.get(id).state.blocked, /锚点没了/)
})

test('basePriority：动态改 priority 不影响回归基线', async () => {
  const s = new Scheduler()
  const id = s.create('retaliate', 20, async () => ({ idle: true }))
  const t = s.get(id)
  t.priority = 100
  assert.equal(t.basePriority, 20)
})
