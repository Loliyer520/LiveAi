/**
 * Declarative API plugin system — the base layer for giving the model new
 * capabilities WITHOUT code changes:
 *
 *   data/plugins/<name>/plugin.json   manifest (see PluginManifest below)
 *   data/plugins/secrets.json         flat {KEY: value} API-key store
 *   data/plugins/_state.json          per-plugin enabled overrides
 *
 * A manifest declares HTTP tools: JSON-schema parameters, a request template
 * with {{param}} / {{secrets.KEY}} interpolation, and an optional response
 * extractor/template. Dropping a folder into the directory + plugin_manage
 * reload makes the tools live; no rebuild, no restart, no prompt edits.
 *
 * Safety: every plugin request goes through the SSRF-guarded webFetch and the
 * per-turn network budget (a manifest may declare `allow_internal: true` on a
 * tool to reach loopback/private services — a master-only decision since
 * manifests live on disk); secret values are scrubbed from tool output; a
 * broken manifest is quarantined (recorded, skipped) instead of crashing
 * anything.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { ToolSchema } from '../models/types.js';
import type { ToolContext, ToolResult } from './tools.js';
import { webFetch } from './net.js';
import { spendNetworkBudget, sendLinesVerbatim, SEND_FILE_MAX_LINES } from './actions.js';
import { probeRecord } from '../observability/probe.js';

export interface PluginToolRequest {
  method?: string;
  url: string;
  headers?: Record<string, string>;
  body?: string;
}

export interface PluginToolResponse {
  format?: 'json' | 'text';
  /** Dot path into the JSON body (e.g. `data.list[0].name`); whole body when omitted. */
  extract?: string;
  /** Output template with {{dot.path}} placeholders resolved against the JSON body, then params. */
  template?: string;
  /** APIs that signal failure in-band: compare this path's value to ok_value. */
  code_path?: string;
  ok_value?: unknown;
  /** Failure message path (default 'msg'). */
  msg_path?: string;
  /** Render an array from the body: `from` supports `a.b[].c` flattening. */
  list?: { from: string; item: string; max?: number | string; separator?: string };
  /** Persist a body subtree into the scope workspace (for file_* / send_file_excerpt). */
  save?: {
    from: string;
    path: string;
    html_to_text?: boolean;
    /** Truthy after interpolation ("true"/"1"/"yes") → also deliver the saved text verbatim, line by line. */
    send?: string;
  };
  /**
   * Batch variant of save for map-shaped bodies (`{itemId: item, …}`): iterate
   * every entry, per-item save (+ optional verbatim send). `{{__key}}` in
   * path/item templates resolves to the map key; other placeholders resolve
   * against the item, then tool params. One round-trip instead of N calls —
   * the agent loop re-bills the whole prompt per tool call, so bulk endpoints
   * must be consumed in bulk.
   */
  save_each?: {
    from: string;
    path: string;
    html_to_text?: boolean;
    /** Truthy after interpolation → also deliver each item's text verbatim. */
    send?: string;
  };
}

export interface PluginToolDef {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  /** true = only the master scope sees this tool. */
  master_only?: boolean;
  /**
   * 可见 scope 白名单（与 master_only 互斥、优先）：列出 scope key
   * （如 "domain:mc"）、"master" 占位符（=主AI scope）或裸类型名
   * （如 "bili"/"mcclient" = 该类型全部 scope，领域中枢的下级 AI 用）。
   * 中枢把工具下放给领域主级 AI 时用它——主AI 收敛后不再持有领域执行工具。
   */
  scopes?: string[];
  /**
   * true = this tool's URL may point at loopback/private addresses (skips the
   * SSRF address check). Only settable in the on-disk manifest — never from
   * model input — so it stays a master decision (e.g. local novel API).
   */
  allow_internal?: boolean;
  request: PluginToolRequest;
  response?: PluginToolResponse;
}

/**
 * 面板可编辑的配置项声明（manifest v2）。值存
 * data/plugins/_config.json（{插件名: {KEY: 值}}），请求模板用
 * {{config.KEY}} 引用——密钥仍走 secrets.json，config 放非敏感设置。
 */
export interface PluginConfigField {
  title: string;
  description?: string;
  type: 'string' | 'number' | 'boolean';
  default?: string | number | boolean;
}

export interface PluginManifest {
  name: string;
  description?: string;
  enabled?: boolean;
  /** manifest v2：插件版本（面板展示/市场对版本用）。 */
  version?: string;
  /** manifest v2：要求的最小核心版本（semver 字符串，可缺省）。 */
  minCore?: string;
  /** manifest v2：面板自动生成配置表单。 */
  config?: Record<string, PluginConfigField>;
  tools: PluginToolDef[];
}

export interface PluginInfo {
  name: string;
  description: string;
  enabled: boolean;
  tools: string[];
  error?: string;
}

/** Panel-facing tool spec (see PluginRegistry.toolDetails); no URLs/secrets. */
export interface PluginToolDetail {
  name: string;
  description: string;
  method: string;
  params: { key: string; type: string; description: string; required: boolean }[];
  allowInternal: boolean;
  usesSecrets: boolean;
  /** URL/头/体里引用了 {{config.*}}（manifest v2）。 */
  usesConfig: boolean;
  /** 引用了 {{config.*}} 但对应配置项还没有值 → 面板提示先保存配置。 */
  configMissing: boolean;
  /** GET + 无密钥 + 无副作用 → 面板可直接试玩。 */
  tryable: boolean;
}

export interface PluginTryResult {
  ok: boolean;
  status?: number;
  ms?: number;
  body?: string;
  error?: string;
}

const NAME_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
const PLACEHOLDER = /\{\{\s*([\w.[\]]+)\s*\}\}/g;
const MAX_OUTPUT_CHARS = 4000;
const MAX_SAVE_CHARS = 2_000_000;
/** save_each 单次处理的条目上限（批量章节接口 20 章可达 1MB+，更多让模型分批）。 */
const SAVE_EACH_MAX_ITEMS = 15;

/**
 * scopes 白名单条目匹配："master"=主AI scope；精确 scope key；"type" 裸
 * 类型名=该类型全部 scope（如 "bili" 命中 bili:BV1xx，"mcclient" 命中各
 * MC client 下级 AI）。
 */
function scopeEntryMatches(entry: string, scopeKey: string, includeMaster: boolean): boolean {
  if (entry === 'master') return includeMaster;
  if (entry === scopeKey) return true;
  if (!entry.includes(':')) {
    const separator = scopeKey.indexOf(':');
    return entry === (separator === -1 ? scopeKey : scopeKey.slice(0, separator));
  }
  return false;
}

export class PluginRegistry {
  private manifests = new Map<string, PluginManifest>();
  private errors = new Map<string, string>();
  private enabledOverrides: Record<string, boolean> = {};
  private secrets: Record<string, string> = {};
  /** manifest v2 配置值：{插件名: {KEY: 值}}（_config.json，含默认值回填）。 */
  private configValues: Record<string, Record<string, string | number | boolean>> = {};

  constructor(readonly dir: string) {}

  /** Synchronous (re)scan; called at startup and by plugin_manage reload. */
  load(): void {
    this.manifests.clear();
    this.errors.clear();
    this.enabledOverrides = this.readJsonFile<Record<string, boolean>>(join(this.dir, '_state.json')) ?? {};
    this.secrets = this.readJsonFile<Record<string, string>>(join(this.dir, 'secrets.json')) ?? {};
    this.configValues = this.readJsonFile<Record<string, Record<string, string | number | boolean>>>(join(this.dir, '_config.json')) ?? {};
    if (!existsSync(this.dir)) return;
    for (const entry of readdirSync(this.dir, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith('_') || entry.name.startsWith('.')) continue;
      this.loadOne(entry.name);
    }
    // v2 配置项缺省值回填（只回填内存，不动磁盘——首次保存才落盘）。
    for (const manifest of this.manifests.values()) {
      for (const [key, field] of Object.entries(manifest.config ?? {})) {
        if (this.configValues[manifest.name]?.[key] === undefined && field.default !== undefined) {
          (this.configValues[manifest.name] ??= {})[key] = field.default;
        }
      }
    }
  }

  private loadOne(folder: string): void {
    const manifestPath = join(this.dir, folder, 'plugin.json');
    try {
      const raw = this.readJsonFile<PluginManifest>(manifestPath);
      if (!raw) throw new Error('plugin.json 缺失或不是合法 JSON');
      const manifest = this.validate(raw);
      if (this.manifests.has(manifest.name)) throw new Error(`插件名 ${manifest.name} 重复`);
      this.manifests.set(manifest.name, manifest);
    } catch (error) {
      this.errors.set(folder, error instanceof Error ? error.message : String(error));
    }
  }

  private validate(raw: PluginManifest): PluginManifest {
    const name = String(raw.name ?? '').trim();
    if (!NAME_PATTERN.test(name)) throw new Error(`插件名不合法: ${name || '(空)'}（小写字母开头的标识符）`);
    if (!Array.isArray(raw.tools) || raw.tools.length === 0) throw new Error('tools 为空');
    const seen = new Set<string>();
    for (const tool of raw.tools) {
      const toolName = String(tool?.name ?? '').trim();
      if (!NAME_PATTERN.test(toolName)) throw new Error(`工具名不合法: ${toolName || '(空)'}`);
      if (seen.has(toolName)) throw new Error(`工具名 ${toolName} 在插件内重复`);
      seen.add(toolName);
      if (!String(tool.description ?? '').trim()) throw new Error(`工具 ${toolName} 缺 description`);
      if (!String(tool.request?.url ?? '').trim()) throw new Error(`工具 ${toolName} 缺 request.url`);
    }
    return { ...raw, name };
  }

  private readJsonFile<T>(path: string): T | null {
    try {
      if (!existsSync(path)) return null;
      return JSON.parse(readFileSync(path, 'utf-8')) as T;
    } catch {
      return null;
    }
  }

  isEnabled(manifest: PluginManifest): boolean {
    return this.enabledOverrides[manifest.name] ?? manifest.enabled ?? true;
  }

  /** Persist an enable/disable override (_state.json; manifests untouched). */
  setEnabled(name: string, enabled: boolean): void {
    if (!this.manifests.has(name)) throw new Error(`没有插件 ${name}`);
    this.enabledOverrides[name] = enabled;
    mkdirSync(this.dir, { recursive: true });
    writeFileSync(join(this.dir, '_state.json'), JSON.stringify(this.enabledOverrides, null, 2), 'utf-8');
  }

  /** manifest v2 配置：声明 + 生效值（面板表单用）。 */
  getConfig(name: string): { spec: Record<string, PluginConfigField>; values: Record<string, string | number | boolean> } | null {
    const manifest = this.manifests.get(name);
    if (!manifest) return null;
    return { spec: manifest.config ?? {}, values: { ...(this.configValues[name] ?? {}) } };
  }

  /** manifest v2 配置保存：只收声明过的 KEY，按声明类型强转后落 _config.json。 */
  setConfig(name: string, values: Record<string, unknown>): Record<string, string | number | boolean> {
    const manifest = this.manifests.get(name);
    if (!manifest) throw new Error(`没有插件 ${name}`);
    const spec = manifest.config ?? {};
    const next: Record<string, string | number | boolean> = {};
    for (const [key, field] of Object.entries(spec)) {
      const incoming = values[key];
      if (incoming === undefined || incoming === null || String(incoming).trim() === '') {
        if (field.default !== undefined) next[key] = field.default;
        continue;
      }
      if (field.type === 'number') {
        const num = Number(incoming);
        if (!Number.isFinite(num)) throw new Error(`配置项 ${key} 须是数字`);
        next[key] = num;
      } else if (field.type === 'boolean') {
        next[key] = incoming === true || /^(1|true|yes|on)$/i.test(String(incoming));
      } else {
        next[key] = String(incoming);
      }
    }
    this.configValues[name] = next;
    mkdirSync(this.dir, { recursive: true });
    writeFileSync(join(this.dir, '_config.json'), JSON.stringify(this.configValues, null, 2), 'utf-8');
    return next;
  }

  listInfo(): PluginInfo[] {
    const infos: PluginInfo[] = [];
    for (const manifest of [...this.manifests.values()].sort((a, b) => a.name.localeCompare(b.name))) {
      infos.push({
        name: manifest.name,
        description: manifest.description ?? '',
        enabled: this.isEnabled(manifest),
        tools: manifest.tools.map((tool) => tool.name),
      });
    }
    for (const [folder, error] of [...this.errors.entries()].sort()) {
      infos.push({ name: folder, description: '', enabled: false, tools: [], error });
    }
    return infos;
  }

  /**
   * Panel detail: per-tool specs for the webui plugin page (params, method,
   * tryable flag). Request URLs and secrets never leave the process — the
   * panel only learns whether a tool uses secrets, never the values.
   */
  toolDetails(name: string): { info: PluginInfo; tools: PluginToolDetail[]; version?: string; minCore?: string } | null {
    const manifest = this.manifests.get(name);
    if (!manifest) return null;
    const tools = manifest.tools.map((tool) => {
      const props = (tool.parameters as { properties?: Record<string, { type?: string; description?: string; default?: unknown }> }).properties ?? {};
      const required = new Set((tool.parameters as { required?: string[] }).required ?? []);
      const templates = [tool.request.url, ...Object.values(tool.request.headers ?? {}), tool.request.body ?? ''].join('\n');
      const usesSecrets = /\{\{\s*secrets\./.test(templates);
      const hasSideEffects = tool.response?.save !== undefined || tool.response?.save_each !== undefined;
      const method = (tool.request.method ?? 'GET').toUpperCase();
      const usesConfig = /\{\{\s*config\./.test(templates);
      const configValues = this.configValues[name] ?? {};
      // 把已解析的 config 占位替换成空后再测——仍剩 {{config.}} = 有配置项缺值。
      const unresolved = /\{\{\s*config\./.test(
        templates.replace(/\{\{\s*config\.([\w.]+)\s*\}\}/g, (whole, key: string) => (configValues[key] !== undefined ? '' : whole)),
      );
      const configMissing = usesConfig && unresolved;
      return {
        name: tool.name,
        description: tool.description,
        method,
        params: Object.entries(props).map(([key, spec]) => ({
          key,
          type: typeof spec?.type === 'string' ? spec.type : 'string',
          description: spec?.description ?? '',
          required: required.has(key),
        })),
        allowInternal: tool.allow_internal === true,
        usesSecrets,
        usesConfig,
        configMissing,
        tryable: method === 'GET' && !usesSecrets && !hasSideEffects,
      };
    });
    return {
      info: {
        name: manifest.name,
        description: manifest.description ?? '',
        enabled: this.isEnabled(manifest),
        tools: manifest.tools.map((tool) => tool.name),
      },
      version: manifest.version,
      minCore: manifest.minCore,
      tools,
    };
  }

  /** Origin of the first GET/secrets-free tool — the panel health-probe target. */
  serviceOrigin(name: string): { url: string; allowInternal: boolean } | null {
    const manifest = this.manifests.get(name);
    if (!manifest) return null;
    for (const tool of manifest.tools) {
      if ((tool.request.method ?? 'GET').toUpperCase() !== 'GET') continue;
      if (/\{\{\s*secrets\./.test(tool.request.url)) continue;
      try {
        return { url: new URL(tool.request.url).origin, allowInternal: tool.allow_internal === true };
      } catch {
        // unparsable template → try the next tool
      }
    }
    return null;
  }

  /**
   * Panel "试玩": run a GET, secrets-free, side-effect-free tool with plain
   * args — no model context, no scope checks, no workspace writes. Output is
   * secret-scrubbed and capped for display.
   */
  async tryTool(pluginName: string, toolName: string, args: Record<string, unknown>): Promise<PluginTryResult> {
    const manifest = this.manifests.get(pluginName);
    if (!manifest) return { ok: false, error: `没有插件 ${pluginName}` };
    if (!this.isEnabled(manifest)) return { ok: false, error: `插件 ${pluginName} 已停用` };
    const tool = manifest.tools.find((entry) => entry.name === toolName);
    if (!tool) return { ok: false, error: `插件 ${pluginName} 没有工具 ${toolName}` };
    if ((tool.request.method ?? 'GET').toUpperCase() !== 'GET') return { ok: false, error: '试玩只支持 GET 工具' };
    if (/\{\{\s*secrets\./.test(tool.request.url)) return { ok: false, error: '带密钥的工具不支持试玩' };
    if (tool.response?.save !== undefined || tool.response?.save_each !== undefined) {
      return { ok: false, error: '会写工作区/发文件的工具不支持试玩' };
    }
    const required = (tool.parameters as { required?: string[] }).required ?? [];
    const missing = required.filter((key) => args[key] === undefined || args[key] === null || String(args[key]).trim() === '');
    if (missing.length > 0) return { ok: false, error: `缺少必填参数 ${missing.join('、')}` };
    let url: string;
    try {
      const configValues = this.configValues[pluginName] ?? {};
      url = interpolate(tool.request.url, (key) => {
        if (key.startsWith('config.')) {
          const value = configValues[key.slice('config.'.length)];
          if (value === undefined) throw new Error(`配置项 ${key} 没有值（先在面板保存插件配置）`);
          return String(value); // 结构性值（如基址），不编码
        }
        const value = args[key];
        if (value === undefined || value === null) throw new Error(`参数 ${key} 缺失`);
        return encodeURIComponent(typeof value === 'object' ? JSON.stringify(value) : String(value));
      });
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
    const started = Date.now();
    try {
      const result = await webFetch(url, {
        method: 'GET',
        maxBytes: 256 * 1024,
        displayLimit: 8000,
        allowInternal: tool.allow_internal === true,
      });
      probeRecord('plugin.try', Date.now() - started, { tool: toolName, ok: result.status < 400 ? 1 : 0 });
      return {
        ok: result.status < 400,
        status: result.status,
        ms: Date.now() - started,
        body: this.scrub(result.body, this.secretValuesForScrub()).slice(0, 8000),
      };
    } catch (error) {
      probeRecord('plugin.try', Date.now() - started, { tool: toolName, ok: 0 });
      return { ok: false, ms: Date.now() - started, error: error instanceof Error ? error.message : String(error) };
    }
  }

  /** Tool schemas exposed to the model; reservedNames (builtin) win collisions. */
  toolSchemas(scopeKey: string, includeMaster: boolean, reservedNames: Set<string>): ToolSchema[] {
    const schemas: ToolSchema[] = [];
    for (const manifest of this.manifests.values()) {
      if (!this.isEnabled(manifest)) continue;
      for (const tool of manifest.tools) {
        if (tool.scopes !== undefined) {
          const visible = tool.scopes.some((entry) => scopeEntryMatches(entry, scopeKey, includeMaster));
          if (!visible) continue;
        } else if (tool.master_only === true && !includeMaster) {
          continue;
        }
        if (reservedNames.has(tool.name)) {
          this.errors.set(manifest.name, `工具名 ${tool.name} 与内置工具冲突，已跳过`);
          continue;
        }
        schemas.push({
          name: tool.name,
          description: `${tool.description}（插件 ${manifest.name}）`,
          parameters: tool.parameters,
        });
      }
    }
    return schemas;
  }

  /** Execute a plugin tool; null when no enabled plugin owns the name. */
  /** 探针包装：插件工具调用的耗时/失败率进面板性能页。 */
  async execute(toolName: string, args: Record<string, unknown>, context: ToolContext): Promise<ToolResult | null> {
    const started = Date.now();
    try {
      const result = await this.executeInner(toolName, args, context);
      probeRecord('plugin.call', Date.now() - started, { tool: toolName, ok: result?.ok === true ? 1 : 0 });
      return result;
    } catch (error) {
      probeRecord('plugin.call', Date.now() - started, { tool: toolName, ok: 0 });
      throw error;
    }
  }

  private async executeInner(toolName: string, args: Record<string, unknown>, context: ToolContext): Promise<ToolResult | null> {
    let owner: PluginManifest | null = null;
    let def: PluginToolDef | null = null;
    for (const manifest of this.manifests.values()) {
      if (!this.isEnabled(manifest)) continue;
      const found = manifest.tools.find((tool) => tool.name === toolName);
      if (found) {
        owner = manifest;
        def = found;
        break;
      }
    }
    if (!owner || !def) return null;

    // 可见性硬检查（与 toolSchemas 同步）：声明了 scopes 的工具只对白名单
    // scope 放行——模型在别的会话里猜出工具名也执行不了。
    const requesterScope = `${context.scopeType}:${context.scopeId}`;
    if (def.scopes !== undefined) {
      const visible = def.scopes.some((entry) => scopeEntryMatches(entry, requesterScope, context.isMaster));
      if (!visible) return { ok: false, output: `${toolName} 未挂载给本会话（${requesterScope}），不可执行。` };
    } else if (def.master_only === true && !context.isMaster) {
      return { ok: false, output: `${toolName} 仅主AI可用。` };
    }

    try {
      spendNetworkBudget(context);

      // Required-parameter check (models occasionally drop them).
      const required = (def.parameters as { required?: string[] }).required ?? [];
      const missing = required.filter((key) => args[key] === undefined || args[key] === null || String(args[key]).trim() === '');
      if (missing.length > 0) return { ok: false, output: `${toolName}: 缺少必填参数 ${missing.join('、')}。` };

      // Fill JSON-schema defaults for omitted optional params.
      const properties = (def.parameters as { properties?: Record<string, { default?: unknown }> }).properties ?? {};
      args = { ...args };
      for (const [key, spec] of Object.entries(properties)) {
        if (args[key] === undefined && spec && typeof spec === 'object' && 'default' in spec) args[key] = spec.default;
      }

      const secretValues = this.secretValuesForScrub();
      const url = interpolate(def.request.url, (key) => this.resolveValue(key, owner.name, args, context, true));
      const headers: Record<string, string> = {};
      for (const [key, template] of Object.entries(def.request.headers ?? {})) {
        headers[key] = interpolate(template, (key2) => this.resolveValue(key2, owner.name, args, context, false));
      }
      const body = def.request.body === undefined
        ? undefined
        : interpolate(def.request.body, (key) => this.resolveValue(key, owner.name, args, context, false));

      // displayLimit ≥ maxBytes: plugins must parse the FULL JSON body;
      // the model-facing truncation happens after extract/template below.
      // maxBytes 同步放宽到 2MB——批量章节接口一次十几章正文就有 1MB+，
      // 512KB 会把 JSON 截成非法体。
      const result = await webFetch(url, {
        method: def.request.method ?? 'GET',
        headers,
        body,
        maxBytes: 2 * 1024 * 1024,
        displayLimit: 2 * 1024 * 1024,
        allowInternal: def.allow_internal === true,
      });
      let output: string;
      let saveNote = '';
      let savedAndSent = false;
      const response = def.response ?? {};
      const format = response.format ?? 'json';
      if (format === 'json') {
        let parsed: unknown;
        try {
          parsed = JSON.parse(result.body);
        } catch {
          output = `接口返回的不是 JSON（HTTP ${result.status}）：\n${result.body.slice(0, 1000)}`;
          return { ok: result.status < 400, output: this.scrub(output, secretValues) };
        }
        // In-band error codes (e.g. novel API: code=1 success / code=0 failure).
        if (response.code_path) {
          const code = extractPath(parsed, response.code_path);
          const okValue = response.ok_value ?? 0;
          if (String(code) !== String(okValue)) {
            const msg = extractPath(parsed, response.msg_path ?? 'msg');
            return {
              ok: false,
              output: this.scrub(`接口返回失败（${response.code_path}=${String(code)}）：${String(msg ?? '未知错误')}`, secretValues),
            };
          }
        }
        if (response.save_each) {
          const batched = await this.saveEachToWorkspace(response.save_each, owner.name, parsed, args, context);
          saveNote = batched.note;
          savedAndSent = batched.sentToUser;
        }
        if (response.save) {
          const saved = await this.saveToWorkspace(response.save, owner.name, parsed, args, context);
          saveNote = saved.note;
          savedAndSent = saved.sentToUser;
        }
        if (response.template) {
          try {
            output = interpolate(response.template, (key) => {
              const fromBody = extractPath(parsed, key);
              if (fromBody !== undefined) return typeof fromBody === 'object' ? JSON.stringify(fromBody, null, 0) : String(fromBody);
              return this.resolveValue(key, owner.name, args, context, false);
            });
          } catch (error) {
            // 模板字段在响应里不存在（如上游抖动时 200 + 缺 data.title 的残缺体）。
            // 把接口自带的错误说明翻出来给模型一个可判断的失败——而不是
            // 「参数 data.title 缺失」这种让模型原样转述给用户、又无法重试的黑话。
            const keyMatch = error instanceof Error ? /^参数 (.+) 缺失$/.exec(error.message) : null;
            if (keyMatch === null) throw error;
            const why = [extractPath(parsed, response.msg_path ?? 'msg'), extractPath(parsed, 'data.error')]
              .filter((v): v is string => typeof v === 'string' && v.trim() !== '')
              .join('；');
            const savedNote = savedAndSent ? '（注意：data.content 其实已保存/发出，不要重复发送）' : '';
            return {
              ok: false,
              output: this.scrub(
                `接口响应缺少字段 ${keyMatch[1]}${why ? `，接口报错：${why}` : ''}（响应开头：${result.body.slice(0, 300)}）${savedNote}`
                  + '。多半是上游临时抖动，请原样重试一次；仍失败就如实告诉用户暂时取不到、稍后再试，不要编造内容。',
                secretValues,
              ),
            };
          }
        } else if (response.list) {
          output = this.renderList(response.list, owner.name, parsed, args, context);
        } else {
          const extracted = response.extract ? extractPath(parsed, response.extract) : parsed;
          if (response.extract && extracted === undefined) {
            return { ok: false, output: this.scrub(`提取路径 ${response.extract} 在响应里不存在。响应开头：${result.body.slice(0, 500)}`, secretValues) };
          }
          output = typeof extracted === 'string' ? extracted : JSON.stringify(extracted, null, 2);
        }
      } else {
        output = result.body;
      }
      if (output.length > MAX_OUTPUT_CHARS) output = `${output.slice(0, MAX_OUTPUT_CHARS)}\n…（已截断）`;
      const statusNote = result.status >= 400 ? `（HTTP ${result.status}）` : '';
      return { ok: result.status < 400, output: this.scrub(`${output}${saveNote}${statusNote}`, secretValues), sentToUser: savedAndSent || undefined };
    } catch (error) {
      // 异常消息也可能带密钥（比如 fetch 报错里拼的完整 URL），同样要过脱敏。
      return {
        ok: false,
        output: this.scrub(`${toolName} 调用失败: ${error instanceof Error ? error.message : String(error)}`, this.secretValuesForScrub()),
      };
    }
  }

  /** 待脱敏的密钥形态：原值 + URL 编码形态（响应可能回显编码后的查询串）。
   *  下限 4 字符：过短的值误伤面太大，但 4~5 位的短 appid 也得盖住。 */
  private secretValuesForScrub(): string[] {
    return Object.values(this.secrets)
      .filter((value) => value.trim().length >= 4)
      .flatMap((value) => [value, encodeURIComponent(value)]);
  }

  /** Resolve a {{placeholder}}: secrets.*, config.*, scope.*, then tool params. */
  private resolveValue(key: string, pluginName: string, args: Record<string, unknown>, context: ToolContext, encode: boolean): string {
    let raw: string | undefined;
    if (key.startsWith('config.')) {
      const value = this.configValues[pluginName]?.[key.slice('config.'.length)];
      if (value === undefined) throw new Error(`配置项 ${key} 没有值（可在面板插件页设置）`);
      // config 值是面板设置的结构性值（如服务基址），不做 URL 编码。
      return String(value);
    } else if (key.startsWith('secrets.')) {
      raw = this.secrets[key.slice('secrets.'.length)];
      if (raw === undefined) throw new Error(`secrets.json 里没有 ${key}`);
    } else if (key === 'scope.type') {
      raw = context.scopeType;
    } else if (key === 'scope.id') {
      raw = context.scopeId;
    } else {
      const value = args[key];
      if (value === undefined || value === null) throw new Error(`参数 ${key} 缺失`);
      raw = typeof value === 'object' ? JSON.stringify(value) : String(value);
    }
    return encode ? encodeURIComponent(raw) : raw;
  }

  /** Render response.list: flatten `from` (supports `a.b[].c`), template each item, cap at max. */
  private renderList(
    list: NonNullable<PluginToolResponse['list']>,
    pluginName: string,
    parsed: unknown,
    args: Record<string, unknown>,
    context: ToolContext,
  ): string {
    const items = resolveList(parsed, list.from);
    if (items.length === 0) return `（${list.from} 是空的，没有结果）`;
    let max = 20;
    if (typeof list.max === 'number') max = list.max;
    else if (typeof list.max === 'string') {
      try {
        const interpolated = Number(interpolate(list.max, (key) => this.resolveValue(key, pluginName, args, context, false)));
        if (Number.isFinite(interpolated)) max = interpolated;
      } catch {
        // max placeholder unresolvable (param omitted) → keep default
      }
    }
    max = Math.min(Math.max(Math.trunc(max), 1), 300);
    const shown = items.slice(0, max).map((item) =>
      interpolate(list.item, (key) => {
        const fromItem = extractPath(item, key);
        if (fromItem !== undefined) return typeof fromItem === 'object' ? JSON.stringify(fromItem, null, 0) : String(fromItem);
        try {
          return this.resolveValue(key, pluginName, args, context, false);
        } catch {
          return ''; // field missing on both item and params — render blank, don't fail the call
        }
      }),
    );
    const truncated = items.length > max ? `\n…（共 ${items.length} 条，只列了前 ${max} 条）` : '';
    return shown.join(list.separator ?? '\n') + truncated;
  }

  /**
   * Batch save: iterate a map-shaped body (`itemId → item`), per-item
   * html_to_text/save/verbatim-forward. One failing item (上游抖动/锁章) never
   * aborts the rest — it degrades to a per-item line in the note.
   */
  private async saveEachToWorkspace(
    batch: NonNullable<PluginToolResponse['save_each']>,
    pluginName: string,
    parsed: unknown,
    args: Record<string, unknown>,
    context: ToolContext,
  ): Promise<{ note: string; sentToUser: boolean }> {
    const map = extractPath(parsed, batch.from);
    if (map === null || typeof map !== 'object' || Array.isArray(map)) {
      return { note: `\n（批量保存失败：响应里 ${batch.from} 不是对象映射）`, sentToUser: false };
    }
    const entries = Object.entries(map as Record<string, unknown>);
    if (entries.length === 0) return { note: `\n（批量保存失败：${batch.from} 是空映射）`, sentToUser: false };

    // 发送开关整批复用（{{forward}} 之类只与参数有关，逐项求一次即可）。
    let flag = '';
    if (batch.send !== undefined) {
      try {
        flag = interpolate(batch.send, (key) => this.resolveValue(key, pluginName, args, context, false));
      } catch {
        flag = batch.send;
      }
    }
    const wantSend = /^(true|1|yes|是)$/i.test(flag.trim());

    const lines: string[] = [];
    let okCount = 0;
    let sentCount = 0;
    let sentToUser = false;
    for (const [key, raw] of entries.slice(0, SAVE_EACH_MAX_ITEMS)) {
      const item: Record<string, unknown> = raw !== null && typeof raw === 'object' ? raw as Record<string, unknown> : {};
      const title = typeof item.title === 'string' && item.title ? item.title : key;
      const itemError = typeof item.error === 'string' && item.error ? item.error : '';
      const rawText = item.content;
      if (typeof rawText !== 'string' || rawText.trim() === '') {
        lines.push(`《${title}》失败：${itemError || '响应里没有正文'}`);
        continue;
      }
      let text = batch.html_to_text ? htmlToText(rawText) : rawText;
      if (text.length > MAX_SAVE_CHARS) text = `${text.slice(0, MAX_SAVE_CHARS)}\n…（超长已截断）`;
      try {
        const rel = interpolate(batch.path, (k) => this.resolveValue(k, pluginName, { ...args, __key: key }, context, false));
        const abs = context.workspace.resolvePath(rel);
        mkdirSync(dirname(abs), { recursive: true });
        writeFileSync(abs, text, 'utf-8');
        okCount += 1;
        if (wantSend) {
          const sendable = text.split('\n').slice(0, SEND_FILE_MAX_LINES).filter((line) => line.trim());
          const { sent, failure } = await sendLinesVerbatim(context, sendable, 'file-excerpt');
          if (sent > 0) {
            sentCount += 1;
            sentToUser = true;
            lines.push(`《${title}》已存并发给用户`);
          } else {
            lines.push(`《${title}》已存，但发送失败: ${failure || '没有可发送的行'}`);
          }
        } else {
          lines.push(`《${title}》已存 ${context.workspace.displayPath(abs)}`);
        }
      } catch (error) {
        lines.push(`《${title}》失败：${error instanceof Error ? error.message : String(error)}`);
      }
    }
    const overflow = entries.length > SAVE_EACH_MAX_ITEMS
      ? `\n…（响应共 ${entries.length} 项，单次最多处理 ${SAVE_EACH_MAX_ITEMS} 项，剩下的请分批再调）`
      : '';
    const sendNote = wantSend ? `，已发出 ${sentCount} 章` : '';
    return {
      note: `\n（批量完成 ${okCount}/${entries.length} 章${sendNote}：\n${lines.join('\n')}${overflow}）`,
      sentToUser,
    };
  }

  /** Persist a body subtree into the scope workspace; returns a note for the tool output. */
  private async saveToWorkspace(
    save: NonNullable<PluginToolResponse['save']>,
    pluginName: string,
    parsed: unknown,
    args: Record<string, unknown>,
    context: ToolContext,
  ): Promise<{ note: string; sentToUser: boolean }> {
    const none = { note: '', sentToUser: false };
    const value = extractPath(parsed, save.from);
    if (value === undefined) return { ...none, note: `\n（保存失败：响应里没有 ${save.from}）` };
    let text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    if (save.html_to_text) text = htmlToText(text);
    if (text.length > MAX_SAVE_CHARS) text = `${text.slice(0, MAX_SAVE_CHARS)}\n…（超长已截断）`;
    let rel: string;
    try {
      rel = interpolate(save.path, (key) => this.resolveValue(key, pluginName, args, context, false));
      const abs = context.workspace.resolvePath(rel);
      mkdirSync(dirname(abs), { recursive: true });
      writeFileSync(abs, text, 'utf-8');
      rel = context.workspace.displayPath(abs);
    } catch (error) {
      return { ...none, note: `\n（保存失败：${error instanceof Error ? error.message : String(error)}）` };
    }
    let note = `\n（已存到 ${rel}，${text.length} 字符；可用 file_read/file_search/send_file 处理）`;

    // One-shot forwarding: deliver the saved text verbatim when send is truthy.
    if (save.send !== undefined) {
      let flag = '';
      try {
        flag = interpolate(save.send, (key) => this.resolveValue(key, pluginName, args, context, false));
      } catch {
        flag = save.send; // literal string like "true"
      }
      if (/^(true|1|yes|是)$/i.test(flag.trim())) {
        const lines = text.split('\n').slice(0, SEND_FILE_MAX_LINES);
        const sendable = lines.filter((line) => line.trim());
        const { sent, failure } = await sendLinesVerbatim(context, sendable, 'file-excerpt');
        const total = text.split('\n').length;
        const notes: string[] = [];
        if (total > SEND_FILE_MAX_LINES) notes.push(`只发了前 ${SEND_FILE_MAX_LINES} 行，剩余用 send_file from=${SEND_FILE_MAX_LINES + 1} 续发`);
        if (failure) notes.push(`发送中断: ${failure}`);
        if (sent > 0) {
          note += `；已把原文发给用户（${sent} 条消息）${notes.length ? `，${notes.join('，')}` : ''}`;
          return { note, sentToUser: true };
        }
        note += `；发送失败: ${failure || '没有可发送的行'}`;
      }
    }
    return { note, sentToUser: false };
  }

  /** Keep API keys out of anything the model (and history) gets to see. */
  private scrub(text: string, secretValues: string[]): string {
    let cleaned = text;
    for (const value of secretValues) {
      cleaned = cleaned.split(value).join('***');
    }
    return cleaned;
  }
}

/** Dot-path extraction: `a.b[0].c` / `a.b.0.c` both work. */
export function extractPath(value: unknown, path: string): unknown {
  let current = value;
  for (const segment of path.split('.')) {
    const parts = segment.replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean);
    for (const part of parts) {
      if (current === null || current === undefined) return undefined;
      if (Array.isArray(current)) {
        const index = Number(part);
        if (!Number.isInteger(index)) return undefined;
        current = current[index];
      } else if (typeof current === 'object') {
        current = (current as Record<string, unknown>)[part];
      } else {
        return undefined;
      }
    }
  }
  return current;
}

function interpolate(template: string, resolveKey: (key: string) => string): string {
  return template.replace(PLACEHOLDER, (_match, key: string) => resolveKey(key));
}

/** Resolve a list path, flattening `[]` segments: `data.volumes[].chapters` walks every volume. */
function resolveList(value: unknown, path: string): unknown[] {
  const idx = path.indexOf('[].');
  if (idx === -1) {
    const resolved = extractPath(value, path);
    if (resolved === undefined) return [];
    return Array.isArray(resolved) ? resolved : [resolved];
  }
  const head = extractPath(value, path.slice(0, idx));
  if (!Array.isArray(head)) return [];
  const rest = path.slice(idx + 3);
  return head.flatMap((item) => resolveList(item, rest));
}

/** Light HTML→plain-text for API bodies like novel chapters: <p>/<br> → newlines, tags stripped, entities decoded. */
function htmlToText(html: string): string {
  return html
    // style/script CONTENT survives plain tag-stripping — drop the blocks first
    .replace(/<\s*(style|script)\b[\s\S]*?<\s*\/\s*\1\s*>/gi, '')
    .replace(/<\s*br\s*\/?\s*>/gi, '\n')
    .replace(/<\/\s*(p|h[1-6]|div|li|section|article)\s*>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/\u200B/g, '') // zero-width spaces common in ebook markup
    .replace(/&nbsp;/g, ' ')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/[ \t]+\n/g, '\n')
    .trim();
}

// ── default registry (module singleton used by tools.ts) ────────────────────

let defaultRegistry: PluginRegistry | null = null;

/** Wire the registry for this process (called once from chat-module). */
export function initPluginRegistry(dir: string): PluginRegistry {
  defaultRegistry = new PluginRegistry(dir);
  defaultRegistry.load();
  const loaded = defaultRegistry.listInfo();
  const broken = loaded.filter((info) => info.error);
  console.info(`[plugins] loaded ${loaded.filter((info) => !info.error).length} plugin(s) from ${dir}${broken.length > 0 ? `, ${broken.length} broken` : ''}`);
  for (const info of broken) console.error(`[plugins] ${info.name}: ${info.error}`);
  return defaultRegistry;
}

export function pluginRegistry(): PluginRegistry | null {
  return defaultRegistry;
}
