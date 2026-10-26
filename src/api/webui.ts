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
import { execFile } from 'node:child_process';
import { mkdir, readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { join, resolve, sep } from 'node:path';
import type { AgentRecord } from '../store/repository.js';
import type { UsageSnapshot } from '../models/usage.js';
import type { KbEntry, KbMount, KbPendingItem, KbPermission } from '../knowledge/kb.js';
import type { PersonRecord, ScopeRecord } from '../relations/graph.js';
import type { PluginInfo, PluginToolDetail, PluginTryResult } from '../chat/plugins.js';
import { webFetch } from '../chat/net.js';
import { probeReset, probeSnapshot } from '../observability/probe.js';

export interface WebuiDeps {
  /** 号主 QQ：主AI会话禁止被关（防失联），同 scope_manage 的护栏。 */
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
  /** 主AI会话历史（private:masterQq），面板对话轮询用。 */
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
  if (!authorized(request, url, deps)) {
    sendJson(response, 401, { error: 'unauthorized' });
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
      const masterId = String(deps.masterQq);
      agents.sort((a, b) => {
        const am = a.scopeType === 'private' && a.scopeId === masterId ? 0 : 1;
        const bm = b.scopeType === 'private' && b.scopeId === masterId ? 0 : 1;
        if (am !== bm) return am - bm;
        if (a.scopeType !== b.scopeType) return a.scopeType === 'private' ? -1 : 1;
        return a.scopeId.localeCompare(b.scopeId);
      });
      sendJson(response, 200, agents.map((a) => ({
        scopeType: a.scopeType,
        scopeId: a.scopeId,
        displayName: a.displayName,
        enabled: a.enabled !== false,
        isMaster: a.scopeType === 'private' && a.scopeId === masterId,
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
        if (body.enabled === false && scopeType === 'private' && scopeId === String(deps.masterQq)) {
          sendJson(response, 400, { error: '主AI会话不能关闭（防失联）' });
          return;
        }
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
      // 留备份再覆盖：改坏了还能从 .bak 捞回来。
      if (previous !== '') {
        await writeFile(`${deps.modelsConfigPath}.bak-${Date.now()}`, previous, 'utf-8');
      }
      await writeFile(deps.modelsConfigPath, content, 'utf-8');
      await deps.reloadModels();
      sendJson(response, 200, { ok: true });
      return;
    }

    if (request.method === 'GET' && url.pathname === '/api/models') {
      sendJson(response, 200, { text: deps.models.summaryText() });
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
      sendJson(response, 200, napcatSettings(napcat));
      return;
    }

    if (request.method === 'POST' && url.pathname === '/api/napcat/config') {
      const body = (await readBody(request)) as Record<string, unknown>;
      const config = await readConfigJson(deps.configPath);
      const previous = config.napcat ?? {};
      const mode = String(body.mode ?? previous.mode ?? '').trim();
      const reverseWsPort = Number(body.reverseWsPort ?? previous.reverse_ws_port ?? 0);
      const selfId = Number(body.selfId ?? previous.self_id ?? 0);
      const wsUrl = restoreUrlSecrets(
        String(body.wsUrl ?? previous.ws_url ?? '').trim(),
        String(previous.ws_url ?? ''),
      );
      const httpUrl = String(body.httpUrl ?? previous.http_url ?? '').trim();
      const publicBaseUrl = String(body.publicBaseUrl ?? previous.public_base_url ?? '').trim().replace(/\/+$/, '');
      if (mode !== 'server' && mode !== 'client') {
        sendJson(response, 400, { error: '连接方式只能是 server 或 client' });
        return;
      }
      if (!Number.isInteger(reverseWsPort) || reverseWsPort < 0 || reverseWsPort > 65535 || (mode === 'server' && reverseWsPort === 0)) {
        sendJson(response, 400, { error: '监听端口须为 1–65535；主动连接模式可填 0' });
        return;
      }
      if (!Number.isSafeInteger(selfId) || selfId < 0) {
        sendJson(response, 400, { error: '机器人 QQ 必须是非负整数，未知时可填 0' });
        return;
      }
      if (!validConfigUrl(wsUrl, ['ws:', 'wss:']) || !validConfigUrl(httpUrl, ['http:', 'https:']) || !validConfigUrl(publicBaseUrl, ['http:', 'https:'])) {
        sendJson(response, 400, { error: '地址格式不正确：WS 用 ws:// 或 wss://，HTTP 用 http:// 或 https://' });
        return;
      }
      if (mode === 'client' && wsUrl === '') {
        sendJson(response, 400, { error: '主动连接 NapCat 时必须填写 WebSocket 地址' });
        return;
      }
      if (wsUrl.includes(SECRET_MASK)) {
        sendJson(response, 400, { error: 'WS 地址中的 Token 掩码无法还原，请重新填写完整地址' });
        return;
      }
      const submittedToken = String(body.accessToken ?? '').trim();
      const accessToken = submittedToken === '' || submittedToken === SECRET_MASK
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
      const before = await readFile(deps.configPath, 'utf-8').catch(() => '');
      if (before !== '') await writeFile(`${deps.configPath}.bak-${Date.now()}`, before, 'utf-8');
      await writeFile(deps.configPath, JSON.stringify(config, null, 2), 'utf-8');
      sendJson(response, 200, { ok: true, restartRequired: true });
      return;
    }

    // ── Onebot 桥 · 关系网 ─────────────────────────────────────────────────
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
      });
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
  if (deps.token === '') return true;
  const bearer = /^Bearer (.+)$/.exec(String(request.headers.authorization ?? ''));
  const presented = bearer?.[1] ?? url.searchParams.get('token') ?? '';
  return presented === deps.token;
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
  ai?: { summary_enabled?: boolean; intel_enabled?: boolean };
  napcat?: Record<string, unknown>;
  [key: string]: unknown;
}

async function readConfigJson(configPath: string): Promise<ConfigJson> {
  try {
    return JSON.parse(await readFile(configPath, 'utf-8')) as ConfigJson;
  } catch {
    return {};
  }
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
  const config = await readConfigJson(configPath);
  config.ai = config.ai ?? {};
  if (flags.summaryEnabled !== undefined) config.ai.summary_enabled = flags.summaryEnabled === true;
  if (flags.intelEnabled !== undefined) config.ai.intel_enabled = flags.intelEnabled === true;
  const previous = await readFile(configPath, 'utf-8').catch(() => '');
  if (previous !== '') await writeFile(`${configPath}.bak-${Date.now()}`, previous, 'utf-8');
  await writeFile(configPath, JSON.stringify(config, null, 2), 'utf-8');
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
