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
 * per-turn network budget; secret values are scrubbed from tool output; a
 * broken manifest is quarantined (recorded, skipped) instead of crashing
 * anything.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { ToolSchema } from '../models/types.js';
import type { ToolContext, ToolResult } from './tools.js';
import { webFetch } from './net.js';
import { spendNetworkBudget } from './actions.js';

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
}

export interface PluginToolDef {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  /** true = only the master scope sees this tool. */
  master_only?: boolean;
  request: PluginToolRequest;
  response?: PluginToolResponse;
}

export interface PluginManifest {
  name: string;
  description?: string;
  enabled?: boolean;
  tools: PluginToolDef[];
}

export interface PluginInfo {
  name: string;
  description: string;
  enabled: boolean;
  tools: string[];
  error?: string;
}

const NAME_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
const PLACEHOLDER = /\{\{\s*([\w.[\]]+)\s*\}\}/g;
const MAX_OUTPUT_CHARS = 4000;

export class PluginRegistry {
  private manifests = new Map<string, PluginManifest>();
  private errors = new Map<string, string>();
  private enabledOverrides: Record<string, boolean> = {};
  private secrets: Record<string, string> = {};

  constructor(readonly dir: string) {}

  /** Synchronous (re)scan; called at startup and by plugin_manage reload. */
  load(): void {
    this.manifests.clear();
    this.errors.clear();
    this.enabledOverrides = this.readJsonFile<Record<string, boolean>>(join(this.dir, '_state.json')) ?? {};
    this.secrets = this.readJsonFile<Record<string, string>>(join(this.dir, 'secrets.json')) ?? {};
    if (!existsSync(this.dir)) return;
    for (const entry of readdirSync(this.dir, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith('_') || entry.name.startsWith('.')) continue;
      this.loadOne(entry.name);
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

  /** Tool schemas exposed to the model; reservedNames (builtin) win collisions. */
  toolSchemas(includeMaster: boolean, reservedNames: Set<string>): ToolSchema[] {
    const schemas: ToolSchema[] = [];
    for (const manifest of this.manifests.values()) {
      if (!this.isEnabled(manifest)) continue;
      for (const tool of manifest.tools) {
        if (tool.master_only === true && !includeMaster) continue;
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
  async execute(toolName: string, args: Record<string, unknown>, context: ToolContext): Promise<ToolResult | null> {
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

    try {
      spendNetworkBudget(context);

      // Required-parameter check (models occasionally drop them).
      const required = (def.parameters as { required?: string[] }).required ?? [];
      const missing = required.filter((key) => args[key] === undefined || args[key] === null || String(args[key]).trim() === '');
      if (missing.length > 0) return { ok: false, output: `${toolName}: 缺少必填参数 ${missing.join('、')}。` };

      const secretValues = Object.values(this.secrets).filter((value) => value.length >= 6);
      const url = interpolate(def.request.url, (key) => this.resolveValue(key, args, context, true));
      const headers: Record<string, string> = {};
      for (const [key, template] of Object.entries(def.request.headers ?? {})) {
        headers[key] = interpolate(template, (key2) => this.resolveValue(key2, args, context, false));
      }
      const body = def.request.body === undefined
        ? undefined
        : interpolate(def.request.body, (key) => this.resolveValue(key, args, context, false));

      // displayLimit ≥ maxBytes: plugins must parse the FULL JSON body;
      // the model-facing truncation happens after extract/template below.
      const result = await webFetch(url, {
        method: def.request.method ?? 'GET',
        headers,
        body,
        displayLimit: 512 * 1024,
      });
      let output: string;
      const response = def.response ?? {};
      const format = response.format ?? 'json';
      let parsed: unknown = null;
      if (format === 'json') {
        try {
          parsed = JSON.parse(result.body);
        } catch {
          output = `接口返回的不是 JSON（HTTP ${result.status}）：\n${result.body.slice(0, 1000)}`;
          return { ok: result.status < 400, output: this.scrub(output, secretValues) };
        }
        if (response.template) {
          output = interpolate(response.template, (key) => {
            const fromBody = extractPath(parsed, key);
            if (fromBody !== undefined) return typeof fromBody === 'object' ? JSON.stringify(fromBody, null, 0) : String(fromBody);
            return this.resolveValue(key, args, context, false);
          });
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
      return { ok: result.status < 400, output: this.scrub(`${output}${statusNote}`, secretValues) };
    } catch (error) {
      return { ok: false, output: `${toolName} 调用失败: ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  /** Resolve a {{placeholder}}: secrets.*, scope.*, then tool params. */
  private resolveValue(key: string, args: Record<string, unknown>, context: ToolContext, encode: boolean): string {
    let raw: string | undefined;
    if (key.startsWith('secrets.')) {
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
