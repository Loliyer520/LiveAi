import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import { readFile } from 'node:fs/promises';
import { Host } from './host/host.js';
import { loadConfig } from './config/config.js';
import { HealthServer } from './api/health-server.js';
import { ChatModule } from './app/chat-module.js';
import { usageStats } from './models/usage.js';
import { createLogger, errorContext } from './observability/logger.js';
import { pluginRegistry } from './chat/plugins.js';

export async function createApplication(env: NodeJS.ProcessEnv = process.env): Promise<Host> {
  const config = loadConfig(env);
  const logger = createLogger(config.logLevel);
  let host: Host;

  const chat = new ChatModule(config);
  const packageVersion = await readPackageVersion();
  const healthServer = new HealthServer(
    config,
    { isReady: () => host?.snapshot().status === 'ready' },
    {
      masterQq: config.ai.masterQq,
      promptDir: config.storage.promptDir,
      token: config.webuiToken,
      bot: chat.bot,
      repo: chat.repo,
      models: chat.models,
      invalidatePrompts: () => chat.prompts.invalidate(),
      usage: usageStats,
      injectAdmin: (text) => chat.orchestrator.handleAdminWebuiMessage(text),
      masterHistory: () => chat.repo.getMessages('private', String(config.ai.masterQq)),
      modelsConfigPath: config.storage.modelsConfigPath,
      reloadModels: () => chat.models.load(),
      // 知识库：面板以中枢身份复核待审
      kb: chat.kb,
      kbMasterScope: `private:${config.ai.masterQq}`,
      plugins: pluginRegistry(),
      relations: chat.relations,
      // 设置页总开关：主AI会话私聊窗恒常保留（防失联），其余批量切
      setAllScopesEnabled: (enabled) => {
        const masterId = String(config.ai.masterQq);
        let changed = 0;
        for (const agent of chat.repo.listAgents()) {
          if (agent.scopeType === 'private' && agent.scopeId === masterId) continue;
          chat.repo.updateAgent(agent.scopeType, agent.scopeId, { enabled });
          changed += 1;
        }
        return changed;
      },
      setSummaryEnabled: (enabled) => chat.summarizer.setEnabled(enabled),
      setIntelEnabled: (enabled) => chat.intel.setEnabled(enabled),
      summaryEnabled: () => chat.summarizer.isEnabled(),
      intelEnabled: () => chat.intel.isEnabled(),
      configPath: resolve(env.LIVEAI_CONFIG_PATH ?? 'data/config.json'),
      stateDir: config.storage.stateDir,
      workspaceDir: join(config.storage.stateDir, 'workspace'),
      dataDir: join(config.storage.stateDir, '..'),
      mcFetch: async (path) => {
        try {
          const response = await fetch(`${chat.mcBotUrl}${path}`, {
            headers: chat.mcBotToken !== '' ? { authorization: `Bearer ${chat.mcBotToken}` } : {},
            signal: AbortSignal.timeout(5000),
          });
          const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
          return { ok: response.ok, status: response.status, ...body };
        } catch (error) {
          return { ok: false, error: error instanceof Error ? error.message : String(error) };
        }
      },
      mcbotDir: '/my/run/mcbot',
      repoDir: '/my/run/bot',
      packageVersion,
    },
  );
  host = new Host([chat, healthServer]);
  logger.debug('Application created');

  return host;
}

async function readPackageVersion(): Promise<string> {
  try {
    const raw = await readFile('/my/run/bot/package.json', 'utf-8');
    return String(JSON.parse(raw).version ?? '0.0.0');
  } catch {
    return '0.0.0';
  }
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
    logger.error('Failed to start DeepLive', errorContext(error));
    process.exitCode = 1;
  });
}
