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

export interface LiveAiRuntimeConfig {
  readonly napcat: NapcatConfig;
  readonly ai: AiConfig;
  readonly storage: StorageConfig;
}

export interface LiveAiConfig extends LiveAiRuntimeConfig {
  readonly host: string;
  readonly port: number;
  readonly logLevel: LogLevel;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env, projectRoot: string = process.cwd()): LiveAiConfig {
  const fileConfig = readJsonConfig(resolve(env.LIVEAI_CONFIG_PATH ?? 'data/config.json'));

  const napcatFile = (fileConfig.napcat ?? {}) as Record<string, unknown>;
  const aiFile = (fileConfig.ai ?? {}) as Record<string, unknown>;
  const storageFile = (fileConfig.storage ?? {}) as Record<string, unknown>;

  const napcat: NapcatConfig = Object.freeze({
    wsUrl: env.LIVEAI_NAPCAT_WS_URL ?? str(napcatFile.ws_url) ?? 'ws://127.0.0.1:7821/openclaw-bind',
    httpUrl: env.LIVEAI_NAPCAT_HTTP_URL ?? str(napcatFile.http_url) ?? 'http://127.0.0.1:7822',
    selfId: num(env.LIVEAI_NAPCAT_SELF_ID) ?? num(napcatFile.self_id) ?? 0,
    accessToken: env.LIVEAI_NAPCAT_TOKEN ?? str(napcatFile.access_token) ?? '',
    reconnectBaseMs: num(napcatFile.reconnect_base_ms) ?? 1_000,
    reconnectMaxMs: num(napcatFile.reconnect_max_ms) ?? 30_000,
  });

  const ai: AiConfig = Object.freeze({
    masterQq: num(env.LIVEAI_MASTER_QQ) ?? num(aiFile.master_qq) ?? 241898129,
    adminQq: num(env.LIVEAI_ADMIN_QQ) ?? num(aiFile.admin_qq) ?? 241898129,
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

  const host = env.LIVEAI_HOST?.trim() || '127.0.0.1';
  const port = parsePort(env.LIVEAI_PORT);
  const logLevel = parseLogLevel(env.LIVEAI_LOG_LEVEL);

  return Object.freeze({ host, port, logLevel, napcat, ai, storage });
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
    return 3000;
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
