/**
 * 本机 NapCat 运行环境：一键部署 / 启停 / 状态 / 登录二维码。
 *
 * 部署目标（全部落在固定的两个目录，面板只能操作这些路径）：
 *   /my/run/napcat        NapCat 本体、配置、日志、部署脚本产物
 *   /opt/QQ               Linux QQ（官方 deb）
 *   deeplive-napcat.service  systemd 单元（开机自启、退出拉起）
 *
 * 安装器是 scripts/napcat-install.sh；本模块只负责生成配置、拉起安装进程、
 * 读取状态，不拼接 shell 字符串（避免注入，也方便测试）。
 */

import { execFile, spawn } from 'node:child_process';
import { open, mkdir, readFile, stat, statfs, unlink, writeFile } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { join } from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

/** 安装前至少留 1.5GB：QQ deb ~187MB，装完 + NapCat 约 1GB。 */
const MIN_FREE_BYTES = 1536 * 1024 * 1024;
const TAIL_BYTES = 64 * 1024;

export type NapcatControlAction = 'start' | 'stop' | 'restart';

export interface NapcatRuntimeOptions {
  /** NapCat 数据目录（配置、缓存、日志、launcher）。 */
  readonly home: string;
  /** systemd 单元名（不带 .service）。 */
  readonly serviceName: string;
  /** 仓库根目录（找 scripts/napcat-install.sh）。 */
  readonly projectRoot: string;
  /** bot 反向 WS 监听端口（NapCat 主动连入）。 */
  readonly botPort: number;
  /** bot 的对接模式；只有 server 模式能由本机 NapCat 反向连入。 */
  readonly mode: 'server' | 'client';
  /** 与 bot 共用的 WS Token（写入 NapCat 的 WebSocket 客户端配置）。 */
  readonly accessToken: string;
  /** bot 当前是否已收到 NapCat 连接（由 ChatModule 提供）。 */
  readonly isConnected: () => boolean;
  readonly selfId: () => number;
}

export interface NapcatRuntimeStatus {
  /** 当前系统是否在支持范围内（Linux amd64/arm64）。 */
  readonly supported: boolean;
  /** bot 的 NapCat 对接模式；client 模式不能自动部署本机 NapCat。 */
  readonly mode: 'server' | 'client';
  readonly installed: boolean;
  readonly installing: boolean;
  readonly running: boolean;
  readonly serviceState: string;
  readonly serviceEnabled: boolean;
  readonly napcatVersion: string | null;
  readonly qqVersion: string | null;
  readonly qrcodeUpdatedAt: number | null;
  readonly connected: boolean;
  /** bot 上是否有任意 WS 客户端连入（可能是外部 NapCat，与方式一服务无关）。 */
  readonly clientConnected: boolean;
  readonly selfId: number;
  readonly diskFreeBytes: number | null;
  readonly installExit: 'ok' | 'failed' | null;
  readonly installError: string | null;
  readonly installLogTail: string[];
  readonly serviceLogTail: string[];
}

export interface NapcatQrcode {
  readonly available: boolean;
  readonly updatedAt: number | null;
  /** data:image/png;base64,…；只有 available 时存在。 */
  readonly dataUrl?: string;
}

export interface NapcatActionResult {
  readonly ok: boolean;
  readonly output: string;
}

/** WebSocket 客户端条目（字段名与 NapCat onebot11 配置一致）。 */
export interface NapcatWsClient {
  readonly name: string;
  readonly enable: boolean;
  readonly url: string;
  readonly messagePostFormat: 'array' | 'string';
  readonly reportSelfMessage: boolean;
  readonly reconnectInterval: number;
  readonly token: string;
  readonly debug: boolean;
  readonly heartInterval: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** 生成指向 bot 的 NapCat 反向 WS 客户端配置。 */
export function buildReverseWsClient(wsUrl: string, token: string): NapcatWsClient {
  return {
    name: 'deeplive',
    enable: true,
    url: wsUrl,
    messagePostFormat: 'array',
    reportSelfMessage: false,
    reconnectInterval: 5000,
    token,
    debug: false,
    heartInterval: 30000,
  };
}

/**
 * 把 deeplive 客户端合并进现有 onebot11 配置：
 * 保留用户自己加的 HTTP/WS 服务与其它客户端，只替换同名条目。
 */
export function mergeOnebotConfig(existing: unknown, wsUrl: string, token: string): Record<string, unknown> {
  const base = isRecord(existing) ? existing : {};
  const network = isRecord(base.network) ? base.network : {};
  const current = Array.isArray(network.websocketClients) ? network.websocketClients : [];
  const client = buildReverseWsClient(wsUrl, token);
  const clients: unknown[] = [];
  let replaced = false;
  for (const entry of current) {
    if (isRecord(entry) && entry.name === client.name) {
      if (!replaced) clients.push(client);
      replaced = true;
      continue;
    }
    clients.push(entry);
  }
  if (!replaced) clients.push(client);
  return {
    ...base,
    network: {
      httpServers: Array.isArray(network.httpServers) ? network.httpServers : [],
      httpSseServers: Array.isArray(network.httpSseServers) ? network.httpSseServers : [],
      httpClients: Array.isArray(network.httpClients) ? network.httpClients : [],
      websocketServers: Array.isArray(network.websocketServers) ? network.websocketServers : [],
      plugins: Array.isArray(network.plugins) ? network.plugins : [],
      ...network,
      websocketClients: clients,
    },
  };
}

/** systemd 单元内容：安装器和单元模板共用同一份逻辑，便于测试。 */
export function renderSystemdUnit(home: string, serviceName: string): string {
  return [
    '[Unit]',
    `Description=DeepLive NapCat (${serviceName})`,
    'Wants=network-online.target',
    'After=network-online.target',
    '',
    '[Service]',
    'Type=simple',
    // QQ 启动早期会 stat $HOME，未设置直接 SIGILL（ud2 断言），必须显式给。
    `Environment=HOME=${home}`,
    `WorkingDirectory=${home}`,
    `ExecStart=${home}/launcher.sh`,
    'Restart=always',
    'RestartSec=5',
    'TimeoutStopSec=20',
    'KillMode=mixed',
    'LimitCORE=0',
    `StandardOutput=append:${home}/napcat.log`,
    `StandardError=append:${home}/napcat.log`,
    '',
    '[Install]',
    'WantedBy=multi-user.target',
    '',
  ].join('\n');
}

/** 取文本末尾若干行（日志文件可能很大，先截尾部再 split）。 */
export function tailLines(text: string, lines: number): string[] {
  const rows = text.split(/\r?\n/);
  while (rows.length > 0 && rows[rows.length - 1] === '') rows.pop();
  return rows.slice(Math.max(0, rows.length - lines));
}

async function readJson(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, 'utf-8'));
  } catch {
    return null;
  }
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

/** 读取文件末尾 bytes 字节（不存在/读失败返回空串）。 */
async function readTail(path: string, bytes = TAIL_BYTES): Promise<string> {
  let handle;
  try {
    handle = await open(path, 'r');
    const info = await handle.stat();
    const start = Math.max(0, info.size - bytes);
    const length = info.size - start;
    if (length <= 0) return '';
    const buffer = Buffer.alloc(length);
    await handle.read(buffer, 0, length, start);
    return buffer.toString('utf-8');
  } catch {
    return '';
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

function versionOf(value: unknown): string | null {
  if (!isRecord(value)) return null;
  const raw = value.version ?? value.buildVersion;
  if (typeof raw === 'string' || typeof raw === 'number') return String(raw);
  return null;
}

/**
 * NapCat.Shell.zip 自带的 package.json 版本恒为 0.0.1，真实版本由安装器写进
 * napcat/VERSION（GitHub release tag）。这里只接受像版本号的文本，避免把
 * 半截日志/污染内容显示到面板上。
 */
export function versionFromMarker(text: string): string | null {
  const raw = text.trim();
  return /^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$/.test(raw) ? raw : null;
}

export class NapcatRuntime {
  private readonly opts: NapcatRuntimeOptions;
  /** 同一进程内的部署互斥（跨进程由 .install.pid 兜底）。 */
  private spawning = false;

  constructor(options: NapcatRuntimeOptions) {
    this.opts = options;
  }

  get home(): string {
    return this.opts.home;
  }

  get serviceName(): string {
    return this.opts.serviceName;
  }

  get installerPath(): string {
    return join(this.opts.projectRoot, 'scripts', 'napcat-install.sh');
  }

  get wsUrl(): string {
    return `ws://127.0.0.1:${this.opts.botPort}`;
  }

  private get configDir(): string {
    return join(this.opts.home, 'napcat', 'config');
  }

  private get installLogPath(): string {
    return join(this.opts.home, 'install.log');
  }

  private get serviceLogPath(): string {
    return join(this.opts.home, 'napcat.log');
  }

  private get pidPath(): string {
    return join(this.opts.home, '.install.pid');
  }

  private get statePath(): string {
    return join(this.opts.home, '.install-state.json');
  }

  private get qrcodePath(): string {
    return join(this.opts.home, 'napcat', 'cache', 'qrcode.png');
  }

  /** 读取安装进程 pid；进程已退出或 pid 被复用时返回 null 并清掉残留文件。 */
  private async installPid(): Promise<number | null> {
    let raw: string;
    try {
      raw = (await readFile(this.pidPath, 'utf-8')).trim();
    } catch {
      return null;
    }
    const pid = Number(raw);
    if (!Number.isInteger(pid) || pid <= 0) {
      await unlink(this.pidPath).catch(() => undefined);
      return null;
    }
    try {
      process.kill(pid, 0);
    } catch {
      await unlink(this.pidPath).catch(() => undefined);
      return null;
    }
    // 防 PID 复用：确认命令行确实是安装脚本，而不是恰好拿到同一个号的进程。
    try {
      const cmdline = await readFile(`/proc/${pid}/cmdline`, 'utf-8');
      if (!cmdline.includes('napcat-install.sh')) {
        await unlink(this.pidPath).catch(() => undefined);
        return null;
      }
    } catch {
      return null;
    }
    return pid;
  }

  /** 部署前写入/合并 NapCat 配置；幂等，可重复调用。 */
  async ensureConfig(): Promise<void> {
    await mkdir(this.configDir, { recursive: true });
    const onebotPath = join(this.configDir, 'onebot11.json');
    const merged = mergeOnebotConfig(await readJson(onebotPath), this.wsUrl, this.opts.accessToken);
    await writeFile(onebotPath, `${JSON.stringify(merged, null, 2)}\n`, 'utf-8');

    const webuiPath = join(this.configDir, 'webui.json');
    if (!(await exists(webuiPath))) {
      await writeFile(webuiPath, `${JSON.stringify({
        host: '127.0.0.1',
        port: 6099,
        token: randomBytes(16).toString('hex'),
        loginRate: 3,
      }, null, 2)}\n`, 'utf-8');
    }
  }

  /** 一键部署/更新。安装器在后台跑，进度看 status().installLogTail。 */
  async install(): Promise<{ ok: boolean; error?: string }> {
    if (this.opts.mode !== 'server') {
      return { ok: false, error: '当前是 client 模式；本机 NapCat 只支持反向连入（server 模式）' };
    }
    if (this.opts.botPort <= 0) {
      return { ok: false, error: 'bot 的反向 WS 端口未配置，无法自动连接' };
    }
    if (this.spawning || await this.installPid() !== null) {
      return { ok: false, error: '部署正在进行中，请稍候' };
    }
    if (!(await exists(this.installerPath))) {
      return { ok: false, error: `找不到安装脚本：${this.installerPath}` };
    }
    const free = await this.diskFree();
    if (!(await exists('/opt/QQ/qq')) && free !== null && free < MIN_FREE_BYTES) {
      return {
        ok: false,
        error: `磁盘剩余 ${(free / 1024 / 1024 / 1024).toFixed(1)}GB，不足 1.5GB。请先清理再部署`,
      };
    }

    await this.ensureConfig();
    await mkdir(this.opts.home, { recursive: true });
    const log = await open(this.installLogPath, 'a');
    this.spawning = true;
    // 先落 running 状态再 spawn：脚本失败得很快时，EXIT trap 写下的 failed 不会被覆盖。
    await writeFile(this.statePath, `${JSON.stringify({ status: 'running', startedAt: Date.now() })}\n`, 'utf-8');
    try {
      const child = spawn('/usr/bin/env', ['bash', this.installerPath], {
        cwd: this.opts.home,
        detached: true,
        stdio: ['ignore', log.fd, log.fd],
        env: {
          ...process.env,
          NAPCAT_HOME: this.opts.home,
          NAPCAT_SERVICE: this.opts.serviceName,
          NAPCAT_WS_URL: this.wsUrl,
          NAPCAT_WS_TOKEN: this.opts.accessToken,
        },
      });
      // 没有 error 监听时 spawn 失败（bash/env 不存在）是未捕获异常，会带崩 bot。
      child.on('error', (error) => {
        console.error('[napcat] 部署进程启动失败:', error);
      });
      child.unref();
      if (child.pid === undefined) return { ok: false, error: '安装进程启动失败' };
      await writeFile(this.pidPath, `${child.pid}\n`, 'utf-8');
    } finally {
      this.spawning = false;
      await log.close();
    }
    return { ok: true };
  }

  async control(action: NapcatControlAction): Promise<NapcatActionResult> {
    if (!['start', 'stop', 'restart'].includes(action)) {
      return { ok: false, output: '未知操作' };
    }
    const result = await this.systemctl(action, this.opts.serviceName);
    return { ok: result.code === 0, output: (result.stdout || result.stderr || '').trim() };
  }

  async qrcode(): Promise<NapcatQrcode> {
    try {
      const info = await stat(this.qrcodePath);
      if (!info.isFile() || info.size <= 0 || info.size > 2 * 1024 * 1024) {
        return { available: false, updatedAt: null };
      }
      const image = await readFile(this.qrcodePath);
      return {
        available: true,
        updatedAt: info.mtimeMs,
        dataUrl: `data:image/png;base64,${image.toString('base64')}`,
      };
    } catch {
      return { available: false, updatedAt: null };
    }
  }

  /** 安装日志 + NapCat 自身日志尾部；面板排障用。 */
  async logs(lines = 120): Promise<{ install: string[]; service: string[] }> {
    const capped = Math.min(Math.max(lines, 10), 500);
    return {
      install: tailLines(await readTail(this.installLogPath), capped),
      service: tailLines(await readTail(this.serviceLogPath), capped),
    };
  }

  async status(): Promise<NapcatRuntimeStatus> {
    const supported = process.platform === 'linux' && (process.arch === 'x64' || process.arch === 'arm64');
    const installed = await exists(join(this.opts.home, 'napcat', 'napcat.mjs'))
      && await exists('/opt/QQ/qq');
    const installing = await this.installPid() !== null;
    const active = await this.systemctl('is-active', this.opts.serviceName);
    const enabled = await this.systemctl('is-enabled', this.opts.serviceName);
    const serviceState = (active.stdout || 'unknown').trim() || 'unknown';
    const state = await readJson(this.statePath);
    let installExit: 'ok' | 'failed' | null = null;
    let installError: string | null = null;
    if (isRecord(state)) {
      if (state.status === 'ok') installExit = 'ok';
      if (state.status === 'failed') {
        installExit = 'failed';
        if (typeof state.message === 'string' && state.message !== '') installError = state.message;
      }
    }
    if (!installing && installExit === null && isRecord(state) && state.status === 'running') {
      // 进程没了但状态没落盘：按失败处理，别让面板永远转圈。
      installExit = 'failed';
      installError = '部署进程意外退出，请查看安装日志';
    }
    const qr = await stat(this.qrcodePath).catch(() => null);
    const installLog = await readTail(this.installLogPath);
    const serviceLog = await readTail(this.serviceLogPath);
    const napcatVersion = await this.napcatVersion();

    return {
      supported,
      mode: this.opts.mode,
      installed,
      installing,
      running: serviceState === 'active',
      serviceState,
      serviceEnabled: (enabled.stdout || '').trim() === 'enabled',
      napcatVersion,
      qqVersion: versionOf(await readJson('/opt/QQ/resources/app/package.json')),
      qrcodeUpdatedAt: qr?.isFile() ? qr.mtimeMs : null,
      connected: serviceState === 'active' && this.opts.isConnected(),
      /** bot 收到的 WS 连接与方式一服务无关时（比如用户自己电脑连入测试），面板要能区分。 */
      clientConnected: this.opts.isConnected(),
      selfId: this.opts.selfId(),
      diskFreeBytes: await this.diskFree(),
      installExit,
      installError,
      installLogTail: tailLines(installLog, 40),
      serviceLogTail: tailLines(serviceLog, 40),
    };
  }

  /** 优先读安装器写下的 VERSION，退回 package.json（旧部署没有标记文件）。 */
  private async napcatVersion(): Promise<string | null> {
    const marker = await readFile(join(this.opts.home, 'napcat', 'VERSION'), 'utf-8').catch(() => null);
    const fromMarker = marker === null ? null : versionFromMarker(marker);
    if (fromMarker !== null) return fromMarker;
    return versionOf(await readJson(join(this.opts.home, 'napcat', 'package.json')));
  }

  private async diskFree(): Promise<number | null> {
    try {
      const info = await statfs(this.opts.home);
      return info.bsize * info.bavail;
    } catch {
      try {
        const info = await statfs('/');
        return info.bsize * info.bavail;
      } catch {
        return null;
      }
    }
  }

  private async systemctl(...args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
    try {
      const result = await execFileAsync('systemctl', args, { timeout: 8000, maxBuffer: 64 * 1024 });
      return { code: 0, stdout: String(result.stdout ?? ''), stderr: String(result.stderr ?? '') };
    } catch (error) {
      const err = error as { code?: unknown; stdout?: unknown; stderr?: unknown };
      return {
        code: typeof err.code === 'number' ? err.code : 1,
        stdout: String(err.stdout ?? ''),
        stderr: String(err.stderr ?? ''),
      };
    }
  }
}
