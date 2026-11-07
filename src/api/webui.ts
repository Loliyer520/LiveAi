/**
 * 管理面板 API：挂在 HealthServer 同端口（/ 页面 + /api/*）。
 *
 * 端点分组（对应左侧导航）：
 *  - 首页：/api/status（健康、角色模型、用量）
 *  - 知识：/api/kb/*（条目/挂载/待审/复核）
 *  - 管理：/api/chat（与主AI对话）
 *  - 插件导航：/api/plugins、/api/onebot/*（账号/关系网/会话/人设）、
 *    /api/mc/*（边车状态+账号配置+下级AI）、/api/bili/*（统计）
 *  - 设置：/api/settings（总开关/ai 配置）、/api/plugins/toggle、/api/data/*
 *  - 检查更新：/api/update（git HEAD vs origin/master）
 *
 * 鉴权：deps.token 非空时要求 Bearer 或 ?token=；默认绑 127.0.0.1 时可留空。
 */

import type { IncomingMessage, ServerResponse } from 'node:http';
import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { execFile } from 'node:child_process';
import { mkdir, readdir, readFile, rename, rm, stat, unlink, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve, sep } from 'node:path';
import type { AgentRecord } from '../store/repository.js';
import type { UsageSnapshot } from '../models/usage.js';
import type { KbEntry, KbMount, KbPendingItem, KbPermission } from '../knowledge/kb.js';
import type { PersonRecord, ScopeRecord } from '../relations/graph.js';
import type { PluginInfo, PluginToolDetail, PluginTryResult } from '../chat/plugins.js';
import { webFetch } from '../chat/net.js';
import { listPersonaRefs, savePersonaRef, deletePersonaRef, readPersonaRef } from '../store/persona-refs.js';
import { probeReset, probeSnapshot } from '../observability/probe.js';
import type { NapcatActionResult, NapcatQrcode, NapcatRuntimeStatus } from '../napcat/runtime.js';
import { isMasterScope } from '../chat/types.js';

export interface WebuiDeps {
  /** 号主 QQ（号主身份；主AI 是 master:core，与此无关）：对话页区分号主消息。 */
  readonly masterQq: number;
  /** 提示词目录（data/prompt），读写都限死在里面。 */
  readonly promptDir: string;
  /** 空串 = 不鉴权（仅监听 127.0.0.1 时的默认姿态）。 */
  readonly token: string;
  readonly bot: {
    readonly selfId: number;
    readonly selfNickname: string;
    readonly connected: boolean;
    /** server=起 WS 服务端等 NapCat 连入；client=主动连 NapCat 正向 WS。 */
    readonly connectionMode?: 'server' | 'client';
  };
  readonly repo: {
    listAgents(): AgentRecord[];
    updateAgent(scopeType: string, scopeId: string, patch: Partial<AgentRecord>): void;
  };
  readonly models: {
    summaryText(): string;
    resolveRoleChannel(role: string): string | null;
    getModelForRole(role: string, advance?: boolean): { displayName: string; channelName: string } | null;
  };
  /** 提示词写盘后调，让 30s TTL 的缓存立刻失效。 */
  readonly invalidatePrompts: () => void;
  /** 请求次数/token 用量统计（内存自启动累计）。 */
  readonly usage: { snapshot(): UsageSnapshot };
  /** 网页主AI对话：注入主AI会话（admin_webui 来源）。 */
  readonly injectAdmin: (text: string) => void;
  /** 主AI会话历史（master:core，上级 agent 的虚拟会话），面板对话轮询用。 */
  readonly masterHistory: () => { user_id: number; nickname: string; text: string; timestamp: number }[];
  /** models_config.json 的绝对路径与热重载。 */
  readonly modelsConfigPath: string;
  readonly reloadModels: () => Promise<void>;

  // ── 知识库（知识页） ──────────────────────────────────────────────────────
  readonly kb: {
    list(query: { tags?: string[]; q?: string; limit?: number }): KbEntry[];
    add(input: { subject: string; content: string; tags?: string[]; confidence?: number }): KbEntry;
    revise(entryId: string, patch: { subject?: string; content?: string; tags?: string[]; confidence?: number }): KbEntry | null;
    remove(entryId: string): boolean;
    grantMount(target: string, permission: KbPermission, tags?: string[]): KbMount;
    revokeMount(target: string, tags?: string[]): boolean;
    mountsFor(scopeKey: string): KbMount[];
    pendingFor(reviewerScope: string, proposedBy?: string): KbPendingItem[];
    review(pendingId: string, action: 'approve' | 'reject', reviewerScope: string): KbEntry | null;
  };
  /** 中枢 scope key（KB 复核者身份）。 */
  readonly kbMasterScope: string;

  // ── 插件（插件导航 + 设置页插件配置 + 插件子页面详情/试玩） ───────────────
  readonly plugins: {
    listInfo(): PluginInfo[];
    setEnabled(name: string, enabled: boolean): void;
    toolDetails(name: string): { info: PluginInfo; tools: PluginToolDetail[]; version?: string; minCore?: string } | null;
    serviceOrigin(name: string): { url: string; allowInternal: boolean } | null;
    tryTool(pluginName: string, toolName: string, args: Record<string, unknown>): Promise<PluginTryResult>;
    getConfig(name: string): { spec: Record<string, unknown>; values: Record<string, string | number | boolean> } | null;
    setConfig(name: string, values: Record<string, unknown>): Record<string, string | number | boolean>;
  } | null;

  // ── 本机 NapCat 运行环境（NapCat 页的「一键部署」卡片） ─────────────────
  /** 保存连接配置时热更 access_token（免重启）。 */
  readonly napcat?: { setAccessToken(token: string): void };
  readonly napcatRuntime: {
    status(): Promise<NapcatRuntimeStatus>;
    install(): Promise<{ ok: boolean; error?: string }>;
    control(action: 'start' | 'stop' | 'restart'): Promise<NapcatActionResult>;
    qrcode(): Promise<NapcatQrcode>;
    logs(lines?: number): Promise<{ install: string[]; service: string[] }>;
  };

  // ── 关系网（Onebot 桥 · 关系网选项卡） ─────────────────────────────────
  readonly relations: {
    stats(): { people: number; facts: number; scopes: number };
    listPeople(): PersonRecord[];
    listScopes(): ScopeRecord[];
  };

  // ── 设置页（总开关 / ai 配置 / 本地数据） ────────────────────────────────
  /** 批量开关全部非主AI scope（面板总开关）。 */
  readonly setAllScopesEnabled: (enabled: boolean) => number;
  /** 后台任务（总结/情报）热开关 + 面板状态读取。 */
  readonly setSummaryEnabled: (enabled: boolean) => void;
  readonly setIntelEnabled: (enabled: boolean) => void;
  readonly summaryEnabled: () => boolean;
  readonly intelEnabled: () => boolean;
  /** data/config.json 绝对路径（ai.summary_enabled/intel_enabled 落盘）。 */
  readonly configPath: string;
  /** 本地数据管理扫描的根目录（stateDir、工作区、知识库等）。 */
  readonly stateDir: string;
  readonly workspaceDir: string;
  readonly dataDir: string;

  // ── Minecraft 桥 ────────────────────────────────────────────────────────
  /** 探测 mineflayer 边车（127.0.0.1:7660）；不可达时返回 { ok:false }。 */
  readonly mcFetch: (path: string) => Promise<Record<string, unknown>>;
  /** /my/run/mcbot 边车目录（账号配置落 mc-accounts.json）。 */
  readonly mcbotDir: string;

  // ── 检查更新 ────────────────────────────────────────────────────────────
  /** bot 仓根目录（git fetch/ls-remote 的工作目录）。 */
  readonly repoDir: string;
  readonly packageVersion: string;
}

const MAX_BODY = 1024 * 1024;
const STATUS_ROLES = ['main', 'vision', 'summary'];
const SECRET_MASK = '••••••';
/** 设置页本地数据管理允许清理的目录（白名单，防路径穿越）。 */
const DATA_TARGETS = new Set(['workspace', 'debug', 'media-spool', 'workspace-tts']);

export async function handleWebui(
  request: IncomingMessage,
  response: ServerResponse,
  deps: WebuiDeps,
): Promise<void> {
  const url = new URL(request.url ?? '/', 'http://localhost');
  // 面板装饰资源不含会话数据；固定单文件路由，避免带 token 的 CSS 子资源请求。
  if (request.method === 'GET' && url.pathname === '/assets/console-bg.jpg') {
    try {
      const image = await readFile(join(deps.repoDir, 'assets', 'console-bg.jpg'));
      response.writeHead(200, { 'content-type': 'image/jpeg', 'cache-control': 'public, max-age=86400' });
      response.end(image);
    } catch {
      sendJson(response, 404, { error: 'not found' });
    }
    return;
  }
  // favicon 免鉴权：浏览器拉图标不带 token/Authorization，图片本身无敏感内容。
  if (request.method === 'GET' && url.pathname === '/favicon.png') {
    const icon = await readFile(join(dirname(deps.configPath), 'favicon.png')).catch(() => null);
    if (icon === null) {
      sendJson(response, 404, { error: 'no favicon' });
      return;
    }
    response.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'max-age=3600' });
    response.end(icon);
    return;
  }

  if (!authorized(request, url, deps)) {
    sendJson(response, 401, { error: 'unauthorized' });
    return;
  }
  // 跨站写请求护栏：面板自身/curl 不受影响，恶意网页的表单或 fetch 会被挡。
  if (request.method !== 'GET' && request.method !== 'HEAD' && request.method !== 'OPTIONS' && isCrossSiteRequest(request)) {
    sendJson(response, 403, { error: 'cross-site request rejected' });
    return;
  }

  if (request.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
    const { WEBUI_PAGE } = await import('./webui-page.js');
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
    response.end(WEBUI_PAGE);
    return;
  }

  try {
    // ── 首页 ────────────────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/status') {
      const agents = deps.repo.listAgents();
      sendJson(response, 200, {
        uptimeSec: Math.floor(process.uptime()),
        bot: {
          connected: deps.bot.connected,
          selfId: deps.bot.selfId,
          nickname: deps.bot.selfNickname || '（昵称待学习）',
          mode: deps.bot.connectionMode ?? 'client',
        },
        scopes: { total: agents.length, enabled: agents.filter((a) => a.enabled !== false).length },
        usage: deps.usage.snapshot(),
        roles: STATUS_ROLES.map((role) => {
          const channel = deps.models.resolveRoleChannel(role);
          const model = deps.models.getModelForRole(role, false);
          return channel === null ? null : { role, channel, model: model?.displayName ?? '（空）' };
        }).filter((entry) => entry !== null),
      });
      return;
    }

    // ── 会话（Onebot 桥 · 会话管控选项卡；也供设置页总开关统计） ─────────────
    if (request.method === 'GET' && url.pathname === '/api/scopes') {
      const agents = deps.repo.listAgents();
      agents.sort((a, b) => {
        const am = isMasterScope(a.scopeType, a.scopeId) ? 0 : 1;
        const bm = isMasterScope(b.scopeType, b.scopeId) ? 0 : 1;
        if (am !== bm) return am - bm;
        if (a.scopeType !== b.scopeType) return a.scopeType === 'private' ? -1 : 1;
        return a.scopeId.localeCompare(b.scopeId);
      });
      sendJson(response, 200, agents.map((a) => ({
        scopeType: a.scopeType,
        scopeId: a.scopeId,
        displayName: a.displayName,
        enabled: a.enabled !== false,
        isMaster: isMasterScope(a.scopeType, a.scopeId),
        triggerRate: a.triggerRate,
        triggerWords: a.triggerWords,
        hasPersona: a.persona.trim() !== '',
        summaryBuiltAt: a.overallSummaryBuiltAt,
      })));
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/scope') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const scopeType = String(body.scopeType ?? '');
      const scopeId = String(body.scopeId ?? '');
      if ((scopeType !== 'group' && scopeType !== 'private') || !/^\d+$/.test(scopeId)) {
        sendJson(response, 400, { error: '作用域参数不对' });
        return;
      }
      const patch: Partial<AgentRecord> = {};
      if (body.enabled !== undefined) {
        patch.enabled = body.enabled === true;
      }
      if (body.triggerRate !== undefined) {
        const rate = Number(body.triggerRate);
        if (!Number.isFinite(rate)) {
          sendJson(response, 400, { error: '触发率不是数字' });
          return;
        }
        patch.triggerRate = Math.min(1, Math.max(0, rate));
      }
      if (body.triggerWords !== undefined) {
        if (!Array.isArray(body.triggerWords)) {
          sendJson(response, 400, { error: '触发词必须是数组' });
          return;
        }
        const words = [...new Set(
          body.triggerWords.map((w) => String(w ?? '').trim()).filter((w) => w !== '' && w.length <= 32),
        )].slice(0, 20);
        patch.triggerWords = words;
      }
      deps.repo.updateAgent(scopeType, scopeId, patch);
      sendJson(response, 200, { ok: true });
      return;
    }

    // ── 人设（Onebot 桥 · 人设配置选项卡） ─────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/prompts') {
      sendJson(response, 200, await listPromptFiles(deps.promptDir));
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/prompt') {
      const abs = safePromptPath(deps.promptDir, url.searchParams.get('path') ?? '');
      if (abs === null) {
        sendJson(response, 400, { error: '路径不合法' });
        return;
      }
      let content = '';
      try {
        content = await readFile(abs, 'utf-8');
      } catch {
        content = '';
      }
      sendJson(response, 200, { content });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/prompt') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const abs = safePromptPath(deps.promptDir, String(body.path ?? ''));
      if (abs === null) {
        sendJson(response, 400, { error: '路径不合法' });
        return;
      }
      const content = String(body.content ?? '');
      await mkdir(join(abs, '..'), { recursive: true });
      await writeFile(abs, content, 'utf-8');
      deps.invalidatePrompts();
      sendJson(response, 200, { ok: true });
      return;
    }

    // ── 人设版本（人设编辑页）：快照/列表/回读/删除 ──────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/prompt/versions') {
      const abs = safePromptPath(deps.promptDir, url.searchParams.get('path') ?? '');
      if (abs === null) { sendJson(response, 400, { error: '路径不合法' }); return; }
      sendJson(response, 200, { versions: await listPromptVersions(deps.promptDir, url.searchParams.get('path') ?? '') });
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/prompt/versions') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const rel = String(body.path ?? '');
      const abs = safePromptPath(deps.promptDir, rel);
      if (abs === null) { sendJson(response, 400, { error: '路径不合法' }); return; }
      const created = await savePromptVersion(deps.promptDir, rel, String(body.label ?? ''));
      sendJson(response, 200, { ok: true, version: created });
      return;
    }
    if (request.method === 'GET' && url.pathname === '/api/prompt/version') {
      const abs = safePromptPath(deps.promptDir, url.searchParams.get('path') ?? '');
      const id = String(url.searchParams.get('id') ?? '');
      if (abs === null || !/^\d{13,}$/.test(id)) { sendJson(response, 400, { error: '参数不合法' }); return; }
      const content = await readFile(versionFile(deps.promptDir, url.searchParams.get('path') ?? '', id), 'utf-8')
        .catch(() => null);
      if (content === null) { sendJson(response, 404, { error: '没有这个版本' }); return; }
      sendJson(response, 200, { content });
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/prompt/version/delete') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const abs = safePromptPath(deps.promptDir, String(body.path ?? ''));
      const id = String(body.id ?? '');
      if (abs === null || !/^\d{13,}$/.test(id)) { sendJson(response, 400, { error: '参数不合法' }); return; }
      await unlink(versionFile(deps.promptDir, String(body.path ?? ''), id)).catch(() => undefined);
      sendJson(response, 200, { ok: true });
      return;
    }

    // ── 人设参考图（人设编辑页）：列表 / 上传 / 删除 / 预览 ─────────────────
    if (request.method === 'GET' && url.pathname === '/api/persona-refs') {
      sendJson(response, 200, { refs: listPersonaRefs() });
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/persona-refs') {
      try {
        const body = (await readBody(request)) as Record<string, unknown>;
        const ref = savePersonaRef(String(body.name ?? ''), String(body.data ?? ''));
        sendJson(response, 200, { ok: true, ref });
      } catch (error) {
        sendJson(response, 400, { error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/persona-refs/delete') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const removed = deletePersonaRef(String(body.name ?? ''));
      sendJson(response, 200, { ok: removed });
      return;
    }
    if (request.method === 'GET' && url.pathname === '/api/persona-refs/file') {
      const ref = readPersonaRef(url.searchParams.get('name') ?? '');
      if (ref === null) { sendJson(response, 404, { error: '没有这张参考图' }); return; }
      response.writeHead(200, { 'content-type': ref.contentType, 'cache-control': 'no-cache' });
      response.end(ref.data);
      return;
    }

    // ── 管理页：与主AI对话 ──────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/chat') {
      const after = Number(url.searchParams.get('after') ?? 0) || 0;
      const selfId = Number(deps.bot.selfId);
      const masterQq = Number(deps.masterQq);
      const entries = deps.masterHistory();
      const fresh = entries
        .filter((entry) => entry.timestamp > after)
        .slice(-100)
        .map((entry) => ({
          role: entry.user_id === selfId ? 'ai' : entry.user_id === masterQq ? 'me' : entry.user_id === 0 ? 'sys' : 'user',
          name: entry.nickname,
          text: entry.text,
          ts: entry.timestamp,
        }));
      sendJson(response, 200, { messages: fresh, total: entries.length });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/chat') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const text = String(body.text ?? '').trim();
      if (text === '' || text.length > 4000) {
        sendJson(response, 400, { error: '消息为空或超过 4000 字' });
        return;
      }
      deps.injectAdmin(text);
      sendJson(response, 200, { ok: true });
      return;
    }

    // ── 模型配置（设置页 · 模型渠道） ───────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/models/config') {
      let content = '';
      try {
        content = await readFile(deps.modelsConfigPath, 'utf-8');
      } catch {
        content = '';
      }
      // 面板是明文 HTTP：api_key 一律掩码下发，保存时再按上游名还原。
      sendJson(response, 200, { content: maskModelsConfig(content) });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/models/config') {
      const body = (await readBody(request)) as Record<string, unknown>;
      let content = String(body.content ?? '');
      // 掩码 key 还原：没被用户改动的掩码项换回磁盘上的真值。
      let previous = '';
      try {
        previous = await readFile(deps.modelsConfigPath, 'utf-8');
      } catch {
        previous = '';
      }
      const unmasked = unmaskModelsConfig(content, previous);
      if (unmasked.error !== null) {
        sendJson(response, 400, { error: unmasked.error });
        return;
      }
      content = unmasked.content;
      const problem = validateModelsConfig(content);
      if (problem !== null) {
        sendJson(response, 400, { error: problem });
        return;
      }
      // 留备份（滚动保留 10 份）再原子覆盖：改坏了还能从 .bak 捞回来。
      if (previous !== '') {
        await backupConfigWithRetention(deps.modelsConfigPath, previous);
      }
      const modelsTmp = `${deps.modelsConfigPath}.${randomUUID().slice(0, 8)}.tmp`;
      await writeFile(modelsTmp, content, 'utf-8');
      await rename(modelsTmp, deps.modelsConfigPath);
      await deps.reloadModels();
      sendJson(response, 200, { ok: true });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/models') {
      sendJson(response, 200, { text: deps.models.summaryText() });
      return;
    }

    // ── 用量统计（模型配置页）：完整快照，含角色/模型分解 ────────────────────
    if (request.method === 'GET' && url.pathname === '/api/usage') {
      sendJson(response, 200, deps.usage.snapshot());
      return;
    }

    // ── 知识库（知识页） ────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/kb/entries') {
      const tags = (url.searchParams.get('tags') ?? '')
        .split(',').map((tag) => tag.trim()).filter(Boolean);
      const q = (url.searchParams.get('q') ?? '').trim();
      const limit = Math.max(1, Math.min(500, Math.trunc(Number(url.searchParams.get('limit') ?? 200)) || 200));
      sendJson(response, 200, { entries: deps.kb.list({ tags, q: q === '' ? undefined : q, limit }) });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/entry') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const subject = String(body.subject ?? '').trim();
      const content = String(body.content ?? '').trim();
      if (subject === '' || content === '') {
        sendJson(response, 400, { error: 'subject / content 都不能为空' });
        return;
      }
      const tags = Array.isArray(body.tags)
        ? body.tags.map((tag) => String(tag).trim()).filter(Boolean)
        : [];
      const entry = deps.kb.add({ subject, content, tags });
      sendJson(response, 200, { ok: true, entry });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/entry/update') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const id = String(body.id ?? '').trim();
      if (id === '') {
        sendJson(response, 400, { error: '缺 id' });
        return;
      }
      const patch: { subject?: string; content?: string; tags?: string[] } = {};
      if (body.subject !== undefined) patch.subject = String(body.subject).trim();
      if (body.content !== undefined) patch.content = String(body.content).trim();
      if (Array.isArray(body.tags)) patch.tags = body.tags.map((tag) => String(tag).trim()).filter(Boolean);
      const entry = deps.kb.revise(id, patch);
      if (entry === null) {
        sendJson(response, 404, { error: `没有条目 ${id}` });
        return;
      }
      sendJson(response, 200, { ok: true, entry });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/entry/delete') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const id = String(body.id ?? '').trim();
      if (!deps.kb.remove(id)) {
        sendJson(response, 404, { error: `没有条目 ${id}` });
        return;
      }
      sendJson(response, 200, { ok: true });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/kb/mounts') {
      // 全局挂载表：遍历已知 scope + 'all' 聚合（KB 类没有全量导出，挂载面本来就小）。
      const scopeKeys = new Set<string>(['all', deps.kbMasterScope]);
      for (const agent of deps.repo.listAgents()) scopeKeys.add(`${agent.scopeType}:${agent.scopeId}`);
      const seen = new Set<string>();
      const mounts: (KbMount & { key: string })[] = [];
      for (const scopeKey of scopeKeys) {
        for (const mount of deps.kb.mountsFor(scopeKey)) {
          const key = `${mount.target}|${(mount.tags ?? []).join(',')}|${mount.permission}`;
          if (seen.has(key)) continue;
          seen.add(key);
          mounts.push({ ...mount, key });
        }
      }
      sendJson(response, 200, { mounts });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/mount') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const target = String(body.target ?? '').trim();
      const permission = String(body.permission ?? 'read') as KbPermission;
      if (target === '') {
        sendJson(response, 400, { error: '缺 target（scope key 或 all）' });
        return;
      }
      if (!['read', 'append', 'write'].includes(permission)) {
        sendJson(response, 400, { error: 'permission 只能是 read / append / write' });
        return;
      }
      const tags = Array.isArray(body.tags)
        ? body.tags.map((tag) => String(tag).trim()).filter(Boolean)
        : undefined;
      const mount = deps.kb.grantMount(target, permission, tags);
      sendJson(response, 200, { ok: true, mount });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/unmount') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const target = String(body.target ?? '').trim();
      const tags = Array.isArray(body.tags)
        ? body.tags.map((tag) => String(tag).trim()).filter(Boolean)
        : undefined;
      if (!deps.kb.revokeMount(target, tags)) {
        sendJson(response, 404, { error: '没有找到对应挂载' });
        return;
      }
      sendJson(response, 200, { ok: true });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/kb/pending') {
      sendJson(response, 200, { pending: deps.kb.pendingFor(deps.kbMasterScope) });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/kb/review') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const id = String(body.id ?? '').trim();
      const action = String(body.action ?? '');
      if (action !== 'approve' && action !== 'reject') {
        sendJson(response, 400, { error: 'action 只能是 approve / reject' });
        return;
      }
      try {
        const entry = deps.kb.review(id, action, deps.kbMasterScope);
        sendJson(response, 200, { ok: true, entry });
      } catch (error) {
        sendJson(response, 404, { error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }

    // ── 插件（导航 + 设置页开关） ───────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/plugins') {
      sendJson(response, 200, { plugins: deps.plugins?.listInfo() ?? [] });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/plugins/toggle') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const name = String(body.name ?? '').trim();
      if (deps.plugins === null) {
        sendJson(response, 500, { error: '插件注册表未初始化' });
        return;
      }
      try {
        deps.plugins.setEnabled(name, body.enabled === true);
        sendJson(response, 200, { ok: true });
      } catch (error) {
        sendJson(response, 400, { error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }

    // ── 插件子页面：详情（工具规格 + 服务健康 + novel 指令机会话） ────────────
    const pluginDetailMatch = url.pathname.match(/^\/api\/plugins\/([\w-]+)\/detail$/);
    if (request.method === 'GET' && pluginDetailMatch) {
      if (deps.plugins === null) {
        sendJson(response, 500, { error: '插件注册表未初始化' });
        return;
      }
      const name = pluginDetailMatch[1];
      const detail = deps.plugins.toolDetails(name);
      if (detail === null) {
        sendJson(response, 404, { error: `没有插件 ${name}` });
        return;
      }
      // 服务健康：探测第一个 GET/无密钥工具的源站（本地服务放行内网）。
      let service: Record<string, unknown> | null = null;
      const origin = deps.plugins.serviceOrigin(name);
      if (origin !== null) {
        const started = Date.now();
        try {
          const result = await webFetch(origin.url, {
            method: 'GET',
            maxBytes: 256 * 1024,
            displayLimit: 200,
            allowInternal: origin.allowInternal,
          });
          service = { url: origin.url, ok: true, status: result.status, ms: Date.now() - started };
        } catch (error) {
          service = { url: origin.url, ok: false, ms: Date.now() - started, error: error instanceof Error ? error.message : String(error) };
        }
      }
      // novel 附加：指令模式会话（novel_command.scopes，已切流到 deeplive-novel 时为空）。
      const extras: Record<string, unknown> = {};
      if (name === 'novel') {
        const config = await readConfigJson(deps.configPath);
        const novelCmd = (config.novel_command ?? {}) as Record<string, unknown>;
        extras.commandScopes = Array.isArray(novelCmd.scopes) ? novelCmd.scopes.map((value) => String(value)) : [];
        extras.commandBaseUrl = typeof novelCmd.base_url === 'string' ? novelCmd.base_url : '';
      }
      // manifest v2 配置声明 + 生效值（面板自动表单）。
      const pluginConfig = deps.plugins.getConfig(name);
      sendJson(response, 200, { ...detail, service, extras, config: pluginConfig ?? { spec: {}, values: {} } });
      return;
    }

    // ── 插件子页面：manifest v2 配置保存 ─────────────────────────────────────
    const pluginConfigMatch = url.pathname.match(/^\/api\/plugins\/([\w-]+)\/config$/);
    if (request.method === 'POST' && pluginConfigMatch) {
      if (deps.plugins === null) {
        sendJson(response, 500, { error: '插件注册表未初始化' });
        return;
      }
      const body = (await readBody(request)) as Record<string, unknown>;
      const values = (body.values !== null && typeof body.values === 'object' ? body.values : {}) as Record<string, unknown>;
      try {
        const saved = deps.plugins.setConfig(pluginConfigMatch[1], values);
        sendJson(response, 200, { ok: true, values: saved });
      } catch (error) {
        sendJson(response, 400, { error: error instanceof Error ? error.message : String(error) });
      }
      return;
    }

    // ── 插件子页面：试玩（仅 GET/无密钥/无副作用工具） ───────────────────────
    const pluginTryMatch = url.pathname.match(/^\/api\/plugins\/([\w-]+)\/try$/);
    if (request.method === 'POST' && pluginTryMatch) {
      if (deps.plugins === null) {
        sendJson(response, 500, { error: '插件注册表未初始化' });
        return;
      }
      const body = (await readBody(request)) as Record<string, unknown>;
      const args = (body.args !== null && typeof body.args === 'object' ? body.args : {}) as Record<string, unknown>;
      const result = await deps.plugins.tryTool(pluginTryMatch[1], String(body.tool ?? ''), args);
      sendJson(response, result.ok ? 200 : 400, result);
      return;
    }

    // ── Onebot 桥 · NapCat 连接配置 ─────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/napcat/config') {
      const config = await readConfigJson(deps.configPath);
      const napcat = config.napcat ?? {};
      const settings = napcatSettings(napcat);
      // 登录过本 bot 的账号花名册：当前连接的账号自动登记，供表单直接选号。
      const rosterPath = resolve(deps.configPath, '..', 'napcat-accounts.json');
      let roster: { id: number; nickname: string; lastSeen: number }[] = [];
      try {
        const raw = JSON.parse(await readFile(rosterPath, 'utf-8')) as { accounts?: unknown };
        if (Array.isArray(raw.accounts)) {
          roster = raw.accounts
            .filter((a): a is Record<string, unknown> => typeof a === 'object' && a !== null)
            .map((a) => ({ id: Number(a.id ?? 0), nickname: String(a.nickname ?? ''), lastSeen: Number(a.lastSeen ?? 0) || 0 }))
            .filter((a) => Number.isSafeInteger(a.id) && a.id > 10000);
        }
      } catch { /* 没有花名册就从空开始 */ }
      const connectedId = Number(deps.bot.selfId);
      if (deps.bot.connected && Number.isSafeInteger(connectedId) && connectedId > 10000) {
        const nick = String(deps.bot.selfNickname ?? '').trim();
        const found = roster.find((a) => a.id === connectedId);
        if (found) {
          if (nick) found.nickname = nick;
          found.lastSeen = Date.now();
        } else {
          roster.push({ id: connectedId, nickname: nick, lastSeen: Date.now() });
        }
        roster.sort((a, b) => b.lastSeen - a.lastSeen);
        await writeFile(rosterPath, JSON.stringify({ accounts: roster.slice(0, 20) }, null, 1), 'utf-8').catch(() => undefined);
      }
      // 服务端连接地址：主机取浏览器访问用的 host，端口取监听端口，可直接粘进 NapCat。
      const host = String(request.headers.host ?? '').split(':')[0] || '127.0.0.1';
      const serverUrl = settings.mode === 'server' && settings.reverseWsPort > 0
        ? `ws://${host}:${settings.reverseWsPort}/deeplive-bind`
        : '';
      sendJson(response, 200, {
        ...settings,
        knownAccounts: roster,
        connectedId: deps.bot.connected ? connectedId : 0,
        serverUrl,
        // 服务端模式没配 WS Token：只有同机 NapCat 能连进来，远程会被拒——提前亮黄灯。
        warn: settings.mode === 'server' && String(napcat.access_token ?? '') === ''
          ? '未配置 WS Token，仅本机 NapCat 能连入；远程连接请先生成 token'
          : '',
      });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/napcat/config') {
      const body = (await readBody(request)) as Record<string, unknown>;
      // 校验在串行写队列内做：validation error 置 errorOut 并中止写入。
      let errorOut: { status: number; message: string } | null = null;
      let accessToken = '';
      let previous: Record<string, unknown> = {};
      let nextValues: { mode: string; wsUrl: string; httpUrl: string; reverseWsPort: number; publicBaseUrl: string; selfId: number } | null = null;
      const fail = (status: number, message: string): false => {
        errorOut = { status, message };
        return false;
      };
      try {
        await updateConfigJson(deps.configPath, (config) => {
          previous = config.napcat ?? {};
          const mode = String(body.mode ?? previous.mode ?? '').trim();
          const reverseWsPort = Number(body.reverseWsPort ?? previous.reverse_ws_port ?? 0);
          const selfId = Number(body.selfId ?? previous.self_id ?? 0);
          const wsUrl = restoreUrlSecrets(
            String(body.wsUrl ?? previous.ws_url ?? '').trim(),
            String(previous.ws_url ?? ''),
          );
          const httpUrl = String(body.httpUrl ?? previous.http_url ?? '').trim();
          const publicBaseUrl = String(body.publicBaseUrl ?? previous.public_base_url ?? '').trim().replace(/\/+$/, '');
          if (mode !== 'server' && mode !== 'client') return fail(400, '连接方式只能是 server 或 client');
          if (!Number.isInteger(reverseWsPort) || reverseWsPort < 0 || reverseWsPort > 65535 || (mode === 'server' && reverseWsPort === 0)) {
            return fail(400, '监听端口须为 1–65535；主动连接模式可填 0');
          }
          if (!Number.isSafeInteger(selfId) || selfId < 0) return fail(400, '机器人 QQ 必须是非负整数，未知时可填 0');
          if (!validConfigUrl(wsUrl, ['ws:', 'wss:']) || !validConfigUrl(httpUrl, ['http:', 'https:']) || !validConfigUrl(publicBaseUrl, ['http:', 'https:'])) {
            return fail(400, '地址格式不正确：WS 用 ws:// 或 wss://，HTTP 用 http:// 或 https://');
          }
          if (mode === 'client' && wsUrl === '') return fail(400, '主动连接 NapCat 时必须填写 WebSocket 地址');
          if (wsUrl.includes(SECRET_MASK)) return fail(400, 'WS 地址中的 Token 掩码无法还原，请重新填写完整地址');
          const submittedToken = String(body.accessToken ?? '').trim();
          accessToken = body.clearToken === true
            ? ''
            : submittedToken === '' || submittedToken === SECRET_MASK
              ? String(previous.access_token ?? '')
              : submittedToken;
          config.napcat = {
            ...previous,
            mode,
            ws_url: wsUrl,
            http_url: httpUrl,
            reverse_ws_port: reverseWsPort,
            public_base_url: publicBaseUrl,
            self_id: selfId,
            access_token: accessToken,
          };
          nextValues = { mode, wsUrl, httpUrl, reverseWsPort, publicBaseUrl, selfId };
          return true;
        });
      } catch (error) {
        if (error instanceof ConfigCorruptError) {
          sendJson(response, 409, { error: error.message });
          return;
        }
        throw error;
      }
      if (errorOut !== null) {
        sendJson(response, (errorOut as { status: number }).status, { error: (errorOut as { message: string }).message });
        return;
      }
      // token 热更：新连接立即按新值校验，其余改动（端口/模式/地址）仍需重启。
      const tokenChanged = accessToken !== String(previous.access_token ?? '');
      if (tokenChanged) deps.napcat?.setAccessToken(accessToken);
      const next = nextValues as unknown as { mode: string; wsUrl: string; httpUrl: string; reverseWsPort: number; publicBaseUrl: string; selfId: number };
      const restartRequired =
        next.mode !== String(previous.mode ?? next.mode)
        || next.reverseWsPort !== Number(previous.reverse_ws_port ?? next.reverseWsPort)
        || next.wsUrl !== String(previous.ws_url ?? next.wsUrl)
        || next.httpUrl !== String(previous.http_url ?? next.httpUrl)
        || next.publicBaseUrl !== String(previous.public_base_url ?? next.publicBaseUrl)
        || next.selfId !== Number(previous.self_id ?? next.selfId);
      sendJson(response, 200, { ok: true, restartRequired });
      return;
    }

    // ── Onebot 桥 · 关系网 ─────────────────────────────────────────────────
    // ── Onebot 桥 · 本机 NapCat 自动部署/运行 ───────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/napcat/runtime') {
      const status: NapcatRuntimeStatus = await deps.napcatRuntime.status();
      sendJson(response, 200, status);
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/napcat/runtime/install') {
      const result = await deps.napcatRuntime.install();
      sendJson(response, result.ok ? 200 : 400, result);
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/napcat/runtime/action') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const action = String(body.action ?? '');
      if (action !== 'start' && action !== 'stop' && action !== 'restart') {
        sendJson(response, 400, { error: '操作只能是 start、stop 或 restart' });
        return;
      }
      const result: NapcatActionResult = await deps.napcatRuntime.control(action);
      sendJson(response, result.ok ? 200 : 500, result);
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/napcat/runtime/qrcode') {
      const qrcode: NapcatQrcode = await deps.napcatRuntime.qrcode();
      sendJson(response, 200, qrcode);
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/napcat/runtime/logs') {
      const lines = Number(url.searchParams.get('lines') ?? 120);
      sendJson(response, 200, await deps.napcatRuntime.logs(Number.isFinite(lines) ? lines : 120));
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/relations') {
      const people = deps.relations.listPeople()
        .sort((a, b) => b.lastSeenAt - a.lastSeenAt)
        .slice(0, 500)
        .map((person) => ({
          userId: person.userId,
          canonicalName: person.canonicalName,
          scopes: person.scopes,
          impression: person.impression,
          facts: person.facts.filter((fact) => fact.status === 'active'),
          factCount: person.facts.filter((fact) => fact.status === 'active').length,
          lastSeenAt: person.lastSeenAt,
        }));
      const scopes = deps.relations.listScopes()
        .sort((a, b) => b.lastActiveAt - a.lastActiveAt)
        .slice(0, 500)
        .map((scope) => ({
          scopeKey: scope.scopeKey,
          displayName: scope.displayName,
          impression: scope.impression,
          memberCount: scope.memberIds.length,
          topicTags: scope.topicTags,
          lastActiveAt: scope.lastActiveAt,
        }));
      sendJson(response, 200, { stats: deps.relations.stats(), people, scopes });
      return;
    }

    // ── Minecraft 桥 ────────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/mc/status') {
      const sidecar = await deps.mcFetch('/status');
      const account = await readMcAccount(deps.mcbotDir);
      sendJson(response, 200, { sidecar, account });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/mc/account') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const auth = String(body.auth ?? '').trim();
      if (!['offline', 'microsoft'].includes(auth)) {
        sendJson(response, 400, { error: 'auth 只能是 offline / microsoft' });
        return;
      }
      const account = {
        auth,
        username: String(body.username ?? '').trim(),
        password: String(body.password ?? '').trim(),
        updatedAt: Date.now(),
      };
      await writeFile(join(deps.mcbotDir, 'mc-accounts.json'), JSON.stringify(account, null, 2), 'utf-8');
      sendJson(response, 200, { ok: true });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/mc/subagents') {
      const agents = deps.repo.listAgents().filter((a) => a.scopeType === 'mcclient');
      sendJson(response, 200, {
        subagents: agents.map((a) => ({
          scopeKey: `mcclient:${a.scopeId}`,
          displayName: a.displayName || a.scopeId,
          enabled: a.enabled !== false,
          persona: a.persona.slice(0, 200),
          summaryBuiltAt: a.overallSummaryBuiltAt,
        })),
      });
      return;
    }

    // ── 探针（性能页）：统一计时打点的聚合与慢事件现场 ─────────────────────
    if (request.method === 'GET' && url.pathname === '/api/probes') {
      sendJson(response, 200, probeSnapshot());
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/probes/reset') {
      probeReset();
      sendJson(response, 200, { ok: true });
      return;
    }

    // ── 哔哩桥 · 统计 ──────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/bili/stats') {
      const agents = deps.repo.listAgents();
      const videos = agents.filter((a) => a.scopeType === 'bili');
      const usage = deps.usage.snapshot();
      sendJson(response, 200, {
        hubEnabled: agents.find((a) => a.scopeType === 'domain' && a.scopeId === 'bili')?.enabled !== false,
        videoScopeCount: videos.length,
        videoScopes: videos.slice(0, 100).map((a) => ({
          bvid: a.scopeId,
          displayName: a.displayName,
          enabled: a.enabled !== false,
          summaryBuiltAt: a.overallSummaryBuiltAt,
        })),
        usage: { total: usage.total, lastError: usage.lastError },
      });
      return;
    }

    // ── 设置页 ──────────────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/settings') {
      const agents = deps.repo.listAgents();
      const enabledCount = agents.filter((a) => a.enabled !== false).length;
      const configJson = await readConfigJson(deps.configPath);
      sendJson(response, 200, {
        llm: {
          scopesTotal: agents.length,
          scopesEnabled: enabledCount,
          summaryEnabled: deps.summaryEnabled(),
          intelEnabled: deps.intelEnabled(),
          /** config.json 里的持久值（重启后的落点），与热开关分开展示。 */
          persisted: {
            summaryEnabled: configJson.ai?.summary_enabled !== false,
            intelEnabled: configJson.ai?.intel_enabled !== false,
          },
        },
        plugins: deps.plugins?.listInfo() ?? [],
        /** 号主/管理员 QQ：config.json 持久值，改完需重启生效（路由按启动时快照算）。 */
        identity: {
          masterQq: Number(configJson.ai?.master_qq ?? 0) || 0,
          adminQq: Number(configJson.ai?.admin_qq ?? 0) || 0,
        },
      });
      return;
    }

    // 号主/管理员 QQ：写进 config.json 的 ai.master_qq / ai.admin_qq。
    // 号主 QQ 只是「号主身份」（信任/防踢防禁言），主AI 是上级 agent，没有 QQ 号。
    // 热态的 config 是启动快照（Object.freeze），这里只改磁盘，重启后路由才换目标。
    if (request.method === 'POST' && url.pathname === '/api/settings/identity') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const validQq = (value: unknown, field: string): number => {
        const n = Number(value);
        if (!Number.isInteger(n) || n < 10000 || n > 9999999999) {
          throw new Error(`${field} 不是合法 QQ 号（5~10 位数字）`);
        }
        return n;
      };
      let masterQq = 0;
      let adminQq = 0;
      try {
        masterQq = validQq(body.masterQq, '号主 QQ');
        adminQq = validQq(body.adminQq, '管理员 QQ');
      } catch (error) {
        sendJson(response, 400, { error: error instanceof Error ? error.message : String(error) });
        return;
      }
      if (masterQq === adminQq) {
        sendJson(response, 400, { error: '号主与管理员不能是同一个 QQ（两种身份权限不同，2026-10-30 身份混淆事故）' });
        return;
      }
      try {
        await updateConfigJson(deps.configPath, (config) => {
          config.ai = config.ai ?? {};
          config.ai.master_qq = masterQq;
          config.ai.admin_qq = adminQq;
        });
      } catch (error) {
        if (error instanceof ConfigCorruptError) {
          sendJson(response, 409, { error: error.message });
          return;
        }
        throw error;
      }
      sendJson(response, 200, { ok: true, needsRestart: true });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/settings/llm') {
      const body = (await readBody(request)) as Record<string, unknown>;
      let changed = 0;
      if (body.scopesEnabled !== undefined) {
        changed = deps.setAllScopesEnabled(body.scopesEnabled === true);
      }
      if (body.summaryEnabled !== undefined) {
        deps.setSummaryEnabled(body.summaryEnabled === true);
      }
      if (body.intelEnabled !== undefined) {
        deps.setIntelEnabled(body.intelEnabled === true);
      }
      if (body.persist === true) {
        await persistAiFlags(deps.configPath, {
          summaryEnabled: body.summaryEnabled,
          intelEnabled: body.intelEnabled,
        });
      }
      sendJson(response, 200, { ok: true, changedScopes: changed });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/data') {
      sendJson(response, 200, { dirs: await scanDataDirs(deps) });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/data/clear') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const target = String(body.target ?? '');
      if (!DATA_TARGETS.has(target)) {
        sendJson(response, 400, { error: `不允许清理 ${target || '(空)'}（白名单: ${[...DATA_TARGETS].join(' / ')}）` });
        return;
      }
      const root = target === 'workspace' ? deps.workspaceDir : join(deps.stateDir, target);
      const removed = await clearDirContents(root);
      sendJson(response, 200, { ok: true, removed });
      return;
    }

    // ── 检查更新 ────────────────────────────────────────────────────────────
    if (request.method === 'GET' && url.pathname === '/api/update') {
      sendJson(response, 200, await checkUpdate(deps));
      return;
    }

    sendJson(response, 404, { error: 'not found' });
  } catch (error) {
    sendJson(response, 500, { error: error instanceof Error ? error.message : String(error) });
  }
}

function authorized(request: IncomingMessage, url: URL, deps: WebuiDeps): boolean {
  if (deps.token === '') {
    // 未配 token 的免鉴权模式只对本机开放：服务默认监听 127.0.0.1 时是便利，
    // 一旦有人把监听改成 0.0.0.0，原来的 `return true` 就是整个面板裸奔。
    if (!isLoopbackAddress(request.socket.remoteAddress)) return false;
    const host = String(request.headers.host ?? '').toLowerCase().replace(/:\d+$/, '');
    return host === '' || host === 'localhost' || host.startsWith('127.') || host === '[::1]';
  }
  const bearer = /^Bearer (.+)$/.exec(String(request.headers.authorization ?? ''));
  const presented = bearer?.[1] ?? url.searchParams.get('token') ?? '';
  return presented !== '' && constantTimeEqual(presented, deps.token);
}

function isLoopbackAddress(address: string | undefined): boolean {
  if (!address) return false;
  const value = address.toLowerCase();
  return value === '::1' || value.startsWith('127.') || value.startsWith('::ffff:127.');
}

/** 恒定时间比较（先哈希到定长，长度/前缀不泄露）。 */
function constantTimeEqual(presented: string, expected: string): boolean {
  const digest = (value: string): Buffer => createHash('sha256').update(value, 'utf8').digest();
  return timingSafeEqual(digest(presented), digest(expected));
}

/**
 * 浏览器跨站判定：Sec-Fetch-Site 优先；不支持时看 Origin 是否等于 Host。
 * 无 Origin 的请求（curl、同源顶层导航）放行。
 */
function isCrossSiteRequest(request: IncomingMessage): boolean {
  const site = String(request.headers['sec-fetch-site'] ?? '').toLowerCase();
  if (site !== '' && site !== 'same-origin' && site !== 'none') return true;
  const origin = String(request.headers.origin ?? '');
  if (origin === '') return false;
  try {
    return new URL(origin).host.toLowerCase() !== String(request.headers.host ?? '').toLowerCase();
  } catch {
    return true;
  }
}

async function listPromptFiles(promptDir: string): Promise<{ path: string; size: number }[]> {
  const out: { path: string; size: number }[] = [];
  const scan = async (sub: string): Promise<void> => {
    let names: string[] = [];
    try {
      names = await readdir(join(promptDir, sub));
    } catch {
      return;
    }
    for (const name of names.sort()) {
      const rel = sub === '' ? name : `${sub}/${name}`;
      if (!name.endsWith('.txt')) continue;
      const info = await stat(join(promptDir, rel)).catch(() => null);
      out.push({ path: rel, size: info?.size ?? 0 });
    }
  };
  await scan('');
  await scan('staff');
  await scan('domain');
  return out;
}

/** 只允许 promptDir 内的相对 .txt 路径（含 staff/ domain/ 子目录），拒绝 .. 与绝对路径。 */
function safePromptPath(promptDir: string, rel: string): string | null {
  const trimmed = rel.trim();
  if (!/^[\w][\w./-]*\.txt$/.test(trimmed) || trimmed.includes('..')) return null;
  const abs = resolve(promptDir, trimmed);
  const root = resolve(promptDir);
  if (abs !== root && !abs.startsWith(root + sep)) return null;
  return abs;
}

// ── 人设版本存储：data/prompt/.versions/<rel 换杠>/<时间戳>.json ──────────────
// 每个 .txt 最多留 30 份，超出按时间淘汰；文件名即 id，内容带 label。

interface PromptVersionMeta { id: string; label: string; savedAt: number; bytes: number; preview: string }

function versionDir(promptDir: string, rel: string): string {
  const flat = rel.trim().replace(/[^A-Za-z0-9._-]+/g, '__');
  return join(promptDir, '.versions', flat);
}

function versionFile(promptDir: string, rel: string, id: string): string {
  return join(versionDir(promptDir, rel), `${id}.json`);
}

async function savePromptVersion(promptDir: string, rel: string, label: string): Promise<PromptVersionMeta | null> {
  const abs = safePromptPath(promptDir, rel);
  if (abs === null) return null;
  const content = await readFile(abs, 'utf-8').catch(() => null);
  if (content === null) return null;
  const dir = versionDir(promptDir, rel);
  await mkdir(dir, { recursive: true });
  const id = String(Date.now());
  const meta: PromptVersionMeta = {
    id,
    label: label.trim().slice(0, 60),
    savedAt: Number(id),
    bytes: Buffer.byteLength(content, 'utf-8'),
    preview: content.trim().slice(0, 80),
  };
  await writeFile(join(dir, `${id}.json`), JSON.stringify({ ...meta, content }, null, 1), 'utf-8');
  // 超出上限就按 id（即时间）从旧开始淘汰。
  const ids = (await readdir(dir)).filter((n) => /^\d{13,}\.json$/.test(n)).sort();
  while (ids.length > 30) {
    await unlink(join(dir, ids.shift() as string)).catch(() => undefined);
  }
  return meta;
}

async function listPromptVersions(promptDir: string, rel: string): Promise<PromptVersionMeta[]> {
  const dir = versionDir(promptDir, rel);
  let names: string[] = [];
  try {
    names = await readdir(dir);
  } catch {
    return [];
  }
  const out: PromptVersionMeta[] = [];
  for (const name of names) {
    if (!/^\d{13,}\.json$/.test(name)) continue;
    const raw = JSON.parse(await readFile(join(dir, name), 'utf-8').catch(() => 'null')) as Record<string, unknown> | null;
    if (raw === null) continue;
    out.push({
      id: String(raw.id ?? name.replace(/\.json$/, '')),
      label: String(raw.label ?? ''),
      savedAt: Number(raw.savedAt ?? 0) || 0,
      bytes: Number(raw.bytes ?? 0) || 0,
      preview: String(raw.preview ?? '').slice(0, 80),
    });
  }
  return out.sort((a, b) => b.savedAt - a.savedAt);
}

// ── mc 账号配置 ─────────────────────────────────────────────────────────────

interface McAccount {
  auth: string;
  username: string;
  password: string;
  updatedAt?: number;
}

async function readMcAccount(mcbotDir: string): Promise<McAccount> {
  try {
    const raw = JSON.parse(await readFile(join(mcbotDir, 'mc-accounts.json'), 'utf-8')) as Partial<McAccount>;
    return {
      auth: raw.auth === 'microsoft' ? 'microsoft' : 'offline',
      username: String(raw.username ?? ''),
      // 密码不下发明文，只回掩码让前端知道已配置过
      password: String(raw.password ?? '') !== '' ? '••••••' : '',
      updatedAt: Number(raw.updatedAt ?? 0) || 0,
    };
  } catch {
    return { auth: 'offline', username: '', password: '' };
  }
}

// ── 设置页辅助 ──────────────────────────────────────────────────────────────

interface ConfigJson {
  napcat?: Record<string, unknown>;
  [key: string]: unknown;
  ai?: {
    summary_enabled?: boolean;
    intel_enabled?: boolean;
    master_qq?: number;
    admin_qq?: number;
    [key: string]: unknown;
  };
}

/** config.json 损坏时必须中止写盘：拿 {} 继续合并会把其他配置节静默清空。 */
class ConfigCorruptError extends Error {}

async function readConfigJson(configPath: string): Promise<ConfigJson> {
  let raw: string;
  try {
    raw = await readFile(configPath, 'utf-8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return {};
    throw new ConfigCorruptError(`config.json 读取失败：${error instanceof Error ? error.message : String(error)}`);
  }
  try {
    return JSON.parse(raw) as ConfigJson;
  } catch (error) {
    throw new ConfigCorruptError(
      `config.json 不是合法 JSON（${error instanceof Error ? error.message : String(error)}），`
      + '已拒绝任何修改；请手工修复，或从同目录 config.json.bak-* 备份恢复。',
    );
  }
}

/**
 * 同一配置文件的「读-改-写」串行化 + 原子落盘：并发的面板写请求排队执行，
 * 不再出现两个请求各读旧文件、后写覆盖先写导致配置节静默丢失。
 * mutate 返回 false 可中止本次写入（校验失败场景）。
 */
const configWriteQueues = new Map<string, Promise<unknown>>();

async function updateConfigJson(configPath: string, mutate: (config: ConfigJson) => boolean | void): Promise<void> {
  const queued = (configWriteQueues.get(configPath) ?? Promise.resolve())
    .then(async () => {
      const config = await readConfigJson(configPath);
      if (mutate(config) === false) return;
      const previous = await readFile(configPath, 'utf-8').catch(() => '');
      if (previous !== '') await backupConfigWithRetention(configPath, previous);
      const tmp = `${configPath}.${randomUUID().slice(0, 8)}.tmp`;
      await writeFile(tmp, JSON.stringify(config, null, 2), 'utf-8');
      await rename(tmp, configPath);
    });
  // 队列本身永不拒绝：一次失败不能卡死后续所有配置写入。
  configWriteQueues.set(configPath, queued.catch(() => {}));
  return queued;
}

/** 备份只留最近 10 份：.bak-* 里带明文密钥，无限堆积会扩大泄密面。 */
async function backupConfigWithRetention(configPath: string, content: string, keep = 10): Promise<void> {
  await writeFile(`${configPath}.bak-${Date.now()}`, content, 'utf-8');
  try {
    const dir = dirname(configPath);
    const prefix = `${basename(configPath)}.bak-`;
    const backups = (await readdir(dir)).filter((name) => name.startsWith(prefix)).sort();
    for (const stale of backups.slice(0, Math.max(0, backups.length - keep))) {
      await rm(join(dir, stale), { force: true });
    }
  } catch { /* 清旧失败不挡保存主流程 */ }
}

function napcatSettings(napcat: Record<string, unknown>) {
  const wsUrl = String(napcat.ws_url ?? '');
  const accessToken = String(napcat.access_token ?? '');
  const reverseWsPort = Number(napcat.reverse_ws_port ?? 0);
  return {
    mode: napcat.mode === 'client' || napcat.mode === 'server' ? napcat.mode : reverseWsPort > 0 ? 'server' : 'client',
    wsUrl: wsUrl.replace(/([?&](?:access_token|token)=)[^&]*/gi, '$1' + SECRET_MASK),
    httpUrl: String(napcat.http_url ?? ''),
    reverseWsPort,
    publicBaseUrl: String(napcat.public_base_url ?? ''),
    selfId: Number(napcat.self_id ?? 0),
    accessToken: accessToken === '' ? '' : SECRET_MASK,
  };
}

function validConfigUrl(value: string, protocols: string[]): boolean {
  if (value === '') return true;
  try {
    return protocols.includes(new URL(value).protocol);
  } catch {
    return false;
  }
}

/** 编辑地址其他部分时，保留查询参数里未明文下发的 Token。 */
function restoreUrlSecrets(next: string, previous: string): string {
  if (!next.includes(SECRET_MASK) || previous === '') return next;
  let previousUrl: URL;
  try {
    previousUrl = new URL(previous);
  } catch {
    return next;
  }
  return next.replace(/([?&](access_token|token)=)••••••/gi, (match, prefix: string, key: string) => {
    const value = previousUrl.searchParams.get(key);
    return value === null ? match : prefix + encodeURIComponent(value);
  });
}

/** 把 ai.summary_enabled / intel_enabled 写回 config.json（总开关的持久落点）。 */
async function persistAiFlags(
  configPath: string,
  flags: { summaryEnabled?: unknown; intelEnabled?: unknown },
): Promise<void> {
  await updateConfigJson(configPath, (config) => {
    config.ai = config.ai ?? {};
    if (flags.summaryEnabled !== undefined) config.ai.summary_enabled = flags.summaryEnabled === true;
    if (flags.intelEnabled !== undefined) config.ai.intel_enabled = flags.intelEnabled === true;
    return true;
  });
}

/** 本地数据管理：列出受管目录的体积（一层统计，够用且快）。 */
async function scanDataDirs(deps: WebuiDeps): Promise<{ name: string; path: string; bytes: number; files: number; clearable: boolean }[]> {
  const candidates = [
    { name: '会话状态 state', path: deps.stateDir, clearable: false },
    { name: '工作区 workspace', path: deps.workspaceDir, clearable: true },
    { name: '调试快照 debug', path: join(deps.stateDir, 'debug'), clearable: true },
    { name: '媒体外吐 media-spool', path: join(deps.stateDir, 'media-spool'), clearable: true },
    { name: '知识库 knowledge', path: join(deps.dataDir, 'knowledge'), clearable: false },
    { name: '表情库 stickers', path: join(deps.dataDir, 'stickers'), clearable: false },
    { name: '插件数据 plugins', path: join(deps.dataDir, 'plugins'), clearable: false },
  ];
  const out = [];
  for (const candidate of candidates) {
    const { bytes, files } = await dirSize(candidate.path);
    out.push({ ...candidate, bytes, files });
  }
  return out;
}

async function dirSize(root: string, depth = 0): Promise<{ bytes: number; files: number }> {
  if (depth > 4) return { bytes: 0, files: 0 };
  let bytes = 0;
  let files = 0;
  let names: string[] = [];
  try {
    names = await readdir(root);
  } catch {
    return { bytes: 0, files: 0 };
  }
  for (const name of names) {
    const info = await stat(join(root, name)).catch(() => null);
    if (info === null) continue;
    if (info.isDirectory()) {
      const sub = await dirSize(join(root, name), depth + 1);
      bytes += sub.bytes;
      files += sub.files;
    } else {
      bytes += info.size;
      files += 1;
    }
  }
  return { bytes, files };
}

/** 清空目录内容但保留目录本身（白名单目标专用）。 */
async function clearDirContents(root: string): Promise<number> {
  let removed = 0;
  let names: string[] = [];
  try {
    names = await readdir(root);
  } catch {
    return 0;
  }
  const { rm } = await import('node:fs/promises');
  for (const name of names) {
    await rm(join(root, name), { recursive: true, force: true }).catch(() => undefined);
    removed += 1;
  }
  return removed;
}

// ── 检查更新 ────────────────────────────────────────────────────────────────

interface UpdateInfo {
  version: string;
  local: { commit: string; branch: string; summary: string };
  remote: { commit: string; summary: string } | null;
  behind: number | null;
  upToDate: boolean | null;
  error: string | null;
  checkedAt: number;
}

async function checkUpdate(deps: WebuiDeps): Promise<UpdateInfo> {
  const base: UpdateInfo = {
    version: deps.packageVersion,
    local: { commit: '', branch: '', summary: '' },
    remote: null,
    behind: null,
    upToDate: null,
    error: null,
    checkedAt: Date.now(),
  };
  try {
    const head = await git(deps.repoDir, ['log', '-1', '--format=%H%x09%s']);
    const [commit, summary] = head.split('\t');
    base.local = { commit: (commit ?? '').slice(0, 12), branch: await git(deps.repoDir, ['rev-parse', '--abbrev-ref', 'HEAD']), summary: summary ?? '' };
  } catch (error) {
    base.error = `读取本地 git 失败: ${error instanceof Error ? error.message : String(error)}`;
    return base;
  }
  try {
    const lsRemote = await git(deps.repoDir, ['ls-remote', 'origin', 'refs/heads/master', 'refs/heads/main'], 15_000);
    const remoteLine = lsRemote.split('\n').find((line) => line.trim() !== '');
    const remoteCommit = remoteLine?.split(/\s+/)[0] ?? '';
    if (remoteCommit === '') {
      base.error = '远端没有 master/main 分支';
      return base;
    }
    base.remote = { commit: remoteCommit.slice(0, 12), summary: '' };
    base.upToDate = base.local.commit !== '' && remoteCommit.startsWith(base.local.commit);
    if (!base.upToDate) {
      const behind = await git(deps.repoDir, ['rev-list', '--count', `HEAD..${remoteCommit}`], 15_000).catch(() => '');
      base.behind = behind !== '' && /^\d+$/.test(behind.trim()) ? Number(behind.trim()) : null;
    } else {
      base.behind = 0;
    }
  } catch (error) {
    base.error = `查询远端失败（离线属正常）: ${error instanceof Error ? error.message : String(error)}`;
  }
  return base;
}

function git(cwd: string, args: string[], timeoutMs = 10_000): Promise<string> {
  return new Promise((resolvePromise, rejectPromise) => {
    execFile('git', args, { cwd, timeout: timeoutMs, env: { ...process.env, HOME: process.env.HOME || '/root' } }, (error, stdout) => {
      if (error) rejectPromise(error);
      else resolvePromise(stdout.trim());
    });
  });
}

// ── 模型配置掩码（沿用原实现） ───────────────────────────────────────────────

const KEY_MASK = '•••';

/** 下发前掩码：每个上游的 api_key 显示为 •••+尾4 位（够辨认哪个 key，不泄露）。 */
function maskModelsConfig(content: string): string {
  let parsed: { upstreams?: Record<string, unknown>[] };
  try {
    parsed = JSON.parse(content) as { upstreams?: Record<string, unknown>[] };
  } catch {
    return content; // 不是合法 JSON 就原样给（编辑器里也要能看到坏内容）
  }
  if (!Array.isArray(parsed.upstreams)) return content;
  for (const upstream of parsed.upstreams) {
    const key = String(upstream.api_key ?? '');
    if (key !== '') upstream.api_key = KEY_MASK + key.slice(-4);
  }
  return JSON.stringify(parsed, null, 2);
}

/** 保存前还原：仍是掩码形态的 key 按上游名从旧文件取真值；对不上名就报错。 */
function unmaskModelsConfig(content: string, previous: string): { content: string; error: string | null } {
  let parsed: { upstreams?: Record<string, unknown>[] };
  try {
    parsed = JSON.parse(content) as { upstreams?: Record<string, unknown>[] };
  } catch {
    return { content, error: null }; // 交给后面的 JSON 校验报错
  }
  if (!Array.isArray(parsed.upstreams)) return { content, error: null };
  let oldUpstreams: Record<string, unknown>[] = [];
  try {
    const old = JSON.parse(previous) as { upstreams?: Record<string, unknown>[] };
    if (Array.isArray(old.upstreams)) oldUpstreams = old.upstreams;
  } catch {
    oldUpstreams = [];
  }
  for (const upstream of parsed.upstreams) {
    const key = String(upstream.api_key ?? '');
    if (!key.startsWith(KEY_MASK)) continue;
    const name = String(upstream.name ?? '');
    const original = oldUpstreams.find((o) => String(o.name ?? '') === name);
    const originalKey = String(original?.api_key ?? '');
    if (originalKey === '') {
      return { content, error: `上游 ${name} 的 api_key 是掩码且找不到原值，请填完整 key` };
    }
    upstream.api_key = originalKey;
  }
  return { content: JSON.stringify(parsed, null, 2), error: null };
}

/** models_config.json 入库前校验：结构不对直接 400，绝不写盘（写坏 = 全场模型不可用）。 */
function validateModelsConfig(content: string): string | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(content) as Record<string, unknown>;
  } catch (error) {
    return `JSON 解析失败: ${error instanceof Error ? error.message : String(error)}`;
  }
  if (typeof parsed !== 'object' || parsed === null) return '顶层必须是对象';
  if (!Array.isArray(parsed.upstreams)) return 'upstreams 必须是数组';
  if (!Array.isArray(parsed.channels)) return 'channels 必须是数组';
  const names = new Set<string>();
  for (const [index, upstream] of parsed.upstreams.entries()) {
    const u = upstream as Record<string, unknown>;
    const name = String(u?.name ?? '').trim();
    if (name === '') return `upstreams[${index}] 缺 name`;
    if (String(u?.base_url ?? '').trim() === '') return `上游 ${name} 缺 base_url`;
    if (names.has(name)) return `上游名重复: ${name}`;
    names.add(name);
  }
  for (const [index, channel] of parsed.channels.entries()) {
    const c = channel as Record<string, unknown>;
    const name = String(c?.name ?? '').trim();
    if (name === '') return `channels[${index}] 缺 name`;
    if (!Array.isArray(c?.models) || c.models.length === 0) return `渠道 ${name} 的 models 不能为空`;
    for (const entry of c.models as Record<string, unknown>[]) {
      const upstreamName = String(entry?.upstream ?? '').trim();
      if (!names.has(upstreamName)) return `渠道 ${name} 引用了不存在的上游: ${upstreamName}`;
      if (String(entry?.model_id ?? '').trim() === '') return `渠道 ${name} 的模型缺 model_id`;
    }
  }
  if (parsed.roles !== undefined && (typeof parsed.roles !== 'object' || parsed.roles === null || Array.isArray(parsed.roles))) {
    return 'roles 必须是对象';
  }
  return null;
}

function readBody(request: IncomingMessage): Promise<unknown> {
  return new Promise((resolvePromise, rejectPromise) => {
    const chunks: Buffer[] = [];
    let total = 0;
    request.on('data', (chunk: Buffer) => {
      total += chunk.byteLength;
      if (total > MAX_BODY) {
        rejectPromise(new Error('请求体太大'));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      try {
        resolvePromise(JSON.parse(Buffer.concat(chunks).toString('utf-8') || '{}'));
      } catch {
        rejectPromise(new Error('请求体不是合法 JSON'));
      }
    });
    request.on('error', rejectPromise);
  });
}

function sendJson(response: ServerResponse, statusCode: number, payload: unknown): void {
  response.writeHead(statusCode, { 'content-type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(payload));
}
