/**
 * AI orchestrator — the chat runtime. Ported from legacy core/ai_runtime.py's
 * message path, preserving the preemption ("插队") semantics end to end:
 *
 *  1. inbound: dedup → persist → trigger decision (private/@/words/rate) →
 *     debounce window touch → submit to the scope mailbox (FIFO per scope).
 *  2. per-scope actor consumes one turn at a time; a burst that arrives while
 *     the scope is busy simply queues (mailbox), it never interleaves.
 *  3. mid-turn: after every tool round the mailbox is drained and folded into
 *     the next round's trigger messages (deferred count rises → 补审提醒 tells
 *     the model its previous draft is void).
 *  4. post-turn: the coordinator drains again and merges everything into ONE
 *     follow-up turn (skip if only silent events queued).
 *  5. epoch guard: global bumps (clear / reload) invalidate queued turns;
 *     stale-by-age messages are dropped at every boundary.
 *  6. crash guard: on exception the executed-tool inventory is appended to
 *     history so the next turn knows what side effects really happened.
 */

import { NapcatBot } from '../bot/napcat.js';
import { dirname, join } from 'node:path';
import { mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { ScopeRepository } from '../store/repository.js';
import type { MessageArchive } from '../store/archive.js';
import type { DiarySummarizer } from '../store/summarizer.js';
import { TurnLogStore } from '../store/turn-log.js';
import type { RelationGraph } from '../relations/graph.js';
import type { KnowledgeBase } from '../knowledge/kb.js';
import { domainDefForScope, hubForScope } from './domains.js';
import { withProbe } from '../observability/probe.js';
import type { IntelCollector } from '../relations/collector.js';
import { PromptStore } from '../prompt/store.js';
import { buildChildMessages } from '../prompt/assemble.js';
import { ModelManager } from '../models/manager.js';
import { completeChatTracked } from '../models/usage.js';
import { ProtocolError } from '../models/protocol.js';
import { InMemoryEventMailbox } from '../scope/mailbox.js';
import { CharacterSessionRegistry } from '../scope/session.js';
import { ScopeActorDispatcher } from '../scope/actor.js';
import { AtomicTurnBatchCoordinator, mergeFollowupItems } from '../scope/coordinator.js';
import { envelopeFromTurnItem, type TurnItem } from '../scope/turn.js';
import type { ChatMessage, HistoryEntry, ModelMessage, ToolCall, TriggerEntry } from './types.js';
import { MASTER_SCOPE_ID, MASTER_SCOPE_KEY, MASTER_SCOPE_TYPE, isInternalReportEntry, isMasterScope as isMasterScopeOf, messageNickname } from './types.js';
import { cleanText, markMentionsSelf, shouldIgnoreMessage, sourceKindOf, sourceLabelOf } from './source.js';
import { GroupReplyWindows, buildDebounceTriggerMessage, shouldTrigger } from './trigger.js';
import { GroupRolesCache } from './group-roles.js';
import { chatToolSchemas, executeChatTool, refreshStickerIndex, type StickerRefreshResult, type ToolContext, type ToolResult } from './tools.js';
import { TaskScheduler, type ScheduledTask } from './scheduler.js';
import type { NovelCommandHandler } from './novel-cmd.js';
import { embedImageAsDataUrl, extractImageRefs } from './images.js';
import { cacheImage, cachedImageUri, ensureImageCached } from './image-cache.js';
import { extractFileRefs, extractVideoRefs } from './files.js';
import { replyMessageLines } from './reply-lines.js';
import { Workspace } from './workspace.js';
import { globalIdentityCard } from '../store/global-identity.js';
import { listPersonaRefs } from '../store/persona-refs.js';
import type { AiConfig } from '../config/config.js';

const MAX_TOOL_ITERATIONS = 12;
const MAX_SCHEDULED_TURN_RETRIES = 2;
const SCHEDULED_RETRY_BASE_MS = 1_000;
/** 面向用户发消息的工具：执行前过一道「新消息拦截」（仿真人先看一眼再说）。 */
const SEND_INTERLOCK_TOOLS = new Set(['send_message', 'send_image', 'send_video', 'send_sticker']);
const SOFT_ERROR_PATTERNS = [
  'timeout', 'timed out', 'status=5', 'connection', 'overloaded', 'rate limit',
  '429', '502', '503', '504', 'empty content',
];

export class AiOrchestrator {
  private readonly mailbox = new InMemoryEventMailbox();
  private readonly sessions: CharacterSessionRegistry;
  private readonly coordinator: AtomicTurnBatchCoordinator;
  private readonly dispatcher: ScopeActorDispatcher;
  private readonly groupWindows: GroupReplyWindows;
  private readonly promptStore: PromptStore;
  private readonly repo: ScopeRepository;
  private readonly models: ModelManager;
  private readonly bot: NapcatBot;
  private readonly config: AiConfig;
  private readonly archive: MessageArchive;
  private readonly summarizer: DiarySummarizer;
  private readonly relations: RelationGraph;
  private readonly intel: IntelCollector;
  private readonly kb: KnowledgeBase;
  private readonly tasks: TaskScheduler;
  private readonly workspaceDir: string;
  private readonly groupRoles: GroupRolesCache;
  /** 小说阅读指令机（不走模型；null = 未配置启用 scope）。 */
  private readonly novelCmd: NovelCommandHandler | null;
  /** Per-scope tool-call and turn-summary logs (browsable in WebUI). */
  private readonly turnLog: TurnLogStore;

  private messageEpoch = 0;
  private readonly recentMessageKeys = new Map<string, number>();
  private readonly scheduledRetryTimers = new Set<ReturnType<typeof setTimeout>>();
  private running = false;
  /** 主AI回复的出路：跟随主人最近一次的来路（QQ 私聊 or 手机数据线）。 */
  /** 主AI 回复的去向：数据线来的回合回主人手机；其余（面板/下级上报）send_message 只落主AI会话，面板可见。 */
  private masterChannel: 'panel' | 'dataline' = 'panel';

  constructor(deps: {
    bot: NapcatBot;
    repo: ScopeRepository;
    promptStore: PromptStore;
    models: ModelManager;
    config: AiConfig;
    archive: MessageArchive;
    summarizer: DiarySummarizer;
    relations: RelationGraph;
    intel: IntelCollector;
    /** 知识库（条目 + 挂载 ACL + 下级追加待审核队列）。 */
    kb: KnowledgeBase;
    /** Path of the persisted tasks.json (alarms + recurring tasks). */
    tasksPath: string;
    /** Root of per-scope file workspaces (defaults to <stateDir>/workspace). */
    workspaceDir?: string;
    /** 小说阅读指令机（可选）：指定 scope 的整句指令直接应答，跳过模型。 */
    novelCmd?: NovelCommandHandler | null;
  }) {
    this.bot = deps.bot;
    this.repo = deps.repo;
    this.promptStore = deps.promptStore;
    this.models = deps.models;
    this.config = deps.config;
    this.archive = deps.archive;
    this.summarizer = deps.summarizer;
    this.relations = deps.relations;
    this.intel = deps.intel;
    this.kb = deps.kb;
    this.tasks = new TaskScheduler(deps.tasksPath, (task) => this.fireTask(task));
    this.novelCmd = deps.novelCmd ?? null;
    this.workspaceDir = deps.workspaceDir ?? join(dirname(deps.tasksPath), 'workspace');
    this.turnLog = new TurnLogStore(join(dirname(deps.tasksPath), 'turn-logs'));
    this.groupRoles = new GroupRolesCache(deps.bot);
    this.sessions = new CharacterSessionRegistry(this.mailbox);
    this.coordinator = new AtomicTurnBatchCoordinator(this.mailbox);
    this.dispatcher = new ScopeActorDispatcher({
      mailbox: this.mailbox,
      sessions: this.sessions,
      consume: (scopeKey, item) => this.consumeTurn(scopeKey, item),
      isStale: (item) => this.isMessageStale(item.message),
      onIdle: () => this.purgeRecentKeys(),
    });
    this.groupWindows = new GroupReplyWindows({
      isEpochStale: (epoch) => this.isEpochStale(epoch),
      isScopeBusy: (scopeKey) => this.scopeBusy(scopeKey),
      fireTrigger: (scopeKey, scopeId, epoch) => this.fireGroupDebounce(scopeKey, scopeId, epoch),
    });
  }

  start(): void {
    this.running = true;
    this.tasks.start(); // recovers persisted alarms/recurring tasks (fires overdue)
  }

  /** Per-scope tool-call / turn-summary logs (panel reads these). */
  get logs(): TurnLogStore {
    return this.turnLog;
  }

  async stop(): Promise<void> {
    this.running = false;
    this.bumpEpoch();
    this.groupWindows.cancelAll();
    this.tasks.stop(); // timers cleared; tasks.json persists for next boot
    for (const timer of this.scheduledRetryTimers) clearTimeout(timer);
    this.scheduledRetryTimers.clear();
    await this.dispatcher.close();
    await this.repo.flush();
    await this.archive.flush();
    await this.intel.drain();
    await this.relations.flush();
    await this.kb.flush();
    await this.turnLog.flush();
  }

  // ── inbound ───────────────────────────────────────────────────────────────

  handleMessage(message: ChatMessage): void {
    if (!this.running) return;
    if (this.isCurrentSelf(message.userId)) return;
    // Self-chat (QQ "发给自己") is not a conversation scope — never process it.
    if (message.chatType === 'private' && this.isCurrentSelf(message.chatId)) return;
    // 面板控制台来的 → 主AI回复回面板（数据线通道让位）。号主 QQ 私聊是普通下级会话，与主AI无关。
    if (message.chatType === MASTER_SCOPE_TYPE && (message.rawData as Record<string, unknown>)?.source === 'admin_webui') {
      this.masterChannel = 'panel';
    }
    if (this.isDuplicateEvent(message)) return;
    void this.enqueueMessage(message);
  }

  /**
   * QQ 数据线（我的电脑/我的手机）入口：主人在手机上用同一账号给 Linux 发消息。
   * 这是号主直连主AI的控制台通道：注入主AI会话（master:core），并记住来路——
   * 此后主AI的回复经 sendTextOverride 改走数据线回主人手机。
   */
  handleDatalineMessage(message: ChatMessage): void {
    if (!this.running) return;
    this.masterChannel = 'dataline';
    const relayed: ChatMessage = {
      ...message,
      chatType: MASTER_SCOPE_TYPE,
      chatId: MASTER_SCOPE_ID,
      userId: this.config.masterQq,
      rawData: { ...(message.rawData as Record<string, unknown>), source: 'dataline' },
    };
    if (this.isDuplicateEvent(relayed)) return;
    void this.enqueueMessage(relayed);
  }

  /** Friend/group request events go only to the master scope, never into user chats. */
  handleQqRequestEvent(event: Record<string, unknown>): void {
    if (!this.running) return;
    const text = formatQqRequestEvent(event);
    const message: ChatMessage = {
      chatType: MASTER_SCOPE_TYPE,
      chatId: MASTER_SCOPE_ID,
      userId: 0,
      text,
      rawMessage: text,
      sender: { nickname: 'QQ 系统' },
      messageId: null,
      mentionsSelf: false,
      timestamp: Number(event.time ?? Date.now() / 1000),
      rawData: { ...event, source: 'qq_request_event' },
    };
    if (this.isDuplicateEvent(message)) return;
    void this.enqueueMessage(message);
  }

  /**
   * 其他设备的同账号消息（主人在手机上用本账号聊天）：进上下文但不触发生成。
   * 移植 legacy _enqueue_self_message：写历史（标签 本账号其他设备）+ 以
   * silentEvent 提交——空闲时被 consumeTurn 的静默头守卫消化掉；有回合在跑
   * 时并入触发上下文，模型立刻"看到"。回声已由 napcat 层双层去重挡掉。
   */
  handleSelfMessage(message: ChatMessage): void {
    if (!this.running) return;
    // 自我会话（QQ「发给自己」）不是对话场所，永远不进上下文（会成环）。
    if (message.chatType === 'private' && this.isCurrentSelf(message.chatId)) return;
    const text = (message.text || '').trim();
    if (!text) return;
    if (this.isDuplicateEvent(message)) return;
    console.info(
      `[orchestrator] self-event scope=${message.chatType}:${String(message.chatId)} mid=${String(message.messageId ?? '')}`
      + ` text=${text.slice(0, 40).replace(/\n/g, ' ')}`,
    );
    message.rawData = { ...(message.rawData as Record<string, unknown>), source: 'self_other_device' };

    const scopeType = message.chatType;
    const scopeId = String(message.chatId);
    const scopeKey = `${scopeType}:${scopeId}`;
    const sourceKind = sourceKindOf(message);
    const label = sourceLabelOf(message);
    const ref = this.repo.registerMessageRef(scopeType, scopeId, message.messageId);
    const entry: HistoryEntry = {
      user_id: Number(this.bot.selfId),
      nickname: this.bot.selfNickname || 'AI',
      text,
      raw_message: message.rawMessage,
      message_id: message.messageId,
      message_ref: ref,
      timestamp: message.timestamp,
      source_kind: sourceKind,
      source_label: label,
      image_refs: [],
      file_refs: [],
    };
    if (this.repo.appendMessage(scopeType, scopeId, entry)) {
      this.summarizer.notify(scopeType, scopeId);
    }

    const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
    const triggerEntry = this.buildTriggerEntry(message, text, ref, label, sourceKind, []);
    const item: TurnItem = {
      kind: 'message',
      message,
      cleaned: text,
      agentId: agent.agentId,
      scopeKey,
      deferredCount: 1,
      triggerMessages: [triggerEntry],
      messageEpoch: this.messageEpoch,
      historySeed: null,
      silentEvent: true,
    };
    this.dispatcher.submitEvent(envelopeFromTurnItem(item), item);
  }

  private async enqueueMessage(message: ChatMessage): Promise<void> {
    try {
      if (shouldIgnoreMessage(message)) return;
      const scopeType = message.chatType;
      const scopeId = String(message.chatId);
      const scopeKey = `${scopeType}:${scopeId}`;
      const sourceKind = sourceKindOf(message);
      const label = sourceLabelOf(message);
      const cleaned = cleanText(message, this.bot.selfId, (message.rawData as Record<string, unknown>).self_id);

      const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
      const ref = this.repo.registerMessageRef(scopeType, scopeId, message.messageId);
      const imageRefs = extractImageRefs(message);
      for (const imageUrl of imageRefs) cacheImage(imageUrl); // 进站预抓：图链会过期，本地副本才是事后回看/编辑的可靠来源
      const fileRefs = extractFileRefs(message);
      const videoRefs = extractVideoRefs(message);
      const baseText = cleaned || message.text;
      let displayText = baseText;
      if (imageRefs.length > 0) displayText = `${displayText}${displayText ? ' ' : ''}[图片×${imageRefs.length}]`;
      if (videoRefs.length > 0) displayText = `${displayText}${displayText ? ' ' : ''}[视频×${videoRefs.length}]`;
      for (const fileRef of fileRefs) {
        displayText = `${displayText}${displayText ? ' ' : ''}[文件: ${fileRef.name}]`;
      }
      const entry: HistoryEntry = {
        user_id: message.userId,
        nickname: messageNickname(message),
        text: displayText,
        raw_message: message.rawMessage,
        message_id: message.messageId,
        message_ref: ref,
        timestamp: message.timestamp,
        source_kind: sourceKind,
        source_label: label,
        image_refs: imageRefs,
        file_refs: fileRefs,
        video_refs: videoRefs,
      };
      if (this.repo.appendMessage(scopeType, scopeId, entry)) {
        this.summarizer.notify(scopeType, scopeId);
      }
      // Relation network: observe every real participant, and let the
      // async intel collector count this message toward its window.
      if (message.userId > 0 && String(message.userId) !== String(this.bot.selfId) && scopeType !== MASTER_SCOPE_TYPE) {
        const agentNow = this.repo.getOrCreateAgent(scopeType, scopeId);
        this.relations.touchMember(scopeKey, agentNow.displayName, message.userId, messageNickname(message));
        this.intel.notify(scopeType, scopeId);
      }
      void this.resolveDisplayName(scopeType, scopeId);

      if (this.isMessageStale(message)) {
        console.info(`[orchestrator] stale message dropped scope=${scopeKey}`);
        return;
      }

      // 小说阅读指令机：启用 scope 里的整句指令（搜书/选/目录/看/下一章）
      // 直接应答并记历史，跳过触发门与模型——指令以外的消息照常往下走 AI。
      if (this.novelCmd !== null && this.novelCmd.handles(scopeKey) && message.userId > 0) {
        try {
          if (await this.novelCmd.tryHandle(message, cleaned)) return;
        } catch (error) {
          console.warn('[novel-cmd] handle failed:', error instanceof Error ? error.message : String(error));
        }
      }

      const triggered = shouldTrigger(message, cleaned, agent);
      this.touchGroupWindow(message, scopeKey, scopeId);
      if (!triggered) return;

      const triggerEntry = this.buildTriggerEntry(message, cleaned, ref, label, sourceKind, imageRefs);
      const item: TurnItem = {
        kind: 'message',
        message,
        cleaned,
        agentId: agent.agentId,
        scopeKey,
        deferredCount: 0,
        triggerMessages: [triggerEntry],
        messageEpoch: this.messageEpoch,
        historySeed: null,
        silentEvent: false,
      };
      console.info(
        `[orchestrator] trigger scope=${scopeKey} from=${message.userId} kind=${sourceKind} mid=${String(message.messageId ?? '')}`
        + ` text=${(cleaned || message.text).slice(0, 40).replace(/\n/g, ' ')}`,
      );
      this.dispatcher.submitEvent(envelopeFromTurnItem(item), item);
    } catch (error) {
      console.error('[orchestrator] enqueue failed:', error);
    }
  }

  // ── turn loop (actor consumer) ────────────────────────────────────────────

  private async consumeTurn(scopeKey: string, item: TurnItem): Promise<void> {
    let current: TurnItem | null = item;
    while (current !== null) {
      if (!hasActionableEvent(current)) {
        // 纯静默批次（其他设备的本人消息等）：内容已在入队时写进历史，
        // 永不单独触发生成。先并入此刻排队的后续——有真实消息就一起走，
        // 没有就到此为止（legacy _fold_silent_followup_head 语义）。
        const batch = this.coordinator.drainScopeFollowup(
          scopeKey,
          current.historySeed ?? [],
          null,
          (pending) => this.isMessageStale(pending.message),
        );
        current = mergeFollowupItems(scopeKey, current, batch?.turnItem ?? null);
        if (current === null || !hasActionableEvent(current)) {
          console.info(`[orchestrator] consumed silent-only batch scope=${scopeKey}`);
          return;
        }
        continue;
      }
      const turnStart = Date.now();
      console.info(`[orchestrator] turn start scope=${scopeKey}`);
      const turnItem: TurnItem = current;
      const followup = await withProbe('chat.turn', () => this.runMessageTurn(scopeKey, turnItem), { scope: scopeKey });
      console.info(`[orchestrator] turn done scope=${scopeKey} took=${Math.round((Date.now() - turnStart) / 1000)}s`);
      current = followup;
    }
  }

  private async runMessageTurn(scopeKey: string, item: TurnItem): Promise<TurnItem | null> {
    let message = item.message;
    if (message === null) return this.mergeFollowupAfterTurn(scopeKey, item, false);
    const runEpoch = this.resolveEpoch(item.messageEpoch);
    if (this.isEpochStale(runEpoch)) return null;

    const scopeType = message.chatType;
    const scopeId = String(message.chatId);
    const domain = domainDefForScope(`${scopeType}:${scopeId}`);
    let cleaned = item.cleaned || message.text;
    this.models.beginRequest('main');
    const executedTools: string[] = [];
    /** 回合开始时间（用于回合摘要的耗时统计）。 */
    const turnStartedAt = Date.now();
    /** 本回合触发消息文本（摘要日志用）。 */
    const triggerTexts: string[] = [];
    const sentImageKeys = new Set<string>();
    // 生图/改图同回合同参数只执行一次：模型遇错爱原样连试，既烧钱又刷屏。
    const imageToolCalls = new Map<string, ToolResult>();
    const isMasterScope = isMasterScopeOf(scopeType, scopeId);
    /** Master's last plain-text reply — relayed back to the reporting child. */
    let lastModelText = '';
    /** Latest child scope that reported into this master turn (回传 target). */
    let relayFromScope = '';
    /** Hoisted so the post-turn debounce reset can see it after the catch. */
    let sentThisTurn = false;
    // 请示回执追踪：本轮触发（含中途 fold 进来）的带 need_reply 上报，
    // 回合结束时还没给来源会话回话，就注入一次 [回执提醒] 追问（提醒自身
    // 不带 ack 标记，最多追一次，不会自激）。声明在 try 外，catch 后仍可用。
    const pendingAcks = new Map<string, string>(); // 来源会话 → 上报摘要
    const collectAcks = (entries: TriggerEntry[]): void => {
      for (const entry of entries) {
        const from = String((entry as { ack_required_from?: unknown }).ack_required_from ?? '');
        if (from === '' || pendingAcks.has(from)) continue;
        pendingAcks.set(from, String(entry.text ?? '').replace(/\s+/g, ' ').slice(0, 120));
      }
    };

    try {
      const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
      const bundle = await this.promptStore.bundle();
      // main.txt / 领域提示词用 {{master_qq}} 占位，发布版不把号主 QQ 写死。
      const persona = isMasterScope
        ? bundle.main.replaceAll('{{master_qq}}', String(this.config.masterQq))
        : domain !== null
          ? (bundle.domainPrompts[domain.id] ?? agent.persona ?? bundle.char).replaceAll('{{master_qq}}', String(this.config.masterQq))
          : agent.persona || bundle.char;
      const diarySummaries = this.repo.getDiarySummaries(scopeType, scopeId);

      const historyAll = this.repo.getMessages(scopeType, scopeId);
      let historyCursor = historyAll.length;
      let historyBeforeTrigger = item.historySeed
        ? [...item.historySeed]
        : stripTriggerEntries(historyAll, item.triggerMessages);
      let triggerMessages = dedupeEntries(item.triggerMessages);
      triggerTexts.length = 0;
      triggerTexts.push(...triggerMessages.map((entry) => String(entry.text ?? '')));
      if (isMasterScope) {
        relayFromScope = latestRelaySource(triggerMessages);
      }
      // 请示回执追踪：见 try 外声明。
      collectAcks(triggerMessages);

      let toolRound = 0;
      // In-turn conversation extension (legacy tool_context): each assistant
      // reply and its tool results are appended here and replayed on every
      // later round of THIS turn — without it the model never sees what its
      // tools returned (view_image descriptions etc.) and loops on the same
      // call until the iteration cap, ending the turn in silence.
      const extension: ModelMessage[] = [];
      // extension 里"原图直出"消息的下标：下一轮请求前替换为过期标记。
      const liveImageOnce: number[] = [];
      let fallbackPrompts = 0;
      // 发送拦截（仿真人「等等我看下你新发的」）：模型决定发送的那一刻，
      // 若 mailbox 里躺着它还没看过的真人新消息，第一次发送不执行、改返回
      // 「对方刚说了……」的提示；模型看完仍想发就重调一次（水位标记放行），
      // 想改口就先回应新消息。水位=最后一条未读消息 id，新消息再进来水位变化重新拦。
      let sendInterlockWatermark = '';
      // Per-turn autonomous-action state: the workspace sandbox and the shared
      // network-call budget must live for the WHOLE turn, not one tool round.
      const turnWorkspace = new Workspace(join(this.workspaceDir, `${scopeType}_${scopeId}`));
      const networkBudget = { used: 0 };
      sentThisTurn = false;
      for (;;) {
        // Master turns are usually triggered by internal child reports; the
        // persona tail (main.txt + tool protocol) must still be injected.
        // A debounce nudge is synthetic (user_id=0) but it continues a real
        // group conversation, so it must keep the persona tail just like a
        // user-triggered round.
        const injectPersona =
          isMasterScope ||
          (toolRound === 0
            && !triggerMessages.some((entry) => isInternalReportEntry(entry) && !isDebounceTrigger(entry)));
        // 主级结论回到 QQ 子AI时，它本身就是一次该主动转告用户的异步结果。
        // 不能只靠普通群聊的「少说话」倾向，否则模型收到答案仍 stay_silent，
        // 直到用户再追问才把早已收到的结论说出去。
        const hasMasterReply = (scopeType === 'group' || scopeType === 'private')
          && triggerMessages.some((entry) => /^\[(?:主级回话|主AI回传)(?=\s|$)/.test(String(entry.text ?? '')));
        const { system, messages } = buildChildMessages({
          bundle,
          message,
          persona,
          impression: this.relations.scopeImpression(scopeKey),
          historyBeforeTrigger,
          triggerMessages,
          background: {
            impression: this.relations.scopeImpression(scopeKey),
            globalIdentityContext: await this.identityContextWithRefs(scopeKey),
            groupContext: scopeType === 'group' ? await this.groupContext(scopeId) : '',
            triggerInfo: scopeType === 'group' ? buildTriggerInfo(agent) : '',
            senderCard: message.userId > 0 ? this.relations.personCard(message.userId) : '',
            scopeRelationCard: this.relations.scopeCard(scopeKey),
            relationOverview: isMasterScope ? this.relations.overviewCard() : '',
            imageHint: buildImageHint(triggerMessages),
            deferredCount: item.deferredCount,
            displayName: agent.displayName,
            overallSummary: agent.overallSummary,
            diarySummaries,
            knowledgeLines: [],
            mountedKnowledgeLines: [],
            recentThinkNotes: this.repo.recentNotes(scopeType, scopeId),
            isMasterMessage: this.isMasterMessage(message),
            isAdminMessage: this.isAdminMessage(message),
            botSelfId: String(this.bot.selfId),
            legacySelfIds: this.bot.legacySelfIds.map(String),
            masterQq: String(this.config.masterQq),
            nowText: nowText(),
          },
          injectPersona,
          chatMode: true,
          masterMode: isMasterScope,
          // 真实用户消息触发的回合：在触发消息尾部钉一条工具协议提醒。
          // 小说/闲聊-heavy 的会话里散文"重力"会淹没 system 里的协议，
          // deepseek-flash 这类指令遵循偏弱的模型就只用文字描述动作
          //（"我取90到94章"）而不调工具（2026-10-02 雪球会话实锤）。
          modeHint:
            hasMasterReply
              ? '（系统提示，仅你可见、用户看不到：本轮包含主AI/领域中枢对先前请示的正式回话。'
                + '其中给当前用户或当前会话的结论，必须立即调用 send_message 用自己的口吻主动转达，不能 stay_silent 等用户追问；'
                + '不要重复上报同一份结论。只有明确标注为纯内部协调、无需让用户知道的内容才可以沉默。'
                + '同一结论常经多条链路先后回到你这里——转达前先对照你最近几条发言，此前已把实质相同的结论告诉过用户的（换措辞复述也算），本轮 stay_silent，不要再讲一遍。）'
              : message.userId !== 0 || sourceKindOf(message) === 'admin_webui' || isDebounceMessage(message)
                ? '（系统提示，仅你可见、用户看不到：回复用户必须调用 send_message 工具，直接输出的普通文字用户完全收不到；'
                  + '要查资料/取章节/发文件就直接调用对应工具，不要用文字描述你要做的动作。'
                  + '工具要以工具调用的形式发起，把工具名当普通文字写出来不算调用。）'
                : '',
        });

        const model = this.models.getModelForRole('main');
        if (model === null) throw new Error('没有可用的聊天模型（roles.main 未配置或渠道为空）');

        let reply;
        try {
          // 首 token 计时：流式下能看出模型"多久才开始说话"。
          // legacy 运行时把这个数打在日志里，用来判断上游是否半死。
          const startedAt = Date.now();
          let firstTokenMs: number | null = null;
          reply = await completeChatTracked('main', model, {
            system,
            messages: [...messages, ...extension],
            tools: chatToolSchemas(scopeKey, isMasterScope),
            temperature: 0.85,
            maxTokens: 2048,
            thinkingLevel: 'off',
            onDelta: () => {
              if (firstTokenMs === null) firstTokenMs = Date.now() - startedAt;
            },
          });
          console.info(
            `[orchestrator] llm scope=${scopeKey} first_token=${firstTokenMs ?? '-'}ms total=${Date.now() - startedAt}ms`,
          );
        } catch (error) {
          this.models.notifyFailure('main');
          throw error;
        }
        // 每轮决策落一行：调了什么工具（带参数摘要）/ 纯文本前缀。「有回合无输出」
        // 类故障（模型散文回复、莫名 stay_silent、同一张图反复重发）没有它只能瞎猜。
        this.dumpTurnRequest(scopeKey, String(message.messageId ?? ''), toolRound, system, [...messages, ...extension], reply.toolCalls.length);
        // 原图直出只在拿到它的下一轮请求里出现一次：本轮请求已经带过它了，
        // 从下一轮起替换为过期标记——既不滚进上下文，也不再吃图片 token。
        for (const idx of liveImageOnce) {
          const stale = extension[idx];
          if (stale !== undefined && typeof stale.content !== 'string') {
            stale.content = '【原图已过期，如需查看，请再次调用 view_image_raw 工具获取原图】';
          }
        }
        liveImageOnce.length = 0;
        const argsPreview = reply.toolCalls
          .map((c) => `${c.name}(${JSON.stringify(c.arguments ?? {}).slice(0, 120)})`)
          .join(' + ');
        console.info(`[orchestrator] round scope=${scopeKey} tools=${argsPreview || '-'} text=${reply.text.trim().slice(0, 80)}`);
        // Only no-tool-call rounds count as a "conclusion": opus emits its
        // reasoning as plain text ALONGSIDE tool calls, and relaying that
        // leaked English chain-of-thought into child scopes (2026-09-29).
        // 赋值挪进下面的结论分支：要走重发救援的散文不能当结论——它被丢弃、
        // 用户没看到，若记成 lastModelText 还会被当 [主AI回传] 转播给下级。
        if (reply.toolCalls.length === 0) {
          // Plain text without a tool call: the user saw nothing of it. Legacy
          // re-prompted once ("重新决策") before giving up, so a model that
          // answered in prose (e.g. homework it just viewed) gets one chance
          // to resend via send_message or to explicitly stay_silent. Internal
          // turns (relays, alarms) keep plain text as their conclusion.
          // 面板控制台消息（userId=0 的 admin_webui）也是主人在敲字——同样
          // 给重发机会，不然她回纯文本时主人永远看不到（2026-10-02 面板
          // 有回合无回复之鉴：turn done 17s 却零输出）。
          // 重发救援放宽到 2 次：deepseek-flash 在小说-heavy 会话里会连续
          // 散文回复（甚至说「我重发一下」也还是散文），1 次救不回来
          //（2026-10-02 雪球连续 7 个回合零输出之鉴）。
          const rescuable = message.userId !== 0 || sourceKindOf(message) === 'admin_webui' || isDebounceMessage(message);
          if (!sentThisTurn && fallbackPrompts < 2 && reply.text.trim() && rescuable) {
            fallbackPrompts += 1;
            extension.push({ role: 'assistant', content: reply.text });
            extension.push({
              role: 'user',
              content:
                '你刚才输出的那段普通文字并没有被发送——用户完全没看到它（只有调用 send_message 工具发送的内容用户才能看到）。'
                + '本条系统消息同样只对你可见，用户也看不到。如果你确实想把刚才那些话说给用户，请重新调用 send_message 工具发送；'
                + '如果决定不回复，请调用 stay_silent 工具结束本回合。',
            });
            toolRound += 1;
            continue;
          }
          // No tool call and nothing (more) to rescue: "nothing to say".
          // 重发机会已经用过还回纯文本 = 真异常，落一行日志方便排查。
          if (reply.text.trim() && fallbackPrompts > 0) {
            console.info(`[orchestrator] plain text after re-prompt, dropped scope=${scopeKey} text=${reply.text.trim().slice(0, 120)}`);
          }
          if (reply.text.trim()) lastModelText = reply.text.trim();
          break;
        }

        const toolContext: ToolContext = {
          bot: this.bot,
          repo: this.repo,
          archive: this.archive,
          relations: this.relations,
          kb: this.kb,
          tasks: this.tasks,
          masterQq: this.config.masterQq,
          isMaster: isMasterScope,
          notifyMaster: (payload) => this.relayToMaster(payload),
          workspace: turnWorkspace,
          networkBudget,
          delegateToChild: (targetType, targetId, instruction) => this.delegateToChild(targetType, targetId, instruction),
          sendToScope: (targetType, targetId, content) => this.sendToScope(targetType, targetId, content),
          peerInject: (targetScopeKey, text, extraRaw) => this.injectInternalMessage(targetScopeKey, text, extraRaw),
          // 主AI 没有 QQ 窗口：数据线来的回合 send_message 改走数据线回主人手机；
          // 其余回合只落主AI会话（面板对话可见），要找人请 message_scope/委派下级。
          // 无 QQ 窗口的虚拟 scope（domain 中枢 / bili 视频下级 / mcclient 下级）：
          // send_message 改道汇报给对应中枢（domain→主AI，bili→哔哩中枢，
          // mcclient→MC 中枢），由中枢决定下一步（回主人/再委派/存知识库）。
          sendTextOverride: isMasterScope
            ? this.masterChannel === 'dataline'
              ? (line) => this.bot.sendDatalineText(line)
              : () => Promise.resolve({})
            : domain !== null || scopeType === 'bili' || scopeType === 'mcclient'
              ? (line) => {
                  this.relayToMaster({ text: line, fromScope: `${scopeType}:${scopeId}` });
                  return Promise.resolve({});
                }
              : undefined,
          triggerImageRefs: triggerMessages.flatMap((entry) => entry.image_refs ?? []),
          triggerVideoRefs: triggerMessages.flatMap((entry) => (entry as { video_refs?: string[] }).video_refs ?? []),
          describeImage: (url, question) => this.describeImage(url, question),
          getGroupRoles: scopeType === 'group' ? () => this.groupRoles.get(scopeId) : undefined,
          scopeType,
          scopeId,
          executedTools,
          sentImageKeys,
        };

        let sawStaySilent = false;
        // Replay the assistant turn verbatim (tool_use ids must match the
        // tool_result blocks below; fabricate ids when the provider omitted them).
        const calls = reply.toolCalls.map((call, index) => ({
          ...call,
          id: call.id || `toolu_${Date.now()}_${index}_${Math.random().toString(36).slice(2, 8)}`,
        }));
        extension.push({ role: 'assistant', content: reply.text, toolCalls: calls });
        // 同一批 tool calls 里拦了一个发送就全拦：提示还没被模型读到，不能让后半批漏发。
        let sendBlockedThisBatch = false;
        for (const call of calls) {
          // ── 发送拦截：模型没看过的真人新消息躺在 mailbox 里时，第一次发送
          // 不执行，换成提示让模型先「看到」新消息再决定（下轮 fold 会把它们
          // 带进上下文）。重调（水位相同）或 fold 后 mailbox 已空则正常发送。
          if (SEND_INTERLOCK_TOOLS.has(call.name)) {
            const unseen = this.unseenUserMessages(scopeKey);
            const watermark = unseen.length > 0 ? String(unseen[unseen.length - 1].message?.messageId ?? '') : '';
            if (unseen.length > 0 && (sendBlockedThisBatch || (watermark !== '' && watermark !== sendInterlockWatermark))) {
              sendBlockedThisBatch = true;
              sendInterlockWatermark = watermark;
              const preview = unseen
                .map((t) => {
                  const m = t.message!;
                  const who = String(m.sender?.nickname || m.sender?.card || m.userId);
                  return `${who}: ${(t.cleaned || m.text).slice(0, 120)}`;
                })
                .join('\n');
              executedTools.push(`${call.name}(拦截)`);
              extension.push({
                role: 'tool_result',
                toolCallId: call.id,
                content: `【发送前暂停——本条没有发出去】就在你思考/调用工具期间，对方发来了 ${unseen.length} 条新消息：\n${preview}\n`
                  + `请先看完新消息再决定：刚才要发的内容如果仍然合适，请重新调用一次 ${call.name}（同样内容即可，这次会真正发出）；`
                  + '如果已不再合适，就回应新消息、重新组织回复。',
              });
              continue;
            }
          }
          const imageToolKey = call.name === 'image_edit' || call.name === 'image_generate'
            ? `${call.name}:${JSON.stringify(call.arguments ?? {})}`
            : '';
          const previousImageResult = imageToolKey !== '' ? imageToolCalls.get(imageToolKey) : undefined;
          const result: ToolResult = previousImageResult !== undefined
            ? {
                ...previousImageResult,
                output: `（本回合已经用完全相同的参数调用过 ${call.name}，这次没有重复请求上游。以下是上次结果；失败请先修正参数或停止，成功不要重复生成。）\n${previousImageResult.output}`,
                inlineImageUrls: undefined,
              }
            : await executeChatTool(call, toolContext);
          if (imageToolKey !== '' && previousImageResult === undefined) imageToolCalls.set(imageToolKey, result);
          // 工具调用日志
          this.turnLog.appendToolLog(scopeType, scopeId, {
            at: Date.now(),
            name: call.name,
            argsPreview: JSON.stringify(call.arguments ?? {}).slice(0, 200),
            resultPreview: result.output.slice(0, 300),
            ok: result.ok,
            durationMs: Date.now() - turnStartedAt,
          });
          // 请示回执：主级回话/委派/直达工具命中来源会话即算已回执。
          if (result.ok && pendingAcks.size > 0) {
            const ackTarget = this.ackTargetOfCall(call);
            if (ackTarget !== '') pendingAcks.delete(ackTarget);
          }
          if (result.staySilent) sawStaySilent = true;
          if (result.ok && (result.sentToUser || call.name === 'send_message')) sentThisTurn = true;
          extension.push({ role: 'tool_result', toolCallId: call.id, content: result.output });
          // view_image_raw：原图内联成图像块，紧随 tool_result 的一条 user 消息，
          // 仅下一轮请求可见（再往后被替换成过期标记，不进 repo 历史）。
          if (result.ok && result.inlineImageUrls && result.inlineImageUrls.length > 0) {
            const blocks: ModelMessage['content'] = [
              { type: 'text', text: '【系统注入：上一次工具取到的原图，仅本轮可见；之后的轮次会显示为已过期】' },
            ];
            for (const imageUrl of result.inlineImageUrls) {
              try {
                blocks.push({ type: 'image', url: await embedImageAsDataUrl(imageUrl) });
              } catch (error) {
                console.warn(`[orchestrator] inline image failed scope=${scopeKey} url=${imageUrl.slice(0, 100)}: ${error instanceof Error ? error.message : String(error)}`);
              }
            }
            if (blocks.length > 1) {
              extension.push({ role: 'user', content: blocks });
              liveImageOnce.push(extension.length - 1);
            }
          }
        }
        if (sawStaySilent) break;

        toolRound += 1;
        if (toolRound >= MAX_TOOL_ITERATIONS) break;

        // ── mid-turn pickup (插队): fold what arrived while we were working.
        // The loop continues either way — after a tool round the model must
        // see the tool results and finish deciding (legacy continued on
        // tool_result blocks; breaking here stranded query tools like
        // view_image with their result discarded).
        const pending = this.drainLiveTool(scopeKey);
        if (pending !== null) {
          const folded = mergeFollowupItems(scopeKey, pending);
          // Continuation for the next round: this round's triggers become history,
          // plus anything persisted during the turn the model must know it did
          // (its own send_message outputs) — while the newly folded messages
          // become the next round's triggers, deduped out of history.
          if (folded !== null) {
            const foldedIds = new Set(
              folded.triggerMessages.map((entry) => String(entry.message_id ?? '')).filter((id) => id !== ''),
            );
            const fresh = this.repo.getMessages(scopeType, scopeId).slice(historyCursor);
            historyCursor += fresh.length;
            historyBeforeTrigger.push(...triggerMessages.map((entry) => triggerToHistory(entry)));
            for (const entry of fresh) {
              const isSelfOutput = String(entry.user_id) === String(this.bot.selfId);
              if (isSelfOutput || isInternalReportEntry(entry)) historyBeforeTrigger.push(entry);
            }
            historyBeforeTrigger = historyBeforeTrigger.filter(
              (entry) => !foldedIds.has(String(entry.message_id ?? '')),
            );
            triggerMessages = dedupeEntries(folded.triggerMessages);
            collectAcks(triggerMessages);
            if (isMasterScope) {
              relayFromScope = latestRelaySource(triggerMessages) || relayFromScope;
            }
            item.deferredCount += Math.max(1, folded.deferredCount);
            if (folded.message !== null) {
              item.message = folded.message;
              item.cleaned = folded.cleaned;
              // Refresh the turn-local view too: sender/modeHint/rescue for
              // the next round must describe the NEWEST message, not the one
              // that started this turn.
              message = folded.message;
              cleaned = folded.cleaned;
            }
          }
        }
      }
    } catch (error) {
      // A scheduled turn is durable work. Retry only transient model failures
      // before any tool ran: replaying after a side effect could send twice,
      // create duplicate tasks, or repeat an account/group mutation.
      // 用户消息回合同样享受有界重试（纯瞬时协议错误、零副作用时）——
      // 上游抽一次风就吃一条用户消息，是「丢消息」投诉的主要来源。
      if (this.scheduleTurnRetry(scopeKey, item, error, executedTools, runEpoch)) return null;
      // 回合失败必须落 stdout：admin 通知发的是 bot 自己（admin_qq=bot 号）会被自聊拦截，
      // 中断便签又只记 error.name——不打这条日志，协议层报错无从排查。
      console.error('[orchestrator] turn failed:', error);
      this.persistInterruptNote(scopeType, scopeId, error, executedTools);
      this.notifyAdminIfNeeded(scopeType, scopeId, error, executedTools);
    }

    // She just spoke in this group: drop any armed debounce window so the
    // next 5s lull doesn't immediately invite her to "continue" — that
    // re-prompt made her re-answer her own last reply verbatim (2026-09-29).
    if (sentThisTurn && scopeType === 'group') this.groupWindows.cancel(scopeKey);

    // 回合摘要日志
    this.turnLog.appendTurnLog(scopeType, scopeId, {
      at: Date.now(),
      scopeKey,
      triggerPreview: (triggerTexts[0] ?? '').slice(0, 240),
      toolsUsed: executedTools.slice(),
      sent: sentThisTurn,
      durationMs: Date.now() - turnStartedAt,
    });

    // 主AI回传闭环: master's plain-text conclusion goes back to the child that
    // reported, as an internal intel note (legacy "直接输出普通文本回传"语义).
    // Only for internally-triggered turns — a reply to the master himself must
    // never leak into a child scope.
    if (isMasterScope && message.userId === 0 && lastModelText && relayFromScope && !isSilenceConclusion(lastModelText)) {
      this.relayBackToChild(relayFromScope, lastModelText);
      pendingAcks.delete(relayFromScope);
    }

    // 请示回执兜底：带 need_reply 的上报在本回合没有得到任何回话（没调
    // domain_message/message_scope/delegate_to_child 命中来源会话，也没走
    // 主AI回传闭环），追问一次。提醒自身不带 ack 标记，不会连环自激。
    if (pendingAcks.size > 0 && this.running && !this.isEpochStale(runEpoch)) {
      const lines = [...pendingAcks.entries()]
        .map(([from, snippet]) => `- ${from}：${snippet}`)
        .join('\n');
      console.warn(`[orchestrator] ack reminder scope=${scopeKey} pending=${[...pendingAcks.keys()].join(',')}`);
      this.injectInternalMessage(
        scopeKey,
        `[回执提醒] 本轮收到的请示类上报（带 need_reply 标记）到现在还没有任何回执：\n${lines}\n`
          + '请示方还在等结论。请逐条给出明确答复：用 domain_message(target="<来源会话>"，如 "private:123456") '
          + '把结论（批准/驳回/暂缓及原因）回给来源会话；主AI也可以直接 delegate_to_child 委派来源会话的子AI执行（委派即算回执）。'
          + '有意不回复的极少数情况，用 memory_write 记一句「不回执+理由」，别让来源会话干等。',
        { ack_reminder: true },
      );
    }

    const completed = !this.isEpochStale(runEpoch);
    return this.mergeFollowupAfterTurn(scopeKey, item, completed);
  }

  private scheduleTurnRetry(
    scopeKey: string,
    item: TurnItem,
    error: unknown,
    executedTools: string[],
    runEpoch: number,
  ): boolean {
    if (executedTools.length > 0 || !(error instanceof ProtocolError) || !error.transient) return false;
    // 定时回合必重试；普通用户消息回合也救，但仅限真消息（别把内部心跳之类的反复重放）。
    if (!hasScheduledTask(item) && !(item.kind === 'message' && item.message !== null)) return false;
    const previousAttempt = Math.max(0, Math.trunc(Number(item.scheduledRetryAttempt ?? 0)));
    if (previousAttempt >= MAX_SCHEDULED_TURN_RETRIES) return false;
    const attempt = previousAttempt + 1;
    const delayMs = SCHEDULED_RETRY_BASE_MS * 2 ** (attempt - 1);
    const retryItem: TurnItem = { ...item, scheduledRetryAttempt: attempt };
    console.warn(`[orchestrator] turn retry scope=${scopeKey} kind=${item.kind} attempt=${attempt} in ${delayMs}ms`);
    const timer = setTimeout(() => {
      this.scheduledRetryTimers.delete(timer);
      if (!this.running || this.isEpochStale(runEpoch)) return;
      this.dispatcher.submitEvent(envelopeFromTurnItem(retryItem), retryItem);
    }, delayMs);
    timer.unref?.();
    this.scheduledRetryTimers.add(timer);
    return true;
  }

  // ── post-turn merge ───────────────────────────────────────────────────────

  private mergeFollowupAfterTurn(scopeKey: string, item: TurnItem, completed: boolean): TurnItem | null {
    const historySeed = item.historySeed ?? [];
    const batch = this.coordinator.drainScopeFollowup(
      scopeKey,
      historySeed,
      completed ? { source: 'turn-complete' } : null,
      (pending) => this.isMessageStale(pending.message),
    );
    if (batch === null) return null;
    const followup = batch.turnItem;
    if (!hasActionableEvent(followup)) {
      console.info(`[orchestrator] skip silent-only followup scope=${scopeKey}`);
      return null;
    }
    console.info(
      `[orchestrator] followup turn scope=${scopeKey} items=${followup.batchItems?.length ?? 1}`
      + ` silent0=${String(followup.batchItems?.[0]?.silentEvent ?? followup.silentEvent)}`
      + ` text=${(followup.cleaned || followup.message?.text || '').slice(0, 40).replace(/\n/g, ' ')}`,
    );
    return followup;
  }

  private drainLiveTool(scopeKey: string): TurnItem | null {
    const batch = this.coordinator.drainScopeFollowup(
      scopeKey,
      [],
      null,
      (pending) => this.isMessageStale(pending.message),
    );
    if (batch === null) return null;
    // 静默条目（其他设备的本人消息）也要返回：折入进行中的回合触发上下文，
    // 让模型立刻看到——它不会制造新回合（回合本来就因工具轮继续），
    // 制造新回合的闸门在 consumeTurn / mergeFollowupAfterTurn 两处。
    return { ...batch.turnItem, scopeKey };
  }

  /**
   * mailbox 里排队中、本轮模型还没看到的真人消息（只读不弹出）。
   * 排除：内部事件（user_id=0）、bot 自己（含其他设备同步过来的自己的话）。
   */
  private unseenUserMessages(scopeKey: string): TurnItem[] {
    const selfId = String(this.bot.selfId);
    return this.mailbox
      .peekScopeTransients<TurnItem>(scopeKey)
      .filter((t): t is TurnItem => t !== null)
      .filter((t) =>
        t.kind === 'message'
        && t.message !== null
        && t.message.userId !== 0
        && String(t.message.userId) !== selfId
        && !t.silentEvent
        && !this.isCurrentSelf(t.message.userId));
  }

  // ── epoch / stale ─────────────────────────────────────────────────────────

  private bumpEpoch(): void {
    this.messageEpoch += 1;
    this.dispatcher.clearRuntimeState();
    this.groupWindows.cancelAll();
  }

  private isEpochStale(epoch: number): boolean {
    return Math.trunc(epoch) !== this.messageEpoch;
  }

  private resolveEpoch(epoch: number | null): number {
    return epoch === null || epoch === 0 ? this.messageEpoch : Math.trunc(epoch);
  }

  private isMessageStale(message: ChatMessage | null): boolean {
    if (message === null) return false;
    // An alarm/delegation is durable work, not an old QQ message. A busy scope
    // must not discard it after five minutes; the scheduler already decides
    // when it should fire, and retries may legitimately arrive later still.
    if (sourceKindOf(message) === 'internal_task') return false;
    return Date.now() / 1000 - message.timestamp > this.config.staleMessageMaxAgeSeconds;
  }

  private scopeBusy(scopeKey: string): boolean {
    const session = this.sessions.get(scopeKey);
    return session ? session.isBusy() : this.mailbox.pendingCount(scopeKey) > 0;
  }

  // ── dedup ─────────────────────────────────────────────────────────────────

  /** 现役账号判定：只认当前登录号，legacy 旧号不丢（号主可能拿回来自己用）。 */
  private isCurrentSelf(id: unknown): boolean {
    if (typeof this.bot.isCurrentSelfId === 'function') return this.bot.isCurrentSelfId(id);
    return this.bot.isSelfId(id);
  }

  private isDuplicateEvent(message: ChatMessage): boolean {
    const messageId = message.messageId;
    const key =
      messageId !== null && messageId !== undefined && messageId !== ''
        ? `${message.chatType}:${message.chatId}:${messageId}`
        : `${message.chatType}:${message.chatId}:${message.userId}:${message.rawMessage}`;
    const now = Date.now() / 1000;
    this.purgeRecentKeys();
    if (this.recentMessageKeys.has(key)) return true;
    this.recentMessageKeys.set(key, now);
    return false;
  }

  private purgeRecentKeys(): void {
    const cutoff = Date.now() / 1000 - 180;
    for (const [key, seenAt] of this.recentMessageKeys) {
      if (seenAt < cutoff) this.recentMessageKeys.delete(key);
    }
  }

  // ── group debounce ────────────────────────────────────────────────────────

  private touchGroupWindow(message: ChatMessage, scopeKey: string, scopeId: string): void {
    if (message.chatType !== 'group' || message.userId === 0) return;
    this.groupWindows.touch(scopeKey, scopeId, this.messageEpoch);
  }

  private fireGroupDebounce(scopeKey: string, scopeId: string, epoch: number): void {
    const synthetic = buildDebounceTriggerMessage(scopeId, epoch, this.bot.selfId);
    this.handleMessage(synthetic);
  }

  // ── master relay / scheduled tasks ─────────────────────────────────────────

  /**
   * 下级上报路由（三层架构汇报链）：QQ 会话（group/private）报给 Onebot 中枢，
   * mcclient 报给 Minecraft 中枢，bili 报给哔哩中枢；领域中枢与未知 scope
   * 直报最高级中枢（主AI）。hubForScope 返回 null = 直报主AI。
   */
  private relayToMaster(payload: { text: string; requestType?: string; fromScope: string; needReply?: boolean }): void {
    const masterScope = MASTER_SCOPE_KEY;
    // 请示类上报（显式 need_reply 或 system_operation）必须拿到回执：打上
    // ack_required 标记，接收方回合结束时若没给来源会话回话，会被系统追问。
    const needReply = payload.needReply === true || payload.requestType === 'system_operation';
    const text = `[子AI上报 from ${payload.fromScope}${payload.requestType ? ` type=${payload.requestType}` : ''}${needReply ? ' need_reply=1' : ''}]\n${payload.text}`;
    const hub = hubForScope(payload.fromScope, masterScope);
    if (hub !== null) {
      this.injectInternalMessage(hub, text, {
        relayed_from: payload.fromScope,
        ...(needReply ? { ack_required: payload.fromScope } : {}),
      });
      return;
    }
    const relay: ChatMessage = {
      chatType: MASTER_SCOPE_TYPE,
      chatId: MASTER_SCOPE_ID,
      userId: 0,
      text,
      rawMessage: text,
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: `sys_relay_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: {
        source: 'agent_message',
        relayed_from: payload.fromScope,
        ...(needReply ? { ack_required: payload.fromScope } : {}),
      },
    };
    this.handleMessage(relay);
  }

  /** Scheduler fire callback: a task firing becomes an internal scope turn. */
  private fireTask(task: ScheduledTask): boolean | Promise<boolean> {
    // Recovery can run before NapCat has connected. Keep the task pending and
    // let the scheduler retry instead of producing a turn whose send/tools are
    // guaranteed to fail while the bridge is offline. Test doubles may omit
    // `connected`; only an explicit false means definitely unavailable.
    if (!this.running || this.bot.connected === false) return false;
    if (task.kind === 'send_image') return this.fireScheduledImage(task);
    if (task.kind === 'watchdog') {
      this.injectInternalMessage(
        task.originScope,
        `[回执超时保底触发] ${task.note}\n`
          + `（这是你自己设置的回执超时保底（${task.taskId}${task.tag ? `，tag=${task.tag}` : ''}）。`
          + '先检查这件事现在有没有结果：已经完成且已回话就 stay_silent 收场；'
          + '还没结果就继续跟进——该催的催、该重发的重发，或用工具直接查证；'
          + '确实卡死了就如实向用户交代卡在哪、下一步打算。'
          + '不要假装它已经完成。需要再等就重新 set_watchdog 续期。）',
        { watchdog_task: task.taskId },
      );
      return true;
    }
    if (task.kind === 'recurring') {
      this.injectInternalMessage(
        task.originScope,
        `[周期任务触发] ${task.note}\n（这是你自己创建的周期任务（${task.taskId}），按指令内容自主判断这轮要做什么；不需要行动就 stay_silent。要停止它用 cancel_task。）`,
        { recurring_task: task.taskId },
      );
    } else {
      this.injectInternalMessage(task.originScope, `[闹钟触发] ${task.note}`, { alarm_task: task.taskId });
    }
    return true;
  }

  /** 定时原图发送是确定性投递：不再唤醒模型猜短ID/旧链接，发送成功才完成任务。 */
  private async fireScheduledImage(task: ScheduledTask): Promise<boolean> {
    if (!task.imageUrl) throw new Error(`定时发图任务 ${task.taskId} 缺少 imageUrl`);
    const separator = task.originScope.indexOf(':');
    const scopeType = task.originScope.slice(0, separator);
    const scopeId = task.originScope.slice(separator + 1);
    if ((scopeType !== 'group' && scopeType !== 'private') || !/^\d+$/.test(scopeId)) {
      throw new Error(`定时发图目标不是 QQ 会话: ${task.originScope}`);
    }
    let cached = await cachedImageUri(task.imageUrl);
    if (cached === null && await ensureImageCached(task.imageUrl)) cached = await cachedImageUri(task.imageUrl);
    if (cached === null) {
      this.tasks.cancel(task.taskId);
      const warning = '定时图片发送失败：本地原图已失效，且原链接无法重新下载。请重新发图后再安排。';
      console.warn(`[scheduler] ${task.taskId} cancelled: cached original is unavailable`);
      const notice = await this.bot.sendText(scopeType, Number(scopeId), warning);
      const noticeId = (notice as { message_id?: number | string }).message_id;
      const noticeRef = this.repo.registerMessageRef(scopeType, scopeId, noticeId ?? null);
      this.repo.appendMessage(scopeType, scopeId, {
        user_id: this.bot.selfId,
        nickname: this.bot.selfNickname || 'AI',
        text: warning,
        message_id: noticeId ?? null,
        message_ref: noticeRef,
        timestamp: Date.now() / 1000,
        source_label: '',
      });
      return true;
    }
    const file = cached.startsWith('data:')
      ? `base64://${cached.slice(cached.indexOf(',') + 1)}`
      : cached;
    const result = await this.bot.sendImage(scopeType, Number(scopeId), file);
    const sentId = (result as { message_id?: number | string }).message_id;
    const ref = this.repo.registerMessageRef(scopeType, scopeId, sentId ?? null);
    this.repo.appendMessage(scopeType, scopeId, {
      user_id: this.bot.selfId,
      nickname: this.bot.selfNickname || 'AI',
      text: '[图片]',
      raw_message: `[CQ:image,file=scheduled:${task.taskId}]`,
      message_id: sentId ?? null,
      message_ref: ref,
      timestamp: Date.now() / 1000,
      source_label: '',
      image_refs: [task.imageUrl],
    });
    return true;
  }

  /**
   * Vision describe for view_image (role 'vision', falls back to main).
   * Single-shot used to mean one flaky upstream = user-facing "看不清";
   * now walks the channel's fallback chain (notifyFailure advances it) and
   * logs every attempt so future failures are diagnosable from the log.
   */
  private async describeImage(url: string, question: string): Promise<string> {
    // Providers generally refuse to fetch remote URLs themselves, so embed
    // the image as a data: URL (legacy _download_remote_image semantics:
    // download locally with browser-ish headers, then base64).
    const embedded = await embedImageAsDataUrl(url);
    this.models.beginRequest('vision');
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const model = this.models.getModelForRole('vision');
      if (model === null) throw new Error('没有可用的视觉模型（roles.vision / roles.main 均未配置）');
      try {
        const reply = await completeChatTracked('vision', model, {
          system: [],
          messages: [
            {
              role: 'user',
              content: [
                { type: 'image', url: embedded },
                { type: 'text', text: question },
              ],
            },
          ],
          tools: [],
          temperature: 0.4,
          maxTokens: 800,
          thinkingLevel: 'off',
        });
        const text = reply.text.trim();
        if (!text) throw new Error('视觉模型返回了空内容');
        console.info(`[vision] ok model=${model.displayName} attempt=${attempt} chars=${text.length}`);
        return text;
      } catch (error) {
        lastError = error;
        console.warn(`[vision] attempt ${attempt} failed model=${model.displayName}: ${error instanceof Error ? error.message : String(error)}`);
        this.models.notifyFailure('vision');
      }
    }
    throw lastError instanceof Error ? lastError : new Error(String(lastError));
  }

  /** 表情库增量刷新（启动自动同步 / 主AI sticker_refresh 共用）：新表情视觉打标。 */
  async refreshStickerLibrary(): Promise<StickerRefreshResult> {
    return refreshStickerIndex(this.bot, (url, question) => this.describeImage(url, question));
  }

  // ── master coordination (委派 / 回传 / 直接发言) ──────────────────────────

  /**
   * Inject a synthetic internal message into any scope's actor pipeline — the
   * shared transport for alarms, master delegation and master relay-backs.
   */
  private injectInternalMessage(scope: string, text: string, extraRaw: Record<string, unknown> = {}): void {
    const separator = scope.indexOf(':');
    const scopeType = scope.slice(0, separator);
    const scopeId = scope.slice(separator + 1);
    // QQ scope 的 id 是数字；domain 等虚拟 scope 保留字符串 id（Number('mc')=NaN 会丢）。
    const numericId = Number(scopeId);
    const chatId = Number.isFinite(numericId) && String(numericId) === scopeId ? numericId : scopeId;
    const message: ChatMessage = {
      chatType: scopeType as ChatMessage['chatType'],
      chatId,
      userId: 0,
      text,
      rawMessage: text,
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: `sys_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', ...extraRaw },
    };
    this.handleMessage(message);
  }

  /** 管理面板主AI对话入口：以「系统管理员（后台控制台）」身份把消息注入主AI会话。 */
  handleAdminWebuiMessage(text: string): void {
    const content = String(text ?? '').trim();
    if (content === '') return;
    const message: ChatMessage = {
      chatType: MASTER_SCOPE_TYPE,
      chatId: MASTER_SCOPE_ID,
      userId: 0,
      text: content,
      rawMessage: content,
      sender: { nickname: '系统管理员', user_id: 0 },
      messageId: `webui_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'admin_webui' },
    };
    this.handleMessage(message);
  }

  /** Master → child: hand an instruction to another scope's child AI. */
  private delegateToChild(targetScopeType: string, targetScopeId: string, instruction: string): string {
    const taskId = `delegate_${Math.random().toString(36).slice(2, 10)}`;
    const text = [
      `[主AI委派] ${instruction}`,
      '（这是主AI直接交给你的任务：可以自然地与用户交流来完成它；完成后用 notify_master 回报结果，request_type=delegate_result。）',
    ].join('\n');
    this.injectInternalMessage(`${targetScopeType}:${targetScopeId}`, text, { delegate_task: taskId });
    return taskId;
  }

  /**
   * 请示回执：把一次工具调用归一化成它「回话到的目标会话」。
   * domain_message 的 target 允许领域裸 id / master / 全限定会话名，统一折算成
   * scopeKey 形态与 pendingAcks 比对。
   */
  private ackTargetOfCall(call: ToolCall): string {
    const args = call.arguments ?? {};
    if (call.name === 'message_scope' || call.name === 'delegate_to_child') {
      const type = String(args.target_scope_type ?? '').trim();
      const id = String(args.target_scope_id ?? '').trim();
      return type !== '' && id !== '' ? `${type}:${id}` : '';
    }
    if (call.name === 'domain_message') {
      const target = String(args.target ?? '').trim();
      if (target === '') return '';
      if (target === 'master') return MASTER_SCOPE_KEY;
      return target.includes(':') ? target : `domain:${target}`;
    }
    return '';
  }

  /** Master → source child: relay the master's conclusion back as intel. */
  private relayBackToChild(fromScope: string, masterText: string): void {
    const masterScope = MASTER_SCOPE_KEY;
    if (fromScope === masterScope) return; // never relay into ourselves
    const text = `[主AI回传] ${masterText.slice(0, 800)}\n（这是对此前上报的正式回话；其中给当前用户或当前会话的结论，要用 send_message 主动转达，不要等用户追问。）`;
    this.injectInternalMessage(fromScope, text, { relayed_from: masterScope });
  }

  /** Master → users: send a message directly to another scope (bypass child). */
  private async sendToScope(targetScopeType: string, targetScopeId: string, content: string): Promise<string> {
    if (targetScopeType === 'private' && this.isCurrentSelf(targetScopeId)) {
      return '目标是机器人自己的账号，已跳过（不跟自己聊天）。';
    }
    // Same outbound normalization as send_message: strip [[...]] markers and
    // (private only) CQ:at codes, then deliver line-by-line ("分条").
    let text = String(content ?? '').replace(/\[\[.*?\]\]/g, '');
    if (targetScopeType === 'private') text = text.replace(/\[CQ:at,qq=\d+\]/g, '');
    const lines = replyMessageLines(text);
    if (lines.length === 0) return '内容过滤后为空，未发送。';
    const refs: string[] = [];
    let failure = '';
    for (const line of lines) {
      try {
        const result = await this.bot.sendText(targetScopeType as 'group' | 'private', Number(targetScopeId), line);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = this.repo.registerMessageRef(targetScopeType, targetScopeId, sentId ?? null);
        refs.push(ref);
        this.repo.appendMessage(targetScopeType, targetScopeId, {
          user_id: this.bot.selfId,
          nickname: this.bot.selfNickname || 'AI',
          text: line,
          raw_message: line,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: 'master-direct',
        });
      } catch (error) {
        failure = error instanceof Error ? error.message : String(error);
        break;
      }
    }
    if (refs.length === 0) return `直接发送失败: ${failure}`;
    const interrupted = failure ? `（后续行发送中断: ${failure}）` : '';
    return `已直接发送 ${refs.length} 条到 ${targetScopeType}:${targetScopeId}（短ID ${refs.join('、')}）${interrupted}。`;
  }

  // ── crash guard / admin notify ────────────────────────────────────────────

  private persistInterruptNote(scopeType: string, scopeId: string, error: unknown, executedTools: string[]): void {
    try {
      const executed = describeExecuted(executedTools);
      let note = `[系统] 上轮 AI 处理异常中断: ${error instanceof Error ? error.name : typeof error}`;
      if (executed) {
        note += `\n中断前这些工具已经执行完并生效了: ${executed}。\n这一轮的模型回复丢失了，但上面的操作是真做过的，不要当成没发生；如需确认结果请重新查询，不要重复执行。`;
      }
      this.repo.appendMessage(scopeType, scopeId, {
        user_id: this.bot.selfId,
        nickname: this.bot.selfNickname || 'AI',
        text: note,
        raw_message: note,
        message_id: null,
        timestamp: Date.now() / 1000,
        source_label: 'system-error',
      });
    } catch (persistError) {
      console.error('[orchestrator] interrupt-note persist failed:', persistError);
    }
  }

  private notifyAdminIfNeeded(scopeType: string, scopeId: string, error: unknown, executedTools: string[]): void {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    const lowered = message.toLowerCase();
    const isSoft = SOFT_ERROR_PATTERNS.some((pattern) => lowered.includes(pattern));
    if (isSoft && executedTools.length === 0) return; // soft errors stay quiet
    // void+try/catch 接不住 rejection（node 24 默认 unhandledRejection=throw 会崩进程），必须挂 .catch
    void this.bot.sendText('private', this.config.adminQq, `[AI异常通知]\n会话: ${scopeType}:${scopeId}\n错误: ${message.slice(0, 200)}\n已执行工具: ${describeExecuted(executedTools) || '无'}`)
      .catch(() => { /* best-effort */ });
  }

  // ── misc helpers ──────────────────────────────────────────────────────────

  /** 号主本人发的消息（号主私聊下级会话，或经数据线直连主AI）。 */
  private isMasterMessage(message: ChatMessage): boolean {
    return (message.chatType === 'private' || message.chatType === MASTER_SCOPE_TYPE)
      && String(message.userId) === String(this.config.masterQq);
  }

  private isAdminMessage(message: ChatMessage): boolean {
    return message.chatType === 'private' && String(message.userId) === String(this.config.adminQq);
  }

  private buildTriggerEntry(
    message: ChatMessage,
    cleaned: string,
    ref: string,
    label: string,
    sourceKind: string,
    imageRefs: string[] = [],
  ): TriggerEntry {
    const baseText = cleaned || message.text;
    return {
      user_id: message.userId,
      nickname: messageNickname(message),
      text: markMentionsSelf(
        message,
        imageRefs.length > 0 ? `${baseText}${baseText ? ' ' : ''}[图片×${imageRefs.length}]` : baseText,
      ),
      raw_message: message.rawMessage,
      message_id: message.messageId,
      message_ref: ref,
      timestamp: message.timestamp,
      source_label: label,
      source_kind: sourceKind as TriggerEntry['source_kind'],
      raw_source: String((message.rawData as Record<string, unknown>).source ?? ''),
      // 请示回执追踪：上报方要求回话时，本回合结束还没回执就会被系统追问。
      ack_required_from: String((message.rawData as Record<string, unknown>).ack_required ?? '') || undefined,
      image_refs: imageRefs,
    };
  }

  private async resolveDisplayName(scopeType: string, scopeId: string): Promise<void> {
    try {
      // 虚拟 scope（domain/bili/mcclient）没有对应 QQ 实体，id 也可能是字符串。
      if (scopeType !== 'group' && scopeType !== 'private') return;
      const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
      if (agent.displayName) return;
      const info =
        scopeType === 'group'
          ? await this.bot.getGroupInfo(Number(scopeId))
          : await this.bot.getStrangerInfo(Number(scopeId));
      const name = String((info as { group_name?: string; nickname?: string }).group_name ?? (info as { nickname?: string }).nickname ?? '').trim();
      if (name) this.repo.updateAgent(scopeType, scopeId, { displayName: name });
    } catch {
      // best-effort
    }
  }

  /** 全局身份卡 + 人设参考图清单：面板上传的参考图告诉 AI 有哪些可用（画自己用）。 */
  private async identityContextWithRefs(scopeKey: string): Promise<string> {
    const card = await globalIdentityCard(scopeKey);
    const refs = listPersonaRefs();
    if (refs.length === 0) return card;
    const line =
      `[人设参考图] 面板上传了 ${refs.length} 张「自己长什么样」的参考图：${refs
        .map((r) => `persona-ref://${r.name}`)
        .join('、')}。` +
      '画自己/自拍/保持形象一致时用 image_edit：以参考图为底改图（image=persona-ref://…）；要把「自己」画进用户发来的照片，就 image=用户那张图 + persona_ref=参考图名 两个一起传，模型会同时看到两张（用户图为主、参考图定长相）。';
    return card === '' ? line : `${card}\n${line}`;
  }

  /**
   * 回合请求落盘：每轮发给模型的完整 system+messages 存 data/state/turn-dump/
   * （每会话只留最近 3 回合）。「模型当时到底看见了什么上下文」类暗伤——
   * 比如自己重发同一张图 4 次——有了这份现场就不用再猜（2026-10-30）。
   */
  private dumpTurnRequest(
    scopeKey: string,
    mid: string,
    round: number,
    system: unknown,
    messages: unknown,
    toolCallCount: number,
  ): void {
    try {
      const dir = join(dirname(this.workspaceDir), 'turn-dump', scopeKey.replace(/[^\w-]/g, '_'));
      mkdirSync(dir, { recursive: true });
      const stamp = `${Date.now()}_${mid || 'nomid'}`;
      for (const old of readdirSync(dir).sort().reverse().slice(80)) {
        void rmSync(join(dir, old), { force: true, recursive: true });
      }
      // 同一回合（同 stamp 前缀）多轮共享一个子目录；这里按回合一文件简单化：
      // 每轮追加一份快照，保留最近 80 份，够回看完整回合。
      const payload = JSON.stringify({ scopeKey, mid, round, toolCallCount, system, messages }, null, 1);
      writeFileSync(join(dir, `${stamp}_r${round}.json`), payload);
    } catch {
      // 落盘失败不影响回合。
    }
  }

  private async groupContext(scopeId: string): Promise<string> {
    const lines: string[] = [];
    try {
      const info = await this.bot.getGroupInfo(Number(scopeId));
      const name = String(info.group_name ?? '').trim();
      if (name) lines.push(`群名: ${name}`);
      if (Number.isFinite(info.member_count) && Number(info.member_count) > 0) {
        lines.push(`群人数: ${info.member_count}${Number.isFinite(info.max_member_count) ? ` / 上限 ${info.max_member_count}` : ''}`);
      }
    } catch {
      // best-effort
    }
    const roles = await this.groupRoles.get(scopeId);
    if (roles.owner !== null) lines.push(`群主: ${roles.names[roles.owner] ?? roles.owner}(${roles.owner})`);
    if (roles.admins.length > 0) {
      const shown = roles.admins.slice(0, 8).map((id) => `${roles.names[id] ?? id}(${id})`).join('、');
      lines.push(`管理员: ${shown}${roles.admins.length > 8 ? ` 等 ${roles.admins.length} 人` : ''}`);
    }
    if (roles.botRole === 'owner' || roles.botRole === 'admin') {
      lines.push(`我在群里的身份: ${roles.botRole === 'owner' ? '群主' : '管理员'}（可以对违规内容用 recall_message 撤回群员消息、group_mute 禁言群员；只用于广告/刷屏/人身攻击等，别滥用，拿不准就不动）`);
    } else {
      lines.push('我在群里的身份: 普通成员（没有撤回他人消息和禁言的权限）');
    }
    return lines.join('\n');
  }
}

// ── pure helpers ─────────────────────────────────────────────────────────────

function hasScheduledTask(item: TurnItem): boolean {
  return [item, ...(item.batchItems ?? [])].some((candidate) => {
    const raw = candidate.message?.rawData;
    return raw !== undefined && (raw.alarm_task !== undefined || raw.recurring_task !== undefined);
  });
}

function stripTriggerEntries(history: HistoryEntry[], triggerMessages: TriggerEntry[]): HistoryEntry[] {
  const triggerIds = new Set(
    triggerMessages.map((entry) => String(entry.message_id ?? '')).filter((id) => id !== ''),
  );
  if (triggerIds.size > 0) {
    return history.filter((entry) => !triggerIds.has(String(entry.message_id ?? '')));
  }
  if (triggerMessages.length > 0 && history.length >= triggerMessages.length) {
    return history.slice(0, history.length - triggerMessages.length);
  }
  return history;
}

function dedupeEntries(entries: TriggerEntry[]): TriggerEntry[] {
  const seen = new Set<string>();
  const result: TriggerEntry[] = [];
  for (const entry of entries) {
    const key = [
      String(entry.message_id ?? ''),
      String(entry.message_ref ?? ''),
      String(entry.raw_message ?? ''),
      String(entry.text ?? ''),
      String(entry.timestamp ?? ''),
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(entry);
  }
  return result;
}

/**
 * Group debounce nudge: a synthetic user_id=0 message ("群里沉默了5秒…")
 * that continues a real conversation. isInternalReportEntry() treats it as
 * internal (and history rendering keeps it invisible, which is correct), but
 * unlike relays/alarms it is still a chat turn and needs persona + rescue.
 */
function isDebounceMessage(message: ChatMessage): boolean {
  return String((message.rawData as Record<string, unknown>).source ?? '') === 'group_reply_debounce';
}

function isDebounceTrigger(entry: TriggerEntry): boolean {
  return String(entry.raw_source ?? '') === 'group_reply_debounce';
}

function hasActionableEvent(item: TurnItem): boolean {
  const items = item.batchItems && item.batchItems.length > 0 ? item.batchItems : [item];
  return items.some((entry) => !(entry.kind === 'message' && entry.silentEvent));
}

function triggerToHistory(entry: TriggerEntry): HistoryEntry {
  return {
    user_id: entry.user_id,
    nickname: entry.nickname,
    text: entry.text,
    raw_message: entry.raw_message,
    message_id: entry.message_id ?? null,
    message_ref: entry.message_ref,
    timestamp: entry.timestamp,
    source_kind: entry.source_kind,
    source_label: entry.source_label,
    image_refs: entry.image_refs,
  };
}

function describeExecuted(executedTools: string[]): string {
  if (executedTools.length === 0) return '';
  const counts = new Map<string, number>();
  for (const name of executedTools) counts.set(name, (counts.get(name) ?? 0) + 1);
  return [...counts.entries()].map(([name, count]) => `${name}×${count}`).join(', ');
}

/** 主AI 这轮其实是「不说话」（stay_silent 的各种写法）：不能当结论回传给子AI。
 *  实锤 2026-10-30：主AI 纯文本输出了字面 "stay_silent"，被原样封进 [主AI回传]
 *  打回中枢，平白多跑一轮还把 "stay_silent" 当情报滚进两边上下文。 */
function isSilenceConclusion(text: string): boolean {
  return /^(?:<stay_silent\s*\/?\s*(?:<\/stay_silent>)?|<\/?stay_silent\s*\/?>|stay_silent|（已回复，本轮结束）)\s*$/i.test(text.trim());
}

/** Last `[子AI上报 from scope]` marker among the turn's triggers (回传目标). */
function latestRelaySource(triggers: TriggerEntry[]): string {
  let found = '';
  for (const entry of triggers) {
    const match = /\[子AI上报 from ([^\]\s]+)(?:\s[^\]]*)?\]/.exec(String(entry.text ?? ''));
    if (match) found = match[1];
  }
  return found;
}

/** Background hint when the current triggers carry images (legacy view_image nudge). */
function buildImageHint(triggers: TriggerEntry[]): string {
  const count = triggers.reduce((sum, entry) => sum + (entry.image_refs?.length ?? 0), 0);
  if (count === 0) return '';
  return (
    `本次消息包含 ${count} 张图片（聊天记录里显示为 [图片×N] 标记）。`
    + '用户明确要求改图时，直接调用 image_edit：image 省略就会自动使用本轮最新的图，不必先 view_image；'
    + 'prompt 必须完整保留用户的每一项修改要求并同时执行（例如「镂空+像素」=去背景并像素化，不是二选一，也不要只完成像素化）。'
    + '指令已经明确时不要反问确认。只有需要看清细节才能写修改要求时才 view_image。'
    + '编辑历史图时，只能使用消息里实际标出的 [#XXXX] 短ID，禁止编造。'
    + '只有用户明确要求立即把当前原图发回时，才可无参调用 send_image；指定历史图片必须传真实 message_ref。没有当前图片且有多个历史候选时不能默认退回最新一张。不要先 view_image，也不要修改原图。'
    + '用户要求稍后/定时把原图发回时，直接调用 schedule_image（例如 10 分钟后 at="+10m"），不要用 create_task 或图片旧链接代替。'
    + '工具失败按报错修正一次；同一报错连续出现就停止，不要换着参数反复硬试。'
  );
}

/** Group-scope background line: current trigger settings + 没融入少说话 guidance. */
function buildTriggerInfo(agent: { triggerRate: number; triggerWords: string[] }): string {
  return (
    `当前随机接话率 ${(agent.triggerRate * 100).toFixed(1)}%，触发词：${agent.triggerWords.join('、') || '（无）'}。`
    + '如果还没融入这个群、和大家不熟，尽量少说话、先观察，宁可沉默也不要硬接话。'
    + '觉得被嫌吵就调低接话率，混熟了想更活跃再调高；想让大家能用别的称呼叫你也可以加触发词——用 trigger_config 工具自己调整。'
  );
}

export function formatQqRequestEvent(event: Record<string, unknown>): string {
  const type = String(event.request_type ?? '');
  const flag = String(event.flag ?? '');
  const user = String(event.user_id ?? '?');
  const comment = String(event.comment ?? '').slice(0, 500);
  if (type === 'friend') {
    return `[QQ 好友申请] 申请人=${user}，验证消息=${JSON.stringify(comment)}，flag=${flag}。`
      + '可用 qq_account 查看并 approve_friend/reject_friend；不要替主人随意扩大好友圈。';
  }
  const invite = String(event.sub_type ?? '') === 'invite';
  const group = String(event.group_id ?? '?');
  return invite
    ? `[QQ 入群邀请] ${user} 邀请机器人加入群 ${group}，验证消息=${JSON.stringify(comment)}，flag=${flag}。`
      + '同意即加群，可用 qq_account 的 approve_group/reject_group 处理。'
    : `[QQ 入群申请] ${user} 申请加入群 ${group}，验证消息=${JSON.stringify(comment)}，flag=${flag}。`
      + '可用 qq_account 的 approve_group/reject_group 处理；无明确依据不要自动放行。';
}

function nowText(): string {
  const date = new Date();
  const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} 星期${weekdays[date.getDay()]} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
