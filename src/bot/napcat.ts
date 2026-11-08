/**
 * NapCat / OneBot 11 adapter. Ported from legacy pack/napcat.py:
 *
 *  - forward WebSocket to NapCat for events (auto-reconnect, backoff)
 *  - HTTP API for actions (send/recall/group info) with access token
 *  - two-layer self-sent dedup: pending-send cache (content match right after
 *    we send) + recent message-id cache, so the bot never feeds its own
 *    outgoing messages back to itself as user input
 *  - friend/group request events cached for later approval tools
 */

import { createHash, timingSafeEqual } from 'node:crypto';
import type { ChatMessage } from '../chat/types.js';
import type { BridgeWatchdog } from './bridge-watchdog.js';
import { WsServer, type WsServerConnection } from './ws-server.js';
import { IdempotencyLedger } from './idempotency.js';

export interface NapcatHandlers {
  onGroupMessage?: (message: ChatMessage) => void;
  onPrivateMessage?: (message: ChatMessage) => void;
  onSelfMessage?: (message: ChatMessage) => void;
  /** QQ 数据线（我的电脑/我的手机）消息 — 桥侧补丁以 sub_type=dataline 上报。 */
  onDatalineMessage?: (message: ChatMessage) => void;
  onFriendRequest?: (event: Record<string, unknown>) => void;
  onGroupRequest?: (event: Record<string, unknown>) => void;
  onConnected?: () => void;
  onDisconnected?: (reason: string) => void;
}

export interface NapcatOptions {
  /**
   * 对接方式：server = 起 WebSocket 服务端等 NapCat 连入（需 reverseWsPort）；
   * client = 主动连 wsUrl 的正向 WS（事件+动作共用一条连接，断线期动作退
   * httpUrl）。缺省按 reverseWsPort 是否配置推导。
   */
  mode?: 'server' | 'client';
  wsUrl: string;
  httpUrl: string;
  selfId: number;
  /**
   * 允许接管反向 WS 连接的账号白名单。非空时，自报不在名单里的 NapCat
   * 连接一律拒绝（防换号后旧 NapCat 还在跑、或陌生人拿到 token 顶号）。
   * 缺省为空 = 不限制，谁先连上谁是现役。
   */
  allowedSelfIds?: readonly number[];
  /** Accounts this bot ran on before; their history lines still count as self. */
  legacySelfIds?: readonly number[];
  accessToken?: string;
  handlers: NapcatHandlers;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  /** 僵尸离线看门狗：post 成败都会回报，连续 BRIDGE_NOT_READY 由其判定处置。 */
  watchdog?: BridgeWatchdog;
  /**
   * mode=server：bot 起服务端监听该端口，NapCat（NAT 后）主动连进来，
   * 事件推送与动作调用共用这条双工连接。
   */
  reverseWsPort?: number;
  reverseWsHost?: string;
}

interface PendingSelfSent {
  key: string;
  expiresAt: number;
}

export class NapcatBot {
  private selfIdValue: number;
  private selfNicknameValue = '';
  private ws: WebSocket | null = null;
  private closed = false;
  private reconnectAttempt = 0;
  private pendingSelfSent = new Map<string, PendingSelfSent>();
  private recentSelfSentIds = new Map<string, number>();
  private readonly pendingRequestEvents = new Map<string, { event: Record<string, unknown>; receivedAt: number }>();
  /** Idempotency ledger: prevents duplicate sends during crash recovery / retries. */
  private readonly idempotency = new IdempotencyLedger();
  // 反向 WS 模式：NapCat 主动连进来的那条连接 + 在途动作的 echo 匹配表。
  private wsServer: WsServer | null = null;
  private reverseConn: WsServerConnection | null = null;
  /** 现役连接自报的账号；null=还没问到。 */
  private reverseConnSelfId: number | null = null;
  /** 正在做身份握手的连接。握手期间不挤掉现役连接。 */
  private pendingConn: WsServerConnection | null = null;
  /** 连接序号：握手期间又来了新连接时，旧握手作废。 */
  private connectSeq = 0;
  private actionSeq = 0;
  private readonly pendingActions = new Map<
    string,
    { resolve: (value: Record<string, unknown>) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();

  constructor(private readonly options: NapcatOptions) {
    this.selfIdValue = options.selfId;
  }

  /**
   * 面板保存连接配置时热更 access_token，免重启：鉴权每次连入都读
   * options.accessToken，换成新值后下一条连接立即按新 token 校验；
   * 已建立的连接是升级时验过的，不受影响。
   */
  setAccessToken(token: string): void {
    (this.options as { accessToken?: string }).accessToken = token || undefined;
  }

  /** Bot QQ; if unconfigured, adopted from the first event that carries self_id. */
  get selfId(): number {
    return this.selfIdValue;
  }

  /** Bot 账号的真实 QQ 昵称（get_login_info 学到；未学到时为空串，调用方自行兜底）。 */
  private connectedValue = false;

  /** WS 当前是否连着（管理面板状态灯用）。 */
  get connected(): boolean {
    return this.connectedValue;
  }

  /** 对接方式：server=本机起 WS 服务端等 NapCat 连入；client=主动连 NapCat 正向 WS。 */
  get connectionMode(): 'server' | 'client' {
    return this.isReverseMode() ? 'server' : 'client';
  }

  /** 反向（服务端）模式判定：显式 mode 优先，缺省按 reverseWsPort 是否配置推导。 */
  private isReverseMode(): boolean {
    if (this.options.mode !== undefined) return this.options.mode === 'server';
    return this.options.reverseWsPort !== undefined;
  }

  get selfNickname(): string {
    return this.selfNicknameValue;
  }

  /** Accounts this bot ran on before the current one. */
  get legacySelfIds(): readonly number[] {
    return this.options.legacySelfIds ?? [];
  }

  /** True for the current account AND any legacy account this bot used to run on. */
  isSelfId(id: unknown): boolean {
    const value = String(id ?? '');
    if (value === '' || value === '0') return false;
    if (value === String(this.selfIdValue)) return true;
    return this.legacySelfIds.some((legacy) => String(legacy) === value);
  }

  /**
   * 只认「现役登录账号」。入站闸/自聊闸必须用这个而不是 isSelfId：legacy
   * 旧号可能已被真人（号主）拿回来当自己的人在用——按 legacy 丢弃会把号主
   * 从旧号发来的私聊全部静默吞掉（2026-11-03 换号回滚实测踩中）。legacy
   * 集合只服务于历史渲染（旧号发言=自己说的），不参与入站过滤。
   */
  isCurrentSelfId(id: unknown): boolean {
    const value = String(id ?? '');
    return value !== '' && value !== '0' && value === String(this.selfIdValue);
  }

  /** 探 QQ 宿主状态（看门狗用）：反向模式下动作也走双工连接。 */
  async fetchStatus(): Promise<{ online: boolean; phase: string }> {
    const data = (await this.post('get_status', {})) as { online?: boolean; stat?: { bridge?: { phase?: string } } };
    return { online: data.online === true, phase: data.stat?.bridge?.phase ?? '?' };
  }

  start(): void {
    this.closed = false;
    if (this.isReverseMode()) {
      this.startReverseServer();
      return;
    }
    this.connect();
  }

  stop(): void {
    this.closed = true;
    this.ws?.close(1000, 'shutdown');
    this.ws = null;
    for (const [echo, entry] of this.pendingActions) {
      clearTimeout(entry.timer);
      entry.reject(new Error('NapCat action aborted: shutdown'));
      this.pendingActions.delete(echo);
    }
    void this.wsServer?.stop();
    this.wsServer = null;
    this.reverseConn = null;
    this.reverseConnSelfId = null;
    this.pendingConn = null;
  }

  /**
   * 反向 WS 鉴权。配置了 access_token 就恒定时间比对；没配则只放行本机
   * NapCat——反向端口默认监听 0.0.0.0，空 token 全放行等于让任何能连上
   * 端口的人以 bot 身份收发消息。
   */
  private authorizeReverseConnection(token: string | null, remoteAddress: string | null): boolean {
    const expected = this.options.accessToken ?? '';
    if (expected === '') return isLoopbackAddress(remoteAddress);
    if (token === null) return false;
    return constantTimeEqual(token, expected);
  }

  private startReverseServer(): void {
    const host = this.options.reverseWsHost ?? '0.0.0.0';
    const port = this.options.reverseWsPort as number;
    const server = new WsServer({
      authorize: (token, _url, remoteAddress) => this.authorizeReverseConnection(token, remoteAddress),
      onConnection: (conn) => {
        this.reconnectAttempt = 0;
        // 关闭动作不能立即做：先问这条连接是哪个号，退役账号（封号换号后
        // 仍在跑的旧 NapCat）只许兜底，不许把现役账号挤下线。
        const previousPending = this.pendingConn;
        this.pendingConn = conn;
        if (previousPending !== null && previousPending.id !== conn.id) {
          try { previousPending.close(1000); } catch { /* already gone */ }
        }
        void this.adoptReverseConnection(conn);
      },
      onText: (conn, data) => {
        // 切换窗口里可能同时挂着两条连接：动作响应照常匹配，事件只认现役
        // 连接，免得退役账号的心跳把 self_id 又翻回去、消息重复触发。
        if (this.resolveActionResponse(data)) return;
        if (this.reverseConn?.id !== conn.id) return;
        try {
          this.handleEvent(data);
        } catch (error) {
          console.error('[napcat] event handling failed:', error);
        }
      },
      onClose: (conn) => {
        if (this.pendingConn?.id === conn.id) this.pendingConn = null;
        if (this.reverseConn?.id !== conn.id) return;
        this.reverseConn = null;
        this.reverseConnSelfId = null;
        this.connectedValue = false;
        this.options.handlers.onDisconnected?.('reverse ws closed');
        this.failPendingActions('NapCat reverse connection closed');
      },
    });
    this.wsServer = server;
    server.start(host, port).catch((error) => {
      console.error(`[napcat] reverse ws server failed to listen ${host}:${port}:`, error);
    });
  }

  /** 动作响应匹配：消息带 echo 且在 pendingActions 表里 → 兑现对应 promise。 */
  private resolveActionResponse(raw: string): boolean {
    if (!raw.includes('"echo"')) return false;
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return false;
    }
    const echo = data.echo;
    if (typeof echo !== 'string' || !this.pendingActions.has(echo)) return false;
    const entry = this.pendingActions.get(echo);
    if (entry === undefined) return false;
    clearTimeout(entry.timer);
    this.pendingActions.delete(echo);
    const accepted = data.status === 'ok' || data.status === 'async';
    // Older/other OneBot implementations omit retcode on success; only a
    // present non-zero value means failure.
    const retcode = data.retcode;
    if (!accepted || (retcode !== undefined && retcode !== null && retcode !== 0)) {
      entry.reject(new Error(`NapCat action failed: ${String(data.status)}/${String(data.retcode)} ${String(data.message ?? '')}`));
    } else {
      entry.resolve((data.data as Record<string, unknown>) ?? {});
    }
    return true;
  }

  private failPendingActions(reason: string): void {
    for (const [echo, entry] of this.pendingActions) {
      clearTimeout(entry.timer);
      entry.reject(new Error(`NapCat action failed: ${reason}`));
      this.pendingActions.delete(echo);
    }
  }

  /** 客户端模式的连接 URL：accessToken 以 query 参数带上（原生 WebSocket 不能加 header）。 */
  private clientUrl(): string {
    const url = this.options.wsUrl;
    const token = this.options.accessToken ?? '';
    if (token === '') return url;
    return url + (url.includes('?') ? '&' : '?') + 'access_token=' + encodeURIComponent(token);
  }

  private connect(): void {
    if (this.closed) return;
    const ws = new WebSocket(this.clientUrl());
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.connectedValue = true;
      this.options.handlers.onConnected?.();
      void this.refreshLoginInfo();
    };
    ws.onmessage = (event) => {
      const raw = typeof event.data === 'string' ? event.data : '';
      // 客户端模式动作也走这条连接（echo 匹配），先看是不是动作响应。
      if (this.resolveActionResponse(raw)) return;
      try {
        this.handleEvent(raw);
      } catch (error) {
        console.error('[napcat] event handling failed:', error);
      }
    };
    ws.onclose = () => {
      this.connectedValue = false;
      this.options.handlers.onDisconnected?.('ws closed');
      this.failPendingActions('NapCat client connection closed');
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows; nothing else to do here.
    };
  }

  private scheduleReconnect(): void {
    if (this.closed) return;
    const base = this.options.reconnectBaseMs ?? 1000;
    const max = this.options.reconnectMaxMs ?? 30000;
    const delay = Math.min(max, base * 2 ** Math.min(this.reconnectAttempt, 5));
    this.reconnectAttempt += 1;
    setTimeout(() => this.connect(), delay);
  }

  // ── inbound events ────────────────────────────────────────────────────────

  private handleEvent(raw: string): void {
    if (!raw) return;
    let data: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (typeof parsed !== 'object' || parsed === null) return;
      data = parsed;
    } catch {
      return;
    }
    if (data.meta_event_type === 'heartbeat' || data.meta_event_type === 'lifecycle') {
      this.adoptSelfId(data);
      return;
    }
    this.adoptSelfId(data);

    const postType = String(data.post_type ?? '');
    if (postType === 'message') {
      this.handleIncomingMessage(data, false);
      return;
    }
    if (postType === 'message_sent') {
      this.handleIncomingMessage(data, true);
      return;
    }
    if (postType === 'request') {
      const requestType = String(data.request_type ?? '');
      const cacheKey = `${requestType}:${String(data.user_id ?? '')}:${String(data.flag ?? '')}`;
      this.purgeOldRequests();
      this.pendingRequestEvents.set(cacheKey, { event: data, receivedAt: Date.now() });
      if (requestType === 'friend') this.options.handlers.onFriendRequest?.(data);
      else if (requestType === 'group') this.options.handlers.onGroupRequest?.(data);
      return;
    }
    // notices (group_increase, recall, …) — reserved for later modules.
  }

  /**
   * 从事件/心跳里学习当前账号。封号换号后 NapCat 可能不重连同一条 WS，
   * 但心跳会带新的 self_id——跟着它走，换号无需重启 bot。
   */
  private adoptSelfId(data: Record<string, unknown>): void {
    const candidate = Number(data.self_id);
    if (!Number.isFinite(candidate) || candidate <= 10000) return;
    if (this.selfIdValue === candidate) return;
    // 配了账号白名单时，名单外的 self_id 不认——旧 NapCat 的心跳不能把
    // 身份翻回未配置的号。
    if (!this.isAllowedSelfId(candidate)) return;
    if (this.selfIdValue !== 0) {
      console.info(`[napcat] 登录账号切换：${this.selfIdValue} → ${candidate}`);
    }
    this.selfIdValue = candidate;
  }

  /**
   * 账号白名单：allowedSelfIds（配置里明确允许接管连接的号）。为空 =
   * 不限制（谁先连上谁是现役，兼容没配白名单的部署）。
   */
  private allowedSelfIdList(): number[] {
    const ids: number[] = [];
    for (const id of this.options.allowedSelfIds ?? []) {
      if (Number.isFinite(id) && id > 10000 && !ids.includes(id)) ids.push(id);
    }
    return ids;
  }

  /** 白名单非空时只认名单里的号；名单为空一律放行。 */
  private isAllowedSelfId(id: number | null): boolean {
    if (id === null) return false;
    const allow = this.allowedSelfIdList();
    return allow.length === 0 || allow.includes(id);
  }

  /** 学会当前账号：selfId 跟着现役连接走，昵称用于展示/历史渲染。 */
  private applyIdentity(userId: number, nickname: string): void {
    if (this.selfIdValue !== 0 && this.selfIdValue !== userId) {
      console.info(`[napcat] 登录账号切换：${this.selfIdValue} → ${userId}`);
    }
    this.selfIdValue = userId;
    if (nickname !== '') this.selfNicknameValue = nickname;
  }

  /**
   * 反向连接接管：先问这条连接自报哪个号，再决定是否顶掉现役连接。
   * 配了账号白名单（allowedSelfIds）时，自报不在名单里的连接一律拒绝；
   * 换号后旧 NapCat 若还在跑，会每 30 秒重连一次反向 WS——不设防会把
   * 现役账号挤掉，两个号来回切换，bot 回复从错误的号发出必然失败。
   */
  private async adoptReverseConnection(conn: WsServerConnection): Promise<void> {
    // 没有现役连接：立即接管（事件不能被身份探测拖住），身份随后补学；
    // 补学发现是白名单外的号会立刻断开它。
    if (this.reverseConn === null) {
      if (this.pendingConn?.id === conn.id) this.pendingConn = null;
      if (this.closed) {
        try { conn.close(1000); } catch { /* already gone */ }
        return;
      }
      this.reconnectAttempt = 0;
      this.reverseConn = conn;
      this.reverseConnSelfId = null;
      this.connectedValue = true;
      this.options.handlers.onConnected?.();
      void this.refreshLoginInfo(conn);
      return;
    }
    const seq = ++this.connectSeq;
    const identity = await this.probeLoginInfo(conn);
    if (seq !== this.connectSeq) {
      // 握手期间又来了一条更新的连接，这条交给后者处理。
      try { conn.close(1000); } catch { /* already gone */ }
      return;
    }
    if (this.pendingConn?.id === conn.id) this.pendingConn = null;
    if (this.closed) {
      try { conn.close(1000); } catch { /* already gone */ }
      return;
    }
    const current = this.reverseConn;
    if (identity !== null && !this.isAllowedSelfId(identity.userId)) {
      console.info(`[napcat] 拒绝未配置账号连接（${identity.userId}），白名单=${JSON.stringify(this.allowedSelfIdList())}`);
      try { conn.close(1000); } catch { /* already gone */ }
      return;
    }
    if (identity === null && current !== null && current.id !== conn.id) {
      // 新连接问不出身份（NapCat 忙/连接半死）：保留现役，别把好连接换掉。
      console.warn('[napcat] 新连接 5s 内未自报账号，保留现役连接');
      try { conn.close(1000); } catch { /* already gone */ }
      return;
    }
    if (current !== null && current.id !== conn.id) {
      try { current.close(1000); } catch { /* already gone */ }
    }
    this.reverseConn = conn;
    this.reverseConnSelfId = identity?.userId ?? null;
    this.connectedValue = true;
    if (identity !== null) this.applyIdentity(identity.userId, identity.nickname);
    this.options.handlers.onConnected?.();
    if (identity === null) void this.refreshLoginInfo(conn);
  }

  /** 在指定连接上问 get_login_info；短超时，失败返回 null（不作为看门狗失败计）。 */
  private async probeLoginInfo(conn: WsServerConnection, timeoutMs = 5000): Promise<{ userId: number; nickname: string } | null> {
    try {
      const info = (await this.postOverWs(
        'get_login_info',
        {},
        timeoutMs,
        (payload) => conn.send(payload),
        'REVERSE_WS_NOT_CONNECTED',
        false,
      )) as { user_id?: number; nickname?: string };
      const userId = Number(info.user_id);
      if (!Number.isFinite(userId) || userId <= 10000) return null;
      return { userId, nickname: String(info.nickname ?? '').trim() };
    } catch {
      return null;
    }
  }

  /**
   * 拉 get_login_info：同时校正 selfId 并学习真实昵称（替代历史里写死的人设昵称）。
   * selfId 一律跟随当前登录账号更新——封号换号后 NapCat 重新登录，不需要重启 bot
   * 就能把新号学成自己，否则新号自己的回声会被当成用户消息。
   */
  private async refreshLoginInfo(conn: WsServerConnection | null = this.reverseConn): Promise<void> {
    try {
      const info = (conn === null
        ? await this.post('get_login_info', {})
        : await this.postOverWs(
          'get_login_info',
          {},
          30000,
          (payload) => conn.send(payload),
          'REVERSE_WS_NOT_CONNECTED',
        )) as { user_id?: number; nickname?: string };
      const uid = Number(info.user_id);
      // 迟到的响应可能来自已被换掉的连接：只认现役连接的账号，别把 selfId
      // 翻到别的号。
      if (Number.isFinite(uid) && uid > 10000 && (conn === null || conn.id === this.reverseConn?.id)) {
        if (!this.isAllowedSelfId(uid)) {
          console.info(`[napcat] 现役连接自报白名单外账号（${uid}），断开`);
          if (conn !== null) {
            try { conn.close(1000); } catch { /* already gone */ }
          }
          return;
        }
        this.applyIdentity(uid, String(info.nickname ?? '').trim());
        this.reverseConnSelfId = uid;
      }
    } catch (error) {
      console.warn('[napcat] get_login_info failed:', error);
    }
  }

  private handleIncomingMessage(data: Record<string, unknown>, selfSent: boolean): void {
    const messageType = String(data.message_type ?? '');
    if (messageType !== 'group' && messageType !== 'private') return;
    const chatType = messageType as 'group' | 'private';
    // 入站私聊跟踪（排查丢事件用，LIVEAI_TRACE_INBOUND=1 开）：每条私聊事件一行，
    // 与是否触发无关。
    if (chatType === 'private' && process.env.LIVEAI_TRACE_INBOUND === '1') {
      console.info(
        `[napcat] inbound private self_sent=${selfSent} user=${String(data.user_id ?? '')}`
          + ` target=${String(data.target_id ?? '')} sub=${String(data.sub_type ?? '')}`
          + ` mid=${String(data.message_id ?? '')} text=${String(data.raw_message ?? '').slice(0, 30).replace(/\n/g, ' ')}`,
      );
    }
    // message_sent 私聊回声里 user_id 是机器人自己，真正的会话对象在 target_id——
    // 不用它的话回声去重键永远对不上 sendText 登记的 private:<对方QQ>:<内容>。
    const targetId =
      chatType === 'group'
        ? Number(data.group_id)
        : selfSent
          ? Number(data.target_id ?? data.user_id)
          : Number(data.user_id);
    const message = buildChatMessage(data, chatType, targetId);

    // B 站小程序分享卡（CQ:json，b23.tv 短链）：异步解析出 BV 号再分发，
    // 让 AI 直接拿到可喂 bili 插件的 bvid。解析失败/超时按原文放行。
    if (message.text.includes('b23.tv/')) {
      void this.dispatchWithBiliResolve(message, data, selfSent);
      return;
    }
    this.dispatchMessage(message, data, selfSent);
  }

  private async dispatchWithBiliResolve(
    message: ChatMessage,
    data: Record<string, unknown>,
    selfSent: boolean,
  ): Promise<void> {
    const short = message.text.match(/https?:\/\/b23\.tv\/[A-Za-z0-9]+/)?.[0];
    if (short) {
      const bvid = await resolveB23(short);
      if (bvid) message.text = `${message.text}\n（${bvid}）`;
    }
    this.dispatchMessage(message, data, selfSent);
  }

  private dispatchMessage(message: ChatMessage, data: Record<string, unknown>, selfSent: boolean): void {
    // 入口已保证只有 group/private，ChatMessage.chatType 类型更宽，这里收窄。
    const chatType = message.chatType as 'group' | 'private';
    const targetId = Number(message.chatId);

    // QQ 数据线（我的电脑/我的手机）：sender 永远是自己（手机端同账号发的也算
    // self-sent），所以必须在下面的 self 丢弃逻辑之前单独分流。回声（机器人自己
    // 经 sendDatalineText 发出的）照旧走 pending/id 双层去重。
    if (String(data.sub_type ?? '') === 'dataline') {
      if (this.isPendingSelfSent(chatType, targetId, message) || this.isRecentSelfSentId(message.messageId)) {
        return;
      }
      this.options.handlers.onDatalineMessage?.(message);
      return;
    }

    if (selfSent || Number(data.user_id) === this.selfId) {
      if (this.isPendingSelfSent(chatType, targetId, message) || this.isRecentSelfSentId(message.messageId)) {
        return; // our own send echo — swallow
      }
      this.options.handlers.onSelfMessage?.(message);
      return;
    }
    if (chatType === 'group') this.options.handlers.onGroupMessage?.(message);
    else this.options.handlers.onPrivateMessage?.(message);
  }

  private isPendingSelfSent(chatType: 'group' | 'private', targetId: number, message: ChatMessage): boolean {
    const key = pendingKey(chatType, targetId, message.rawMessage);
    const hit = this.pendingSelfSent.get(key);
    if (hit && hit.expiresAt > Date.now()) {
      this.pendingSelfSent.delete(key);
      if (message.messageId !== null && message.messageId !== undefined && message.messageId !== '') {
        this.rememberSelfSent(message.messageId);
      }
      return true;
    }
    this.pendingSelfSent.delete(key);
    return false;
  }

  private isRecentSelfSentId(messageId: number | string | null): boolean {
    if (messageId === null || messageId === undefined || messageId === '') return false;
    const key = String(messageId);
    const seenAt = this.recentSelfSentIds.get(key);
    if (seenAt === undefined) return false;
    this.purgeOldSelfSent();
    return true;
  }

  private rememberSelfSent(messageId: number | string): void {
    this.recentSelfSentIds.set(String(messageId), Date.now());
    this.purgeOldSelfSent();
  }

  private purgeOldSelfSent(): void {
    const cutoff = Date.now() - 10 * 60 * 1000;
    for (const [key, seenAt] of this.recentSelfSentIds) {
      if (seenAt < cutoff) this.recentSelfSentIds.delete(key);
    }
    for (const [key, entry] of this.pendingSelfSent) {
      if (entry.expiresAt < Date.now()) this.pendingSelfSent.delete(key);
    }
  }

  // ── outbound actions ──────────────────────────────────────────────────────

  private async post(action: string, params: Record<string, unknown>, timeoutMs = 30000): Promise<Record<string, unknown>> {
    if (this.isReverseMode()) return this.postOverReverseWs(action, params, timeoutMs);
    // 客户端模式：WS 活着走双工连接（OneBot 正向 WS 动作经 echo 匹配），
    // WS 断开期间退走 HTTP（配了 httpUrl 时）。
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN) {
      return this.postOverClientWs(action, params, timeoutMs);
    }
    if (this.options.httpUrl !== '') return this.postOverHttp(action, params, timeoutMs);
    const error = new Error(`NapCat ${action} failed: failed/0 WS_NOT_CONNECTED`);
    this.options.watchdog?.recordFailure(error);
    return Promise.reject(error);
  }

  /** 反向模式：动作 JSON 走双工连接，靠 echo 匹配响应。 */
  private postOverReverseWs(action: string, params: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown>> {
    const conn = this.reverseConn;
    return this.postOverWs(
      action,
      params,
      timeoutMs,
      conn === null ? null : (payload) => conn.send(payload),
      'REVERSE_WS_NOT_CONNECTED',
    );
  }

  /** 客户端模式：动作经正向 WS 连接发送（调用方已确认 readyState=OPEN）。 */
  private postOverClientWs(action: string, params: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown>> {
    const ws = this.ws;
    return this.postOverWs(
      action,
      params,
      timeoutMs,
      ws === null ? null : (payload) => ws.send(payload),
      'WS_NOT_CONNECTED',
    );
  }

  /** WS 动作共用实现：echo 匹配 + 超时 + 看门狗回报。sender=null 表示连接不在。 */
  private postOverWs(
    action: string,
    params: Record<string, unknown>,
    timeoutMs: number,
    sender: ((payload: string) => void) | null,
    offlineError: string,
    report = true,
  ): Promise<Record<string, unknown>> {
    if (sender === null) {
      const error = new Error(`NapCat ${action} failed: failed/0 ${offlineError}`);
      if (report) this.options.watchdog?.recordFailure(error);
      return Promise.reject(error);
    }
    const echo = `act${++this.actionSeq}`;
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingActions.delete(echo);
        const error = new Error(`NapCat ${action} failed: timeout after ${timeoutMs}ms`);
        if (report) this.options.watchdog?.recordFailure(error);
        reject(error);
      }, timeoutMs);
      this.pendingActions.set(echo, {
        resolve: (value) => {
          if (report) this.options.watchdog?.recordSuccess();
          resolve(value);
        },
        reject: (error) => {
          if (report) this.options.watchdog?.recordFailure(error);
          reject(error);
        },
        timer,
      });
      try {
        sender(JSON.stringify({ action, params, echo }));
      } catch (error) {
        clearTimeout(timer);
        this.pendingActions.delete(echo);
        reject(new Error(`NapCat ${action} failed: ${error instanceof Error ? error.message : String(error)}`));
      }
    });
  }

  private async postOverHttp(action: string, params: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers: Record<string, string> = { 'content-type': 'application/json' };
      if (this.options.accessToken) headers.authorization = `Bearer ${this.options.accessToken}`;
      const response = await fetch(`${this.options.httpUrl}/${action}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(params),
        signal: controller.signal,
      });
      const payload = (await response.json()) as { status: string; retcode?: number | null; data?: unknown; message?: string };
      const accepted = payload.status === 'ok' || payload.status === 'async';
      const retcode = payload.retcode;
      if (!accepted || (retcode !== undefined && retcode !== null && retcode !== 0)) {
        const error = new Error(`NapCat ${action} failed: ${payload.status}/${payload.retcode} ${payload.message ?? ''}`);
        this.options.watchdog?.recordFailure(error);
        throw error;
      }
      this.options.watchdog?.recordSuccess();
      return (payload.data as Record<string, unknown>) ?? {};
    } finally {
      clearTimeout(timer);
    }
  }

  /** Public OneBot action entry for dedicated QQ tools and the guarded napcat_call tool. */
  async callApi(action: string, params: Record<string, unknown>, timeoutMs = 30000): Promise<Record<string, unknown>> {
    return this.post(action, params, timeoutMs);
  }

  async sendText(chatType: 'group' | 'private', targetId: number, text: string): Promise<Record<string, unknown>> {
    const message = [{ type: 'text', data: { text } }];
    this.markPendingSelfSent(chatType, targetId, text);
    const action = chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      chatType === 'group'
        ? { group_id: targetId, message }
        : { user_id: targetId, message };
    const result = await this.post(action, params);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    return result;
  }

  /**
   * QQ 数据线（我的电脑/我的手机）：发到本机的「我的设备」会话，主人手机在
   * 「我的电脑」里收到。走桥侧自定义动作 send_dataline_msg。回声会以
   * sub_type=dataline 的 message_sent 回来，去重键与普通私聊一致
   * （private + 自己的 selfId + 内容），这里同样先登记 pending。
   */
  async sendDatalineText(text: string): Promise<Record<string, unknown>> {
    this.markPendingSelfSent('private', Number(this.selfId), text);
    const result = await this.post('send_dataline_msg', {
      message: [{ type: 'text', data: { text } }],
    });
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    return result;
  }

  async sendReplyText(message: ChatMessage, content: string): Promise<Record<string, unknown>> {
    const chatType = message.chatType as 'group' | 'private';
    const targetId = Number(message.chatId);
    // Idempotency: skip if we sent this exact message recently (crash recovery / retry)
    if (this.idempotency.isDuplicate(chatType, targetId, 'text', content)) {
      return { message_id: -1 }; // Sentinel: suppressed duplicate
    }
    const segments: Record<string, unknown>[] = [];
    if (message.messageId !== null && message.messageId !== undefined) {
      segments.push({ type: 'reply', data: { id: String(message.messageId) } });
    }
    segments.push({ type: 'text', data: { text: content } });
    this.markPendingSelfSent(chatType, targetId, content);
    const action = message.chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      message.chatType === 'group'
        ? { group_id: targetId, message: segments }
        : { user_id: targetId, message: segments };
    const result = await this.post(action, params);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    this.idempotency.record(chatType, targetId, 'text', content);
    return result;
  }

  async sendImage(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
    if (this.idempotency.isDuplicate(chatType, targetId, 'image', file)) {
      return { message_id: -1 };
    }
    const message = [{ type: 'image', data: { file, subType: 0 } }];
    const action = chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      chatType === 'group'
        ? { group_id: targetId, message }
        : { user_id: targetId, message };
    const result = await this.post(action, params);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    this.idempotency.record(chatType, targetId, 'image', file);
    return result;
  }

  /**
   * 发收藏表情（OneBot image 段 subType=1）：QQ 里渲染成表情气泡而不是
   * 大图。file 传表情的 p.qpic.cn URL（fetch_custom_face 拉到的收藏表情）。
   */
  async sendSticker(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
    if (this.idempotency.isDuplicate(chatType, targetId, 'sticker', file)) {
      return { message_id: -1 };
    }
    const message = [{ type: 'image', data: { file, subType: 1 } }];
    const action = chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      chatType === 'group'
        ? { group_id: targetId, message }
        : { user_id: targetId, message };
    const result = await this.post(action, params);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    this.idempotency.record(chatType, targetId, 'sticker', file);
    return result;
  }

  /**
   * 拉取本账号的收藏表情清单（fetch_custom_face）：返回 p.qpic.cn URL 数组。
   * 表情包索引刷新用——URL 里内嵌 32 位十六进制 MD5，作增量比对键。
   */
  async fetchCustomFace(): Promise<string[]> {
    const data: unknown = await this.post('fetch_custom_face', {});
    // 实测本机 NapCat 返回 data: ["https://p.qpic.cn/…", …]（纯字符串数组）；
    // 兼容其他版本的 [{ url }] 与 { data: [...] } 包裹形态
    const list = Array.isArray(data)
      ? data
      : Array.isArray((data as { data?: unknown } | null)?.data)
        ? ((data as { data: unknown[] }).data)
        : [];
    const urls: string[] = [];
    for (const item of list) {
      const url = typeof item === 'string' ? item : (item as { url?: unknown } | null)?.url;
      if (typeof url === 'string' && url !== '') urls.push(url);
    }
    return urls;
  }

  /**
   * 发语音（OneBot record 段）。file 传本地绝对路径（file://…）：NapCat 侧
   * 用 ffmpeg 转码成 QQ 语音格式（silk），wav/mp3 都收。
   */
  async sendRecord(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
    const message = [{ type: 'record', data: { file } }];
    const action = chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      chatType === 'group'
        ? { group_id: targetId, message }
        : { user_id: targetId, message };
    const result = await this.post(action, params, 60_000);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    return result;
  }

  /**
   * 发视频（OneBot video 段）。file 传本地绝对路径（file://…）：NapCat 侧
   * 用 ffmpeg 转码成 QQ 视频格式。B 站 CDN 直链要 Referer，由 send_video 工具
   * 先下载到本地再走这里，不把裸 URL 丢给 NapCat。
   */
  async sendVideo(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
    if (this.idempotency.isDuplicate(chatType, targetId, 'video', file)) {
      return { message_id: -1 };
    }
    const message = [{ type: 'video', data: { file } }];
    const action = chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      chatType === 'group'
        ? { group_id: targetId, message }
        : { user_id: targetId, message };
    const result = await this.post(action, params, 180_000);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    this.idempotency.record(chatType, targetId, 'video', file);
    return result;
  }

  async recallMessage(messageId: number | string): Promise<Record<string, unknown>> {
    return this.post('delete_msg', { message_id: messageId });
  }

  async getGroupInfo(groupId: number): Promise<{ group_name?: string; member_count?: number; max_member_count?: number }> {
    try {
      return (await this.post('get_group_info', { group_id: groupId })) as {
        group_name?: string;
        member_count?: number;
        max_member_count?: number;
      };
    } catch {
      return {};
    }
  }

  /** OneBot get_group_member_list: full member list with role fields. */
  async getGroupMemberList(groupId: number): Promise<Record<string, unknown>[]> {
    const result = await this.post('get_group_member_list', { group_id: groupId }, 60000);
    return Array.isArray(result) ? result : [];
  }

  /** OneBot set_group_ban: duration in seconds, 0 lifts the mute. */
  async setGroupBan(groupId: number, userId: number, durationSeconds: number): Promise<void> {
    await this.post('set_group_ban', { group_id: groupId, user_id: userId, duration: durationSeconds });
  }

  async getStrangerInfo(userId: number): Promise<{ nickname?: string }> {
    try {
      return (await this.post('get_stranger_info', { user_id: userId })) as { nickname?: string };
    } catch {
      return {};
    }
  }

  /** OneBot get_image: resolve an inbound image file id through NapCat itself. */
  async getImage(file: string): Promise<{ file?: string; path?: string; url?: string; file_size?: number }> {
    const result = await this.post('get_image', { file }, 30000);
    return result as { file?: string; path?: string; url?: string; file_size?: number };
  }

  /** OneBot get_file: resolve a received file_id to a downloadable URL. */
  async getFile(fileId: string): Promise<{ url?: string; file_name?: string; file_size?: number }> {
    const result = await this.post('get_file', { file_id: fileId }, 30000);
    return result as { url?: string; file_name?: string; file_size?: number };
  }

  cachedRequests(requestType: 'friend' | 'group'): Record<string, unknown>[] {
    this.purgeOldRequests();
    return [...this.pendingRequestEvents.values()]
      .map((entry) => entry.event)
      .filter((event) => String(event.request_type) === requestType);
  }

  consumeRequest(cacheKey: string): Record<string, unknown> | undefined {
    const entry = this.pendingRequestEvents.get(cacheKey);
    this.pendingRequestEvents.delete(cacheKey);
    return entry?.event;
  }

  /** Friend/group requests are actionable for 24h, then forgotten. */
  private purgeOldRequests(): void {
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    for (const [key, entry] of this.pendingRequestEvents) {
      if (entry.receivedAt < cutoff) this.pendingRequestEvents.delete(key);
    }
  }

  private markPendingSelfSent(chatType: 'group' | 'private', targetId: number, rawMessage: string): void {
    const key = pendingKey(chatType, targetId, rawMessage);
    this.pendingSelfSent.set(key, { key, expiresAt: Date.now() + 30000 });
  }
}

function pendingKey(chatType: 'group' | 'private', targetId: number, rawMessage: string): string {
  return `${chatType}:${targetId}:${rawMessage}`;
}

export function buildChatMessage(
  data: Record<string, unknown>,
  chatType: 'group' | 'private',
  targetId: number,
): ChatMessage {
  const sender = (data.sender as ChatMessage['sender']) ?? {};
  const selfIds = new Set([String(data.self_id ?? ''), ''].filter(Boolean));
  return {
    chatType,
    chatId: targetId,
    userId: Number(data.user_id ?? sender.user_id ?? 0),
    text: extractText(data),
    rawMessage: String(data.raw_message ?? ''),
    sender,
    messageId: (data.message_id as number | string | null) ?? null,
    mentionsSelf: messageMentionsSelf(data, selfIds),
    timestamp: Number(data.time ?? Date.now() / 1000),
    rawData: data,
  };
}

function extractText(data: Record<string, unknown>): string {
  const rawSegments = data.message;
  if (Array.isArray(rawSegments)) {
    return rawSegments
      .map((segment) => {
        if (typeof segment === 'string') return segment;
        if (typeof segment !== 'object' || segment === null) return '';
        const entry = segment as { type?: string; data?: { text?: string; data?: string } };
        if (entry.type === 'text') return String(entry.data?.text ?? '');
        if (entry.type === 'json') return describeJsonCard(String(entry.data?.data ?? ''));
        return '';
      })
      .join('');
  }
  const rawText = String(data.raw_message ?? '');
  if (rawText.includes('[CQ:json,')) {
    return rawText.replace(/\[CQ:json,data=(.*)\]/g, (_m, d: string) => describeJsonCard(d)).trim();
  }
  return rawText;
}

/**
 * QQ 小程序/分享卡片（json 段）转成 AI 看得懂的一行文字。
 * B 站分享（appid 1109937557 或链接含 b23.tv/bilibili.com）带标题+链接；
 * 其他卡片给 prompt 摘要，解析不了就空着（不挡消息）。
 */
function describeJsonCard(raw: string): string {
  if (!raw) return '';
  let card: Record<string, unknown> | null = null;
  try {
    card = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    try {
      // CQ 码字符串形态会把 JSON 实体转义（&#44; 等），兜底解一层再试
      const unescaped = raw.replace(/&#(\d+);/g, (_, code: string) => String.fromCharCode(Number(code)));
      card = JSON.parse(unescaped) as Record<string, unknown>;
    } catch {
      return '';
    }
  }
  if (card === null || typeof card !== 'object') return '';
  const meta = (card.meta as Record<string, unknown> | undefined) ?? {};
  const detail = (meta.detail_1 as Record<string, unknown> | undefined) ?? {};
  const url = String(detail.qqdocurl ?? detail.jumpUrl ?? '');
  const title = String(detail.desc ?? detail.title ?? '').trim();
  if (/b23\.tv|bilibili\.com/.test(url) || String(detail.appid ?? '') === '1109937557') {
    return `[分享B站视频]《${title || '未知标题'}》 ${url}`.trim();
  }
  const prompt = String(card.prompt ?? '').replace(/^\[.+?\]/, '').trim();
  const appName = String(detail.title ?? '').trim();
  return `[分享卡片${appName ? `·${appName}` : ''}]${prompt || title}`.trim() || '';
}

/** b23.tv 短链 HEAD 跟随一次重定向拿 BV 号；失败/超时（5s）返回 null。 */
async function resolveB23(shortUrl: string): Promise<string | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const resp = await fetch(shortUrl, { method: 'HEAD', redirect: 'manual', signal: controller.signal });
    const location = resp.headers.get('location') ?? '';
    return location.match(/BV[0-9A-Za-z]+/)?.[0] ?? null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function messageMentionsSelf(data: Record<string, unknown>, selfIds: Set<string>): boolean {
  const raw = String(data.raw_message ?? '');
  for (const match of raw.matchAll(/\[CQ:at,qq=(\d+)(?:,[^\]]*)?\]/g)) {
    if (selfIds.has(match[1]!)) return true;
  }
  const segments = data.message;
  if (Array.isArray(segments)) {
    return segments.some((segment) => {
      if (typeof segment !== 'object' || segment === null) return false;
      const entry = segment as { type?: string; data?: { qq?: string } };
      return entry.type === 'at' && selfIds.has(String(entry.data?.qq ?? ''));
    });
  }
  return false;
}

/** 127.0.0.0/8 与 ::1 / ::ffff:127.x —— 反向 WS 未配 token 时只信本机。 */
function isLoopbackAddress(address: string | null): boolean {
  if (address === null) return false;
  const value = address.toLowerCase();
  return value === '::1' || value.startsWith('127.') || value.startsWith('::ffff:127.');
}

/** 长度无关的恒定时间比较（先哈希到定长，避免提前返回泄露长度/前缀）。 */
function constantTimeEqual(presented: string, expected: string): boolean {
  const digest = (value: string): Buffer => createHash('sha256').update(value, 'utf8').digest();
  return timingSafeEqual(digest(presented), digest(expected));
}
