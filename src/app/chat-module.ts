/**
 * Chat module — wires NapCat adapter, persistence (state + archive),
 * summarizer, prompts, model manager and the AI orchestrator into one Host
 * module (start = connect + listen).
 */

import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import { exec } from 'node:child_process';
import type { LiveAiModule } from '../host/types.js';
import type { LiveAiConfig } from '../config/config.js';
import { NapcatBot } from '../bot/napcat.js';
import { BridgeWatchdog } from '../bot/bridge-watchdog.js';
import { ScopeRepository } from '../store/repository.js';
import { MessageArchive } from '../store/archive.js';
import { DiarySummarizer } from '../store/summarizer.js';
import { RelationGraph } from '../relations/graph.js';
import { KnowledgeBase } from '../knowledge/kb.js';
import { IntelCollector } from '../relations/collector.js';
import { PromptStore } from '../prompt/store.js';
import { ModelManager } from '../models/manager.js';
import { AiOrchestrator } from '../chat/orchestrator.js';
import { NovelCommandHandler } from '../chat/novel-cmd.js';
import { initPluginRegistry } from '../chat/plugins.js';
import { configureMcBot, configureStickers, configureTts } from '../chat/tools.js';
import { configureMediaSpool } from '../chat/media-spool.js';
import { configureMc2d, mc2dClient } from '../chat/mc2d.js';

export class ChatModule implements LiveAiModule {
  readonly name = 'chat';

  readonly bot: NapcatBot;
  readonly repo: ScopeRepository;
  readonly archive: MessageArchive;
  readonly summarizer: DiarySummarizer;
  readonly relations: RelationGraph;
  readonly kb: KnowledgeBase;
  readonly intel: IntelCollector;
  readonly prompts: PromptStore;
  readonly models: ModelManager;
  readonly orchestrator: AiOrchestrator;
  private stickerRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly watchdogTimers = new Set<ReturnType<typeof setTimeout>>();
  private readonly watchdogEnabled = process.env.LIVEAI_BRIDGE_WATCHDOG !== '0';
  private readonly bridgeWatchdog: BridgeWatchdog | null;
  private bridgeProbeTimer: ReturnType<typeof setInterval> | null = null;
  private bridgeProbeFailures = 0;
  /** 重启后验证仍不在线 → 登录态失效，需要人工扫码，停止自动重启。 */
  private bridgeNeedsHuman = false;
  private lastHumanWarnAt = 0;
  /** 反向 WS 模式（NapCat 跑在主人本地，主动连入）：看门狗不重启，只通知。 */
  private readonly reverseMode: boolean;
  private readonly masterQq: number;
  private readonly napcatHttpUrl: string;
  private readonly napcatToken: string;
  private readonly mc2dAutoJoin: boolean;
  /** Minecraft 边车连接信息（管理面板代理探测用）。 */
  readonly mcBotUrl: string;
  readonly mcBotToken: string;

  constructor(config: LiveAiConfig) {
    this.archive = new MessageArchive(join(config.storage.stateDir, 'archive'));
    this.repo = new ScopeRepository(config.storage.stateDir, config.ai.historyLimit, this.archive);
    this.prompts = new PromptStore(config.storage.promptDir);
    this.models = new ModelManager(config.storage.modelsConfigPath);
    this.summarizer = new DiarySummarizer(this.repo, this.models, config.ai.summaryEnabled);
    this.relations = new RelationGraph(join(config.storage.stateDir, 'relations.json'));
    this.kb = new KnowledgeBase(
      join(config.storage.stateDir, '..', 'knowledge'),
      `private:${config.ai.masterQq}`,
    );
    this.intel = new IntelCollector(
      this.relations,
      this.repo,
      this.models,
      () => this.bot.selfId,
      config.ai.intelEnabled,
    );
    // Bot first: its handlers only fire on WebSocket events after start(),
    // by which time the orchestrator below exists.
    this.masterQq = config.ai.masterQq;
    this.napcatHttpUrl = config.napcat.httpUrl;
    this.napcatToken = config.napcat.accessToken ?? '';
    this.bridgeWatchdog = this.watchdogEnabled
      ? new BridgeWatchdog({ onRestartNeeded: (n) => this.onBridgeZombie(n) })
      : null;
    this.reverseMode = config.napcat.mode === 'server';
    this.bot = new NapcatBot({
      mode: config.napcat.mode,
      wsUrl: config.napcat.wsUrl,
      httpUrl: config.napcat.httpUrl,
      selfId: config.napcat.selfId,
      legacySelfIds: config.napcat.legacySelfIds,
      accessToken: config.napcat.accessToken,
      reconnectBaseMs: config.napcat.reconnectBaseMs,
      reconnectMaxMs: config.napcat.reconnectMaxMs,
      watchdog: this.bridgeWatchdog ?? undefined,
      reverseWsPort: this.reverseMode ? config.napcat.reverseWsPort : undefined,      handlers: {
        onGroupMessage: (message) => this.orchestrator.handleMessage(message),
        onPrivateMessage: (message) => this.orchestrator.handleMessage(message),
        onSelfMessage: (message) => this.orchestrator.handleSelfMessage(message),
        onDatalineMessage: (message) => this.orchestrator.handleDatalineMessage(message),
        onConnected: () => console.info(`[napcat] websocket connected (${this.bot.selfId || 'self_id 待学习'})`),
        onDisconnected: (reason) => console.info(`[napcat] websocket disconnected: ${reason}, reconnecting…`),
      },
    });
    this.orchestrator = new AiOrchestrator({
      bot: this.bot,
      repo: this.repo,
      promptStore: this.prompts,
      models: this.models,
      config: config.ai,
      archive: this.archive,
      summarizer: this.summarizer,
      relations: this.relations,
      intel: this.intel,
      kb: this.kb,
      tasksPath: join(config.storage.stateDir, 'tasks.json'),
      novelCmd: config.novelCmd.scopes.length > 0
        ? new NovelCommandHandler({
          scopes: config.novelCmd.scopes,
          baseUrl: config.novelCmd.baseUrl,
          fallbackBaseUrls: config.novelCmd.fallbackBaseUrls,
          statePath: join(config.storage.stateDir, 'novel-cmd.json'),
          bot: this.bot,
          repo: this.repo,
        })
        : null,
    });
    initPluginRegistry(process.env.LIVEAI_PLUGIN_DIR ?? join(config.storage.stateDir, '..', 'plugins'));
    configureTts(config.tts.baseUrl, config.tts.timeoutMs);
    configureStickers(join(config.storage.stateDir, '..', 'stickers', 'index.json'));
    // 反向 WS 模式下 NapCat 在号主本地：视频/语音文件经 /media/ 外吐给它拉取。
    configureMediaSpool(
      this.reverseMode && config.napcat.publicBaseUrl !== ''
        ? { dir: join(config.storage.stateDir, 'media-spool'), publicBase: config.napcat.publicBaseUrl }
        : null,
    );
    this.mc2dAutoJoin = config.mc2d.autoJoin;
    configureMc2d(
      config.mc2d.enabled
        ? { wsUrl: config.mc2d.wsUrl, name: config.mc2d.name, uid: config.mc2d.uid }
        : null,
    );
    // Minecraft 边车：URL 可 env 覆盖；token 与 mc 插件共用 data/plugins/secrets.json
    let mcBotToken = '';
    try {
      const secretsPath = join(config.storage.stateDir, '..', 'plugins', 'secrets.json');
      const secrets = JSON.parse(readFileSync(secretsPath, 'utf-8')) as Record<string, string>;
      mcBotToken = typeof secrets.MCBOT_TOKEN === 'string' ? secrets.MCBOT_TOKEN : '';
    } catch {
      // 没装 mc 插件/没有 secrets → mc_view 会收到 401，报错信息里已指路
    }
    const mcBotUrl = process.env.LIVEAI_MCBOT_URL ?? 'http://127.0.0.1:7660';
    configureMcBot(mcBotUrl, mcBotToken);
    this.mcBotUrl = mcBotUrl;
    this.mcBotToken = mcBotToken;
  }

  async start(): Promise<void> {
    await this.models.load();
    await this.migrateImpressions();
    await this.recoverPendingSummaries();
    this.orchestrator.start();
    this.bot.start();
    this.scheduleStickerRefresh();
    this.startBridgeProbe();
    if (this.mc2dAutoJoin) mc2dClient()?.autoJoin();
  }

  async stop(): Promise<void> {
    if (this.stickerRefreshTimer !== null) {
      clearTimeout(this.stickerRefreshTimer);
      this.stickerRefreshTimer = null;
    }
    for (const timer of this.watchdogTimers) clearTimeout(timer);
    this.watchdogTimers.clear();
    if (this.bridgeProbeTimer !== null) {
      clearInterval(this.bridgeProbeTimer);
      this.bridgeProbeTimer = null;
    }
    await this.orchestrator.stop();
    await this.summarizer.drainKnownScopes();
    await this.relations.flush();
    mc2dClient()?.shutdown();
    this.bot.stop();
  }

  /**
   * QQ 宿主僵尸离线处置（2026-10-02 / 10-13 两次实发病灶）：
   * 连续 BRIDGE_NOT_READY → 自动重启 napcat-qq-host（重登录自愈），
   * 25s 后验证 online，恢复则私聊通知主人。
   * 反向 WS 模式下宿主在主人本地，重启服务器侧无意义——只通知。
   */
  private onBridgeZombie(consecutiveFailures: number): void {
    if (this.reverseMode) {
      console.warn(`[bridge-watchdog] 连续 ${consecutiveFailures} 次动作失败（反向模式，宿主在号主本地，无法远程处置）`);
      return;
    }
    console.warn(`[bridge-watchdog] 连续 ${consecutiveFailures} 次 BRIDGE_NOT_READY，自动重启 napcat-qq-host`);
    exec('systemctl restart napcat-qq-host', (error) => {
      if (error) console.error('[bridge-watchdog] restart failed:', error.message);
    });
    const verify = setTimeout(() => {
      this.watchdogTimers.delete(verify);
      void this.verifyBridgeRecovered(consecutiveFailures);
    }, 25_000);
    verify.unref();
    this.watchdogTimers.add(verify);
  }

  private async verifyBridgeRecovered(consecutiveFailures: number): Promise<void> {
    try {
      const status = await this.bot.fetchStatus();
      if (!status.online) {
        this.bridgeNeedsHuman = true;
        console.warn(`[bridge-watchdog] 重启后 25s 仍未 online（phase=${status.phase}）——登录态失效，自动重启无效，需人工重新登录`);
        return;
      }
      this.bridgeNeedsHuman = false;
      console.info('[bridge-watchdog] QQ 已恢复在线');
      await this.bot.sendText(
        'private',
        this.masterQq,
        `[系统] 检测到 QQ 宿主僵尸离线（连续 ${consecutiveFailures} 次发送 BRIDGE_NOT_READY），已自动重启 napcat-qq-host，现已恢复在线。`,
      );
    } catch (error) {
      console.warn('[bridge-watchdog] verify/notify failed:', error instanceof Error ? error.message : String(error));
    }
  }

  /**
   * 主动探针（每 60s）：被动计数有盲区——号被腾讯「登录已失效」整体踢下线时
   * 没有任何发送动作（2026-10-13 22:48 实发，静默到次日才发现），只能靠探针。
   * 连续 2 次 online:false 走与被动相同的冷却门控重启；重启验证失败置
   * needsHuman 后停止自动重启（登录态失效重启无用），每 30 分钟吼一次日志。
   */
  private startBridgeProbe(): void {
    if (!this.watchdogEnabled || this.bridgeWatchdog === null) return;
    this.bridgeProbeTimer = setInterval(() => void this.probeBridge(), 60_000);
    this.bridgeProbeTimer.unref();
  }

  private async probeBridge(): Promise<void> {
    try {
      const status = await this.bot.fetchStatus();
      if (status.online) {
        this.bridgeProbeFailures = 0;
        this.bridgeNeedsHuman = false;
        return;
      }
      this.bridgeProbeFailures += 1;
      const phase = status.phase;
      console.warn(`[bridge-watchdog] 探针发现 QQ 不在线（连续 ${this.bridgeProbeFailures} 次，phase=${phase}）`);
      if (this.bridgeNeedsHuman) {
        if (Date.now() - this.lastHumanWarnAt > 30 * 60_000) {
          this.lastHumanWarnAt = Date.now();
          console.warn('[bridge-watchdog] QQ 登录态已失效，自动重启无法恢复，需人工扫码重新登录（NapCat WebUI :7630 → GetQQLoginQrcode）');
        }
        return;
      }
      if (this.bridgeProbeFailures >= 2) this.bridgeWatchdog?.tryRestart();
    } catch {
      // 探针自身失败 = napcat backend 都没响应，WS 重连逻辑会管，不计数
    }
  }

  /**
   * 启动后延时同步一次表情包库（fetch_custom_face → 按 MD5 增量 → 新表情
   * 视觉打标）。延时是等 NapCat 连接/登录就绪；无新表情时只花一次清单拉取。
   */
  private scheduleStickerRefresh(): void {
    this.stickerRefreshTimer = setTimeout(() => {
      this.stickerRefreshTimer = null;
      this.orchestrator
        .refreshStickerLibrary()
        .then((result) => console.info(`[chat-module] sticker refresh: ${result.message}`))
        .catch((error) =>
          console.warn('[chat-module] sticker refresh failed:', error instanceof Error ? error.message : String(error))
        );
    }, 45_000);
    this.stickerRefreshTimer.unref();
  }

  /** Restart recovery: scopes with unsummarized diary segments re-enqueue. */
  private async recoverPendingSummaries(): Promise<void> {
    for (const scopeKey of await this.repo.listPersistedScopes()) {
      const separator = scopeKey.indexOf(':');
      const scopeType = scopeKey.slice(0, separator);
      const scopeId = scopeKey.slice(separator + 1);
      if (this.repo.getUnsummarizedSegments(scopeType, scopeId).length > 0) {
        this.summarizer.notify(scopeType, scopeId);
      }
    }
  }

  /**
   * One-time merge: per-scope 印象 used to live on the agent record; it now
   * belongs to the relation graph's scope node. Adopt any legacy value that
   * the graph doesn't already have.
   */
  private async migrateImpressions(): Promise<void> {
    try {
      for (const scopeKey of await this.repo.listPersistedScopes()) {
        const separator = scopeKey.indexOf(':');
        const scopeType = scopeKey.slice(0, separator);
        const scopeId = scopeKey.slice(separator + 1);
        const legacy = this.repo.getOrCreateAgent(scopeType, scopeId).impression;
        if (legacy && !this.relations.scopeImpression(scopeKey)) {
          this.relations.setScopeImpression(scopeKey, legacy);
        }
      }
      await this.relations.flush();
    } catch (error) {
      console.error('[chat-module] impression migration failed:', error);
    }
  }
}
