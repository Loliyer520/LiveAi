/**
 * 2D 我的世界（MC2D）联机客户端。
 *
 * 笔端小游戏的服务端（/tmp/coding/WZRY/server/mc2d-server.js，43 机 :6601，
 * 本机经 mc2d-tunnel 映射到 127.0.0.1:6601）把「方块/时间/生物」当权威，
 * 玩家物理在客户端本地算完 5Hz 上报。所以这里复刻一份服务端同款世界生成
 * （mulberry32 + 同一套地形公式）和笔端同款物理，让 bot 以普通玩家身份
 * 「冰糖」进服：走路会撞墙、会跳台阶，干活受 6 格手长限制，不是直接改坐标。
 *
 * 零依赖：WS 用 Node 24 自带的全局 WebSocket，PNG 用 node:zlib 手写编码。
 */

import { deflateSync } from 'node:zlib';

export const MC2D_TILE = 32;
const WORLD_W = 400;
const WORLD_H = 80;
const GRAVITY = 1200;
const MOVE_SPEED = 120;
const JUMP_VELOCITY = -420;
const TICK_MS = 33;

/** 服务端/笔端一致的「非实体」方块：空气、掉落物、锭、钻石、装备。 */
const NON_SOLID = new Set<number>([0, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]);
/** 原木/树叶：可横穿、可从下往上穿，只在下落时承接（和笔端 isPlat 一致）。 */
const PLATFORM = new Set<number>([4, 5]);

interface BlockInfo {
  readonly name: string;
  readonly rgb: readonly [number, number, number];
  /** 挖掉后的掉落物 id（0=不掉落）。 */
  readonly drop?: number;
}

const BLOCK_INFO: Record<number, BlockInfo> = {
  1: { name: '草方块', rgb: [90, 158, 69], drop: 2 },
  2: { name: '泥土', rgb: [125, 90, 63], drop: 2 },
  3: { name: '石头', rgb: [122, 122, 122], drop: 8 },
  4: { name: '原木', rgb: [107, 79, 47], drop: 4 },
  5: { name: '树叶', rgb: [63, 141, 63], drop: 0 },
  6: { name: '沙子', rgb: [214, 198, 126], drop: 6 },
  7: { name: '木板', rgb: [184, 138, 85], drop: 7 },
  8: { name: '圆石', rgb: [133, 133, 133], drop: 8 },
  9: { name: '煤矿石', rgb: [74, 74, 74], drop: 9 },
  10: { name: '铁矿石', rgb: [216, 175, 147], drop: 10 },
  11: { name: '金矿石', rgb: [252, 238, 75], drop: 11 },
  12: { name: '钻石矿石', rgb: [93, 236, 245], drop: 21 },
  13: { name: '工作台', rgb: [165, 130, 95], drop: 13 },
  14: { name: '火把', rgb: [255, 210, 127], drop: 14 },
  15: { name: '猪肉', rgb: [232, 145, 141] },
  16: { name: '苹果', rgb: [216, 59, 59] },
  17: { name: '床', rgb: [176, 58, 58], drop: 17 },
  18: { name: '铁锭', rgb: [216, 216, 216] },
  19: { name: '金锭', rgb: [252, 238, 75] },
  20: { name: '熔炉', rgb: [111, 111, 111], drop: 20 },
  21: { name: '钻石', rgb: [93, 236, 245] },
  22: { name: '木镐', rgb: [184, 138, 85] },
  23: { name: '石镐', rgb: [133, 133, 133] },
  24: { name: '铁镐', rgb: [216, 216, 216] },
  25: { name: '钻石镐', rgb: [93, 236, 245] },
  26: { name: '木剑', rgb: [184, 138, 85] },
  27: { name: '石剑', rgb: [133, 133, 133] },
  28: { name: '铁剑', rgb: [216, 216, 216] },
  29: { name: '钻石剑', rgb: [93, 236, 245] },
  30: { name: '木棍', rgb: [107, 74, 30] },
};

const MAP_GLYPH: Record<number, string> = {
  0: '.', 1: 'G', 2: 'D', 3: 'S', 4: 'W', 5: 'L', 6: 'N', 7: 'P', 8: 'S',
  9: 'C', 10: 'I', 11: 'O', 12: 'K', 13: 'T', 14: '*', 15: 'p', 16: 'a',
  17: 'B', 18: 'i', 19: 'g', 20: 'F', 21: 'k', 22: '1', 23: '2', 24: '3',
  25: '4', 26: '5', 27: '6', 28: '7', 29: '8', 30: 's',
};

const MOB_NAMES: Record<string, string> = { pig: '猪', zombie: '僵尸', skeleton: '骷髅', creeper: '苦力怕' };
const MOB_GLYPH: Record<string, string> = { pig: 'p', zombie: 'z', skeleton: 'x', creeper: 'c' };
const MOB_RGB: Record<string, readonly [number, number, number]> = {
  pig: [232, 145, 141],
  zombie: [90, 140, 90],
  skeleton: [200, 200, 200],
  creeper: [67, 160, 71],
};

/** 与服务端 genWorld 逐行一致的确定性世界生成。 */
export function mulberry32(seed: number): () => number {
  let a = seed | 0;
  return function (): number {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function generateWorld(seed: number): Uint8Array {
  const rng = mulberry32(seed || 1);
  const world = new Uint8Array(WORLD_W * WORLD_H);
  const get = (x: number, y: number): number => world[y * WORLD_W + x];
  const set = (x: number, y: number, v: number): void => {
    if (x >= 0 && x < WORLD_W && y >= 0 && y < WORLD_H) world[y * WORLD_W + x] = v;
  };
  for (let x = 0; x < WORLD_W; x++) {
    const surface = Math.floor(WORLD_H * 0.45 + 8 * Math.sin(x * 0.06) + 4 * Math.sin(x * 0.18) + 2 * Math.sin(x * 0.35));
    for (let y = 0; y < WORLD_H; y++) {
      if (y < surface) set(x, y, 0);
      else if (y === surface) set(x, y, 1);
      else if (y < surface + 4) set(x, y, 2);
      else if (rng() < 0.04 && y > surface + 6) set(x, y, 0);
      else {
        const r = rng();
        if (r < 0.012) set(x, y, 9);
        else if (r < 0.019 && y > surface + 8) set(x, y, 10);
        else if (r < 0.023 && y > surface + 14) set(x, y, 11);
        else if (r < 0.0255 && y > surface + 20) set(x, y, 12);
        else set(x, y, 3);
      }
    }
    if (Math.sin(x * 0.12) > 0.75) {
      for (let y = surface + 1; y < surface + 4; y++) set(x, y, 6);
    }
    if (x > 2 && x < WORLD_W - 2 && rng() < 0.06 && get(x, surface) === 1 && get(x, surface - 1) === 0) {
      const h = 3 + Math.floor(rng() * 3);
      for (let i = 1; i <= h; i++) set(x, surface - i, 4);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) set(x + dx, surface - h - dy, 5);
      }
    }
  }
  return world;
}

// ── PNG 编码（零依赖，RGBA + filter 0） ───────────────────────────────────────

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) !== 0 ? (0xedb88320 ^ (c >>> 1)) >>> 0 : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buffer: Buffer): number {
  let c = 0xffffffff;
  for (const byte of buffer) c = (CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8)) >>> 0;
  return (c ^ 0xffffffff) >>> 0;
}

function pngChunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const typeBuffer = Buffer.from(type, 'latin1');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuffer, data])), 0);
  return Buffer.concat([length, typeBuffer, data, crc]);
}

export function encodePng(width: number, height: number, rgba: Uint8Array): Buffer {
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0;
    Buffer.from(rgba.buffer, rgba.byteOffset + y * stride, stride).copy(raw, y * (stride + 1) + 1);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

// ── 客户端 ───────────────────────────────────────────────────────────────────

export interface Mc2dOptions {
  readonly wsUrl: string;
  readonly name: string;
  readonly uid: string;
}

interface SelfState {
  id: number;
  name: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  facing: number;
  hp: number;
  food: number;
  onGround: boolean;
  anim: number;
}

interface OtherPlayer {
  id: number;
  name: string;
  x: number;
  y: number;
  facing: number;
  hp: number;
  anim: number;
}

interface Mob {
  id: number;
  type: string;
  x: number;
  y: number;
  dir: number;
  hp: number;
  fz: number;
}

interface ChatLine {
  name: string;
  msg: string;
  ts: number;
}

interface JoinWaiter {
  resolve: () => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

/** 进服/工具调用失败时抛这个（消息可直接给模型看）。 */
export class Mc2dError extends Error {}

export class Mc2dClient {
  private ws: WebSocket | null = null;
  /** 期望在线（断线要自动重连）；mc2d_leave 置 false。 */
  private desired = false;
  private joined = false;
  private closed = false;

  private world = generateWorld(1);
  private seed = 1;
  private time = 6000;
  private day = 1;
  private selfState: SelfState = {
    id: 0, name: '', x: 0, y: 0, vx: 0, vy: 0, facing: 1, hp: 20, food: 20, onGround: false, anim: 0,
  };
  private others = new Map<number, OtherPlayer>();
  private mobs = new Map<number, Mob>();
  private chatLines: ChatLine[] = [];
  private inventory = new Map<number, number>();

  private waiters: JoinWaiter[] = [];
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private sendTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempt = 0;
  private saveTick = 0;
  private dying = false;

  private moveTarget: { x: number } | null = null;
  private moveResolver: ((reached: boolean) => void) | null = null;
  private moveTimer: ReturnType<typeof setTimeout> | null = null;
  private wantJump = false;
  private lastProgressX = 0;
  private stuckTicks = 0;

  constructor(private readonly options: Mc2dOptions) {}

  get joinedValue(): boolean {
    return this.joined;
  }

  /** 启动即进服（config.auto_join）。失败只打日志，等第一次工具调用再试。 */
  autoJoin(): void {
    void this.ensureJoined().catch((error: unknown) => {
      console.warn('[mc2d] auto join failed:', error instanceof Error ? error.message : String(error));
    });
  }

  /** 连接并等到 init（已在线则立即返回）。 */
  async ensureJoined(timeoutMs = 12_000): Promise<void> {
    if (this.closed) throw new Mc2dError('MC2D 客户端已关闭');
    if (this.joined) return;
    this.desired = true;
    if (this.ws !== null && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
      await this.waitJoin(timeoutMs);
      return;
    }
    this.open();
    await this.waitJoin(timeoutMs);
  }

  /** 主动下线（之后工具调用会重新进服）。 */
  leave(): void {
    this.desired = false;
    this.joined = false;
    this.clearReconnect();
    this.stopTimers();
    this.finishMove(false);
    this.rejectWaiters(new Mc2dError('已离开 MC2D 服务器'));
    const ws = this.ws;
    this.ws = null;
    try {
      ws?.close();
    } catch {
      // ignore
    }
  }

  /** 进程退出/模块销毁：下线且不再重连。 */
  shutdown(): void {
    this.closed = true;
    this.leave();
  }

  // ── 工具动作 ─────────────────────────────────────────────────────────────

  async status(): Promise<string> {
    await this.ensureJoined();
    const p = this.selfState;
    const tx = this.tileX();
    const ty = this.tileY();
    const phase = this.isDay() ? '白天' : '夜晚';
    const lines: string[] = [];
    lines.push(`2D 我的世界联机 · 第${this.day}天 ${this.clockText()}（${phase}） · 你在线`);
    lines.push(`你：${p.name} P${p.id} 位置(${tx},${ty}) 生命${Math.round(p.hp)}/20 饥饿${Math.round(p.food)}/20`);
    const players = [...this.others.values()];
    lines.push(players.length === 0
      ? '在线玩家：只有你'
      : `在线玩家：${players.map((o) => `${o.name || `P${o.id}`}(${this.tileOf(o.x)},${this.tileOf(o.y)}) 生命${o.hp}`).join('、')}`);
    const nearby = [...this.mobs.values()]
      .map((m) => ({ m, d: Math.hypot((m.x - p.x) / MC2D_TILE, (m.y - p.y) / MC2D_TILE) }))
      .filter((item) => item.d <= 12)
      .sort((a, b) => a.d - b.d);
    lines.push(nearby.length === 0
      ? '附近生物：12 格内没有'
      : `附近生物：${nearby.map(({ m, d }) => `${MOB_NAMES[m.type] ?? m.type}#${m.id}(${this.tileOf(m.x)},${this.tileOf(m.y)}) 血${m.hp} 距${d.toFixed(1)}格`).join('、')}`);
    const inventory = this.inventoryText();
    lines.push(`背包：${inventory === '' ? '空' : inventory}`);
    const chat = this.chatLines.slice(-5);
    if (chat.length > 0) lines.push(`最近聊天：${chat.map((line) => `${line.name}: ${line.msg}`).join(' / ')}`);
    lines.push('');
    lines.push(this.mapText());
    return lines.join('\n');
  }

  /** 走到目标方块列 x（会自动跳 1-2 格台阶），返回实际位置。 */
  async moveTo(x: number, y?: number): Promise<{ reached: boolean; x: number; y: number }> {
    await this.ensureJoined();
    if (!Number.isFinite(x)) throw new Mc2dError('x 必须是数字（目标方块列 0-399）');
    const target = Math.max(1, Math.min(WORLD_W - 2, Math.round(x)));
    const distance = Math.abs(target * MC2D_TILE - this.selfState.x);
    const timeoutMs = Math.min(45_000, Math.max(4_000, Math.round((distance / MOVE_SPEED) * 1000) + 4_000));
    this.finishMove(false);
    this.moveTarget = { x: target * MC2D_TILE };
    this.lastProgressX = this.selfState.x;
    this.stuckTicks = 0;
    const reached = await new Promise<boolean>((resolve) => {
      this.moveResolver = resolve;
      this.moveTimer = setTimeout(() => this.finishMove(false), timeoutMs);
      this.moveTimer.unref?.();
    });
    const note = reached ? '' : `（${Math.round(timeoutMs / 1000)} 秒没走到${y === undefined ? '' : '目标层'}，可能被地形卡住，可再调用一次或先挖开挡路的方块）`;
    if (note !== '') console.info(`[mc2d] move timeout to x=${target}`);
    return { reached, x: this.tileX(), y: this.tileY() };
  }

  async dig(x: number, y: number): Promise<string> {
    await this.ensureJoined();
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Mc2dError('x/y 必须是数字（方块坐标）');
    const tx = Math.round(x);
    const ty = Math.round(y);
    if (!this.inReach(tx, ty)) throw new Mc2dError('够不着：只能挖身边 6 格以内的方块，先 mc2d_move 靠近。');
    const block = this.blockAt(tx, ty);
    if (block === 0) throw new Mc2dError(`(${tx},${ty}) 是空气，没东西可挖。`);
    if (block === 15 || block === 16) throw new Mc2dError('那是掉落物，不是方块。');
    this.send({ t: 'act', a: 'dig', tx, ty });
    this.setBlock(tx, ty, 0);
    const info = BLOCK_INFO[block];
    const drop = info?.drop ?? 0;
    let gained = '';
    if (drop > 0) {
      this.addItem(drop, 1);
      gained = `，获得${BLOCK_INFO[drop]?.name ?? `#${drop}`}×1`;
    } else if (block === 5 && Math.random() < 0.15) {
      this.addItem(16, 1);
      gained = '，还掉了个苹果×1';
    }
    return `挖掉了${info?.name ?? `方块#${block}`}(${tx},${ty})${gained}。`;
  }

  async place(x: number, y: number, block: number): Promise<string> {
    await this.ensureJoined();
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(block)) throw new Mc2dError('x/y/block 必须是数字');
    const tx = Math.round(x);
    const ty = Math.round(y);
    const id = Math.round(block);
    const allowed = (id >= 1 && id <= 14) || id === 17 || id === 20;
    if (!allowed) throw new Mc2dError('block 只能是实体方块 id：1-14、17（床）、20（熔炉）。15/16 是食物，不能当方块放。');
    if (!this.inReach(tx, ty)) throw new Mc2dError('够不着：只能放在身边 6 格以内，先 mc2d_move 靠近。');
    if (this.blockAt(tx, ty) !== 0) throw new Mc2dError(`(${tx},${ty}) 已经有方块了，只能往空气里放。`);
    const have = this.inventory.get(id) ?? 0;
    if (have <= 0) throw new Mc2dError(`背包里没有${BLOCK_INFO[id]?.name ?? `#${id}`}，先去挖/收集再放。`);
    this.send({ t: 'act', a: 'place', tx, ty, b: id });
    this.setBlock(tx, ty, id);
    this.inventory.set(id, have - 1);
    if (have - 1 <= 0) this.inventory.delete(id);
    return `在(${tx},${ty})放了一个${BLOCK_INFO[id]?.name ?? `#${id}`}。背包剩${(this.inventory.get(id) ?? 0)}个。`;
  }

  /** 打怪：不传 mobId 打最近的（6 格内）。 */
  async attack(mobId?: number, damage = 4): Promise<string> {
    await this.ensureJoined();
    const dmg = Math.max(1, Math.min(8, Math.round(Number.isFinite(damage) ? damage : 4)));
    let mob: Mob | undefined;
    if (mobId !== undefined && Number.isFinite(mobId)) {
      mob = this.mobs.get(Math.round(mobId));
      if (mob === undefined) throw new Mc2dError(`没有 id=${Math.round(mobId)} 的生物（可能已经死了/走远了），用 mc2d_status 看当前目标。`);
    } else {
      let best = Infinity;
      for (const candidate of this.mobs.values()) {
        const distance = Math.abs(candidate.x - this.selfState.x) + Math.abs(candidate.y - this.selfState.y);
        if (distance < best) {
          best = distance;
          mob = candidate;
        }
      }
      if (mob === undefined) throw new Mc2dError('附近没有生物。');
    }
    const mx = this.tileOf(mob.x);
    const my = this.tileOf(mob.y);
    if (!this.inReach(mx, my)) throw new Mc2dError(`最近的${MOB_NAMES[mob.type] ?? mob.type}#${mob.id}在(${mx},${my})，超出 6 格手长，先 mc2d_move 靠近。`);
    const before = mob.hp;
    this.send({ t: 'act', a: 'atk', mid: mob.id, dmg });
    mob.hp = Math.max(0, mob.hp - dmg);
    const tag = `${MOB_NAMES[mob.type] ?? mob.type}#${mob.id}`;
    return mob.hp <= 0
      ? `给了${tag}最后一击（${before}→0 血）。`
      : `攻击${tag}，造成 ${dmg} 点伤害（${before}→${mob.hp} 血）。`;
  }

  async say(text: string): Promise<string> {
    await this.ensureJoined();
    const msg = String(text).trim().slice(0, 60);
    if (msg === '') throw new Mc2dError('聊天内容为空。');
    this.send({ t: 'chat', msg });
    return `已在游戏里说：「${msg}」`;
  }

  /** 渲染当前视野成 PNG（给视觉模型看）。 */
  async viewPng(): Promise<Buffer> {
    await this.ensureJoined();
    return this.renderPng();
  }

  positionText(): string {
    return `位置(${this.tileX()},${this.tileY()})`;
  }

  // ── 连接层 ───────────────────────────────────────────────────────────────

  private open(): void {
    this.clearReconnect();
    if (this.ws !== null) {
      const old = this.ws;
      this.ws = null;
      try {
        old.close();
      } catch {
        // ignore
      }
    }
    const ws = new WebSocket(this.options.wsUrl);
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectAttempt = 0;
      const uid = this.options.uid.slice(0, 64).replace(/[^\w-]/g, '');
      this.send({ t: 'join', name: this.options.name.slice(0, 12), uid });
    };
    ws.onmessage = (event) => {
      if (typeof event.data !== 'string') return;
      try {
        this.handleFrame(event.data);
      } catch (error) {
        console.warn('[mc2d] frame handling failed:', error instanceof Error ? error.message : String(error));
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      const wasJoined = this.joined;
      this.ws = null;
      this.joined = false;
      this.stopTimers();
      this.finishMove(false);
      this.rejectWaiters(new Mc2dError('MC2D 连接已断开'));
      if (wasJoined) console.warn('[mc2d] disconnected');
      if (this.desired && !this.closed) this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose 紧随其后，出错细节由超时/断开路径统一处理
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null || !this.desired || this.closed) return;
    const delay = Math.min(30_000, 2_000 * 2 ** Math.min(this.reconnectAttempt, 4));
    this.reconnectAttempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.desired && !this.closed && this.ws === null) this.open();
    }, delay);
    this.reconnectTimer.unref?.();
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private waitJoin(timeoutMs: number): Promise<void> {
    if (this.joined) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((waiter) => waiter.timer !== timer);
        reject(new Mc2dError(`连接 MC2D 服务器超时（${Math.round(timeoutMs / 1000)} 秒，${this.options.wsUrl}）`));
      }, timeoutMs);
      timer.unref?.();
      this.waiters.push({ resolve, reject, timer });
    });
  }

  private resolveWaiters(): void {
    const waiters = this.waiters;
    this.waiters = [];
    for (const waiter of waiters) {
      clearTimeout(waiter.timer);
      waiter.resolve();
    }
  }

  private rejectWaiters(error: Error): void {
    const waiters = this.waiters;
    this.waiters = [];
    for (const waiter of waiters) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
  }

  private send(payload: Record<string, unknown>): void {
    const ws = this.ws;
    if (ws === null || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(payload));
    } catch {
      // 下一拍重连逻辑会兜底
    }
  }

  // ── 服务端帧 ─────────────────────────────────────────────────────────────

  private handleFrame(raw: string): void {
    const frame = JSON.parse(raw) as Record<string, any>;
    if (frame === null || typeof frame !== 'object') return;
    const t = String(frame.t ?? '');
    if (t === 'init') {
      this.handleInit(frame);
      return;
    }
    if (!this.joined) return;
    switch (t) {
      case 'st': {
        this.time = Number(frame.time) || this.time;
        this.day = Number(frame.day) || this.day;
        const mobs = new Map<number, Mob>();
        for (const m of Array.isArray(frame.mobs) ? frame.mobs : []) {
          const id = Number(m?.id);
          if (!Number.isFinite(id)) continue;
          mobs.set(id, {
            id,
            type: String(m.type ?? ''),
            x: Number(m.x) || 0,
            y: Number(m.y) || 0,
            dir: Number(m.dir) || 1,
            hp: Number(m.hp) || 0,
            fz: Number(m.fz) || 0,
          });
        }
        this.mobs = mobs;
        const others = new Map<number, OtherPlayer>();
        for (const p of Array.isArray(frame.players) ? frame.players : []) {
          const id = Number(p?.id);
          if (!Number.isFinite(id) || id === this.selfState.id) continue;
          const old = this.others.get(id);
          others.set(id, {
            id,
            name: String(p.name ?? old?.name ?? ''),
            x: Number(p.x) || 0,
            y: Number(p.y) || 0,
            facing: Number(p.facing) || 1,
            hp: Number(p.hp) || 0,
            anim: Number(p.anim) || 0,
          });
        }
        this.others = others;
        return;
      }
      case 'diff': {
        for (const cell of Array.isArray(frame.cells) ? frame.cells : []) {
          if (Array.isArray(cell) && cell.length >= 3) this.setBlock(Number(cell[0]), Number(cell[1]), Number(cell[2]));
        }
        return;
      }
      case 'pj': {
        const p = frame.p ?? {};
        const id = Number(p.id);
        if (Number.isFinite(id) && id !== this.selfState.id) {
          this.others.set(id, {
            id,
            name: String(p.name ?? ''),
            x: Number(p.x) || 0,
            y: Number(p.y) || 0,
            facing: Number(p.facing) || 1,
            hp: Number(p.hp) || 20,
            anim: 0,
          });
        }
        return;
      }
      case 'pl': {
        this.others.delete(Number(frame.id));
        return;
      }
      case 'chat': {
        this.chatLines.push({ name: String(frame.name ?? `P${frame.id}`), msg: String(frame.msg ?? ''), ts: Date.now() });
        if (this.chatLines.length > 20) this.chatLines.splice(0, this.chatLines.length - 20);
        return;
      }
      case 'hurt': {
        if (Number(frame.id) === this.selfState.id) {
          this.selfState.hp = Math.max(0, Number(frame.hp) || 0);
          if (this.selfState.hp <= 0) this.respawn();
        }
        return;
      }
      case 'mhit': {
        const mob = this.mobs.get(Number(frame.mid));
        if (mob !== undefined) {
          mob.hp = Math.max(0, Number(frame.hp) || 0);
          if (Number.isFinite(Number(frame.x))) mob.x = Number(frame.x);
        }
        return;
      }
      case 'drop': {
        if (Number(frame.id) === this.selfState.id) this.addItem(Number(frame.item) || 15, 1);
        return;
      }
      case 'prespawn': {
        if (Number(frame.id) === this.selfState.id) {
          this.selfState.x = Number(frame.x) || this.selfState.x;
          this.selfState.y = Number(frame.y) || this.selfState.y;
          this.selfState.vx = 0;
          this.selfState.vy = 0;
          this.selfState.hp = 20;
        }
        return;
      }
      case 'boom':
      case 'arr':
      case 'pong':
      default:
        return;
    }
  }

  private handleInit(frame: Record<string, any>): void {
    this.seed = Number(frame.seed) || 1;
    this.world = generateWorld(this.seed);
    for (const cell of Array.isArray(frame.cells) ? frame.cells : []) {
      if (Array.isArray(cell) && cell.length >= 3) this.setBlock(Number(cell[0]), Number(cell[1]), Number(cell[2]));
    }
    this.time = Number(frame.time) || 6000;
    this.day = Number(frame.day) || 1;
    this.selfState = {
      id: Number(frame.id) || 0,
      name: String(frame.name ?? this.options.name),
      x: (Number(frame.spawnX) || 0) * MC2D_TILE,
      y: (Number(frame.spawnY) || 0) * MC2D_TILE,
      vx: 0,
      vy: 0,
      facing: 1,
      hp: 20,
      food: 20,
      onGround: false,
      anim: 0,
    };
    this.others = new Map();
    for (const p of Array.isArray(frame.players) ? frame.players : []) {
      const id = Number(p?.id);
      if (!Number.isFinite(id) || id === this.selfState.id) continue;
      this.others.set(id, {
        id,
        name: String(p.name ?? ''),
        x: Number(p.x) || 0,
        y: Number(p.y) || 0,
        facing: Number(p.facing) || 1,
        hp: Number(p.hp) || 20,
        anim: Number(p.anim) || 0,
      });
    }
    this.mobs = new Map();
    for (const m of Array.isArray(frame.mobs) ? frame.mobs : []) {
      const id = Number(m?.id);
      if (!Number.isFinite(id)) continue;
      this.mobs.set(id, {
        id,
        type: String(m.type ?? ''),
        x: Number(m.x) || 0,
        y: Number(m.y) || 0,
        dir: Number(m.dir) || 1,
        hp: Number(m.hp) || 0,
        fz: Number(m.fz) || 0,
      });
    }
    this.chatLines = (Array.isArray(frame.chat) ? frame.chat.slice(-10) : []).map((line: any) => ({
      name: String(line?.name ?? `P${line?.id}`),
      msg: String(line?.msg ?? ''),
      ts: Number(line?.ts) || Date.now(),
    }));
    this.inventory = new Map();
    const profile = frame.prof;
    if (profile && Array.isArray(profile.inv)) {
      for (const item of profile.inv) {
        const id = Number(item?.id);
        const count = Number(item?.count);
        if (id > 0 && count > 0) this.addItem(id, count);
      }
    }
    this.joined = true;
    this.desired = true;
    this.reconnectAttempt = 0;
    this.startTimers();
    this.resolveWaiters();
    console.info(`[mc2d] joined as ${this.selfState.name} P${this.selfState.id} (${this.options.wsUrl})`);
  }

  private startTimers(): void {
    this.stopTimers();
    this.tickTimer = setInterval(() => this.tick(), TICK_MS);
    this.tickTimer.unref?.();
    this.sendTimer = setInterval(() => this.sendState(), 200);
    this.sendTimer.unref?.();
  }

  private stopTimers(): void {
    if (this.tickTimer !== null) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
    if (this.sendTimer !== null) {
      clearInterval(this.sendTimer);
      this.sendTimer = null;
    }
  }

  private sendState(): void {
    if (!this.joined) return;
    const p = this.selfState;
    this.send({
      t: 's',
      x: Math.round(p.x),
      y: Math.round(p.y),
      vx: Math.round(p.vx),
      facing: p.facing,
      hp: Math.round(p.hp),
      anim: Math.abs(p.vx) > 20 ? 1 : 0,
    });
    this.saveTick += 1;
    if (this.saveTick >= 50) {
      this.saveTick = 0;
      const inv = [...this.inventory.entries()].slice(0, 36).map(([id, count]) => ({ id, count }));
      this.send({
        t: 'save',
        inv,
        tools: { pick: 0, sword: 0, sticks: 0 },
        sel: 0,
        x: Math.round(p.x),
        y: Math.round(p.y),
        hp: Math.round(p.hp),
        food: Math.round(p.food),
        bed: null,
        ach: [],
      });
    }
  }

  // ── 本地物理（与笔端 index.vue 同参数） ─────────────────────────────────

  private tick(): void {
    if (!this.joined) return;
    const dt = 0.033;
    const p = this.selfState;
    let input = 0;
    if (this.moveTarget !== null) {
      const dx = this.moveTarget.x - p.x;
      if (Math.abs(dx) <= 8) {
        this.finishMove(true);
      } else {
        input = dx > 0 ? 1 : -1;
      }
    }
    if (input !== 0) {
      p.vx = input * MOVE_SPEED;
      p.facing = input;
    } else {
      p.vx *= 0.7;
    }
    if (this.wantJump && p.onGround) {
      p.vy = JUMP_VELOCITY;
      p.onGround = false;
    }
    this.wantJump = false;

    p.vy += GRAVITY * dt;
    if (p.vy > 1100) p.vy = 1100;
    p.x += p.vx * dt;
    this.resolveX();
    const vyBefore = p.vy;
    p.y += p.vy * dt;
    this.resolveY();
    if (p.onGround && vyBefore > 750) {
      const damage = Math.floor((vyBefore - 750) / 55);
      if (damage > 0) {
        p.hp = Math.max(0, p.hp - damage);
        if (p.hp <= 0) this.respawn();
      }
    }
    if (p.y > WORLD_H * MC2D_TILE + 300) this.respawn();
    p.anim = Math.abs(p.vx) > 20 ? 1 : 0;

    if (this.moveTarget !== null && input !== 0) {
      // 前方 1-2 格高有实体墙就跳（正常平地不会误跳，因为看的是脚下往上的身体层）
      if (p.onGround) {
        const footRow = Math.floor((p.y + MC2D_TILE * 1.6 - 1) / MC2D_TILE);
        const frontTx = Math.floor((p.x + (input > 0 ? MC2D_TILE * 0.7 : 0) + input * 2) / MC2D_TILE);
        if (this.hardAt(frontTx, footRow - 1) || this.hardAt(frontTx, footRow - 2)) this.wantJump = true;
      }
      if (Math.abs(p.x - this.lastProgressX) < 0.5) this.stuckTicks += 1;
      else this.stuckTicks = 0;
      this.lastProgressX = p.x;
      if (this.stuckTicks > 20 && p.onGround) {
        this.wantJump = true;
        this.stuckTicks = 0;
      }
    }
  }

  private resolveX(): void {
    const p = this.selfState;
    const ts = MC2D_TILE;
    const left = Math.floor((p.x + 1) / ts);
    const right = Math.floor((p.x + ts * 0.7 - 1) / ts);
    const top = Math.floor((p.y + 1) / ts);
    const bottom = Math.floor((p.y + ts * 1.6 - 1) / ts);
    for (let ty = top; ty <= bottom; ty++) {
      const hitL = this.hardAt(left, ty);
      const hitR = this.hardAt(right, ty);
      if (hitL && hitR) {
        p.y = (top - 1.6) * ts;
        p.vy = 0;
        return;
      }
      if (hitL) {
        p.x = (left + 1) * ts + 1;
        p.vx = 0;
      } else if (hitR) {
        p.x = (right - 0.7) * ts - 1;
        p.vx = 0;
      }
    }
    if (p.x < 0) {
      p.x = 0;
      p.vx = 0;
    }
    const maxX = (WORLD_W - 1) * ts;
    if (p.x > maxX) {
      p.x = maxX;
      p.vx = 0;
    }
  }

  private resolveY(): void {
    const p = this.selfState;
    const ts = MC2D_TILE;
    const left = Math.floor((p.x + 1) / ts);
    const right = Math.floor((p.x + ts * 0.7 - 1) / ts);
    const top = Math.floor((p.y + 1) / ts);
    const bottom = Math.floor((p.y + ts * 1.6 - 1) / ts);
    p.onGround = false;
    for (let tx = left; tx <= right; tx++) {
      if (this.hardAt(tx, top)) {
        p.y = (top + 1) * ts + 1;
        p.vy = 0;
      }
      const platform = PLATFORM.has(this.blockAt(tx, bottom));
      if (this.hardAt(tx, bottom) || (platform && p.vy >= 0)) {
        p.y = (bottom - 1.6) * ts;
        p.vy = 0;
        p.onGround = true;
      }
    }
    if (p.y > WORLD_H * ts) {
      p.y = 0;
      p.vy = 0;
    }
  }

  private respawn(): void {
    if (this.dying) return;
    this.dying = true;
    this.selfState.hp = 20;
    this.send({ t: 'act', a: 'die', bx: -1, by: -1 });
    const timer = setTimeout(() => {
      this.dying = false;
    }, 1_500);
    timer.unref?.();
  }

  private finishMove(reached: boolean): void {
    if (this.moveResolver !== null) {
      const resolve = this.moveResolver;
      this.moveResolver = null;
      resolve(reached);
    }
    this.moveTarget = null;
    if (this.moveTimer !== null) {
      clearTimeout(this.moveTimer);
      this.moveTimer = null;
    }
    this.stuckTicks = 0;
  }

  // ── 世界查询 ─────────────────────────────────────────────────────────────

  private blockAt(tx: number, ty: number): number {
    if (tx < 0 || tx >= WORLD_W || ty < 0 || ty >= WORLD_H) return 0;
    return this.world[ty * WORLD_W + tx];
  }

  private setBlock(tx: number, ty: number, block: number): void {
    if (!Number.isFinite(tx) || !Number.isFinite(ty)) return;
    const x = Math.round(tx);
    const y = Math.round(ty);
    if (x < 0 || x >= WORLD_W || y < 0 || y >= WORLD_H) return;
    this.world[y * WORLD_W + x] = Math.max(0, Math.min(255, Math.round(block)));
  }

  private hardAt(tx: number, ty: number): boolean {
    const block = this.blockAt(tx, ty);
    return block !== 0 && !NON_SOLID.has(block) && !PLATFORM.has(block);
  }

  private surfaceAt(tx: number): number {
    if (tx < 0 || tx >= WORLD_W) return Math.floor(WORLD_H * 0.45);
    for (let y = 0; y < WORLD_H; y++) {
      if (this.world[y * WORLD_W + tx] !== 0) return y;
    }
    return Math.floor(WORLD_H * 0.45);
  }

  private tileOf(pixel: number): number {
    return Math.floor(pixel / MC2D_TILE);
  }

  private tileX(): number {
    return this.tileOf(this.selfState.x + MC2D_TILE * 0.35);
  }

  private tileY(): number {
    return this.tileOf(this.selfState.y + MC2D_TILE * 0.9);
  }

  private inReach(tx: number, ty: number): boolean {
    const dx = Math.abs(tx - Math.floor((this.selfState.x + MC2D_TILE * 0.35) / MC2D_TILE));
    const dy = Math.abs(ty - Math.floor((this.selfState.y + MC2D_TILE) / MC2D_TILE));
    return dx + dy <= 6;
  }

  private addItem(id: number, count: number): void {
    if (!Number.isFinite(id) || id <= 0 || !Number.isFinite(count) || count <= 0) return;
    this.inventory.set(id, (this.inventory.get(id) ?? 0) + count);
  }

  private inventoryText(): string {
    return [...this.inventory.entries()]
      .filter(([, count]) => count > 0)
      .map(([id, count]) => `${BLOCK_INFO[id]?.name ?? `#${id}`}×${count}`)
      .join(' ');
  }

  private isDay(): boolean {
    return this.time > 6000 && this.time < 18000;
  }

  private clockText(): string {
    const hours = Math.floor(this.time / 1000);
    const minutes = Math.floor(((this.time % 1000) * 60) / 1000);
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
  }

  /** 身边 31×15 格方块俯视图（横轴=东西，纵轴=高度）。 */
  private mapText(): string {
    const centerX = this.tileX();
    const centerY = this.tileY();
    const rows: string[] = [];
    for (let y = centerY - 8; y <= centerY + 6; y++) {
      let line = '';
      for (let x = centerX - 15; x <= centerX + 15; x++) {
        let glyph = MAP_GLYPH[this.blockAt(x, y)] ?? '·';
        for (const mob of this.mobs.values()) {
          if (this.tileOf(mob.x) === x && this.tileOf(mob.y) === y) {
            glyph = MOB_GLYPH[mob.type] ?? '?';
            break;
          }
        }
        for (const other of this.others.values()) {
          if (this.tileOf(other.x) === x && this.tileOf(other.y) === y) {
            glyph = 'P';
            break;
          }
        }
        if (x === centerX && y === centerY) glyph = '@';
        line += glyph;
      }
      rows.push(line);
    }
    return [
      '附近地形（每格=1 方块，@=你 P=其他玩家 p猪 z僵尸 x骷髅 c苦力怕；.空气 G草 D土 S石 W原木 L树叶 N沙 C煤 I铁 O金 K钻石 B床 T工作台 F熔炉）：',
      ...rows,
    ].join('\n');
  }

  // ── 画面渲染（给视觉模型） ───────────────────────────────────────────────

  private renderPng(): Buffer {
    const scale = 16;
    const viewW = 30;
    const viewH = 18;
    const width = viewW * scale;
    const height = viewH * scale;
    const rgba = new Uint8Array(width * height * 4);
    const sky = this.skyRgb();
    const selfTileX = this.tileX();
    const selfTileY = this.tileY();
    const originTx = selfTileX - Math.floor(viewW / 2);
    const originTy = selfTileY - Math.floor(viewH / 2);

    const fillRect = (px: number, py: number, pw: number, ph: number, rgb: readonly [number, number, number]): void => {
      const x0 = Math.max(0, px);
      const y0 = Math.max(0, py);
      const x1 = Math.min(width, px + pw);
      const y1 = Math.min(height, py + ph);
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const offset = (y * width + x) * 4;
          rgba[offset] = rgb[0];
          rgba[offset + 1] = rgb[1];
          rgba[offset + 2] = rgb[2];
          rgba[offset + 3] = 255;
        }
      }
    };

    for (let row = 0; row < viewH; row++) {
      for (let col = 0; col < viewW; col++) {
        const tx = originTx + col;
        const ty = originTy + row;
        let rgb: readonly [number, number, number] = sky;
        const block = this.blockAt(tx, ty);
        if (block !== 0) {
          const base = BLOCK_INFO[block]?.rgb ?? [136, 136, 136];
          const noise = (((tx * 73856093) ^ (ty * 19349663)) >>> 0) % 17 - 8;
          rgb = [
            Math.max(0, Math.min(255, base[0] + noise)),
            Math.max(0, Math.min(255, base[1] + noise)),
            Math.max(0, Math.min(255, base[2] + noise)),
          ];
        } else if (ty > this.surfaceAt(tx)) {
          rgb = [20, 22, 30];
        }
        fillRect(col * scale, row * scale, scale, scale, rgb);
      }
    }

    const drawEntity = (
      entX: number,
      entY: number,
      pixelW: number,
      pixelH: number,
      rgb: readonly [number, number, number],
    ): void => {
      const px = Math.round(((entX - originTx * MC2D_TILE) * scale) / MC2D_TILE);
      const py = Math.round(((entY - originTy * MC2D_TILE) * scale) / MC2D_TILE);
      const pw = Math.max(3, Math.round((pixelW * scale) / MC2D_TILE));
      const ph = Math.max(3, Math.round((pixelH * scale) / MC2D_TILE));
      fillRect(px - 1, py - 1, pw + 2, ph + 2, [12, 12, 12]);
      fillRect(px, py, pw, ph, rgb);
    };

    for (const mob of this.mobs.values()) {
      drawEntity(mob.x, mob.y, MC2D_TILE * 0.8, MC2D_TILE * 1.2, MOB_RGB[mob.type] ?? [200, 200, 200]);
    }
    for (const other of this.others.values()) {
      drawEntity(other.x, other.y, MC2D_TILE * 0.7, MC2D_TILE * 1.6, [90, 150, 255]);
    }
    drawEntity(this.selfState.x, this.selfState.y, MC2D_TILE * 0.7, MC2D_TILE * 1.6, [255, 214, 90]);
    // 头顶白点，方便一眼认出自己
    const selfPx = Math.round(((this.selfState.x - originTx * MC2D_TILE) * scale) / MC2D_TILE);
    const selfPy = Math.round(((this.selfState.y - originTy * MC2D_TILE) * scale) / MC2D_TILE);
    fillRect(selfPx + Math.round((MC2D_TILE * 0.7 * scale) / MC2D_TILE / 2) - 2, selfPy - 6, 4, 3, [255, 255, 255]);

    return encodePng(width, height, rgba);
  }

  private skyRgb(): readonly [number, number, number] {
    const elevation = Math.sin((this.time / 24000) * Math.PI * 2 - Math.PI / 2);
    const lerp = (
      from: readonly [number, number, number],
      to: readonly [number, number, number],
      ratio: number,
    ): readonly [number, number, number] => [
      Math.round(from[0] + (to[0] - from[0]) * ratio),
      Math.round(from[1] + (to[1] - from[1]) * ratio),
      Math.round(from[2] + (to[2] - from[2]) * ratio),
    ];
    if (elevation < -0.2) return [11, 16, 32];
    if (elevation < 0.3) return lerp([11, 16, 32], [58, 95, 122], (elevation + 0.2) / 0.5);
    return lerp([58, 95, 122], [126, 192, 238], Math.min(1, (elevation - 0.3) / 0.4));
  }
}

// ── 模块级单例（chat-module 启动时配置，tools.ts 取用） ─────────────────────

let activeClient: Mc2dClient | null = null;

export function configureMc2d(options: Mc2dOptions | null): void {
  activeClient?.shutdown();
  activeClient = options === null ? null : new Mc2dClient(options);
}

export function mc2dClient(): Mc2dClient | null {
  return activeClient;
}
