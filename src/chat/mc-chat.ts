import { randomUUID, createHash } from 'node:crypto';
import type { ChatMessage } from './types.js';

export interface McChatLine { username: string; text: string; whisper: boolean }

/** Only player speech is input; joins, announcements and tool echoes are not. */
export function parseMcChat(message: string): McChatLine[] {
  return message.split('\n').flatMap((line) => {
    const match = line.match(/^\[\d{2}:\d{2}:\d{2}\] (chat|whisper): <([A-Za-z0-9_]{1,16})> (.+)$/);
    if (!match) return [];
    const whisper = match[1] === 'whisper';
    return [{ username: match[2]!, text: match[3]!.replace(whisper ? /^私聊:\s*/ : /^$/, '').trim(), whisper }];
  });
}

/** Dynamic probability; explicit addressing is handled before this gate. */
export function mcChatTriggerRate(message: ChatMessage, baseRate: number): number {
  if (baseRate <= 0) return 0;
  const raw = message.rawData;
  const solo = raw.mc_solo === true;
  const followup = raw.mc_followup === true;
  if (followup) return 0.9;
  if (solo && /[?？]|^(在吗|你好|嗨|hello|hi|hey)\b|^(在吗|你好|嗨)[！!。\s]*$/i.test(message.text)) return 1;
  if (solo) return Math.min(0.8, Math.max(0.5, baseRate));
  const activity = Math.max(1, Number(raw.mc_recent_count) || 1);
  const question = /[?？]|怎么|如何|谁能|有人/.test(message.text);
  return Math.min(0.5, baseRate * (question ? 4 : 1)) / Math.max(1, activity / 10);
}

/** Poll existing sidecar API without restarting or modifying the game client. */
export class McChatBridge {
  private timer: ReturnType<typeof setInterval> | null = null;
  private busy = false;
  private stopped = false;
  private cursor: number | null = null;
  private connection = '';
  private readonly epoch = randomUUID();
  private recent: number[] = [];
  private lastSpeaker = '';
  private lastReplyAt = 0;
  private lastErrorLogAt = 0;

  constructor(private readonly url: string, private readonly token: string,
    private readonly deliver: (message: ChatMessage) => void,
    private readonly request: typeof fetch = fetch) {}

  private async get(path: string): Promise<{ ok?: boolean; message?: string; next?: number }> {
    const response = await this.request(`${this.url}${path}`, {
      headers: this.token ? { authorization: `Bearer ${this.token}` } : {},
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json() as { ok?: boolean; message?: string; next?: number };
    if (body.ok !== true) throw new Error('sidecar rejected request');
    return body;
  }

  async pollOnce(): Promise<void> {
    if (this.busy || this.stopped) return;
    this.busy = true;
    try {
      const status = await this.get('/status');
      const online = status.message?.match(/^在线：([A-Za-z0-9_]{1,16}) @ ([^\n]+)$/m);
      if (!online) { this.connection = ''; this.cursor = null; return; }
      const connection = online[0];
      const reset = connection !== this.connection;
      const body = await this.get(`/events?limit=100${this.cursor === null || reset ? '' : `&since=${this.cursor}`}`);
      if (this.stopped) return;
      const next = body.next;
      if (!Number.isSafeInteger(next) || next! < 0) throw new Error('invalid event cursor');
      // First attachment / changed server / sidecar restart: never replay old speech.
      if (reset || this.cursor === null || next! < this.cursor) {
        this.connection = connection; this.cursor = next!;
        this.recent = []; this.lastSpeaker = ''; this.lastReplyAt = 0;
        console.info(`[mc-chat] attached username=${online[1]} cursor=${next}; retained events skipped`);
        return;
      }
      if (next === this.cursor) return;
      const previous = this.cursor;
      this.cursor = next!;
      const names = status.message?.match(/^玩家(?:列表|在线)（\d+）[：:]\s*(.*)$/m)?.[1]
        ?.split(/[,，]/).map((s) => s.trim()).filter(Boolean) ?? [];
      const username = online[1]!;
      const now = Date.now();
      for (const [index, line] of parseMcChat(body.message ?? '').entries()) {
        if (line.username.toLowerCase() === username.toLowerCase()) { this.lastReplyAt = now; continue; }
        if (!line.text) continue;
        this.recent = this.recent.filter((t) => now - t < 60_000); this.recent.push(now);
        const followup = line.username === this.lastSpeaker && now - this.lastReplyAt < 60_000;
        this.lastSpeaker = line.username;
        const userId = -1 - (createHash('sha256').update(line.username.toLowerCase()).digest().readUInt32BE(0) & 0x3fffffff);
        const mentions = line.whisper || new RegExp(`(^|[^a-z0-9_])${username}([^a-z0-9_]|$)`, 'i').test(line.text)
          || /冰糖|深活|deeplive/i.test(line.text);
        this.deliver({
          chatType: 'domain', chatId: 'mc', userId, text: line.text, rawMessage: line.text,
          sender: { nickname: `Minecraft:${line.username}` },
          messageId: `mc:${this.epoch}:${previous}:${next}:${index}`,
          mentionsSelf: mentions, timestamp: now / 1000,
          rawData: { source: 'minecraft_chat', mc_username: line.username, mc_connection: connection,
            mc_whisper: line.whisper, mc_solo: names.length === 2 && names.includes(line.username),
            mc_followup: followup, mc_recent_count: this.recent.length },
        });
        console.info(`[mc-chat] received player=${line.username} direct=${mentions} recent=${this.recent.length}`);
      }
    } catch (error) {
      if (!this.stopped && Date.now() - this.lastErrorLogAt >= 30_000) {
        this.lastErrorLogAt = Date.now();
        console.warn(`[mc-chat] poll failed: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally { this.busy = false; }
  }

  start(): void {
    if (this.timer) return;
    this.stopped = false;
    void this.pollOnce();
    this.timer = setInterval(() => { void this.pollOnce(); }, 1_000);
    this.timer.unref?.();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
