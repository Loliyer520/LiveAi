/**
 * ROADMAP P0 三件套：声明式领域注册表、插件 manifest v2（配置项）、统一探针。
 */
import { createServer } from 'node:http';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { domainDefs, loadDomains, domainDefForScope, hubForScope, domainLoadErrors } from '../src/chat/domains.js';
import { PluginRegistry } from '../src/chat/plugins.js';
import { probeRecord, probeSnapshot, probeReset, withProbe } from '../src/observability/probe.js';
import type { ToolContext } from '../src/chat/tools.js';

test('声明式领域注册表：data/domains/*.json 可覆盖内置并新增领域，路由随之生效', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dl-domains-'));
  try {
    // 覆盖内置（改 title）+ 新增（child_types 挂路由）
    await writeFile(join(dir, 'mc.json'), JSON.stringify({ title: 'MC 大脑' }), 'utf-8');
    await writeFile(join(dir, 'novel.json'), JSON.stringify({
      title: '小说中枢',
      description: '每本书一个下级 AI',
      child_types: ['novelbook'],
    }), 'utf-8');
    await writeFile(join(dir, 'Bad_Id.json'), '{}', 'utf-8');
    loadDomains(dir);

    assert.equal(domainDefForScope('domain:mc')?.title, 'MC 大脑', 'JSON 覆盖内置 title');
    const novel = domainDefForScope('domain:novel');
    assert.ok(novel, 'JSON 新增领域生效');
    assert.equal(novel?.custom, true);
    assert.equal(novel?.description, '每本书一个下级 AI');
    assert.equal(hubForScope('novelbook:BV1', 'private:1'), 'domain:novel', 'child_types 挂进汇报路由');
    assert.equal(hubForScope('mcclient:a', 'private:1'), 'domain:mc', '内置路由不变');
    assert.equal(hubForScope('group:1', 'private:1'), 'domain:onebot');
    await writeFile(join(dir, 'broken.json'), 'not json', 'utf-8');
    loadDomains(dir);
    assert.equal(domainLoadErrors().length, 2, '坏 JSON 与坏 id 都被记录而不是崩溃');
    assert.ok(domainDefs().length >= 4);
  } finally {
    await rm(dir, { recursive: true, force: true });
    loadDefaults();
  }
  function loadDefaults(): void {
    // 恢复无自定义目录的状态（data/domains 不存在 = 纯内置）
    loadDomains(join(tmpdir(), 'dl-domains-none-'));
  }
});

test('插件 manifest v2：config 声明 + {{config.*}} 引用 + 面板读写', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dl-pluginv2-'));
  const server = await httpServerForPluginTests();
  try {
    await mkdir(join(dir, 'demo'), { recursive: true });
    await writeFile(join(dir, 'demo', 'plugin.json'), JSON.stringify({
      name: 'demo',
      description: 'v2 演示',
      version: '1.2.0',
      config: {
        base: { title: '服务基址', type: 'string', default: `http://127.0.0.1:${server.port}` },
        limit: { title: '上限', type: 'number', default: 3 },
        verbose: { title: '啰嗦', type: 'boolean', default: false },
      },
      tools: [{
        name: 'demo_get',
        description: '取演示数据',
        allow_internal: true,
        parameters: { type: 'object', properties: { q: { type: 'string' } }, required: ['q'] },
        request: { method: 'GET', url: '{{config.base}}/echo?q={{q}}&limit={{config.limit}}' },
        response: { format: 'text' },
      }],
    }), 'utf-8');
    const registry = new PluginRegistry(dir);
    registry.load();

    // 默认值回填 + 面板读取
    const config = registry.getConfig('demo');
    assert.ok(config, 'getConfig 返回声明+值');
    assert.equal(config?.spec.base?.type, 'string');
    assert.equal(config?.values.base, `http://127.0.0.1:${server.port}`, 'default 已回填');

    // 详情带版本与 config 标记
    const detail = registry.toolDetails('demo');
    assert.equal(detail?.version, '1.2.0');
    assert.equal(detail?.tools[0]?.usesConfig, true);
    assert.equal(detail?.tools[0]?.configMissing, false);

    // 保存配置（类型强转）+ 执行时 {{config.*}} 解析
    const saved = registry.setConfig('demo', { limit: '7', verbose: 'yes' });
    assert.equal(saved.limit, 7, '数字强转');
    assert.equal(saved.verbose, true);
    const result = await registry.execute('demo_get', { q: 'hi' }, stubContext());
    assert.equal(result?.ok, true, `execute 输出: ${result?.output}`);
    assert.ok(result?.output.includes('limit=7'), `URL 里 config.limit 已解析: ${result?.output}`);

    // 面板试玩也走 config
    const tried = await registry.tryTool('demo', 'demo_get', { q: 'play' });
    assert.equal(tried.ok, true);
    assert.ok(tried.body?.includes('limit=7'));

    // 声明外的 KEY 一律丢弃
    const strict = registry.setConfig('demo', { limit: '1', evil: '; rm -rf' });
    assert.equal('evil' in strict, false, '未声明 KEY 被丢弃');
  } finally {
    server.close();
    await rm(dir, { recursive: true, force: true });
  }
});

test('统一探针：record/withProbe 聚合、慢事件、快照与清零', async () => {
  probeReset();
  probeRecord('llm.call', 50);
  probeRecord('llm.call', 30);
  probeRecord('slow.thing', 99999);
  await withProbe('wrapped', async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
  const snap = probeSnapshot();
  const llm = snap.aggregates.find((a) => a.name === 'llm.call');
  assert.equal(llm?.count, 2);
  assert.equal(llm?.avgMs, 40);
  const slow = snap.aggregates.find((a) => a.name === 'slow.thing');
  assert.ok(slow && slow.maxMs >= snap.slowThresholdMs, '超阈值进聚合');
  assert.ok(snap.slow.some((s) => s.name === 'slow.thing'), '慢事件留现场');
  assert.ok(snap.recent.some((s) => s.name === 'wrapped'), 'withProbe 打点进 recent');
  probeReset();
  assert.equal(probeSnapshot().aggregates.length, 0, '清零生效');
});

// ── 测试辅助 ────────────────────────────────────────────────────────────────

/** 起一个本地 echo HTTP 服务（插件请求落点）。 */
async function httpServerForPluginTests(): Promise<{ port: number; close: () => void }> {
  const srv = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    res.setHeader('content-type', 'text/plain');
    res.end(`${url.pathname}?${url.searchParams.toString()}`);
  });
  await new Promise<void>((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const address = srv.address();
  const port = typeof address === 'object' && address !== null ? address.port : 0;
  return { port, close: () => srv.close() };
}

function stubContext(): ToolContext {
  return {
    scopeType: 'private',
    scopeId: '241898129',
    isMaster: true,
    networkBudget: { used: 0 },
  } as unknown as ToolContext;
}

