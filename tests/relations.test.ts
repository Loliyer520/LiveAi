import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { RelationGraph } from '../src/relations/graph.js';
import { IntelCollector, parseExtraction } from '../src/relations/collector.js';
import { ScopeRepository } from '../src/store/repository.js';
import { ModelManager } from '../src/models/manager.js';
import { executeChatTool, chatToolSchemas } from '../src/chat/tools.js';
import type { ToolContext } from '../src/chat/tools.js';
import { Workspace } from '../src/chat/workspace.js';

test('touchMember builds people + scopes with alias map', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    graph.touchMember('group:1', '闲聊群', 100, '小明');
    graph.touchMember('group:1', '闲聊群', 100, '小明pro');
    graph.touchMember('private:100', '', 100, '小明');
    graph.touchMember('group:1', '闲聊群', 101, '小红');

    const person = graph.person(100)!;
    assert.equal(person.canonicalName, '小明', 'latest nickname wins');
    assert.equal(person.scopes.length, 2, 'cross-scope presence tracked');
    assert.equal(person.aliases['group:1'], '小明pro', 'per-scope alias kept');

    const scopeCard = graph.scopeCard('group:1');
    assert.ok(scopeCard.includes('小明(100)'), 'canonical name rendered');
    assert.ok(scopeCard.includes('小红(101)'));

    const stats = graph.stats();
    assert.equal(stats.people, 2);
    assert.equal(stats.scopes, 2);
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('addFact dedupes, caps, supersedes and searches', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    const first = graph.addFact({ subject: 100, category: 'preference', text: '喜欢原神', sourceScope: 'group:1', sourceTool: 'intel_report' })!;
    const dup = graph.addFact({ subject: 100, category: 'preference', text: '喜欢原神', sourceScope: 'group:2', sourceTool: 'auto_extract' })!;
    assert.equal(dup.id, first.id, 'identical text dedupes');

    const second = graph.addFact({ subject: 100, category: 'identity', text: '高三学生', sourceScope: 'group:1', sourceTool: 'intel_report' })!;
    assert.notEqual(second.id, first.id);

    const superseder = graph.supersedeFact(first.id, '更喜欢星穹铁道')!;
    assert.equal(graph.findFact(first.id)!.status, 'superseded');
    assert.equal(graph.findFact(first.id)!.supersededBy, superseder.id);

    // search only sees active facts
    assert.equal(graph.searchFacts('原神').length, 0);
    assert.equal(graph.searchFacts('星穹铁道').length, 1);

    // capacity retirement keeps the newest facts active
    for (let index = 0; index < 70; index += 1) {
      graph.addFact({ subject: 100, category: 'other', text: `事实编号${index}`, sourceScope: 'group:1', sourceTool: 'auto_extract' });
    }
    const active = graph.person(100)!.facts.filter((fact) => fact.status === 'active');
    assert.equal(active.length, 60);
    assert.ok(active.some((fact) => fact.text === '事实编号69'), 'oldest retired, newest kept');
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('personCard formats with aliases, categories and confidence marks', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    graph.touchMember('group:1', '群', 100, '小明');
    graph.touchMember('group:2', '群2', 100, '明哥');
    graph.addFact({ subject: 100, category: 'identity', text: '高三学生，理科班', sourceScope: 'group:1', sourceTool: 'intel_report' });
    graph.addFact({ subject: 100, category: 'preference', text: '讨厌香菜', sourceScope: 'group:2', sourceTool: 'auto_extract', confidence: 0.5 });

    const card = graph.personCard(100);
    assert.ok(card.includes('明哥(100)'), 'canonical name first');
    assert.ok(card.includes('2 个会话'));
    assert.ok(card.includes('小明'), 'other-scope alias listed');
    assert.ok(card.includes('[identity]'));
    assert.ok(card.includes('高三学生'));
    assert.ok(card.includes('（不太确定）'), 'low-confidence marked');
    assert.equal(graph.personCard(999), '');
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('parseExtraction tolerates fences and surrounding prose', () => {
  const parsed = parseExtraction('好的，结果如下：\n```json\n{"facts":[{"subject_id":1,"category":"preference","text":"喜欢猫","confidence":0.9}],"topics":["宠物"],"scope_impression":"养猫交流群"}\n```');
  assert.equal(parsed.facts.length, 1);
  assert.equal(parsed.facts[0].subject_id, 1);
  assert.equal(parsed.topics[0], '宠物');
  assert.equal(parsed.scopeImpression, '养猫交流群');
  assert.deepEqual(parseExtraction('不是JSON'), { facts: [], topics: [], scopeImpression: '' });
});

test('impressions merge into the graph and render on cards', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    graph.touchMember('group:1', '闲聊群', 100, '小明');
    graph.setScopeImpression('group:1', '游戏开黑群，晚上最活跃，小明是组织者');
    graph.setPersonImpression(100, '外向靠谱，说话算数，群里的话事人');

    assert.equal(graph.scopeImpression('group:1'), '游戏开黑群，晚上最活跃，小明是组织者');
    assert.ok(graph.scopeCard('group:1').includes('印象: 游戏开黑群'));
    const card = graph.personCard(100);
    assert.ok(card.includes('印象：外向靠谱'));

    // empty / invalid writes are no-ops
    graph.setScopeImpression('group:1', '   ');
    graph.setPersonImpression(0, '无效');
    assert.equal(graph.scopeImpression('group:1'), '游戏开黑群，晚上最活跃，小明是组织者');
    assert.equal(graph.person(0), null);

    // overviewCard surfaces impression-bearing nodes for the master scope
    graph.addFact({ subject: 100, category: 'identity', text: '高三学生', sourceScope: 'group:1', sourceTool: 'master' });
    const overview = graph.overviewCard();
    assert.ok(overview.includes('小明(100)'));
    assert.ok(overview.includes('条情报'));
    assert.ok(overview.includes('group:1'));
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('intel_report and relation_query tools round-trip through the graph', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    const context = toolContext(graph);

    const report = await executeChatTool(
      { name: 'intel_report', arguments: { fact_subject_id: 100, fact_category: 'identity', fact_text: '下个月生日', confidence: 0.9 } },
      context,
    );
    assert.equal(report.ok, true);
    assert.ok(report.output.includes('1 条情报'));

    const query = await executeChatTool({ name: 'relation_query', arguments: { user_id: 100 } }, context);
    assert.ok(query.output.includes('人物档案'));
    assert.ok(query.output.includes('下个月生日'));

    const search = await executeChatTool({ name: 'relation_query', arguments: { query: '生日' } }, context);
    assert.ok(search.output.includes('情报命中'));

    const miss = await executeChatTool({ name: 'relation_query', arguments: { query: '不存在' } }, context);
    assert.ok(miss.output.includes('没有命中'));
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('collector extracts facts and topics into the shared graph', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-col-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  const repo = new ScopeRepository(join(dir, 'state'), 500);
  const modelsJson = join(dir, 'models_config.json');
  await writeFile(modelsJson, JSON.stringify({
    upstreams: [{ name: 'stub', base_url: 'http://model.test', api_key: 'sk-test', protocol: 'anthropic' }],
    channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'stub', model_id: 'test-model' }] }],
    roles: { main: 'main' },
  }), 'utf-8');
  const models = new ModelManager(modelsJson);
  await models.load();

  const extraction = JSON.stringify({
    facts: [
      { subject_id: 100, category: 'preference', text: '最近在准备考研', confidence: 0.9 },
      { subject_id: 0, category: 'other', text: '无效主体会被丢弃', confidence: 0.9 },
    ],
    topics: ['考研', '学习'],
    scope_impression: '考研互助群，氛围卷但友善',
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    const url = String(input instanceof Request ? input.url : input);
    assert.ok(url.includes('model.test'), `unexpected fetch: ${url}`);
    const rawBody = input instanceof Request ? await input.clone().text() : String(init?.body ?? '{}');
    const body = JSON.parse(rawBody) as { messages?: { content: unknown }[] };
    const prompt = JSON.stringify(body.messages ?? []);
    assert.ok(prompt.includes('情报'), `unexpected prompt: ${prompt.slice(0, 200)}`);
    return new Response(JSON.stringify({ content: [{ type: 'text', text: extraction }], usage: { input_tokens: 1, output_tokens: 1 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;

  try {
    const collector = new IntelCollector(graph, repo, models, () => 999, true);
    for (let index = 0; index < 20; index += 1) {
      repo.appendMessage('group', '5', {
        user_id: 100,
        nickname: '小明',
        text: `聊天内容 ${index}，说到考研复习`,
        timestamp: 1_700_000_000 + index,
      });
      collector.notify('group', '5');
    }
    await collector.drain();

    const person = graph.person(100);
    assert.ok(person !== null, 'person created from extracted subject');
    assert.ok(person.facts.some((fact) => fact.text === '最近在准备考研' && fact.sourceTool === 'auto_extract'));
    assert.equal(graph.person(0), null, 'subject 0 discarded');
    assert.ok(graph.scopeCard('group:5').includes('考研'));
    assert.ok(graph.scopeCard('group:5').includes('学习'));
    assert.equal(graph.scopeImpression('group:5'), '考研互助群，氛围卷但友善', 'scope impression refreshed by extraction');
    await graph.flush();
  } finally {
    globalThis.fetch = originalFetch;
    await repo.flush();
    await rm(dir, { recursive: true, force: true });
  }
});

function toolContext(graph: RelationGraph, isMaster = false): ToolContext {
  return {
    bot: {} as never,
    repo: {} as never,
    archive: {} as never,
    relations: graph,
    tasks: {} as never,
    masterQq: 1,
    isMaster,
    notifyMaster: () => {},
    delegateToChild: () => 'delegate_x',
    sendToScope: async () => 'ok',
    triggerImageRefs: [],
    describeImage: async () => '',
    scopeType: 'group',
    scopeId: '1',
    executedTools: [],
    workspace: new Workspace('/tmp/liveai-test-workspace'),
    networkBudget: { used: 0 },
  };
}

test('chatToolSchemas swaps report tools for master tools in the master scope', () => {
  const child = chatToolSchemas(false).map((schema) => schema.name);
  assert.ok(child.includes('notify_master'));
  assert.ok(child.includes('intel_report'));
  assert.ok(child.includes('impression_write'));
  assert.ok(!child.includes('relation_write'));
  assert.ok(!child.includes('delegate_to_child'));

  const master = chatToolSchemas(true).map((schema) => schema.name);
  assert.ok(!master.includes('notify_master'), 'master never relays to itself');
  assert.ok(!master.includes('intel_report'));
  assert.ok(master.includes('relation_write'));
  assert.ok(master.includes('delegate_to_child'));
  assert.ok(master.includes('message_scope'));
  assert.ok(master.includes('relation_query'), 'read path stays');
  assert.ok(master.includes('memory_search'));
});

test('master-only tools are gated by scope, relation_write edits the graph', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-rel-'));
  const graph = new RelationGraph(join(dir, 'relations.json'));
  try {
    // child context: hard reject, no self-loop path
    const denied = await executeChatTool(
      { name: 'relation_write', arguments: { action: 'add_fact', user_id: 100, text: 'x' } },
      toolContext(graph, false),
    );
    assert.equal(denied.ok, false);
    assert.ok(denied.output.includes('仅限主AI'));
    const deniedDelegate = await executeChatTool(
      { name: 'delegate_to_child', arguments: { target_scope_type: 'group', target_scope_id: '9', instruction: 'x' } },
      toolContext(graph, false),
    );
    assert.equal(deniedDelegate.ok, false);

    // master context: full write path
    const master = toolContext(graph, true);
    const impression = await executeChatTool(
      { name: 'relation_write', arguments: { action: 'set_person_impression', user_id: 100, text: '沉默但靠谱' } },
      master,
    );
    assert.equal(impression.ok, true);
    assert.equal(graph.person(100)!.impression, '沉默但靠谱');

    const added = await executeChatTool(
      { name: 'relation_write', arguments: { action: 'add_fact', user_id: 100, category: 'identity', text: '山东人' } },
      master,
    );
    assert.equal(added.ok, true);
    const factId = /fact_id (f_\w+)/.exec(added.output)![1];

    const superseded = await executeChatTool(
      { name: 'relation_write', arguments: { action: 'supersede_fact', fact_id: factId, text: '山东烟台人' } },
      master,
    );
    assert.equal(superseded.ok, true);
    assert.equal(graph.searchFacts('山东人').length, 0);
    assert.equal(graph.searchFacts('烟台').length, 1);

    const scopeImpression = await executeChatTool(
      { name: 'relation_write', arguments: { action: 'set_scope_impression', target_scope_type: 'group', target_scope_id: '9', text: '技术群' } },
      master,
    );
    assert.equal(scopeImpression.ok, true);
    assert.equal(graph.scopeImpression('group:9'), '技术群');

    const delegated = await executeChatTool(
      { name: 'delegate_to_child', arguments: { target_scope_type: 'group', target_scope_id: '9', instruction: '问一下周末聚会时间' } },
      master,
    );
    assert.equal(delegated.ok, true);
    assert.ok(delegated.output.includes('已委派'));
    await graph.flush();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
