import type { LogLevel } from '../config/config.js';

const LOG_LEVEL_WEIGHT: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
};

export interface Logger {
  debug(message: string, context?: Record<string, unknown>): void;
  info(message: string, context?: Record<string, unknown>): void;
  warn(message: string, context?: Record<string, unknown>): void;
  error(message: string, context?: Record<string, unknown>): void;
}

export function createLogger(level: LogLevel, output: NodeJS.WriteStream = process.stdout): Logger {
  const minimumWeight = LOG_LEVEL_WEIGHT[level];

  return {
    debug: (message, context) => write('debug', message, context),
    info: (message, context) => write('info', message, context),
    warn: (message, context) => write('warn', message, context),
    error: (message, context) => write('error', message, context),
  };

  function write(entryLevel: LogLevel, message: string, context?: Record<string, unknown>): void {
    if (LOG_LEVEL_WEIGHT[entryLevel] < minimumWeight) {
      return;
    }

    const entry = {
      timestamp: new Date().toISOString(),
      level: entryLevel,
      message,
      ...context,
    };
    output.write(`${JSON.stringify(entry)}\n`);
  }
}

export function errorContext(error: unknown): Record<string, unknown> {
  if (error instanceof Error) {
    return { error: { name: error.name, message: error.message, stack: error.stack } };
  }
  return { error: { message: String(error) } };
}
