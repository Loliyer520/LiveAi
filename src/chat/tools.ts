/**
 * Chat-mode tool schemas + executor. Ported core set from legacy
 * core/ai_tools_schema.py / _run_ai_tool_call: the send/stay_silent protocol
 * pair, recall, notes (memory), master relay, alarms.
 */

import { resolve as resolveAbs, sep as pathSep, dirname } from 'node:path';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import type { ToolSchema } from '../models/types.js';
import type { NapcatBot } from '../bot/napcat.js';
import type { ScopeRepository } from '../store/repository.js';
import type { MessageArchive } from '../store/archive.js';
import type { RelationGraph } from '../relations/graph.js';
import type { TaskScheduler } from './scheduler.js';
import type { ChatMessage, HistoryEntry } from './types.js';
import { replyMessageLines } from './reply-lines.js';
import { stripThinking } from './thinking.js';
import { ACTION_TOOL_SCHEMAS, executeActionTool, spendNetworkBudget } from './actions.js';
import { formatUsageText, usageStats } from '../models/usage.js';
import { pluginRegistry } from './plugins.js';
import { downloadToFile } from './net.js';
import { spoolToUrl } from './media-spool.js';
import { extractFrameTiled, videoDuration } from './video-frames.js';
import { mc2dClient } from './mc2d.js';
import type { GroupRolesInfo } from './group-roles.js';
import type { Workspace } from './workspace.js';
import type { KnowledgeBase, KbPermission } from '../knowledge/kb.js';
import { domainDefForScope, domainDefs, domainScopeHasBuiltin } from './domains.js';

function domainList(): string { return domainDefs().map((def) => `domain:${def.id}（${def.title}）`).join('、'); }

// ── send_tts 端点（chat-module 启动时注入；测试可覆盖） ─────────────────────
let ttsBaseUrl = 'http://127.0.0.1:8020';
let ttsTimeoutMs = 90_000;

export function configureTts(baseUrl: string, timeoutMs: number): void {
  ttsBaseUrl = baseUrl.replace(/\/+$/, '');
  ttsTimeoutMs = timeoutMs;
}

// ── Minecraft 边车（/my/run/mcbot，deeplive-mcbot.service @127.0.0.1:7660） ────
let mcBotBaseUrl = 'http://127.0.0.1:7660';
let mcBotToken = '';

export function configureMcBot(baseUrl: string, token: string): void {
  mcBotBaseUrl = baseUrl.replace(/\/+$/, '');
  mcBotToken = token;
}

/** 调边车 HTTP 接口；返回 {ok,message} 或抛错。 */
async function mcBotCall(path: string): Promise<{ ok: boolean; message: string; pngBase64?: string }> {
  const response = await fetch(`${mcBotBaseUrl}${path}`, {
    headers: mcBotToken ? { authorization: `Bearer ${mcBotToken}` } : {},
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) throw new Error(`边车 HTTP ${response.status}（liveai-mcbot 没在跑？systemctl status deeplive-mcbot）`);
  const body = (await response.json()) as { ok?: boolean; message?: string; png_base64?: string };
  return { ok: body.ok === true, message: String(body.message ?? ''), pngBase64: body.png_base64 };
}

/**
 * mc_view：拉边车渲染的世界图（俯视高度图+朝向剖面），交视觉模型。
 */
async function mcView(
  question: string,
  radius: number,
  describeImage: (url: string, q: string) => Promise<string>,
): Promise<string> {
  const result = await mcBotCall(`/view?radius=${radius}`);
  if (!result.ok || !result.pngBase64) return `看世界失败：${result.message}`;
  const prompt = question.trim() !== ''
    ? question
    : '这是真实 Minecraft 世界渲染图，两块面板。上图=俯视高度图：中心蓝色十字=我自己，蓝线朝向=视线方向，白点=其他玩家，红点=敌对生物，绿点=被动生物，黄点=掉落物；颜色越亮的地形越高，暗棕色块=比自己低很多的地表，天蓝色=高出视野的上方/虚空；水面是蓝色。下图=沿视线方向的竖直剖面：黑色=空气/洞穴，蓝色小块=我，能看到地形起伏、地层和矿脉。请客观描述：1) 我站在什么地形上；2) 周围有什么值得注意的（矿/水/树/生物/玩家/掉落物）分别在哪个方向；3) 有没有危险。';
  const description = await describeImage(`data:image/png;base64,${result.pngBase64}`, prompt);
  return `世界渲染图（说明见下图注）：\n${result.message}\n\n视觉模型描述：\n${description}`;
}

// ── 表情包索引（data/stickers/index.json：号主 QQ 收藏表情 + 视觉打标） ─────
export interface StickerEntry {
  md5: string;
  url: string;
  desc: string;
  tags: string[];
}

let stickerIndexPath: string | null = null;
let stickerIndexCache: StickerEntry[] | null = null;

export function configureStickers(indexPath: string): void {
  stickerIndexPath = indexPath;
  stickerIndexCache = null;
}

/** 各会话最近一次下载的视频（send_video 记、watch_video 默认看它）。 */
const lastVideoByScope = new Map<string, string>();

function loadStickerIndex(): StickerEntry[] {
  if (stickerIndexCache !== null) return stickerIndexCache;
  let entries: StickerEntry[] = [];
  if (stickerIndexPath !== null) {
    try {
      const raw: unknown = JSON.parse(readFileSync(stickerIndexPath, 'utf-8'));
      if (Array.isArray(raw)) entries = raw as StickerEntry[];
    } catch {
      // 索引文件缺失/损坏按空库处理，工具会给出可读提示
    }
  }
  stickerIndexCache = entries;
  return entries;
}

function stickerScore(entry: StickerEntry, query: string): number {
  const q = query.toLowerCase();
  let score = 0;
  for (const tag of entry.tags) {
    const t = tag.toLowerCase();
    if (t === q) score += 5;
    else if (t.includes(q) || q.includes(t)) score += 3;
  }
  if (entry.desc.toLowerCase().includes(q)) score += 2;
  return score;
}

// ── 表情库自动同步：fetch_custom_face → MD5 增量 → 新表情视觉打标 ──────────

/** 视觉打标 prompt：要 JSON，解析见 parseStickerCaption。 */
const STICKER_CAPTION_PROMPT =
  '看这张QQ表情包图，只输出一行 JSON，不要任何多余文字：'
  + '{"desc":"一句不超过20字的中文描述：主体+情绪或动作，图里有文字要转写出来",'
  + '"tags":["3到6个中文检索标签：情绪词/主体词/动作词"]}';

/** 收藏表情 URL 里内嵌的 32 位十六进制 MD5（增量比对键）。 */
function extractStickerMd5(url: string): string | null {
  const match = url.match(/_([0-9A-Fa-f]{32})_/);
  return match === null ? null : match[1].toUpperCase();
}

/**
 * 解析视觉模型的打标输出。优先 JSON；退化到 desc/tags 字段正则、
 * 两行文本（首行描述+次行标签）、单行逗号合并等各种模型实际会出现的形态。
 */
export function parseStickerCaption(raw: string): { desc: string; tags: string[] } {
  const text = raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
  const clean = (value: string): string => value.trim().slice(0, 60);
  const splitTags = (value: string): string[] =>
    value.split(/[,，、]/).map((tag) => tag.trim()).filter((tag) => tag !== '' && tag.length <= 20).slice(0, 8);
  try {
    const obj = JSON.parse(text) as { desc?: unknown; tags?: unknown };
    if (typeof obj.desc === 'string' && obj.desc.trim() !== '') {
      const tags = Array.isArray(obj.tags)
        ? obj.tags.filter((tag): tag is string => typeof tag === 'string' && tag.trim() !== '').map((tag) => clean(tag)).slice(0, 8)
        : [];
      return { desc: clean(obj.desc), tags };
    }
  } catch {
    // 走下面的退化解析
  }
  const descMatch = text.match(/"desc"\s*:\s*"([^"]+)"/);
  if (descMatch !== null) {
    const tagsMatch = text.match(/"tags"\s*:\s*\[([^\]]*)\]/);
    const tags = tagsMatch === null ? [] : (tagsMatch[1].match(/"([^"]*)"/g) ?? []).map((tag) => clean(tag.slice(1, -1)));
    return { desc: clean(descMatch[1]), tags };
  }
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line !== '');
  if (lines.length >= 2) return { desc: clean(lines[0]), tags: splitTags(lines[1]) };
  if (lines.length === 1) {
    const parts = lines[0].split(/[,，、]/).map((part) => part.trim()).filter((part) => part !== '');
    if (parts.length >= 5) return { desc: clean(parts.slice(0, 2).join('，')), tags: parts.slice(2, 8) };
    return { desc: clean(lines[0]), tags: [] };
  }
  return { desc: '', tags: [] };
}

export interface StickerRefreshResult {
  ok: boolean;
  total: number;
  kept: number;
  added: number;
  removed: number;
  failed: number;
  message: string;
}

let stickerRefreshInFlight: Promise<StickerRefreshResult> | null = null;

/**
 * 同步收藏表情到索引：拉 fetch_custom_face 清单，按 MD5 增量比对——已打标
 * 的只刷新 URL（CDN 路径会变），新表情逐张让视觉模型看图打标，被取消收藏
 * 的移除。打标失败的不写入（下次刷新重试），整库拉空视为接口异常不动索引。
 * 并发调用共用同一次执行。
 */
export async function refreshStickerIndex(
  bot: NapcatBot,
  describeImage: (url: string, question: string) => Promise<string>,
): Promise<StickerRefreshResult> {
  if (stickerRefreshInFlight !== null) return stickerRefreshInFlight;
  stickerRefreshInFlight = doRefreshStickerIndex(bot, describeImage);
  try {
    return await stickerRefreshInFlight;
  } finally {
    stickerRefreshInFlight = null;
  }
}

async function doRefreshStickerIndex(
  bot: NapcatBot,
  describeImage: (url: string, question: string) => Promise<string>,
): Promise<StickerRefreshResult> {
  const fail = (message: string): StickerRefreshResult => ({ ok: false, total: 0, kept: 0, added: 0, removed: 0, failed: 0, message });
  if (stickerIndexPath === null) return fail('表情包索引路径未配置，跳过同步。');
  let urls: string[];
  try {
    urls = await bot.fetchCustomFace();
  } catch (error) {
    return fail(`拉取收藏表情失败: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (urls.length === 0) return fail('收藏表情清单拉取为空（接口异常或未登录），跳过同步不动索引。');
  const existing = loadStickerIndex();
  const byMd5 = new Map(existing.map((entry) => [entry.md5, entry]));
  const next: StickerEntry[] = [];
  let added = 0;
  let failed = 0;
  const errors: string[] = [];
  for (const url of urls) {
    const md5 = extractStickerMd5(url);
    const prev = md5 === null ? undefined : byMd5.get(md5);
    if (prev !== undefined) {
      next.push({ ...prev, url });
      continue;
    }
    try {
      const caption = await describeImage(url, STICKER_CAPTION_PROMPT);
      const { desc, tags } = parseStickerCaption(caption);
      if (desc === '') throw new Error('打标结果解析不出描述');
      next.push({ md5: md5 ?? url, url, desc, tags });
      added += 1;
    } catch (error) {
      // 失败不占坑：不写入，下次刷新还会重试这张
      failed += 1;
      if (errors.length < 3) errors.push(error instanceof Error ? error.message : String(error));
    }
  }
  try {
    mkdirSync(dirname(stickerIndexPath), { recursive: true });
    writeFileSync(stickerIndexPath, `${JSON.stringify(next, null, 2)}\n`, 'utf-8');
  } catch (error) {
    return fail(`表情包索引写入失败: ${error instanceof Error ? error.message : String(error)}`);
  }
  stickerIndexCache = next;
  const kept = next.length - added;
  const removed = Math.max(0, existing.length - kept);
  const parts = [`收藏表情 ${urls.length} 张：新打标 ${added}、已有 ${kept}、移除 ${removed}`];
  if (failed > 0) parts.push(`打标失败 ${failed}${errors.length > 0 ? `（${errors.join('；')}）` : ''}，下次同步重试`);
  return { ok: true, total: urls.length, kept, added, removed, failed, message: `${parts.join('，')}。` };
}

export interface ToolContext {
  bot: NapcatBot;
  repo: ScopeRepository;
  /** Append-only archive backing memory_search. */
  archive: MessageArchive;
  /** Shared cross-scope relation graph. */
  relations: RelationGraph;
  /** Structured knowledge base（条目+挂载 ACL+待审核队列，见 src/knowledge/kb.ts）。测试桩可不提供。 */
  kb?: KnowledgeBase;
  /** Persisted alarm/recurring-task scheduler (timers survive restarts). */
  tasks: TaskScheduler;
  masterQq: number;
  /** True when the current turn runs in the master (主AI) scope. */
  isMaster: boolean;
  /** Called with the master-relay payload; orchestrator routes it. */
  notifyMaster: (payload: { text: string; requestType?: string; fromScope: string }) => void;
  /** Per-scope sandboxed filesystem root for the autonomous-action tools. */
  workspace: Workspace;
  /** Per-turn network call counter (web_* / download_* share the budget). */
  networkBudget: { used: number };
  /** Master only: inject an instruction turn into another scope's child AI. */
  delegateToChild: (targetScopeType: string, targetScopeId: string, instruction: string) => string;
  /** Master only: send a message directly to users in another scope. */
  sendToScope: (targetScopeType: string, targetScopeId: string, content: string) => Promise<string>;
  /** 主级互聊（domain_message）：向任意 scope 注入一条 [主级互聊 from …] 内部消息。 */
  peerInject: (targetScopeKey: string, text: string, extraRaw?: Record<string, unknown>) => void;
  /**
   * When set, send_message routes its lines through this sender instead of
   * bot.sendText/sendReplyText (master scope replying over the QQ dataline
   * channel — 我的电脑/我的手机 — which has no reply-quote support).
   */
  sendTextOverride?: (line: string) => Promise<Record<string, unknown>>;
  /** Image URLs attached to this turn's trigger messages (view_image fallback). */
  triggerImageRefs: string[];
  /** Vision-model describe call (role 'vision', falls back to main). */
  describeImage: (url: string, question: string) => Promise<string>;
  /** Group scopes: owner/admins + the bot's own role (drives admin tools). */
  getGroupRoles?: () => Promise<GroupRolesInfo>;
  scopeType: string;
  scopeId: string;
  executedTools: string[];
}

export interface ToolResult {
  ok: boolean;
  output: string;
  /** Turn-level side effects the orchestrator must honor. */
  staySilent?: boolean;
  /** The tool delivered user-visible content (suppresses the re-prompt rescue). */
  sentToUser?: boolean;
}

export const CHAT_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'send_message',
    description:
      '给用户发送消息（唯一发言途径，普通文字不会被发送）。content 里可用 <thinking>...</thinking> 包裹思考，会自动过滤。用 \\n 分行即可分条发送：每一行作为一条独立的 QQ 短消息发出（像人连续发几条消息那样）；超长行会自动按标点断成 ≤36 字的短条。',
    parameters: {
      type: 'object',
      properties: {
        content: { type: 'string', description: '要发送的内容（thinking 标签会被过滤）' },
        reply_to_id: { type: 'string', description: '可选，要引用回复的消息短ID，如 A1B2' },
      },
      required: ['content'],
    },
  },
  {
    name: 'stay_silent',
    description: '决定本轮不说话时调用，直接结束本回合。不要把沉默理由写成普通文字。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'recall_message',
    description: '撤回一条消息（传消息短ID）。自己的消息 2 分钟内可撤；如果我是群管理员，可以撤回群里任何人的消息（只用于广告/刷屏/人身攻击等违规内容）。',
    parameters: {
      type: 'object',
      properties: { message_ref: { type: 'string', description: '消息短ID，如 A1B2' } },
      required: ['message_ref'],
    },
  },
  {
    name: 'group_mute',
    description: '【群聊专用，需要我是群管理员】禁言群员 minutes 分钟（0=解除禁言，最大 43200 即 30 天）。只用于广告、刷屏、人身攻击等违规行为，别拿来玩闹；群主不能被禁言，管理员只有群主身份的 bot 才能禁言。',
    parameters: {
      type: 'object',
      properties: {
        qq: { type: 'string', description: '对方 QQ 号' },
        minutes: { type: 'number', description: '禁言分钟数，0=解除禁言' },
      },
      required: ['qq', 'minutes'],
    },
  },
  {
    name: 'memory_list',
    description: '列出当前会话最近的 AI 备忘（短条目）。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'memory_write',
    description: '写一条备忘（会被压缩进提示词的"最近备注"区，不发给用户）。不要用它在聊天里说话。',
    parameters: {
      type: 'object',
      properties: { text: { type: 'string', description: '备忘内容，一句话' } },
      required: ['text'],
    },
  },
  {
    name: 'kb_list',
    description:
      '查知识库（只能看到挂载给本会话的条目）。tags 按标签过滤（多个=同时满足），q 按主题/内容模糊搜索。'
      + '返回条目 ID、主题、标签、可信度——拿到 ID 后用 kb_read 看正文。',
    parameters: {
      type: 'object',
      properties: {
        tags: { type: 'array', items: { type: 'string' }, description: '标签过滤（可选）' },
        q: { type: 'string', description: '关键词模糊搜索（可选）' },
        limit: { type: 'number', description: '最多返回条数，默认 20' },
      },
    },
  },
  {
    name: 'kb_read',
    description: '读知识库某条目的正文（传 kb_list 拿到的条目 ID）。',
    parameters: {
      type: 'object',
      properties: { id: { type: 'string', description: '条目 ID' } },
      required: ['id'],
    },
  },
  {
    name: 'kb_append',
    description:
      '向知识库追加一条新知（需挂载方给过追加权限）。下级 AI 追加会进主级待审核队列，复核通过才生效；'
      + '主AI直接生效。tags 写上所属方面（如 mc、server、人物关系）。',
    parameters: {
      type: 'object',
      properties: {
        subject: { type: 'string', description: '主题，一句话（80字内）' },
        content: { type: 'string', description: '正文（2000字内）' },
        tags: { type: 'array', items: { type: 'string' }, description: '标签数组' },
        confidence: { type: 'number', description: '可信度 0~1，默认 0.5（下级报备）/0.8（主AI直录）' },
      },
      required: ['subject', 'content'],
    },
  },
  {
    name: 'notify_master',
    description:
      '联系主AI：上报情报、跨会话协调、请求高风险授权、系统级操作。request_type 常用: intel / set_user_preference / relation_add_fact / system_operation / cross_session_relay。',
    parameters: {
      type: 'object',
      properties: {
        text: { type: 'string', description: '要告诉主AI的完整内容' },
        request_type: { type: 'string', description: '请求类型' },
      },
      required: ['text'],
    },
  },
  {
    name: 'create_task',
    description:
      '创建任务。kind=set_alarm 一次性闹钟（at 传 Unix 秒或"+90s"/"+5m"/"+2h"/"+1d"）；'
      + 'kind=recurring_task 周期任务（every 传"+30m"/"+6h"等间隔，note 写每次触发时要执行的完整指令）。'
      + '任务持久化保存，重启不丢。',
    parameters: {
      type: 'object',
      properties: {
        kind: { type: 'string', description: 'set_alarm / recurring_task' },
        at: { type: 'string', description: '闹钟时间（Unix 秒或 +90s/+5m/+2h/+1d）' },
        every: { type: 'string', description: '周期间隔（+30m/+6h/+1d，最短 1 分钟）' },
        note: { type: 'string', description: '提醒内容 / 周期任务每次要执行的指令' },
      },
      required: ['kind', 'note'],
    },
  },
  {
    name: 'list_tasks',
    description: '列出还未触发的闹钟/周期任务（主AI可见所有会话的任务）。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'cancel_task',
    description: '取消一个还没触发的任务（传 task_id，可从 list_tasks 查到）。',
    parameters: {
      type: 'object',
      properties: { task_id: { type: 'string', description: '要取消的任务 ID' } },
      required: ['task_id'],
    },
  },
  {
    name: 'view_image',
    description:
      '查看图片：默认看本轮消息带的图（index 从 1 开始），也可传 message_ref 看历史某条消息里的图。'
      + 'question 写你想从图里知道什么（默认整体描述）。不要对每张图都调，先看提示文字判断需不需要。',
    parameters: {
      type: 'object',
      properties: {
        message_ref: { type: 'string', description: '可选，历史消息短ID，如 A1B2；不传则看本轮消息的图' },
        index: { type: 'number', description: '第几张图，从 1 开始，默认 1' },
        question: { type: 'string', description: '想从图中了解什么' },
      },
    },
  },
  {
    name: 'send_image',
    description:
      '给用户发送一张图片。file 传 http(s) 图片链接、base64:// 数据或 NapCat 可访问的本地绝对路径。'
      + '需要配文字说明时用 send_message 另发一条。',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: '图片地址：http(s)://… / base64://… / 绝对路径' },
      },
      required: ['file'],
    },
  },
  {
    name: 'send_tts',
    description:
      '把一段文字合成为语音消息发给用户（满穗音色）。适合唱歌、念台词、情绪表达等文字传不出味道的场合；'
      + '日常对话仍用 send_message 发文字，别每句都发语音。text 最多 300 字，超了会被拒——请拆成几段分别调用。'
      + 'emotion 可选：default 对话（默认）/ narration 叙述旁白 / angry 愤怒 / fear 恐惧 / pain 疼痛；'
      + 'speed 可选 0.5~2.0（默认 1）；pause 句间停顿 0~2 秒（默认 0.3，朗读/台词可用长停顿拉节奏）；'
      + 'temperature 表现力 0.1~2（越大语调起伏越大，默认 0.6≈日常随口说，要戏剧化再调高）。合成要几秒钟，调用后等结果，成功就别再重复发。',
    parameters: {
      type: 'object',
      properties: {
        text: { type: 'string', description: '要念出来的文字（≤300 字）' },
        emotion: { type: 'string', description: 'default / narration / angry / fear / pain，默认 default' },
        speed: { type: 'number', description: '语速 0.5~2.0，默认 1' },
        pause: { type: 'number', description: '句间停顿秒数 0~2，默认 0.3；朗读、念台词时可以调大营造节奏' },
        temperature: { type: 'number', description: '表现力 0.1~2，越大语调起伏越大；默认 0.6（主人调定的日常感），要夸张/戏剧化再调高' },
      },
      required: ['text'],
    },
  },
  {
    name: 'send_video',
    description:
      '给用户发送一条视频消息。url 传视频直链（http(s) 的 mp4/flv 等；B 站 CDN 直链会自动带 Referer）。'
      + '先下载再发，最大 150MB，下载+转码要等一会儿；太大就换低清晰度的直链（如 B 站取 360P）。'
      + '配文字说明用 send_message 另发。看完就走的小视频场景（B 站插件 bili_url 取直链后）用它。',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: '视频直链 http(s)://…（B 站插件 bili_url 返回的 play_url）' },
      },
      required: ['url'],
    },
  },
  {
    name: 'watch_video',
    description:
      '用眼睛看视频：从视频文件抽取若干帧、贴成一张带时间戳的长图，交给视觉模型看（真多模态）。'
      + '想看视频里发生了什么、验证画面内容、找某个画面时用它。file 传 send_video 返回的工作区路径（不传默认看最近一次下载的视频），'
      + '也可直接给 url 现下载。moments 写想看的时刻（秒，逗号分隔，最多 10 个，如 0,30,120）；不写则自动均匀取 5 帧。'
      + '画面是抽帧快照，看不到声音和连贯动作；要发给用户看还是用 send_video。',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: '工作区内的视频路径（send_video 输出里的「已存工作区」），可选' },
        url: { type: 'string', description: '视频直链（可选，给了就现下载）' },
        moments: { type: 'string', description: '想看的时刻，秒，逗号分隔（如 0,15,60,180），最多 10 个；不写自动均匀取 5 帧' },
        question: { type: 'string', description: '想从画面里知道什么；不写则客观描述每帧内容' },
      },
    },
  },
  {
    name: 'send_sticker',
    description:
      '发一张表情包（号主的收藏表情库）。query 写想表达的意思或情绪关键词（如 开心、无语、哭、猫猫、鼓掌、问号、震惊），'
      + '会挑最贴的一张发出去；写「随机」就随便来一张。想斗图、接梗、缓和气氛或文字差点意思时用；'
      + '查不到会返回可用标签清单，换个词再试。别连续刷屏，一轮最多发一两次。',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '想表达的意思/情绪关键词，如 无语、猫猫、鼓掌；或「随机」' },
      },
      required: ['query'],
    },
  },
  {
    name: 'trigger_config',
    description:
      '调整自己在本会话群里的触发设置（私聊必回，只在群聊有意义）。'
      + 'action=set_rate 设随机接话率（rate 0~1，越小越安静，默认 0.01）；'
      + 'action=add_word / remove_word 增删自己的触发词（word）；'
      + 'action=list 查看当前设置。被嫌吵就调低，混熟了想活跃再调高。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'set_rate / add_word / remove_word / list' },
        rate: { type: 'number', description: 'set_rate：随机接话率 0~1' },
        word: { type: 'string', description: 'add_word/remove_word：触发词（≤20字）' },
      },
      required: ['action'],
    },
  },
  {
    name: 'memory_search',
    description:
      '精确检索历史聊天记录（含你发过的消息和很久以前的内容，不限于当前上下文窗口）。'
      + 'query 用空格分隔多个关键词（全部命中才返回），用引号包住必须连续出现的短语，如 "北京 见面" 周末。'
      + '需要回忆具体细节、时间、承诺、谁说过什么时优先用它，别凭印象编。',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '搜索词：空格分词=AND，引号=精确短语' },
        scope: { type: 'string', description: '"current"（默认，只搜当前会话）或 "all"（搜所有会话）' },
        user_id: { type: 'number', description: '可选，只搜某人（QQ号）发的消息' },
        days: { type: 'number', description: '可选，只搜最近 N 天' },
        limit: { type: 'number', description: '可选，返回条数上限，默认 12，最大 50' },
      },
      required: ['query'],
    },
  },
  {
    name: 'intel_report',
    description:
      '向全局关系网络上报情报：某个QQ号的一条客观事实（身份/偏好/关系/重要事件等）。'
      + '所有会话的子AI共享这份网络，你在任何群里了解到某人的信息，都会进入他在其他会话的档案。'
      + '只上报确认过或高概率的事，主观臆测不要报。这条信息用户看不到。',
    parameters: {
      type: 'object',
      properties: {
        fact_subject_id: { type: 'number', description: '情报主体的 QQ 号' },
        fact_category: {
          type: 'string',
          description: '分类：identity（身份）/ preference（偏好）/ relationship（关系）/ event（事件）/ emotion（情绪倾向）/ other',
        },
        fact_text: { type: 'string', description: '一句话客观陈述，≤60字，带具体锚点' },
        confidence: { type: 'number', description: '把握 0~1，默认 0.8' },
      },
      required: ['fact_subject_id', 'fact_category', 'fact_text'],
    },
  },
  {
    name: 'relation_query',
    description:
      '查询全局关系网络：某人的人物档案（跨所有会话的观察）、按关键词搜情报、或列出本会话已知成员概况。'
      + '想不起来某个人是谁、谁和谁什么关系、以前聊过什么相关的事，先查这里再回答。',
    parameters: {
      type: 'object',
      properties: {
        user_id: { type: 'number', description: '查这个 QQ 号的人物档案' },
        query: { type: 'string', description: '关键词搜情报（空格分词=AND），如 "北京 见面"' },
        list_members: { type: 'boolean', description: 'true 时列出本会话已知成员一句话概况' },
      },
    },
  },
  {
    name: 'impression_write',
    description:
      '更新你对当前会话的整体印象（用途/氛围/关键人物/说话风格），一句话 60 字内。'
      + '印象会写进全局关系网络的会话档案，之后每次唤醒都会带在提示词里。'
      + '只有当你对会话的理解有实质更新时才写，不要每轮都写。',
    parameters: {
      type: 'object',
      properties: { text: { type: 'string', description: '新的会话印象，一句话' } },
      required: ['text'],
    },
  },
];

/** 主AI scope 专属工具：关系网写入、跨会话委派、直接发消息。 */
const MASTER_ONLY_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'relation_write',
    description:
      '写入/修订全局关系网络（仅主AI）。action 说明：'
      + 'set_person_impression=更新某人整体印象（user_id + text）；'
      + 'add_fact=给某人追加一条事实（user_id + category + text，confidence 可选）；'
      + 'supersede_fact=用新表述取代一条旧情报（fact_id + text）；'
      + 'retract_fact=撤回一条错误情报（fact_id）；'
      + 'set_scope_impression=修订某会话的印象（target_scope_type + target_scope_id + text）。'
      + '写入前先用 relation_query 查已有档案做增量更新，避免重复和覆盖。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          description: 'set_person_impression / add_fact / supersede_fact / retract_fact / set_scope_impression',
        },
        user_id: { type: 'number', description: '目标人物 QQ 号（人物类 action 必填）' },
        category: { type: 'string', description: 'add_fact 分类：identity/preference/event/relationship/emotion/other' },
        text: { type: 'string', description: '印象/事实/新表述的内容' },
        confidence: { type: 'number', description: 'add_fact 把握 0~1，默认 1（主AI亲自录入）' },
        fact_id: { type: 'string', description: 'supersede/retract 的目标情报 ID' },
        target_scope_type: { type: 'string', description: 'set_scope_impression 目标会话类型 group/private' },
        target_scope_id: { type: 'string', description: 'set_scope_impression 目标会话 ID' },
      },
      required: ['action'],
    },
  },
  {
    name: 'delegate_to_child',
    description:
      '委派另一个会话/领域中枢/领域下级做事（仅主AI）：instruction 会被注入目标，由那边的 AI 自主执行'
      + '（它可以自然地与用户说话、查证、回报）。跨会话联系/转达/查情况优先用它，而不是自己直接出面。'
      + 'target_scope_type：group / private 是 QQ 会话；domain 是领域主级中枢（如 domain mc = Minecraft 中枢，'
      + 'MC 相关任务一律委派给它，它持有全部 mc_* 工具和 mc_view）；bili 是 B 站视频下级 AI'
      + '（target_scope_id 传 bvid，如 BV1xx，每个视频一个独立上下文）；mcclient 是 MC client 下级 AI'
      + '（target_scope_id 传 client 名）。'
      + 'instruction 要写全背景：谁要求的、要做什么、说完要不要回报。对方完成后会通过 notify_master 回报结果。',
    parameters: {
      type: 'object',
      properties: {
        target_scope_type: { type: 'string', description: '目标类型 group / private / domain / bili / mcclient' },
        target_scope_id: { type: 'string', description: '目标 ID（群号、QQ 号、领域 id 如 mc、或 bvid / client 名）' },
        instruction: { type: 'string', description: '给目标的完整指令' },
      },
      required: ['target_scope_type', 'target_scope_id', 'instruction'],
    },
  },
  {
    name: 'message_scope',
    description:
      '以机器人身份直接向指定会话的用户发一条消息（仅主AI）。'
      + '仅在必须绕过子AI直接说话时使用（系统通知、紧急干预）；日常联系一律用 delegate_to_child 让子AI自然转达。',
    parameters: {
      type: 'object',
      properties: {
        target_scope_type: { type: 'string', description: '目标会话类型 group/private' },
        target_scope_id: { type: 'string', description: '目标会话 ID' },
        content: { type: 'string', description: '要发送的内容' },
      },
      required: ['target_scope_type', 'target_scope_id', 'content'],
    },
  },
  {
    name: 'plugin_manage',
    description:
      '管理 API 插件（仅主AI）。插件是给 bot 增加能力的 JSON 声明式 HTTP 工具（data/plugins/<名>/plugin.json）。'
      + 'action：list=列出所有插件及其工具/状态/加载错误；enable/disable 加 name=插件名 开关某个插件（立即生效并持久化）；'
      + 'reload=重新扫描插件目录（管理员改完插件文件后调用，热加载不用重启）。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'list / enable / disable / reload' },
        name: { type: 'string', description: '插件名（enable/disable 时必填）' },
      },
      required: ['action'],
    },
  },
  {
    name: 'scope_manage',
    description:
      '管理各会话的子AI（仅主AI）。action：'
      + 'list=列出所有会话的开关/随机触发率/触发词；'
      + 'get=查看单个会话配置（target_scope_type + target_scope_id）；'
      + 'set_enabled=开关子AI（enabled=true/false；关闭后该会话任何消息都不回复——含@和触发词——历史照记；主AI会话不可关）；'
      + 'set_trigger_rate=设随机触发率（rate，0~1 的小数，群消息没有@也没有触发词时回复的概率，0=只认@和触发词）；'
      + 'add_trigger_words=追加触发词（words 字符串数组）；'
      + 'remove_trigger_words=删除触发词（words 字符串数组）。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'list / get / set_enabled / set_trigger_rate / add_trigger_words / remove_trigger_words' },
        target_scope_type: { type: 'string', description: '目标会话类型 group/private（list 以外必填）' },
        target_scope_id: { type: 'string', description: '目标会话 ID（list 以外必填）' },
        enabled: { type: 'boolean', description: 'set_enabled：开=true 关=false' },
        rate: { type: 'number', description: 'set_trigger_rate：0~1' },
        words: { type: 'array', items: { type: 'string' }, description: '触发词数组' },
      },
      required: ['action'],
    },
  },
  {
    name: 'usage_stats',
    description:
      '查看本次启动以来的模型请求次数与 token 用量（仅主AI）：合计 + 按角色（main/vision/summary）'
      + '和按模型分桶，附最近一条模型错误。排查「回复变慢/一直失败/烧 token 太猛」时用。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'sticker_refresh',
    description:
      '同步号主的收藏表情到表情包库（仅主AI）。拉取收藏清单按 MD5 增量比对：新表情用视觉模型看图自动打标（描述+检索标签），'
      + '已打标的跳过不重复花钱，被取消收藏的移除。每次启动会自动同步一次，主人说「更新/刷新表情库」'
      + '或表情库报缺、有新收藏时手动调。新表情多时打标要一会儿，属正常。',
    parameters: { type: 'object', properties: {} },
  },
  // ── 2D 我的世界联机（MC2D）：bot 以「冰糖」身份进服陪玩 ──────────────────
  {
    name: 'mc2d_status',
    description:
      '查看 2D 我的世界联机游戏状态（仅主AI）：自己坐标/生命/饥饿/背包、在线玩家、附近生物、最近聊天，'
      + '以及身边方块地形图。第一次调用会自动以「冰糖」进服，掉线会自动重连。'
      + '主人问「你在游戏里吗/周围有什么/旁边是谁」时先调它。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'mc2d_move',
    description:
      '让游戏角色走到目标方块列 x（仅主AI，0-399）。按真实物理走，会自动跳 1-2 格高的台阶，'
      + '最多约 45 秒，被地形卡住会返回实际位置——可以再调一次或先用 mc2d_dig 挖开挡路方块。',
    parameters: {
      type: 'object',
      properties: { x: { type: 'number', description: '目标方块列（横坐标）' } },
      required: ['x'],
    },
  },
  {
    name: 'mc2d_dig',
    description:
      '挖掉指定方块（仅主AI）。只能在身边 6 格（曼哈顿距离）内挖，挖到的掉落物自动进背包。'
      + '用 mc2d_status 的地形图确认方块坐标再挖。',
    parameters: {
      type: 'object',
      properties: {
        x: { type: 'number', description: '方块列' },
        y: { type: 'number', description: '方块行（0=世界顶部，越大越深）' },
      },
      required: ['x', 'y'],
    },
  },
  {
    name: 'mc2d_place',
    description:
      '在指定位置放一个方块（仅主AI，6 格内，目标必须是空气，背包里要有该方块）。'
      + 'block 用方块 id：2 泥土 / 4 原木 / 6 沙子 / 7 木板 / 8 圆石 / 9 煤矿 / 10 铁矿 / 11 金矿 / 12 钻石矿 / 17 床 / 20 熔炉。',
    parameters: {
      type: 'object',
      properties: {
        x: { type: 'number', description: '方块列' },
        y: { type: 'number', description: '方块行' },
        block: { type: 'number', description: '方块 id（见描述）' },
      },
      required: ['x', 'y', 'block'],
    },
  },
  {
    name: 'mc2d_attack',
    description:
      '攻击附近的生物（仅主AI，6 格内）。不传 mob_id 就打最近的。damage 1-8，默认 4。'
      + '打死猪会掉猪肉；僵尸/骷髅/苦力怕晚上才刷。',
    parameters: {
      type: 'object',
      properties: {
        mob_id: { type: 'number', description: '目标生物 id（mc2d_status 里有），不传=最近' },
        damage: { type: 'number', description: '伤害 1-8，默认 4' },
      },
    },
  },
  {
    name: 'mc2d_say',
    description: '在 MC2D 游戏聊天框里说一句话（仅主AI，最多 60 字），服务器里的其他玩家能看到。',
    parameters: {
      type: 'object',
      properties: { message: { type: 'string', description: '要说的话，≤60 字' } },
      required: ['message'],
    },
  },
  {
    name: 'mc2d_view',
    description:
      '把游戏当前画面渲染成图片交给视觉模型看（仅主AI）：能识别自己所在的地形、周围方块、其他玩家和生物。'
      + '想确认「眼前是什么/怎么走/怪在哪」时用；坐标和方块名优先用 mc2d_status 的文本地形图。',
    parameters: {
      type: 'object',
      properties: { question: { type: 'string', description: '可选，想让视觉模型重点看什么' } },
    },
  },
  {
    name: 'mc2d_leave',
    description: '让角色退出 MC2D 服务器（仅主AI）。下次调用任意 mc2d_* 工具会自动重新进服。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'kb_edit',
    description:
      '修订知识库既有条目（仅主AI）：原地修订保留条目 ID，挂载继续生效（supersede 语义）。'
      + 'subject/content/tags/confidence 传要改的部分，都不传=只刷新来源和时间。',
    parameters: {
      type: 'object',
      properties: {
        id: { type: 'string', description: '条目 ID' },
        subject: { type: 'string', description: '新主题（可选）' },
        content: { type: 'string', description: '新正文（可选）' },
        tags: { type: 'array', items: { type: 'string' }, description: '新标签数组（可选，整体替换）' },
        confidence: { type: 'number', description: '新可信度（可选）' },
      },
      required: ['id'],
    },
  },
  {
    name: 'kb_delete',
    description: '删除知识库条目（仅主AI）。删除后所有挂载方都看不到，谨慎使用（误录优先 kb_edit 修订）。',
    parameters: {
      type: 'object',
      properties: { id: { type: 'string', description: '条目 ID' } },
      required: ['id'],
    },
  },
  {
    name: 'kb_mount',
    description:
      '把知识库挂载给某个会话（仅主AI）。target=会话 scope（如 group:12345、private:678 或 all=全部会话）；'
      + 'permission：read 只读 / append 追加（下级新增进待审核队列）/ write 读写（可修订）；'
      + 'tags 限定挂载范围（不传=全部条目）。中枢自己天然全权，无需挂载。',
    parameters: {
      type: 'object',
      properties: {
        target: { type: 'string', description: '目标 scope key 或 all' },
        permission: { type: 'string', description: 'read / append / write' },
        tags: { type: 'array', items: { type: 'string' }, description: '标签范围（可选）' },
      },
      required: ['target', 'permission'],
    },
  },
  {
    name: 'kb_unmount',
    description: '撤销知识库挂载（仅主AI）。target 必填；tags 不传=撤销该 target 的全部挂载。',
    parameters: {
      type: 'object',
      properties: {
        target: { type: 'string', description: '目标 scope key' },
        tags: { type: 'array', items: { type: 'string' }, description: '只撤销匹配该标签范围的挂载（可选）' },
      },
      required: ['target'],
    },
  },
  {
    name: 'kb_pending',
    description: '列出知识库待审核队列（仅主AI）：下级 AI 的追加报备，等复核。',
    parameters: {
      type: 'object',
      properties: { proposed_by: { type: 'string', description: '只看某个 scope 提出的（可选）' } },
    },
  },
  {
    name: 'kb_review',
    description:
      '复核知识库待审项（仅主AI）。action=approve 通过入库（来源记为 approved:）/ reject 驳回丢弃。'
      + '通过后条目按原有标签入库，挂载方立即可见。',
    parameters: {
      type: 'object',
      properties: {
        id: { type: 'string', description: '待审项 ID' },
        action: { type: 'string', description: 'approve / reject' },
      },
      required: ['id', 'action'],
    },
  },
];

/** 主AI scope 不用的子AI工具（notify_master 会中继给自己形成自环）。 */
const CHILD_ONLY_TOOL_NAMES = new Set(['notify_master', 'intel_report', 'impression_write']);

/**
 * 主级层互聊工具：主AI 与领域中枢（domain:*）之间直接对话，不绕道汇报链。
 * 下发给主AI scope 与全部领域中枢 scope。
 */
const HUB_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'domain_message',
    description:
      '主级AI互聊（仅主AI与领域中枢可用）：直接给另一个领域中枢或主AI留言，对方会话里会收到'
      + '[主级互聊 from …] 注入。target 传领域 id（mc / onebot / bili…）或 "master"。'
      + '跨领域协调、约行动、同步情报优先用它；它是主级之间的横向通道，不等同 notify_master 汇报。',
    parameters: {
      type: 'object',
      properties: {
        target: { type: 'string', description: '目标领域 id（如 mc、onebot、bili）或 master' },
        content: { type: 'string', description: '要传达的完整内容' },
        request_type: { type: 'string', description: '可选，请求类型标注（coord / intel / request 等）' },
      },
      required: ['target', 'content'],
    },
  },
];

/**
 * Scope-dependent tool list: children get the chat set; the master scope swaps
 * the report-to-master tools for direct relation-write and delegation tools;
 * 领域主级 AI（domain:*）拿 CHILD 集 + 自己的 builtinTools（如 mc_view）+
 * 主级互聊工具；bili/mcclient 等虚拟下级 scope 拿 CHILD 集（无 QQ 窗口，
 * 发言经 orchestrator 改道汇报给自己领域的中枢）。
 */
export function chatToolSchemas(scopeKey: string, isMaster: boolean): ToolSchema[] {
  const domain = domainDefForScope(scopeKey);
  const builtin: ToolSchema[] = isMaster
    ? [
      ...CHAT_TOOL_SCHEMAS.filter((schema) => !CHILD_ONLY_TOOL_NAMES.has(schema.name)),
      ...ACTION_TOOL_SCHEMAS,
      ...MASTER_ONLY_TOOL_SCHEMAS,
      ...HUB_TOOL_SCHEMAS,
    ]
    : domain !== null
      ? [...CHAT_TOOL_SCHEMAS, ...ACTION_TOOL_SCHEMAS, ...domain.builtinTools, ...HUB_TOOL_SCHEMAS]
      : [...CHAT_TOOL_SCHEMAS, ...ACTION_TOOL_SCHEMAS];
  const reserved = new Set(builtin.map((schema) => schema.name));
  const pluginSchemas = pluginRegistry()?.toolSchemas(scopeKey, isMaster, reserved) ?? [];
  return [...builtin, ...pluginSchemas];
}

export async function executeChatTool(
  call: { name: string; arguments: Record<string, unknown> },
  context: ToolContext,
): Promise<ToolResult> {
  context.executedTools.push(call.name);
  const args = call.arguments ?? {};
  switch (call.name) {
    case 'send_message': {
      // Never "chat" with the bot's own account (self-chat is not a scope).
      if (context.scopeType === 'private' && botIsSelfId(context.bot, context.scopeId)) {
        return { ok: false, output: '这个会话是机器人自己的账号，不跟自己聊天。' };
      }
      // Outbound normalization (legacy _send_scope_message): drop thinking,
      // [[...]] markers, and CQ:at codes in private chats, then split into
      // per-message lines ("分条").
      let text = stripThinking(String(args.content ?? '')).replace(/\[\[.*?\]\]/g, '');
      if (context.scopeType === 'private') text = text.replace(/\[CQ:at,qq=\d+\]/g, '');
      const lines = replyMessageLines(text);
      if (lines.length === 0) return { ok: false, output: 'send_message: content 过滤 thinking 后为空，未发送。' };
      let replyToMessage: ChatMessage | null = null;
      const replyRef = String(args.reply_to_id ?? '').trim();
      if (replyRef) {
        const messageId = context.repo.resolveRef(context.scopeType, context.scopeId, replyRef);
        if (messageId) {
          replyToMessage = {
            chatType: context.scopeType as 'group' | 'private',
            chatId: Number(context.scopeId),
            userId: 0,
            text: '',
            rawMessage: '',
            sender: {},
            messageId,
            mentionsSelf: false,
            timestamp: Date.now() / 1000,
            rawData: {},
          };
        }
      }
      // Reply quote anchors to the first line only; the rest go out plain.
      // sendTextOverride (dataline master channel) has no quote support.
      const refs: string[] = [];
      let failure = '';
      for (const [index, line] of lines.entries()) {
        try {
          const result = context.sendTextOverride
            ? await context.sendTextOverride(line)
            : index === 0 && replyToMessage
              ? await context.bot.sendReplyText(replyToMessage, line)
              : await context.bot.sendText(context.scopeType as 'group' | 'private', Number(context.scopeId), line);
          const sentId = (result as { message_id?: number | string }).message_id;
          const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
          refs.push(ref);
          const entry: HistoryEntry = {
            user_id: context.bot.selfId,
            nickname: context.bot.selfNickname || 'AI',
            text: line,
            raw_message: line,
            message_id: sentId ?? null,
            message_ref: ref,
            timestamp: Date.now() / 1000,
            source_label: '',
          };
          context.repo.appendMessage(context.scopeType, context.scopeId, entry);
        } catch (error) {
          failure = error instanceof Error ? error.message : String(error);
          break;
        }
      }
      if (refs.length === 0) return { ok: false, output: `send_message 发送失败: ${failure}` };
      const interrupted = failure ? `（后续行发送中断: ${failure}）` : '';
      return { ok: true, output: `已发送 ${refs.length} 条（短ID ${refs.join('、')}）${interrupted}。`, sentToUser: true };
    }
    case 'stay_silent':
      return { ok: true, output: '本轮保持沉默。', staySilent: true };
    case 'recall_message': {
      const ref = String(args.message_ref ?? '').trim();
      const entry = context.repo.findEntryByRef(context.scopeType, context.scopeId, ref);
      if (!entry || entry.message_id === null || entry.message_id === undefined) {
        return { ok: false, output: `没有找到短ID ${ref} 对应的消息。` };
      }
      const selfAuthored = String(entry.user_id) === String(context.bot.selfId);
      if (!selfAuthored) {
        // Recalling someone else's message needs admin/owner powers in a group.
        if (context.scopeType !== 'group') return { ok: false, output: '私聊里撤不了对方的消息，只能撤回自己的。' };
        const roles = context.getGroupRoles ? await context.getGroupRoles() : null;
        if (!roles || roles.botRole === 'member') return { ok: false, output: '我不是这个群的管理员，撤不了别人的消息。' };
      }
      try {
        await context.bot.recallMessage(entry.message_id);
        return { ok: true, output: `已撤回短ID ${ref} 的消息。` };
      } catch (error) {
        return { ok: false, output: `撤回失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'group_mute': {
      if (context.scopeType !== 'group') return { ok: false, output: '禁言只在群聊里可用。' };
      const roles = context.getGroupRoles ? await context.getGroupRoles() : null;
      if (!roles || roles.botRole === 'member') return { ok: false, output: '我不是这个群的管理员，没有禁言权限。' };
      const qq = Number(String(args.qq ?? '').trim());
      if (!Number.isInteger(qq) || qq <= 0) return { ok: false, output: 'qq 参数不是有效的 QQ 号。' };
      if (qq === Number(context.bot.selfId)) return { ok: false, output: '不能禁言我自己。' };
      if (qq === Number(context.masterQq)) return { ok: false, output: '号主不能被禁言。' };
      if (roles.owner !== null && qq === roles.owner) return { ok: false, output: '群主不能被禁言。' };
      if (roles.botRole === 'admin' && roles.admins.includes(qq)) {
        return { ok: false, output: '我只是管理员，禁言不了别的管理员（要群主才行）。' };
      }
      const minutes = Math.min(Math.max(0, Math.trunc(Number(args.minutes ?? 0) || 0)), 43200);
      try {
        await context.bot.setGroupBan(Number(context.scopeId), qq, minutes * 60);
        const name = roles.names[qq] ?? String(qq);
        return {
          ok: true,
          output: minutes === 0 ? `已解除 ${name}(${qq}) 的禁言。` : `已禁言 ${name}(${qq}) ${minutes} 分钟。`,
        };
      } catch (error) {
        return { ok: false, output: `禁言操作失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'memory_list': {
      const notes = context.repo.recentNotes(context.scopeType, context.scopeId, 20);
      return { ok: true, output: notes.length > 0 ? notes.map((note, i) => `${i + 1}. ${note}`).join('\n') : '（暂无备忘）' };
    }
    case 'memory_write': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.repo.addNote(context.scopeType, context.scopeId, text);
      return { ok: true, output: '已记录。' };
    }
    case 'notify_master': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.notifyMaster({
        text,
        requestType: String(args.request_type ?? '') || undefined,
        fromScope: `${context.scopeType}:${context.scopeId}`,
      });
      return { ok: true, output: '已上报主AI。' };
    }
    case 'create_task': {
      const kind = String(args.kind ?? '').trim();
      const note = String(args.note ?? '').trim();
      if (!note) return { ok: false, output: 'note 为空。' };
      const originScope = `${context.scopeType}:${context.scopeId}`;
      if (kind === 'set_alarm') {
        const at = parseRelativeTime(String(args.at ?? ''));
        if (at === null) return { ok: false, output: 'at 格式无法解析（支持 Unix 秒或 +90s/+5m/+2h/+1d）。' };
        if (at <= Date.now() / 1000) return { ok: false, output: '闹钟时间必须在未来。' };
        const taskId = context.tasks.scheduleAlarm(originScope, at, note);
        return { ok: true, output: `闹钟已创建（task ${taskId}），到点会在本会话提醒你。` };
      }
      if (kind === 'recurring_task') {
        const interval = parseInterval(String(args.every ?? ''));
        if (interval === null) return { ok: false, output: 'every 格式无法解析（支持 +30m/+6h/+1d，最短 1 分钟）。' };
        const taskId = context.tasks.scheduleRecurring(originScope, interval, note);
        return { ok: true, output: `周期任务已创建（task ${taskId}），每 ${formatInterval(interval)} 触发一次；用 cancel_task 可停止。` };
      }
      return { ok: false, output: `暂不支持的任务类型: ${kind || '(空)'}（可用: set_alarm / recurring_task）` };
    }
    case 'list_tasks': {
      const tasks = context.isMaster ? context.tasks.list() : context.tasks.list(`${context.scopeType}:${context.scopeId}`);
      if (tasks.length === 0) return { ok: true, output: '（没有待触发的任务）' };
      const lines = tasks.slice(0, 20).map((task) => {
        const when = new Date(task.runAt * 1000);
        const pad = (value: number) => String(value).padStart(2, '0');
        const timeText = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}`;
        const kind = task.kind === 'recurring' ? `周期(每${formatInterval(task.intervalSeconds ?? 0)})` : '闹钟';
        const scope = context.isMaster ? ` ${task.originScope}` : '';
        return `- ${task.taskId} [${kind}]${scope} ${timeText}：${task.note.slice(0, 60)}`;
      });
      return { ok: true, output: `待触发任务（${tasks.length}）：\n${lines.join('\n')}` };
    }
    case 'cancel_task': {
      const taskId = String(args.task_id ?? '').trim();
      if (!taskId) return { ok: false, output: 'task_id 为空。' };
      const task = context.tasks.find(taskId);
      if (!task) return { ok: false, output: `没有找到任务 ${taskId}。` };
      if (!context.isMaster && task.originScope !== `${context.scopeType}:${context.scopeId}`) {
        return { ok: false, output: `任务 ${taskId} 不属于本会话，如需取消请通过 notify_master 找主AI。` };
      }
      if (!context.tasks.cancel(taskId)) return { ok: false, output: `任务 ${taskId} 已触发或已取消。` };
      return { ok: true, output: `已取消任务 ${taskId}。` };
    }
    case 'view_image': {
      let refs = context.triggerImageRefs;
      let sourceNote = '本轮消息';
      const ref = String(args.message_ref ?? '').trim();
      if (ref) {
        const normalized = ref.replace(/^\[#?|\]$/g, '');
        const entry = context.repo.findEntryByRef(context.scopeType, context.scopeId, normalized);
        if (!entry) return { ok: false, output: `没有找到短ID ${ref} 对应的消息。` };
        refs = Array.isArray(entry.image_refs) ? (entry.image_refs as string[]) : [];
        sourceNote = `消息 ${ref}`;
        if (refs.length === 0) return { ok: false, output: `消息 ${ref} 里没有图片。` };
      }
      if (refs.length === 0) return { ok: false, output: '本轮消息没有图片；如果图在历史消息里，传 message_ref。' };
      const index = Math.max(1, Math.trunc(Number(args.index ?? 1)) || 1);
      if (index > refs.length) {
        return { ok: false, output: `${sourceNote}只有 ${refs.length} 张图，index ${index} 超出范围。` };
      }
      const question = String(args.question ?? '').trim() || '请客观描述这张图片的内容：主体、场景、文字信息（如有）、值得注意的细节。';
      try {
        const description = await context.describeImage(refs[index - 1], question);
        return { ok: true, output: `图片内容（${sourceNote}第 ${index}/${refs.length} 张）：\n${description}` };
      } catch (error) {
        console.warn(`[vision] view_image failed scope=${context.scopeType}:${context.scopeId} url=${String(refs[index - 1]).slice(0, 120)}: ${error instanceof Error ? error.message : String(error)}`);
        return { ok: false, output: `图片解析失败: ${error instanceof Error ? error.message : String(error)}（这通常是上游临时抖动，请直接再调用一次本工具重试；连续两次都失败再告诉用户看不了）` };
      }
    }
    case 'send_image': {
      const file = String(args.file ?? '').trim();
      if (!file) return { ok: false, output: 'file 为空。' };
      if (!/^(https?:\/\/|base64:\/\/|\/)/i.test(file)) {
        return { ok: false, output: 'file 必须是 http(s) 链接、base64:// 数据或绝对路径。' };
      }
      // 本地绝对路径只放行本会话工作区内的文件——否则一条注入就能让 NapCat 把
      // secrets.json 之类的任意文件读出去发掉。
      if (file.startsWith('/')) {
        const abs = resolveAbs(file);
        if (abs !== context.workspace.root && !abs.startsWith(context.workspace.root + pathSep)) {
          return { ok: false, output: '本地路径只支持本会话工作区内的文件（先把图 download_file 下来再发）。' };
        }
      }
      try {
        const result = await context.bot.sendImage(context.scopeType as 'group' | 'private', Number(context.scopeId), file);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
        context.repo.appendMessage(context.scopeType, context.scopeId, {
          user_id: context.bot.selfId,
          nickname: context.bot.selfNickname || 'AI',
          text: '[图片]',
          raw_message: `[CQ:image,file=${file.slice(0, 120)}]`,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: '',
          image_refs: /^https?:\/\//i.test(file) ? [file] : [],
        });
        return { ok: true, output: `图片已发送（短ID ${ref}）。`, sentToUser: true };
      } catch (error) {
        return { ok: false, output: `图片发送失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'send_tts': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      if (text.length > 300) {
        return { ok: false, output: `text 有 ${text.length} 字，超过 300 字上限——请拆成几段分别调用。` };
      }
      // 中英文情感名都收（模型偶尔写中文标签）
      const EMOTIONS: Record<string, string> = {
        default: 'default', 默认: 'default', 对话: 'default',
        narration: 'narration', 叙述: 'narration', 旁白: 'narration',
        angry: 'angry', 愤怒: 'angry', fear: 'fear', 恐惧: 'fear', pain: 'pain', 疼痛: 'pain',
      };
      const emotionRaw = String(args.emotion ?? 'default').trim().toLowerCase();
      const emotion = EMOTIONS[emotionRaw];
      if (emotion === undefined) {
        return { ok: false, output: `emotion「${emotionRaw}」不支持。可用：default（对话）/ narration（叙述）/ angry（愤怒）/ fear（恐惧）/ pain（疼痛）。` };
      }
      let speed = Number(args.speed ?? 1);
      if (!Number.isFinite(speed)) speed = 1;
      speed = Math.min(2, Math.max(0.5, speed));
      let pause = Number(args.pause ?? 0.3);
      if (!Number.isFinite(pause)) pause = 0.3;
      pause = Math.min(2, Math.max(0, pause));
      // 默认 0.6：主人 A/B/C 对照后调定的「日常语音消息」档（0.7 播音感重、0.3 太平）
      let temperature = Number(args.temperature ?? 0.6);
      if (!Number.isFinite(temperature)) temperature = 0.6;
      temperature = Math.min(2, Math.max(0.1, temperature));
      try {
        spendNetworkBudget(context);
        const abs = context.workspace.resolvePath(`tts/tts-${Date.now()}.wav`);
        const url = `${ttsBaseUrl}/tts?text=${encodeURIComponent(text)}&emotion=${emotion}&speed=${speed}&fragment_interval=${pause}&temperature=${temperature}`;
        const { bytes, contentType } = await downloadToFile(url, abs, 20 * 1024 * 1024, { allowInternal: true, timeoutMs: ttsTimeoutMs });
        // 校验 RIFF/WAVE 头：服务异常时可能回 JSON 错误体，别把错误当语音发出去
        const head = readFileSync(abs).subarray(0, 64);
        const isWav = bytes > 44 && head.subarray(0, 4).toString('latin1') === 'RIFF' && head.subarray(8, 12).toString('latin1') === 'WAVE';
        if (!isWav) {
          const body = readFileSync(abs, 'utf-8').slice(0, 300);
          return { ok: false, output: `TTS 服务返回的不是音频（${bytes}B, ${contentType}）：${body}。多半是服务或上游抖动，稍后重试一次；仍失败就改用 send_message 发文字。` };
        }
        const sampleRate = head.readUInt32LE(24);
        const bitsPerSample = head.readUInt16LE(34);
        const bytesPerSec = sampleRate * (bitsPerSample / 8) || 32_000;
        const duration = Math.round(bytes / bytesPerSec);
        const ttsTarget = (await spoolToUrl(abs, 'voice.wav')) ?? `file://${abs}`;
        const result = await context.bot.sendRecord(context.scopeType as 'group' | 'private', Number(context.scopeId), ttsTarget);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
        context.repo.appendMessage(context.scopeType, context.scopeId, {
          user_id: context.bot.selfId,
          nickname: context.bot.selfNickname || 'AI',
          text: '[语音]',
          raw_message: `[CQ:record,file=${abs.slice(-40)}] ${text.slice(0, 60)}`,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: '',
        });
        return { ok: true, output: `语音已发送（约 ${duration} 秒，短ID ${ref}）。`, sentToUser: true };
      } catch (error) {
        return { ok: false, output: `语音发送失败: ${error instanceof Error ? error.message : String(error)}。可重试一次；仍失败就改用 send_message 发文字。` };
      }
    }
    case 'send_video': {
      const url = String(args.url ?? '').trim();
      if (!/^https?:\/\//i.test(url)) {
        return { ok: false, output: 'url 必须是 http(s) 视频直链。' };
      }
      try {
        spendNetworkBudget(context);
        const abs = context.workspace.resolvePath(`videos/video-${Date.now()}.mp4`);
        // B 站视频 CDN 校验 Referer（实测缺失 403），识别 CDN 域名自动补浏览器头
        const headers = /bilivideo|akamaized\.net|hdslb\.com/i.test(url)
          ? {
            referer: 'https://www.bilibili.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
          }
          : undefined;
        const { bytes, contentType } = await downloadToFile(url, abs, 150 * 1024 * 1024, { headers, timeoutMs: 300_000 });
        const head = readFileSync(abs).subarray(0, 16).toString('latin1');
        if (bytes < 100_000 || head.startsWith('<') || head.startsWith('{')) {
          const body = readFileSync(abs, 'utf-8').slice(0, 200);
          return { ok: false, output: `下载到的不是有效视频（${bytes}B, ${contentType}）：${body}。多半是直链过期或被风控——用 bili_url 重新取一条再发。` };
        }
        // NapCat 远端（反向 WS）看不到服务器 file:// 路径——经 /media/ 外吐给它拉
        const sendTarget = (await spoolToUrl(abs, 'video.mp4')) ?? `file://${abs}`;
        const result = await context.bot.sendVideo(context.scopeType as 'group' | 'private', Number(context.scopeId), sendTarget);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
        context.repo.appendMessage(context.scopeType, context.scopeId, {
          user_id: context.bot.selfId,
          nickname: context.bot.selfNickname || 'AI',
          text: '[视频]',
          raw_message: `[CQ:video,file=${abs.slice(-40)}] ${url.slice(0, 80)}`,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: '',
        });
        lastVideoByScope.set(`${context.scopeType}:${context.scopeId}`, abs);
        return { ok: true, output: `视频已发送（${(bytes / 1048576).toFixed(1)}MB，短ID ${ref}）。已存工作区 ${abs}——要自己看画面内容用 watch_video。`, sentToUser: true };
      } catch (error) {
        return { ok: false, output: `视频发送失败: ${error instanceof Error ? error.message : String(error)}。太大可换 360P 直链（B 站 bili_url 的 qn=16）再试一次。` };
      }
    }
    case 'watch_video': {
      let abs = '';
      const fileArg = String(args.file ?? '').trim();
      const urlArg = String(args.url ?? '').trim();
      if (fileArg !== '') {
        // 绝对路径须在工作区内；相对路径按工作区根解析（send_video 输出是绝对路径，模型也常偷懒写相对）
        abs = fileArg.startsWith('/') ? resolveAbs(fileArg) : context.workspace.resolvePath(fileArg);
        if (abs !== context.workspace.root && !abs.startsWith(context.workspace.root + pathSep)) {
          return { ok: false, output: 'file 只支持本会话工作区内的视频（用 send_video 下载过的那个）。' };
        }
      } else if (urlArg !== '') {
        if (!/^https?:\/\//i.test(urlArg)) return { ok: false, output: 'url 必须是 http(s) 直链。' };
        try {
          spendNetworkBudget(context);
          abs = context.workspace.resolvePath(`videos/watch-${Date.now()}.mp4`);
          const headers = /bilivideo|akamaized\.net|hdslb\.com/i.test(urlArg)
            ? { referer: 'https://www.bilibili.com/', 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36' }
            : undefined;
          await downloadToFile(urlArg, abs, 150 * 1024 * 1024, { headers, timeoutMs: 300_000 });
          lastVideoByScope.set(`${context.scopeType}:${context.scopeId}`, abs);
        } catch (error) {
          return { ok: false, output: `视频下载失败: ${error instanceof Error ? error.message : String(error)}` };
        }
      } else {
        abs = lastVideoByScope.get(`${context.scopeType}:${context.scopeId}`) ?? '';
        if (abs === '') return { ok: false, output: '本会话还没有下载过视频。先 send_video 或 bili_url+send_video 下载，再 watch_video；或直接给 url。' };
      }
      // 时刻解析：逗号/空白分隔的秒数，去重排序，钳到 [0, 时长)，最多 10 帧
      const duration = await videoDuration(abs);
      const rawMoments = [...String(args.moments ?? '').matchAll(/\d+(?:\.\d+)?/g)].map((m) => Number(m[0]));
      const maxT = duration === null ? Number.MAX_SAFE_INTEGER : Math.max(0, duration - 0.5);
      let moments = [...new Set(rawMoments.map((t) => Math.min(Math.max(t, 0), maxT)))].sort((a, b) => a - b).slice(0, 10);
      if (moments.length === 0) {
        if (duration === null) {
          return { ok: false, output: '视频时长探测失败，moments 必须手动指定（如 0,30,120）。' };
        }
        moments = [1, 2, 3, 4, 5].map((k) => Math.round((duration * k) / 6));
      }
      const question = String(args.question ?? '').trim() || '这是一张由多个视频帧从上到下拼成的长图，每帧左上角有 t=秒数 水印。请按帧顺序客观描述每帧画面：主体、场景、动作、文字信息（如有）。';
      try {
        const workDir = context.workspace.resolvePath(`videos/frames-${Date.now()}`);
        const tiled = await extractFrameTiled(abs, moments, workDir);
        const base64 = readFileSync(tiled).toString('base64');
        const description = await context.describeImage(`data:image/jpeg;base64,${base64}`, question);
        return { ok: true, output: `视频画面（${moments.length} 帧：t=${moments.map((t) => Math.round(t)).join('/')}s，长图从上到下）：\n${description}` };
      } catch (error) {
        return { ok: false, output: `看视频失败: ${error instanceof Error ? error.message : String(error)}。可减少帧数（moments 给 2-3 个）重试一次。` };
      }
    }
    case 'send_sticker': {
      const query = String(args.query ?? '').trim();
      if (!query) return { ok: false, output: 'query 为空。' };
      const list = loadStickerIndex();
      if (list.length === 0) {
        return { ok: false, output: '表情包索引还没建好（收藏表情还没同步打标完，启动后会自动同步），先用文字回，别硬发表情。' };
      }
      let pick: StickerEntry | undefined;
      if (/^(随机|随便|来一张|来一个)/.test(query)) {
        pick = list[Math.floor(Math.random() * list.length)];
      } else {
        const scored = list
          .map((entry) => ({ entry, score: stickerScore(entry, query) }))
          .sort((a, b) => b.score - a.score);
        const best = scored[0];
        if (!best || best.score <= 0) {
          const tags = [...new Set(list.flatMap((entry) => entry.tags))];
          return { ok: false, output: `表情库里没有和「${query}」对得上的。可用关键词参考：${tags.slice(0, 60).join('、')}${tags.length > 60 ? ' …' : ''}。换个词再试。` };
        }
        // 同分里随机挑一张，避免每次都发同一张
        const pool = scored.filter((item) => item.score === best.score).map((item) => item.entry);
        pick = pool[Math.floor(Math.random() * pool.length)];
      }
      try {
        const result = await context.bot.sendSticker(context.scopeType as 'group' | 'private', Number(context.scopeId), pick.url);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
        context.repo.appendMessage(context.scopeType, context.scopeId, {
          user_id: context.bot.selfId,
          nickname: context.bot.selfNickname || 'AI',
          text: '[表情]',
          raw_message: `[CQ:image,subType=1] ${pick.desc}`,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: '',
        });
        return { ok: true, output: `已发表情：${pick.desc}（短ID ${ref}）。`, sentToUser: true };
      } catch (error) {
        return { ok: false, output: `表情发送失败: ${error instanceof Error ? error.message : String(error)}。可重试一次；仍失败就改用文字。` };
      }
    }
    case 'sticker_refresh': {
      if (!context.isMaster) return { ok: false, output: 'sticker_refresh 仅主AI可用。' };
      const result = await refreshStickerIndex(context.bot, context.describeImage);
      return { ok: result.ok, output: result.message };
    }
    case 'mc2d_status': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      try {
        return { ok: true, output: await client.status() };
      } catch (error) {
        return { ok: false, output: `进服/查看失败: ${error instanceof Error ? error.message : String(error)}。检查 127.0.0.1:6601（mc2d-tunnel）是否在线。` };
      }
    }
    case 'mc2d_move': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      const x = Number(args.x);
      if (!Number.isFinite(x)) return { ok: false, output: 'x 必须是数字（目标方块列 0-399）。' };
      try {
        const result = await client.moveTo(x);
        return {
          ok: true,
          output: result.reached
            ? `走到目标附近了，现在在(${result.x},${result.y})。`
            : `还没走到，现在在(${result.x},${result.y})——可能被地形卡住或目标太远。可以再调一次 mc2d_move，或先 mc2d_dig 挖开挡路的方块。`,
        };
      } catch (error) {
        return { ok: false, output: `走路失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_dig': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      const x = Number(args.x);
      const y = Number(args.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return { ok: false, output: 'x/y 必须是数字（方块坐标）。' };
      try {
        return { ok: true, output: await client.dig(x, y) };
      } catch (error) {
        return { ok: false, output: `挖掘失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_place': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      const x = Number(args.x);
      const y = Number(args.y);
      const block = Number(args.block);
      if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(block)) {
        return { ok: false, output: 'x/y/block 必须都是数字。' };
      }
      try {
        return { ok: true, output: await client.place(x, y, block) };
      } catch (error) {
        return { ok: false, output: `放置失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_attack': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      const mobId = args.mob_id === undefined || args.mob_id === '' ? undefined : Number(args.mob_id);
      const damage = args.damage === undefined || args.damage === '' ? 4 : Number(args.damage);
      if (mobId !== undefined && !Number.isFinite(mobId)) return { ok: false, output: 'mob_id 必须是数字。' };
      try {
        return { ok: true, output: await client.attack(mobId, Number.isFinite(damage) ? damage : 4) };
      } catch (error) {
        return { ok: false, output: `攻击失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_say': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      try {
        return { ok: true, output: await client.say(String(args.message ?? '')) };
      } catch (error) {
        return { ok: false, output: `游戏内发言失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_view': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      const question = String(args.question ?? '').trim()
        || '这是 2D 我的世界（横版侧视图）当前画面。黄色带白点的小人是你自己，蓝色是其他玩家，粉色是猪，灰/绿色是骷髅/僵尸，亮绿色是苦力怕。请客观描述：你站在什么地形上、周围方块（草/泥土/石头/原木/矿）、最近的生物或玩家在你的哪一侧、有没有危险。';
      try {
        const png = await client.viewPng();
        const description = await context.describeImage(`data:image/png;base64,${png.toString('base64')}`, question);
        return { ok: true, output: `游戏画面（${client.positionText()}）：\n${description}` };
      } catch (error) {
        return { ok: false, output: `看游戏画面失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'mc2d_leave': {
      if (!context.isMaster) return { ok: false, output: 'mc2d 工具仅主AI可用。' };
      const client = mc2dClient();
      if (client === null) return { ok: false, output: 'MC2D 未启用（config.json 的 mc2d.enabled=false）。' };
      client.leave();
      return { ok: true, output: '已离开 MC2D 服务器（下次调用 mc2d_* 会重新进服）。' };
    }
    case 'mc_view': {
      const scopeKey = `${context.scopeType}:${context.scopeId}`;
      if (!domainScopeHasBuiltin(scopeKey, 'mc_view')) {
        return { ok: false, output: 'mc_view 是 Minecraft 中枢（domain:mc）的领域工具：请主AI 用 delegate_to_child 委派 MC 中枢执行。' };
      }
      const radius = Math.max(8, Math.min(32, Math.round(Number(args.radius ?? 20)) || 20));
      const question = String(args.question ?? '');
      try {
        return { ok: true, output: await mcView(question, radius, context.describeImage) };
      } catch (error) {
        return { ok: false, output: `看世界失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'trigger_config': {
      const action = String(args.action ?? '').trim();
      const agent = context.repo.getOrCreateAgent(context.scopeType, context.scopeId);
      switch (action) {
        case 'set_rate': {
          const rate = Number(args.rate);
          if (!Number.isFinite(rate) || rate < 0 || rate > 1) {
            return { ok: false, output: 'rate 必须是 0~1 的数字（如 0.05）。' };
          }
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerRate: rate });
          return { ok: true, output: `随机接话率已调整为 ${(rate * 100).toFixed(1)}%（立即生效，跨重启保存）。` };
        }
        case 'add_word': {
          const word = String(args.word ?? '').trim();
          if (!word || word.length > 20) return { ok: false, output: 'word 为空或超过 20 字。' };
          if (agent.triggerWords.some((existing) => existing.toLowerCase() === word.toLowerCase())) {
            return { ok: true, output: `触发词「${word}」已经存在。` };
          }
          const triggerWords = [...agent.triggerWords, word];
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerWords });
          return { ok: true, output: `已添加触发词「${word}」，当前共 ${triggerWords.length} 个：${triggerWords.join('、')}` };
        }
        case 'remove_word': {
          const word = String(args.word ?? '').trim();
          if (!word) return { ok: false, output: 'word 为空。' };
          const triggerWords = agent.triggerWords.filter((existing) => existing.toLowerCase() !== word.toLowerCase());
          if (triggerWords.length === agent.triggerWords.length) {
            return { ok: false, output: `没有触发词「${word}」，当前触发词：${agent.triggerWords.join('、') || '（无）'}` };
          }
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerWords });
          return { ok: true, output: `已删除触发词「${word}」，剩余：${triggerWords.join('、') || '（无）'}` };
        }
        case 'list':
          return {
            ok: true,
            output:
              `当前触发设置：随机接话率 ${(agent.triggerRate * 100).toFixed(1)}%，`
              + `触发词（${agent.triggerWords.length}）：${agent.triggerWords.join('、') || '（无）'}。`,
          };
        default:
          return { ok: false, output: `未知 action: ${action || '(空)'}，可用: set_rate / add_word / remove_word / list` };
      }
    }
    case 'memory_search': {
      const query = String(args.query ?? '').trim();
      if (!query) return { ok: false, output: 'query 为空。' };
      const scope = String(args.scope ?? 'current').trim().toLowerCase();
      const scopeKeys = scope === 'all' ? [] : [`${context.scopeType}:${context.scopeId}`];
      const days = Number(args.days ?? 0);
      const from = Number.isFinite(days) && days > 0 ? Date.now() / 1000 - days * 86400 : undefined;
      const limit = Number(args.limit ?? 12);
      const hits = await context.archive.search({
        query,
        scopeKeys,
        userId: Number.isFinite(Number(args.user_id)) && args.user_id !== undefined ? Number(args.user_id) : undefined,
        from,
        limit,
      });
      if (hits.length === 0) {
        return { ok: true, output: `没有找到与「${query}」相关的历史消息。可以换更短的关键词、去掉引号短语，或扩大 days/scope 再试。` };
      }
      const lines = hits.map((hit) => {
        const parts = [
          ...hit.contextBefore.map((line) => `  上文 ${line}`),
          `▶ ${hit.line}`,
          ...hit.contextAfter.map((line) => `  下文 ${line}`),
        ];
        return parts.join('\n');
      });
      return { ok: true, output: `命中 ${hits.length} 条（按时间顺序，▶ 是命中行）：\n${lines.join('\n')}` };
    }
    case 'intel_report': {
      const subject = Number(args.fact_subject_id);
      const text = String(args.fact_text ?? '').trim();
      if (!Number.isFinite(subject) || subject <= 0) return { ok: false, output: 'fact_subject_id 必须是有效的 QQ 号。' };
      if (!text) return { ok: false, output: 'fact_text 为空。' };
      const added = context.relations.addFact({
        subject,
        category: String(args.fact_category ?? 'other') as never,
        text,
        sourceScope: `${context.scopeType}:${context.scopeId}`,
        sourceTool: 'intel_report',
        confidence: Number.isFinite(Number(args.confidence)) ? Number(args.confidence) : undefined,
      });
      if (added === null) return { ok: false, output: '情报无效（内容为空或主体不合法）。' };
      const person = context.relations.person(subject);
      const activeCount = person ? person.facts.filter((fact) => fact.status === 'active').length : 1;
      return { ok: true, output: `已录入全局关系网络（该人物现有 ${activeCount} 条情报）。` };
    }
    case 'usage_stats': {
      return { ok: true, output: formatUsageText(usageStats.snapshot()) };
    }
    case 'relation_query': {
      const userId = Number(args.user_id);
      const query = String(args.query ?? '').trim();
      const parts: string[] = [];
      if (Number.isFinite(userId) && userId > 0) {
        const card = context.relations.personCard(userId);
        parts.push(card ? `人物档案：\n${card}` : `关系网络里还没有 ${userId} 的档案。`);
      }
      if (query) {
        const facts = context.relations.searchFacts(query, 10);
        parts.push(
          facts.length > 0
            ? `情报命中：\n${facts.map((fact) => `- [${fact.id}] ${fact.subject}[${fact.category}] ${fact.text}`).join('\n')}`
            : `没有命中「${query}」的情报。`,
        );
      }
      if (args.list_members === true || (!Number.isFinite(userId) && !query)) {
        const members = context.relations.knownMembers(`${context.scopeType}:${context.scopeId}`);
        if (members.length === 0) {
          parts.push('本会话还没有已知成员记录。');
        } else {
          parts.push(`本会话已知成员（${members.length}）：\n${members
            .slice(-10)
            .map((person) => {
              const topFact = person.facts.find((fact) => fact.status === 'active');
              return `- ${person.canonicalName}(${person.userId})${topFact ? `：${topFact.text}` : ''}`;
            })
            .join('\n')}`);
        }
      }
      return { ok: true, output: parts.length > 0 ? parts.join('\n\n') : '请提供 user_id、query 或 list_members 之一。' };
    }
    case 'impression_write': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.relations.setScopeImpression(`${context.scopeType}:${context.scopeId}`, text);
      return { ok: true, output: '会话印象已更新。' };
    }
    case 'relation_write': {
      if (!context.isMaster) return { ok: false, output: 'relation_write 仅限主AI使用，请通过 notify_master 上报由主AI归档。' };
      return executeRelationWrite(args, context);
    }
    case 'scope_manage': {
      if (!context.isMaster) return { ok: false, output: 'scope_manage 仅限主AI使用。' };
      return executeScopeManage(args, context);
    }
    case 'delegate_to_child': {
      if (!context.isMaster) return { ok: false, output: 'delegate_to_child 仅限主AI使用，需要跨会话协调请调用 notify_master。' };
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const instruction = String(args.instruction ?? '').trim();
      if (!targetType || !targetId || !instruction) return { ok: false, output: 'target_scope_type / target_scope_id / instruction 都不能为空。' };
      if (!['group', 'private', 'domain', 'bili', 'mcclient'].includes(targetType)) {
        return { ok: false, output: `target_scope_type 只能是 group / private / domain / bili / mcclient，收到: ${targetType}` };
      }
      if (targetType === 'domain' && domainDefForScope(`domain:${targetId}`) === null) {
        return {
          ok: false,
          output: `没有注册 domain:${targetId} 这个领域中枢。现有领域: ${domainList()}。`,
        };
      }
      if (targetType === context.scopeType && targetId === context.scopeId) return { ok: false, output: '目标是当前会话（主AI自己），直接处理即可，无需委派。' };
      const taskId = context.delegateToChild(targetType, targetId, instruction);
      return { ok: true, output: `已委派 ${targetType}:${targetId} 的子AI（task ${taskId}），它完成后会通过 notify_master 回报。` };
    }
    case 'domain_message': {
      const callerScope = `${context.scopeType}:${context.scopeId}`;
      if (!context.isMaster && context.scopeType !== 'domain') {
        return { ok: false, output: 'domain_message 仅限主AI与领域中枢使用；下级 AI 请用 notify_master 经汇报链上报。' };
      }
      const target = String(args.target ?? '').trim();
      const content = String(args.content ?? '').trim();
      if (!target || !content) return { ok: false, output: 'target / content 都不能为空。' };
      const requestType = String(args.request_type ?? '').trim();
      if (target === 'master') {
        context.notifyMaster({
          text: content,
          requestType: requestType || 'peer_chat',
          fromScope: callerScope,
        });
        return { ok: true, output: '已直发最高级中枢（主AI）。' };
      }
      if (domainDefForScope(`domain:${target}`) === null) {
        return { ok: false, output: `没有注册 domain:${target} 这个领域中枢。现有领域: ${domainList()}；找最高级中枢传 target="master"。` };
      }
      if (context.scopeType === 'domain' && context.scopeId === target) {
        return { ok: false, output: '目标就是自己，直接处理即可。' };
      }
      const text = `[主级互聊 from ${callerScope}${requestType ? ` type=${requestType}` : ''}]\n${content}`;
      context.peerInject(`domain:${target}`, text, { peer_chat: callerScope });
      return { ok: true, output: `已发给 domain:${target}（主级互聊通道），它会在自己的会话里收到。` };
    }
    case 'message_scope': {
      if (!context.isMaster) return { ok: false, output: 'message_scope 仅限主AI使用。' };
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const content = stripThinking(String(args.content ?? '')).trim();
      if (!targetType || !targetId || !content) return { ok: false, output: 'target_scope_type / target_scope_id / content 都不能为空。' };
      if (targetType !== 'group' && targetType !== 'private') return { ok: false, output: `target_scope_type 只能是 group 或 private，收到: ${targetType}` };
      try {
        const output = await context.sendToScope(targetType, targetId, content);
        return { ok: true, output };
      } catch (error) {
        return { ok: false, output: `发送失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'kb_list': {
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const kb = context.kb;
      const scopeKey = `${context.scopeType}:${context.scopeId}`;
      const visible = kb.visibleFor(scopeKey);
      const tagFilter = Array.isArray(args.tags) ? args.tags.map((tag) => String(tag).trim()).filter(Boolean) : [];
      let entries = visible;
      if (tagFilter.length > 0) {
        entries = entries.filter((entry) => tagFilter.every((tag) => entry.tags.includes(tag)));
      }
      const text = String(args.q ?? '').trim().toLowerCase();
      if (text) {
        entries = entries.filter(
          (entry) => entry.subject.toLowerCase().includes(text) || entry.content.toLowerCase().includes(text),
        );
      }
      const limit = Math.max(1, Math.min(100, Math.trunc(Number(args.limit ?? 20)) || 20));
      const page = entries.slice(0, limit);
      if (page.length === 0) {
        return { ok: true, output: visible.length === 0 ? '（知识库里没有挂载给本会话的条目）' : '（没有匹配的条目）' };
      }
      const lines = page.map((entry) => {
        const perm = kb.permissionFor(scopeKey, entry) ?? 'read';
        return `${entry.id}  ${entry.subject}  [${entry.tags.join(',') || '无标签'}]  可信度${entry.confidence}  ${perm}`;
      });
      const more = entries.length > page.length ? `\n（还有 ${entries.length - page.length} 条，可加大 limit 或加过滤条件）` : '';
      return { ok: true, output: `知识库（本会话可见 ${visible.length} 条）：\n${lines.join('\n')}${more}` };
    }
    case 'kb_read': {
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const scopeKey = `${context.scopeType}:${context.scopeId}`;
      const id = String(args.id ?? '').trim();
      const entry = context.kb.get(id);
      if (!entry) return { ok: false, output: `没有知识条目 ${id}（或已删除）。` };
      const perm = context.kb.permissionFor(scopeKey, entry);
      if (perm === null) return { ok: false, output: `条目 ${id}（${entry.subject}）没有挂载给本会话，不可读。` };
      const stamp = new Date(entry.updatedAt * 1000).toISOString().slice(0, 16).replace('T', ' ');
      return {
        ok: true,
        output:
          `${entry.subject}\n标签: ${entry.tags.join(',') || '（无）'}  可信度: ${entry.confidence}  来源: ${entry.source}  更新: ${stamp}\n`
          + `${entry.content}\n（条目 ID ${entry.id}，本会话权限 ${perm}）`,
      };
    }
    case 'kb_append': {
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const scopeKey = `${context.scopeType}:${context.scopeId}`;
      const subject = String(args.subject ?? '').trim();
      const content = String(args.content ?? '').trim();
      const tags = Array.isArray(args.tags) ? args.tags.map((tag) => String(tag).trim()).filter(Boolean) : [];
      if (!subject || !content) return { ok: false, output: 'subject / content 都不能为空。' };
      const confidence = args.confidence === undefined ? undefined : Math.max(0, Math.min(1, Number(args.confidence) || 0));
      try {
        if (context.isMaster) {
          const entry = context.kb.add({ subject, content, tags, source: `master:${scopeKey}`, confidence: confidence ?? 0.8 });
          return { ok: true, output: `已入库：${entry.id}  ${entry.subject}（主AI直录立即生效）` };
        }
        const pendingId = context.kb.propose(scopeKey, { subject, content, tags, confidence });
        return { ok: true, output: `已提交主AI复核（待审项 ${pendingId}），通过后入库生效。` };
      } catch (error) {
        return { ok: false, output: error instanceof Error ? error.message : String(error) };
      }
    }
    case 'kb_edit': {
      if (!context.isMaster) return { ok: false, output: 'kb_edit 仅主AI可用；下级 AI 的新知请用 kb_append 报备。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const id = String(args.id ?? '').trim();
      if (!context.kb.get(id)) return { ok: false, output: `没有知识条目 ${id}。` };
      const patch: { subject?: string; content?: string; tags?: string[]; source: string; confidence?: number } = {
        source: `master:${context.scopeType}:${context.scopeId}`,
      };
      if (args.subject !== undefined) patch.subject = String(args.subject);
      if (args.content !== undefined) patch.content = String(args.content);
      if (args.tags !== undefined) {
        patch.tags = Array.isArray(args.tags) ? args.tags.map((tag) => String(tag).trim()).filter(Boolean) : [];
      }
      if (args.confidence !== undefined) patch.confidence = Number(args.confidence);
      try {
        const entry = context.kb.revise(id, patch);
        return { ok: true, output: `已修订：${entry.id}  ${entry.subject}（ID 保留，挂载继续生效）` };
      } catch (error) {
        return { ok: false, output: error instanceof Error ? error.message : String(error) };
      }
    }
    case 'kb_delete': {
      if (!context.isMaster) return { ok: false, output: 'kb_delete 仅主AI可用。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const id = String(args.id ?? '').trim();
      try {
        const removed = context.kb.remove(id);
        return removed
          ? { ok: true, output: `已删除知识条目 ${id}，所有挂载方都不再可见。` }
          : { ok: false, output: `没有知识条目 ${id}。` };
      } catch (error) {
        return { ok: false, output: error instanceof Error ? error.message : String(error) };
      }
    }
    case 'kb_mount': {
      if (!context.isMaster) return { ok: false, output: 'kb_mount 仅主AI可用。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const target = String(args.target ?? '').trim();
      const permission = String(args.permission ?? '').trim() as KbPermission;
      const tags = Array.isArray(args.tags) ? args.tags.map((tag) => String(tag).trim()).filter(Boolean) : undefined;
      if (!target) return { ok: false, output: 'target 不能为空（会话 scope key 或 all）。' };
      if (permission !== 'read' && permission !== 'append' && permission !== 'write') {
        return { ok: false, output: `permission 只能是 read/append/write，收到: ${permission}` };
      }
      context.kb.grantMount(target, permission, tags);
      const scope = tags && tags.length > 0 ? `标签 [${tags.join(',')}] 范围` : '全部条目';
      return { ok: true, output: `已挂载：${target} → ${scope}，权限 ${permission}。` };
    }
    case 'kb_unmount': {
      if (!context.isMaster) return { ok: false, output: 'kb_unmount 仅主AI可用。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const target = String(args.target ?? '').trim();
      const tags = Array.isArray(args.tags) ? args.tags.map((tag) => String(tag).trim()).filter(Boolean) : undefined;
      if (!target) return { ok: false, output: 'target 不能为空。' };
      const removed = context.kb.revokeMount(target, tags);
      return removed
        ? { ok: true, output: `已撤销 ${target} 的挂载${tags && tags.length > 0 ? `（标签 [${tags.join(',')}] 范围）` : ''}。` }
        : { ok: false, output: `没有找到 ${target} 匹配的挂载。` };
    }
    case 'kb_pending': {
      if (!context.isMaster) return { ok: false, output: 'kb_pending 仅主AI可用。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const proposedBy = String(args.proposed_by ?? '').trim() || undefined;
      const items = context.kb.pendingFor(`${context.scopeType}:${context.scopeId}`, proposedBy);
      if (items.length === 0) return { ok: true, output: '（待审核队列为空）' };
      const lines = items.map((item) => {
        const stamp = new Date(item.proposedAt * 1000).toISOString().slice(0, 16).replace('T', ' ');
        return `${item.id}  ${item.entry.subject}  [${item.entry.tags.join(',') || '无标签'}]  ${item.proposedBy} @ ${stamp}`;
      });
      return { ok: true, output: `待审核 ${items.length} 条：\n${lines.join('\n')}\n逐条用 kb_review 决定 approve/reject。` };
    }
    case 'kb_review': {
      if (!context.isMaster) return { ok: false, output: 'kb_review 仅主AI可用。' };
      if (!context.kb) return { ok: false, output: '知识库未初始化。' };
      const id = String(args.id ?? '').trim();
      const action = String(args.action ?? '').trim();
      if (action !== 'approve' && action !== 'reject') {
        return { ok: false, output: `action 只能是 approve/reject，收到: ${action}` };
      }
      try {
        const entry = context.kb.review(id, action, `${context.scopeType}:${context.scopeId}`);
        return action === 'approve' && entry
          ? { ok: true, output: `已通过入库：${entry.id}  ${entry.subject}` }
          : { ok: true, output: `已驳回待审项 ${id}。` };
      } catch (error) {
        return { ok: false, output: error instanceof Error ? error.message : String(error) };
      }
    }
    default: {
      if (call.name === 'plugin_manage') {
        const registry = pluginRegistry();
        if (!registry) return { ok: false, output: '插件系统未初始化。' };
        const action = String(args.action ?? '').trim();
        try {
          if (action === 'reload') {
            registry.load();
            const infos = registry.listInfo();
            const good = infos.filter((info) => !info.error);
            const bad = infos.filter((info) => info.error);
            return {
              ok: true,
              output: `已重新扫描：${good.length} 个插件可用${bad.length > 0 ? `，${bad.length} 个加载失败（${bad.map((info) => `${info.name}: ${info.error}`).join('；')}）` : ''}。`,
            };
          }
          if (action === 'enable' || action === 'disable') {
            const name = String(args.name ?? '').trim();
            registry.setEnabled(name, action === 'enable');
            return { ok: true, output: `插件 ${name} 已${action === 'enable' ? '启用' : '停用'}（立即生效，重启后保持）。` };
          }
          // list
          const infos = registry.listInfo();
          if (infos.length === 0) return { ok: true, output: '插件目录是空的（data/plugins/<名>/plugin.json 声明式定义 HTTP API 工具，放好后用 reload 加载）。' };
          const lines = infos.map((info) => {
            if (info.error) return `✗ ${info.name}（加载失败: ${info.error}）`;
            return `${info.enabled ? '●' : '○'} ${info.name}${info.description ? ` — ${info.description}` : ''}\n  工具: ${info.tools.join('、')}`;
          });
          return { ok: true, output: `插件列表（●启用 ○停用）：\n${lines.join('\n')}` };
        } catch (error) {
          return { ok: false, output: `plugin_manage 失败: ${error instanceof Error ? error.message : String(error)}` };
        }
      }
      const actionResult = await executeActionTool(call, context);
      if (actionResult) return actionResult;
      const pluginResult = await pluginRegistry()?.execute(call.name, args, context);
      if (pluginResult) return pluginResult;
      return { ok: false, output: `未知或不可用的工具: ${call.name}，本次未执行。` };
    }
  }
}

function botIsSelfId(bot: NapcatBot, id: unknown): boolean {
  // Stubs in older tests may not implement isSelfId; fall back to selfId.
  return typeof bot.isSelfId === 'function' ? bot.isSelfId(id) : String(id) === String(bot.selfId);
}

function executeScopeManage(args: Record<string, unknown>, context: ToolContext): ToolResult {
  const action = String(args.action ?? '').trim();
  const repo = context.repo;

  const describe = (agent: { scopeType: string; scopeId: string; enabled: boolean; triggerRate: number; triggerWords: string[]; displayName: string }): string =>
    `${agent.scopeType}:${agent.scopeId}${agent.displayName ? `（${agent.displayName}）` : ''} `
    + `[${agent.enabled ? '开' : '关'}] 随机触发率=${agent.triggerRate} 触发词=[${agent.triggerWords.join('、')}]`;

  if (action === 'list') {
    const agents = repo.listAgents();
    if (agents.length === 0) return { ok: true, output: '还没有任何会话记录。' };
    return { ok: true, output: `共 ${agents.length} 个会话：\n${agents.map(describe).join('\n')}` };
  }

  const targetType = String(args.target_scope_type ?? '').trim();
  const targetId = String(args.target_scope_id ?? '').trim();
  if (!targetType || !targetId) return { ok: false, output: 'target_scope_type / target_scope_id 都不能为空。' };
  if (targetType !== 'group' && targetType !== 'private') return { ok: false, output: `target_scope_type 只能是 group 或 private，收到: ${targetType}` };

  // 主AI会话是对外生命线，关闭后主人就联系不上主AI了。
  const isMasterScope = targetType === 'private' && targetId === String(context.masterQq);

  switch (action) {
    case 'get': {
      const agent = repo.getOrCreateAgent(targetType, targetId);
      return { ok: true, output: describe(agent) };
    }
    case 'set_enabled': {
      if (typeof args.enabled !== 'boolean') return { ok: false, output: 'set_enabled 需要 enabled=true/false。' };
      if (isMasterScope && !args.enabled) return { ok: false, output: '主AI会话不能关闭——关了你就联系不上我了。' };
      repo.updateAgent(targetType, targetId, { enabled: args.enabled });
      const agent = repo.getOrCreateAgent(targetType, targetId);
      return { ok: true, output: `已${args.enabled ? '开启' : '关闭'}：${describe(agent)}` };
    }
    case 'set_trigger_rate': {
      const rate = Number(args.rate);
      if (!Number.isFinite(rate) || rate < 0 || rate > 1) return { ok: false, output: 'rate 必须是 0~1 的小数（如 0.05）。' };
      repo.updateAgent(targetType, targetId, { triggerRate: rate });
      const agent = repo.getOrCreateAgent(targetType, targetId);
      return { ok: true, output: `触发率已调整：${describe(agent)}` };
    }
    case 'add_trigger_words':
    case 'remove_trigger_words': {
      const words = Array.isArray(args.words) ? args.words.map((w) => String(w).trim()).filter(Boolean) : [];
      if (words.length === 0) return { ok: false, output: 'words 不能为空数组。' };
      const current = repo.getOrCreateAgent(targetType, targetId).triggerWords;
      const next = action === 'add_trigger_words'
        ? [...new Set([...current, ...words])]
        : current.filter((w) => !words.includes(w));
      if (next.length === 0) return { ok: false, output: '触发词不能清空——至少留一个，否则只能@触发。' };
      repo.updateAgent(targetType, targetId, { triggerWords: next });
      const agent = repo.getOrCreateAgent(targetType, targetId);
      return { ok: true, output: `触发词已更新：${describe(agent)}` };
    }
    default:
      return { ok: false, output: `未知 action: ${action}（支持 list / get / set_enabled / set_trigger_rate / add_trigger_words / remove_trigger_words）` };
  }
}

function executeRelationWrite(args: Record<string, unknown>, context: ToolContext): ToolResult {
  const action = String(args.action ?? '').trim();
  const relations = context.relations;
  switch (action) {
    case 'set_person_impression': {
      const userId = Number(args.user_id);
      const text = String(args.text ?? '').trim();
      if (!Number.isFinite(userId) || userId <= 0 || !text) return { ok: false, output: 'user_id 和 text 都不能为空。' };
      relations.setPersonImpression(userId, text);
      return { ok: true, output: `已更新 ${userId} 的人物印象。` };
    }
    case 'add_fact': {
      const userId = Number(args.user_id);
      const text = String(args.text ?? '').trim();
      if (!Number.isFinite(userId) || userId <= 0 || !text) return { ok: false, output: 'user_id 和 text 都不能为空。' };
      const added = relations.addFact({
        subject: userId,
        category: String(args.category ?? 'other') as never,
        text,
        sourceScope: `${context.scopeType}:${context.scopeId}`,
        sourceTool: 'master',
        confidence: Number.isFinite(Number(args.confidence)) ? Number(args.confidence) : 1.0,
      });
      if (added === null) return { ok: false, output: '情报无效（内容为空或主体不合法）。' };
      return { ok: true, output: `已为 ${userId} 追加情报（fact_id ${added.id}）。` };
    }
    case 'supersede_fact': {
      const factId = String(args.fact_id ?? '').trim();
      const text = String(args.text ?? '').trim();
      if (!factId || !text) return { ok: false, output: 'fact_id 和 text 都不能为空。' };
      const next = relations.supersedeFact(factId, text);
      if (next === null) return { ok: false, output: `没有找到情报 ${factId}。` };
      return { ok: true, output: `已用新表述取代旧情报（新 fact_id ${next.id}）。` };
    }
    case 'retract_fact': {
      const factId = String(args.fact_id ?? '').trim();
      if (!factId) return { ok: false, output: 'fact_id 为空。' };
      if (!relations.retractFact(factId)) return { ok: false, output: `没有找到情报 ${factId}。` };
      return { ok: true, output: `已撤回情报 ${factId}。` };
    }
    case 'set_scope_impression': {
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const text = String(args.text ?? '').trim();
      if (!targetType || !targetId || !text) return { ok: false, output: 'target_scope_type / target_scope_id / text 都不能为空。' };
      relations.setScopeImpression(`${targetType}:${targetId}`, text);
      return { ok: true, output: `已更新会话 ${targetType}:${targetId} 的印象。` };
    }
    default:
      return { ok: false, output: `未知 action: ${action || '(空)'}，可用: set_person_impression / add_fact / supersede_fact / retract_fact / set_scope_impression` };
  }
}

function parseRelativeTime(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const interval = parseInterval(trimmed, 1);
  if (interval !== null) return Date.now() / 1000 + interval;
  const parsed = Number(trimmed);
  if (Number.isFinite(parsed) && parsed > 1e9) return parsed;
  return null;
}

/** "+90s"/"+5m"/"+2h"/"+1d" → seconds (null if unparsable or below minSeconds). */
function parseInterval(value: string, minSeconds = 60): number | null {
  const relative = /^\+(\d+(?:\.\d+)?)([smhd])$/.exec(value.trim().toLowerCase());
  if (!relative) return null;
  const amount = Number(relative[1]);
  const unit = relative[2] === 's' ? 1 : relative[2] === 'm' ? 60 : relative[2] === 'h' ? 3600 : 86400;
  const seconds = amount * unit;
  return seconds >= minSeconds ? seconds : null;
}

function formatInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}天`;
  if (seconds % 3600 === 0) return `${seconds / 3600}小时`;
  if (seconds % 60 === 0) return `${seconds / 60}分钟`;
  return `${seconds}秒`;
}
