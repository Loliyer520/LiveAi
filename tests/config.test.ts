import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config/config.js';

test('loadConfig uses safe defaults', () => {
  assert.deepEqual(loadConfig({}), {
    host: '127.0.0.1',
    port: 3000,
    logLevel: 'info',
  });
});

test('loadConfig parses valid environment values', () => {
  assert.deepEqual(loadConfig({
    LIVEAI_HOST: '0.0.0.0',
    LIVEAI_PORT: '8080',
    LIVEAI_LOG_LEVEL: 'DEBUG',
  }), {
    host: '0.0.0.0',
    port: 8080,
    logLevel: 'debug',
  });
});

test('loadConfig rejects invalid ports', () => {
  assert.throws(() => loadConfig({ LIVEAI_PORT: '0' }), /LIVEAI_PORT/);
  assert.throws(() => loadConfig({ LIVEAI_PORT: 'not-a-port' }), /LIVEAI_PORT/);
});

test('loadConfig rejects invalid log levels', () => {
  assert.throws(() => loadConfig({ LIVEAI_LOG_LEVEL: 'trace' }), /LIVEAI_LOG_LEVEL/);
});
