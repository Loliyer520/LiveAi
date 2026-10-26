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
import { ScopeRepository } from '../store/repository.js';
import type { MessageArchive } from '../store/archive.js';
import type { DiarySummarizer } from '../store/summarizer.js';
import type { RelationGraph } from '../relations/graph.js';
import type { KnowledgeBase } from '../knowledge/kb.js';
import { domainDefForScope, hubForScope } from './domains.js';
import type { IntelCollector } from '../relations/collector.js';
import { PromptStore } from '../prompt/store.js';
import { buildChildMessages } from '../prompt/assemble.js';
import { ModelManager } from '../models/manager.js';
import { completeChatTracked } from '../models/usage.js';
import { InMemoryEventMailbox } from '../scope/mailbox.js';
import { CharacterSessionRegistry } from '../scope/session.js';
import { ScopeActorDispatcher } from '../scope/actor.js';
import { AtomicTurnBatchCoordinator, mergeFollowupItems } from '../scope/coordinator.js';
import { envelopeFromTurnItem, type TurnItem } from '../scope/turn.js';
import type { ChatMessage, HistoryEntry, ModelMessage, TriggerEntry } from './types.js';
import { isInternalReportEntry, messageNickname } from './types.js';
import { cleanText, markMentionsSelf, shouldIgnoreMessage, sourceKindOf, sourceLabelOf } from './source.js';
import { GroupReplyWindows, buildDebounceTriggerMessage, shouldTrigger } from './trigger.js';
import { GroupRolesCache } from './group-roles.js';
import { chatToolSchemas, executeChatTool, refreshStickerIndex, type StickerRefreshResult, type ToolContext } from './tools.js';
import { TaskScheduler, type ScheduledTask } from './scheduler.js';
import type { NovelCommandHandler } from './novel-cmd.js';
import { embedImageAsDataUrl, extractImageRefs } from './images.js';
import { extractFileRefs } from './files.js';
import { replyMessageLines } from './reply-lines.js';
import { Workspace } from './workspace.js';
import type { AiConfig } from '../config/config.js';

const MAX_TOOL_ITERATIONS = 12;
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

  private messageEpoch = 0;
  private readonly recentMessageKeys = new Map<string, number>();
  private running = false;
  /** 主AI回复的出路：跟随主人最近一次的来路（QQ 私聊 or 手机数据线）。 */
  private masterChannel: 'qq' | 'dataline' = 'qq';

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

  async stop(): Promise<void> {
    this.running = false;
    this.bumpEpoch();
    this.groupWindows.cancelAll();
    this.tasks.stop(); // timers cleared; tasks.json persists for next boot
    await this.dispatcher.close();
    await this.repo.flush();
    await this.archive.flush();
    await this.intel.drain();
    await this.relations.flush();
    await this.kb.flush();
  }

  // ── inbound ───────────────────────────────────────────────────────────────

  handleMessage(message: ChatMessage): void {
    if (!this.running) return;
    if (this.bot.isSelfId(message.userId)) return;
    // Self-chat (QQ "发给自己") is not a conversation scope — never process it.
    if (message.chatType === 'private' && this.bot.isSelfId(message.chatId)) return;
    // 主人从自己的 QQ 私聊过来了 → 主AI回复走回 QQ 通道（数据线通道让位）。
    if (message.chatType === 'private' && String(message.userId) === String(this.config.masterQq)) {
      this.masterChannel = 'qq';
    }
    if (this.isDuplicateEvent(message)) return;
    void this.enqueueMessage(message);
  }

  /**
   * QQ 数据线（我的电脑/我的手机）入口：主人在手机上用同一账号给 Linux 发消息。
   * 注入主AI会话（private:masterQq，与主人 QQ 私聊同一个连续会话），并记住
   * 来路——此后主AI的回复经 sendTextOverride 改走数据线回主人手机。
   */
  handleDatalineMessage(message: ChatMessage): void {
    if (!this.running) return;
    this.masterChannel = 'dataline';
    const relayed: ChatMessage = {
      ...message,
      chatType: 'private',
      chatId: this.config.masterQq,
      userId: this.config.masterQq,
      rawData: { ...(message.rawData as Record<string, unknown>), source: 'dataline' },
    };
    if (this.isDuplicateEvent(relayed)) return;
    void this.enqueueMessage(relayed);
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
    if (message.chatType === 'private' && this.bot.isSelfId(message.chatId)) return;
    const text = (message.text || '').trim();
    if (!text) return;
    if (this.isDuplicateEvent(message)) return;
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
      const fileRefs = extractFileRefs(message);
      const baseText = cleaned || message.text;
      let displayText = baseText;
      if (imageRefs.length > 0) displayText = `${displayText}${displayText ? ' ' : ''}[图片×${imageRefs.length}]`;
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
      };
      if (this.repo.appendMessage(scopeType, scopeId, entry)) {
        this.summarizer.notify(scopeType, scopeId);
      }
      // Relation network: observe every real participant, and let the
      // async intel collector count this message toward its window.
      if (message.userId > 0 && String(message.userId) !== String(this.bot.selfId)) {
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
      const followup = await this.runMessageTurn(scopeKey, current);
      console.info(`[orchestrator] turn done scope=${scopeKey} took=${Math.round((Date.now() - turnStart) / 1000)}s`);
      current = followup;
    }
  }

  private async runMessageTurn(scopeKey: string, item: TurnItem): Promise<TurnItem | null> {
    const message = item.message;
    if (message === null) return this.mergeFollowupAfterTurn(scopeKey, item, false);
    const runEpoch = this.resolveEpoch(item.messageEpoch);
    if (this.isEpochStale(runEpoch)) return null;

    const scopeType = message.chatType;
    const scopeId = String(message.chatId);
    const domain = domainDefForScope(`${scopeType}:${scopeId}`);
    const cleaned = item.cleaned || message.text;
    this.models.beginRequest('main');
    const executedTools: string[] = [];
    const isMasterScope = scopeType === 'private' && scopeId === String(this.config.masterQq);
    /** Master's last plain-text reply — relayed back to the reporting child. */
    let lastModelText = '';
    /** Latest child scope that reported into this master turn (回传 target). */
    let relayFromScope = '';
    /** Hoisted so the post-turn debounce reset can see it after the catch. */
    let sentThisTurn = false;

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
      if (isMasterScope) {
        relayFromScope = latestRelaySource(triggerMessages);
      }

      let toolRound = 0;
      // In-turn conversation extension (legacy tool_context): each assistant
      // reply and its tool results are appended here and replayed on every
      // later round of THIS turn — without it the model never sees what its
      // tools returned (view_image descriptions etc.) and loops on the same
      // call until the iteration cap, ending the turn in silence.
      const extension: ModelMessage[] = [];
      let fallbackPrompts = 0;
      // Per-turn autonomous-action state: the workspace sandbox and the shared
      // network-call budget must live for the WHOLE turn, not one tool round.
      const turnWorkspace = new Workspace(join(this.workspaceDir, `${scopeType}_${scopeId}`));
      const networkBudget = { used: 0 };
      sentThisTurn = false;
      for (;;) {
        // Master turns are usually triggered by internal child reports; the
        // persona tail (main.txt + tool protocol) must still be injected.
        const injectPersona =
          isMasterScope ||
          (toolRound === 0 && !triggerMessages.some((entry) => isInternalReportEntry(entry)));
        const { system, messages } = buildChildMessages({
          bundle,
          message,
          persona,
          impression: this.relations.scopeImpression(scopeKey),
          historyBeforeTrigger,
          triggerMessages,
          background: {
            impression: this.relations.scopeImpression(scopeKey),
            globalIdentityContext: '',
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
            message.userId !== 0 || sourceKindOf(message) === 'admin_webui'
              ? '（系统提示，仅你可见、用户看不到：回复用户必须调用 send_message 工具，直接输出的普通文字用户完全收不到；'
                + '要查资料/取章节/发文件就直接调用对应工具，不要用文字描述你要做的动作。'
                + '工具要以工具调用的形式发起，把工具名当普通文字写出来不算调用。）'
              : '',
        });

        const model = this.models.getModelForRole('main');
        if (model === null) throw new Error('没有可用的聊天模型（roles.main 未配置或渠道为空）');

        let reply;
        try {
          reply = await completeChatTracked('main', model, {
            system,
            messages: [...messages, ...extension],
            tools: chatToolSchemas(scopeKey, isMasterScope),
            temperature: 0.85,
            maxTokens: 2048,
            thinkingLevel: 'off',
          });
        } catch (error) {
          this.models.notifyFailure('main');
          throw error;
        }
        // 每轮决策落一行：调了什么工具 / 纯文本前缀。「有回合无输出」类
        // 故障（模型散文回复、莫名 stay_silent）没有它只能瞎猜。
        console.info(`[orchestrator] round scope=${scopeKey} tools=${reply.toolCalls.map((c) => c.name).join('+') || '-'} text=${reply.text.trim().slice(0, 80)}`);
        // Only no-tool-call rounds count as a "conclusion": opus emits its
        // reasoning as plain text ALONGSIDE tool calls, and relaying that
        // leaked English chain-of-thought into child scopes (2026-09-29).
        if (reply.toolCalls.length === 0 && reply.text.trim()) lastModelText = reply.text.trim();

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
          const rescuable = message.userId !== 0 || sourceKindOf(message) === 'admin_webui';
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
          // 主人从手机数据线来的回合：send_message 改走数据线回手机，不发 QQ 私聊。
          // 无 QQ 窗口的虚拟 scope（domain 中枢 / bili 视频下级 / mcclient 下级）：
          // send_message 改道汇报给对应中枢（domain→主AI，bili→哔哩中枢，
          // mcclient→MC 中枢），由中枢决定下一步（回主人/再委派/存知识库）。
          sendTextOverride: isMasterScope && this.masterChannel === 'dataline'
            ? (line) => this.bot.sendDatalineText(line)
            : domain !== null || scopeType === 'bili' || scopeType === 'mcclient'
              ? (line) => {
                  this.relayToMaster({ text: line, fromScope: `${scopeType}:${scopeId}` });
                  return Promise.resolve({});
                }
              : undefined,
          triggerImageRefs: triggerMessages.flatMap((entry) => entry.image_refs ?? []),
          describeImage: (url, question) => this.describeImage(url, question),
          getGroupRoles: scopeType === 'group' ? () => this.groupRoles.get(scopeId) : undefined,
          scopeType,
          scopeId,
          executedTools,
        };

        let sawStaySilent = false;
        // Replay the assistant turn verbatim (tool_use ids must match the
        // tool_result blocks below; fabricate ids when the provider omitted them).
        const calls = reply.toolCalls.map((call, index) => ({
          ...call,
          id: call.id || `toolu_${Date.now()}_${index}_${Math.random().toString(36).slice(2, 8)}`,
        }));
        extension.push({ role: 'assistant', content: reply.text, toolCalls: calls });
        for (const call of calls) {
          const result = await executeChatTool(call, toolContext);
          if (result.staySilent) sawStaySilent = true;
          if (result.ok && (result.sentToUser || call.name === 'send_message')) sentThisTurn = true;
          extension.push({ role: 'tool_result', toolCallId: call.id, content: result.output });
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
            if (isMasterScope) {
              relayFromScope = latestRelaySource(triggerMessages) || relayFromScope;
            }
            item.deferredCount += Math.max(1, folded.deferredCount);
            if (folded.message !== null) {
              item.message = folded.message;
              item.cleaned = folded.cleaned;
            }
          }
        }
      }
    } catch (error) {
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

    // 主AI回传闭环: master's plain-text conclusion goes back to the child that
    // reported, as an internal intel note (legacy "直接输出普通文本回传"语义).
    // Only for internally-triggered turns — a reply to the master himself must
    // never leak into a child scope.
    if (isMasterScope && message.userId === 0 && lastModelText && relayFromScope) {
      this.relayBackToChild(relayFromScope, lastModelText);
    }

    const completed = !this.isEpochStale(runEpoch);
    return this.mergeFollowupAfterTurn(scopeKey, item, completed);
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
    return Date.now() / 1000 - message.timestamp > this.config.staleMessageMaxAgeSeconds;
  }

  private scopeBusy(scopeKey: string): boolean {
    const session = this.sessions.get(scopeKey);
    return session ? session.isBusy() : this.mailbox.pendingCount(scopeKey) > 0;
  }

  // ── dedup ─────────────────────────────────────────────────────────────────

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
  private relayToMaster(payload: { text: string; requestType?: string; fromScope: string }): void {
    const masterScope = `private:${this.config.masterQq}`;
    const text = `[子AI上报 from ${payload.fromScope}${payload.requestType ? ` type=${payload.requestType}` : ''}]\n${payload.text}`;
    const hub = hubForScope(payload.fromScope, masterScope);
    if (hub !== null) {
      this.injectInternalMessage(hub, text, { relayed_from: payload.fromScope });
      return;
    }
    const relay: ChatMessage = {
      chatType: 'private',
      chatId: Number(this.config.masterQq),
      userId: 0,
      text,
      rawMessage: text,
      sender: { nickname: '后台任务', user_id: 0 },
      messageId: `sys_relay_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      mentionsSelf: false,
      timestamp: Date.now() / 1000,
      rawData: { source: 'agent_message', relayed_from: payload.fromScope },
    };
    this.handleMessage(relay);
  }

  /** Scheduler fire callback: a task firing becomes an internal scope turn. */
  private fireTask(task: ScheduledTask): void {
    if (!this.running) return;
    if (task.kind === 'recurring') {
      this.injectInternalMessage(
        task.originScope,
        `[周期任务触发] ${task.note}\n（这是你自己创建的周期任务（${task.taskId}），按指令内容自主判断这轮要做什么；不需要行动就 stay_silent。要停止它用 cancel_task。）`,
        { recurring_task: task.taskId },
      );
    } else {
      this.injectInternalMessage(task.originScope, `[闹钟触发] ${task.note}`, { alarm_task: task.taskId });
    }
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
      chatType: 'private',
      chatId: this.config.masterQq,
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

  /** Master → source child: relay the master's conclusion back as intel. */
  private relayBackToChild(fromScope: string, masterText: string): void {
    const masterScope = `private:${this.config.masterQq}`;
    if (fromScope === masterScope) return; // never relay into ourselves
    const text = `[主AI回传] ${masterText.slice(0, 800)}`;
    this.injectInternalMessage(fromScope, text, { relayed_from: masterScope });
  }

  /** Master → users: send a message directly to another scope (bypass child). */
  private async sendToScope(targetScopeType: string, targetScopeId: string, content: string): Promise<string> {
    if (targetScopeType === 'private' && this.bot.isSelfId(targetScopeId)) {
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

  private isMasterMessage(message: ChatMessage): boolean {
    return message.chatType === 'private' && String(message.userId) === String(this.config.masterQq);
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

  private async groupContext(scopeId: string): Promise<string> {
    const lines: string[] = [];
    try {
      const info = await this.bot.getGroupInfo(Number(scopeId));
      const name = String(info.group_name ?? '').trim();
      if (name) lines.push(`群名: ${name}`);
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
    + '需要了解图片内容时调用 view_image 工具查看（index 从 1 开始）；'
    + '与当前对话无关的图不必每张都看。'
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

function nowText(): string {
  const date = new Date();
  const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} 星期${weekdays[date.getDay()]} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
