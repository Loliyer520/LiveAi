export const LOG_LEVELS = ['debug', 'info', 'warn', 'error'] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export interface LiveAiConfig {
  readonly host: string;
  readonly port: number;
  readonly logLevel: LogLevel;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): LiveAiConfig {
  const host = env.LIVEAI_HOST?.trim() || '127.0.0.1';
  const port = parsePort(env.LIVEAI_PORT);
  const logLevel = parseLogLevel(env.LIVEAI_LOG_LEVEL);

  return Object.freeze({ host, port, logLevel });
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
