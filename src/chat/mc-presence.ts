/**
 * 真实 Minecraft 边车（deeplive-mcbot @127.0.0.1:7660）的在场情报缓存。
 *
 * 身份频道 status() 是同步的，且会被拼进订阅者的系统提示——只有低频变化才
 * 不打爆前缀缓存。所以这里后台低频轮询边车 /status，只缓存「在不在线 +
 * 在线玩家名单」；坐标/血量/背包这类高频状态仍走 mc_status 工具按需查。
 *
 * 事件只在状态真正变化时留痕（上线/掉线/玩家进出/边车失联），供任何会话
 * 用 intel_channel events 补课——避免下级 AI 拿不到 MC 事实时自行脑补。
 */

import { recordIdentityEvent } from '../store/global-identity.js';

const POLL_MS = 20_000;

let baseUrl = 'http://127.0.0.1:7660';
let token = '';
let timer: ReturnType<typeof setInterval> | null = null;
let inFlight = false;
let snapshot = '真实 MC 服务器：状态未知（边车还没回报，deeplive-mcbot 可能没在跑）。';
let lastKey = 'unknown';

export function configureMcPresence(url: string, bearer: string): void {
  baseUrl = url.replace(/\/+$/, '');
  token = bearer;
  lastKey = 'unknown';
}

/** 订阅者 prompt 里注入的低频状态行。 */
export function mcPresenceStatus(): string {
  return snapshot;
}

/** 解析边车 /status 的 message → 一行摘要；纯函数便于测试。 */
export function buildPresenceSnapshot(message: string): { text: string; key: string; dead: boolean } {
  const lines = message
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');
  const onlineLine = lines.find((line) => line.startsWith('在线：'));
  if (onlineLine !== undefined) {
    const playersLine = lines.find(
      (line) => line.startsWith('玩家列表（') || line.startsWith('玩家在线（'),
    );
    const players = playersLine
      ?.replace(/^玩家(列表|在线)（\d*）[：:]\s*/, '')
      .trim() ?? '';
    const healthLine = lines.find((line) => line.startsWith('生命 '));
    const health = Number(healthLine?.match(/^生命\s*(\d+)\/20/)?.[1] ?? Number.NaN);
    const dead = health === 0;
    const text = `真实 MC 服务器：${onlineLine.replace(/^在线：/, '')}`
      + (playersLine !== undefined ? `\n在线玩家：${players === '' ? '无' : players}` : '')
      + (dead ? '\n角色状态：已死亡，正在等待重生' : '');
    return { text, key: `online|dead=${dead ? 1 : 0}|${players}`, dead };
  }
  const connectingLine = lines.find((line) => line.startsWith('正在连接'));
  if (connectingLine !== undefined) {
    return { text: `真实 MC 服务器：${connectingLine}`, key: 'connecting', dead: false };
  }
  const errorLine = lines.find((line) => line.startsWith('上次错误：'));
  const text = '真实 MC 服务器：未连接，角色不在服里。'
    + (errorLine !== undefined ? `（${errorLine}）` : '');
  return { text, key: 'offline', dead: false };
}

function applyState(text: string, key: string): void {
  snapshot = text;
  if (key === lastKey) return;
  const wasOnline = lastKey.startsWith('online|');
  const isOnline = key.startsWith('online|');
  const wasDead = lastKey.includes('|dead=1|');
  const isDead = key.includes('|dead=1|');
  const flat = text.replace(/\n/g, '｜');
  if (isOnline && !wasOnline) {
    recordIdentityEvent('mc', `MC 角色上线进服：${flat}`);
  } else if (isOnline && wasOnline && isDead && !wasDead) {
    recordIdentityEvent('mc', `MC 角色死亡，正在自动重生：${flat}`);
  } else if (isOnline && wasOnline && !isDead && wasDead) {
    recordIdentityEvent('mc', 'MC 角色已重生。');
  } else if (isOnline && wasOnline) {
    recordIdentityEvent('mc', `MC 服务器玩家变化：${flat}`);
  } else if (!isOnline && wasOnline && key === 'offline') {
    recordIdentityEvent('mc', `MC 角色掉线/离开服务器：${flat}`);
  } else if (!isOnline && wasOnline) {
    recordIdentityEvent('mc', 'MC 边车状态查询失败，暂时无法确认角色是否在服里。');
  }
  lastKey = key;
}

let lastRespawnAt = 0;

/** 角色死了就替它重生——否则名单里有名字，世界里却没有身体。 */
async function requestRespawn(fetchImpl: typeof fetch): Promise<void> {
  const now = Date.now();
  if (now - lastRespawnAt < 15_000) return;
  lastRespawnAt = now;
  try {
    await fetchImpl(`${baseUrl}/respawn`, {
      headers: token !== '' ? { authorization: `Bearer ${token}` } : {},
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    // 下一轮轮询会再试；失败不改变快照。
  }
}

/** 轮询一次；失败只降级为「状态未知」，不编造在线/掉线。 */
export async function pollMcPresenceOnce(fetchImpl: typeof fetch = fetch): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  try {
    const response = await fetchImpl(`${baseUrl}/status`, {
      headers: token !== '' ? { authorization: `Bearer ${token}` } : {},
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) {
      applyState(`真实 MC 服务器：边车查询失败（HTTP ${response.status}），无法确认角色是否在服里。`, 'unknown');
      return;
    }
    const body = (await response.json()) as { ok?: boolean; message?: string };
    const message = typeof body.message === 'string' ? body.message : '';
    if (message === '') {
      applyState('真实 MC 服务器：边车返回空状态，无法确认角色是否在服里。', 'unknown');
      return;
    }
    const next = buildPresenceSnapshot(message);
    applyState(next.text, next.key);
    if (next.dead) await requestRespawn(fetchImpl);
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    applyState(`真实 MC 服务器：边车查询失败（${reason.slice(0, 120)}），无法确认角色是否在服里。`, 'unknown');
  } finally {
    inFlight = false;
  }
}

export function startMcPresencePolling(intervalMs = POLL_MS): void {
  if (timer !== null) return;
  void pollMcPresenceOnce();
  timer = setInterval(() => {
    void pollMcPresenceOnce();
  }, intervalMs);
  timer.unref?.();
}

export function stopMcPresencePolling(): void {
  if (timer === null) return;
  clearInterval(timer);
  timer = null;
}
