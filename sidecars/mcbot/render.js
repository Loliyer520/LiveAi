/**
 * 纯 JS 世界渲染：俯视高度图 + 朝向剖面图，双拼一张 PNG（node:zlib 编码，
 * 零原生依赖）。给视觉模型看的，不追求像素级还原——能读出地形、矿、水、
 * 实体方位就够。颜色表手写常用方块 + 按名字关键字的启发式兜底。
 */

import { deflateSync } from 'node:zlib'

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

/** 无压缩辅助区：CRC32 查表 */
const CRC_TABLE = (() => {
  const table = new Int32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c
  }
  return table
})()

function crc32(buffer) {
  let c = 0xffffffff
  for (let i = 0; i < buffer.length; i++) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk(type, data) {
  const out = Buffer.alloc(12 + data.length)
  out.writeUInt32BE(data.length, 0)
  out.write(type, 4, 'ascii')
  data.copy(out, 8)
  out.writeUInt32BE(crc32(out.subarray(4, 8 + data.length)), 8 + data.length)
  return out
}

/** RGBA → PNG（filter 0，zlib level 6 够用） */
export function encodePng(width, height, rgba) {
  const stride = width * 4
  const raw = Buffer.alloc((stride + 1) * height)
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride)
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // RGBA
  return Buffer.concat([
    PNG_SIGNATURE,
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw, { level: 6 })),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

// ---------------------------------------------------------------------------
// 调色板
// ---------------------------------------------------------------------------

/** 手写颜色表：常用地形/建材/矿。值取自游戏贴图的主观平均色。 */
const PALETTE = {
  grass_block: [110, 168, 68],
  dirt: [134, 96, 67],
  dirt_path: [148, 122, 65],
  coarse_dirt: [119, 85, 59],
  rooted_dirt: [96, 70, 48],
  podzol: [91, 64, 36],
  mycelium: [111, 99, 105],
  mud: [60, 57, 58],
  stone: [125, 125, 125],
  cobblestone: [110, 110, 110],
  deepslate: [80, 80, 84],
  cobbled_deepslate: [74, 74, 78],
  granite: [149, 103, 85],
  diorite: [188, 188, 191],
  andesite: [136, 136, 137],
  calcite: [223, 223, 222],
  tuff: [108, 109, 102],
  bedrock: [84, 84, 84],
  obsidian: [21, 18, 30],
  crying_obsidian: [42, 30, 74],
  sand: [219, 207, 163],
  red_sand: [190, 102, 33],
  sandstone: [216, 203, 155],
  gravel: [127, 124, 123],
  clay: [160, 166, 178],
  snow: [249, 254, 254],
  snow_block: [249, 254, 254],
  powder_snow: [245, 250, 250],
  ice: [145, 183, 253],
  packed_ice: [141, 180, 250],
  blue_ice: [116, 167, 253],
  oak_leaves: [96, 144, 56],
  spruce_leaves: [57, 97, 57],
  birch_leaves: [107, 141, 66],
  jungle_leaves: [77, 122, 27],
  acacia_leaves: [85, 130, 56],
  dark_oak_leaves: [69, 106, 34],
  azalea_leaves: [90, 128, 48],
  cherry_leaves: [233, 177, 201],
  mangrove_leaves: [46, 128, 32],
  oak_log: [102, 81, 50],
  spruce_log: [58, 37, 16],
  birch_log: [215, 203, 187],
  jungle_log: [87, 111, 33],
  acacia_log: [103, 96, 86],
  dark_oak_log: [51, 39, 21],
  mangrove_log: [59, 44, 33],
  cherry_log: [54, 32, 40],
  crimson_stem: [89, 33, 66],
  warped_stem: [42, 94, 96],
  oak_planks: [162, 130, 78],
  spruce_planks: [114, 84, 48],
  birch_planks: [192, 175, 121],
  jungle_planks: [160, 115, 80],
  acacia_planks: [168, 90, 50],
  dark_oak_planks: [66, 43, 21],
  cherry_planks: [226, 179, 194],
  crafting_table: [138, 92, 49],
  furnace: [105, 105, 105],
  chest: [162, 114, 45],
  torch: [255, 200, 90],
  lantern: [255, 190, 100],
  glowstone: [255, 217, 130],
  sea_lantern: [172, 199, 190],
  lava: [255, 120, 20],
  water: [63, 118, 228],
  coal_ore: [115, 115, 115],
  deepslate_coal_ore: [80, 80, 82],
  iron_ore: [140, 118, 96],
  deepslate_iron_ore: [96, 84, 74],
  copper_ore: [148, 103, 75],
  gold_ore: [146, 122, 75],
  deepslate_gold_ore: [90, 80, 62],
  redstone_ore: [130, 70, 66],
  deepslate_redstone_ore: [88, 56, 55],
  emerald_ore: [80, 158, 80],
  deepslate_emerald_ore: [70, 116, 72],
  lapis_ore: [80, 100, 140],
  deepslate_lapis_ore: [64, 78, 108],
  diamond_ore: [95, 148, 148],
  deepslate_diamond_ore: [76, 112, 114],
  netherrack: [97, 38, 38],
  nether_bricks: [44, 22, 26],
  soul_sand: [81, 62, 50],
  soul_soil: [75, 57, 46],
  glow_lichen: [137, 168, 173],
  end_stone: [219, 222, 158],
  purpur_block: [167, 125, 167],
  nether_quartz_ore: [111, 77, 77],
  ancient_debris: [98, 66, 57],
  brick_block: [150, 97, 83],
  stone_bricks: [122, 122, 122],
  moss_block: [90, 124, 46],
  hay_block: [199, 155, 15],
  pumpkin: [196, 118, 21],
  melon: [111, 144, 31],
  bookshelf: [154, 124, 79],
  tnt: [186, 68, 42],
  slime_block: [111, 192, 91],
  honey_block: [251, 167, 50],
  amethyst_block: [133, 97, 191],
  budding_amethyst: [133, 97, 191],
  copper_block: [192, 108, 75],
  iron_block: [220, 220, 220],
  gold_block: [246, 208, 61],
  diamond_block: [98, 219, 214],
  emerald_block: [54, 216, 113],
  lapis_block: [31, 64, 188],
  redstone_block: [168, 31, 31],
  coal_block: [21, 21, 22],
  netherite_block: [68, 62, 63],
  spawner: [34, 44, 44],
  farmland: [127, 88, 55],
  wheat: [180, 168, 60],
  carrots: [90, 130, 40],
  potatoes: [100, 140, 60],
  short_grass: [110, 168, 68],
  grass: [110, 168, 68],
  tall_grass: [100, 158, 62],
  fern: [90, 148, 58],
  dandelion: [230, 210, 60],
  poppy: [210, 60, 50],
  cornflower: [70, 100, 200],
  sunflower: [235, 205, 60],
  sugar_cane: [140, 190, 100],
  bamboo: [120, 170, 70],
  cactus: [80, 130, 60],
  vine: [70, 120, 50],
  glass: [200, 230, 240],
  tinted_glass: [46, 40, 52],
  sea_pickle: [80, 110, 60],
  mossy_cobblestone: [106, 120, 90],
  magenta_glazed_terracotta: [168, 92, 155],
  terracotta: [152, 94, 68],
  white_terracotta: [209, 178, 161],
  target: [220, 200, 190],
  lodestone: [137, 130, 126],
  respawn_anchor: [70, 40, 90],
  beehive: [172, 132, 79],
  beacon: [120, 230, 220],
  shroomlight: [240, 130, 60],
  froglights: [140, 210, 180],
}

const WOOL_COLORS = {
  white: [233, 236, 239], orange: [240, 118, 19], magenta: [189, 68, 179],
  light_blue: [58, 175, 217], yellow: [248, 197, 39], lime: [112, 185, 25],
  pink: [237, 141, 172], gray: [62, 68, 71], light_gray: [142, 142, 134],
  cyan: [21, 137, 145], purple: [121, 42, 172], blue: [53, 57, 157],
  brown: [114, 71, 40], green: [84, 109, 27], red: [161, 39, 34], black: [20, 21, 25],
}

for (const [color, rgb] of Object.entries(WOOL_COLORS)) {
  PALETTE[`${color}_wool`] = rgb
  PALETTE[`${color}_concrete`] = rgb
  PALETTE[`${color}_terracotta`] = rgb.map((v) => Math.round(v * 0.72))
  PALETTE[`${color}_stained_glass`] = rgb
}

/** 名字启发式兜底：表里没有的方块按关键字猜个颜色。 */
function guessColor(name) {
  if (/_log$|_stem$|_wood$/.test(name)) return [110, 86, 55]
  if (/_leaves$|_azalea_leaves_replaceable/.test(name)) return [90, 138, 52]
  if (/^.*_planks$/.test(name)) return [160, 128, 80]
  if (/ore$/.test(name)) {
    if (name.includes('deepslate')) return [82, 82, 86]
    if (name.includes('nether')) return [100, 50, 50]
    return [125, 125, 125]
  }
  if (name.includes('deepslate')) return [80, 80, 84]
  if (name.includes('blackstone')) return [45, 38, 43]
  if (name.includes('basalt')) return [70, 68, 70]
  if (name.includes('nether')) return [95, 40, 40]
  if (name.includes('coral')) return [200, 90, 120]
  if (name.includes('copper')) return [190, 110, 78]
  if (name.includes('waxed')) return guessColor(name.replace('waxed_', ''))
  if (/^.*_door$|^.*_trapdoor$/.test(name)) return [130, 96, 58]
  if (/^.*_fence$|^.*_fence_gate$|^.*_wall$/.test(name)) return [125, 125, 125]
  if (/^.*_sign$|^.*_button$|^.*_pressure_plate$|^.*_slab$|^.*_stairs$/.test(name)) return [140, 112, 70]
  if (name.includes('moss')) return [90, 124, 46]
  if (name.includes('sculk')) return [16, 34, 42]
  if (/^(.*?)_carpet$/.test(name)) return WOOL_COLORS[name.replace('_carpet', '')] ?? [180, 180, 180]
  if (/^.*concrete_powder$/.test(name)) {
    const base = name.replace('_concrete_powder', '')
    return WOOL_COLORS[base] ?? [180, 180, 180]
  }
  return [150, 150, 150]
}

function blockColor(name) {
  const known = PALETTE[name]
  if (known) return known
  return guessColor(name)
}

const AIR_NAMES = new Set(['air', 'cave_air', 'void_air'])
const TRANSPARENT_THIN = new Set(['short_grass', 'grass', 'tall_grass', 'fern', 'large_fern', 'dead_bush', 'sweet_berry_bush', 'wheat', 'carrots', 'potatoes', 'beetroots', 'torch', 'wall_torch', 'soul_torch', 'redstone_torch', 'rail', 'powered_rail', 'detector_rail', 'activator_rail', 'ladder', 'lever', 'sugar_cane', 'bamboo', 'vine', 'glow_lichen', 'snow', 'fire', 'soul_fire', 'redstone_wire', 'string', 'tripwire'])

function isAirish(block) {
  return !block || AIR_NAMES.has(block.name)
}

function shade(color, factor) {
  const clamp = (v) => Math.max(0, Math.min(255, Math.round(v)))
  return [clamp(color[0] * factor), clamp(color[1] * factor), clamp(color[2] * factor), 255]
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

const SCALE = 5

function facingOf(bot) {
  // mineflayer yaw: 0 = +z（南），逆时针增；方向向量 = (-sin yaw, cos yaw)
  const yaw = bot.entity.yaw
  const dx = -Math.sin(yaw)
  const dz = Math.cos(yaw)
  let compass = '南'
  if (Math.abs(dx) > Math.abs(dz)) compass = dx > 0 ? '东' : '西'
  else compass = dz > 0 ? '南' : '北'
  return { dx, dz, compass }
}

function entityKind(entity, self) {
  if (entity === self) return 'self'
  if (entity.type === 'player') return 'player'
  if (entity.type === 'mob') {
    return /zombie|skeleton|creeper|spider|witch|slime|phantom|pillager|vindicator|ravager|blaze|ghast|magma|husk|stray|drowned|wither|guardian|hoglin|warden|breeze|bogged|silverfish|endermite|vex|evoker/i.test(entity.name ?? '')
      ? 'hostile'
      : 'passive'
  }
  if (entity.type === 'object' || entity.name === 'item') return 'item'
  return 'other'
}

const KIND_COLOR = {
  self: [70, 130, 255],
  player: [255, 255, 255],
  hostile: [240, 60, 50],
  passive: [90, 210, 70],
  item: [250, 210, 60],
  other: [200, 120, 220],
}

function drawDot(rgba, width, cx, cy, color, radius) {
  const r2 = radius * radius
  for (let y = -radius - 1; y <= radius + 1; y++) {
    for (let x = -radius - 1; x <= radius + 1; x++) {
      const d2 = x * x + y * y
      if (d2 > r2 + radius) continue
      const px = cx + x
      const py = cy + y
      if (px < 0 || py < 0 || px >= width || py >= rgba.length / (width * 4)) continue
      const outline = d2 > r2
      const c = outline ? [20, 20, 20, 255] : shade(color, 1)
      const offset = (py * width + px) * 4
      rgba[offset] = c[0]
      rgba[offset + 1] = c[1]
      rgba[offset + 2] = c[2]
      rgba[offset + 3] = 255
    }
  }
}

/**
 * 主入口：返回 { png: Buffer, message: string }。
 * radius = 俯视半径（格）；剖面取同半径、沿视线方向过脚下方块的竖直切片。
 */
export function renderView(bot, { radius = 20 } = {}) {
  radius = Math.max(8, Math.min(32, Math.round(radius)))
  const eye = bot.entity.position
  const eyeBlock = eye.floored()
  const facing = facingOf(bot)
  const panelW = (2 * radius + 1) * SCALE
  const topH = panelW
  const sectionYTop = 8
  const sectionYBottom = -18
  const sectionH = (sectionYTop - sectionYBottom + 1) * SCALE
  const margin = 6
  const width = panelW + margin * 2
  const height = topH + sectionH + margin * 3
  const rgba = Buffer.alloc(width * height * 4)
  // 底色深灰
  for (let i = 0; i < width * height; i++) {
    rgba[i * 4] = 30
    rgba[i * 4 + 1] = 30
    rgba[i * 4 + 2] = 34
    rgba[i * 4 + 3] = 255
  }

  const setPx = (x, y, c) => {
    if (x < 0 || y < 0 || x >= width || y >= height) return
    const o = (y * width + x) * 4
    rgba[o] = c[0]
    rgba[o + 1] = c[1]
    rgba[o + 2] = c[2]
    rgba[o + 3] = 255
  }

  // ---- 面板 1：俯视高度图 ----
  const topOriginX = margin
  const topOriginY = margin
  const notable = []
  const NOTABLE = /ore$|^chest$|^trapped_chest$|^spawner$|^lava$|ancient_debris/
  const heightGrid = new Array((2 * radius + 1) * (2 * radius + 1)).fill(null)
  // 俯视扫描比剖面看得深：低于窗口的地表画暗色而不是天空色，避免和水面混淆。
  const deepBottom = sectionYBottom - 34
  for (let dz = -radius; dz <= radius; dz++) {
    for (let dx = -radius; dx <= radius; dx++) {
      let top = null
      for (let dy = sectionYTop; dy >= deepBottom; dy--) {
        const block = bot.blockAt(eyeBlock.offset(dx, dy, dz))
        if (isAirish(block) || TRANSPARENT_THIN.has(block.name)) continue
        top = { block, y: eyeBlock.y + dy }
        break
      }
      const px = topOriginX + (dx + radius) * SCALE
      const py = topOriginY + (dz + radius) * SCALE
      let color
      if (!top) {
        color = [150, 190, 235] // 天空（真·视野外高处/虚空）
      } else {
        const base = blockColor(top.block.name)
        // 高度明暗：越高越亮；低于剖面窗口底的深地表压到 0.5 以下，一眼区分「远处低地」和「天空」
        const norm = (top.y - (eyeBlock.y + sectionYBottom)) / (sectionYTop - sectionYBottom)
        const factor = norm >= 0 ? 0.62 + 0.38 * norm : Math.max(0.42, 0.62 + norm * 0.25)
        color = shade(base, factor)
        if (top.block.name === 'lava') color = [255, 110, 20, 255]
        if (NOTABLE.test(top.block.name) && notable.length < 40 && (Math.abs(dx) > 2 || Math.abs(dz) > 2)) {
          notable.push({ name: top.block.name, dx, dz, dy: top.y - eyeBlock.y })
        }
      }
      heightGrid[(dz + radius) * (2 * radius + 1) + (dx + radius)] = top
      for (let sy = 0; sy < SCALE; sy++) {
        for (let sx = 0; sx < SCALE; sx++) setPx(px + sx, py + sy, color)
      }
    }
  }

  // 俯视面板上的实体点
  const worldToTop = (wx, wz) => [
    topOriginX + Math.round((wx - eye.x) * SCALE + radius * SCALE + SCALE / 2),
    topOriginY + Math.round((wz - eye.z) * SCALE + radius * SCALE + SCALE / 2),
  ]
  const seen = new Set()
  for (const entity of Object.values(bot.entities)) {
    const kind = entityKind(entity, bot.entity)
    if (kind === 'other' && entity.name !== 'item') continue
    const d = entity.position.distanceTo(eye)
    if (d > radius + 4) continue
    const key = `${entity.name}|${entity.username ?? ''}|${Math.round(entity.position.x)}|${Math.round(entity.position.z)}`
    if (seen.has(key)) continue
    seen.add(key)
    const [cx, cy] = worldToTop(entity.position.x, entity.position.z)
    drawDot(rgba, width, cx, cy, KIND_COLOR[kind], kind === 'self' ? 5 : 4)
  }
  // 自己的朝向线
  {
    const [cx, cy] = worldToTop(eye.x, eye.z)
    for (let i = 3; i <= 12; i++) {
      setPx(Math.round(cx + facing.dx * i), Math.round(cy + facing.dz * i), [70, 130, 255])
    }
  }

  // ---- 面板 2：朝向剖面 ----
  const axisX = Math.abs(facing.dx) >= Math.abs(facing.dz) ? Math.sign(facing.dx) : 0
  const axisZ = axisX === 0 ? Math.sign(facing.dz) : 0
  const secOriginX = margin
  const secOriginY = topH + margin * 2
  for (let i = -radius; i <= radius; i++) {
    const wx = eyeBlock.x + i * axisX
    const wz = eyeBlock.z + i * axisZ
    for (let dy = sectionYTop; dy >= sectionYBottom; dy--) {
      const block = bot.blockAt(eyeBlock.offset(i * axisX, dy, i * axisZ))
      const px = secOriginX + (i + radius) * SCALE
      const py = secOriginY + (sectionYTop - dy) * SCALE
      let color
      if (isAirish(block)) {
        color = [16, 16, 20] // 洞穴/天空黑
      } else {
        const base = blockColor(block.name)
        const depth = dy // 越深越暗
        const factor = depth >= 0 ? 1.0 : Math.max(0.5, 1.0 + depth * 0.028)
        color = shade(base, factor)
      }
      for (let sy = 0; sy < SCALE; sy++) {
        for (let sx = 0; sx < SCALE; sx++) setPx(px + sx, py + sy, color)
      }
    }
  }
  // 剖面里的自己（中心，脚下一格）
  {
    const cx = secOriginX + radius * SCALE + Math.floor(SCALE / 2)
    const cy = secOriginY + (sectionYTop - 0) * SCALE + Math.floor(SCALE / 2)
    for (let sy = -2; sy <= 1; sy++) {
      for (let sx = -1; sx <= 1; sx++) setPx(cx + sx, cy + sy, [70, 130, 255])
    }
  }

  // ---- 文本摘要 ----
  const lines = []
  lines.push(`俯视图：中心=你，白点=玩家，红点=敌对生物，绿点=被动生物，黄点=掉落物，蓝线=你的朝向（${facing.compass}）。越亮的地形越高。`)
  lines.push(`剖面图：你前方直到 ${radius} 格远的竖直切片（黑=空气/洞穴，蓝色小块=你）。`)
  const entityLines = []
  for (const entity of Object.values(bot.entities)) {
    const kind = entityKind(entity, bot.entity)
    if (kind === 'self' || kind === 'other') continue
    const d = entity.position.distanceTo(eye)
    if (d > radius + 4) continue
    const rel = entity.position.clone().subtract(eye)
    const label = entity.type === 'player' ? `玩家 ${entity.username}` : entity.name
    entityLines.push(`${label}（${kind === 'hostile' ? '敌对' : kind === 'item' ? '掉落物' : kind === 'player' ? '玩家' : '生物'}）距离 ${d.toFixed(0)} 格，相对偏移 x=${rel.x.toFixed(0)} y=${rel.y.toFixed(0)} z=${rel.z.toFixed(0)}`)
  }
  if (entityLines.length) lines.push(`周围实体：\n- ${entityLines.join('\n- ')}`)
  if (notable.length) {
    const sorted = notable
      .sort((a, b) => (a.dx * a.dx + a.dz * a.dz) - (b.dx * b.dx + b.dz * b.dz))
      .slice(0, 20)
    lines.push(`地表/浅层值得注意的方块（相对你）：${sorted.map((n) => `${n.name}@(${n.dx},${n.dy},${n.dz})`).join(' ')}`)
  }

  const png = encodePng(width, height, rgba)
  return { png, message: lines.join('\n') }
}
