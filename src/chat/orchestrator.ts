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
import type { IntelCollector } from '../relations/collector.js';
import { PromptStore } from '../prompt/store.js';
import { buildChildMessages } from '../prompt/assemble.js';
import { ModelManager } from '../models/manager.js';
import { completeChat } from '../models/protocol.js';
import { InMemoryEventMailbox } from '../scope/mailbox.js';
import { CharacterSessionRegistry } from '../scope/session.js';
import { ScopeActorDispatcher } from '../scope/actor.js';
import { AtomicTurnBatchCoordinator, mergeFollowupItems } from '../scope/coordinator.js';
import { envelopeFromTurnItem, type TurnItem } from '../scope/turn.js';
import type { ChatMessage, HistoryEntry, ModelMessage, TriggerEntry } from './types.js';
import { isInternalReportEntry, messageNickname } from './types.js';
import { cleanText, markMentionsSelf, shouldIgnoreMessage, sourceKindOf, sourceLabelOf } from './source.js';
import { GroupReplyWindows, buildDebounceTriggerMessage, shouldTrigger } from './trigger.js';
import { chatToolSchemas, executeChatTool, type ToolContext } from './tools.js';
import { TaskScheduler, type ScheduledTask } from './scheduler.js';
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
  private readonly tasks: TaskScheduler;
  private readonly workspaceDir: string;

  private messageEpoch = 0;
  private readonly recentMessageKeys = new Map<string, number>();
  private running = false;

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
    /** Path of the persisted tasks.json (alarms + recurring tasks). */
    tasksPath: string;
    /** Root of per-scope file workspaces (defaults to <stateDir>/workspace). */
    workspaceDir?: string;
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
    this.tasks = new TaskScheduler(deps.tasksPath, (task) => this.fireTask(task));
    this.workspaceDir = deps.workspaceDir ?? join(dirname(deps.tasksPath), 'workspace');
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
  }

  // ── inbound ───────────────────────────────────────────────────────────────

  handleMessage(message: ChatMessage): void {
    if (!this.running) return;
    if (String(message.userId) === String(this.bot.selfId)) return;
    if (this.isDuplicateEvent(message)) return;
    void this.enqueueMessage(message);
  }

  handleSelfMessage(message: ChatMessage): void {
    if (!this.running) return;
    if (!String(message.text ?? '').trim()) return;
    const relayed: ChatMessage = {
      ...message,
      rawData: { ...message.rawData, source: 'self_other_device' },
    };
    void this.enqueueMessage(relayed);
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
      const followup = await this.runMessageTurn(scopeKey, current);
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
    const cleaned = item.cleaned || message.text;
    const executedTools: string[] = [];
    const isMasterScope = scopeType === 'private' && scopeId === String(this.config.masterQq);
    /** Master's last plain-text reply — relayed back to the reporting child. */
    let lastModelText = '';
    /** Latest child scope that reported into this master turn (回传 target). */
    let relayFromScope = '';

    try {
      const agent = this.repo.getOrCreateAgent(scopeType, scopeId);
      const bundle = await this.promptStore.bundle();
      const persona = isMasterScope ? bundle.main : agent.persona || bundle.char;
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
      let fallbackPrompted = false;
      // Per-turn autonomous-action state: the workspace sandbox and the shared
      // network-call budget must live for the WHOLE turn, not one tool round.
      const turnWorkspace = new Workspace(join(this.workspaceDir, `${scopeType}_${scopeId}`));
      const networkBudget = { used: 0 };
      let sentThisTurn = false;
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
            masterQq: String(this.config.masterQq),
            nowText: nowText(),
          },
          injectPersona,
          chatMode: true,
          masterMode: isMasterScope,
          modeHint: '',
        });

        const model = this.models.getModelForRole('main');
        if (model === null) throw new Error('没有可用的聊天模型（roles.main 未配置或渠道为空）');

        let reply;
        try {
          reply = await completeChat(model, {
            system,
            messages: [...messages, ...extension],
            tools: chatToolSchemas(isMasterScope),
            temperature: 0.85,
            maxTokens: 2048,
            thinkingLevel: 'off',
          });
        } catch (error) {
          this.models.notifyFailure('main');
          throw error;
        }
        if (reply.text.trim()) lastModelText = reply.text.trim();

        if (reply.toolCalls.length === 0) {
          // Plain text without a tool call: the user saw nothing of it. Legacy
          // re-prompted once ("重新决策") before giving up, so a model that
          // answered in prose (e.g. homework it just viewed) gets one chance
          // to resend via send_message or to explicitly stay_silent. Internal
          // turns (relays, alarms) keep plain text as their conclusion.
          if (!sentThisTurn && !fallbackPrompted && reply.text.trim() && message.userId !== 0) {
            fallbackPrompted = true;
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
          break;
        }

        const toolContext: ToolContext = {
          bot: this.bot,
          repo: this.repo,
          archive: this.archive,
          relations: this.relations,
          tasks: this.tasks,
          masterQq: this.config.masterQq,
          isMaster: isMasterScope,
          notifyMaster: (payload) => this.relayToMaster(payload),
          workspace: turnWorkspace,
          networkBudget,
          delegateToChild: (targetType, targetId, instruction) => this.delegateToChild(targetType, targetId, instruction),
          sendToScope: (targetType, targetId, content) => this.sendToScope(targetType, targetId, content),
          triggerImageRefs: triggerMessages.flatMap((entry) => entry.image_refs ?? []),
          describeImage: (url, question) => this.describeImage(url, question),
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
      this.persistInterruptNote(scopeType, scopeId, error, executedTools);
      this.notifyAdminIfNeeded(scopeType, scopeId, error, executedTools);
    }

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
    const pending = { ...batch.turnItem, scopeKey };
    if (!hasActionableEvent(pending)) return null;
    return pending;
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

  private relayToMaster(payload: { text: string; requestType?: string; fromScope: string }): void {
    const masterScopeType = 'private';
    const masterScopeId = String(this.config.masterQq);
    const text = `[子AI上报 from ${payload.fromScope}${payload.requestType ? ` type=${payload.requestType}` : ''}]\n${payload.text}`;
    const relay: ChatMessage = {
      chatType: 'private',
      chatId: Number(masterScopeId),
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

  /** Vision describe for view_image (role 'vision', falls back to main). */
  private async describeImage(url: string, question: string): Promise<string> {
    const model = this.models.getModelForRole('vision');
    if (model === null) throw new Error('没有可用的视觉模型（roles.vision / roles.main 均未配置）');
    try {
      // Providers generally refuse to fetch remote URLs themselves, so embed
      // the image as a data: URL (legacy _download_remote_image semantics:
      // download locally with browser-ish headers, then base64).
      const embedded = await embedImageAsDataUrl(url);
      const reply = await completeChat(model, {
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
      return reply.text.trim() || '（视觉模型没有返回描述）';
    } catch (error) {
      this.models.notifyFailure('vision');
      throw error;
    }
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
    const message: ChatMessage = {
      chatType: scopeType as 'group' | 'private',
      chatId: Number(scopeId),
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
          nickname: '冰糖',
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
        nickname: '冰糖',
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
    try {
      void this.bot.sendText('private', this.config.adminQq, `[AI异常通知]\n会话: ${scopeType}:${scopeId}\n错误: ${message.slice(0, 200)}\n已执行工具: ${describeExecuted(executedTools) || '无'}`);
    } catch {
      // best-effort
    }
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
    try {
      const info = await this.bot.getGroupInfo(Number(scopeId));
      const name = String(info.group_name ?? '').trim();
      return name ? `群名: ${name}` : '';
    } catch {
      return '';
    }
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
