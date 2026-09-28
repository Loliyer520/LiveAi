import { fileURLToPath } from 'node:url';
import { Host } from './host/host.js';
import { loadConfig } from './config/config.js';
import { HealthServer } from './api/health-server.js';
import { ChatModule } from './app/chat-module.js';
import { createLogger, errorContext } from './observability/logger.js';

export async function createApplication(env: NodeJS.ProcessEnv = process.env): Promise<Host> {
  const config = loadConfig(env);
  const logger = createLogger(config.logLevel);
  let host: Host;

  const chat = new ChatModule(config);
  const healthServer = new HealthServer(config, {
    isReady: () => host?.snapshot().status === 'ready',
  });
  host = new Host([chat, healthServer]);
  logger.debug('Application created');

  return host;
}

async function main(): Promise<void> {
  const host = await createApplication();
  let stopping: Promise<void> | undefined;

  const stop = (): Promise<void> => {
    stopping ??= host.stop();
    return stopping;
  };

  process.once('SIGINT', () => {
    void stop().then(() => process.exit(0));
  });
  process.once('SIGTERM', () => {
    void stop().then(() => process.exit(0));
  });

  await host.start();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error: unknown) => {
    const logger = createLogger('error');
    logger.error('Failed to start LiveAI', errorContext(error));
    process.exitCode = 1;
  });
}
