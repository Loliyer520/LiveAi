// 行为层引擎单测：网格世界 + 假 rust 后端 + 假 RCON，不连真服。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createBehaviorEngine } from './behaviors.js'

/** 搭一个可脚本化的小世界：bot 会按 look 方向走路，挖/放改地图。 */
export function makeWorld({ health = 20, food = 20, withNav = false, tools = null } = {}) {
  const blocks = new Map()  // 'x,y,z' -> name；缺省 = air
  const key = (x, y, z) => `${x},${y},${z}`
  const state = {
    pos: [0.5, 70, 0.5],
    yaw: 0,
    health,
    food,
    buttons: 0,
    attacks: 0,
    events: [],
    zombie: null,   // [x,y,z] | null
    playerPos: null, // [x,y,z] | null（followPlayer 的目标玩家）
    rconLog: [],
  }
  // 默认平地：y=69 全 stone
  const fillPlane = (r = 12) => {
    for (let x = -r; x <= r; x++) for (let z = -r; z <= r; z++) blocks.set(key(x, 69, z), 'stone')
  }

  const blockIs = async (x, y, z, name) => {
    const b = blocks.get(key(x, y, z))
    const n = String(name).replace(/^minecraft:/, '')
    if (n === 'air' || n === 'cave_air' || n === 'void_air') return !b
    if (n === '#minecraft:logs') return Boolean(b && b.endsWith('_log'))
    if (n === '#minecraft:leaves') return Boolean(b && b.endsWith('_leaves'))
    return b === n
  }
  const blockIsAir = async (x, y, z) => !blocks.get(key(x, y, z))

  const rust = {
    active: true,
    get snapshot() {
      return {
        position: [...state.pos], yaw: state.yaw, pitch: 0,
        health: state.health, food: state.food, inventory: [],
      }
    },
    async request(op, fields = {}) {
      if (op === 'status') return this.snapshot
      if (op === 'move') {
        state.buttons = fields.buttons
        if (fields.buttons & 1) {
          const ox = state.pos[0]
          const oz = state.pos[2]
          // 朝 yaw 走 0.45 格（vanilla yaw：atan2(-dx,dz)）
          state.pos[0] += -Math.sin(state.yaw) * 0.45
          state.pos[2] += Math.cos(state.yaw) * 0.45
          const fx = Math.floor(state.pos[0])
          const fz = Math.floor(state.pos[2])
          const fy = Math.floor(state.pos[1])
          const solidAt = (bx, by, bz) => Boolean(blocks.get(key(bx, by, bz)))
          // 碰撞：脚/头格有实体则走不进
          let blocked = solidAt(fx, fy, fz) || solidAt(fx, fy + 1, fz)
          // 对角楔死（原版 AABB 角碰撞）：x/z 同时跨列时，两个正交前柱
          // 任一在脚/头格有实体，碰撞箱被角卡住——对角列是空气也走不动。
          if (!blocked && fx !== Math.floor(ox) && fz !== Math.floor(oz)) {
            blocked = solidAt(fx, fy, Math.floor(oz)) || solidAt(fx, fy + 1, Math.floor(oz))
              || solidAt(Math.floor(ox), fy, fz) || solidAt(Math.floor(ox), fy + 1, fz)
          }
          // 但按住跳且上方两格空可跨上一格
          if (blocked) {
            const canStep = (fields.buttons & 16)
              && !blocks.get(key(fx, fy + 1, fz)) && !blocks.get(key(fx, fy + 2, fz))
              && Boolean(blocks.get(key(fx, fy, fz)))
            if (canStep) {
              state.pos[1] = fy + 1
            } else {
              state.pos[0] = ox
              state.pos[2] = oz
              return { moving: false }
            }
          } else {
            // 重力：脚下一格是空气就落一格
            const onGround = Boolean(blocks.get(key(fx, fy - 1, fz)))
            if (!onGround) state.pos[1] -= 1
          }
        }
        return { moving: fields.buttons !== 0 }
      }
      if (op === 'attack') { if (fields.down) state.attacks += 1; return { down: fields.down } }
      if (op === 'stop') { state.buttons = 0; return { stopped: true } }
      if (op === 'select') return { selected: fields.slot }
      if (op === 'use') return { down: fields.down }
      return {}
    },
  }

  const rcon = async (cmd) => {
    state.rconLog.push(cmd)
    let m = cmd.match(/^execute if block (-?\d+) (-?\d+) (-?\d+) (\S+)/)
    if (m) {
      const ok = await blockIs(Number(m[1]), Number(m[2]), Number(m[3]), m[4])
      if (!ok) throw new Error('Test failed')
      return 'Test passed'
    }
    m = cmd.match(/(?:^execute positioned [\d. -]+ run )?data get entity @e\[type=minecraft:(\w+),distance=\.\.[\d.]+,sort=nearest,limit=1\] Pos/)
    if (m) {
      if (m[1] === 'zombie' && state.zombie) return `zombie has the following entity data: [${state.zombie.map((v) => `${v}d`).join(', ')}]`
      throw new Error('No entity was found')
    }
    throw new Error('Unknown command')
  }

  // 本地世界扫描模拟：直接查内存地图（对应 Rust 客户端区块缓存），零 RCON。
  const nativeScan = async ({ names, radius, dyMin = -3, dyMax = 12, limit = 64 }) => {
    state.nativeScans = (state.nativeScans ?? 0) + 1
    const [px, py, pz] = state.pos
    const [cx, cy, cz] = [Math.floor(px), Math.floor(py), Math.floor(pz)]
    const wanted = new Set(names.map((n) => String(n).replace(/^minecraft:/, '')))
    const hits = []
    let scanned = 0
    for (let dx = -radius; dx <= radius; dx++) {
      for (let dz = -radius; dz <= radius; dz++) {
        for (let dy = dyMin; dy <= dyMax; dy++) {
          scanned += 1
          const b = blocks.get(key(cx + dx, cy + dy, cz + dz))
          if (b && wanted.has(b)) hits.push([cx + dx, cy + dy, cz + dz, b])
        }
      }
    }
    hits.sort((a, b) => (a[0] - cx) ** 2 + (a[2] - cz) ** 2 - ((b[0] - cx) ** 2 + (b[2] - cz) ** 2))
    return { hits: hits.slice(0, limit), scanned, unloaded: 0, center: [cx, cy, cz] }
  }

  // 假 nav：吐 RLE 体素盒（对应 Rust 端 nav op 的格式）。
  // 碰撞顶高：满块 16，半砖 8，其余 0；palette[0] 恒为空气。
  const nav = async ({ radius = 16, dyMin = -8, dyMax = 8 } = {}) => {
    state.navCalls = (state.navCalls ?? 0) + 1
    const [px, py, pz] = state.pos
    const [cx, cy, cz] = [Math.floor(px), Math.floor(py), Math.floor(pz)]
    const ox = cx - radius
    const oy = cy + dyMin
    const oz = cz - radius
    const w = radius * 2 + 1
    const h = dyMax - dyMin + 1
    const d = radius * 2 + 1
    const cells = new Array(w * h * d).fill('air')
    const palette = [['minecraft:air', 0]]
    const slotOf = new Map()
    const slotFor = (name, top) => {
      const k = `${name}|${top}`
      if (!slotOf.has(k)) {
        slotOf.set(k, palette.length)
        palette.push([`minecraft:${name}`, top])
      }
      return slotOf.get(k)
    }
    for (let y = 0; y < h; y++) {
      for (let z = 0; z < d; z++) {
        for (let x = 0; x < w; x++) {
          const b = blocks.get(key(ox + x, oy + y, oz + z))
          const idx = (y * d + z) * w + x
          if (b) cells[idx] = `s${slotFor(b, 16)}`
        }
      }
    }
    // RLE：同槽连成一串
    const runs = []
    let cur = null
    let count = 0
    for (const c of cells) {
      const slot = c === 'air' ? 0 : Number(c.slice(1))
      if (cur === slot) { count++; continue }
      if (cur !== null) runs.push([cur, count])
      cur = slot
      count = 1
    }
    if (cur !== null) runs.push([cur, count])
    return {
      ok: true,
      origin: [ox, oy, oz],
      size: [w, h, d],
      palette,
      runs,
      feet: [px, py, pz],
      pos: [px, py, pz],
      eye: 1.62,
      on_ground: true,
      unloaded: 0,
    }
  }

  const deps = {
    rust,
    rcon,
    nativeScan,
    ...(withNav ? { nav } : {}),
    ...(tools ? { toolsInInventory: async () => tools } : {}),
    addEvent: (type, text) => state.events.push(`${type}: ${text}`),
    log: () => {},
    blockIs,
    blockIsAir,
    look: async (yaw) => { state.yaw = yaw },
    lookAtPoint: async (x, y, z) => {
      state.yaw = Math.atan2(-(x - state.pos[0]), z - state.pos[2])
    },
    digBlock: async (x, y, z) => {
      if (blocks.get(key(x, y, z)) === 'tough_stone') return false  // 挖不动的硬块（测连挖两轮报阻）
      blocks.delete(key(x, y, z))
      return true
    },
    placeBlock: async (x, y, z, item) => {
      if (blocks.get(key(x, y, z))) throw new Error('不是空气')
      blocks.set(key(x, y, z), item)
      return true
    },
    eatFood: async () => { state.food = 20; state.events.push('ate'); return '吃了 bread×1' },
    ensureHotbar: async () => 0,
    entityPos: async () => state.playerPos,
    normalizeItem: (s) => String(s).toLowerCase().replace(/^minecraft:/, ''),
    statePath: null,
    tickMs: 20,
    stepMs: 2,
  }
  return { state, blocks, key, fillPlane, deps }
}

test('travel：平地直行到达', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000 })
  assert.match(msg, /已走到/)
  assert.ok(Math.hypot(8 - w.state.pos[0], 0 - w.state.pos[2]) < 1.3)
})

test('travel：一堵墙 + dig=true 挖隧道通过', async () => {
  const w = makeWorld()
  w.fillPlane()
  for (let z = -2; z <= 2; z++) {
    w.blocks.set(w.key(4, 70, z), 'dirt')
    w.blocks.set(w.key(4, 71, z), 'dirt')
  }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000, dig: true })
  assert.match(msg, /已走到/)
  assert.equal(w.blocks.get(w.key(4, 70, 0)), undefined, '脚格应被挖穿')
  assert.equal(w.blocks.get(w.key(4, 71, 0)), undefined, '头格应被挖穿')
})

test('travel：禁挖遇墙 → 侧绕不成报被挡死', async () => {
  const w = makeWorld()
  w.fillPlane()
  for (let z = -12; z <= 12; z++) for (let y = 70; y <= 73; y++) w.blocks.set(w.key(4, y, z), 'bedrock')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 5_000, dig: false })
  assert.match(msg, /被挡死|超时/)
  assert.ok(w.state.pos[0] < 4, '不应穿过基岩墙')
})

test('travel：深沟无桥即停，给桥则搭过', async () => {
  const w = makeWorld()
  w.fillPlane()
  for (let z = -12; z <= 12; z++) {
    w.blocks.delete(w.key(4, 69, z))
    w.blocks.delete(w.key(4, 68, z))
    w.blocks.delete(w.key(5, 69, z))
    w.blocks.delete(w.key(5, 68, z))
  }
  const engine = createBehaviorEngine(w.deps)
  const msg1 = await engine.travel({ x: 9, z: 0, timeoutMs: 8_000, dig: true })
  assert.match(msg1, /深沟|悬崖/)
  // 给桥重来
  w.state.pos = [0.5, 70, 0.5]
  const msg2 = await engine.travel({ x: 9, z: 0, timeoutMs: 30_000, dig: true, bridge: 'dirt' })
  assert.match(msg2, /已走到/)
  assert.equal(w.blocks.get(w.key(4, 69, 0)), 'dirt', '沟沿应搭出 dirt')
})

test('travel：y 上行挖楼梯——留脚踏格当台阶逐列爬升', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 2 格高台地：x=4..8, y=70..71 实心（台面 y=72）
  for (let x = 4; x <= 8; x++) for (let z = -2; z <= 2; z++) for (let y = 70; y <= 71; y++) w.blocks.set(w.key(x, y, z), 'dirt')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, y: 72, timeoutMs: 30_000, dig: true })
  assert.match(msg, /已走到/)
  assert.ok(Math.abs(w.state.pos[1] - 72) < 1.6, `应爬到台面 72，实际 ${w.state.pos[1]}`)
  // 楼梯挖的铁证：首列脚踏格 (4,70) 必须还在（平挖隧道会把它也挖掉）
  assert.equal(w.blocks.get(w.key(4, 70, 0)), 'dirt', '首列脚踏格应保留当台阶')
  assert.equal(w.blocks.get(w.key(4, 71, 0)), undefined, '首列头格应被挖掉')
})

test('travel：y 是到点硬条件——高度不够不得报到达', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  // 目标就在脚下水平位但高 5 格，没给桥方块：不许报到达
  const msg = await engine.travel({ x: 2, z: 0, y: 75, timeoutMs: 4_000, dig: true })
  assert.doesNotMatch(msg, /已走到/)
})

test('travel：视角死带——直线行走不反复重发 look（治左瞧右看抽搐）', async () => {
  const w = makeWorld()
  w.fillPlane()
  let lookCalls = 0
  const origLook = w.deps.look
  w.deps.look = async (yaw) => { lookCalls += 1; return origLook(yaw) }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000 })
  assert.match(msg, /已走到/)
  assert.ok(lookCalls <= 5, `直线 8 格 look 调用应 ≤5（有死带），实际 ${lookCalls}`)
})

test('travel：水平到点但目标在脚下深处——如实报高度不符，不在原地抽搐', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  // y=60 远低于站立面 70：水平能到柱位，但下不去
  const msg = await engine.travel({ x: 3, z: 0, y: 60, timeoutMs: 10_000 })
  assert.match(msg, /高度不符/, `实际：${msg}`)
})

test('mine：扫描定位+走过去挖，凑够数量并返程', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 一小撮泥土垛：(4,70,2) 与其正上方 (4,71,2)
  w.blocks.set(w.key(4, 70, 2), 'dirt')
  w.blocks.set(w.key(4, 71, 2), 'dirt')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.mine({ block: 'dirt', count: 2, range: 8, timeoutMs: 30_000, back: true })
  assert.match(msg, /完成：挖到 2\/2 个 dirt/, `实际：${msg}`)
  assert.equal(w.blocks.get(w.key(4, 70, 2)), undefined)
  assert.equal(w.blocks.get(w.key(4, 71, 2)), undefined)
  assert.match(msg, /返程：已走到/, `应返程，实际：${msg}`)
})

test('mine：别名 log=任意原木 + 分层扫描够到头顶 y+6 的树', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 天然台阶山坡（每列抬一格）爬到 y=75，桦木在 y=76=cy+6——
  // 单层带 [cy-3,cy+4] 之外，分层扫描前实机就是这样（山地树在头顶）永远扫不到。
  for (let i = 0; i <= 5; i++) for (let y = 70; y <= 70 + i; y++) w.blocks.set(w.key(2 + i, y, 0), 'dirt')
  w.blocks.set(w.key(7, 76, 0), 'birch_log')  // 故意用桦木：别名 log 必须跨树种命中
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.mine({ block: 'log', count: 1, range: 10, timeoutMs: 30_000 })
  assert.match(msg, /完成：挖到 1\/1 个 log/, `实际：${msg}`)
  assert.equal(w.blocks.get(w.key(7, 76, 0)), undefined, '原木应被挖掉')
})

test('travel：对角楔死——正交双柱挡脚时挖楔角通过', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 对角走廊 (2,2) 畅通，但两个正交前柱 (2,1)/(1,2) 是石头：
  // 原版碰撞箱被角楔死（11-05 实机：山体内楼梯角双石头，bot 原地跳满 25s 超时，
  // 对角探柱是空气所以既不报墙也不报沟）。修复后应主动挖掉楔角通过。
  w.blocks.set(w.key(2, 70, 1), 'stone')
  w.blocks.set(w.key(1, 70, 2), 'stone')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 5, z: 5, timeoutMs: 20_000, dig: true })
  assert.match(msg, /已走到/, `实际：${msg}`)
  assert.equal(w.blocks.get(w.key(2, 70, 1)), undefined, '楔角石 (2,70,1) 应被挖掉')
  assert.equal(w.blocks.get(w.key(1, 70, 2)), undefined, '楔角石 (1,70,2) 应被挖掉')
})

test('travel：挖楼梯连挖两轮不破——如实报阻，不空烧整段行程', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 高台全是挖不动的 tough_stone：楼梯每挖必败。
  // 旧版同一格 12s 一轮无限重挖，把整段 timeout 烧在原地；修复后两轮即报阻。
  for (let z = -1; z <= 1; z++) for (let y = 70; y <= 72; y++) w.blocks.set(w.key(3, y, z), 'tough_stone')
  const engine = createBehaviorEngine(w.deps)
  const t0 = Date.now()
  const msg = await engine.travel({ x: 6, z: 0, y: 73, timeoutMs: 60_000, dig: true })
  assert.match(msg, /遇阻/, `实际：${msg}`)
  assert.match(msg, /连挖两轮不破/, `实际：${msg}`)
  assert.ok(Date.now() - t0 < 30_000, '应在两轮失败后快速报阻，而非空转到超时')
})

test('mine：目标已在挖掘半径内就跳过 travel 直接挖', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 原木悬在头顶 2 格、水平 1.5 格：三维距 ~2.96 ≤ 4.2，站着伸手就够。
  // 旧版必发 travel（还带上行 y 提示）绕路死磕高度；修复后应零跑腿直接挖。
  w.blocks.set(w.key(3, 70, 0), 'oak_log')
  let traveled = false
  const engine = createBehaviorEngine(w.deps)
  const origTravel = engine.travel
  engine.travel = async (...a) => { traveled = true; return origTravel(...a) }
  const msg = await engine.mine({ block: 'log', count: 1, range: 8, timeoutMs: 20_000 })
  assert.match(msg, /完成：挖到 1\/1 个 log/, `实际：${msg}`)
  assert.equal(traveled, false, '已在挖掘半径内不应发 travel')
  assert.equal(w.blocks.get(w.key(3, 70, 0)), undefined, '原木应被挖掉')
})

test('find：只报坐标不动手，别名 log 跨树种命中', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.blocks.set(w.key(4, 70, 0), 'birch_log')   // 桦木：别名 log 必须命中
  w.blocks.set(w.key(6, 71, 2), 'oak_log')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.find({ block: 'log', range: 8, limit: 10, timeoutMs: 15_000 })
  assert.match(msg, /找到 2 处 log/, `实际：${msg}`)
  assert.match(msg, /\(4,70,0\)/, `实际：${msg}`)
  assert.match(msg, /\(6,71,2\)/, `实际：${msg}`)
  // 一块都不许挖掉——勘察不动手
  assert.equal(w.blocks.get(w.key(4, 70, 0)), 'birch_log')
  assert.equal(w.blocks.get(w.key(6, 71, 2)), 'oak_log')
})

test('find：自己站的那列也探——头顶正上方的原木不再漏', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 实机 11-05 盲区：bot 站在树干列里，扫描从 r=1 起永远探不到本列，
  // 头顶两格外就是原木却报「五万格零命中」。
  w.blocks.set(w.key(0, 74, 0), 'oak_log')   // bot 在 (0.5,70,0.5)，正头顶 cy+4
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.find({ block: 'log', range: 6, limit: 10, timeoutMs: 10_000 })
  assert.match(msg, /找到 1 处 log/, `实际：${msg}`)
  assert.match(msg, /\(0,74,0\)/, `实际：${msg}`)
})

test('mine：目标在自己头顶本列——扫得到也挖得到', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.blocks.set(w.key(0, 74, 0), 'spruce_log')  // 正头顶 cy+4，三维距 4.0 ≤ 4.2 免跑腿
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.mine({ block: 'log', count: 1, range: 6, timeoutMs: 20_000 })
  assert.match(msg, /完成：挖到 1\/1 个 log/, `实际：${msg}`)
  assert.equal(w.blocks.get(w.key(0, 74, 0)), undefined, '头顶原木应被挖掉')
})

test('find：扫不到如实报空', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.find({ block: 'log', range: 3, limit: 10, timeoutMs: 10_000 })
  assert.match(msg, /扫不到 log/, `实际：${msg}`)
})

test('mine：范围内没有就如实报告', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.mine({ block: 'diamond_ore', count: 1, range: 3, timeoutMs: 10_000 })
  assert.match(msg, /挖到 0\/1/, `实际：${msg}`)
  assert.match(msg, /扫不到/, `实际：${msg}`)
})

test('followPlayer：寻路跟到玩家身边并保持', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.state.playerPos = [6.5, 70, 0.5]
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.followPlayer({ name: 'Steve', distance: 3, timeoutMs: 2_000 })
  assert.match(msg, /时长到/, `实际：${msg}`)
  const d = Math.hypot(6.5 - w.state.pos[0], 0.5 - w.state.pos[2])
  assert.ok(d <= 3.2, `应保持 3 格内，实际 ${d.toFixed(2)}`)
})

test('followPlayer：玩家不在线连续找不到即终止', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.followPlayer({ name: 'Ghost', timeoutMs: 30_000 })
  assert.match(msg, /终止.*找不到玩家 Ghost/, `实际：${msg}`)
})

test('触发器：受击发事件，auto_retaliate 索敌反击', async () => {
  const w = makeWorld({ health: 20 })
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  engine.start()
  try {
    await new Promise((r) => setTimeout(r, 60))   // 让 tick 建立 prev 基线
    w.state.health = 14                            // 模拟被打
    w.state.zombie = [3.5, 70, 0.5]
    await new Promise((r) => setTimeout(r, 400))  // 等 tick 索敌+反击
    assert.ok(w.state.events.some((e) => e.includes('受击')), '应有受击事件')
    assert.ok(w.state.events.some((e) => e.includes('反击 zombie')), '应有反击事件')
    assert.ok(w.state.attacks > 0, '应真的挥刀')
    assert.ok(
      w.state.rconLog.some((c) => c.startsWith('execute positioned')),
      '索敌必须 execute positioned 到自身坐标（RCON distance 默认锚世界出生点）',
    )
  } finally {
    w.state.zombie = null   // 让 fight 循环下拍即收，不然要等到 25s 死线
    engine.stop()
    await new Promise((r) => setTimeout(r, 100))
  }
})

test('反击：够不着的敌人 5s 放弃进冷却，不再饿死 travel', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 基岩墙挡住追怪路，僵尸在墙后 5.5 格（半径 6 内）
  for (let z = -12; z <= 12; z++) for (let y = 70; y <= 73; y++) w.blocks.set(w.key(3, y, z), 'bedrock')
  w.state.zombie = [5.5, 70, 0.5]
  const engine = createBehaviorEngine(w.deps)
  engine.start()
  try {
    await new Promise((r) => setTimeout(r, 6_500))  // 等 fight 追墙 5s 自动放弃
    assert.ok(w.state.events.some((e) => e.includes('反击放弃')), '应有反击放弃事件')
    // 冷却期内 travel 不再被反击抢占，应一把成行
    const msg = await engine.travel({ x: -8, z: 0, timeoutMs: 15_000 })
    assert.match(msg, /已走到/, `travel 应能成行，实际：${msg}`)
  } finally {
    w.state.zombie = null
    engine.stop()
    await new Promise((r) => setTimeout(r, 100))
  }
})

test('auto_eat：饥饿低于阈值自动进食', async () => {
  const w = makeWorld({ food: 10 })
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  engine.start()
  try {
    await new Promise((r) => setTimeout(r, 300))
    assert.ok(w.state.events.includes('ate'), '应触发进食')
    assert.equal(w.state.food, 20)
  } finally {
    engine.stop()
  }
})

test('guard_health：低血打断 travel 并停手', async () => {
  const w = makeWorld({ health: 20 })
  w.fillPlane(70)
  const engine = createBehaviorEngine(w.deps)
  engine.start()
  try {
    const p = engine.travel({ x: 60, z: 0, timeoutMs: 30_000 })
    await new Promise((r) => setTimeout(r, 30))
    w.state.health = 6   // 掉进护卫线
    const msg = await p
    assert.match(msg, /中断|低血/)
    assert.ok(w.state.pos[0] < 55, '不应走完')
  } finally {
    engine.stop()
  }
})

test('holdBegin 打断 travel（新指令优先）', async () => {
  const w = makeWorld()
  w.fillPlane(30)
  const engine = createBehaviorEngine(w.deps)
  const p = engine.travel({ x: 25, z: 0, timeoutMs: 30_000 })
  await new Promise((r) => setTimeout(r, 50))
  engine.holdBegin()
  const msg = await p
  engine.holdEnd()
  assert.match(msg, /被新指令打断/)
})

test('开关与阈值：setToggle/setThreshold + describe', async () => {
  const w = makeWorld()
  const engine = createBehaviorEngine(w.deps)
  engine.setToggle('auto_retaliate', false)
  assert.equal(engine.cfg.auto_retaliate, false)
  engine.setThreshold('radius', 10)
  assert.equal(engine.cfg.radius, 10)
  const desc = engine.describe()
  assert.match(desc, /auto_retaliate 自动反击：关/)
  assert.match(desc, /半径 10 格/)
  assert.throws(() => engine.setToggle('nope', true), /没有开关/)
})

// ---- 本地世界扫描（Rust 客户端内存区块）替代 RCON 一格一探 ----

test('mine：本地扫描直接命中，不再逐格 RCON', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.blocks.set(w.key(3, 70, 0), 'oak_log')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.mine({ block: 'log', count: 1, range: 12, timeoutMs: 30_000 })
  assert.match(msg, /mine 完成：挖到 1\/1 个 log/)
  assert.ok((w.state.nativeScans ?? 0) >= 1, '应走本地扫描')
  assert.ok(!w.state.rconLog.some((c) => /execute if block (?:-?\d+ ){3}(?:#|oak_log)/.test(c)),
    '扫描阶段不应再逐格探 RCON（验证/寻路预算内允许）')
})

test('find：本地扫描报 2 处树，一枪收工', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.blocks.set(w.key(4, 70, 4), 'oak_log')
  w.blocks.set(w.key(8, 70, 0), 'spruce_log')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.find({ block: 'tree', range: 12, limit: 10, timeoutMs: 20_000 })
  assert.match(msg, /find 找到 2 处/)
  assert.match(msg, /本地世界/)
  assert.equal(w.state.nativeScans, 1)
})

test('find：本地扫描没树如实报空', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.find({ block: 'log', range: 8, limit: 5, timeoutMs: 20_000 })
  assert.match(msg, /扫不到/)
  assert.match(msg, /本地世界/)
})

// ---------------------------------------------------------------------------
// A* 体素寻路段（有 nav 依赖时走这条）
// ---------------------------------------------------------------------------

test('A*：平地直行到达（走 nav 段，不用 RCON 盲探）', async () => {
  const w = makeWorld({ withNav: true })
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000 })
  assert.match(msg, /已走到/)
  assert.ok(Math.hypot(8 - w.state.pos[0], 0 - w.state.pos[2]) < 1.3)
  assert.ok((w.state.navCalls ?? 0) >= 1, '应走过 nav')
})

test('A*：绕开挡路的墙，不挖也能过', async () => {
  const w = makeWorld({ withNav: true })
  w.fillPlane()
  // 一堵 1 格厚的墙（z=-8..8），只能从旁边绕过去
  for (let z = -8; z <= 8; z++) {
    for (let y = 70; y <= 71; y++) w.blocks.set(w.key(4, y, z), 'stone')
  }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000, dig: false })
  assert.match(msg, /已走到/, `实际：${msg}`)
  // 走的是绕路（z 有偏移），不是笔直穿墙
  assert.ok(w.blocks.get(w.key(4, 70, 0)), '墙不该被挖穿（dig=false）')
})

test('A*：挖穿挡路的墙', async () => {
  const w = makeWorld({ withNav: true })
  w.fillPlane()
  // 一堵 1 格厚的墙：绕路比挖贵（A* 挖一格 cost 6，绕 8 格 cost 8）→ 应该挖穿
  for (let z = -8; z <= 8; z++) {
    for (let y = 70; y <= 71; y++) w.blocks.set(w.key(4, y, z), 'dirt')
  }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000, dig: true })
  assert.match(msg, /已走到/, `实际：${msg}`)
  assert.equal(w.blocks.get(w.key(4, 70, 0)), undefined, '脚格应被挖穿')
  assert.equal(w.blocks.get(w.key(4, 71, 0)), undefined, '头格应被挖穿')
})

test('A*：背包没有镐 → 绕开石头墙，不硬挖（实机 11-07 缺陷）', async () => {
  // 背包只有铁剑：石头徒手挖不动。A* 若还规划挖隧道，就会原地刨 24 秒。
  // 正确行为：把石头视为不可挖 → 自动绕路。
  const w = makeWorld({ withNav: true, tools: ['iron_sword'] })
  w.fillPlane()
  for (let z = -8; z <= 8; z++) {
    for (let y = 70; y <= 71; y++) w.blocks.set(w.key(4, y, z), 'stone')
  }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 30_000, dig: true })
  assert.match(msg, /已走到/, `实际：${msg}`)
  assert.ok(w.blocks.get(w.key(4, 70, 0)), '没镐就不该挖石头墙')
  assert.ok(w.blocks.get(w.key(4, 71, 0)), '没镐就不该挖石头墙')
})

test('A*：没镐且无路可绕 → 如实报「缺镐」，不再退回盲探硬刨（实机 11-07 缺陷）', async () => {
  // 石头墙从 z=-14 铺到 z=14（超出 nav 盒），旁边无路可绕：
  // A* 判死路 → 应当报「缺镐」，而不是退回盲探去刨石头（那会白烧 24 秒）。
  const w = makeWorld({ withNav: true, tools: ['iron_sword'] })
  w.fillPlane()
  for (let z = -14; z <= 14; z++) {
    for (let y = 70; y <= 76; y++) w.blocks.set(w.key(4, y, z), 'stone')
  }
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 12_000, dig: true })
  assert.match(msg, /缺镐/, `实际：${msg}`)
  assert.ok(w.blocks.get(w.key(4, 70, 0)), '没镐就不该挖石头墙')
})

test('A*：禁挖遇基岩墙 → 诚实报挡死，不穿墙', async () => {  const w = makeWorld({ withNav: true })
  w.fillPlane()
  for (let z = -12; z <= 12; z++) for (let y = 70; y <= 73; y++) w.blocks.set(w.key(4, y, z), 'bedrock')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.travel({ x: 8, z: 0, timeoutMs: 8_000, dig: false })
  assert.match(msg, /被挡死|超时|遇阻/, `实际：${msg}`)
  assert.ok(w.state.pos[0] < 4, `不应穿过基岩墙，实际位置 ${w.state.pos}`)
})

// ---------------------------------------------------------------------------
// 3D 打印建筑 / 网格勘察
// ---------------------------------------------------------------------------

test('build：house 蓝图逐块放下（先地板后墙，门洞留空）', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.build({ blueprint: 'house', material: 'stone', timeoutMs: 60_000 })
  assert.match(msg, /build 完工/)
  // 地板 25 块
  let floors = 0
  for (let x = 0; x < 5; x++) for (let z = 0; z < 5; z++) if (w.blocks.get(w.key(x + 1, 70, z)) === 'stone') floors++
  assert.equal(floors, 25, `地板应 25 块，实际 ${floors}`)
  // 门洞：plan x=2 → world x=3；门高 2 格（plan y=1,2 → world y=71,72）应为空
  assert.equal(w.blocks.get(w.key(3, 71, 0)), undefined, '门洞下格应留空')
  assert.equal(w.blocks.get(w.key(3, 72, 0)), undefined, '门洞上格应留空')
  assert.equal(w.blocks.get(w.key(3, 70, 0)), 'stone', '门下的地板应保留')
  // 侧墙应实
  assert.equal(w.blocks.get(w.key(1, 70, 0)), 'stone', '侧墙应有方块')
})

test('build：已有方块跳过，不重复放', async () => {
  const w = makeWorld()
  w.fillPlane()
  w.blocks.set(w.key(1, 70, 0), 'stone')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.build({ blueprint: 'platform', material: 'stone', timeoutMs: 60_000 })
  assert.match(msg, /已有 1 块跳过/)
})

test('build：显式 blocks 蓝图按坐标放', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.build({ blocks: '0,0,0:stone;1,0,0:dirt;2,0,0:oak_log', timeoutMs: 60_000 })
  assert.match(msg, /build 完工/)
  assert.equal(w.blocks.get(w.key(1, 70, 0)), 'stone')
  assert.equal(w.blocks.get(w.key(2, 70, 0)), 'dirt')
  assert.equal(w.blocks.get(w.key(3, 70, 0)), 'oak_log')
})

test('build：缺材料如实报（不放成，也不假装成功）', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  // ensureHotbar 在假世界里恒返回 0，不抛——所以这里改测 placeBlock 失败的情形
  w.deps.placeBlock = async () => false
  const engine2 = createBehaviorEngine(w.deps)
  const msg = await engine2.build({ blueprint: 'pillar', material: 'stone', timeoutMs: 30_000 })
  assert.match(msg, /失败/)
  assert.match(msg, /缺材料|失败/)
})

test('survey：体素网格一次扫出树和矿（不走 RCON 逐格）', async () => {
  const w = makeWorld({ withNav: true })
  w.fillPlane()
  w.blocks.set(w.key(5, 71, 3), 'oak_log')
  w.blocks.set(w.key(-4, 69, 6), 'coal_ore')
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.survey({ block: 'tree', radius: 12, limit: 10 })
  assert.match(msg, /survey 找到 1 处/)
  assert.match(msg, /\(5,71,3\)/)
  const msg2 = await engine.survey({ block: 'coal_ore', radius: 12, limit: 10 })
  assert.match(msg2, /survey 找到 1 处/)
})

test('survey：没有目标如实报空', async () => {
  const w = makeWorld({ withNav: true })
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  const msg = await engine.survey({ block: 'diamond_ore', radius: 10, limit: 5 })
  assert.match(msg, /没有/)
})

test('follow_and_mine：采集任务挂链接约束——离跟随目标远就阻塞，近了放行', async () => {
  const w = makeWorld()
  w.fillPlane()
  const engine = createBehaviorEngine(w.deps)
  w.state.playerPos = [50.5, 70, 0.5]
  const { taskIds } = engine.createTaskFromRequest('follow_and_mine', 10, {
    name: '主人', block: 'dirt', count: 2, stayWithin: 8,
  })
  assert.equal(taskIds.length, 2)
  const [followId, mineId] = taskIds
  const follow = engine.scheduler.get(followId)
  const mine = engine.scheduler.get(mineId)
  assert.equal(follow.priority, 10)
  assert.equal(mine.priority, 5)
  assert.ok(mine.constraints, 'mine 任务应带约束')
  // follow 看到的目标在 50 格外 → mine 本拍应被拦
  follow.state.target = [50.5, 70, 0.5]
  let v = await mine.constraints.check(mine, engine.scheduler, [0.5, 70, 0.5])
  assert.equal(v?.blocked, true)
  assert.match(v.reason, /先跟上/)
  // follow 目标回到身边 → 放行
  follow.state.target = [3.5, 70, 1.5]
  v = await mine.constraints.check(mine, engine.scheduler, [0.5, 70, 0.5])
  assert.equal(v, null)
})

test('build_with_materials：清点库存 → 采够泥土 → 自动开工盖完', async () => {
  const w = makeWorld()
  w.fillPlane()
  // 一片露天泥土供采集（9 块的平台要 9 块）——放在远处，别和建造点重叠
  for (let x = 7; x <= 11; x++) for (let z = 4; z <= 8; z++) w.blocks.set(w.key(x, 70, z), 'dirt')
  const inv = { dirt: 0 }
  const origDig = w.deps.digBlock
  w.deps.digBlock = async (x, y, z) => {
    const name = w.blocks.get(w.key(x, y, z))
    const ok = await origDig(x, y, z)
    if (ok && name === 'dirt') inv.dirt += 1
    return ok
  }
  w.deps.inventoryCount = async (n) => inv[n] ?? 0
  const engine = createBehaviorEngine(w.deps)
  const { taskIds } = engine.createTaskFromRequest('build_with_materials', 10, {
    shape: 'platform', material: 'dirt', w: 3, h: 1, d: 3, range: 8, sliceMs: 5000,
  })
  const id = taskIds[0]
  let phases = new Set()
  for (let i = 0; i < 600; i++) {
    await engine.scheduleStep()
    const t = engine.scheduler.get(id)
    if (t) phases.add(t.state.phase)
    if (!t) break
  }
  assert.ok(!engine.scheduler.get(id), '任务应已结束（reap 进历史）')
  const hist = engine.snapshotHistory(10).find((h) => h.id === id)
  assert.ok(hist, '历史里应能查到该任务')
  assert.equal(hist.status, 'done', `状态：${hist.status}｜${hist.note}`)
  assert.match(hist.note, /备料建房完成/, `实际：${hist.note}`)
  assert.ok(inv.dirt >= 9, `应采到 ≥9 块泥土，实际 ${inv.dirt}`)
  assert.ok(phases.has('gather') && phases.has('build'), `阶段应经过 gather/build：${[...phases]}`)
  // 世界泥土总数：原 25 格 → 采走 9 → 盖回 9 = 25；平台盖在采集点附近（建造锚点=当前脚下）
  const dirtTotal = [...w.blocks.entries()].filter(([, n]) => n === 'dirt').length
  assert.equal(dirtTotal, 25, `采走的泥土应全部盖回，实际世界泥土 ${dirtTotal} 格`)
  const around = [...w.blocks.entries()]
    .filter(([k, n]) => n === 'dirt' && Math.abs(Number(k.split(',')[0]) - w.state.pos[0]) < 4
      && Math.abs(Number(k.split(',')[2]) - w.state.pos[2]) < 4).length
  assert.ok(around >= 9, `脚下 4 格内应有 3×3 平台，实际 ${around} 格`)
})
