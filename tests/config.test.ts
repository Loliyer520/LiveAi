import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config/config.js';

test('loadConfig uses safe defaults', () => {
  // Point at a nonexistent config file: the defaults test must not read the
  // live data/config.json (whose values change with the deployment).
  const config = loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json' });
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.port, 7650);
  assert.equal(config.logLevel, 'info');
  assert.equal(config.napcat.wsUrl, 'ws://127.0.0.1:7631/openclaw-bind');
  assert.equal(config.napcat.httpUrl, 'http://127.0.0.1:7632');
  assert.equal(config.napcat.selfId, 0);
  assert.equal(config.ai.masterQq, 0);
  assert.equal(config.ai.globalTriggerRate, 0.01);
  assert.equal(config.ai.staleMessageMaxAgeSeconds, 300);
  assert.equal(config.storage.stateDir.endsWith(joinPath('data', 'state')), true);
});

test('loadConfig parses valid environment values', () => {
  const config = loadConfig({
    LIVEAI_HOST: '0.0.0.0',
    LIVEAI_PORT: '8080',
    LIVEAI_LOG_LEVEL: 'DEBUG',
    LIVEAI_NAPCAT_WS_URL: 'ws://127.0.0.1:3001/ws',
    LIVEAI_MASTER_QQ: '123456',
  });
  assert.equal(config.host, '0.0.0.0');
  assert.equal(config.port, 8080);
  assert.equal(config.logLevel, 'debug');
  assert.equal(config.napcat.wsUrl, 'ws://127.0.0.1:3001/ws');
  assert.equal(config.ai.masterQq, 123456);
});

test('loadConfig rejects invalid ports', () => {
  assert.throws(() => loadConfig({ LIVEAI_PORT: '0' }), /LIVEAI_PORT/);
  assert.throws(() => loadConfig({ LIVEAI_PORT: 'not-a-port' }), /LIVEAI_PORT/);
});

test('loadConfig rejects invalid log levels', () => {
  assert.throws(() => loadConfig({ LIVEAI_LOG_LEVEL: 'trace' }), /LIVEAI_LOG_LEVEL/);
});

test('napcat.mode：显式优先，缺省按 reverse_ws_port 推导', async () => {
  // 默认（无 mode、无 reverse_ws_port）= client
  assert.equal(loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json' }).napcat.mode, 'client');
  // reverse_ws_port>0 推导 server（旧配置兼容）
  assert.equal(
    loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json', LIVEAI_NAPCAT_REVERSE_WS_PORT: '7636' }).napcat.mode,
    'server',
  );
  // 显式 mode 覆盖推导
  assert.equal(
    loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json', LIVEAI_NAPCAT_REVERSE_WS_PORT: '7636', LIVEAI_NAPCAT_MODE: 'client' }).napcat.mode,
    'client',
  );
  // 文件里写 mode 也认
  const dir = await mkdtemp(join(tmpdir(), 'liveai-config-'));
  try {
    const file = join(dir, 'config.json');
    await writeFile(file, JSON.stringify({ napcat: { mode: 'server', reverse_ws_port: 7000 } }));
    const config = loadConfig({ LIVEAI_CONFIG_PATH: file });
    assert.equal(config.napcat.mode, 'server');
    assert.equal(config.napcat.reverseWsPort, 7000);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('loadConfig is frozen deeply', () => {
  const config = loadConfig({});
  assert.equal(Object.isFrozen(config), true);
  assert.equal(Object.isFrozen(config.napcat), true);
  assert.equal(Object.isFrozen(config.ai), true);
  assert.equal(Object.isFrozen(config.storage), true);
});

function joinPath(...parts: string[]): string {
  return parts.join('/');
}

test('loadConfig defaults legacySelfIds to empty', () => {
  const config = loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json' });
  assert.deepEqual(config.napcat.legacySelfIds, []);
});

test('loadConfig parses legacy_self_ids and drops junk', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-config-'));
  try {
    const file = join(dir, 'config.json');
    await writeFile(file, JSON.stringify({ napcat: { legacy_self_ids: [987654322, 'bad', 0o0] } }));
    const config = loadConfig({ LIVEAI_CONFIG_PATH: file });
    assert.deepEqual(config.napcat.legacySelfIds, [987654322, 0]);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('loadConfig reads webui section from config.json, env wins', async () => {
  const dir = await mkdtemp(joinPath(tmpdir(), 'liveai-config-'));
  try {
    await mkdir(joinPath(dir, 'data'), { recursive: true });
    await writeFile(joinPath(dir, 'data', 'config.json'), JSON.stringify({
      webui: { host: '0.0.0.0', port: 9000, token: 'file-token' },
    }), 'utf-8');
    const configPath = joinPath(dir, 'data', 'config.json');
    const fromFile = loadConfig({ LIVEAI_CONFIG_PATH: configPath }, dir);
    assert.equal(fromFile.host, '0.0.0.0');
    assert.equal(fromFile.port, 9000);
    assert.equal(fromFile.webuiToken, 'file-token');

    const envWins = loadConfig({ LIVEAI_CONFIG_PATH: configPath, LIVEAI_WEBUI_TOKEN: 'env-token', LIVEAI_HOST: '127.0.0.1' }, dir);
    assert.equal(envWins.webuiToken, 'env-token');
    assert.equal(envWins.host, '127.0.0.1');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
