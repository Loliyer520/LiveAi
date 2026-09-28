/**
 * Autonomous-action tool set — the general-purpose capability layer. Instead
 * of hardcoding features (weather, novel excerpts, …), these primitives let
 * the model compose them itself inside one turn:
 *
 *   查天气      web_search → web_fetch
 *   发小说段落  download_file → file_search → file_read → send_file_excerpt
 *   收文件研究  download_qq_file → extract_archive → file_list/file_read
 *
 * All filesystem access is sandboxed to the per-scope workspace; all network
 * access goes through the SSRF-guarded helpers in net.ts. Schemas are merged
 * into the chat tool list by tools.ts; executeActionTool returns null for
 * tool names it does not own.
 */

import { appendFile, mkdir, readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import type { ToolSchema } from '../models/types.js';
import type { ToolContext, ToolResult } from './tools.js';
import { downloadToFile, webFetch, webSearch } from './net.js';
import { extractGzip, extractZip, isGzip, isZip } from './zip.js';
import { safeFilename } from './workspace.js';
import type { HistoryEntry } from './types.js';

const DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024;
const WRITE_MAX_CHARS = 200_000;
const READ_MAX_LINES = 200;
const READ_MAX_CHARS = 12_000;
const SEARCH_MAX_HITS = 30;
const EXCERPT_MAX_LINES = 30;
const EXCERPT_LINE_MAX_CHARS = 4_000;
const NETWORK_CALLS_PER_TURN = 8;

export const ACTION_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'web_search',
    description:
      '联网搜索，返回标题+链接+摘要。想找某个信息、某个公开 API 的地址时先搜，再用 web_fetch 打开链接取内容。',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '搜索关键词' },
        max_results: { type: 'number', description: '返回条数，默认 5，最多 10' },
      },
      required: ['query'],
    },
  },
  {
    name: 'web_fetch',
    description:
      '抓取一个 http(s) 网页或调用公开 API，返回状态码和正文（超长截断）。'
      + '支持 method/headers/body 调 REST 接口（body 传字符串，如 JSON）。'
      + '例：查天气可抓 https://wttr.in/北京?format=j1 。内网地址禁止访问。'
      + '要保存大文件（小说 txt、压缩包等）请改用 download_file。',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'http(s) 链接' },
        method: { type: 'string', description: 'GET/POST/…，默认 GET' },
        headers: { type: 'object', description: '请求头（可选）' },
        body: { type: 'string', description: '请求体（可选，非 GET 时）' },
      },
      required: ['url'],
    },
  },
  {
    name: 'download_file',
    description:
      '把 http(s) 资源下载到本会话工作区（上限 25MB），返回保存路径/大小/类型。下载后可用 file_read/file_search/extract_archive 处理。',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'http(s) 链接' },
        filename: { type: 'string', description: '保存文件名（可选，默认取 URL 最后一段）' },
      },
      required: ['url'],
    },
  },
  {
    name: 'download_qq_file',
    description:
      '下载聊天里收到的文件到工作区：传消息短ID（message_ref），取该消息携带的文件。历史里 [文件: 名称] 的消息才有文件。',
    parameters: {
      type: 'object',
      properties: {
        message_ref: { type: 'string', description: '带文件的消息短ID' },
        index: { type: 'number', description: '消息里第几个文件，默认 1' },
      },
      required: ['message_ref'],
    },
  },
  {
    name: 'file_list',
    description: '列出本会话工作区里的文件（相对路径 + 大小）。',
    parameters: {
      type: 'object',
      properties: { subdir: { type: 'string', description: '子目录（可选）' } },
    },
  },
  {
    name: 'file_read',
    description:
      '读工作区文件的一段内容，返回带行号的文本（from/to 为 1 起始的行号，单次最多 200 行）。看文件内容、定位段落用它。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '工作区内相对路径' },
        from: { type: 'number', description: '起始行（默认 1）' },
        to: { type: 'number', description: '结束行（默认 from+199）' },
      },
      required: ['path'],
    },
  },
  {
    name: 'file_search',
    description:
      '在工作区文件里按正则搜索，返回命中行号和该行内容（最多 30 条）。找小说章节/关键词位置用它，拿到行号后再 file_read 或 send_file_excerpt。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '工作区内相对路径' },
        pattern: { type: 'string', description: '正则表达式，如 第.{1,6}章' },
      },
      required: ['path', 'pattern'],
    },
  },
  {
    name: 'file_write',
    description: '往工作区写文本（草稿、笔记、整理结果），上限 20 万字符。append=true 追加。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '工作区内相对路径' },
        content: { type: 'string', description: '要写入的文本' },
        append: { type: 'boolean', description: 'true=追加，默认覆盖' },
      },
      required: ['path', 'content'],
    },
  },
  {
    name: 'extract_archive',
    description: '解压工作区里的 .zip / .gz 文件到同名目录（防路径越界，总量上限 64MB），返回解出的文件列表。',
    parameters: {
      type: 'object',
      properties: { path: { type: 'string', description: '工作区内压缩包相对路径' } },
      required: ['path'],
    },
  },
  {
    name: 'send_file_excerpt',
    description:
      '把工作区文件的指定行区间【原文】直接发给用户：每一行作为一条独立消息逐条发出，不需要你复述内容。'
      + '配合 file_search 定位行号后使用（如发小说选段）。单次最多 30 行；from/to 为 1 起始的行号（含两端）。',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '工作区内相对路径' },
        from: { type: 'number', description: '起始行（1 起始，含）' },
        to: { type: 'number', description: '结束行（含；最多 from+29）' },
      },
      required: ['path', 'from', 'to'],
    },
  },
];

const ACTION_TOOL_NAMES = new Set(ACTION_TOOL_SCHEMAS.map((schema) => schema.name));

/** Execute an autonomous-action tool; null when the name is not ours. */
export async function executeActionTool(
  call: { name: string; arguments: Record<string, unknown> },
  context: ToolContext,
): Promise<ToolResult | null> {
  if (!ACTION_TOOL_NAMES.has(call.name)) return null;
  const args = call.arguments ?? {};
  try {
    return await run(call.name, args, context);
  } catch (error) {
    return { ok: false, output: `${call.name} 失败: ${error instanceof Error ? error.message : String(error)}` };
  }
}

async function run(name: string, args: Record<string, unknown>, context: ToolContext): Promise<ToolResult> {
  switch (name) {
    case 'web_search': {
      spendNetwork(context);
      const query = String(args.query ?? '').trim();
      if (!query) return { ok: false, output: 'query 为空。' };
      const maxResults = Math.min(10, Math.max(1, Number(args.max_results ?? 5) || 5));
      const hits = await webSearch(query, maxResults);
      if (hits.length === 0) return { ok: true, output: `搜索「${query}」没有结果。` };
      const text = hits.map((hit, index) => `${index + 1}. ${hit.title}\n   ${hit.url}\n   ${hit.snippet}`).join('\n');
      return { ok: true, output: `搜索「${query}」结果：\n${text}` };
    }

    case 'web_fetch': {
      spendNetwork(context);
      const headers: Record<string, string> = {};
      if (args.headers && typeof args.headers === 'object') {
        for (const [key, value] of Object.entries(args.headers as Record<string, unknown>)) {
          headers[key] = String(value);
        }
      }
      const result = await webFetch(String(args.url ?? ''), {
        method: String(args.method ?? 'GET'),
        headers,
        body: args.body === undefined ? undefined : String(args.body),
      });
      const tail = result.truncated ? '\n…（正文过长已截断，完整内容可用 download_file 保存后 file_read 分段看）' : '';
      return { ok: true, output: `HTTP ${result.status} ${result.contentType}\n${result.body}${tail}` };
    }

    case 'download_file': {
      spendNetwork(context);
      const url = String(args.url ?? '').trim();
      let filename = String(args.filename ?? '').trim();
      if (!filename) {
        try {
          filename = new URL(url).pathname.split('/').filter(Boolean).pop() ?? '';
        } catch { /* fall through to safeFilename default */ }
      }
      filename = safeFilename(decodeURIComponent(filename), `download-${Date.now()}`);
      await context.workspace.ensure();
      const target = context.workspace.resolvePath(join('downloads', filename));
      const { bytes, contentType } = await downloadToFile(url, target, DOWNLOAD_MAX_BYTES);
      const rel = context.workspace.displayPath(target);
      return {
        ok: true,
        output: `已下载到 ${rel}（${formatBytes(bytes)}，${contentType || '未知类型'}）。`
          + '文本文件用 file_read/file_search 查看，压缩包用 extract_archive 解开。',
      };
    }

    case 'download_qq_file': {
      spendNetwork(context);
      const ref = String(args.message_ref ?? '').trim().replace(/^\[#?|\]$/g, '');
      const entry = context.repo.findEntryByRef(context.scopeType, context.scopeId, ref);
      if (!entry) return { ok: false, output: `没有找到短ID ${ref} 对应的消息。` };
      const fileRefs = Array.isArray(entry.file_refs) ? entry.file_refs : [];
      if (fileRefs.length === 0) return { ok: false, output: `消息 ${ref} 里没有文件。` };
      const index = Math.max(1, Number(args.index ?? 1) || 1);
      if (index > fileRefs.length) return { ok: false, output: `消息 ${ref} 只有 ${fileRefs.length} 个文件。` };
      const fileRef = fileRefs[index - 1] as { name?: string; url?: string; fileId?: string };
      let url = String(fileRef.url ?? '').trim();
      if (!url && fileRef.fileId) {
        const resolved = await context.bot.getFile(String(fileRef.fileId));
        url = String(resolved.url ?? '').trim();
      }
      if (!url) return { ok: false, output: `文件「${fileRef.name ?? ''}」拿不到下载地址（可能已过期）。` };
      await context.workspace.ensure();
      const target = context.workspace.resolvePath(join('inbox', safeFilename(String(fileRef.name ?? ''), `qq-file-${Date.now()}`)));
      const { bytes, contentType } = await downloadToFile(url, target, DOWNLOAD_MAX_BYTES);
      const rel = context.workspace.displayPath(target);
      return {
        ok: true,
        output: `已下载「${fileRef.name ?? ''}」到 ${rel}（${formatBytes(bytes)}，${contentType || '未知类型'}）。`
          + '文本文件用 file_read/file_search 查看，压缩包用 extract_archive 解开。',
      };
    }

    case 'file_list': {
      const subdir = String(args.subdir ?? '').trim();
      const dirAbs = subdir ? context.workspace.resolvePath(subdir) : context.workspace.root;
      let names: string[];
      try {
        names = await readdir(dirAbs);
      } catch {
        return { ok: true, output: '工作区还没有文件。' };
      }
      if (names.length === 0) return { ok: true, output: '这个目录是空的。' };
      const lines: string[] = [];
      for (const name of names.slice(0, 50)) {
        const abs = join(dirAbs, name);
        const info = await stat(abs).catch(() => null);
        const display = subdir ? `${subdir.replace(/\/+$/, '')}/${name}` : name;
        lines.push(info?.isDirectory() ? `${display}/` : `${display}（${formatBytes(info?.size ?? 0)}）`);
      }
      return { ok: true, output: `工作区文件：\n${lines.join('\n')}` };
    }

    case 'file_read': {
      const abs = context.workspace.resolvePath(String(args.path ?? ''));
      const content = await readTextCapped(abs);
      const lines = content.split('\n');
      const from = Math.max(1, Number(args.from ?? 1) || 1);
      const to = Math.min(lines.length, Number(args.to ?? 0) || from + READ_MAX_LINES - 1, from + READ_MAX_LINES - 1);
      if (from > lines.length) return { ok: false, output: `文件只有 ${lines.length} 行，from=${from} 超出范围。` };
      let body = '';
      for (let index = from; index <= to; index += 1) {
        const line = `${index}: ${lines[index - 1]}`;
        if (body.length + line.length > READ_MAX_CHARS) {
          body += `\n…（输出达到长度上限，后续行请缩小区间再读）`;
          break;
        }
        body += `${line}\n`;
      }
      return { ok: true, output: `共 ${lines.length} 行，显示 ${from}-${to} 行：\n${body.trimEnd()}` };
    }

    case 'file_search': {
      const abs = context.workspace.resolvePath(String(args.path ?? ''));
      const content = await readTextCapped(abs);
      let regex: RegExp;
      try {
        regex = new RegExp(String(args.pattern ?? ''), 'g');
      } catch {
        return { ok: false, output: 'pattern 不是合法正则。' };
      }
      const lines = content.split('\n');
      const hits: string[] = [];
      for (const [index, line] of lines.entries()) {
        regex.lastIndex = 0;
        if (!regex.test(line)) continue;
        hits.push(`${index + 1}: ${line.length > 200 ? `${line.slice(0, 200)}…` : line}`);
        if (hits.length >= SEARCH_MAX_HITS) break;
      }
      if (hits.length === 0) return { ok: true, output: `没有命中（文件共 ${lines.length} 行）。` };
      const suffix = hits.length >= SEARCH_MAX_HITS ? '\n…（已达命中上限，可换更精确的正则）' : '';
      return { ok: true, output: `命中 ${hits.length} 行（文件共 ${lines.length} 行）：\n${hits.join('\n')}${suffix}` };
    }

    case 'file_write': {
      const content = String(args.content ?? '');
      if (content.length > WRITE_MAX_CHARS) return { ok: false, output: `内容超过 ${WRITE_MAX_CHARS} 字符上限。` };
      const abs = context.workspace.resolvePath(String(args.path ?? ''));
      await context.workspace.ensure();
      await mkdir(dirname(abs), { recursive: true });
      if (args.append === true) {
        await appendFile(abs, content, 'utf-8');
      } else {
        await writeFile(abs, content, 'utf-8');
      }
      const rel = context.workspace.displayPath(abs);
      return { ok: true, output: `已写入 ${rel}（${content.length} 字符${args.append === true ? '，追加' : ''}）。` };
    }

    case 'extract_archive': {
      const abs = context.workspace.resolvePath(String(args.path ?? ''));
      const buffer = await readFile(abs);
      const rel = context.workspace.displayPath(abs);
      if (isZip(buffer)) {
        const destDir = abs.replace(/\.zip$/i, '');
        const names = await extractZip(buffer, destDir);
        if (names.length === 0) return { ok: true, output: `${rel} 里没有可解出的文件。` };
        const destRel = context.workspace.displayPath(destDir);
        const shown = names.slice(0, 50).map((name) => `${destRel}/${name}`);
        return { ok: true, output: `已解出 ${names.length} 个文件到 ${destRel}/：\n${shown.join('\n')}${names.length > 50 ? '\n…' : ''}` };
      }
      if (isGzip(buffer)) {
        const destAbs = abs.replace(/\.gz$/i, '');
        const bytes = await extractGzip(buffer, destAbs);
        return { ok: true, output: `已解出 ${context.workspace.displayPath(destAbs)}（${formatBytes(bytes)}）。` };
      }
      return { ok: false, output: `${rel} 不是 zip/gz 格式，无法解压。` };
    }

    case 'send_file_excerpt': {
      const abs = context.workspace.resolvePath(String(args.path ?? ''));
      const content = await readTextCapped(abs);
      const lines = content.split('\n');
      const from = Math.max(1, Number(args.from ?? 0) || 0);
      let to = Math.min(lines.length, Number(args.to ?? 0) || 0);
      if (!from || !to || from > to) return { ok: false, output: 'from/to 行号无效（1 起始，from ≤ to）。' };
      if (from > lines.length) return { ok: false, output: `文件只有 ${lines.length} 行，from=${from} 超出范围。` };
      let truncatedByCap = false;
      if (to - from + 1 > EXCERPT_MAX_LINES) {
        to = from + EXCERPT_MAX_LINES - 1;
        truncatedByCap = true;
      }
      const selected: string[] = [];
      for (let index = from; index <= to; index += 1) {
        let line = (lines[index - 1] ?? '').trimEnd();
        if (line.length > EXCERPT_LINE_MAX_CHARS) line = `${line.slice(0, EXCERPT_LINE_MAX_CHARS)}…`;
        if (line.trim()) selected.push(line);
      }
      if (selected.length === 0) return { ok: false, output: `第 ${from}-${to} 行都是空行，没有可发送的内容。` };

      // Verbatim delivery: each source line becomes its own QQ message,
      // untranslated and un-narrated; per-line history like send_message.
      const refs: string[] = [];
      let failure = '';
      for (const line of selected) {
        try {
          const result = await context.bot.sendText(context.scopeType as 'group' | 'private', Number(context.scopeId), line);
          const sentId = (result as { message_id?: number | string }).message_id;
          const msgRef = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
          refs.push(msgRef);
          const entry: HistoryEntry = {
            user_id: context.bot.selfId,
            nickname: '冰糖',
            text: line,
            raw_message: line,
            message_id: sentId ?? null,
            message_ref: msgRef,
            timestamp: Date.now() / 1000,
            source_label: 'file-excerpt',
          };
          context.repo.appendMessage(context.scopeType, context.scopeId, entry);
        } catch (error) {
          failure = error instanceof Error ? error.message : String(error);
          break;
        }
      }
      if (refs.length === 0) return { ok: false, output: `send_file_excerpt 发送失败: ${failure}` };
      const notes: string[] = [];
      if (truncatedByCap) notes.push(`行数超过单次上限，只发了前 ${EXCERPT_MAX_LINES} 行，剩余请再调用本工具续发`);
      if (failure) notes.push(`后续行发送中断: ${failure}`);
      return {
        ok: true,
        output: `已把第 ${from}-${to} 行原文逐条发出（共 ${refs.length} 条）${notes.length ? `。${notes.join('；')}` : '。'}`,
        sentToUser: true,
      };
    }

    default:
      return null as never;
  }
}

/** Charge one network call against the per-turn budget (shared by plugins). */
export function spendNetworkBudget(context: ToolContext): void {
  context.networkBudget.used += 1;
  if (context.networkBudget.used > NETWORK_CALLS_PER_TURN) {
    throw new Error(`本轮网络调用次数已用完（${NETWORK_CALLS_PER_TURN} 次上限），先用已有结果收尾。`);
  }
}

const spendNetwork = spendNetworkBudget;

/** Read a text file with a 4MB cap; binary-looking content is refused. */
async function readTextCapped(abs: string): Promise<string> {
  const info = await stat(abs);
  if (info.isDirectory()) throw new Error('这是一个目录，不是文件');
  if (info.size > 4 * 1024 * 1024) throw new Error('文件超过 4MB，太大读不了（可用 file_search 定位或 extract_archive 处理）');
  const buffer = await readFile(abs);
  if (buffer.includes(0)) throw new Error('这是二进制文件，读不出文本内容');
  return buffer.toString('utf-8');
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`;
  return `${bytes}B`;
}
