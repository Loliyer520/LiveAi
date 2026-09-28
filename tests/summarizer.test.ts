import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ScopeRepository } from '../src/store/repository.js';
import { MessageArchive } from '../src/store/archive.js';
import { DiarySummarizer } from '../src/store/summarizer.js';
import { ModelManager } from '../src/models/manager.js';

interface Fixture {
  repo: ScopeRepository;
  archive: MessageArchive;
  summarizer: DiarySummarizer;
  models: ModelManager;
  replyTexts: string[];
  setFailModel: (fail: boolean) => void;
  cleanup: () => Promise<void>;
}

async function makeFixture(): Promise<Fixture> {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-sum-'));
  const stateDir = join(dir, 'state');
  const archive = new MessageArchive(join(dir, 'archive'));
  const repo = new ScopeRepository(stateDir, 500, archive);
  const modelsJson = join(dir, 'models_config.json');
  await writeFile(modelsJson, JSON.stringify({
    upstreams: [{ name: 'stub', base_url: 'http://model.test', api_key: 'sk-test', protocol: 'anthropic' }],
    channels: [{ name: 'main', strategy: 'fallback', models: [{ upstream: 'stub', model_id: 'test-model' }] }],
    roles: { main: 'main' },
  }), 'utf-8');
  const models = new ModelManager(modelsJson);
  await models.load();

  const replyTexts: string[] = [];
  let fail = false;
  let modelCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    const url = String(input instanceof Request ? input.url : input);
    assert.ok(url.includes('model.test'), `unexpected fetch: ${url}`);
    if (fail) return new Response('down', { status: 503 });
    const rawBody = input instanceof Request ? await input.clone().text() : String(init?.body ?? '{}');
    const body = JSON.parse(rawBody) as { messages?: { content: unknown }[] };
    const prompt = JSON.stringify(body.messages ?? []);
    assert.ok(prompt.includes('摘要'), `unexpected prompt: ${prompt.slice(0, 200)}`);
    const text = replyTexts.length > 0
      ? replyTexts[Math.min(replyTexts.length - 1, modelCalls)] ?? '默认摘要'
      : '默认摘要';
    modelCalls += 1;
    return new Response(JSON.stringify({ content: [{ type: 'text', text }], usage: { input_tokens: 1, output_tokens: 1 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as typeof fetch;

  const summarizer = new DiarySummarizer(repo, models, true);

  return {
    repo,
    archive,
    summarizer,
    models,
    replyTexts,
    setFailModel: (value: boolean) => {
      fail = value;
    },
    cleanup: async () => {
      globalThis.fetch = originalFetch;
      await repo.flush();
      await archive.flush();
      await rm(dir, { recursive: true, force: true });
    },
  };
}

function entry(n: number) {
  return { user_id: 100, nickname: 'user100', text: `测试消息 ${n}`, timestamp: 1_700_000_000 + n, message_id: n };
}

test('segment fills at 50, summarizes, then overall primer is built', async () => {
  const fixture = await makeFixture();
  try {
    let needs = false;
    for (let index = 0; index < 50; index += 1) {
      needs = fixture.repo.appendMessage('group', '1', entry(index));
    }
    assert.equal(needs, true, '50th message rolls the segment');
    assert.equal(fixture.repo.getUnsummarizedSegments('group', '1').length, 1);

    fixture.summarizer.notify('group', '1');
    await waitFor(() => fixture.repo.getDiarySummaries('group', '1').length === 1);

    assert.equal(fixture.repo.getUnsummarizedSegments('group', '1').length, 0, 'raw segment retired');
    const agent = fixture.repo.getOrCreateAgent('group', '1');
    assert.equal(agent.overallSummary, '默认摘要');
    assert.equal(agent.overallSummaryAtSegments, 1);
    await fixture.repo.flush();
  } finally {
    await fixture.cleanup();
  }
});

test('overall primer refreshes only after 3 fresh segments', async () => {
  const fixture = await makeFixture();
  try {
    fixture.replyTexts.push('分段一', '分段二', '分段三');
    for (let count = 0; count < 150; count += 1) {
      fixture.repo.appendMessage('group', '2', entry(count));
    }
    fixture.summarizer.notify('group', '2');
    await waitFor(() => fixture.repo.getDiarySummaries('group', '2').length === 3);

    // 3rd call = overall rebuild (calls: seg1, seg2, seg3, overall)
    const agent = fixture.repo.getOrCreateAgent('group', '2');
    assert.equal(agent.overallSummaryAtSegments, 3);
    await fixture.repo.flush();
  } finally {
    await fixture.cleanup();
  }
});

test('failed summarization keeps the raw segment queued for retry', async () => {
  const fixture = await makeFixture();
  try {
    fixture.setFailModel(true);
    for (let index = 0; index < 50; index += 1) {
      fixture.repo.appendMessage('private', '3', entry(index));
    }
    fixture.summarizer.notify('private', '3');
    await sleep(300);
    assert.equal(fixture.repo.getDiarySummaries('private', '3').length, 0, 'no summary on failure');
    assert.equal(fixture.repo.getUnsummarizedSegments('private', '3').length, 1, 'raw segment retained');

    fixture.setFailModel(false);
    fixture.summarizer.notify('private', '3');
    await waitFor(() => fixture.repo.getDiarySummaries('private', '3').length === 1);
    assert.equal(fixture.repo.getUnsummarizedSegments('private', '3').length, 0);
    await fixture.repo.flush();
  } finally {
    await fixture.cleanup();
  }
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate: () => boolean, timeoutMs = 8000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(25);
  }
  assert.fail('waitFor timed out');
}
