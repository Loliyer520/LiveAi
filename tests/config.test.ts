import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config/config.js';

test('loadConfig uses safe defaults', () => {
  // Point at a nonexistent config file: the defaults test must not read the
  // live data/config.json (whose values change with the deployment).
  const config = loadConfig({ LIVEAI_CONFIG_PATH: 'data/defaults-do-not-exist.json' });
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.port, 3000);
  assert.equal(config.logLevel, 'info');
  assert.equal(config.napcat.wsUrl, 'ws://127.0.0.1:7821/openclaw-bind');
  assert.equal(config.napcat.httpUrl, 'http://127.0.0.1:7822');
  assert.equal(config.napcat.selfId, 0);
  assert.equal(config.ai.masterQq, 241898129);
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
