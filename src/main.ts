import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { Host } from './host/host.js';
import { loadConfig } from './config/config.js';
import { HealthServer } from './api/health-server.js';
import { ChatModule } from './app/chat-module.js';
import { NapcatRuntime } from './napcat/runtime.js';
import { usageStats } from './models/usage.js';
import { createLogger, errorContext } from './observability/logger.js';
import { pluginRegistry } from './chat/plugins.js';
import { MASTER_SCOPE_ID, MASTER_SCOPE_KEY, MASTER_SCOPE_TYPE, isMasterScope } from './chat/types.js';

/**
 * 以本文件位置推导项目根：从别的 cwd 起服务时 data/config.json 不能解析丢。
 * tsx 下本文件在 src/，构建后在 dist/src/——层数不同，向上找 package.json 才稳。
 */
function findProjectRoot(start: string): string {
  let dir = start;
  for (let i = 0; i < 5; i += 1) {
    if (existsSync(join(dir, 'package.json'))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return start;
}
const PROJECT_ROOT = findProjectRoot(dirname(fileURLToPath(import.meta.url)));

export async function createApplication(env: NodeJS.ProcessEnv = process.env): Promise<Host> {
  const config = loadConfig(env, PROJECT_ROOT);
  const logger = createLogger(config.logLevel);
  let host: Host;

  const chat = new ChatModule(config);
  const napcatRuntime = new NapcatRuntime({
    home: '/my/run/napcat',
    serviceName: 'deeplive-napcat',
    projectRoot: PROJECT_ROOT,
    botPort: config.napcat.reverseWsPort,
    mode: config.napcat.mode,
    accessToken: config.napcat.accessToken,
    isConnected: () => chat.bot.connected,
    selfId: () => chat.bot.selfId,
  });
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
      masterHistory: () => chat.repo.getMessages(MASTER_SCOPE_TYPE, MASTER_SCOPE_ID),
      modelsConfigPath: config.storage.modelsConfigPath,
      reloadModels: () => chat.models.load(),
      // 知识库：面板以中枢身份复核待审
      kb: chat.kb,
      kbMasterScope: MASTER_SCOPE_KEY,
      plugins: pluginRegistry(),
      napcatRuntime,
      napcat: chat.bot,
      relations: chat.relations,
      // 设置页总开关：主AI会话（master:core，上级 agent）恒常保留，其余批量切
      setAllScopesEnabled: (enabled) => {
        let changed = 0;
        for (const agent of chat.repo.listAgents()) {
          if (isMasterScope(agent.scopeType, agent.scopeId)) continue;
          chat.repo.updateAgent(agent.scopeType, agent.scopeId, { enabled });
          changed += 1;
        }
        return changed;
      },
      setSummaryEnabled: (enabled) => chat.summarizer.setEnabled(enabled),
      setIntelEnabled: (enabled) => chat.intel.setEnabled(enabled),
      summaryEnabled: () => chat.summarizer.isEnabled(),
      intelEnabled: () => chat.intel.isEnabled(),
      configPath: resolve(PROJECT_ROOT, env.LIVEAI_CONFIG_PATH ?? 'data/config.json'),
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
      repoDir: PROJECT_ROOT,
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
