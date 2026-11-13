/** Declarative API plugin system: registry, interpolation, extraction, execution. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PluginRegistry, extractPath, initPluginRegistry, interpolateJsonBody } from '../src/chat/plugins.js';
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
    bot: { selfId: 987654322 } as unknown as NapcatBot,
    repo: null as never,
    archive: null as never,
    relations: null as never,
    tasks: null as never,
    masterQq: 123456789,
    isMaster: false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    peerInject: () => undefined,
    workspace: new Workspace('/tmp/liveai-test-workspace'),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
          triggerVideoRefs: [],
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
    const childNames = registry.toolSchemas('group:1', false, reserved).map((schema) => schema.name);
    const masterNames = registry.toolSchemas('private:241898129', true, reserved).map((schema) => schema.name);
    assert.deepEqual(childNames, ['weather_query']);
    assert.deepEqual(masterNames.sort(), ['weather_admin', 'weather_query']);

    registry.setEnabled('weather', false);
    assert.equal(registry.toolSchemas('private:241898129', true, reserved).length, 0);
    assert.equal(await registry.execute('weather_query', { city: '北京' }, fakeContext()), null);

    // the override persists in _state.json and survives a reload
    const state = JSON.parse(await readFile(join(dir, '_state.json'), 'utf-8')) as Record<string, boolean>;
    assert.equal(state.weather, false);
    registry.load();
    assert.equal(registry.toolSchemas('private:241898129', true, new Set()).length, 0);
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
    assert.equal(registry.toolSchemas('private:241898129', true, new Set(['send_message'])).length, 0);
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

test('allow_internal manifest flag permits loopback URLs; default still refused', async () => {
  const localPlugin = (allowInternal: boolean) => JSON.stringify({
    name: 'local',
    tools: [{
      name: 'local_query',
      description: 'local',
      allow_internal: allowInternal,
      parameters: { type: 'object', properties: {} },
      request: { url: 'http://127.0.0.1:7640/health' },
      response: { format: 'json', template: 'ok={{data.device.device_id}}' },
    }],
  });
  const restore = stubFetch({ code: 0, data: { device: { device_id: 'dev-1' } } });
  try {
    // without the flag the SSRF guard refuses before any fetch happens
    const denied = await makePluginDir({ 'local/plugin.json': localPlugin(false) });
    try {
      const registry = new PluginRegistry(denied.dir);
      registry.load();
      const result = await registry.execute('local_query', {}, fakeContext());
      assert.equal(result?.ok, false);
      assert.ok(result?.output.includes('内网'), result?.output);
    } finally {
      await denied.cleanup();
    }

    const allowed = await makePluginDir({ 'local/plugin.json': localPlugin(true) });
    try {
      const registry = new PluginRegistry(allowed.dir);
      registry.load();
      const result = await registry.execute('local_query', {}, fakeContext());
      assert.ok(result?.ok, result?.output);
      assert.equal(result?.output, 'ok=dev-1');
    } finally {
      await allowed.cleanup();
    }
  } finally {
    restore();
  }
});

test('chatToolSchemas merges plugin tools after initPluginRegistry', async () => {
  const { dir, cleanup } = await makePluginDir({ 'weather/plugin.json': WEATHER_PLUGIN });
  try {
    initPluginRegistry(dir);
    const childNames = chatToolSchemas('group:1', false).map((schema) => schema.name);
    const masterNames = chatToolSchemas('private:241898129', true).map((schema) => schema.name);
    assert.ok(childNames.includes('weather_query'));
    assert.ok(!childNames.includes('weather_admin'));
    assert.ok(masterNames.includes('weather_admin'));
    // builtin still intact
    assert.ok(childNames.includes('send_message'));
  } finally {
    await cleanup();
  }
});

const DOCUMENT_PLUGIN = JSON.stringify({
  name: 'document',
  description: '文档',
  tools: [
    {
      name: 'document_directory',
      description: '目录',
      parameters: {
        type: 'object',
        properties: { book_id: { type: 'string' }, count: { type: 'number', default: 2 } },
        required: ['book_id'],
      },
      request: { method: 'GET', url: 'https://93.184.216.34/document/directory/{{book_id}}' },
      response: {
        format: 'json',
        code_path: 'code',
        ok_value: 1,
        msg_path: 'error',
        list: {
          from: 'data.volumes[].chapters',
          max: '{{count}}',
          separator: '\n',
          item: '{{title}}｜{{item_id}}',
        },
        save: { from: 'data', path: 'documents/{{book_id}}_directory.json' },
      },
    },
    {
      name: 'document_content',
      description: '正文',
      parameters: {
        type: 'object',
        properties: {
          book_id: { type: 'string' },
          item_id: { type: 'string' },
          forward: { type: 'boolean', default: false },
        },
        required: ['book_id', 'item_id'],
      },
      request: { method: 'GET', url: 'https://93.184.216.34/document/content?tab={{book_id}}&item_id={{item_id}}' },
      response: {
        format: 'json',
        code_path: 'code',
        ok_value: 1,
        msg_path: 'error',
        template: '已取到（{{data.item_id}}）。',
        save: { from: 'data.content', path: 'documents/{{book_id}}/{{item_id}}.txt', html_to_text: true, send: '{{forward}}' },
      },
    },
    {
      name: 'document_chapters',
      description: '批量正文',
      parameters: {
        type: 'object',
        properties: {
          book_id: { type: 'string' },
          item_ids: { type: 'string' },
          forward: { type: 'boolean', default: true },
        },
        required: ['book_id', 'item_ids'],
      },
      request: { method: 'GET', url: 'https://93.184.216.34/document/chapters?ids={{item_ids}}&book_id={{book_id}}' },
      response: {
        format: 'json',
        code_path: 'code',
        ok_value: 1,
        msg_path: 'error',
        template: '批量取章完成，逐章结果：',
        save_each: { from: 'data', path: 'documents/{{book_id}}/{{__key}}.txt', html_to_text: true, send: '{{forward}}' },
      },
    },
  ],
});

const DIRECTORY_BODY = {
  code: 1,
  data: {
    volumes: [
      { chapters: [
        { item_id: 'c1', title: '第一章 雨' },
        { item_id: 'c2', title: '第二章 灯' },
      ] },
      { chapters: [
        { item_id: 'c3', title: '第三章 桥' },
      ] },
    ],
  },
};

function stubFetch(body: unknown): () => void {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })) as typeof fetch;
  return () => {
    globalThis.fetch = originalFetch;
  };
}

test('list flattens volumes[].chapters, applies schema default max, saves full JSON', async () => {
  const { dir, cleanup } = await makePluginDir({ 'document/plugin.json': DOCUMENT_PLUGIN });
  const restore = stubFetch(DIRECTORY_BODY);
  const workspaceDir = await mkdtemp(join(tmpdir(), 'liveai-document-ws-'));
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const context = { ...fakeContext(), workspace: new Workspace(workspaceDir) };
    const result = await registry.execute('document_directory', { book_id: 'b1' }, context);
    assert.ok(result?.ok);
    // flattened across both volumes, schema default count=2 applied, truncation noted
    assert.equal(
      result?.output.split('\n（已存到')[0],
      '第一章 雨｜c1\n第二章 灯｜c2\n…（共 3 条，只列了前 2 条）',
    );
    // full directory JSON saved into the scope workspace
    const saved = JSON.parse(await readFile(join(workspaceDir, 'documents', 'b1_directory.json'), 'utf-8')) as typeof DIRECTORY_BODY.data;
    assert.equal(saved.volumes[1].chapters[0].item_id, 'c3');
    assert.ok(result?.output.includes('documents/b1_directory.json'));

    // explicit count overrides the default
    const full = await registry.execute('document_directory', { book_id: 'b1', count: 10 }, context);
    assert.ok(full?.output.includes('第三章 桥｜c3'));
    assert.ok(!full?.output.includes('只列了前'));
  } finally {
    restore();
    await rm(workspaceDir, { recursive: true, force: true });
    await cleanup();
  }
});

test('in-band error code surfaces msg_path; content saved as de-tagged txt', async () => {
  const { dir, cleanup } = await makePluginDir({ 'document/plugin.json': DOCUMENT_PLUGIN });
  const workspaceDir = await mkdtemp(join(tmpdir(), 'liveai-document-ws-'));
  let restore = stubFetch({ code: 0, error: 'tab 不能为空' });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const context = { ...fakeContext(), workspace: new Workspace(workspaceDir) };
    const failure = await registry.execute('document_content', { book_id: 'b1', item_id: 'c1' }, context);
    assert.equal(failure?.ok, false);
    assert.ok(failure?.output.includes('tab 不能为空'));

    restore();
    restore = stubFetch({ code: 1, data: { item_id: 'c1', content: '<style>.volumePicture img { bd-object-fit: screen; }</style><p>第一行。</p><p>第二行&amp;第三行。</p>' } });
    const ok = await registry.execute('document_content', { book_id: 'b1', item_id: 'c1' }, context);
    assert.ok(ok?.ok);
    assert.ok(ok?.output.includes('已取到（c1）。'));
    assert.ok(ok?.output.includes('documents/b1/c1.txt'));
    const txt = await readFile(join(workspaceDir, 'documents', 'b1', 'c1.txt'), 'utf-8');
    // style block content must not leak into the plain text
    assert.equal(txt, '第一行。\n第二行&第三行。');
  } finally {
    restore();
    await rm(workspaceDir, { recursive: true, force: true });
    await cleanup();
  }
});

test('save.send forwards the saved text verbatim when the flag is truthy', async () => {
  const { dir, cleanup } = await makePluginDir({ 'document/plugin.json': DOCUMENT_PLUGIN });
  const workspaceDir = await mkdtemp(join(tmpdir(), 'liveai-document-ws-'));
  const restore = stubFetch({ code: 1, data: { item_id: 'c1', content: '<p>第一行。</p>\n<p>第二行。</p>' } });
  const sent: string[] = [];
  const context = {
    ...fakeContext(),
    workspace: new Workspace(workspaceDir),
    bot: {
      selfId: 987654322,
      async sendText(_t: string, _id: number, text: string) {
        sent.push(text);
        return { message_id: 9000 + sent.length };
      },
    } as never,
    repo: {
      registerMessageRef: () => 'RF',
      appendMessage: () => false,
    } as never,
  };
  try {
    const registry = new PluginRegistry(dir);
    registry.load();

    // forward=true → one call saves AND delivers verbatim (packed into one message)
    const forwarded = await registry.execute('document_content', { book_id: 'b1', item_id: 'c1', forward: true }, context);
    assert.ok(forwarded?.ok);
    assert.equal(forwarded?.sentToUser, true);
    assert.deepEqual(sent, ['第一行。\n第二行。']);
    assert.ok(forwarded?.output.includes('已把原文发给用户'));

    // default (forward=false) → saves only, sends nothing
    const savedOnly = await registry.execute('document_content', { book_id: 'b1', item_id: 'c1' }, context);
    assert.ok(savedOnly?.ok);
    assert.equal(savedOnly?.sentToUser, undefined);
    assert.equal(sent.length, 1);
  } finally {
    restore();
    await rm(workspaceDir, { recursive: true, force: true });
    await cleanup();
  }
});

test('save_each batch: per-chapter save + verbatim send; one bad chapter degrades to a note line', async () => {
  // 「再取五章」型请求：一次调用批量取发，替代循环 document_content——
  // agent 循环每调一次工具都全量重发 prompt，五连发就是五份全价上下文。
  const { dir, cleanup } = await makePluginDir({ 'document/plugin.json': DOCUMENT_PLUGIN });
  const workspaceDir = await mkdtemp(join(tmpdir(), 'liveai-document-ws-'));
  const restore = stubFetch({
    code: 1,
    data: {
      c1: { item_id: 'c1', title: '第一章 雨', content: '<p>一。</p>' },
      c2: { item_id: 'c2', title: '第二章 灯', content: '<p>二。</p>' },
      c3: { error: '上游暂时拉不到章节内容' },
    },
  });
  const sent: string[] = [];
  const context = {
    ...fakeContext(),
    workspace: new Workspace(workspaceDir),
    bot: {
      selfId: 987654322,
      async sendText(_t: string, _id: number, text: string) {
        sent.push(text);
        return { message_id: 9000 + sent.length };
      },
    } as never,
    repo: {
      registerMessageRef: () => 'RF',
      appendMessage: () => false,
    } as never,
  };
  try {
    const registry = new PluginRegistry(dir);
    registry.load();

    // forward 默认 true → 逐章存盘 + 逐章原文直发
    const batched = await registry.execute('document_chapters', { book_id: 'b1', item_ids: 'c1,c2,c3' }, context);
    assert.ok(batched?.ok, batched?.output);
    assert.equal(batched?.sentToUser, true);
    assert.deepEqual(sent, ['一。', '二。'], 'two good chapters sent verbatim, bad chapter not sent');
    assert.equal(await readFile(join(workspaceDir, 'documents', 'b1', 'c1.txt'), 'utf-8'), '一。');
    assert.equal(await readFile(join(workspaceDir, 'documents', 'b1', 'c2.txt'), 'utf-8'), '二。');
    assert.ok(batched?.output.includes('《第一章 雨》已存并发给用户'));
    assert.ok(batched?.output.includes('《第二章 灯》已存并发给用户'));
    // 坏章标题回退到 key，逐章报失败不影响整体结果
    assert.ok(batched?.output.includes('《c3》失败：上游暂时拉不到章节内容'));
    assert.ok(batched?.output.includes('2/3'));

    // forward=false → 只存不发
    restore();
    stubFetch({
      code: 1,
      data: { c1: { item_id: 'c1', title: '第一章 雨', content: '<p>一。</p>' } },
    });
    const savedOnly = await registry.execute('document_chapters', { book_id: 'b1', item_ids: 'c1', forward: false }, context);
    assert.ok(savedOnly?.ok);
    assert.equal(savedOnly?.sentToUser, undefined);
    assert.equal(sent.length, 2);
    assert.ok(savedOnly?.output.includes('documents/b1/c1.txt'));
  } finally {
    restore();
    await rm(workspaceDir, { recursive: true, force: true });
    await cleanup();
  }
});

test('crippled success body (code ok but missing template field) surfaces real upstream error, not 参数缺失 jargon', async () => {
  // 2026-10-05 雪球连环「data.title缺失」事故：fq 上游抖动时回 200+code 成功
  // 但 data 是 {error:...}——模板取 data.title 扑空，模型原样转述黑话给用户。
  const { dir, cleanup } = await makePluginDir({ 'document/plugin.json': DOCUMENT_PLUGIN });
  const workspaceDir = await mkdtemp(join(tmpdir(), 'liveai-document-ws-'));
  const restore = stubFetch({ code: 1, data: { error: '上游暂时拉不到章节内容' } });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const context = { ...fakeContext(), workspace: new Workspace(workspaceDir) };
    const failure = await registry.execute('document_content', { book_id: 'b1', item_id: 'c1' }, context);
    assert.equal(failure?.ok, false);
    assert.ok(failure?.output.includes('data.item_id'), 'names the missing field');
    assert.ok(failure?.output.includes('上游暂时拉不到章节内容'), 'surfaces data.error');
    assert.ok(failure?.output.includes('重试'), 'advises retry instead of parroting jargon');
    assert.ok(!failure?.output.includes('参数 data.item_id 缺失'), 'no raw 参数缺失 jargon');
  } finally {
    restore();
    await rm(workspaceDir, { recursive: true, force: true });
    await cleanup();
  }
});

test('inline image short-ID miss fails honestly instead of substituting the current image', async () => {
  const { dir, cleanup } = await makePluginDir({
    'image/plugin.json': JSON.stringify({
      name: 'image',
      tools: [{
        name: 'image_edit',
        description: 'edit',
        parameters: { type: 'object', properties: { image: { type: 'string' } }, required: ['image'] },
        inline_data_uri: ['image'],
        request: { url: 'https://93.184.216.34/edit', body: '{"image":{{image}}}' },
      }],
    }),
  });
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const context = fakeContext();
    context.triggerImageRefs = ['https://a.cn/unrelated-current.jpg'];
    context.repo = {
      findEntryByRef: () => null,
      recentImageRefs: () => [{ ref: 'REAL', ageMinutes: 1 }],
    } as never;
    const result = await registry.execute('image_edit', { image: '#TYPO' }, context);
    assert.equal(result?.ok, false);
    assert.ok(result?.output.includes('不能偷偷改用本轮其他图片'));
    assert.ok(result?.output.includes('REAL'));
  } finally {
    await cleanup();
  }
});

test('JSON body 插值：字符串内转义引号/换行，字符串外保留数组（image_edit 400 回归）', () => {
  const values: Record<string, string> = {
    prompt: '删除 "Mutianyu Great Wall"\n并保留城墙',
    image: JSON.stringify(['data:image/png;base64,AAAA']),
  };
  const body = interpolateJsonBody('{"prompt":"{{prompt}}","image":{{image}}}', (key) => values[key]);
  assert.deepEqual(JSON.parse(body), {
    prompt: values.prompt,
    image: ['data:image/png;base64,AAAA'],
  });
});

test('JSON body 插值：模板原有转义与占位符相邻时仍合法', () => {
  const body = interpolateJsonBody('{"text":"前缀\\n{{value}}后缀","n":{{n}}}', (key) => key === 'value' ? '"中"' : '2');
  assert.deepEqual(JSON.parse(body), { text: '前缀\n"中"后缀', n: 2 });
});

test('event hooks: hook-only manifest loads; emit posts matching events with filters and templates', async () => {
  const { dir, cleanup } = await makePluginDir({
    'relay/plugin.json': JSON.stringify({
      name: 'relay',
      description: '事件转发',
      hooks: [
        { event: 'group_message', request: { url: 'https://93.184.216.34/hook?g={{event.scopeId}}' }, filter: { scopes: ['group:777'], contains: ['打卡'] } },
        { event: 'friend_request', request: { url: 'https://93.184.216.34/req', body: '{"who":"{{event.userId}}","k":"{{secrets.HOOK_KEY}}"}' } },
      ],
    }),
    'secrets.json': JSON.stringify({ HOOK_KEY: 'hk-999' }),
    'broken/plugin.json': JSON.stringify({ name: 'broken', hooks: [{ event: 'nope', request: { url: 'https://x.test' } }] }),
  });
  const originalFetch = globalThis.fetch;
  const calls: { url: string; body: string }[] = [];
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    calls.push({ url: String(input instanceof Request ? input.url : input), body: String(init?.body ?? '') });
    return new Response('ok', { status: 200 });
  }) as typeof fetch;
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const infos = registry.listInfo();
    assert.deepEqual(infos.find((i) => i.name === 'relay')?.hooks, ['group_message', 'friend_request']);
    assert.match(infos.find((i) => i.name === 'broken')?.error ?? '', /event 不合法/);

    // 过滤：scope 不在白名单 / 文本不含关键词 → 不派发
    assert.equal(registry.emit('group_message', { scopeType: 'group', scopeId: '888', text: '打卡' }).dispatched, 0);
    assert.equal(registry.emit('group_message', { scopeType: 'group', scopeId: '777', text: '早上好' }).dispatched, 0);

    const hit = registry.emit('group_message', { scopeType: 'group', scopeId: '777', userId: 5, text: '今日打卡' });
    assert.equal(hit.dispatched, 1);
    assert.deepEqual((await hit.settled).map((r) => r.ok), [true]);
    assert.ok(calls[0].url.endsWith('/hook?g=777'), calls[0].url);
    const defaultBody = JSON.parse(calls[0].body);
    assert.equal(defaultBody.event, 'group_message');
    assert.equal(defaultBody.data.text, '今日打卡');

    await registry.emit('friend_request', { userId: 42 }).settled;
    assert.deepEqual(JSON.parse(calls[1].body), { who: '42', k: 'hk-999' });

    // 关闭插件后不再派发
    registry.setEnabled('relay', false);
    assert.equal(registry.emit('friend_request', { userId: 1 }).dispatched, 0);
  } finally {
    globalThis.fetch = originalFetch;
    await cleanup();
  }
});

test('event hooks: failures and SSRF refusals resolve as errors, never throw', async () => {
  const { dir, cleanup } = await makePluginDir({
    'relay/plugin.json': JSON.stringify({
      name: 'relay',
      hooks: [
        { event: 'connected', request: { url: 'http://127.0.0.1:9/hook' } },
        { event: 'connected', request: { url: 'https://93.184.216.34/h', headers: { 'x-k': '{{secrets.MISSING}}' } } },
      ],
    }),
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response('nope', { status: 500 })) as typeof fetch;
  try {
    const registry = new PluginRegistry(dir);
    registry.load();
    const results = await registry.emit('connected', { self_id: 1 }).settled;
    assert.equal(results.length, 2);
    assert.ok(results.every((r) => r.ok === false));
    assert.ok(results.some((r) => /secrets\.json 里没有/.test(r.error ?? '')));
  } finally {
    globalThis.fetch = originalFetch;
    await cleanup();
  }
});
