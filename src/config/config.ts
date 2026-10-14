/**
 * Configuration — env-first, with data/config.json as the persistent layer
 * (napcat endpoints, master/admin identity, trigger defaults, storage paths).
 * Ported from legacy config.yaml.example + config loader.
 */

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

export const LOG_LEVELS = ['debug', 'info', 'warn', 'error'] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export interface NapcatConfig {
  readonly wsUrl: string;
  readonly httpUrl: string;
  readonly selfId: number;
  /** QQ accounts this bot previously ran on; their old messages are still "us". */
  readonly legacySelfIds: readonly number[];
  readonly accessToken: string;
  readonly reconnectBaseMs: number;
  readonly reconnectMaxMs: number;
}

export interface AiConfig {
  readonly masterQq: number;
  readonly adminQq: number;
  readonly globalTriggerRate: number;
  readonly staleMessageMaxAgeSeconds: number;
  readonly historyLimit: number;
  readonly requestTimeoutMs: number;
  readonly summaryEnabled: boolean;
  readonly intelEnabled: boolean;
}

export interface StorageConfig {
  readonly stateDir: string;
  readonly modelsConfigPath: string;
  readonly promptDir: string;
}

export interface TtsConfig {
  /** 满穗 TTS（GPT-SoVITS，经 frps 转发）基址；send_tts 工具用它合成语音。 */
  readonly baseUrl: string;
  /** 合成超时（长文本要跑十几秒）。 */
  readonly timeoutMs: number;
}

export interface NovelCmdConfig {
  /**
   * 启用「小说阅读指令机」的 scope 键列表（"private:123456" 形态）。
   * 这些会话里搜书/选/目录/看/下一章 等整句指令直接由指令机应答，不走模型。
   */
  readonly scopes: readonly string[];
  /** fq-novel 服务基址。 */
  readonly baseUrl: string;
  /** 互备基址链（主线路挂了依次改走，默认=经跳板隧道到 183 的 /fq 代理）。 */
  readonly fallbackBaseUrls: readonly string[];
}

export interface LiveAiRuntimeConfig {
  readonly napcat: NapcatConfig;
  readonly ai: AiConfig;
  readonly storage: StorageConfig;
  readonly tts: TtsConfig;
  readonly novelCmd: NovelCmdConfig;
}

export interface LiveAiConfig extends LiveAiRuntimeConfig {
  readonly host: string;
  readonly port: number;
  readonly logLevel: LogLevel;
  /** 管理面板鉴权 token，空串 = 不鉴权（仅适合绑 127.0.0.1）。 */
  readonly webuiToken: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env, projectRoot: string = process.cwd()): LiveAiConfig {
  const fileConfig = readJsonConfig(resolve(env.LIVEAI_CONFIG_PATH ?? 'data/config.json'));

  const napcatFile = (fileConfig.napcat ?? {}) as Record<string, unknown>;
  const aiFile = (fileConfig.ai ?? {}) as Record<string, unknown>;
  const storageFile = (fileConfig.storage ?? {}) as Record<string, unknown>;

  const napcat: NapcatConfig = Object.freeze({
    wsUrl: env.LIVEAI_NAPCAT_WS_URL ?? str(napcatFile.ws_url) ?? 'ws://127.0.0.1:7631/openclaw-bind',
    httpUrl: env.LIVEAI_NAPCAT_HTTP_URL ?? str(napcatFile.http_url) ?? 'http://127.0.0.1:7632',
    selfId: num(env.LIVEAI_NAPCAT_SELF_ID) ?? num(napcatFile.self_id) ?? 0,
    legacySelfIds: Object.freeze(
      (Array.isArray(napcatFile.legacy_self_ids) ? napcatFile.legacy_self_ids : [])
        .map((value) => num(value))
        .filter((value): value is number => value !== null),
    ),
    accessToken: env.LIVEAI_NAPCAT_TOKEN ?? str(napcatFile.access_token) ?? '',
    reconnectBaseMs: num(napcatFile.reconnect_base_ms) ?? 1_000,
    reconnectMaxMs: num(napcatFile.reconnect_max_ms) ?? 30_000,
  });

  const ai: AiConfig = Object.freeze({
    masterQq: num(env.LIVEAI_MASTER_QQ) ?? num(aiFile.master_qq) ?? 0,
    adminQq: num(env.LIVEAI_ADMIN_QQ) ?? num(aiFile.admin_qq) ?? 0,
    globalTriggerRate: num(aiFile.global_trigger_rate) ?? 0.01,
    staleMessageMaxAgeSeconds: num(aiFile.stale_message_max_age_seconds) ?? 300,
    historyLimit: num(aiFile.history_limit) ?? 500,
    requestTimeoutMs: num(aiFile.request_timeout_ms) ?? 180_000,
    summaryEnabled: (aiFile.summary_enabled as boolean | undefined) ?? true,
    intelEnabled: (aiFile.intel_enabled as boolean | undefined) ?? true,
  });

  const storage: StorageConfig = Object.freeze({
    stateDir: resolve(projectRoot, env.LIVEAI_STATE_DIR ?? str(storageFile.state_dir) ?? 'data/state'),
    modelsConfigPath: resolve(projectRoot, env.LIVEAI_MODELS_CONFIG ?? str(storageFile.models_config_path) ?? 'data/models_config.json'),
    promptDir: resolve(projectRoot, env.LIVEAI_PROMPT_DIR ?? str(storageFile.prompt_dir) ?? 'data/prompt'),
  });

  // tts 段：满穗 TTS 服务（GPT-SoVITS）。默认本机 frps 转发的 8020。
  const ttsFile = (fileConfig.tts ?? {}) as Record<string, unknown>;
  const tts: TtsConfig = Object.freeze({
    baseUrl: (env.LIVEAI_TTS_URL ?? str(ttsFile.base_url) ?? 'http://127.0.0.1:8020').replace(/\/+$/, ''),
    timeoutMs: num(env.LIVEAI_TTS_TIMEOUT_MS) ?? num(ttsFile.timeout_ms) ?? 90_000,
  });

  // novel_command 段：小说阅读指令机（指定会话不走模型直接应答搜书/看章）。
  // scopes 空 = 不启用。env 用逗号分隔多个 scope。
  const novelCmdFile = (fileConfig.novel_command ?? {}) as Record<string, unknown>;
  const novelCmdScopesRaw = env.LIVEAI_NOVEL_CMD_SCOPES ?? (
    Array.isArray(novelCmdFile.scopes) ? novelCmdFile.scopes.map((value) => String(value)).join(',') : ''
  );
  const novelCmd: NovelCmdConfig = Object.freeze({
    scopes: Object.freeze(
      novelCmdScopesRaw.split(',').map((value) => value.trim()).filter((value) => /^(private|group):\d+$/.test(value)),
    ),
    baseUrl: (env.LIVEAI_NOVEL_CMD_URL ?? str(novelCmdFile.base_url) ?? 'http://127.0.0.1:7640').replace(/\/+$/, ''),
    fallbackBaseUrls: (env.LIVEAI_NOVEL_CMD_FALLBACK_URLS ?? str(novelCmdFile.fallback_base_urls) ?? 'http://127.0.0.1:7641/fq')
      .split(',').map((value) => value.trim().replace(/\/+$/, '')).filter(Boolean),
  });

  // webui 段：管理面板监听与鉴权（env 优先，其次 config.json，最后默认）。
  const webuiFile = (fileConfig.webui ?? {}) as Record<string, unknown>;
  const host = env.LIVEAI_HOST?.trim() || str(webuiFile.host) || '127.0.0.1';
  const port = parsePort(env.LIVEAI_PORT ?? str(webuiFile.port) ?? undefined);
  const webuiToken = env.LIVEAI_WEBUI_TOKEN ?? str(webuiFile.token) ?? '';
  const logLevel = parseLogLevel(env.LIVEAI_LOG_LEVEL);

  return Object.freeze({ host, port, logLevel, webuiToken, napcat, ai, storage, tts, novelCmd });
}

function readJsonConfig(path: string): Record<string, unknown> {
  try {
    if (!existsSync(path)) return {};
    return JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>;
  } catch (error) {
    throw new Error(`配置文件解析失败 ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function str(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  return text === '' ? null : text;
}

function num(value: unknown): number | null {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parsePort(value: string | undefined): number {
  if (value === undefined || value.trim() === '') {
    return 7650;  // 端口编排：自用带
  }

  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('LIVEAI_PORT must be an integer between 1 and 65535');
  }
  return port;
}

function parseLogLevel(value: string | undefined): LogLevel {
  if (value === undefined || value.trim() === '') {
    return 'info';
  }

  const logLevel = value.trim().toLowerCase();
  if (!LOG_LEVELS.includes(logLevel as LogLevel)) {
    throw new Error(`LIVEAI_LOG_LEVEL must be one of: ${LOG_LEVELS.join(', ')}`);
  }
  return logLevel as LogLevel;
}
