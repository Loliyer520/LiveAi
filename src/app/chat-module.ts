/**
 * Chat module — wires NapCat adapter, persistence (state + archive),
 * summarizer, prompts, model manager and the AI orchestrator into one Host
 * module (start = connect + listen).
 */

import { join } from 'node:path';
import type { LiveAiModule } from '../host/types.js';
import type { LiveAiConfig } from '../config/config.js';
import { NapcatBot } from '../bot/napcat.js';
import { ScopeRepository } from '../store/repository.js';
import { MessageArchive } from '../store/archive.js';
import { DiarySummarizer } from '../store/summarizer.js';
import { RelationGraph } from '../relations/graph.js';
import { IntelCollector } from '../relations/collector.js';
import { PromptStore } from '../prompt/store.js';
import { ModelManager } from '../models/manager.js';
import { AiOrchestrator } from '../chat/orchestrator.js';
import { initPluginRegistry } from '../chat/plugins.js';

export class ChatModule implements LiveAiModule {
  readonly name = 'chat';

  readonly bot: NapcatBot;
  readonly repo: ScopeRepository;
  readonly archive: MessageArchive;
  readonly summarizer: DiarySummarizer;
  readonly relations: RelationGraph;
  readonly intel: IntelCollector;
  readonly prompts: PromptStore;
  readonly models: ModelManager;
  readonly orchestrator: AiOrchestrator;

  constructor(config: LiveAiConfig) {
    this.archive = new MessageArchive(join(config.storage.stateDir, 'archive'));
    this.repo = new ScopeRepository(config.storage.stateDir, config.ai.historyLimit, this.archive);
    this.prompts = new PromptStore(config.storage.promptDir);
    this.models = new ModelManager(config.storage.modelsConfigPath);
    this.summarizer = new DiarySummarizer(this.repo, this.models, config.ai.summaryEnabled);
    this.relations = new RelationGraph(join(config.storage.stateDir, 'relations.json'));
    this.intel = new IntelCollector(
      this.relations,
      this.repo,
      this.models,
      () => this.bot.selfId,
      config.ai.intelEnabled,
    );
    // Bot first: its handlers only fire on WebSocket events after start(),
    // by which time the orchestrator below exists.
    this.bot = new NapcatBot({
      wsUrl: config.napcat.wsUrl,
      httpUrl: config.napcat.httpUrl,
      selfId: config.napcat.selfId,
      accessToken: config.napcat.accessToken,
      reconnectBaseMs: config.napcat.reconnectBaseMs,
      reconnectMaxMs: config.napcat.reconnectMaxMs,
      handlers: {
        onGroupMessage: (message) => this.orchestrator.handleMessage(message),
        onPrivateMessage: (message) => this.orchestrator.handleMessage(message),
        onSelfMessage: (message) => this.orchestrator.handleSelfMessage(message),
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
      tasksPath: join(config.storage.stateDir, 'tasks.json'),
    });
    initPluginRegistry(process.env.LIVEAI_PLUGIN_DIR ?? join(config.storage.stateDir, '..', 'plugins'));
  }

  async start(): Promise<void> {
    await this.models.load();
    await this.migrateImpressions();
    await this.recoverPendingSummaries();
    this.orchestrator.start();
    this.bot.start();
  }

  async stop(): Promise<void> {
    await this.orchestrator.stop();
    await this.summarizer.drainKnownScopes();
    await this.relations.flush();
    this.bot.stop();
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
