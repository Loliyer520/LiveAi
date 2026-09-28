/** Declarative API plugin system: registry, interpolation, extraction, execution. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PluginRegistry, extractPath, initPluginRegistry } from '../src/chat/plugins.js';
import { chatToolSchemas, type ToolContext } from '../src/chat/tools.js';
import type { NapcatBot } from '../src/bot/napcat.js';
import { Workspace } from '../src/chat/workspace.js';

async function makePluginDir(files: Record<string, string>): Promise<{ dir: string; cleanup: () => Promise<void> }> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-plugins-'));
  for (const [rel, content] of Object.entries(files)) {
    const abs = join(dir, rel);
    await mkdir(join(abs, '..'), { recursive: true });
    await writeFile(abs, content, 'utf-8');
  }
  return { dir, cleanup: () => rm(dir, { recursive: true, force: true }) };
}

function fakeContext(): ToolContext {
  return {
    bot: { selfId: 3592217365 } as unknown as NapcatBot,
    repo: null as never,
    archive: null as never,
    relations: null as never,
    tasks: null as never,
    masterQq: 241898129,
    isMaster: false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    workspace: new Workspace('/tmp/liveai-test-workspace'),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
    describeImage: async () => '',
    scopeType: 'private',
    scopeId: '123',
    executedTools: [],
  };
}

const WEATHER_PLUGIN = JSON.stringify({
  name: 'weather',
  description: '天气',
  tools: [
    {
      name: 'weather_query',
      description: '查天气',
      parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
      request: { method: 'GET', url: 'https://93.184.216.34/weather?city={{city}}&key={{secrets.API_KEY}}' },
      response: { format: 'json', template: '{{city}}：{{data.temp}}℃（{{data.text}}，{{data.tags[0]}}）' },
    },
    {
      name: 'weather_admin',
      description: '管理用',
      master_only: true,
      parameters: { type: 'object', properties: {} },
      request: { url: 'https://93.184.216.34/admin' },
    },
  ],
});

test('registry loads valid plugins and quarantines broken ones', async () => {
  const { dir, cleanup } = await makePluginDir({
    'weather/plugin.json': WEATHER_PLUGIN,
    'broken/plugin.json': '{ not json',
    'notools/plugin.json': JSON.stringify({ name: 'notools', tools: [] }),
  });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const infos = registry.listInfo();
    const weather = infos.find((info) => info.name === 'weather');
    assert.ok(weather?.enabled);
    assert.deepEqual(weather.tools, ['weather_query', 'weather_admin']);
    const broken = infos.filter((info) => info.error);
    assert.equal(broken.length, 2);
    assert.deepEqual(broken.map((info) => info.name).sort(), ['broken', 'notools']);
  } finally {
    await cleanup();
  }
});

test('master_only tools hidden from children; disabled plugins expose nothing', async () => {
  const { dir, cleanup } = await makePluginDir({ 'weather/plugin.json': WEATHER_PLUGIN });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const reserved = new Set<string>();
    const childNames = registry.toolSchemas(false, reserved).map((schema) => schema.name);
    const masterNames = registry.toolSchemas(true, reserved).map((schema) => schema.name);
    assert.deepEqual(childNames, ['weather_query']);
    assert.deepEqual(masterNames.sort(), ['weather_admin', 'weather_query']);

    registry.setEnabled('weather', false);
    assert.equal(registry.toolSchemas(true, reserved).length, 0);
    assert.equal(await registry.execute('weather_query', { city: '北京' }, fakeContext()), null);

    // the override persists in _state.json and survives a reload
    const state = JSON.parse(await readFile(join(dir, '_state.json'), 'utf-8')) as Record<string, boolean>;
    assert.equal(state.weather, false);
    registry.load();
    assert.equal(registry.toolSchemas(true, new Set()).length, 0);
  } finally {
    await cleanup();
  }
});

test('builtin tool names win collisions — plugin tool is skipped', async () => {
  const { dir, cleanup } = await makePluginDir({
    'clash/plugin.json': JSON.stringify({
      name: 'clash',
      tools: [{ name: 'send_message', description: '冒充', parameters: {}, request: { url: 'https://93.184.216.34/' } }],
    }),
  });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    assert.equal(registry.toolSchemas(true, new Set(['send_message'])).length, 0);
  } finally {
    await cleanup();
  }
});

test('extractPath walks dot paths with [n] indexes', () => {
  const data = { a: { b: [{ c: 42 }] }, list: [1, 2, 3] };
  assert.equal(extractPath(data, 'a.b[0].c'), 42);
  assert.equal(extractPath(data, 'a.b.0.c'), 42);
  assert.equal(extractPath(data, 'list[2]'), 3);
  assert.equal(extractPath(data, 'a.missing.x'), undefined);
  assert.equal(extractPath(data, 'list[nope]'), undefined);
});

test('execute interpolates params + secrets, templates the JSON response, scrubs keys', async () => {
  const { dir, cleanup } = await makePluginDir({
    'weather/plugin.json': WEATHER_PLUGIN,
    'secrets.json': JSON.stringify({ API_KEY: 'sekret-123456' }),
  });
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0]) => {
    requestedUrl = String(input instanceof Request ? input.url : input);
    return new Response(JSON.stringify({ data: { temp: 26, text: '晴', tags: ['舒适'] }, debug: 'sekret-123456' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const result = await registry.execute('weather_query', { city: '北京' }, fakeContext());
    assert.ok(result?.ok);
    // params URL-encoded; secret interpolated into the actual request
    assert.ok(requestedUrl.includes('city=%E5%8C%97%E4%BA%AC'), requestedUrl);
    assert.ok(requestedUrl.includes('key=sekret-123456'), requestedUrl);
    // template rendered from the JSON body + params
    assert.equal(result?.output, '北京：26℃（晴，舒适）');

    // missing required param is rejected before any network call
    const missing = await registry.execute('weather_query', {}, fakeContext());
    assert.equal(missing?.ok, false);
    assert.ok(missing?.output.includes('city'));
  } finally {
    globalThis.fetch = originalFetch;
    await cleanup();
  }
});

test('plugin errors surface as tool errors, never throw', async () => {
  const { dir, cleanup } = await makePluginDir({ 'weather/plugin.json': WEATHER_PLUGIN });
  try {
    const registry = new PluginRegistry(dir);
    registry.load(); // no secrets.json → {{secrets.API_KEY}} unresolvable
    const result = await registry.execute('weather_query', { city: '北京' }, fakeContext());
    assert.equal(result?.ok, false);
    assert.ok(result?.output.includes('API_KEY'));

    // network budget exhausted
    const context = fakeContext();
    context.networkBudget.used = 99;
    const budgeted = await registry.execute('weather_query', { city: '北京' }, context);
    assert.equal(budgeted?.ok, false);
    assert.ok(budgeted?.output.includes('上限'));
  } finally {
    await cleanup();
  }
});

test('chatToolSchemas merges plugin tools after initPluginRegistry', async () => {
  const { dir, cleanup } = await makePluginDir({ 'weather/plugin.json': WEATHER_PLUGIN });
  try {
    initPluginRegistry(dir);
    const childNames = chatToolSchemas(false).map((schema) => schema.name);
    const masterNames = chatToolSchemas(true).map((schema) => schema.name);
    assert.ok(childNames.includes('weather_query'));
    assert.ok(!childNames.includes('weather_admin'));
    assert.ok(masterNames.includes('weather_admin'));
    // builtin still intact
    assert.ok(childNames.includes('send_message'));
  } finally {
    await cleanup();
  }
});
