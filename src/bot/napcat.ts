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

import type { ChatMessage } from '../chat/types.js';
import type { BridgeWatchdog } from './bridge-watchdog.js';
import { WsServer, type WsServerConnection } from './ws-server.js';

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
  private readonly pendingRequestEvents = new Map<string, Record<string, unknown>>();
  // 反向 WS 模式：NapCat 主动连进来的那条连接 + 在途动作的 echo 匹配表。
  private wsServer: WsServer | null = null;
  private reverseConn: WsServerConnection | null = null;
  private actionSeq = 0;
  private readonly pendingActions = new Map<
    string,
    { resolve: (value: Record<string, unknown>) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();

  constructor(private readonly options: NapcatOptions) {
    this.selfIdValue = options.selfId;
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
  }

  private startReverseServer(): void {
    const host = this.options.reverseWsHost ?? '0.0.0.0';
    const port = this.options.reverseWsPort as number;
    const server = new WsServer({
      authorize: (token) => this.options.accessToken === undefined || this.options.accessToken === '' || token === this.options.accessToken,
      onConnection: (conn) => {
        // 新连接优先：旧连接可能已半死（NapCat 重连场景），挤掉防僵尸。
        if (this.reverseConn !== null && this.reverseConn.id !== conn.id) {
          try { this.reverseConn.close(1000); } catch { /* already gone */ }
        }
        this.reverseConn = conn;
        this.reconnectAttempt = 0;
        this.connectedValue = true;
        this.options.handlers.onConnected?.();
        void this.refreshLoginInfo();
      },
      onText: (conn, data) => {
        // 先看是不是动作响应（带 echo），不是才当事件分发。
        if (this.resolveActionResponse(data)) return;
        try {
          this.handleEvent(data);
        } catch (error) {
          console.error('[napcat] event handling failed:', error);
        }
      },
      onClose: (conn) => {
        if (this.reverseConn?.id !== conn.id) return;
        this.reverseConn = null;
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
    if (!accepted || data.retcode !== 0) {
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
      this.pendingRequestEvents.set(cacheKey, data);
      if (requestType === 'friend') this.options.handlers.onFriendRequest?.(data);
      else if (requestType === 'group') this.options.handlers.onGroupRequest?.(data);
      return;
    }
    // notices (group_increase, recall, …) — reserved for later modules.
  }

  private adoptSelfId(data: Record<string, unknown>): void {
    if (this.selfIdValue !== 0) return;
    const candidate = Number(data.self_id);
    if (Number.isFinite(candidate) && candidate > 10000) this.selfIdValue = candidate;
  }

  /** 拉 get_login_info：同时校正 selfId 并学习真实昵称（替代历史里写死的人设昵称）。 */
  private async refreshLoginInfo(): Promise<void> {
    try {
      const info = (await this.post('get_login_info', {})) as { user_id?: number; nickname?: string };
      const uid = Number(info.user_id);
      if (Number.isFinite(uid) && uid > 10000 && this.selfIdValue === 0) this.selfIdValue = uid;
      const nick = String(info.nickname ?? '').trim();
      if (nick) this.selfNicknameValue = nick;
    } catch (error) {
      console.warn('[napcat] get_login_info failed:', error);
    }
  }

  private handleIncomingMessage(data: Record<string, unknown>, selfSent: boolean): void {
    const messageType = String(data.message_type ?? '');
    if (messageType !== 'group' && messageType !== 'private') return;
    const chatType = messageType as 'group' | 'private';
    // message_sent 私聊回声里 user_id 是机器人自己，真正的会话对象在 target_id——
    // 不用它的话回声去重键永远对不上 sendText 登记的 private:<对方QQ>:<内容>。
    const targetId =
      chatType === 'group'
        ? Number(data.group_id)
        : selfSent
          ? Number(data.target_id ?? data.user_id)
          : Number(data.user_id);
    const message = buildChatMessage(data, chatType, targetId);

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
  ): Promise<Record<string, unknown>> {
    if (sender === null) {
      const error = new Error(`NapCat ${action} failed: failed/0 ${offlineError}`);
      this.options.watchdog?.recordFailure(error);
      return Promise.reject(error);
    }
    const echo = `act${++this.actionSeq}`;
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingActions.delete(echo);
        const error = new Error(`NapCat ${action} failed: timeout after ${timeoutMs}ms`);
        this.options.watchdog?.recordFailure(error);
        reject(error);
      }, timeoutMs);
      this.pendingActions.set(echo, {
        resolve: (value) => {
          this.options.watchdog?.recordSuccess();
          resolve(value);
        },
        reject: (error) => {
          this.options.watchdog?.recordFailure(error);
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
      const payload = (await response.json()) as { status: string; retcode: number; data?: unknown; message?: string };
      const accepted = payload.status === 'ok' || payload.status === 'async';
      if (!accepted || payload.retcode !== 0) {
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
    const segments: Record<string, unknown>[] = [];
    if (message.messageId !== null && message.messageId !== undefined) {
      segments.push({ type: 'reply', data: { id: String(message.messageId) } });
    }
    segments.push({ type: 'text', data: { text: content } });
    this.markPendingSelfSent(message.chatType as 'group' | 'private', Number(message.chatId), content);
    const action = message.chatType === 'group' ? 'send_group_msg' : 'send_private_msg';
    const params: Record<string, unknown> =
      message.chatType === 'group'
        ? { group_id: Number(message.chatId), message: segments }
        : { user_id: Number(message.chatId), message: segments };
    const result = await this.post(action, params);
    const messageId = (result as { message_id?: number | string }).message_id;
    if (messageId !== undefined && messageId !== null && messageId !== -1) {
      this.rememberSelfSent(messageId);
    }
    return result;
  }

  async sendImage(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
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
    return result;
  }

  /**
   * 发收藏表情（OneBot image 段 subType=1）：QQ 里渲染成表情气泡而不是
   * 大图。file 传表情的 p.qpic.cn URL（fetch_custom_face 拉到的收藏表情）。
   */
  async sendSticker(chatType: 'group' | 'private', targetId: number, file: string): Promise<Record<string, unknown>> {
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
    return result;
  }

  async recallMessage(messageId: number | string): Promise<Record<string, unknown>> {
    return this.post('delete_msg', { message_id: messageId });
  }

  async getGroupInfo(groupId: number): Promise<{ group_name?: string }> {
    try {
      return (await this.post('get_group_info', { group_id: groupId })) as { group_name?: string };
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

  /** OneBot get_file: resolve a received file_id to a downloadable URL. */
  async getFile(fileId: string): Promise<{ url?: string; file_name?: string; file_size?: number }> {
    const result = await this.post('get_file', { file_id: fileId }, 30000);
    return result as { url?: string; file_name?: string; file_size?: number };
  }

  cachedRequests(requestType: 'friend' | 'group'): Record<string, unknown>[] {
    return [...this.pendingRequestEvents.values()].filter(
      (event) => String(event.request_type) === requestType,
    );
  }

  consumeRequest(cacheKey: string): Record<string, unknown> | undefined {
    const event = this.pendingRequestEvents.get(cacheKey);
    this.pendingRequestEvents.delete(cacheKey);
    return event;
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
        const entry = segment as { type?: string; data?: { text?: string } };
        return entry.type === 'text' ? String(entry.data?.text ?? '') : '';
      })
      .join('');
  }
  return String(data.raw_message ?? '');
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
