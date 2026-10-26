/** 管理面板 API：鉴权、会话读写护栏、提示词路径安全。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request } from 'node:http';
import { HealthServer } from '../src/api/health-server.js';
import { ScopeRepository } from '../src/store/repository.js';
import { KnowledgeBase } from '../src/knowledge/kb.js';
import { RelationGraph } from '../src/relations/graph.js';
import { UsageTracker } from '../src/models/usage.js';
import type { WebuiDeps } from '../src/api/webui.js';

interface Fixture {
  port: number;
  repo: ScopeRepository;
  promptDir: string;
  modelsPath: string;
  configPath: string;
  adminInbox: string[];
  invalidated: () => number;
  cleanup: () => Promise<void>;
}

async function makeFixture(token = ''): Promise<Fixture> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-webui-'));
  const promptDir = join(dir, 'prompt');
  await mkdir(join(promptDir, 'staff'), { recursive: true });
  await writeFile(join(promptDir, 'char.txt'), '人设内容', 'utf-8');
  await writeFile(join(promptDir, 'staff', '10_a.txt'), 'staff 层', 'utf-8');
  const repo = new ScopeRepository(join(dir, 'state'), 500);
  repo.updateAgent('private', '123456789', { displayName: '号主' });
  repo.updateAgent('group', '777', { triggerRate: 0.5, triggerWords: ['词一'] });
  const modelsPath = join(dir, 'models_config.json');
  await writeFile(modelsPath, JSON.stringify({
    upstreams: [{ name: 'a', base_url: 'https://a.test', api_key: 'sk-secret-1234567890', protocol: 'anthropic' }],
    channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'a', model_id: 'm1' }] }],
    roles: { main: 'main' },
  }), 'utf-8');
  const adminInbox: string[] = [];
  const usage = new UsageTracker();
  usage.recordSuccess('main', 'a/m1', { text: '好', toolCalls: [], usage: { inputTokens: 1200, outputTokens: 34 } });
  usage.recordFailure('main', 'a/m1', new Error('upstream 403: 预扣费额度失败'));
  // 主AI会话历史：一条号主消息 + 一条 AI 回复
  repo.appendMessage('private', '123456789', { user_id: 123456789, nickname: '号主', text: '在吗', timestamp: 1000, message_id: 1 });
  repo.appendMessage('private', '123456789', { user_id: 987654321, nickname: '测试号', text: '在的', timestamp: 1001, message_id: 2 });
  await repo.flush(); // listAgents 扫磁盘文件，先让异步 persist 落盘
  let invalidateCount = 0;
  let summaryOn = true;
  let intelOn = true;
  const configPath = join(dir, 'config.json');
  await writeFile(configPath, JSON.stringify({
    ai: { summary_enabled: true, intel_enabled: true },
    napcat: {
      mode: 'server',
      ws_url: 'ws://127.0.0.1:7631/openclaw-bind?access_token=ws-secret',
      http_url: 'http://127.0.0.1:7632',
      access_token: 'napcat-secret',
      reverse_ws_port: 7636,
      public_base_url: 'http://127.0.0.1:7650',
      self_id: 987654321,
      legacy_self_ids: [123456],
    },
  }), 'utf-8');
  const deps: WebuiDeps = {
    masterQq: 123456789,
    promptDir,
    token,
    bot: { selfId: 987654321, selfNickname: '测试号', connected: true },
    repo,
    models: {
      summaryText: () => '渠道:\nmain [fallback]: a/m1',
      resolveRoleChannel: (role) => (role === 'main' ? 'main' : null),
      getModelForRole: () => ({ displayName: 'a/m1', channelName: 'main' }),
    },
    invalidatePrompts: () => {
      invalidateCount += 1;
    },
    usage,
    injectAdmin: (text) => adminInbox.push(text),
    masterHistory: () => repo.getMessages('private', '123456789'),
    modelsConfigPath: modelsPath,
    reloadModels: async () => undefined,
    kb: new KnowledgeBase(join(dir, 'knowledge'), 'private:123456789'),
    kbMasterScope: 'private:123456789',
    plugins: null,
    relations: new RelationGraph(join(dir, 'relations.json')),
    setAllScopesEnabled: (enabled) => {
      let changed = 0;
      for (const agent of repo.listAgents()) {
        if (agent.scopeType === 'private' && agent.scopeId === '123456789') continue;
        repo.updateAgent(agent.scopeType, agent.scopeId, { enabled });
        changed += 1;
      }
      return changed;
    },
    setSummaryEnabled: (enabled) => {
      summaryOn = enabled;
    },
    setIntelEnabled: (enabled) => {
      intelOn = enabled;
    },
    summaryEnabled: () => summaryOn,
    intelEnabled: () => intelOn,
    configPath,
    stateDir: join(dir, 'state'),
    workspaceDir: join(dir, 'workspace'),
    dataDir: dir,
    mcFetch: async () => ({ ok: false, error: '测试环境无边车' }),
    mcbotDir: join(dir, 'mcbot'),
    repoDir: dir,
    packageVersion: '0.0.0-test',
  };
  const server = new HealthServer({ host: '127.0.0.1', port: 0 }, { isReady: () => true }, deps);
  await server.start();
  const address = server.address();
  assert.ok(address);
  return {
    port: address.port,
    repo,
    promptDir,
    modelsPath,
    configPath,
    adminInbox,
    invalidated: () => invalidateCount,
    cleanup: async () => {
      await server.stop();
      await repo.flush();
      await rm(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
    },
  };
}

function call(
  port: number,
  method: string,
  path: string,
  body?: unknown,
  token = '',
): Promise<{ statusCode: number; body: string }> {
  return new Promise((resolvePromise, reject) => {
    const req = request(
      {
        hostname: '127.0.0.1',
        port,
        path,
        method,
        headers: {
          'content-type': 'application/json',
          ...(token ? { authorization: `Bearer ${token}` } : {}),
        },
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () => resolvePromise({
          statusCode: response.statusCode ?? 0,
          body: Buffer.concat(chunks).toString('utf8'),
        }));
      },
    );
    req.on('error', reject);
    if (body !== undefined) req.write(JSON.stringify(body));
    req.end();
  });
}

test('status / scopes / models endpoints respond with live data', async () => {
  const fixture = await makeFixture();
  try {
    const status = JSON.parse((await call(fixture.port, 'GET', '/api/status')).body);
    assert.equal(status.bot.connected, true);
    assert.equal(status.bot.nickname, '测试号');
    assert.equal(status.scopes.total, 2);

    const scopes = JSON.parse((await call(fixture.port, 'GET', '/api/scopes')).body);
    assert.equal(scopes[0].isMaster, true, '号主会话排第一');
    const group = scopes.find((s: { scopeId: string }) => s.scopeId === '777');
    assert.equal(group.triggerRate, 0.5);
    assert.deepEqual(group.triggerWords, ['词一']);

    const page = await call(fixture.port, 'GET', '/');
    assert.equal(page.statusCode, 200);
    assert.ok(page.body.includes('DeepLive'));

    const models = JSON.parse((await call(fixture.port, 'GET', '/api/models')).body);
    assert.ok(models.text.includes('a/m1'));

    assert.equal(status.usage.total.requests, 2, '用量随状态接口下发');
    assert.equal(status.usage.total.failures, 1);
    assert.equal(status.usage.byRole.main.inputTokens, 1200);
    assert.ok(status.usage.lastError.message.includes('403'));
  } finally {
    await fixture.cleanup();
  }
});

test('scope update writes through, master scope refuses disable', async () => {
  const fixture = await makeFixture();
  try {
    const off = await call(fixture.port, 'POST', '/api/scope', {
      scopeType: 'group', scopeId: '777', enabled: false, triggerRate: 9,
    });
    assert.equal(off.statusCode, 200);
    const agent = fixture.repo.getOrCreateAgent('group', '777');
    assert.equal(agent.enabled, false);
    assert.equal(agent.triggerRate, 1, 'rate 钳到 0~1');

    const refused = await call(fixture.port, 'POST', '/api/scope', {
      scopeType: 'private', scopeId: '123456789', enabled: false,
    });
    assert.equal(refused.statusCode, 400);
    assert.ok(JSON.parse(refused.body).error.includes('防失联'));
    assert.equal(fixture.repo.getOrCreateAgent('private', '123456789').enabled, true);

    const bad = await call(fixture.port, 'POST', '/api/scope', { scopeType: 'dm', scopeId: '1' });
    assert.equal(bad.statusCode, 400);
  } finally {
    await fixture.cleanup();
  }
});

test('prompt read/write confined to promptDir, write invalidates cache', async () => {
  const fixture = await makeFixture();
  try {
    const read = JSON.parse((await call(fixture.port, 'GET', '/api/prompt?path=char.txt')).body);
    assert.equal(read.content, '人设内容');

    const traversal = await call(fixture.port, 'GET', '/api/prompt?path=../config.json');
    assert.equal(traversal.statusCode, 400);
    const absolute = await call(fixture.port, 'GET', '/api/prompt?path=/etc/passwd');
    assert.equal(absolute.statusCode, 400);
    const notTxt = await call(fixture.port, 'GET', '/api/prompt?path=secrets.json');
    assert.equal(notTxt.statusCode, 400);

    const before = fixture.invalidated();
    const write = await call(fixture.port, 'POST', '/api/prompt', { path: 'staff/20_b.txt', content: '新层' });
    assert.equal(write.statusCode, 200);
    assert.equal(fixture.invalidated(), before + 1, '写盘后缓存失效回调');
    const reread = JSON.parse((await call(fixture.port, 'GET', '/api/prompt?path=staff/20_b.txt')).body);
    assert.equal(reread.content, '新层');

    const list = JSON.parse((await call(fixture.port, 'GET', '/api/prompts')).body);
    assert.deepEqual(list.map((f: { path: string }) => f.path).sort(), ['char.txt', 'staff/10_a.txt', 'staff/20_b.txt']);
  } finally {
    await fixture.cleanup();
  }
});

test('token auth gates page and api when configured', async () => {
  const fixture = await makeFixture('s3cret');
  try {
    const noAuth = await call(fixture.port, 'GET', '/api/status');
    assert.equal(noAuth.statusCode, 401);
    const page = await call(fixture.port, 'GET', '/');
    assert.equal(page.statusCode, 401);
    const withBearer = await call(fixture.port, 'GET', '/api/status', undefined, 's3cret');
    assert.equal(withBearer.statusCode, 200);
    const withQuery = await call(fixture.port, 'GET', '/api/status?token=s3cret');
    assert.equal(withQuery.statusCode, 200);
    const healthOpen = await call(fixture.port, 'GET', '/health');
    assert.equal(healthOpen.statusCode, 200, '探针不受面板鉴权影响');

    const assetsDir = join(fixture.modelsPath, '..', 'assets');
    await mkdir(assetsDir, { recursive: true });
    await writeFile(join(assetsDir, 'console-bg.jpg'), Buffer.from([0xff, 0xd8, 0xff, 0xd9]));
    const background = await call(fixture.port, 'GET', '/assets/console-bg.jpg');
    assert.equal(background.statusCode, 200, 'CSS 背景图无需 token 查询参数');
    assert.equal(background.body.length > 0, true);
  } finally {
    await fixture.cleanup();
  }
});

test('chat: history roles mapped, admin message injected, cursor paging works', async () => {
  const fixture = await makeFixture();
  try {
    const all = JSON.parse((await call(fixture.port, 'GET', '/api/chat')).body);
    assert.equal(all.messages.length, 2);
    assert.equal(all.messages[0].role, 'me', '号主消息');
    assert.equal(all.messages[1].role, 'ai', 'bot 自己');

    const none = JSON.parse((await call(fixture.port, 'GET', '/api/chat?after=1001')).body);
    assert.equal(none.messages.length, 0, 'after 游标过滤');

    const sent = await call(fixture.port, 'POST', '/api/chat', { text: '面板来的指令' });
    assert.equal(sent.statusCode, 200);
    assert.deepEqual(fixture.adminInbox, ['面板来的指令']);

    const empty = await call(fixture.port, 'POST', '/api/chat', { text: '   ' });
    assert.equal(empty.statusCode, 400);
  } finally {
    await fixture.cleanup();
  }
});

test('models config: read, validate before write, backup kept', async () => {
  const fixture = await makeFixture();
  try {
    const read = JSON.parse((await call(fixture.port, 'GET', '/api/models/config')).body);
    assert.ok(read.content.includes('"m1"'), '读到配置');
    assert.ok(!read.content.includes('sk-secret'), 'api_key 掩码下发，不泄露真值');
    assert.ok(read.content.includes('•••7890'), '保留尾 4 位供辨认');

    // 掩码原样保存 → 真值自动还原，不写坏磁盘上的 key
    const maskedSave = await call(fixture.port, 'POST', '/api/models/config', { content: read.content });
    assert.equal(maskedSave.statusCode, 200);
    assert.ok((await readFile(fixture.modelsPath, 'utf-8')).includes('sk-secret-1234567890'), '掩码保存后真值还在');

    // 掩码对不上上游名 → 拒绝，不让用户误以为保存成功
    const orphanMask = await call(fixture.port, 'POST', '/api/models/config', {
      content: JSON.stringify({
        upstreams: [{ name: 'newup', base_url: 'https://n.test', api_key: '•••zzzz', protocol: 'anthropic' }],
        channels: [{ name: 'main', models: [{ upstream: 'newup', model_id: 'm3' }] }],
        roles: { main: 'main' },
      }),
    });
    assert.equal(orphanMask.statusCode, 400);

    const badJson = await call(fixture.port, 'POST', '/api/models/config', { content: '{oops' });
    assert.equal(badJson.statusCode, 400);
    const badRef = await call(fixture.port, 'POST', '/api/models/config', {
      content: JSON.stringify({ upstreams: [], channels: [{ name: 'x', models: [{ upstream: 'ghost', model_id: 'm' }] }] }),
    });
    assert.equal(badRef.statusCode, 400);
    assert.ok(JSON.parse(badRef.body).error.includes('ghost'), '引用不存在的上游被拒');

    // 坏内容不落盘
    assert.ok((await readFile(fixture.modelsPath, 'utf-8')).includes('"m1"'));

    const good = JSON.stringify({
      upstreams: [{ name: 'b', base_url: 'https://b.test', api_key: 'sk-y', protocol: 'completions' }],
      channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'b', model_id: 'm2' }] }],
      roles: { main: 'main' },
    });
    const write = await call(fixture.port, 'POST', '/api/models/config', { content: good });
    assert.equal(write.statusCode, 200);
    const files = await readdir(join(fixture.modelsPath, '..'));
    assert.equal(files.filter((f) => f.startsWith('models_config.json.bak-')).length, 2, '每次保存都留一个备份');
  } finally {
    await fixture.cleanup();
  }
});

test('NapCat config: masked read, secret-preserving save, validation and backup', async () => {
  const fixture = await makeFixture();
  try {
    const response = await call(fixture.port, 'GET', '/api/napcat/config');
    assert.equal(response.statusCode, 200);
    const settings = JSON.parse(response.body);
    assert.equal(settings.mode, 'server');
    assert.equal(settings.reverseWsPort, 7636);
    assert.ok(!response.body.includes('ws-secret'), 'WS 查询参数 Token 不下发明文');
    assert.ok(!response.body.includes('napcat-secret'), '独立 Token 不下发明文');
    assert.ok(settings.wsUrl.includes('access_token=••••••'));

    const invalid = await call(fixture.port, 'POST', '/api/napcat/config', { ...settings, mode: 'bad' });
    assert.equal(invalid.statusCode, 400);
    assert.equal(JSON.parse(await readFile(fixture.configPath, 'utf-8')).napcat.mode, 'server', '非法配置不落盘');

    settings.mode = 'client';
    settings.reverseWsPort = 0;
    settings.wsUrl = 'ws://127.0.0.1:7631/changed?access_token=••••••';
    settings.accessToken = '';
    const saved = await call(fixture.port, 'POST', '/api/napcat/config', settings);
    assert.equal(saved.statusCode, 200);
    assert.equal(JSON.parse(saved.body).restartRequired, true);
    const disk = JSON.parse(await readFile(fixture.configPath, 'utf-8'));
    assert.equal(disk.napcat.mode, 'client');
    assert.equal(disk.napcat.ws_url, 'ws://127.0.0.1:7631/changed?access_token=ws-secret', '改地址仍还原原 WS Token');
    assert.equal(disk.napcat.access_token, 'napcat-secret', 'Token 留空保持原值');
    assert.deepEqual(disk.napcat.legacy_self_ids, [123456], '未编辑字段保留');
    assert.equal(disk.ai.summary_enabled, true, '其他配置段保留');
    const files = await readdir(join(fixture.configPath, '..'));
    assert.equal(files.filter((f) => f.startsWith('config.json.bak-')).length, 1, '保存前留备份');
  } finally {
    await fixture.cleanup();
  }
});

test('面板内嵌 JS 可解析（白屏回归：字符串引号错配曾致整段脚本失效）', async () => {
  const { WEBUI_PAGE } = await import('../src/api/webui-page.js');
  const html = WEBUI_PAGE as string;
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.ok(blocks.length > 0, '面板应含内嵌脚本');
  for (const code of blocks) {
    assert.doesNotThrow(() => new Function(code as string), '内嵌 JS 必须语法合法');
  }
  // 内嵌脚本禁用反引号/模板插值（会吃掉外层模板），也一并守住
  for (const code of blocks) {
    assert.ok(!code.includes('`'), '内嵌 JS 不得含反引号');
  }
});
