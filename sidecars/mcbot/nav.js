// 体素导航：把 Rust 端 `nav` op 吐出的 RLE 体素盒解成可查询网格，跑 A*。
//
// 为什么这么设计（对着旧 travel 的三条病根）：
//  1. 旧 travel 每一拍都靠 `execute if block` 盲探周围，一次 RCON 往返 ~10ms，
//     几拍就把节奏拖成「走两步停一下」。现在一次拿整盒，零网络。
//  2. 旧 travel「卡住就跳」，见坎就蹦。现在每格带碰撞顶高（1/16 格精度）：
//     半砖 8、雪层 2、台阶 16——只有真的要抬一整格才跳。
//  3. 旧 travel 只会朝目标拉直线，撞墙右转 90°，撞多了原地抽。现在真的绕。
//
// 坐标系：runs 按 y → z → x 顺序铺（x 最内层），和 bot.rs 的循环一致。
// palette[0] 恒为空气；u32::MAX 表示区块未加载，一律当墙（view_distance 只有 4，
// 森林/山坡边缘很容易踩到没加载的格子，掉进去就是无限下落）。

const UNLOADED = 4294967295

// 碰撞顶高的三档语义（单位 1/16 格）
export const FULL = 16        // 满块：不可进入
export const STEP_LIMIT = 10  // 抬升不超过这个值（0.625 格）可以直接走上去，不用跳

/**
 * 危险方块：碰撞体是空的（水/火/岩浆/蛛网），按顶高看像是平地，
 * 但走上去就是掉进岩浆或陷在火里。一律当禁区，绕开走。
 * 水里能游，可客户端没有可靠的游泳控制，宁可绕远。
 */
const DANGER = new Set([
  'water', 'lava', 'flowing_water', 'flowing_lava', 'fire', 'soul_fire',
  'campfire', 'soul_campfire', 'magma_block', 'powder_snow', 'cactus',
  'sweet_berry_bush', 'wither_rose', 'cobweb', 'scaffolding',
])
const bareName = (name) => String(name ?? '').replace(/^minecraft:/, '')


/**
 * 解码 `nav` op 的返回值。
 * @returns {{origin:number[], size:number[], w:number, h:number,
 *   top:(x:number,y:number,z:number)=>number,
 *   name:(x:number,y:number,z:number)=>string|null,
 *   inBox:(x:number,y:number,z:number)=>boolean,
 *   feet:number[], pos:number[], onGround:boolean, eye:number, unloaded:number}}
 */
export function decodeGrid(payload) {
  if (!payload || !Array.isArray(payload.origin) || !Array.isArray(payload.runs)) {
    throw new Error('nav 返回格式不对（缺 origin/runs）')
  }
  const origin = payload.origin.slice(0, 3).map(Number)
  const size = payload.size.slice(0, 3).map(Number)
  const w = size[0]
  const h = size[1]
  const d = size[2]
  const [ox, oy, oz] = origin
  const palette = payload.palette ?? [['minecraft:air', 0]]

  const tops = new Uint8Array(palette.length)
  const names = new Array(palette.length)
  for (let i = 0; i < palette.length; i++) {
    const entry = palette[i]
    tops[i] = Math.max(0, Math.min(16, Math.round(Number(entry?.[1] ?? 0))))
    names[i] = entry?.[0] ?? null
  }

  const n = w * h * d
  const top = new Uint8Array(n)      // 每格碰撞顶高
  const pal = new Int32Array(n)      // 每格调色板下标；-1 = 未加载
  const haz = new Uint8Array(n)      // 1 = 危险方块（水/火/岩浆…），不许落脚
  pal.fill(-1)

  // 按调色板槽预判一次，避免每格重复剥前缀
  const slotHaz = new Uint8Array(palette.length)
  for (let i = 0; i < palette.length; i++) slotHaz[i] = DANGER.has(bareName(names[i])) ? 1 : 0

  let cursor = 0
  for (const run of payload.runs) {
    const slot = run[0]
    const count = run[1]
    if (slot === UNLOADED) {
      // 未加载：保持 pal = -1 且 top 记满块（不可通行），游标照走
      for (let k = 0; k < count && cursor < n; k++, cursor++) top[cursor] = FULL
      continue
    }
    const tall = tops[slot] ?? FULL
    const h = slotHaz[slot]
    for (let k = 0; k < count && cursor < n; k++, cursor++) {
      top[cursor] = tall
      pal[cursor] = slot
      if (h) haz[cursor] = 1
    }
  }
  // runs 没铺满（边界情况）：剩下的当墙，宁可不走也别掉虚空
  for (; cursor < n; cursor++) top[cursor] = FULL

  const inBox = (x, y, z) => {
    const ix = x - ox
    const iy = y - oy
    const iz = z - oz
    return ix >= 0 && ix < w && iy >= 0 && iy < h && iz >= 0 && iz < d
  }
  const at = (x, y, z) => {
    const ix = x - ox
    const iy = y - oy
    const iz = z - oz
    if (ix < 0 || ix >= w || iy < 0 || iy >= h || iz < 0 || iz >= d) return -1
    return (iy * d + iz) * w + ix
  }

  return {
    origin,
    size,
    w,
    h,
    d,
    top: (x, y, z) => {
      const i = at(x, y, z)
      // 盒外一律当满块：视野边缘之外的东西看不见，不许走
      return i < 0 ? FULL : top[i]
    },
    loaded: (x, y, z) => {
      const i = at(x, y, z)
      return i >= 0 && pal[i] >= 0
    },
    name: (x, y, z) => {
      const i = at(x, y, z)
      if (i < 0 || pal[i] < 0) return null
      return names[pal[i]]
    },
    // 危险方块（水/岩浆/火/仙人掌/蛛网…）：看着能站，站进去就掉血
    danger: (x, y, z) => {
      const i = at(x, y, z)
      return i >= 0 && haz[i] === 1
    },
    // 每格 16 档顶高直查（A* 内循环用，绕过边界判断）
    _top: top,
    _at: at,
    // 本盒里出现过哪些方块名（去重）：上层用来判断「缺哪种工具」，
    // 不用逐格 RCON 探针（实机 11-07：缺镐时 A* 判死路，报「缺镐」比「两轮不破」有用）。
    palette: names.map((n) => n),
    inBox,
    feet: (payload.feet ?? []).map(Number),
    pos: (payload.pos ?? []).map(Number),
    onGround: !!payload.on_ground,
    eye: Number(payload.eye ?? 1.62),
    unloaded: Number(payload.unloaded ?? 0),
  }
}

/** 机器人身体能不能占住这一格（顶高 < 满块，且不是水/岩浆/火）。 */
export function passable(grid, x, y, z) {
  if (grid.danger(x, y, z)) return false
  return grid.top(x, y, z) < FULL
}

/**
 * 站在 (x,z) 这一列、脚在 y 档时，真正落脚的高度档。
 * 返回 null = 这一列往下都是空的（虚空/悬崖底不可见），或者整列是危险方块。
 *
 * 语义：脚所在格有部分占位（半砖 8、雪 2）→ 脚就落在该格（返回 y）；
 * 该格是满块 → 站在它顶上（返回 y+1）。
 * 危险方块（水/岩浆/火/岩浆块/仙人掌）碰撞体常常是空的，按顶高看像平地，
 * 但落脚就是掉进去烧起来——整列判死，让上层当悬崖处理（有桥才搭桥跨过）。
 */
export function groundLevel(grid, x, z, y) {
  for (let yy = y; yy >= grid.origin[1]; yy--) {
    if (grid.danger(x, yy, z)) return null
    const t = grid.top(x, yy, z)
    if (t === 0) continue
    if (t >= 12) return yy + 1
    return yy
  }
  return null
}

/** 从 (x,z) 往上找一格能站的地方（用于把目标点吸附到可走位置）。 */
export function snapToStandable(grid, x, z, y, span = 6) {
  for (let r = 0; r <= span; r++) {
    for (const yy of [y + r, y - r]) {
      if (yy === y + r && r === 0 && false) continue
      const g = groundLevel(grid, x, z, yy)
      if (g === null) continue
      if (Math.abs(g - y) > span) continue
      if (passable(grid, x, g, z) && passable(grid, x, g + 1, z)) return g
    }
  }
  return null
}

/** 最小二叉堆（A* 开放表）。 */
class Heap {
  constructor() { this.k = []; this.f = [] }
  get size() { return this.k.length }
  push(key, f) {
    const k = this.k
    const fs = this.f
    let i = k.length
    k.push(key)
    fs.push(f)
    while (i > 0) {
      const p = (i - 1) >> 1
      if (fs[p] <= fs[i]) break
      ;[k[p], k[i]] = [k[i], k[p]]
      ;[fs[p], fs[i]] = [fs[i], fs[p]]
      i = p
    }
  }
  pop() {
    const k = this.k
    const fs = this.f
    const topK = k[0]
    const lastK = k.pop()
    const lastF = fs.pop()
    if (k.length) {
      k[0] = lastK
      fs[0] = lastF
      let i = 0
      for (;;) {
        const l = i * 2 + 1
        const r = l + 1
        let m = i
        if (l < fs.length && fs[l] < fs[m]) m = l
        if (r < fs.length && fs[r] < fs[m]) m = r
        if (m === i) break
        ;[k[m], k[i]] = [k[i], k[m]]
        ;[fs[m], fs[i]] = [fs[i], fs[m]]
        i = m
      }
    }
    return topK
  }
}

/**
 * A* 寻路。
 * @param grid decodeGrid 的产物
 * @param start {x,y,z} 脚所在格（整数）
 * @param goal  {x,z,y?} 目标；不给 y 就找该柱位附近任意能站的地方
 * @param opts  {maxDrop=3, dig=true, bridge=false, maxNodes=120000, digBudget=24}
 * @returns {{path:Array<{x,y,z,act}>, cost:number, digs:number, bridges:number,
 *   partial:boolean, end:{x,y,z}, reason?:string}}
 */
export function findPath(grid, start, goal, opts = {}) {
  const maxDrop = opts.maxDrop ?? 3
  const dig = opts.dig !== false
  const bridge = !!opts.bridge
  const maxNodes = opts.maxNodes ?? 120_000
  const digBudget = opts.digBudget ?? 24

  const sx = Math.floor(start.x)
  const sy = Math.floor(start.y)
  const sz = Math.floor(start.z)
  const gx = Math.floor(goal.x)
  const gz = Math.floor(goal.z)
  const goalY = Number.isFinite(goal.y) ? Math.floor(goal.y) : null

  // 目标吸附：目标柱位可能被树/石占满，找最近的可站格
  let goalCells = null
  if (goalY !== null) {
    const g = snapToStandable(grid, gx, gz, goalY, 8)
    if (g !== null) goalCells = new Set([`${gx},${g},${gz}`])
  }
  if (!goalCells) {
    // 目标柱位被占（墙/树）时，目标周围一圈能站的地方都算「到附近了」——
    // travel 的到达判定是水平 1.3 格，只认死柱位会明明走到旁边还说没到。
    // 取 r=1 的九宫格（最远 1 格轴距，落在 1.3 格到达圈内），
    // 每列可站高度都收进来（楼上有路也认）。
    const cells = new Set()
    for (let dx = -1; dx <= 1; dx++) {
      for (let dz = -1; dz <= 1; dz++) {
        const cx = gx + dx
        const cz = gz + dz
        for (const base of [sy + 2, sy - 2]) {
          const g = snapToStandable(grid, cx, cz, base, 8)
          if (g !== null) {
            cells.add(`${cx},${g},${cz}`)
            break
          }
        }
      }
    }
    if (cells.size) goalCells = cells
  }
  const goalDist = (x, z) => Math.hypot(x - gx, z - gz)
  const heuristic = (x, y, z) => {
    const d = goalDist(x, z)
    return d + (goalY !== null ? Math.abs(y - goalY) * 0.6 : 0)
  }
  if (goalCells && goalCells.has(`${sx},${sy},${sz}`)) {
    return { path: [], cost: 0, digs: 0, bridges: 0, partial: false, end: { x: sx, y: sy, z: sz } }
  }

  const key = (x, y, z) => `${x},${y},${z}`
  const open = new Heap()
  const gScore = new Map()
  const came = new Map()
  const sKey = key(sx, sy, sz)
  gScore.set(sKey, 0)
  open.push(sKey, heuristic(sx, sy, sz))
  // 挖取许可：按方块名判（床岩/命令块之类永远不可挖）。没给回调=全允许。
  const canDig = typeof opts.canDig === 'function' ? opts.canDig : () => true
  const digOk = (x, y, z) => { const name = grid.name(x, y, z); return !!name && canDig(name) }

  let nodes = 0
  let bestKey = sKey
  let bestScore = heuristic(sx, sy, sz)
  let bestY = sy
  let reached = null

  const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]]
  // 该格是不是实心满块（可挖的障碍）
  const solidAt = (x, y, z) => grid.inBox(x, y, z) && grid.top(x, y, z) >= FULL

  while (open.size) {
    if (++nodes > maxNodes) break
    const cur = open.pop()
    const [cx, cy, cz] = cur.split(',').map(Number)
    const g = gScore.get(cur)
    if (g === undefined) continue
    const hh = heuristic(cx, cy, cz)
    if (hh < bestScore - 1e-9) { bestScore = hh; bestKey = cur; bestY = cy }
    if (goalCells ? goalCells.has(cur) : goalDist(cx, cz) < 1.01) { reached = cur; break }

    for (const [dx, dz] of DIRS) {
      const nx = cx + dx
      const nz = cz + dz
      if (!grid.inBox(nx, cy, nz)) continue

      // ① 同层挡路：前方柱位在「我们脚这一层/头这一层」是实心 → 挖隧道。
      //    这是最常见的一堵墙（脚格+头格两块）。挖掉后站位不变（脚仍踩 cy-1）。
      //    注意不能拿 groundLevel 判：它只看柱顶，两格厚的墙会被当成
      //    「两格高的台阶」，永远规划不出隧道（实测 11-06：卡在墙前不挖）。
      const feetSolid = solidAt(nx, cy, nz)
      const headSolid = solidAt(nx, cy + 1, nz)
      if (feetSolid || headSolid) {
        // 一格台阶：脚格实心但头格空，且跳上去后身体两格净空 → 直接跳。
        // 别把整面墙都当成「必须挖隧道」——没镐时这一步会把贴脸的矮台判死
        //（实机 11-08：崖上掉落物只差一格高，A* 报缺镐原地跳，永远上不去）。
        if (feetSolid && !headSolid
          && passable(grid, nx, cy + 1, nz) && passable(grid, nx, cy + 2, nz)) {
          const ng = g + 2.2
          const k = key(nx, cy + 1, nz)
          if (!(gScore.get(k) <= ng)) {
            gScore.set(k, ng)
            came.set(k, { from: cur, act: 'jump' })
            open.push(k, ng + heuristic(nx, cy + 1, nz))
          }
          continue
        }
        if (!dig) continue
        const willDig = []
        if (feetSolid) willDig.push([nx, cy, nz])
        if (headSolid) willDig.push([nx, cy + 1, nz])
        if (!willDig.every(([x, y, z]) => digOk(x, y, z))) continue
        // 挖掉脚格就没有脚下实地了 → 会掉下去，这条路不走
        if (feetSolid && !solidAt(nx, cy - 1, nz)) continue
        const ng = g + 1 + willDig.length * 6
        const k = key(nx, cy, nz)
        if (!(gScore.get(k) <= ng)) {
          gScore.set(k, ng)
          came.set(k, { from: cur, act: willDig.length > 1 ? 'dig2' : 'dig', dig: willDig })
          open.push(k, ng + heuristic(nx, cy, nz))
        }
        continue
      }

      // ② 空柱：找落脚高度
      const gy = groundLevel(grid, nx, nz, cy + 1)
      if (gy === null) {
        // 前方是虚空：允许搭桥
        if (!bridge) continue
        const by = cy
        if (!passable(grid, nx, by, nz) || !passable(grid, nx, by + 1, nz)) continue
        const k = key(nx, by, nz)
        const ng = g + 8
        if (!(gScore.get(k) <= ng)) {
          gScore.set(k, ng)
          came.set(k, { from: cur, act: 'bridge' })
          open.push(k, ng + heuristic(nx, by, nz))
        }
        continue
      }
      const rise = gy - cy
      // 抬升：<=0.625 格直接走；1 格要跳（且头顶得有 2 格净空）
      if (rise > 1) {
        if (!dig) continue
        // 高于两格：挖掉这根柱子的顶块（gy-1），把表面降一格再跳上去。
        // （挖顶块而不是挖脚下：挖脚下会连地基一起掉，人跟着掉。）
        const cutY = gy - 1
        if (!grid.inBox(nx, cutY, nz)) continue
        if (!digOk(nx, cutY, nz)) continue
        // 降一格后要有落脚实地（cutY-1 实心）
        if (!solidAt(nx, cutY - 1, nz)) continue
        const g2 = cutY
        const ng = g + 6
        const k = key(nx, g2, nz)
        if (!(gScore.get(k) <= ng)) {
          gScore.set(k, ng)
          came.set(k, { from: cur, act: 'dig', dig: [[nx, cutY, nz]] })
          open.push(k, ng + heuristic(nx, g2, nz))
        }
        continue
      }
      if (rise < -maxDrop) continue
      // 身体净空：脚格 + 头格（半砖/雪这类占位不算挡）
      if (!passable(grid, nx, gy, nz) || !passable(grid, nx, gy + 1, nz)) {
        if (!dig) continue
        const willDig = []
        for (const yy of [gy, gy + 1]) {
          if (solidAt(nx, yy, nz)) willDig.push([nx, yy, nz])
        }
        if (!willDig.length) continue
        if (!willDig.every(([x, y, z]) => digOk(x, y, z))) continue
        // 挖完这一格后落脚点会变（挖掉脚下方块会掉下去），重新取
        const after = groundLevel(grid, nx, nz, gy + 1)
        if (after === null || Math.abs(after - gy) > 1) continue
        const ng = g + 1 + willDig.length * 6
        const k = key(nx, after, nz)
        if (!(gScore.get(k) <= ng)) {
          gScore.set(k, ng)
          came.set(k, { from: cur, act: willDig.length > 1 ? 'dig2' : 'dig', dig: willDig })
          open.push(k, ng + heuristic(nx, after, nz))
        }
        continue
      }
      // 正常走：落地台阶
      const drop = cy - gy
      const cost = rise > 0 ? 2.2 : 1 + drop * 0.45
      const k = key(nx, gy, nz)
      const ng = g + cost
      if (!(gScore.get(k) <= ng)) {
        gScore.set(k, ng)
        came.set(k, { from: cur, act: rise > 0 ? 'jump' : drop > 0 ? 'drop' : 'walk' })
        open.push(k, ng + heuristic(nx, gy, nz))
      }
    }
  }

  const endKey = reached ?? bestKey
  if (!reached && goalDist(...endKey.split(',').map((v, i) => (i === 0 || i === 2 ? Number(v) : 0))) > 1.01) {
    // 走不到：返回能走到的最远点，让上层决定换目标/加时限
  }
  const path = []
  let cur = endKey
  let digsTotal = 0
  let bridgesTotal = 0
  while (cur && cur !== sKey) {
    const [x, y, z] = cur.split(',').map(Number)
    const link = came.get(cur)
    if (!link) break
    const step = { x, y, z, act: link.act }
    if (link.dig) {
      step.dig = link.dig
      digsTotal += link.dig.length
    }
    if (link.act === 'bridge') bridgesTotal++
    path.push(step)
    cur = link.from
  }
  path.reverse()

  return {
    path,
    cost: gScore.get(endKey) ?? 0,
    digs: digsTotal,
    bridges: bridgesTotal,
    partial: !reached,
    reached: !!reached,
    end: endKey.split(',').map(Number),
    endY: bestY,
    goalCells: goalCells ? [...goalCells] : null,
    reason: reached ? null : (nodes > maxNodes ? '搜索节点超限' : '没有可行路线'),
  }
}

/**
 * 把一条格子路径简化成「直段」航点表：连续同向的格子合并，只保留拐点。
 * 执行器按航点走，避免每格都改朝向导致的抖动。
 */
export function simplify(path) {
  if (path.length <= 1) return path.slice()
  const out = [path[0]]
  let dir = null
  for (let i = 1; i < path.length; i++) {
    const p = path[i]
    const q = path[i - 1]
    const nd = `${p.x - q.x},${p.z - q.z}`
    const isAction = p.act && p.act !== 'walk' && p.act !== 'drop'
    if (dir === null || nd !== dir || isAction || p.dig) {
      if (i > 1 && out[out.length - 1] !== q) out.push(q)
      dir = nd
    }
    out.push(p)
    if (isAction) dir = null
  }
  return out
}

/**
 * 在体素网格里按方块名找目标（替代 RCON 逐格探测，零网络）。
 * @param grid decodeGrid 的产物
 * @param names 要找的方块名集合（不带 minecraft: 前缀）
 * @param origin 扫描中心 {x,y,z}
 * @param radius 水平半径（格）
 * @param dyMin/dyMax 垂直偏移范围
 * @param limit 最多返回几处
 * @returns {{hits:Array<[x,y,z]>, scanned:number, unloaded:number}}
 */
export function searchBlocks(grid, names, origin, { radius = 16, dyMin = -11, dyMax = 12, limit = 32 } = {}) {
  const wanted = new Set([...names].map((n) => bareName(n)))
  const hits = []
  const [ox, oy, oz] = origin
  let scanned = 0
  let unloaded = 0
  for (let dx = -radius; dx <= radius; dx++) {
    for (let dz = -radius; dz <= radius; dz++) {
      // 方形盘（不用圆形，省一半格子）
      for (let dy = dyMin; dy <= dyMax; dy++) {
        const x = ox + dx
        const y = oy + dy
        const z = oz + dz
        if (!grid.inBox(x, y, z)) { unloaded++; continue }
        scanned++
        const name = grid.name(x, y, z)
        if (!name) continue
        if (!wanted.has(bareName(name))) continue
        hits.push([x, y, z])
        if (hits.length >= limit) return { hits, scanned, unloaded }
      }
    }
  }
  // 近的先报
  hits.sort((a, b) => ((a[0] - ox) ** 2 + (a[2] - oz) ** 2 + (a[1] - oy) ** 2) - ((b[0] - ox) ** 2 + (b[2] - oz) ** 2 + (b[1] - oy) ** 2))
  return { hits, scanned, unloaded }
}

/**
 * 找一个可站的落脚点（给建筑/采集用）：从 (x,z) 附近找能站人的地面高度。
 * 返回 {x,y,z} 或 null。
 */
export function standableNear(grid, x, z, { span = 3, preferY = null } = {}) {
  const cand = []
  for (let dx = -span; dx <= span; dx++) {
    for (let dz = -span; dz <= span; dz++) {
      const cx = x + dx
      const cz = z + dz
      for (const base of (preferY === null ? [0] : [preferY, preferY + 1, preferY - 1])) {
        const g = groundLevel(grid, cx, cz, base)
        if (g === null) continue
        if (!passable(grid, cx, g, cz) || !passable(grid, cx, g + 1, cz)) continue
        cand.push({ x: cx, y: g, z: cz, d: Math.hypot(dx, dz) })
        break
      }
    }
  }
  if (!cand.length) return null
  cand.sort((a, b) => a.d - b.d)
  return { x: cand[0].x, y: cand[0].y, z: cand[0].z }
}

// ── 放置瞄准（place aiming）───────────────────────────────────────────────
//
// MC 放置的规则：右键命中「支撑方块的某个面」，新方块长在被点方块旁边。
// 想把方块放进 (x,y,z)：
//   · 底面支撑 → 必须命中 (x,y-1,z) 的**顶面**（射线自上而下打在顶面上）；
//   · 侧面支撑 → 必须命中同层邻居 (x±1,y,z)/(x,y,z±1) 的**侧面**。
// 盲瞄（凭距离猜俯角）必然时灵时不灵：站远了射线越过目标格打到别处，
// 物品被消耗但方块没出来。这里用体素网格做**视线遮挡检查**，
// 在候选站位里挑一个真的能看见支撑面的。

/** 射线是否被格子挡住（不含终点本身）。从 a 到 b 走 DDA。 */
export function lineOfSight(grid, ax, ay, az, bx, by, bz) {
  const dx = bx - ax
  const dy = by - ay
  const dz = bz - az
  const len = Math.hypot(dx, dy, dz)
  if (len < 1e-6) return true
  const steps = Math.ceil(len * 3)
  for (let i = 1; i < steps; i++) {
    const t = i / steps
    const x = Math.floor(ax + dx * t)
    const y = Math.floor(ay + dy * t)
    const z = Math.floor(az + dz * t)
    // 终点所在格（支撑块本身）不算遮挡
    if (x === Math.floor(bx) && y === Math.floor(by) && z === Math.floor(bz)) continue
    if (grid.top(x, y, z) >= FULL) return false
  }
  return true
}

/**
 * 给目标格找一组可用的「站位 + 瞄点」。
 * 返回 null 表示没有可行方案；否则 { stand:[x,y,z], aim:[x,y,z], kind:'bottom'|'side' }。
 *   stand 是脚该站的位置（格心），aim 是眼睛该瞄的**支撑面**上的点。
 */
export function planPlacement(grid, target, opts = {}) {
  const [tx, ty, tz] = target
  const { radius = 4 } = opts
  const eyeH = opts.eyeHeight ?? 1.62

  // 盒外的格子 top() 返回 FULL（当墙），不能拿来当支撑——那会把空气误判成实心，
  // 瞄一个空气格，射线自然打空，「物品被消耗但方块没出来」。
  const solid = (x, y, z) => grid.inBox(x, y, z) && grid.loaded(x, y, z) && grid.top(x, y, z) >= FULL

  const candidates = []
  // ① 底面支撑：命中 (tx,ty-1,tz) 的顶面 → 瞄它顶面中心
  if (solid(tx, ty - 1, tz) && !grid.danger(tx, ty - 1, tz)) {
    candidates.push({ kind: 'bottom', support: [tx, ty - 1, tz], aim: [tx + 0.5, ty, tz + 0.5] })
  }
  // ② 侧面支撑：命中同层邻居的侧面 → 瞄两格公共面上的点
  for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    const sx = tx + dx
    const sz = tz + dz
    if (!solid(sx, ty, sz)) continue
    if (grid.danger(sx, ty, sz)) continue
    // 公共面中心：dx=1 → 面在 x=tx+1；dx=-1 → 面在 x=tx
    const aim = [
      tx + (dx === 1 ? 1.0 : dx === -1 ? 0.0 : 0.5),
      ty + 0.5,
      tz + (dz === 1 ? 1.0 : dz === -1 ? 0.0 : 0.5),
    ]
    candidates.push({ kind: 'side', support: [sx, ty, sz], aim })
  }
  if (!candidates.length) return null

  // 站位候选：目标格周围一圈 + 稍远一点（侧放需要退开一点才看得见面）
  const stands = []
  for (let r = 1; r <= radius; r++) {
    for (let dx = -r; dx <= r; dx++) {
      for (let dz = -r; dz <= r; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue
        for (const dy of [0, 1, -1]) {
          const x = tx + dx
          const y = ty + dy
          const z = tz + dz
          if (!passable(grid, x, y, z)) continue
          if (!passable(grid, x, y + 1, z)) continue
          stands.push([x, y, z])
        }
      }
    }
  }
  if (!stands.length) return null

  let best = null
  for (const cand of candidates) {
    for (const st of stands) {
      const ex = st[0] + 0.5
      const ey = st[1] + eyeH
      const ez = st[2] + 0.5
      // 目标格本身不能站人（会挡住放置）
      if (st[0] === tx && st[1] === ty && st[2] === tz) continue
      // 支撑块本身不能站（会挡住射线）
      if (st[0] === cand.support[0] && st[1] === cand.support[1] && st[2] === cand.support[2]) continue
      if (!lineOfSight(grid, ex, ey, ez, cand.aim[0], cand.aim[1], cand.aim[2])) continue
      // 仰角别太刁（贴底面时射线必须自上而下），也别太平（会被中间格挡）
      const horiz = Math.hypot(cand.aim[0] - ex, cand.aim[2] - ez)
      const pitch = Math.atan2(ey - cand.aim[1], horiz)
      if (pitch < 0.35) continue
      const score = Math.abs(st[1] - ty) * 2 + horiz + (cand.kind === 'bottom' ? 0 : 1)
      if (!best || score < best.score) best = { stand: st, aim: cand.aim, kind: cand.kind, pitch, score }
    }
  }
  if (!best) return null
  return { stand: best.stand, aim: best.aim, kind: best.kind, pitch: best.pitch }
}
