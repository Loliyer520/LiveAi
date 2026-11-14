/**
 * 行为层引擎（26.2 Rust 后端）——架在动作层原语之上的组合行为。
 *
 * 分层：
 * - 动作层（server.js 既有端点）：走/跳/挖/放/攻击/使用等单步原语。
 * - 行为层（本文件）：
 *   · 任务行为 travel：给一个坐标，内部自动完成走、跳、挖洞、搭路，
 *     规范条件（允许挖什么、用什么搭桥、用什么工具）由调用方传入。
 *   · 常驻行为：auto_retaliate（遇敌自动反击）、auto_eat（饥饿自动进食）、
 *     guard_health（低血停手报警）。开关与阈值持久化在 behaviors.json。
 *   · 触发器：受击（生命下降）、低血、饥饿、死亡，全部转成事件喂给
 *     mc_events，模型读事件后只需下行为级指令，不必一步步遥控。
 *
 * 仲裁：身体同一时刻只归一方。优先级 反击/护卫 > 进食 > travel；
 * 旧式长动作（goto/dig/place/attack/follow/collect 等）经 holdBegin 取得
 * 身体时会打断 travel（新指令优先），hold 期间引擎只感知不动作。
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { decodeGrid, findPath, searchBlocks, standableNear, groundLevel } from './nav.js'
import { Scheduler } from './scheduler.js'

// ── 3D 打印蓝图预设 ────────────────────────────────────────────────────────
// plan(material) 返回 [x,y,z,item]（相对锚点；y=0 是锚点那一层，往上长）。
// 打印顺序由 build 内部按 y 排序（先地板后墙），这里只管形状。
const BUILD_PRESETS = {
  /** 小屋：5×5 实心地板 + 四面墙（留门洞）+ 屋顶 */
  house: (mat) => {
    const cells = []
    const W = 5, D = 5, H = 4
    for (let x = 0; x < W; x++) for (let z = 0; z < D; z++) cells.push([x, 0, z, mat])  // 地板
    for (let y = 1; y <= H - 1; y++) {
      for (let x = 0; x < W; x++) for (let z = 0; z < D; z++) {
        const edge = x === 0 || z === 0 || x === W - 1 || z === D - 1
        if (!edge) continue
        // 正面留门：x=2、z=0，y=1..2 空着
        if (x === 2 && z === 0 && y <= 2) continue
        cells.push([x, y, z, mat])
      }
    }
    for (let x = 0; x < W; x++) for (let z = 0; z < D; z++) cells.push([x, H, z, mat])  // 顶
    return cells
  },
  /** 塔：3×3 柱子高 12 格 */
  tower: (mat) => {
    const cells = []
    for (let y = 0; y < 12; y++) {
      for (let x = 0; x < 3; x++) for (let z = 0; z < 3; z++) {
        const edge = x === 0 || z === 0 || x === 2 || z === 2
        if (edge) cells.push([x, y, z, mat])
      }
    }
    return cells
  },
  /** 平台：7×7 单层地板 */
  platform: (mat) => {
    const cells = []
    for (let x = 0; x < 7; x++) for (let z = 0; z < 7; z++) cells.push([x, 0, z, mat])
    return cells
  },
  /** 柱子：1×1 高 8 格 */
  pillar: (mat) => {
    const cells = []
    for (let y = 0; y < 8; y++) cells.push([0, y, 0, mat])
    return cells
  },
  /** 墙：9×1×5 面墙（带窗洞） */
  wall: (mat) => {
    const cells = []
    for (let y = 0; y < 5; y++) {
      for (let x = 0; x < 9; x++) {
        if (y === 2 && (x === 3 || x === 5)) continue   // 窗
        cells.push([x, y, 0, mat])
      }
    }
    return cells
  },
  /** 拱门：两根柱 + 一道横梁 */
  arch: (mat) => {
    const cells = []
    for (let y = 0; y < 4; y++) { cells.push([0, y, 0, mat]); cells.push([4, y, 0, mat]) }
    for (let x = 0; x <= 4; x++) cells.push([x, 4, 0, mat])
    return cells
  },
}

const HOSTILES = [
  'zombie', 'husk', 'drowned', 'zombie_villager', 'skeleton', 'stray',
  'spider', 'cave_spider', 'creeper', 'witch', 'slime', 'magma_cube',
  'phantom', 'silverfish', 'enderman', 'pillager', 'vindicator',
  'blaze', 'wither_skeleton', 'zombified_piglin', 'ghast', 'hoglin', 'zoglin',
]

const NEVER_DIG = ['bedrock', 'barrier', 'command_block', 'structure_block', 'end_portal_frame']

const DEFAULTS = {
  auto_retaliate: true,   // 遇敌自动反击
  auto_eat: true,         // 饥饿自动进食
  guard_health: true,     // 低血停手报警
  radius: 6,              // 反击索敌半径（格）
  eat_below: 14,          // 饥饿 ≤ 此值触发进食
  guard_below: 8,         // 生命 ≤ 此值触发护卫
}

export function createBehaviorEngine(deps) {
  const {
    rust, rcon, addEvent, log,
    blockIs, blockIsAir,
    look, lookAtPoint, digBlock: digBlockRaw, placeBlock: placeBlockRaw, eatFood,
    ensureHotbar, normalizeItem,
    entityPos,               // (name) => [x,y,z] | null；followPlayer 定位用
    nearbyEntities,          // (radius) => Promise<[{id,name,position,…}]>；调度任务索敌/捡拾用
    attackEntity,            // (id) => Promise；调度任务攻击用
    nativeScan,              // 可选：本地世界扫描（客户端内存区块，毫秒级零网络）
    nav,                     // 可选：体素网格（RLE 体素盒），A* 寻路用
    toolsInInventory,        // 可选：() => Promise<string[]>，背包里的工具名（判断挖得动）
    inventoryCount,          // 可选：(itemName) => Promise<number>，背包里某种物品的数量（材料估算）
    craftItem,               // 可选：(result, times) => Promise<string>，服务端辅助合成（备料建房用）
    statePath,
    tickMs = 500,
    stepMs = 450,            // travel/fight 的动作节拍（测试可加速）
  } = deps
  const S = (ms) => sleep(Math.min(ms, stepMs))
  // 挖/放期间举起 opBusy：tick 里的反击若此时开打，attack up 会把挖掘的按住打断
  // （曾致 travel 挖楼梯 12s 空挖超时——fight 每 600ms 抢一次攻击键）。
  const digBlock = async (...a) => { engine.opBusy += 1; try { return await digBlockRaw(...a) } finally { engine.opBusy -= 1 } }
  const placeBlock = async (...a) => { engine.opBusy += 1; try { return await placeBlockRaw(...a) } finally { engine.opBusy -= 1 } }

  const engine = {
    cfg: { ...DEFAULTS },
    scheduler: new Scheduler(),  // 新的任务调度系统
    schedulerBusy: false,  // 调度器 step 进行中（防 setInterval 重入）
    hold: 0,               // >0 = 旧式长动作正占用身体
    travelTask: null,      // { cancel, note, x, z }
    action: null,          // { kind: 'fight' | 'eat' } 引擎自己的短动作
    opBusy: 0,             // >0 = 挖/放关键段进行中，反击/进食不得抢攻击键
    fightCooldownUntil: 0, // 够不着放弃后的全局反击冷却（受击可破）——「够不着」是位置问题不是类型问题，怪多时按类型冷却照样轮流饿死 travel
    lastHurtAlert: 0,
    lastGuardAlert: 0,
    lastScan: 0,
    prev: { health: null, food: null },
    fighting: '',          // 当前反击目标类型（描述用）
    timer: null,
    stopped: false,
  }

  // ---- 配置持久化 ----
  try {
    if (statePath && existsSync(statePath)) {
      Object.assign(engine.cfg, JSON.parse(readFileSync(statePath, 'utf8')))
    }
  } catch (error) {
    log('[behavior] 配置读取失败，用默认值:', error.message)
  }
  function saveCfg() {
    if (!statePath) return
    try {
      writeFileSync(statePath, JSON.stringify(engine.cfg, null, 2) + '\n', { mode: 0o600 })
    } catch (error) {
      log('[behavior] 配置写盘失败:', error.message)
    }
  }

  function snap() {
    return rust.snapshot
  }
  function myPos() {
    const p = snap()?.position
    return p && p.every(Number.isFinite) ? p : null
  }

  // ---- 触发器：受击 / 低血 / 饥饿（只发事件，不动身体）----
  function triggers(now) {
    const s = snap()
    if (!s) return { hurt: false }
    const health = Number(s.health ?? 20)
    const food = Number(s.food ?? 20)
    const hurt = engine.prev.health !== null && engine.prev.health - health >= 1
    if (hurt && now - engine.lastHurtAlert > 3_000) {
      engine.lastHurtAlert = now
      addEvent('behavior', `受击：生命 ${engine.prev.health} → ${health}`)
    }
    if (health > 0 && health <= engine.cfg.guard_below && now - engine.lastGuardAlert > 30_000) {
      engine.lastGuardAlert = now
      addEvent('behavior', `⚠ 低血警报：生命 ${health}/20 ≤ ${engine.cfg.guard_below}，已停手待援（guard_health）`)
    }
    engine.prev = { health, food }
    return { hurt }
  }

  // ---- 索敌：常见敌对生物，逐类型取最近一只 ----
  // 注意：RCON 的 distance 以执行位置为锚（默认世界出生点），
  // 必须 execute positioned 到自身坐标再选，否则查到的是出生点周边。
  async function findThreat(type) {
    const me = myPos()
    if (!me) return null
    const at = `execute positioned ${me[0].toFixed(1)} ${me[1].toFixed(1)} ${me[2].toFixed(1)} run `
    const types = type ? [type] : HOSTILES
    for (const t of types) {
      try {
        const out = await rcon(
          `${at}data get entity @e[type=minecraft:${t},distance=..${engine.cfg.radius + 4},sort=nearest,limit=1] Pos`,
        )
        const m = out.match(/\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]/)
        if (m) return { type: t, pos: [Number(m[1]), Number(m[2]), Number(m[3])] }
      } catch {}
    }
    return null
  }

  // ---- 常驻行为：反击 ----
  async function fight(threat) {
    if (engine.action) return
    engine.action = { kind: 'fight' }
    engine.fighting = threat.type
    addEvent('behavior', `反击 ${threat.type}（auto_retaliate）`)
    try {
      const deadline = Date.now() + 25_000
      let bestDist = Infinity
      let bestAt = Date.now()
      while (Date.now() < deadline && !engine.stopped) {
        const t = await findThreat(threat.type)
        if (!t) {
          addEvent('behavior', `威胁解除：${threat.type} 已消失/死亡`)
          break
        }
        const me = myPos()
        if (!me) break
        const dist = Math.hypot(t.pos[0] - me[0], t.pos[2] - me[2])
        const health = Number(snap()?.health ?? 20)
        if (health <= engine.cfg.guard_below) break  // 护卫阈值，停手
        if (dist < bestDist - 0.5) { bestDist = dist; bestAt = Date.now() }
        if (Date.now() - bestAt > 5_000) {
          // 追 5s 距离没缩短=够不着（怪在坑上/墙后）——放弃并进全局冷却，别饿着 travel 空转
          addEvent('behavior', `反击放弃：${threat.type} 够不着（距 ${dist.toFixed(1)} 格，45s 内不再主动追，挨打仍会还手）`)
          engine.fightCooldownUntil = Date.now() + 45_000
          break
        }
        if (dist > 2.8) {
          await look(Math.atan2(-(t.pos[0] - me[0]), t.pos[2] - me[2]), 0)
          await rust.request('move', { buttons: 1, look: [0, 0] })
          await S(350)
        } else {
          await rust.request('move', { buttons: 0, look: [0, 0] })
          await lookAtPoint(t.pos[0], t.pos[1] + 0.9, t.pos[2])
          await rust.request('attack', { down: true })
          await S(320)
          await rust.request('attack', { down: false })
          await S(280)
        }
      }
    } catch (error) {
      addEvent('behavior', `反击中断：${error.message}`)
    } finally {
      engine.fighting = ''
      engine.action = null
      await rust.request('move', { buttons: 0, look: [0, 0] }).catch(() => {})
    }
  }

  // ---- 常驻行为：进食 ----
  async function eat() {
    if (engine.action) return
    engine.action = { kind: 'eat' }
    try {
      const message = await eatFood()
      engine.eatFailUntil = 0
      addEvent('behavior', `自动进食：${message}`)
    } catch (error) {
      // 失败（多是没吃的/快照没刷新）冷却 30s 再试，别半秒刷一条事件
      engine.eatFailUntil = Date.now() + 30_000
      addEvent('behavior', `自动进食失败：${error.message}（30s 后重试）`)
    } finally {
      engine.action = null
    }
  }

  // ---- 主循环 ----
  async function tick() {
    if (!rust.active) return
    const now = Date.now()
    const { hurt } = triggers(now)
    const s = snap()
    if (!s?.position) return
    const health = Number(s.health ?? 20)
    const food = Number(s.food ?? 20)
    // 动态优先级：系统状态临时抬高调度任务的优先级（下一拍生效）。
    //   受击/低血 → 反击类拉到 100（紧急）；饿了 → 进食类拉到 75（重要）。
    // 事件过后回落到创建时的 basePriority。
    if (engine.scheduler.tasks.size) {
      for (const t of engine.scheduler.list()) {
        if (t.type === 'retaliate' || t.type === 'auto_retaliate') {
          t.priority = (hurt || (health > 0 && health <= engine.cfg.guard_below)) ? 100 : t.basePriority
        } else if (t.type === 'eat') {
          t.priority = food <= engine.cfg.eat_below ? 75 : t.basePriority
        }
      }
    }
    // 护卫最优先：低血时停掉赶路（反击循环里也自查阈值），哪怕挖到一半也得停
    if (engine.cfg.guard_health && health <= engine.cfg.guard_below && health > 0) {
      if (engine.travelTask) {
        engine.travelTask.cancel = true
        engine.travelTask.note = `低血护卫打断（生命 ${health}）`
      }
      await rust.request('move', { buttons: 0, look: [0, 0] }).catch(() => {})
      return
    }
    if (engine.hold > 0 || engine.action || engine.opBusy > 0) return  // 身体被占用/关键段，只感知
    // 反击：受击立即索敌，否则每 2.5s 巡一次；够不着的敌人进冷却（受击可破冷却）
    if (engine.cfg.auto_retaliate && (hurt || now - engine.lastScan > 2_500)) {
      engine.lastScan = now
      const threat = await findThreat()
      if (threat) {
        const me = myPos()
        const d = me ? Math.hypot(threat.pos[0] - me[0], threat.pos[2] - me[2]) : 99
        const cooled = !hurt && now < engine.fightCooldownUntil
        if (d <= engine.cfg.radius && !cooled) {
          await fight(threat)
          return
        }
      }
    }
    // 进食：无战斗且饥饿低（失败冷却期内不烦）
    if (engine.cfg.auto_eat && food <= engine.cfg.eat_below && !engine.fighting && now >= (engine.eatFailUntil ?? 0)) {
      await eat()
    }
    // ── 调度器任务：身体空闲时推进一拍（高优先级先试，idle 任务让位给低优先级）──
    // 常驻反击/进食已在上方处理；这里跑用户通过 /schedule/create 挂的通用任务。
    if (engine.hold === 0 && !engine.action && engine.opBusy === 0 && !engine.travelTask
      && !engine.schedulerBusy && engine.scheduler.tasks.size > 0) {
      engine.schedulerBusy = true
      try {
        await engine.scheduler.step(myPos())
      } catch (error) {
        log('[scheduler] step 失败:', error.message)
      } finally {
        engine.schedulerBusy = false
      }
    }
  }

  engine.start = () => {
    if (engine.timer) return
    engine.stopped = false
    engine.timer = setInterval(() => { tick().catch((e) => log('[behavior] tick:', e.message)) }, tickMs)
    engine.timer.unref?.()
  }
  engine.stop = () => {
    engine.stopped = true
    if (engine.timer) clearInterval(engine.timer)
    engine.timer = null
  }

  // ---- 外部动作占用身体（新指令优先：打断 travel）----
  engine.holdBegin = () => {
    engine.hold += 1
    engine.interruptTravel('被新指令打断')
  }
  engine.holdEnd = () => {
    engine.hold = Math.max(0, engine.hold - 1)
  }
  engine.interruptTravel = (why) => {
    if (engine.travelTask) {
      engine.travelTask.cancel = true
      engine.travelTask.note = why
    }
  }
  engine.cancelAll = (why) => {
    engine.interruptTravel(why)
  }

  // ---- 工具闸门（engine 级，travel/mine 共用）----
  // 背包里没有镐就别让 A* 规划挖石头隧道——那会两轮 12s 挖不破、白烧 24 秒还卡在原地
  // （实机 11-07：背包只有铁剑，A* 却规划挖石头隧道，bot 原地刨了两轮）。
  // 正确行为是绕开：A* 的 canDig 返回 false 就自动走别的线。
  let toolCache = null
  const availableTools = async () => {
    if (!toolsInInventory) return null            // 没有这个依赖 → 不做限制（保持旧行为）
    if (toolCache && Date.now() - toolCache.at < 15_000) return toolCache.list
    try {
      const list = await toolsInInventory()
      toolCache = { list, at: Date.now() }
      return list
    } catch (error) {
      log('[tool] 读背包工具失败:', error.message)
      return null
    }
  }
  // 方块 → 需要哪类工具（null = 徒手就能挖，比如泥土/木头/树叶）
  const toolClassOf = (n) => {
    if (!n || n === 'air' || n === 'cave_air' || n === 'void_air') return null
    // 泥土类（锹可挖，徒手也能挖慢一点）——石头类才真正必须要镐
    if (/(dirt|sand|gravel|clay|snow|soul_sand|mycelium|podzol|grass_path|grass_block|farmland)/.test(n)) return 'shovel'
    if (/(stone|cobble|deepslate|ore|obsidian|ancient_debris|bedrock|stone_|concrete|terracotta)/.test(n)) return 'pickaxe'
    if (/(log|wood|planks|stem|bamboo|crafting_table|chest)/.test(n)) return 'axe'
    if (/(leaves|azalea)/.test(n)) return 'axe'
    return null
  }
  const hasToolFor = (list, cls) => {
    if (!cls) return true
    if (!list) return true
    return list.some((t) => t.endsWith(`_${cls}`))
  }
  // 硬门槛：只有「必须镐」的石族才算真缺工具。泥土/木头徒手能挖（只是慢），
  // 不能因为没锹就判死路（实机 11-07 背包只有铁剑，缺的是镐不是锹）。
  const TOOL_HARD = { pickaxe: true }
  const TOOL_CN = { pickaxe: '镐', axe: '斧', shovel: '锹', hoe: '锄' }

  // ---- 开关与阈值 ----
  engine.setToggle = (name, on) => {
    if (!(name in DEFAULTS) || typeof DEFAULTS[name] !== 'boolean') {
      throw new Error(`没有开关 ${name}（现有：auto_retaliate / auto_eat / guard_health）`)
    }
    engine.cfg[name] = Boolean(on)
    saveCfg()
    addEvent('behavior', `常驻行为 ${name} → ${on ? '开' : '关'}`)
  }
  engine.setThreshold = (key, value) => {
    if (!['radius', 'eat_below', 'guard_below'].includes(key)) {
      throw new Error('可调的阈值：radius（索敌半径）/ eat_below（饥饿线）/ guard_below（低血线）')
    }
    engine.cfg[key] = value
    saveCfg()
  }
  engine.describe = () => {
    const t = engine.travelTask
    const lines = [
      `auto_retaliate 自动反击：${engine.cfg.auto_retaliate ? '开' : '关'}（半径 ${engine.cfg.radius} 格${engine.fighting ? `，反击中：${engine.fighting}` : ''}）`,
      `auto_eat 自动进食：${engine.cfg.auto_eat ? '开' : '关'}（饥饿 ≤${engine.cfg.eat_below} 触发）`,
      `guard_health 低血护卫：${engine.cfg.guard_health ? '开' : '关'}（生命 ≤${engine.cfg.guard_below} 停手报警）`,
      `当前任务：${t ? `travel → (${t.x}, ${t.z})${t.note ? `｜${t.note}` : ''}` : '无'}`,
    ]
    return lines.join('\n')
  }

  // ---- 任务行为：travel（寻路 + 挖洞 + 搭路一体）----
  function parseDigSpec(raw) {
    // true=任意（床岩类除外）；false=禁挖；逗号清单=只挖这些
    if (raw === false) return { any: false, list: null }
    if (Array.isArray(raw)) return { any: false, list: raw }
    return { any: true, list: null }
  }

  async function diggable(spec, x, y, z) {
    if (!spec.any && !spec.list) return false
    if (spec.list) {
      for (const name of spec.list) {
        if (await blockIs(x, y, z, `minecraft:${name}`)) return true
      }
      return false
    }
    for (const name of NEVER_DIG) {
      if (await blockIs(x, y, z, `minecraft:${name}`)) return false
    }
    return true
  }

  // 目标别名（mine/find 共用）：多目标逗号分隔；# 开头的方块标签原样用
  // （execute if block 支持 #minecraft:logs）；模型要「树/木头」时给的是概念
  // 不是具体 id，单探 oak_log 在桦木/云杉林里永远扫不到（实机踩过：r=32 三次全空）。
  const BLOCK_ALIASES = {
    log: '#minecraft:logs', logs: '#minecraft:logs', wood: '#minecraft:logs', tree: '#minecraft:logs',
    原木: '#minecraft:logs', 木头: '#minecraft:logs', 树: '#minecraft:logs',
    leaves: '#minecraft:leaves', 树叶: '#minecraft:leaves',
  }
  function buildTargets(raw) {
    const label = String(raw).trim()
    const targets = [...new Set(label.split(',').map((s) => {
      const t = normalizeItem(s.trim())
      if (!t) return null
      return t.startsWith('#') ? t : (BLOCK_ALIASES[t] ?? t)
    }).filter(Boolean))]
    return { label, targets }
  }

  // 方块标签 → 具体方块名（本地扫描 op 只认具体名，RCON 的 execute if block
  // 才支持标签）。没登记的标签 → 返回 null，走 RCON 慢路径兜底。
  const TAG_NAMES = {
    '#minecraft:logs': [
      'oak_log', 'spruce_log', 'birch_log', 'jungle_log', 'acacia_log', 'dark_oak_log',
      'mangrove_log', 'cherry_log', 'pale_oak_log', 'bamboo_block',
      'crimson_stem', 'warped_stem',
    ],
    '#minecraft:leaves': [
      'oak_leaves', 'spruce_leaves', 'birch_leaves', 'jungle_leaves', 'acacia_leaves',
      'dark_oak_leaves', 'mangrove_leaves', 'cherry_leaves', 'azalea_leaves',
      'flowering_azalea_leaves', 'pale_oak_leaves',
    ],
  }
  function nativeNames(targets) {
    const names = []
    for (const t of targets) {
      if (!t.startsWith('#')) { names.push(t); continue }
      const list = TAG_NAMES[t]
      if (!list) return null
      names.push(...list)
    }
    return [...new Set(names)]
  }

  /**
   * travel：走到 (x, z)。opts:
   *  - y：可选目标高度。给出后成为到点硬条件（高度差 >1.6 不算到达）；明显高于当前时自动上行——
   *      实壁挖楼梯（只挖头格+头顶格、留脚下方块当台阶，借 step 跳顶逐列爬升），
   *      禁挖/开阔坑且给了 bridge 则垫块上拔（跳起往脚下塞方块）
   *  - timeoutMs：总时限
   *  - dig：true=随意挖（床岩类除外）｜false=禁挖｜['dirt','stone',…]=只挖清单
   *  - bridge：搭桥方块名（如 dirt）；不给则遇深沟/悬崖即停
   *  - tool：挖掘前确保拿在手里的工具名（可选）
   */
  engine.travel = async (opts) => {
    const { x, z, y = null, timeoutMs = 120_000, dig = true, bridge = null, tool = null } = opts
    const digSpec = parseDigSpec(dig)
    const bridgeItem = bridge ? normalizeItem(bridge === true ? 'dirt' : bridge) : null
    if (bridgeItem) await ensureHotbar(bridgeItem)  // 没有就直接抛错，别走到崖边才发现
    if (engine.travelTask) engine.interruptTravel('被新 travel 替换')
    const task = { cancel: false, note: '', x, z }
    engine.travelTask = task
    addEvent('behavior', `travel 出发 → (${x}, ${z})${y !== null ? `, 高度 ${y}` : ''}${digSpec.any ? '' : digSpec.list ? `，只挖 ${digSpec.list.join('/')}` : '，禁挖'}${bridgeItem ? `，备桥 ${bridgeItem}` : ''}`)
    const deadline = Date.now() + timeoutMs
    let lastPos = null
    let lastProgress = Date.now()
    let sidestepUntil = 0
    let sidestepSign = 1
    let pillarFails = 0
    let lastLookYaw = null  // 视角死带用：上一拍实际发出的朝向
    // 连挖不破计数（按格）：同一格两轮 12s 都挖不破，继续重试只会把整段行程
    // 空烧在原地（实机 11-05：山体内石楼梯三轮 false，25s 的腿全耗在重挖同一格）。
    // 任何一次挖成/位移即清零；同一格计满两轮 → 如实报阻，让上层换目标/换路。
    const digFails = new Map()
    const noteDigFail = (key) => {
      const n = (digFails.get(key) ?? 0) + 1
      digFails.set(key, n)
      return n
    }
    // 垫块上拔一格：跳起往脚下塞桥方块。返回 true=塞上了。
    const pillarUp = async (px, py, pz) => {
      const bx = Math.floor(px)
      const by = Math.floor(py)
      const bz = Math.floor(pz)
      await rust.request('move', { buttons: 0, look: [0, 0] })
      const slot = await ensureHotbar(bridgeItem)
      await rust.request('select', { slot })
      await rust.request('move', { buttons: 16, look: [0, 0] })
      await S(160)
      try {
        await placeBlock(bx, by, bz, bridgeItem, { approach: false })
      } catch (error) {
        log(`[travel] 垫块 (${bx},${by},${bz}) 失败:`, error.message)
      }
      await rust.request('move', { buttons: 0, look: [0, 0] })
      await S(400)
      return !(await blockIsAir(bx, by, bz))
    }

    // ---- 体素 A* 段 ----------------------------------------------------
    // 有 nav 依赖时先走 A*：一次取整盒体素，JS 侧规划，绝不盲探。
    // 任何一步不成立（拿不到网格/走不通/被取消）都如实退回下面的老循环，
    // 老循环保持原样（RCON 盲探那条路继续兜底），两边语义不打架。
    const pickTool = (blockName) => {
      const n = String(blockName ?? '').replace(/^minecraft:/, '')
      if (/(stone|cobble|deepslate|ore|obsidian|ancient_debris|bedrock|stone_)/.test(n)) return 'iron_pickaxe'
      if (/(dirt|sand|gravel|clay|snow|soul_sand|mycelium|podzol)/.test(n)) return 'iron_shovel'
      if (/(log|wood|planks|stem|bamboo)/.test(n)) return 'iron_axe'
      if (/(leaves|azalea)/.test(n)) return 'iron_axe'
      return null
    }
    const bare = (n) => String(n ?? '').replace(/^minecraft:/, '')
    // 按方块名判「能不能挖」——零 RCON，网格里就有名字。
    const canDigName = (name) => {
      if (!digSpec.any && !digSpec.list) return false
      const n = bare(name)
      if (!n) return false
      if (digSpec.list) {
        return digSpec.list.some((t) => bare(t) === n)
      }
      return !NEVER_DIG.includes(n)
    }
    // 点动走到某格的中心（不用 teleport，一步一步压上去）。
    const walkTo = async (tx, tz, { jump = false } = {}) => {
      const until = Date.now() + 9_000
      let lastYaw = null
      for (;;) {
        if (task.cancel) return { stop: 'cancel' }
        if (Date.now() > deadline) return { stop: 'timeout' }
        const s = await rust.request('status')
        const me = s.position ?? []
        if (!me.every(Number.isFinite)) return { stop: 'lost' }
        const [cx, cy, cz] = me
        const ddx = tx - cx
        const ddz = tz - cz
        const d = Math.hypot(ddx, ddz)
        if (d < 0.45) return { ok: true }
        if (Date.now() > until) return { stop: 'stuck' }
        // 反击/进食占身时原地等，别和对方抢按键
        if (engine.action) { await S(400); continue }
        const yaw = Math.atan2(-ddx, ddz)
        const diff = lastYaw === null ? Infinity
          : Math.abs(((yaw - lastYaw + Math.PI * 3) % (Math.PI * 2)) - Math.PI)
        if (diff > 0.08) {
          try { await look(yaw, 0); lastYaw = yaw } catch (error) { log('[travel] look 失败:', error.message) }
        }
        const food = Number(s.food ?? 20)
        const sprint = food > 6 ? 64 : 0
        const buttons = 1 | sprint | (jump ? 16 : 0)
        await rust.request('move', { buttons, look: [0, 0] })
        await S(jump ? 190 : 150)
        await rust.request('move', { buttons: 0, look: [0, 0] })
        await S(jump ? 70 : 110)
      }
    }
    // 一格挖开：先按方块名挑合适的工具（没有就徒手，别瞎报错）。
    const digCell = async (bx, by, bz) => {
      await rust.request('move', { buttons: 0, look: [0, 0] })
      const want = tool ? normalizeItem(tool) : pickTool(bestName(bx, by, bz))
      if (want) {
        try {
          const slot = await ensureHotbar(want)
          await rust.request('select', { slot })
        } catch (error) {
          // 背包里没有这个工具：石族徒手挖不动（实机 11-07：只有铁剑，
          // A* 规划挖石头隧道，两轮 12s 挖不破，bot 原地刨了 24 秒）。
          // 这时如实报「缺工具」，让上层换路/换目标，而不是白烧时限。
          // 斧/锹类没有只是慢（徒手也能挖），不因此判死路。
          const name = bare(bestName(bx, by, bz))
          const cls = toolClassOf(name)
          if (cls && TOOL_HARD[cls]) {
            task.note = `缺${TOOL_CN[cls]}（挖 ${name}）`
            log(`[travel] 背包里没有 ${want}，挖不动 ${name}（${bx},${by},${bz}）`)
            return false
          }
          log(`[travel] 没有 ${want}，徒手挖 (${bx},${by},${bz})`)
        }
      }
      task.note = `挖 (${bx},${by},${bz})`
      const dug = await digBlock(bx, by, bz, 12_000)
      log(`[travel] 挖 (${bx},${by},${bz}) 结果=${dug}`)
      return dug
    }
    let bestName = () => null
    // A* 主段：规划 → 执行 → 遇挖/搭桥就重新规划（网格已变）。
    const navTry = async () => {
      if (!nav) return null
      for (let round = 0; round < 12; round++) {
        const s = await rust.request('status')
        const me = s.position ?? []
        if (!me.every(Number.isFinite)) return null
        const [px, py, pz] = me
        const dist = Math.hypot(x - px, z - pz)
        if (dist < 1.3 && (y === null || Math.abs(py - y) < 1.6)) {
          addEvent('behavior', `travel 到达 (${px.toFixed(1)}, ${pz.toFixed(1)})`)
          return `已走到 (${px.toFixed(1)}, ${pz.toFixed(1)})${y !== null ? `，高度 ${py.toFixed(1)}` : ''}。`
        }
        if (task.cancel) {
          return `travel 中断：${task.note || '被取消'}（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
        }
        if (Date.now() > deadline) {
          return `travel 超时：停在 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标 ${dist.toFixed(1)} 格${task.note ? `，最后障碍：${task.note}` : ''}。`
        }
        let grid
        try {
          const payload = await nav({ radius: 16, dyMin: -8, dyMax: 8 })
          grid = decodeGrid(payload)
        } catch (error) {
          log('[travel] nav 网格取不到，退回盲探走法:', error.message)
          return null
        }
        bestName = (bx, by, bz) => grid.name(bx, by, bz)
        // 工具闸门：背包里没有镐就别规划挖石头隧道——A* 会自动改走绕路的线。
        // 只对「必须镐」的石族设闸（徒手挖不动的）；斧/锹类徒手也能挖，只是慢。
        const tools = await availableTools()
        const digGate = tools
          ? (name) => {
              if (!canDigName(name)) return false
              const cls = toolClassOf(bare(name))
              return !(cls && TOOL_HARD[cls] && !hasToolFor(tools, cls))
            }
          : canDigName
        const start = { x: Math.floor(px), y: Math.floor(py), z: Math.floor(pz) }
        const plan = findPath(grid, start, { x, z, y }, {
          dig: !(!digSpec.any && !digSpec.list),
          bridge: !!bridgeItem,
          canDig: digGate,
        })
        if (!plan.reached) {
          // A* 判死路时不要再退回盲探去硬挖（实机 11-07：A* 已因「没镐」正确判死路，
          // 盲探兜底却又去刨石头，两轮 24 秒原地不动）。
          // 这里区分两种死路：工具不足 → 如实报缺工具；地形真的走不通 → 如实报被挡死。
          // 缺的到底是哪种工具，从网格里**实际挡路**的方块统计（不硬编码）。
          const toolsNow = tools ?? await availableTools()
          const lack = new Set()
          if (toolsNow) {
            for (const entry of grid.palette ?? []) {
              const n = bare(Array.isArray(entry) ? entry[0] : entry)
              if (!n) continue
              const cls = toolClassOf(n)
              if (!cls || !TOOL_HARD[cls]) continue
              if (hasToolFor(toolsNow, cls)) continue
              if (!digSpec.list && NEVER_DIG.includes(n)) continue
              if (digSpec.list && !digSpec.list.some((t) => bare(t) === n)) continue
              lack.add(TOOL_CN[cls])
            }
          }
          const lackText = [...lack].join('/')
          log(`[travel] A* 没找到路（${plan.reason ?? '无'}）${lackText ? `，缺${lackText}` : ''}`)
          // 目标在头顶且背包有垫块：A* 只会跳 1 格/挖楼梯，爬不上多格高差。
          // 交回下面的老循环，用「垫块直拔」一格一格顶上去（实机 11-08：掉落物在
          // 4 格高崖上，A* 判缺镐死路，其实垫土就能上去）。
          if (bridgeItem && y !== null && y > py + 1.2) {
            log('[travel] A* 无路但可垫块上行，转盲探上行')
            return null
          }
          if (lackText && digSpec.any) {
            return `travel 遇阻：缺${lackText}（背包里没有${lackText}），挖不动挡路的东西，停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。换把${lackText}再来，或换个目标。`
          }
          return null
        }
        if (!plan.path.length) {
          return `已走到 (${px.toFixed(1)}, ${pz.toFixed(1)})${y !== null ? `，高度 ${py.toFixed(1)}` : ''}。`
        }
        let replan = false
        // 直行段合并：连续的同向 walk/drop 步合成一个远端目标（一步点动走过去），
        // 挖/搭桥/跳的步保留原样单独执行。nav.js 的 simplify 并不合并，
        // 这里自己按段合并，否则每格都要 260ms 点动一次，一段 40 格要 10 秒。
        // 严格只合并「同方向」的连续步：方向一变就断开，否则斜着插值正好
        // 从墙角切过去（实测穿墙 + 掉沟）。
        const segs = []
        for (let i = 0; i < plan.path.length; i++) {
          const step = plan.path[i]
          const prev = plan.path[i - 1]
          const prevStep = segs[segs.length - 1]
          const plain = step.act === 'walk' || step.act === 'drop'
          // 本步方向 = 与路径上一格的差（不是与段末的差）
          const dirX = prev ? step.x - prev.x : 0
          const dirZ = prev ? step.z - prev.z : 0
          // 同向才合并：方向一变就断开，否则斜着插值正好从墙角切过去（实测穿墙 + 掉沟）
          const sameDir = plain && prevStep && prevStep.plain
            && prevStep.dirX === dirX && prevStep.dirZ === dirZ
          if (sameDir) {
            prevStep.x = step.x
            prevStep.y = step.y
            prevStep.z = step.z
            prevStep.steps++
            continue
          }
          segs.push({ ...step, plain, steps: 1, dirX, dirZ })
        }
        for (const step of segs) {
          if (task.cancel) {
            return `travel 中断：${task.note || '被取消'}（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
          }
          if (Date.now() > deadline) {
            return `travel 超时：停在 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标 ${dist.toFixed(1)} 格${task.note ? `，最后障碍：${task.note}` : ''}。`
          }
          if (step.act === 'dig' || step.act === 'dig2') {
            for (const [bx, by, bz] of (step.dig ?? [])) {
              if (await digCell(bx, by, bz)) continue
              const why = task.note?.startsWith('缺') ? `（${task.note}）` : '（太硬或工具不对）'
              return `travel 遇阻：挖 (${bx},${by},${bz}) 两轮不破${why}，停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。`
            }
            replan = true
            break
          }
          if (step.act === 'bridge') {
            await rust.request('move', { buttons: 0, look: [0, 0] })
            const slot = await ensureHotbar(bridgeItem)
            await rust.request('select', { slot })
            const by = step.y - 1
            task.note = `搭桥 (${step.x},${by},${step.z})`
            try {
              await placeBlock(step.x, by, step.z, bridgeItem, { approach: false })
            } catch (error) {
              log(`[travel] 搭桥 (${step.x},${by},${step.z}) 失败:`, error.message)
            }
            await S(400)
            if (await blockIsAir(step.x, by, step.z)) {
              return `travel 遇阻：搭桥 (${step.x},${by},${step.z}) 放不下（悬空无附着面），停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。`
            }
            replan = true
            break
          }
          const r = await walkTo(step.x + 0.5, step.z + 0.5, { jump: step.act === 'jump' })
          if (!r.ok) {
            if (r.stop === 'cancel') {
              return `travel 中断：${task.note || '被取消'}（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
            }
            if (r.stop === 'timeout') {
              return `travel 超时：停在 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标 ${dist.toFixed(1)} 格。`
            }
            // 走不到位：重新规划（可能格子已变），规划不出来就退回盲探
            replan = true
            break
          }
        }
        if (!replan) {
          // 路径走完却没到点：回到规划循环再核一次
          continue
        }
      }
      return null
    }
    try {
      const navResult = await navTry()
      if (navResult !== null) return navResult
      for (;;) {
        const s = await rust.request('status')
        const me = s.position ?? []
        if (!me.every(Number.isFinite)) throw new Error('还读不到当前位置')
        const [px, py, pz] = me
        const dx = x - px
        const dz = z - pz
        const dist = Math.hypot(dx, dz)
        const wantUp = y !== null && y > py + 1.2  // 目标明显更高：进入上行模式（挖楼梯/垫块）
        if (dist < 1.3 && (y === null || Math.abs(py - y) < 1.6)) {
          addEvent('behavior', `travel 到达 (${px.toFixed(1)}, ${pz.toFixed(1)})`)
          return `已走到 (${px.toFixed(1)}, ${pz.toFixed(1)})${y !== null ? `，高度 ${py.toFixed(1)}` : ''}。`
        }
        // 水平已到柱位但高度差够不着（目标在脚下深处）：继续走只会在目标点
        // 前后左右抽搐到超时，如实报出来交给调用方决策（垫下去/换目标）。
        if (dist < 1.3 && y !== null && !wantUp && Math.abs(py - y) >= 1.6) {
          return `travel 到点但高度不符：水平距目标 ${dist.toFixed(1)} 格，目标高度 ${y} 比当前 ${py.toFixed(1)} 低 ${(py - y).toFixed(1)}，站在柱位上下不去。`
        }
        if (task.cancel) {
          return `travel 中断：${task.note || '被取消'}（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
        }
        if (Date.now() > deadline) {
          return `travel 超时：停在 (${px.toFixed(1)}, ${pz.toFixed(1)})，距目标 ${dist.toFixed(1)} 格${task.note ? `，最后障碍：${task.note}` : ''}。`
        }
        // 反击/进食进行中：原地等它结束再继续
        if (engine.action) {
          await S(400)
          continue
        }
        // 进度跟踪：2.2s 没挪出 0.35 格判卡死
        if (lastPos && Math.hypot(px - lastPos[0], pz - lastPos[1]) > 0.35) lastProgress = Date.now()
        if (!lastPos) lastProgress = Date.now()
        lastPos = [px, pz]
        const stuck = Date.now() - lastProgress > 2_200

        // 车道中线：明显沿一轴行进时，另一轴瞄准当前格中心。
        // 贴方块边界走会让碰撞箱蹭进邻格墙，纯轴向移动被整个吃掉（楔死）。
        let ax = x
        let az = z
        if (dist > 2) {
          if (Math.abs(dx) > Math.abs(dz) * 2.5) az = Math.floor(pz) + 0.5
          else if (Math.abs(dz) > Math.abs(dx) * 2.5) ax = Math.floor(px) + 0.5
        }
        const adx = ax - px
        const adz = az - pz
        const adist = Math.hypot(adx, adz) || 1
        const dirx = adx / adist
        const dirz = adz / adist
        let yaw = Math.atan2(-adx, adz)
        if (Date.now() < sidestepUntil) yaw += sidestepSign * Math.PI / 2
        // 视角死带：朝向变化 <~4.6° 不重发 look。每拍都拿浮点坐标重算 atan2
        // 再照发，人眼看就是边走边左右小抽搐；车道中线模式临界翻转同理。
        const yawDiff = lastLookYaw === null ? Infinity
          : Math.abs(((yaw - lastLookYaw + Math.PI * 3) % (Math.PI * 2)) - Math.PI)
        if (yawDiff > 0.08 || Date.now() < sidestepUntil) {
          await look(yaw, 0)
          lastLookYaw = yaw
        }

        const fx = Math.floor(px + dirx * 0.95)
        const fz = Math.floor(pz + dirz * 0.95)
        const fy = Math.floor(py)
        const ownBlock = fx === Math.floor(px) && fz === Math.floor(pz)
        const feetSolid = !ownBlock && !(await blockIsAir(fx, fy, fz))
        const headSolid = !ownBlock && !(await blockIsAir(fx, fy + 1, fz))
        if (feetSolid || headSolid || stuck) {
          log(`[travel] pos=(${px.toFixed(1)},${py.toFixed(1)},${pz.toFixed(1)}) probe=(${fx},${fy},${fz}) feet=${feetSolid} head=${headSolid} stuck=${stuck} dist=${dist.toFixed(1)}`)
        }

        // 目标就在头顶（水平已近但高度差大）：开阔头顶下直接垫块直拔。
        if (wantUp && bridgeItem && dist < 1.8
          && await blockIsAir(Math.floor(px), fy + 1, Math.floor(pz))
          && await blockIsAir(Math.floor(px), fy + 2, Math.floor(pz))) {
          task.note = '垫块直拔'
          if (await pillarUp(px, py, pz)) {
            pillarFails = 0
            lastProgress = Date.now()
          } else {
            pillarFails += 1
            if (pillarFails >= 3) {
              return `travel 遇阻：目标在上方但三次垫块皆放不进（(${Math.floor(px)},${fy},${Math.floor(pz)})，停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，高度 ${py.toFixed(1)}）。`
            }
          }
          continue
        }

        if (feetSolid || headSolid) {
          // 面前有墙。一格台阶（脚堵头空）先跳，跳不过（卡死）才挖；
          // 两格高墙直接挖 1×2 隧道；禁挖则跳顶/侧绕。
          const step = feetSolid && !headSolid
          const target = feetSolid ? [fx, fy, fz] : [fx, fy + 1, fz]
          // 挖楼梯上行（旧版只会平挖 1×2 隧道，高度永远不变，掉坑里上不来）：
          // 只挖头格与头顶格、留脚下方块当台阶，下一拍 step 跳顶即升一格，逐列爬升。
          if (wantUp && feetSolid && headSolid && await diggable(digSpec, fx, fy + 1, fz)) {
            await rust.request('move', { buttons: 0, look: [0, 0] })
            if (tool) {
              const slot = await ensureHotbar(normalizeItem(tool))
              await rust.request('select', { slot })
            }
            task.note = `挖楼梯 (${fx},${fy + 1},${fz})`
            let dug = await digBlock(fx, fy + 1, fz, 12_000)
            if (dug && !(await blockIsAir(fx, fy + 2, fz))) dug = await digBlock(fx, fy + 2, fz, 12_000)
            log(`[travel] 挖楼梯 (${fx},${fy + 1},${fz}) 结果=${dug}`)
            if (dug) {
              lastProgress = Date.now()
              digFails.clear()
            } else if (noteDigFail(`${fx},${fy + 1},${fz}`) >= 2) {
              return `travel 遇阻：挖楼梯 (${fx},${fy + 1},${fz}) 连挖两轮不破（太硬或攻击被抢），停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，高度 ${py.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。`
            }
            continue
          }
          if ((!step || stuck) && await diggable(digSpec, target[0], target[1], target[2])) {
            await rust.request('move', { buttons: 0, look: [0, 0] })
            if (tool) {
              const slot = await ensureHotbar(normalizeItem(tool))
              await rust.request('select', { slot })
            }
            task.note = `挖 (${target.join(',')})`
            let dug = true
            if (feetSolid) dug = await digBlock(fx, fy, fz, 12_000)
            if (dug && headSolid && !(await blockIsAir(fx, fy + 1, fz))) dug = await digBlock(fx, fy + 1, fz, 12_000)
            log(`[travel] 挖 (${target.join(',')}) 结果=${dug}`)
            if (dug) {
              lastProgress = Date.now()
              digFails.clear()
            } else if (noteDigFail(target.join(',')) >= 2) {
              return `travel 遇阻：挖 (${target.join(',')}) 连挖两轮不破（太硬或攻击被抢），停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，高度 ${py.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。`
            }
            continue
          }
          // 垫块上拔：要上行、头顶开阔、但墙禁挖/挖不动且已卡死时，往脚下塞桥方块一格一格顶上去。
          if (wantUp && stuck && bridgeItem
            && await blockIsAir(Math.floor(px), fy + 1, Math.floor(pz))
            && await blockIsAir(Math.floor(px), fy + 2, Math.floor(pz))) {
            task.note = '垫块上拔'
            if (await pillarUp(px, py, pz)) {
              pillarFails = 0
              lastProgress = Date.now()
            } else {
              pillarFails += 1
              if (pillarFails >= 3) {
                return `travel 遇阻：要上行但挖不动也垫不进（(${Math.floor(px)},${fy},${Math.floor(pz)}) 三次垫块皆空，停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，高度 ${py.toFixed(1)}）。`
              }
            }
            continue
          }
          if (stuck) {
            if (sidestepUntil === 0 || Date.now() > sidestepUntil + 1_600) {
              // 侧绕一拍；两侧轮换
              sidestepSign = -sidestepSign
              sidestepUntil = Date.now() + 700
              if (Date.now() - lastProgress > 9_000) {
                return `travel 被挡死：(${fx},${fy},${fz}) 有方块且未允许挖掘，侧绕也过不去（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
              }
            }
            await rust.request('move', { buttons: 1 | 16, look: [0, 0] })
          } else {
            await rust.request('move', { buttons: 1 | 16, look: [0, 0] })  // 跳顶一格
          }
          await S(450)
          continue
        }

        // 对角楔死：斜跨方块角时，对角探柱（fx,fz）是空气，但两个正交前柱分别卡住
        // 碰撞箱的两个前侧面——bot 原地跳、travel 空转到超时，既不报墙也不报沟
        // （实机 11-05：山体内楼梯角 (109,72,51)/(108,72,52) 双石头，跳满 25s 一步未动）。
        // 卡住且对角无墙时回探两正交前柱：能挖挖楔角，禁挖则侧绕。
        if (stuck) {
          const ownCx = Math.floor(px)
          const ownCz = Math.floor(pz)
          const wedgeCols = []
          if (Math.abs(dx) > 0.3) {
            const wx = Math.floor(px + Math.sign(dx) * 0.9)
            if (wx !== ownCx) wedgeCols.push([wx, ownCz])
          }
          if (Math.abs(dz) > 0.3) {
            const wz = Math.floor(pz + Math.sign(dz) * 0.9)
            if (wz !== ownCz) wedgeCols.push([ownCx, wz])
          }
          let acted = false
          for (const [wx, wz] of wedgeCols) {
            if (wx === fx && wz === fz) continue  // 对角列前面已判空气
            for (const wy of [fy, fy + 1]) {
              if (await blockIsAir(wx, wy, wz)) continue
              if (await diggable(digSpec, wx, wy, wz)) {
                await rust.request('move', { buttons: 0, look: [0, 0] })
                if (tool) {
                  const slot = await ensureHotbar(normalizeItem(tool))
                  await rust.request('select', { slot })
                }
                task.note = `挖楔角 (${wx},${wy},${wz})`
                const dug = await digBlock(wx, wy, wz, 12_000)
                log(`[travel] 挖楔角 (${wx},${wy},${wz}) 结果=${dug}`)
                if (dug) {
                  lastProgress = Date.now()
                  digFails.clear()
                  acted = true
                } else if (noteDigFail(`${wx},${wy},${wz}`) >= 2) {
                  return `travel 遇阻：楔角 (${wx},${wy},${wz}) 连挖两轮不破（太硬或攻击被抢），停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，高度 ${py.toFixed(1)}，距目标 ${dist.toFixed(1)} 格。`
                }
              } else if (!acted && (sidestepUntil === 0 || Date.now() > sidestepUntil + 1_600)) {
                // 禁挖/挖不得的楔角：侧绕一拍，两侧轮换
                sidestepSign = -sidestepSign
                sidestepUntil = Date.now() + 700
                acted = true
              }
            }
          }
          if (acted) continue
        }

        // 面前是沟：浅坑直接下，深沟搭桥或停。
        // 近点（0.95）+远点（2.6）双探：移动是持续状态，450ms 一拍 ≈1.9 格，
        // 单探近点会在下一拍到来前冲出崖边（曾坠沟三次）。
        // 近点（0.95）+远点（2.6）双探：移动是持续状态，450ms 一拍 ≈1.9 格，
        // 单探近点会在下一拍到来前冲出崖边（曾坠沟三次）。
        // 远点见沟不直接搭桥（悬空无附着面必败），改点动脉冲慢行逼近；
        // 近点见沟才停稳搭桥——近列与脚下实地相邻，有附着面可放。
        const nearGap = await blockIsAir(fx, fy - 1, fz) && await blockIsAir(fx, fy - 2, fz)
        let farGap = false
        if (!nearGap) {
          const ffx = Math.floor(px + dirx * 2.6)
          const ffz = Math.floor(pz + dirz * 2.6)
          farGap = (ffx !== fx || ffz !== fz)
            && await blockIsAir(ffx, fy - 1, ffz) && await blockIsAir(ffx, fy - 2, ffz)
        }
        const gapAt = nearGap ? [fx, fz] : null
        if (gapAt) {
          const [gx, gz] = gapAt
          if (!bridgeItem) {
            return `travel 遇阻：前方 (${gx},${fy - 1},${gz}) 是 ≥2 格深沟/悬崖，没给搭桥方块（停在 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
          }
          await rust.request('move', { buttons: 0, look: [0, 0] })
          const slot = await ensureHotbar(bridgeItem)
          await rust.request('select', { slot })
          // 潜行（32）挪到悬沿再放桥：站在块上时侧脸射线必先撞上顶面
          // （原版几何，曾连放四次全落到别处）。潜行被边缘钳住不掉落，
          // 悬出后射线从侧面进入，才可把块贴到沟沿侧脸。
          let overhang = false
          for (let i = 0; i < 8 && !overhang; i++) {
            const cur = await rust.request('status')
            const [cpx, cpy, cpz] = cur.position ?? []
            if (![cpx, cpy, cpz].every(Number.isFinite)) break
            if (cpy < fy - 0.5) break  // 在坠落：潜行没钳住，放弃悬沿硬放
            if (await blockIsAir(Math.floor(cpx), fy - 1, Math.floor(cpz))) { overhang = true; break }
            await rust.request('move', { buttons: 1 | 32, look: [0, 0] })
            await S(140)
            await rust.request('move', { buttons: 32, look: [0, 0] })
            await S(80)
          }
          // 放桥必须验证落块：失手不验会一脚踏空坠沟。
          // 自 fy-1 向下最多三级找可放层——站在丘沿时脚下层悬空无附着面，
          // 降一级贴着低处实地放，走过去落一格即上桥。
          let bridged = false
          let bridgeY = null
          for (let lvl = fy - 1; lvl >= fy - 3 && !bridged; lvl--) {
            if (!(await blockIsAir(gx, lvl, gz))) { bridged = true; bridgeY = lvl; break }  // 已有块，可当桥面
            try {
              await placeBlock(gx, lvl, gz, bridgeItem, { approach: false })
            } catch (error) {
              log(`[travel] 搭桥 (${gx},${lvl},${gz}) 失败:`, error.message)
            }
            await S(400)
            if (!(await blockIsAir(gx, lvl, gz))) { bridged = true; bridgeY = lvl }
          }
          await rust.request('move', { buttons: 0, look: [0, 0] })
          task.note = `搭桥 (${gx},${bridgeY ?? fy - 1},${gz})`
          log(`[travel] 搭桥 (${gx},${bridgeY ?? '?'},${gz}) 结果=${bridged}`)
          if (!bridged) {
            return `travel 遇阻：搭桥三级皆空（(${gx},${fy - 3}~${fy - 1},${gz}) 无处贴放，停在前缘 ${px.toFixed(1)}, ${pz.toFixed(1)}，距目标 ${dist.toFixed(1)} 格）。`
          }
          // 走上桥：桥面常比岸低一两格（贴沿侧脸只够得着下层），
          // 直接点动前移落桥——不主动走上去，下一拍探头又会
          // 隔着桥列看下一列、误判深沟再搭一遍（12-27 实测卡此）。
          for (let i = 0; i < 6; i++) {
            const cur = await rust.request('status')
            const [cpx, cpy, cpz] = cur.position ?? []
            if (![cpx, cpy, cpz].every(Number.isFinite)) break
            if (Math.floor(cpx) === gx && Math.floor(cpz) === gz && Math.abs(cpy - (bridgeY + 1)) < 0.6) break  // 已站上桥
            if (cpy < bridgeY + 0.5) break  // 落过头了（不该发生），交回主循环
            await rust.request('move', { buttons: 1, look: [0, 0] })
            await S(150)
            await rust.request('move', { buttons: 0, look: [0, 0] })
            await S(120)
          }
          lastProgress = Date.now()
          continue
        }

        await rust.request('move', { buttons: stuck ? 1 | 16 : 1, look: [0, 0] })
        if (farGap || dist < 3) {
          // 点动脉冲：短进即停，下一拍重新探，防冲出崖边/冲过终点
          await S(140)
          await rust.request('move', { buttons: 0, look: [0, 0] })
          await S(120)
        } else {
          await S(450)
        }
      }
    } finally {
      if (engine.travelTask === task) engine.travelTask = null
      await rust.request('move', { buttons: 0, look: [0, 0] }).catch(() => {})
    }
  }

  /**
   * 跟随玩家（行为层版 /follow_player）：寻路跟，不是直线走。
   * 每拍 RCON 读目标坐标，超出 distance 就用 travel 走一段（挖墙/搭桥照规范），
   * 玩家在动所以每段限时 slice，到了就站着等下一拍；目标下线连续找不到即终止。
   */
  engine.followPlayer = async (opts) => {
    const { name, distance = 3, timeoutMs = 180_000, dig = true, bridge = null, tool = null } = opts
    if (!name) throw new Error('name 必填（玩家名）')
    if (!entityPos) throw new Error('行为层缺 entityPos 依赖')
    const deadline = Date.now() + timeoutMs
    const t0 = Date.now()
    let legs = 0
    let lost = 0
    addEvent('behavior', `follow 出发 → ${name}（保持 ${distance} 格）`)
    while (Date.now() < deadline && !engine.stopped) {
      const pos = await entityPos(name)
      if (!pos) {
        lost += 1
        if (lost >= 4) return `follow 终止：连续找不到玩家 ${name}（可能下线/换维度），跟了 ${Math.round((Date.now() - t0) / 1000)}s。`
        await S(1_500)
        continue
      }
      lost = 0
      const s = await rust.request('status')
      const me = s.position ?? []
      if (!me.every(Number.isFinite)) throw new Error('还读不到当前位置')
      const dist = Math.hypot(pos[0] - me[0], pos[2] - me[2])
      if (dist <= distance) {
        await lookAtPoint(pos[0], pos[1] + 1.6, pos[2])
        await S(1_000)
        continue
      }
      legs += 1
      const slice = Math.min(25_000, deadline - Date.now())
      if (slice < 2_000) break
      const msg = await engine.travel({ x: pos[0], z: pos[2], y: pos[1], timeoutMs: slice, dig, bridge, tool })
      if (/中断/.test(msg)) return `follow 被打断：${msg}`
      // 受阻/超时但玩家还在动：歇半拍再定位重试，死线前不放弃
      if (/被挡死|遇阻/.test(msg)) {
        addEvent('behavior', `follow 受阻：${msg}`)
        await S(1_500)
      }
    }
    return `follow 时长到：跟了 ${Math.round((Date.now() - t0) / 1000)}s，走了 ${legs} 段路。`
  }

  /**
   * 挖目标方块（行为层 /mine）：以自身为圆心环带外扩扫 range 格找 block，
   * 找到一块走一块（travel 带 y 提示，途中挖墙/搭桥照规范），到位挖掉计数；
   * 挖掉后播种六邻进候选队列（矿脉/树干成片，省得每块都全图重扫）。
   * back=true 完工后 travel 回出发点。扫不到/超时/超范围如实报告。
   */
  engine.mine = async (opts) => {
    const { block, count = 1, tool = null, range = 12, timeoutMs = 180_000, dig = true, bridge = null, back = false } = opts
    const { label, targets } = buildTargets(block)
    if (!targets.length || targets.includes('air')) throw new Error('block 必填（要挖的方块 id，如 oak_log；找任意树用 log）')
    const matchBlock = async (x, y, z) => {
      for (const t of targets) if (await blockIs(x, y, z, t)) return true
      return false
    }
    const toolName = tool ? normalizeItem(tool) : null
    if (toolName) await ensureHotbar(toolName)
    const deadline = Date.now() + timeoutMs
    const s0 = await rust.request('status')
    const start = s0.position ?? []
    if (!start.every(Number.isFinite)) throw new Error('还读不到当前位置')
    addEvent('behavior', `mine 出发：${count}× ${label}（半径 ${range}，出发于 ${start.map((v) => v.toFixed(0)).join(',')}）`)
    let got = 0
    let scanned = 0
    const tried = new Set()
    const queue = []           // 候选 [x,y,z,verified]，前头的先挖；verified=扫描时验明正身
    const inQueue = new Set()
    const pushCand = (x, y, z, verified = false) => {
      const k = `${x},${y},${z}`
      if (inQueue.has(k) || tried.has(k)) return
      inQueue.add(k)
      queue.push([x, y, z, verified])
    }
    const seedNeighbors = (x, y, z) => {
      for (const [dx, dy, dz] of [[0, 1, 0], [0, -1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]]) pushCand(x + dx, y + dy, z + dz, false)
    }
    // 环带外扩扫描：以 (cx,cz) 为心逐环探目标方块，命中即入队。
    // 每列分三层交错探 [cy-3..cy+4] / [cy+5..cy+12] / [cy-11..cy-4]（合并覆盖 cy-11..cy+12）：
    // 山地的树在头顶 y+7~10，单层带 [cy-3,cy+4] 站在谷底永远扫不到（实机踩过：
    // 距树 9 格三次报找不到树）；代价是每列 3× 探测，range 大要给足 timeout。
    const scanNames = nativeScan ? nativeNames(targets) : null
    const scan = async (cx, cy, cz) => {
      // 本地世界扫描（首选）：区块缓存已在客户端内存里，整盒查询毫秒级零网络。
      // 旧 RCON 路径一格一探 ~10ms，半径 12 三层 ≈1.5 万格 ≈2.5 分钟——「扫一下要半天」根因。
      if (scanNames) {
        try {
          const res = await nativeScan({ names: scanNames, radius: range, dyMin: -11, dyMax: 12, limit: 48 })
          scanned += res.scanned ?? 0
          for (const [hx, hy, hz] of res.hits ?? []) pushCand(hx, hy, hz, true)
          return
        } catch (error) {
          log('[scan] 本地扫描失败，回退 RCON 逐格:', error.message)
        }
      }
      // r 从 0 起：自己站的那列也要探——实机 11-05，bot 走进树干列里报「五万格零命中」，
      // 残干就在头顶 (dx=0,dz=0)，r=1 起扫永远探不到脚下/头顶本列。
      for (let r = 0; r <= range; r++) {
        for (let dx = -r; dx <= r; dx++) {
          for (const dz of (Math.abs(dx) === r ? rangeStep(r) : [-r, r])) {
            for (const yb of [cy, cy + 8, cy - 8]) {
              for (let dy = -3; dy <= 4; dy++) {
                if (Date.now() > deadline) return
                scanned += 1
                if (await matchBlock(cx + dx, yb + dy, cz + dz)) pushCand(cx + dx, yb + dy, cz + dz, true)
              }
            }
          }
        }
        if (queue.length) return  // 有活先干，别一口气扫到超时
      }
    }
    const rangeStep = (r) => { const a = []; for (let dz = -r; dz <= r; dz++) a.push(dz); return a }
    await scan(Math.floor(start[0]), Math.floor(start[1]), Math.floor(start[2]))
    while (got < count && Date.now() < deadline) {
      const cand = queue.shift()
      if (!cand) {
        // 队列空：从当前位置补扫一圈，仍空就是 range 内真没了
        const cur = await rust.request('status')
        const me = cur.position ?? []
        if (!me.every(Number.isFinite)) break
        await scan(Math.floor(me[0]), Math.floor(me[1]), Math.floor(me[2]))
        if (!queue.length) break
        continue
      }
      const [bx, by, bz, verified] = cand
      const key = `${bx},${by},${bz}`
      tried.add(key)
      if (Math.hypot(bx - start[0], bz - start[2]) > range) continue   // 超范围阈值
      if (await blockIsAir(bx, by, bz)) {
        // 扫描时是目标、轮到它时已不在——travel 走 1×2 隧道会把目标方块当墙顺手挖掉，
        // 那也是我们的战果，计进账并播种邻居（未验明正身的邻居候选不算）
        if (verified) {
          got += 1
          addEvent('behavior', `mine 挖到 ${label}（${got}/${count}）@ (${bx},${by},${bz})（赶路途中顺手挖掉）`)
          seedNeighbors(bx, by, bz)
        }
        continue
      }
      if (!(await matchBlock(bx, by, bz))) continue                     // 变戏法了
      const cur = await rust.request('status')
      const me = cur.position ?? []
      const py = me.every(Number.isFinite) ? me[1] : by
      // 腿时限随三维距离伸缩：硬顶 25s 对「山体内挖楼梯上行 7 格」这种活必死——
      // 每列楼梯两挖×石 ~7.5s≈15s，9 列要两分钟开外，25s 的腿连挖 6 条全超时
      // （实机 11-05）。好在进度是累积的：腿超时后 bot 留在原地，下条腿接着爬。
      const distEst = me.every(Number.isFinite) ? Math.hypot(bx - me[0], by - me[1], bz - me[2]) : 5
      const slice = Math.min(deadline - Date.now(), Math.max(25_000, distEst * 6_000), 90_000)
      if (slice < 2_000) break
      // y 提示取「目标方块自身的高度」（by）：travel 容差 ±1.6 同时容纳站旁边（by）、
      // 站上目标头顶（by+1）和矮一格（by-1）三种到位姿势。用 by-1 会在机器人跳上
      // 目标方块头顶时 |py-y|=2 永远判不到点（实机踩过：停在 0.3 格外超时）；
      // 用 by+1 则会让 travel 去爬根本不需要爬的高度。
      // 挖掘半径从眼睛算（原版 4.5 是视距，眼高 +1.62）：已伸手可及就不跑路；
      // 腿没走完但够得着也就地开挖——实机 11-05：bot 挖楼梯爬到树干水平 0.1 格、
      // 低 2 格，travel 死磕高度提示报「深沟」遇阻放弃，其实站着伸手就能够着。
      // 按脚底算会矮一截：正头顶 4 格的原木误判够不着（测试踩过）。
      const reachOf = (p) => p.every(Number.isFinite)
        && Math.hypot(bx + 0.5 - p[0], by + 0.5 - (p[1] + 1.62), bz + 0.5 - p[2]) <= 4.2
      if (!reachOf(me)) {
        const leg = await engine.travel({ x: bx + 0.5, z: bz + 0.5, y: by > py - 0.5 ? by : null, timeoutMs: slice, dig, bridge, tool: toolName })
        if (/中断/.test(leg)) return `mine 被打断：${leg}（已挖 ${got}/${count} 个 ${label}）。`
        if (/被挡死|遇阻|超时/.test(leg)) {
          const now = await rust.request('status')
          const np = now.position ?? []
          if (!reachOf(np)) {
            addEvent('behavior', `mine 够不着 (${bx},${by},${bz})：${leg}`)
            continue  // 这块放弃，试下一块
          }
          addEvent('behavior', `mine 腿未走完但 (${bx},${by},${bz}) 已在挖掘半径内，就地开挖`)
        }
      }
      // 到位：方块还在就亲手挖（途中被 travel 顺手挖掉则直接计数）
      if (!(await blockIsAir(bx, by, bz))) {
        if (toolName) {
          const slot = await ensureHotbar(toolName)
          await rust.request('select', { slot })
        }
        await digBlock(bx, by, bz, 25_000)  // 砖/石级别硬度手挖 ~10s，留足余量
      }
      if (await blockIsAir(bx, by, bz)) {
        got += 1
        addEvent('behavior', `mine 挖到 ${label}（${got}/${count}）@ (${bx},${by},${bz})`)
        seedNeighbors(bx, by, bz)
      } else {
        addEvent('behavior', `mine 挖不动 (${bx},${by},${bz})，跳过`)
      }
    }
    let tail = ''
    if (back && start.length) {
      const slice = Math.min(60_000, Math.max(5_000, deadline - Date.now()))
      tail = ` 返程：${await engine.travel({ x: start[0], z: start[2], y: start[1], timeoutMs: slice, dig, bridge, tool: toolName })}`
    }
    const why = got >= count ? '完成' : Date.now() >= deadline ? '超时' : `半径 ${range} 内扫不到更多`
    return `mine ${why}：挖到 ${got}/${count} 个 ${label}（探了 ${scanned} 格）。${tail}`
  }

  /**
   * 找方块只报坐标不动手（行为层 /find）：与 mine 同款分层螺旋扫描
   * （环带外扩 + 每列三层 y 带，山地树在头顶/洞穴在脚下都够得着），
   * 但只勘察报告——「附近有没有树/矿、帮我数数」走这里。
   * 别用 /scan 干这个：它半径被钳在 ±2，只是贴脸探针（实机 11-05：
   * AI 拿 mc_scan「换几个方向扫一圈」找树，永远报空）。
   */
  engine.find = async (opts) => {
    const { block, range = 12, limit = 10, timeoutMs = 60_000 } = opts
    const { label, targets } = buildTargets(block)
    if (!targets.length || targets.includes('air')) throw new Error('block 必填（要找的方块 id，如 oak_log；找任意树用 log）')
    const matchBlock = async (x, y, z) => {
      for (const t of targets) if (await blockIs(x, y, z, t)) return true
      return false
    }
    const deadline = Date.now() + timeoutMs
    const s0 = await rust.request('status')
    const start = s0.position ?? []
    if (!start.every(Number.isFinite)) throw new Error('还读不到当前位置')
    const [cx, cy, cz] = start.map(Math.floor)
    addEvent('behavior', `find 出发：找 ${label}（半径 ${range}，出发于 ${cx},${cy},${cz}）`)
    // 本地世界扫描（首选，毫秒级）：区块缓存就在客户端内存里。
    const scanNames = nativeScan ? nativeNames(targets) : null
    if (scanNames) {
      try {
        const res = await nativeScan({ names: scanNames, radius: range, dyMin: -11, dyMax: 12, limit: Math.max(limit, 32) })
        const found = (res.hits ?? []).slice(0, limit)
        const unscanned = res.unloaded ? `，${res.unloaded} 格未加载` : ''
        if (!found.length) return `find 半径 ${range} 内扫不到 ${label}（本地世界扫了 ${res.scanned ?? 0} 格${unscanned}）。`
        const lines = [`find 找到 ${found.length} 处 ${label}（半径 ${range}，本地世界扫了 ${res.scanned ?? 0} 格${unscanned}，出发于 ${cx},${cy},${cz}）：`]
        for (const [hx, hy, hz] of found) {
          const d = Math.hypot(hx - start[0], hy - start[1], hz - start[2])
          lines.push(`- (${hx},${hy},${hz}) 距 ${d.toFixed(1)} 格`)
        }
        return lines.join('\n')
      } catch (error) {
        log('[scan] 本地扫描失败，回退 RCON 逐格:', error.message)
      }
    }
    let scanned = 0
    const hits = []
    const seen = new Set()
    let lastHitRing = 0
    outer:
    // r 从 0 起：自己站的那列也要探——实机 11-05，bot 站在树干列里扫 5.7 万格报零，
    // 残干就在头顶本列，r=1 起扫是盲人摸象。
    for (let r = 0; r <= range; r++) {
      for (let dx = -r; dx <= r; dx++) {
        for (let dz = -r; dz <= r; dz++) {
          if (Math.abs(dx) !== r && Math.abs(dz) !== r) continue  // 只走环带周界（r=0 即本列）
          for (const yb of [cy, cy + 8, cy - 8]) {
            for (let dy = -3; dy <= 4; dy++) {
              if (Date.now() > deadline) break outer
              scanned += 1
              if (!(await matchBlock(cx + dx, yb + dy, cz + dz))) continue
              const hx = cx + dx
              const hy = yb + dy
              const hz = cz + dz
              const k = `${hx},${hy},${hz}`
              if (seen.has(k)) continue
              seen.add(k)
              hits.push([hx, hy, hz])
              lastHitRing = r
              if (hits.length >= limit) break outer
            }
          }
        }
      }
      // 勘察不是 mine 的「找一块干一块」：首个命中即收会漏报两圈外的林子
      // （实测：环 4 一棵、环 6 一棵，报 1 处），多扫两圈再收。
      if (hits.length && r > lastHitRing + 2) break
    }
    const timedOut = Date.now() >= deadline && hits.length < limit
    if (!hits.length) {
      return `find 半径 ${range} 内扫不到 ${label}（探了 ${scanned} 格${timedOut ? '，超时提前收工，外圈还没扫' : ''}）。`
    }
    const lines = [`find 找到 ${hits.length} 处 ${label}（半径 ${range}，探了 ${scanned} 格，出发于 ${cx},${cy},${cz}）：`]
    for (const [hx, hy, hz] of hits) {
      const d = Math.hypot(hx - start[0], hy - start[1], hz - start[2])
      lines.push(`- (${hx},${hy},${hz}) 距 ${d.toFixed(1)} 格`)
    }
    if (timedOut) lines.push('（超时提前收工，远处可能还有）')
    return lines.join('\n')
  }

  /**
   * 3D 打印建筑（行为层 /build）：给一个蓝图，逐块把方块摆出来。
   *
   * 蓝图有三种来源，优先级从高到低：
   *   1. blocks= 显式列表 "x,y,z:item;x,y,z:item"（相对建筑原点）
   *   2. shape= 方块名 + w/h/d + 材质（实心立方体/墙/地板/空心屋）
   *   3. blueprint= 预设名（house / tower / platform / pillar / wall / arch）
   *
   * 打印顺序：先地板后墙再顶（避免悬空方块掉落），逐块 placeBlock。
   * 已有方块跳过（不浪费材料）；缺材料如实报告放了多少、缺什么。
   */
  /**
   * 生成蓝图方块清单 [x,y,z,item]（相对原点）。engine.build 与材料估算共用，
   * 「要多少块」必须和真正要盖的东西用同一份计算，否则采集量对不上。
   */
  const makeBuildPlan = ({ blueprint = null, blocks = null, shape = null, material = 'stone', w = 5, h = 4, d = 5, hollow = false }) => {
    if (!blueprint && !blocks && !shape) throw new Error('build 必填 blueprint / blocks / shape 之一')
    const mat = normalizeItem(material)
    if (!mat) throw new Error('material 不合法')
    const plan = []
    if (blocks) {
      for (const raw of String(blocks).split(';')) {
        const t = raw.trim()
        if (!t) continue
        const [coord, item] = t.split(':')
        const [x, y, z] = coord.split(',').map((v) => Number(v))
        if (![x, y, z].every(Number.isFinite)) continue
        plan.push([x, y, z, normalizeItem(item || mat) || mat])
      }
    } else if (shape) {
      const W = Math.max(1, Math.min(32, w | 0))
      const H = Math.max(1, Math.min(32, h | 0))
      const D = Math.max(1, Math.min(32, d | 0))
      const solid = shape === 'solid' || shape === 'cube'
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          for (let z = 0; z < D; z++) {
            // 实心块：全部填；空心：只留墙（边缘一格厚）
            const edge = x === 0 || z === 0 || x === W - 1 || z === D - 1
            if (!solid && !edge && y !== 0 && y !== H - 1) continue
            plan.push([x, y, z, mat])
          }
        }
      }
    } else {
      // 预设蓝图
      const P = BUILD_PRESETS[blueprint]
      if (!P) throw new Error(`没有蓝图「${blueprint}」，可选：${Object.keys(BUILD_PRESETS).join(', ')}`)
      P(mat).forEach((b) => plan.push(b))
    }
    if (!plan.length) throw new Error('蓝图是空的')
    return plan
  }
  engine.estimateBuildPlan = (opts) => makeBuildPlan(opts)

  engine.build = async (opts) => {
    const {
      blueprint = null, blocks = null, shape = null, material = 'stone',
      w = 5, h = 4, d = 5, at = null, radius = 24, timeoutMs = 300_000, hollow = false,
    } = opts
    if (!blueprint && !blocks && !shape) throw new Error('build 必填 blueprint / blocks / shape 之一')
    const mat = normalizeItem(material)
    if (!mat) throw new Error('material 不合法')
    if (!placeBlock) throw new Error('行为层缺 placeBlock 依赖')

    // ── 生成蓝图方块清单 [x,y,z,item]（相对原点）────────────────────────
    const plan = makeBuildPlan({ blueprint, blocks, shape, material, w, h, d, hollow })

    // ── 定锚点：默认脚下（站在原地往上盖），at= 指定目标点先走过去 ──────
    const s0 = await rust.request('status')
    const me = s0.position ?? []
    if (!me.every(Number.isFinite)) throw new Error('还读不到当前位置')
    let ax = Math.floor(me[0])
    let ay = Math.floor(me[1])
    let az = Math.floor(me[2])

    if (at) {
      const leg = await engine.travel({
        x: at[0], z: at[2], y: at[1] ?? null, timeoutMs: Math.min(90_000, timeoutMs), dig: true, bridge: null,
      })
      if (/中断/.test(leg)) return `build 被打断：${leg}`
      if (/被挡死|超时/.test(leg)) addEvent('behavior', `build 没走到锚点(${at.join(',')})：${leg}（就地盖）`)
      const s1 = await rust.request('status')
      const me2 = s1.position ?? []
      if (me2.every(Number.isFinite)) {
        ax = Math.floor(me2[0]); ay = Math.floor(me2[1]); az = Math.floor(me2[2])
      }
    }

    // 蓝图高度：贴在「实心地面层」上往上长。放方块必须有相邻方块可依附，
    // 悬空一格 place 会报「六个邻居全是空气」。
    // 用 nav 网格找角色脚下的真实地面高度（角色可能站在水面/悬空/楼板上）。
    let baseY = ay
    let grid = null
    if (nav) {
      try {
        const res = await nav({ radius: 12, dyMin: -12, dyMax: 8 })
        grid = decodeGrid(res)
        const g = groundLevel(grid, ax, az, ay + 1)
        // groundLevel 返回的是「脚下第一层可站的高度」，即第一层空气；
        // 蓝图 y=0 就贴在这一层，地板方块靠它下方那层实心块依附。
        if (g !== null && g <= ay + 1) baseY = g
      } catch (e) {
        addEvent('behavior', `build 没拿到体素网格，锚点按脚下估算：${e.message}`)
      }
    }
    // 蓝图原点挪到角色旁边一格（+x）：原点若压在角色自己脚下，place 会因实体占位失败。
    const ox = ax + 1
    const cells = plan.map(([x, y, z, item]) => ({ x: ox + x, y: baseY + y, z: az + z, item }))

    const deadline = Date.now() + timeoutMs
    addEvent('behavior', `build 开工：${cells.length} 块 ${mat}，锚点 (${ax},${baseY},${az})`)

    // 打印顺序：按 y 从低到高（先地板后墙），同层按离锚点近的先
    cells.sort((a, b) => (a.y - b.y) || (Math.hypot(a.x - ox, a.z - az) - Math.hypot(b.x - ox, b.z - az)))

    let placed = 0
    let skipped = 0
    let failed = 0
    let supports = 0
    const failedItems = new Map()
    const missing = new Map()
    const failedReasons = new Map()
    for (const c of cells) {
      if (Date.now() > deadline) {
        addEvent('behavior', `build 超时收工：已放 ${placed}/${cells.length}`)
        break
      }
      if (engine.action) { await S(400); continue }
      if (engine.travelTask?.cancel) return `build 被打断（已放 ${placed}/${cells.length} 块）。`
      // 已有方块：跳过，不浪费材料
      if (!(await blockIsAir(c.x, c.y, c.z))) { skipped++; continue }
      // 材料够不够
      await ensureHotbar(c.item)
      let ok = false
      let err = ''
      try {
        ok = await placeBlock(c.x, c.y, c.z, c.item, { approach: false })
      } catch (e) {
        err = e?.message ?? String(e)
      }
      // 悬空放不下（六个邻居全是空气）→ 往下连续垫支撑柱直到落到实心地面再试。
      // 蓝图是"实心体"时最外层的地板常常悬在半空，靠这根柱子落地。
      if (!ok && /空气|依附|邻居/.test(err)) {
        let sy = c.y - 1
        let built = 0
        while (sy > c.y - 12) {
          if (!(await blockIsAir(c.x, sy, c.z))) break       // 下面已是实心，够得着
          const supOk = await placeBlock(c.x, sy, c.z, c.item, { approach: false }).catch(() => false)
          if (!supOk) break
          supports++; built++
          sy--
        }
        if (built) ok = await placeBlock(c.x, c.y, c.z, c.item, { approach: false }).catch(() => false)
      }
      if (ok) {
        placed++
        if (placed % 10 === 0) addEvent('behavior', `build 进度 ${placed}/${cells.length}`)
      } else {
        failed++
        failedItems.set(c.item, (failedItems.get(c.item) ?? 0) + 1)
        // 只有「背包里没有」才算缺材料；放不下/放失败是另一回事，别混报
        if (/背包|没有|material|item/i.test(err)) {
          missing.set(c.item, (missing.get(c.item) ?? 0) + 1)
        } else {
          failedReasons.set(err || '放置失败（原因未知）', (failedReasons.get(err || '放置失败（原因未知）') ?? 0) + 1)
        }
        if (err && failedReasons.size + missing.size === 1) {
          addEvent('behavior', `build 放置失败（${c.x},${c.y},${c.z}）：${err}`)
        }
      }
    }
    const parts = [`build 完工：放了 ${placed}/${cells.length} 块${skipped ? `（已有 ${skipped} 块跳过）` : ''}`]
    if (supports) parts.push(`补了 ${supports} 根支撑柱`)
    if (failed) {
      parts.push(`失败 ${failed} 块`)
      if (failedReasons.size) {
        parts.push(`原因：${[...failedReasons.entries()].map(([k, v]) => `${k}（${v} 块）`).join('；')}`)
      }
    }
    if (missing.size) {
      parts.push(`缺材料：${[...missing.entries()].map(([k, v]) => `${k}×${v}`).join('、')}`)
      parts.push('背包里没有这些物品，先 make/give 补齐再 build。')
    }
    if (placed < cells.length && !failed) parts.push('（超时未铺完，可以再 build 一次续工，同位置会跳过已有方块）')
    return parts.join('。')
  }

  /**
   * 网格勘察（/survey）：用 nav 体素盒一次性扫，比 RCON 逐格快一个量级。
   * 适合「这附近有什么」「有多少棵树」「矿脉在哪」——find 的快路径。
   */
  engine.survey = async (opts) => {
    const { block, radius = 16, limit = 20, timeoutMs = 60_000 } = opts
    if (!nav) throw new Error('行为层缺 nav 依赖（客户端不支持体素网格）')
    const { label, targets } = buildTargets(block)
    if (!targets.length || targets.includes('air')) throw new Error('block 必填（要找的方块 id）')
    const names = nativeNames(targets)
    const s0 = await rust.request('status')
    const me = s0.position ?? []
    if (!me.every(Number.isFinite)) throw new Error('还读不到当前位置')
    const o = [Math.floor(me[0]), Math.floor(me[1]), Math.floor(me[2])]
    const t0 = Date.now()
    const res = await nav({ radius: Math.min(32, radius), dyMin: -11, dyMax: 12 })
    const grid = decodeGrid(res)
    const found = names
      ? searchBlocks(grid, names, o, { radius: Math.min(32, radius), dyMin: -11, dyMax: 12, limit })
      : { hits: [], scanned: 0 }
    const ms = Date.now() - t0
    if (!found.hits.length) {
      return `survey 半径 ${radius} 内没有 ${label}（体素网格扫了 ${found.scanned} 格，${ms}ms）。`
    }
    const lines = [`survey 找到 ${found.hits.length} 处 ${label}（体素网格扫了 ${found.scanned} 格，${ms}ms，出发于 ${o.join(',')}）：`]
    for (const [hx, hy, hz] of found.hits) {
      lines.push(`- (${hx},${hy},${hz}) 距 ${Math.hypot(hx - me[0], hy - me[1], hz - me[2]).toFixed(1)} 格`)
    }
    return lines.join('\n')
  }

  // ---- 调度系统 API（通用任务组合） ----
  /**
   * 创建任务：type 是行为名（follow、mine、build等），priority 决定执行顺序
   * fn 是执行函数：async (scheduler, me, tick, task) => { ...逻辑... }
   * 返回 taskId，外部可用于查询/取消
   */
  engine.createTask = (type, priority, fn, id, params) => engine.scheduler.create(type, priority, fn, id, params)

  /**
   * 取消任务
   */
  engine.cancelTask = (taskId, reason) => engine.scheduler.cancel(taskId, reason)

  /**
   * 查询任务
   */
  engine.getTask = (taskId) => engine.scheduler.get(taskId)

  /**
   * 列出所有活跃任务
   */
  engine.listTasks = () => engine.scheduler.list()

  /**
   * 调度器主循环：每帧调一次（由 tick 内部调用，外部也可手动调）
   */
  engine.scheduleStep = async () => {
    const me = myPos()
    return await engine.scheduler.step(me)
  }

  /** 任务快照（给 /schedule/tasks 用）：纯数据，不含函数引用。 */
  const taskSnapshot = (task) => task && ({
    id: task.id,
    type: task.type,
    priority: task.priority,
    status: task.status,
    state: task.state,
    note: task.note,
    error: task.error,
    created: task.createdAt,
    lastTick: task.lastTick,
  })
  engine.taskSnapshot = taskSnapshot
  engine.snapshotTasks = () => engine.scheduler.list().map(taskSnapshot)
  engine.snapshotHistory = (limit = 10) => engine.scheduler.historyList(limit)

  // ── 行为工厂：把「高层意图 + 参数」翻译成每 tick 推进一步的 Task ──────────
  // 工厂返回 { type, fn, state }；fn(scheduler, me, tick, task) 的单次调用只做
  // 一小段工作（走一小步/挖一块/放一批），所以多个任务能在不同 tick 间交错。

  /** 找实体位置：玩家走 RCON 精确坐标；生物走实体内存快照。 */
  const findEntityPos = async (name) => {
    const viaRcon = entityPos ? await entityPos(name).catch(() => null) : null
    if (viaRcon) return viaRcon
    const norm = normalizeItem(name)
    const list = await nearbyEntities(48).catch(() => [])
    const hit = (list ?? []).find((e) => e.name === norm || e.name === name)
    return hit?.position ?? null
  }

  /**
   * 距离约束：锚点/实体离我超过 maxDistance 时本拍让位。
   * 例：mine 任务带 stayWithin=10 & anchorName=主人 → 主人走远了先不挖。
   */
  const distanceConstraint = (anchor, maxDistance) => ({
    check: async (task, scheduler, me) => {
      if (!me || !anchor) return null
      const target = Array.isArray(anchor) ? anchor : await findEntityPos(String(anchor))
      if (!target) return null   // 锚点暂时找不到：不拦（让任务自己处理丢失）
      const d = Math.hypot(target[0] - me[0], target[2] - me[2])
      if (d > maxDistance) {
        const label = Array.isArray(anchor) ? '锚点' : String(anchor)
        return { blocked: true, reason: `离 ${label} ${d.toFixed(1)} 格（>${maxDistance}），先靠近` }
      }
      return null
    },
  })

  /**
   * 链接约束：只在另一个任务（通常是 follow）的目标附近干活——「跟我走，顺便挖」。
   * follow 的 state.target 是它最近一次看到的目标坐标；离得太远就暂停采集先去跟上。
   */
  const linkedFollowConstraint = (followId, maxDistance, label = '跟随目标') => ({
    check: (task, scheduler, me) => {
      const follow = scheduler.get(followId)
      const t = follow?.state?.target
      if (!t || !me) return null
      const d = Math.hypot(t[0] - me[0], t[2] - me[2])
      if (d > maxDistance) {
        return { blocked: true, reason: `离${label} ${d.toFixed(1)} 格（>${maxDistance}），先跟上再干` }
      }
      return null
    },
  })

  /** 把 JSON 参数里的约束描述（anchorName/stayWithin/followId）编成 Task 约束。 */
  const constraintsFromParams = (params = {}, followId = null) => {
    const checks = []
    if (followId) {
      checks.push(linkedFollowConstraint(followId, Number(params.stayWithin ?? 8), params.name ? String(params.name) : '跟随目标'))
    } else if (params.anchorName || (params.anchor && params.stayWithin)) {
      checks.push(distanceConstraint(params.anchorName ?? params.anchor, Number(params.stayWithin ?? 16)))
    }
    if (!checks.length) return null
    return {
      check: async (task, scheduler, me) => {
        for (const c of checks) {
          const v = await c.check(task, scheduler, me)
          if (v) return v
        }
        return null
      },
    }
  }

  /** 跟随（常驻）：每 tick 最多走一小步；已在距离内则 idle 让位。 */
  const followTask = (p) => {
    const name = String(p.name ?? p.target ?? '').trim()
    if (!name) throw new Error('follow 任务缺 name（玩家名/生物名）')
    const wantDist = Math.max(2, Number(p.distance ?? p.dist ?? 3))
    const st = { seen: 0, lost: 0, target: null }
    const fn = async (scheduler, me) => {
      if (!me) return { idle: true }
      const target = await findEntityPos(name)
      if (!target) {
        st.lost += 1
        if (st.lost >= 10) return { done: true, note: `跟丢 ${name}（连续 10 拍找不到）` }
        return { idle: true }
      }
      st.lost = 0
      st.seen += 1
      st.target = target.map((v) => Number(v.toFixed(1)))
      const d = Math.hypot(target[0] - me[0], target[2] - me[2])
      if (d <= wantDist) return { idle: true }   // 已到位：不占身体，让采集任务干活
      const dy = Math.abs(target[1] - me[1])
      await engine.travel({
        x: target[0], z: target[2], y: dy > 1.6 ? target[1] : null,
        timeoutMs: d > 24 ? 2500 : 1500,
        dig: false,
      })
      return { claimed: true }
    }
    return { type: 'follow', fn, state: st }
  }

  /** 采集（定量）：每 tick 挖一块（engine.mine 带限时切片），够了 done。 */
  const mineTask = (p) => {
    const block = String(p.block ?? p.target ?? '').trim()
    if (!block) throw new Error('mine 任务缺 block（方块 id，如 oak_log）')
    const count = Math.max(1, Number(p.count ?? 1))
    const st = { mined: 0, target: count, tries: 0, last: '' }
    const fn = async () => {
      if (st.mined >= count) return { done: true, note: `挖够 ${count} 个 ${block}` }
      st.tries += 1
      const before = st.mined
      const msg = await engine.mine({
        block, count: 1, tool: p.tool ?? null,
        range: Number(p.range ?? 12),
        timeoutMs: Number(p.sliceMs ?? 12_000),
        dig: p.dig ?? true,
        bridge: p.bridge ?? null,
        back: Boolean(p.back ?? false),
      })
      st.last = msg
      const m = msg.match(/挖到\s*(\d+)\s*\/\s*(\d+)/)
      const got = m ? Number(m[1]) : 0
      if (got > 0) { st.mined += got; st.tries = 0 }
      if (st.mined >= count) return { done: true, note: `挖够 ${count} 个 ${block}` }
      // 连续 3 拍零收获：半径内真没了/挖不动，如实结束
      if (st.mined === before && st.tries >= 3) {
        return { done: true, note: `只挖到 ${st.mined}/${count} 个 ${block}（附近没有了）：${msg}` }
      }
      return { claimed: true }
    }
    return { type: 'mine', fn, state: st }
  }

  /** 建筑（蓝图）：每 tick 推进一段（engine.build 自带时限切片），放完 done。 */
  const buildTask = (p) => {
    if (!p.blueprint && !p.blocks && !p.shape) throw new Error('build 任务缺 blueprint / blocks / shape')
    const st = { placed: 0, total: 0, stall: 0, last: '' }
    const fn = async () => {
      const msg = await engine.build({
        blueprint: p.blueprint ?? null,
        blocks: p.blocks ?? null,
        shape: p.shape ?? null,
        material: p.material ?? 'stone',
        w: Number(p.w ?? 5), h: Number(p.h ?? 4), d: Number(p.d ?? 5),
        at: p.at ?? null,
        radius: Number(p.radius ?? 24),
        timeoutMs: Number(p.sliceMs ?? 8000),
        hollow: Boolean(p.hollow ?? false),
      })
      st.last = msg
      const m = msg.match(/放了\s*(\d+)\s*\/\s*(\d+)/)
      const placed = m ? Number(m[1]) : st.placed
      const total = m ? Number(m[2]) : st.total
      // 「已有 N 块跳过」也算完成——续工/重叠施工时几乎全 skip，不能当停滞
      const sk = msg.match(/已有\s*(\d+)\s*块跳过/)
      const skipped = sk ? Number(sk[1]) : 0
      const progress = placed + skipped
      if (total > 0 && progress >= total) return { done: true, note: `建筑完成（${placed}/${total} 放置${skipped ? `，${skipped} 块已有` : ''}）` }
      if (progress > st.placed) { st.placed = progress; st.stall = 0 } else { st.stall += 1 }
      st.total = total || st.total
      if (st.stall >= 3) return { done: true, note: `建筑停滞（${st.placed}/${st.total}）：${msg}` }
      return { claimed: true }
    }
    return { type: 'build', fn, state: st }
  }

  /** 捡拾（常驻/定时）：每 tick 朝最近掉落物走一步，走到就等服务器判拾取。 */
  const collectTask = (p = {}) => {
    const radius = Number(p.radius ?? 24)
    const st = { picked: 0, emptyTicks: 0, lastD: Infinity, stall: 0 }
    const fn = async (scheduler, me) => {
      if (!me) return { idle: true }
      const items = (await nearbyEntities(radius).catch(() => [])).filter((e) => e.name === 'item')
      if (!items.length) {
        st.emptyTicks += 1
        return st.emptyTicks >= 3 ? { done: true, note: `捡了 ${st.picked} 处，附近没了` } : { idle: true }
      }
      st.emptyTicks = 0
      items.sort((a, b) => Math.hypot(a.position[0] - me[0], a.position[2] - me[2])
        - Math.hypot(b.position[0] - me[0], b.position[2] - me[2]))
      const item = items[0]
      // 3D 距离：掉落物常在同一水平面外一格高/低，纯水平距离会「以为到了」
      // 但实际够不着拾取半径，原地空转（实机 11-08：崖边物品水平 1.4 格判到位）。
      const d = Math.hypot(item.position[0] - me[0], item.position[1] - me[1], item.position[2] - me[2])
      if (d > 1.4) {
        // 停滞守护：连续 8 拍距离没缩短 → 这块够不着（崖壁/水/栅栏），如实收工
        if (d >= st.lastD - 0.1) st.stall += 1
        else { st.stall = 0; st.lastD = d }
        if (st.stall >= 8) {
          return { done: true, note: `够不着 ${item.position.map((v) => v.toFixed(0)).join(',')} 的掉落物（差 ${d.toFixed(1)} 格），已捡 ${st.picked} 处` }
        }
        const bridge = p.bridge ?? null
        await engine.travel({
          x: item.position[0], z: item.position[2], y: item.position[1],
          timeoutMs: 6000, dig: true, bridge,
        }).catch(() => {})
        await rust.request('stop').catch(() => {})
        return { claimed: true }
      }
      st.stall = 0
      st.lastD = Infinity
      // 到跟前：等 1s 判拾取，下一拍看它还在不在
      await S(1000)
      const rest = (await nearbyEntities(8).catch(() => [])).filter((e) => e.name === 'item' && e.id === item.id)
      if (!rest.length) st.picked += 1
      return { claimed: true }
    }
    return { type: 'collect', fn, state: st }
  }

  /** 攻击（定量）：每 tick 找目标→靠近→挥一刀，死亡/打不到 3 次收工。 */
  const attackTask = (p) => {
    const name = String(p.name ?? p.target ?? '').trim()
    if (!name) throw new Error('attack 任务缺 name（生物类型或玩家名）')
    const maxHits = Math.max(1, Number(p.count ?? 20))
    const st = { hits: 0, misses: 0 }
    const norm = normalizeItem(name)
    const fn = async (scheduler, me) => {
      if (!me) return { idle: true }
      if (st.hits >= maxHits) return { done: true, note: `已挥 ${st.hits} 刀` }
      const list = (await nearbyEntities(48).catch(() => []))
        .filter((e) => e.name === norm || e.name === name)
      let target = list[0]
      if (!target) {
        // 玩家目标：快照里玩家不带名字，用 RCON 坐标反查最近 player
        const pp = entityPos ? await entityPos(name).catch(() => null) : null
        if (pp) {
          const players = (await nearbyEntities(48).catch(() => [])).filter((e) => e.name === 'player')
          players.sort((a, b) => Math.hypot(a.position[0] - pp[0], a.position[2] - pp[2])
            - Math.hypot(b.position[0] - pp[0], b.position[2] - pp[2]))
          target = players[0]
        }
      }
      if (!target) {
        st.misses += 1
        return st.misses >= 3 ? { done: true, note: `找不到 ${name}，收工` } : { idle: true }
      }
      st.misses = 0
      const d = Math.hypot(target.position[0] - me[0], target.position[2] - me[2])
      if (d > 2.8) {
        await engine.travel({
          x: target.position[0], z: target.position[2], y: target.position[1],
          timeoutMs: 2500, dig: false,
        }).catch(() => {})
        return { claimed: true }
      }
      await attackEntity(target.id)
      st.hits += 1
      await S(650)  // 攻击冷却
      return { claimed: true }
    }
    return { type: 'attack', fn, state: st }
  }

  /** 进食（一次性）：饿了才吃，否则直接完成。 */
  const eatTask = (p = {}) => {
    const st = { done: false }
    const fn = async () => {
      const s = snap()
      const food = Number(s?.food ?? 20)
      const threshold = Number(p.below ?? engine.cfg.eat_below ?? 14)
      if (food > threshold) return { done: true, note: `不饿（${food}/20）` }
      await eat()
      return { done: true, note: `进食完成（之前 ${food}/20）` }
    }
    return { type: 'eat', fn, state: st }
  }

  /** 自动反击（常驻）：每 tick 巡一次敌对生物，有就靠近挥刀，没有就 idle。 */
  const retaliateTask = (p = {}) => {
    const radius = Number(p.radius ?? engine.cfg.radius ?? 6)
    const st = { kills: 0, last: '' }
    const fn = async (scheduler, me) => {
      if (!me) return { idle: true }
      const health = Number(snap()?.health ?? 20)
      if (health <= engine.cfg.guard_below) return { idle: true }  // 低血不主动招惹
      const list = (await nearbyEntities(48).catch(() => []))
        .filter((e) => HOSTILES.includes(e.name))
        .filter((e) => Math.hypot(e.position[0] - me[0], e.position[2] - me[2]) <= radius)
      if (!list.length) return { idle: true }
      list.sort((a, b) => Math.hypot(a.position[0] - me[0], a.position[2] - me[2])
        - Math.hypot(b.position[0] - me[0], b.position[2] - me[2]))
      const threat = list[0]
      const d = Math.hypot(threat.position[0] - me[0], threat.position[2] - me[2])
      if (d > 2.8) {
        await engine.travel({
          x: threat.position[0], z: threat.position[2], y: threat.position[1],
          timeoutMs: 1800, dig: false,
        }).catch(() => {})
        st.last = `追击 ${threat.name}`
        return { claimed: true }
      }
      await attackEntity(threat.id)
      st.last = `反击 ${threat.name}`
      await S(650)
      return { claimed: true }
    }
    return { type: 'retaliate', fn, state: st }
  }

  /**
   * 备料建房（复合）：清点材料 → 缺的采回来（可选原木换木板）→ 开工盖。
   * 一个 Task 内分三个 phase 推进，每拍只做一小段；外部用 /schedule/tasks
   * 能看到 phase/have/missing 进度。
   *
   * 参数：shape/material/w/h/d/hollow/blueprint（同 build），另加
   *   mine_block  采什么原料（默认同 material；木板自动推 *_log）
   *   per_unit    一份原料出几个成品（木板=4，默认 1）
   *   range/sliceMs 采集搜索半径与单拍时限
   *   at          建筑锚点（build 同款）
   */
  const buildWithMaterialsTask = (p) => {
    const material = normalizeItem(p.material ?? 'stone')
    const isPlanks = /_planks$/.test(material)
    const mineBlock = normalizeItem(p.mineBlock ?? p.mine_block ?? (isPlanks ? material.replace(/_planks$/, '_log') : material))
    const perUnit = Math.max(1, Number(p.perUnit ?? p.per_unit ?? (isPlanks ? 4 : 1)))
    const plan = makeBuildPlan({
      blueprint: p.blueprint ?? null, blocks: p.blocks ?? null, shape: p.shape ?? null,
      material, w: Number(p.w ?? 5), h: Number(p.h ?? 4), d: Number(p.d ?? 5), hollow: Boolean(p.hollow ?? false),
    })
    const total = plan.length
    const st = { phase: 'count', total, have: 0, missing: total, mined: 0, crafted: 0, placed: 0, stall: 0, last: '' }
    // 库存同步有延迟（服务端 give 后 Rust 客户端要等下个背包包才刷新，实机约 1s）：
    // 采/合后别立刻按旧快照判失败，轮询到涨了再算账。
    const pollCount = async (item, baseline, tries = 8) => {
      let v = baseline
      for (let i = 0; i < tries; i++) {
        v = inventoryCount ? await inventoryCount(item).catch(() => v) : v
        if (v > baseline) return v
        await S(400)
      }
      return v
    }
    const fn = async () => {
      // ── 1. 清点库存 ───────────────────────────────────────────────
      if (st.phase === 'count') {
        st.have = inventoryCount ? await inventoryCount(material).catch(() => 0) : 0
        st.missing = Math.max(0, total - st.have)
        st.last = `备料：要 ${total} 块 ${material}，背包 ${st.have}，缺 ${st.missing}`
        st.phase = st.missing > 0 ? 'gather' : 'build'
        return { claimed: true }
      }

      // ── 2. 采集缺失材料 ───────────────────────────────────────────
      if (st.phase === 'gather') {
        st.have = inventoryCount ? await inventoryCount(material).catch(() => st.have) : st.have
        st.missing = Math.max(0, total - st.have)
        if (st.missing <= 0) { st.phase = 'build'; return { claimed: true } }
        if (!mineBlock) {
          return { done: true, note: `缺 ${st.missing} 块 ${material}，但没有可采的原料（mine_block 未给）` }
        }

        // 成品需要合成（如木板）：先采原料，够了再兑换
        if (perUnit > 1) {
          const rawHave = inventoryCount ? await inventoryCount(mineBlock).catch(() => 0) : 0
          const rawNeed = Math.ceil(st.missing / perUnit)
          if (rawHave < rawNeed) {
            const before = st.mined
            const msg = await engine.mine({
              block: mineBlock, count: Math.min(64, rawNeed - rawHave), tool: p.tool ?? null,
              range: Number(p.range ?? 12), timeoutMs: Number(p.sliceMs ?? 12_000),
              dig: p.dig ?? true, bridge: p.bridge ?? null, back: true,
            })
            st.last = msg
            const m = msg.match(/挖到\s*(\d+)\s*\/\s*(\d+)/)
            const gained = m ? Number(m[1]) : 0
            st.mined += gained
            if (st.mined === before) st.stall += 1
            else st.stall = 0
            if (gained > 0) await pollCount(mineBlock, rawHave)
            if (st.stall >= 3) {
              return { done: true, note: `备料中断：采不到 ${mineBlock}（已采 ${st.mined}）：${msg}` }
            }
            return { claimed: true }
          }
          if (!craftItem) {
            return { done: true, note: `原料够了（${mineBlock} ${rawHave}）但行为层没有合成依赖，无法做 ${material}` }
          }
          const beforeHave = st.have
          const times = Math.min(16, rawNeed)
          const msg = await craftItem(material, times)
          st.last = msg
          const afterHave = await pollCount(material, beforeHave)
          // 合成后库存没涨 = 产物给不进去（背包满时 give 会掉地上/被拒），
          // 继续循环只会白烧原料。
          if (afterHave <= beforeHave) {
            return { done: true, note: `合成 ${material} 后背包数量没增加（${beforeHave}→${afterHave}，可能背包满是给不进去）：${msg}` }
          }
          st.crafted += afterHave - beforeHave
          st.have = afterHave
          st.missing = Math.max(0, total - st.have)
          return { claimed: true }
        }

        // 直接采成品方块本身（泥土/圆石/原木…）
        const before = st.mined
        const beforeHave = st.have
        const msg = await engine.mine({
          block: mineBlock, count: Math.min(64, st.missing), tool: p.tool ?? null,
          range: Number(p.range ?? 12), timeoutMs: Number(p.sliceMs ?? 12_000),
          dig: p.dig ?? true, bridge: p.bridge ?? null, back: true,
        })
        st.last = msg
        const m = msg.match(/挖到\s*(\d+)\s*\/\s*(\d+)/)
        const gained = m ? Number(m[1]) : 0
        st.mined += gained
        if (st.mined === before) st.stall += 1
        else st.stall = 0
        if (st.stall >= 3) {
          return { done: true, note: `备料中断：采不到 ${mineBlock}（已采 ${st.mined}/${st.missing + st.have}）：${msg}` }
        }
        // 采到东西后等背包同步，再判断还缺多少（否则会重复多挖）
        st.have = gained > 0
          ? await pollCount(material, beforeHave)
          : (inventoryCount ? await inventoryCount(material).catch(() => st.have + st.mined) : st.have + st.mined)
        st.missing = Math.max(0, total - st.have)
        if (st.missing <= 0) { st.phase = 'build'; return { claimed: true } }
        return { claimed: true }
      }

      // ── 3. 开工盖 ─────────────────────────────────────────────────
      const msg = await engine.build({
        blueprint: p.blueprint ?? null, blocks: p.blocks ?? null, shape: p.shape ?? null,
        material, w: Number(p.w ?? 5), h: Number(p.h ?? 4), d: Number(p.d ?? 5),
        at: p.at ?? null, radius: Number(p.radius ?? 24),
        timeoutMs: Number(p.sliceMs ?? 8000), hollow: Boolean(p.hollow ?? false),
      })
      st.last = msg
      const m = msg.match(/放了\s*(\d+)\s*\/\s*(\d+)/)
      const placed = m ? Number(m[1]) : st.placed
      if (m) st.total = Number(m[2]) || st.total
      const sk = msg.match(/已有\s*(\d+)\s*块跳过/)
      const skipped = sk ? Number(sk[1]) : 0
      const progress = placed + skipped
      if (st.total > 0 && progress >= st.total) {
        return { done: true, note: `备料建房完成（${placed}/${st.total}${skipped ? `，${skipped} 块已有` : ''}，采了 ${st.mined}）` }
      }
      if (progress > st.placed) { st.placed = progress; st.stall = 0 } else { st.stall += 1 }
      if (st.stall >= 3) return { done: true, note: `建房停滞（${progress}/${st.total}）：${msg}` }
      return { claimed: true }
    }
    return { type: 'build_with_materials', fn, state: st }
  }

  const FACTORIES = {
    follow: followTask,
    mine: mineTask,
    build: buildTask,
    build_with_materials: buildWithMaterialsTask,
    collect: collectTask,
    attack: attackTask,
    eat: eatTask,
    retaliate: retaliateTask,
    auto_retaliate: retaliateTask,
  }

  /**
   * 从请求创建任务（/schedule/create 用）。
   * 简单行为返回 { taskIds: [id] }；复合行为（如 follow_and_mine）返回多个 id。
   */
  engine.createTaskFromRequest = (type, priority, params = {}) => {
    const timeoutMs = Number(params?.timeoutMs ?? 0) || undefined
    if (type === 'follow_and_mine') {
      // 复合：跟随为主（高优先级），顺路采集为辅（低优先级）——跟随移动/等待的
      // 空拍里采集任务接手，两件事在同一身体上自动交错。
      // 采集受链接约束：离跟随目标超过 stayWithin（默认 8 格）就停手先跟上，
      // 这样「跟我走，顺便挖」不会挖着挖着把主人甩掉。
      const follow = followTask({ ...params })
      const mine = mineTask({ ...params, back: true })
      const followId = engine.scheduler.create('follow', Number(params.followPriority ?? priority ?? 10),
        follow.fn, null, { state: follow.state, timeoutMs })
      const mineId = engine.scheduler.create('mine', Number(params.minePriority ?? Math.max(1, priority - 5)),
        mine.fn, null, {
          state: mine.state,
          constraints: constraintsFromParams(params, followId),
        })
      return {
        taskIds: [followId, mineId],
        message: `跟随 ${params.name}（${followId}）+ 采集 ${params.block}（${mineId}，距目标 >${Number(params.stayWithin ?? 8)} 格自动让路）`,
      }
    }
    const factory = FACTORIES[type]
    if (!factory) {
      throw new Error(`未知任务类型 ${type}（可用：${Object.keys(FACTORIES).join(' / ')} / follow_and_mine）`)
    }
    const built = factory(params ?? {})
    const id = engine.scheduler.create(type, Number(priority ?? 10), built.fn, null, {
      timeoutMs,
      state: built.state,
      constraints: constraintsFromParams(params),
    })
    return { taskIds: [id], message: `${type} 任务已创建（${id}，优先级 ${priority ?? 10}）` }
  }

  /** 取消所有活跃调度任务（/stop、离开服务器时用）。返回被取消的 id。 */
  engine.cancelAllTasks = (reason = '全部取消') => {
    const ids = engine.scheduler.list().map((t) => t.id)
    for (const id of ids) engine.scheduler.cancel(id, reason)
    return ids
  }

  return engine
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
