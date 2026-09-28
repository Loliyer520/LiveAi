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

export interface NapcatHandlers {
  onGroupMessage?: (message: ChatMessage) => void;
  onPrivateMessage?: (message: ChatMessage) => void;
  onSelfMessage?: (message: ChatMessage) => void;
  onFriendRequest?: (event: Record<string, unknown>) => void;
  onGroupRequest?: (event: Record<string, unknown>) => void;
  onConnected?: () => void;
  onDisconnected?: (reason: string) => void;
}

export interface NapcatOptions {
  wsUrl: string;
  httpUrl: string;
  selfId: number;
  accessToken?: string;
  handlers: NapcatHandlers;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
}

interface PendingSelfSent {
  key: string;
  expiresAt: number;
}

export class NapcatBot {
  private selfIdValue: number;
  private ws: WebSocket | null = null;
  private closed = false;
  private reconnectAttempt = 0;
  private pendingSelfSent = new Map<string, PendingSelfSent>();
  private recentSelfSentIds = new Map<string, number>();
  private readonly pendingRequestEvents = new Map<string, Record<string, unknown>>();

  constructor(private readonly options: NapcatOptions) {
    this.selfIdValue = options.selfId;
  }

  /** Bot QQ; if unconfigured, adopted from the first event that carries self_id. */
  get selfId(): number {
    return this.selfIdValue;
  }

  start(): void {
    this.closed = false;
    this.connect();
  }

  stop(): void {
    this.closed = true;
    this.ws?.close(1000, 'shutdown');
    this.ws = null;
  }

  private connect(): void {
    if (this.closed) return;
    const ws = new WebSocket(this.options.wsUrl);
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.options.handlers.onConnected?.();
    };
    ws.onmessage = (event) => {
      try {
        this.handleEvent(typeof event.data === 'string' ? event.data : '');
      } catch (error) {
        console.error('[napcat] event handling failed:', error);
      }
    };
    ws.onclose = () => {
      this.options.handlers.onDisconnected?.('ws closed');
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

  private handleIncomingMessage(data: Record<string, unknown>, selfSent: boolean): void {
    const messageType = String(data.message_type ?? '');
    if (messageType !== 'group' && messageType !== 'private') return;
    const chatType = messageType as 'group' | 'private';
    const targetId = chatType === 'group' ? Number(data.group_id) : Number(data.user_id);
    const message = buildChatMessage(data, chatType, targetId);

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

  // ── outbound actions (HTTP) ───────────────────────────────────────────────

  private async post(action: string, params: Record<string, unknown>, timeoutMs = 30000): Promise<Record<string, unknown>> {
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
        throw new Error(`NapCat ${action} failed: ${payload.status}/${payload.retcode} ${payload.message ?? ''}`);
      }
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

  async sendReplyText(message: ChatMessage, content: string): Promise<Record<string, unknown>> {
    const segments: Record<string, unknown>[] = [];
    if (message.messageId !== null && message.messageId !== undefined) {
      segments.push({ type: 'reply', data: { id: String(message.messageId) } });
    }
    segments.push({ type: 'text', data: { text: content } });
    this.markPendingSelfSent(message.chatType, Number(message.chatId), content);
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
    const message = [{ type: 'image', data: { file } }];
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
