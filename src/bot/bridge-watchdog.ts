/**
 * QQ 宿主僵尸离线看门狗。
 *
 * 病灶（2026-10-02 / 2026-10-13 两次实发）：QQ 账号被踢成僵尸——消息照收
 * （推送通道活着），但一切动作返回 BRIDGE_NOT_READY，bot 表面「装死」。
 * 修复手法固定为 systemctl restart napcat-qq-host（自动重登录）。
 *
 * 本类只做判定，不碰副作用：NapcatBot.post 每次成功/失败回报一次，
 * 连续 BRIDGE_NOT_READY 达到阈值且在冷却期外，回调 onRestartNeeded 一次。
 */

export interface BridgeWatchdogDeps {
  onRestartNeeded: (consecutiveFailures: number) => void;
  /** 连续多少次 BRIDGE_NOT_READY 才判定僵尸（默认 3）。 */
  threshold?: number;
  /** 两次自动重启的最小间隔（默认 180s，重启后 QQ 重登录需要十几秒）。 */
  cooldownMs?: number;
  now?: () => number;
}

const MARKER = 'BRIDGE_NOT_READY';

export class BridgeWatchdog {
  private consecutive = 0;
  private lastRestartAt = Number.NEGATIVE_INFINITY;
  private readonly threshold: number;
  private readonly cooldownMs: number;
  private readonly now: () => number;

  constructor(private readonly deps: BridgeWatchdogDeps) {
    this.threshold = deps.threshold ?? 3;
    this.cooldownMs = deps.cooldownMs ?? 180_000;
    this.now = deps.now ?? (() => Date.now());
  }

  /** 当前连续 BRIDGE_NOT_READY 计数（测试/诊断用）。 */
  get consecutiveFailures(): number {
    return this.consecutive;
  }

  /** 任意一次动作成功 → 计数清零。 */
  recordSuccess(): void {
    this.consecutive = 0;
  }

  /**
   * 动作失败回报。返回 true 表示本次触发了重启回调。
   * 非 BRIDGE_NOT_READY 错误不计数（网络抖动/参数错误不归它管）。
   */
  recordFailure(error: unknown): boolean {
    const message = error instanceof Error ? error.message : String(error);
    if (!message.includes(MARKER)) return false;
    this.consecutive += 1;
    if (this.consecutive < this.threshold) return false;
    return this.tryRestart();
  }

  /**
   * 冷却门控的重启触发（主动探针也用这条路——号被完全踢下线时没有任何
   * 发送动作，recordFailure 永远不会计数，只能靠探针发现）。
   */
  tryRestart(): boolean {
    const now = this.now();
    if (now - this.lastRestartAt < this.cooldownMs) return false;
    this.lastRestartAt = now;
    this.deps.onRestartNeeded(this.consecutive);
    return true;
  }
}
