/**
 * 小说阅读指令机（不走模型）：指定会话里小源的纯指令消息由这里直接应答——
 * 搜书/选书/目录/看章/下一章，数据走本机 fq-novel 服务（127.0.0.1:7640），
 * AI 完全不参与。指令以外的消息照常进 AI 管线。
 *
 * 指令集（整句匹配，避免劫持正常聊天）：
 *   读书            → 帮助
 *   搜书 <关键词>    → 搜索，列前 5 本
 *   选 <序号>        → 选中搜索结果第 N 本（自动载入目录）
 *   目录 [页码]      → 当前书目录，每页 20 章
 *   看 <N> / 第N章   → 发第 N 章正文（阿拉伯/中文数字都行）
 *   下一章 / 上一章  → 翻页
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import type { NapcatBot } from '../bot/napcat.js';
import type { ScopeRepository } from '../store/repository.js';
import type { ChatMessage } from './types.js';

export interface NovelCmdDeps {
  /** 生效的 scope 键列表（"private:3319085316" 形态），空 = 不启用。 */
  scopes: readonly string[];
  /** fq-novel 服务基址。 */
  baseUrl: string;
  /** 互备基址链（2026-10-14 双机互备）：主线路连接失败/业务失败时依次改走。
   * 默认 127.0.0.1:7641/fq = 经南京跳板隧道到重庆 183 的 ForAPI /fq 代理，
   * 对端自己还会再兜回本机（环有界：双方各试一次）。 */
  fallbackBaseUrls: readonly string[];
  /** 阅读状态持久化文件（每 scope 的当前书/目录/进度）。 */
  statePath: string;
  bot: NapcatBot;
  repo: ScopeRepository;
}

interface SearchHit {
  book_id: string;
  title: string;
  author: string;
  score?: string;
  word_count?: string;
}

interface ChapterRef {
  item_id: string;
  title: string;
}

interface ScopeReadingState {
  bookId?: string;
  bookTitle?: string;
  chapters?: ChapterRef[];
  /** 上次发出去的章节下标（0-based），驱动 下一章/上一章。 */
  chapterIdx?: number;
  search?: SearchHit[];
}

const CATALOG_PAGE_SIZE = 20;
/** 单条 QQ 消息的正文切块上限（按段落边界切）。 */
const CHAPTER_CHUNK_CHARS = 1500;
const FETCH_TIMEOUT_MS = 20_000;

const HELP_TEXT =
  '读书指令（直接发，不用@）：\n'
  + '· 搜书 <书名/作者> —— 找书\n'
  + '· 选 <序号> —— 从搜索结果里选一本\n'
  + '· 目录 [页码] —— 看目录（每页 20 章）\n'
  + '· 看 <第几章> —— 发正文，如「看 12」「第十二章」\n'
  + '· 下一章 / 上一章 —— 接着读\n'
  + '指令以外的消息还是雪球本人在回~';

/** 中文数字 → 阿拉伯数字（支持 千百十 与 零，上限到万内够用）。 */
export function parseChineseNumber(text: string): number | null {
  const digits: Record<string, number> = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  const units: Record<string, number> = { 十: 10, 百: 100, 千: 1000 };
  if (!/^[零一二两三四五六七八九十百千万]+$/.test(text)) return null;
  let total = 0;
  let section = 0;
  let number = 0;
  for (const ch of text) {
    if (ch in digits) {
      number = digits[ch];
    } else if (ch === '万') {
      section = (section + number) * 10_000;
      total += section;
      section = 0;
      number = 0;
    } else if (ch in units) {
      const unit = units[ch];
      // 「十」开头 = 一十
      section += (number === 0 ? 1 : number) * unit;
      number = 0;
    }
  }
  return total + section + number;
}

/** 章节号参数：先按阿拉伯数字，再按中文数字。 */
function parseChapterIndex(text: string): number | null {
  const trimmed = text.trim();
  if (/^\d+$/.test(trimmed)) {
    const value = Number(trimmed);
    return value >= 1 ? value : null;
  }
  const value = parseChineseNumber(trimmed);
  return value !== null && value >= 1 ? value : null;
}

/** 长正文按段落边界切块，尽量每块 ≤ limit 字。 */
export function chunkChapterText(text: string, limit: number = CHAPTER_CHUNK_CHARS): string[] {
  const paragraphs = text.split('\n');
  const chunks: string[] = [];
  let current = '';
  for (const paragraph of paragraphs) {
    // 单段就超限：硬切
    let piece = paragraph;
    while (piece.length > limit) {
      const head = current === '' ? '' : `${current}\n`;
      if (head.length >= limit) {
        chunks.push(current);
        current = '';
        continue;
      }
      const room = limit - head.length;
      chunks.push(`${head}${piece.slice(0, room)}`);
      piece = piece.slice(room);
      current = '';
    }
    const candidate = current === '' ? piece : `${current}\n${piece}`;
    if (candidate.length > limit && current !== '') {
      chunks.push(current);
      current = piece;
    } else {
      current = candidate;
    }
  }
  if (current !== '') chunks.push(current);
  return chunks;
}

export class NovelCommandHandler {
  private readonly scopes: Set<string>;
  private readonly baseUrl: string;
  private readonly fallbackBaseUrls: readonly string[];
  private readonly statePath: string;
  private readonly bot: NapcatBot;
  private readonly repo: ScopeRepository;
  private states: Record<string, ScopeReadingState> = {};

  constructor(deps: NovelCmdDeps) {
    this.scopes = new Set(deps.scopes);
    this.baseUrl = deps.baseUrl.replace(/\/+$/, '');
    this.fallbackBaseUrls = (deps.fallbackBaseUrls ?? []).map((b) => b.replace(/\/+$/, ''));
    this.statePath = deps.statePath;
    this.bot = deps.bot;
    this.repo = deps.repo;
    try {
      if (existsSync(this.statePath)) {
        const raw: unknown = JSON.parse(readFileSync(this.statePath, 'utf-8'));
        if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
          this.states = raw as Record<string, ScopeReadingState>;
        }
      }
    } catch {
      // 状态文件损坏：从零开始（只是丢了阅读进度，不致命）
      this.states = {};
    }
  }

  /** 该 scope 是否启用指令机。 */
  handles(scopeKey: string): boolean {
    return this.scopes.has(scopeKey);
  }

  /**
   * 尝试把这条消息当指令处理。命中（含「格式像指令但参数错」）→ true，
   * 已直接回复用户并写好历史，调用方应跳过 AI；不像指令 → false。
   */
  async tryHandle(message: ChatMessage, cleaned: string): Promise<boolean> {
    const text = cleaned.trim();
    const scopeKey = `${message.chatType}:${message.chatId}`;
    const state = this.states[scopeKey] ?? {};
    const reply = (msg: string): Promise<void> => this.send(scopeKey, message, msg);

    if (/^(读书|小说帮助|读书帮助)$/.test(text)) {
      await reply(HELP_TEXT);
      return true;
    }

    const searchMatch = text.match(/^搜(?:书|小说)[:：\s]*(.+)$/s);
    if (searchMatch !== null) {
      await this.handleSearch(scopeKey, message, searchMatch[1].trim(), reply);
      return true;
    }

    const pickMatch = text.match(/^选\s*(\d+)$/);
    if (pickMatch !== null) {
      await this.handlePick(scopeKey, message, state, Number(pickMatch[1]), reply);
      return true;
    }

    // 只认「目录」或「目录 <页码>」整句——「目录里有什么好吃的」这类正常聊天不劫持
    const catalogMatch = text.match(/^目录(?:\s+([0-9]+|[零一二两三四五六七八九十百千万]+))?$/);
    if (catalogMatch !== null) {
      const page = catalogMatch[1] === undefined ? 1 : parseChapterIndex(catalogMatch[1]);
      if (page === null) return false;
      await this.handleCatalog(scopeKey, state, page, reply);
      return true;
    }

    const readMatch =
      text.match(/^(?:看|读)\s*第?\s*([0-9]+|[零一二两三四五六七八九十百千万]+)\s*章?$/) ??
      text.match(/^第\s*([0-9]+|[零一二两三四五六七八九十百千万]+)\s*章$/);
    if (readMatch !== null) {
      const chapter = parseChapterIndex(readMatch[1]);
      if (chapter === null) {
        await reply('章节号没看懂，示例：看 12 或 第十二章');
        return true;
      }
      await this.handleRead(scopeKey, message, state, chapter, reply);
      return true;
    }

    if (/^(下一章|继续)$/.test(text)) {
      const idx = (state.chapterIdx ?? 0) + 1;
      await this.handleRead(scopeKey, message, state, idx + 1, reply);
      return true;
    }
    if (/^上一章$/.test(text)) {
      const idx = state.chapterIdx ?? 0;
      if (idx <= 0) {
        await reply('已经是第一章啦');
        return true;
      }
      await this.handleRead(scopeKey, message, state, idx, reply);
      return true;
    }

    return false;
  }

  // ── 各指令 ────────────────────────────────────────────────────────────────

  private async handleSearch(
    scopeKey: string,
    message: ChatMessage,
    key: string,
    reply: (msg: string) => Promise<void>,
  ): Promise<void> {
    if (key === '') {
      await reply('搜书要带上关键词，如：搜书 葬送的芙莉莲');
      return;
    }
    try {
      const data = await this.fetchFq<{ books?: unknown[] }>(`/search?q=${encodeURIComponent(key)}`);
      const books = (Array.isArray(data.books) ? data.books : []).slice(0, 5).map((item) => {
        const book = item as Record<string, unknown>;
        return {
          book_id: String(book.book_id ?? ''),
          title: String(book.title ?? '（无标题）'),
          author: String(book.author ?? ''),
          score: typeof book.score === 'string' && book.score.trim() !== '' ? book.score : undefined,
          word_count: book.word_count === undefined || book.word_count === '' ? undefined : String(book.word_count),
        };
      }).filter((book) => book.book_id !== '');
      if (books.length === 0) {
        await reply(`没搜到「${key}」相关的书，换个关键词试试`);
        return;
      }
      this.states[scopeKey] = { ...this.states[scopeKey], search: books };
      this.persist();
      const lines = books.map((book, index) => {
        const extras = [book.author, book.score !== undefined ? `评分${book.score}` : null, book.word_count !== undefined ? `${book.word_count}字` : null]
          .filter((part): part is string => part !== null && part !== '')
          .join('｜');
        return `${index + 1}. 《${book.title}》${extras === '' ? '' : ` ${extras}`}`;
      });
      await reply(`搜到 ${books.length} 本：\n${lines.join('\n')}\n回复「选 <序号>」开读`);
    } catch (error) {
      await reply(`搜索失败：${errText(error)}。小说服务可能没起来，让主人看看`);
    }
    void message;
  }

  private async handlePick(
    scopeKey: string,
    message: ChatMessage,
    state: ScopeReadingState,
    index: number,
    reply: (msg: string) => Promise<void>,
  ): Promise<void> {
    const hits = state.search ?? [];
    if (hits.length === 0) {
      await reply('还没有可选的搜索结果，先「搜书 <关键词>」');
      return;
    }
    if (index < 1 || index > hits.length) {
      await reply(`序号超出范围（1~${hits.length}）`);
      return;
    }
    const book = hits[index - 1];
    try {
      const chapters = await this.fetchCatalog(book.book_id);
      this.states[scopeKey] = {
        ...state,
        bookId: book.book_id,
        bookTitle: book.title,
        chapters,
        chapterIdx: undefined,
      };
      this.persist();
      await reply(`已选《${book.title}》，共 ${chapters.length} 章。回复「目录」看章节，或「看 1」直接开读`);
    } catch (error) {
      await reply(`载入《${book.title}》目录失败：${errText(error)}`);
    }
    void message;
  }

  private async handleCatalog(
    scopeKey: string,
    state: ScopeReadingState,
    page: number,
    reply: (msg: string) => Promise<void>,
  ): Promise<void> {
    const chapters = state.chapters;
    if (chapters === undefined || chapters.length === 0 || state.bookTitle === undefined) {
      await reply('还没选书呢：先「搜书 <关键词>」再「选 <序号>」');
      return;
    }
    const totalPages = Math.max(1, Math.ceil(chapters.length / CATALOG_PAGE_SIZE));
    if (page < 1 || page > totalPages) {
      await reply(`页码超出范围（1~${totalPages}，共 ${chapters.length} 章）`);
      return;
    }
    const start = (page - 1) * CATALOG_PAGE_SIZE;
    const lines = chapters.slice(start, start + CATALOG_PAGE_SIZE)
      .map((chapter, offset) => `${start + offset + 1}. ${chapter.title}`);
    const progress = state.chapterIdx !== undefined ? `\n（上次读到第 ${state.chapterIdx + 1} 章）` : '';
    await reply(`《${state.bookTitle}》目录 第${page}/${totalPages}页：\n${lines.join('\n')}${progress}\n回复「目录 ${page + 1}」翻页，「看 <序号>」读对应章`);
  }

  private async handleRead(
    scopeKey: string,
    message: ChatMessage,
    state: ScopeReadingState,
    chapterNo: number,
    reply: (msg: string) => Promise<void>,
  ): Promise<void> {
    const chapters = state.chapters;
    if (chapters === undefined || chapters.length === 0 || state.bookId === undefined) {
      await reply('还没选书呢：先「搜书 <关键词>」再「选 <序号>」');
      return;
    }
    if (chapterNo < 1 || chapterNo > chapters.length) {
      await reply(`章节超出范围（1~${chapters.length}）`);
      return;
    }
    const chapter = chapters[chapterNo - 1];
    let title: string;
    let body: string;
    try {
      const data = await this.fetchFq<{ title?: unknown; text?: unknown; content?: unknown }>(`/chapter/${encodeURIComponent(chapter.item_id)}`);
      title = typeof data.title === 'string' && data.title !== '' ? data.title : chapter.title;
      body = typeof data.text === 'string' && data.text.trim() !== '' ? data.text : htmlToPlainText(String(data.content ?? ''));
      if (body.trim() === '') throw new Error('正文为空');
    } catch (error) {
      await reply(`取第 ${chapterNo} 章失败：${errText(error)}。可以再发一次同样的指令重试`);
      return;
    }
    // 正文发送（分块）前先发标题行，长章在 QQ 里不至于只有一坨墙
    const header = `《${state.bookTitle ?? ''}》${title}`;
    const chunks = chunkChapterText(body);
    await this.sendRaw(message, header);
    for (const chunk of chunks) {
      await this.sendRaw(message, chunk);
    }
    this.states[scopeKey] = { ...this.states[scopeKey], chapterIdx: chapterNo - 1 };
    this.persist();
    this.recordHistory(scopeKey, message, `[小说指令] 已发《${state.bookTitle ?? state.bookId}》${title}（${body.length}字，分${chunks.length + 1}条）`);
  }

  // ── fq 服务访问 / 发送 / 历史 ─────────────────────────────────────────────

  private async fetchCatalog(bookId: string): Promise<ChapterRef[]> {
    const data = await this.fetchFq<{ volume_list?: unknown[] }>(`/book/${encodeURIComponent(bookId)}/catalog`);
    const volumes = Array.isArray(data.volume_list) ? data.volume_list : [];
    const chapters: ChapterRef[] = [];
    for (const volume of volumes) {
      const list = (volume as { chapter_list?: unknown[] }).chapter_list;
      if (!Array.isArray(list)) continue;
      for (const item of list) {
        const entry = item as Record<string, unknown>;
        const itemId = String(entry.item_id ?? '');
        if (itemId === '') continue;
        chapters.push({ item_id: itemId, title: String(entry.title ?? '') });
      }
    }
    if (chapters.length === 0) throw new Error('目录为空');
    return chapters;
  }

  /** fq 服务约定 code:0=成功；非 0 抛带 message 的错。
   *  双机互备：主线路连接失败或业务失败时沿 fallbackBaseUrls 依次改走，
   *  全挂才抛最后一个错。 */
  private async fetchFq<T extends Record<string, unknown>>(path: string): Promise<T> {
    const bases = [this.baseUrl, ...this.fallbackBaseUrls];
    let lastErr: unknown = null;
    for (const base of bases) {
      try {
        const response = await fetch(`${base}${path}`, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
        const payload = (await response.json()) as { code?: unknown; message?: unknown; data?: unknown };
        if (payload.code !== 0) {
          throw new Error(typeof payload.message === 'string' && payload.message !== '' ? payload.message : `code=${String(payload.code)}`);
        }
        return (payload.data ?? {}) as T;
      } catch (err) {
        lastErr = err;
      }
    }
    throw lastErr instanceof Error ? lastErr : new Error(String(lastErr ?? 'novel fetch failed'));
  }

  /** 给用户发一条消息（不记历史——指令应答统一由调用处记一条摘要）。 */
  private async sendRaw(message: ChatMessage, text: string): Promise<void> {
    await this.bot.sendText(message.chatType as 'group' | 'private', Number(message.chatId), text);
  }

  /** 短应答：发出 + 记一条历史（AI 之后能看到指令机回过什么）。 */
  private async send(scopeKey: string, message: ChatMessage, text: string): Promise<void> {
    await this.sendRaw(message, text);
    this.recordHistory(scopeKey, message, text);
  }

  private recordHistory(scopeKey: string, message: ChatMessage, text: string): void {
    const separator = scopeKey.indexOf(':');
    this.repo.appendMessage(scopeKey.slice(0, separator), scopeKey.slice(separator + 1), {
      user_id: this.bot.selfId,
      nickname: this.bot.selfNickname || 'AI',
      text: text.length > 300 ? `${text.slice(0, 300)}…` : text,
      raw_message: `[小说指令] ${text.slice(0, 300)}`,
      message_id: null,
      timestamp: Date.now() / 1000,
      source_label: '小说指令',
      image_refs: [],
      file_refs: [],
    });
    void message;
  }

  private persist(): void {
    try {
      writeFileSync(this.statePath, JSON.stringify(this.states), 'utf-8');
    } catch (error) {
      console.warn('[novel-cmd] state persist failed:', error instanceof Error ? error.message : String(error));
    }
  }
}

/** 章节 content 兜底：剥 XHTML 标签成纯文本（正常走 data.text，用不上）。 */
function htmlToPlainText(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/p>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
