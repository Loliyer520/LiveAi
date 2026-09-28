/** Autonomous-action framework: sandbox, SSRF guard, zip, file tools. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { deflateRawSync, gzipSync } from 'node:zlib';
import { Workspace, safeFilename } from '../src/chat/workspace.js';
import { assertPublicUrl, isPrivateAddress, parseSearchHtml } from '../src/chat/net.js';
import { extractGzip, extractZip } from '../src/chat/zip.js';
import { executeActionTool } from '../src/chat/actions.js';
import type { ToolContext } from '../src/chat/tools.js';
import { ScopeRepository } from '../src/store/repository.js';
import type { NapcatBot } from '../src/bot/napcat.js';

// ── workspace sandbox ───────────────────────────────────────────────────────

test('workspace resolves inside paths and refuses traversal', () => {
  const ws = new Workspace('/tmp/ws-test');
  assert.equal(ws.resolvePath('a/b.txt'), '/tmp/ws-test/a/b.txt');
  assert.equal(ws.resolvePath('/abs/leading/slash.txt'), '/tmp/ws-test/abs/leading/slash.txt');
  assert.throws(() => ws.resolvePath('../escape.txt'), /越界/);
  assert.throws(() => ws.resolvePath('a/../../escape.txt'), /越界/);
  assert.throws(() => ws.resolvePath('..\\escape.txt'), /越界/);
  assert.throws(() => ws.resolvePath(''), /为空/);
});

test('safeFilename strips paths, control chars and dotfile prefixes', () => {
  assert.equal(safeFilename('小说.txt'), '小说.txt');
  assert.equal(safeFilename('a/b/c.txt'), 'c.txt');
  assert.equal(safeFilename('..\\..\\evil'), 'evil');
  assert.equal(safeFilename('.hidden'), 'hidden');
  assert.equal(safeFilename(''), 'file');
});

// ── SSRF guard ──────────────────────────────────────────────────────────────

test('isPrivateAddress classifies loopback/private/link-local', () => {
  for (const ip of ['127.0.0.1', '10.1.2.3', '172.16.0.1', '172.31.255.255', '192.168.1.1', '169.254.1.1', '0.0.0.0', '224.0.0.1']) {
    assert.equal(isPrivateAddress(ip, 4), true, ip);
  }
  for (const ip of ['8.8.8.8', '172.32.0.1', '203.0.113.9']) {
    assert.equal(isPrivateAddress(ip, 4), false, ip);
  }
  assert.equal(isPrivateAddress('::1', 6), true);
  assert.equal(isPrivateAddress('fe80::1', 6), true);
  assert.equal(isPrivateAddress('fd12::1', 6), true);
  assert.equal(isPrivateAddress('2606:4700::1', 6), false);
});

test('assertPublicUrl refuses internal destinations and bad schemes', async () => {
  await assert.rejects(() => assertPublicUrl('http://127.0.0.1:7822/get_status'), /内网/);
  await assert.rejects(() => assertPublicUrl('http://localhost:7810/'), /内网/);
  await assert.rejects(() => assertPublicUrl('http://192.168.0.1/'), /内网/);
  await assert.rejects(() => assertPublicUrl('http://[::1]/'), /内网/);
  await assert.rejects(() => assertPublicUrl('file:///etc/passwd'), /http/);
  await assert.rejects(() => assertPublicUrl('https://user:pass@8.8.8.8/'), /凭据/);
  await assert.rejects(() => assertPublicUrl('not a url'), /解析/);
  const ok = await assertPublicUrl('https://8.8.8.8/dns-query');
  assert.equal(ok.hostname, '8.8.8.8');
});

// ── search HTML parsing ─────────────────────────────────────────────────────

test('parseSearchHtml extracts results and unwraps uddg redirect links', () => {
  const html = `
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwttr.in%2F%3Fformat%3Dj1&amp;rut=abc">wttr.in — <b>天气</b></a>
    <a class="result__snippet" href="#">A weather API &amp; service</a>
    <a class="result__a" href="https://example.com/direct">Direct link</a>
    <a class="result__snippet" href="#">snippet two</a>`;
  const hits = parseSearchHtml(html, 5);
  assert.equal(hits.length, 2);
  assert.equal(hits[0]!.url, 'https://wttr.in/?format=j1');
  assert.equal(hits[0]!.title, 'wttr.in — 天气');
  assert.equal(hits[0]!.snippet, 'A weather API & service');
  assert.equal(hits[1]!.url, 'https://example.com/direct');
});

// ── zip / gzip extraction ───────────────────────────────────────────────────

interface ZipEntrySpec { name: string; data: Buffer; method: 0 | 8 }

function buildZip(entries: ZipEntrySpec[]): Buffer {
  const chunks: Buffer[] = [];
  const centrals: Buffer[] = [];
  for (const entry of entries) {
    const name = Buffer.from(entry.name, 'utf-8');
    const compressed = entry.method === 0 ? entry.data : deflateRawSync(entry.data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(entry.method, 8);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(entry.data.length, 22);
    local.writeUInt16LE(name.length, 26);
    const offset = Buffer.concat(chunks).length;
    chunks.push(local, name, compressed);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(entry.method, 10);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(entry.data.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt32LE(offset, 42);
    centrals.push(Buffer.concat([central, name]));
  }
  const localPart = Buffer.concat(chunks);
  const centralPart = Buffer.concat(centrals);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralPart.length, 12);
  eocd.writeUInt32LE(localPart.length, 16);
  return Buffer.concat([localPart, centralPart, eocd]);
}

test('extractZip handles stored and deflated entries', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-zip-'));
  try {
    const zip = buildZip([
      { name: 'hello.txt', data: Buffer.from('你好世界'), method: 0 },
      { name: 'sub/deflated.txt', data: Buffer.from('压缩内容'.repeat(100)), method: 8 },
    ]);
    const names = await extractZip(zip, dir);
    assert.deepEqual(names.sort(), ['hello.txt', 'sub/deflated.txt']);
    assert.equal(await readFile(join(dir, 'hello.txt'), 'utf-8'), '你好世界');
    assert.equal(await readFile(join(dir, 'sub/deflated.txt'), 'utf-8'), '压缩内容'.repeat(100));
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('extractZip refuses zip-slip entries', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-zip-'));
  try {
    const zip = buildZip([{ name: '../evil.txt', data: Buffer.from('x'), method: 0 }]);
    await assert.rejects(() => extractZip(zip, dir), /越界/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('extractGzip writes the decompressed payload', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-gz-'));
  try {
    const bytes = await extractGzip(gzipSync(Buffer.from('正文')), join(dir, 'out.txt'));
    assert.equal(bytes, Buffer.byteLength('正文'));
    assert.equal(await readFile(join(dir, 'out.txt'), 'utf-8'), '正文');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

// ── action tools over a fake bot ────────────────────────────────────────────

async function makeActionContext() {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-actions-'));
  const repo = new ScopeRepository(join(dir, 'state'), 500);
  const sent: string[] = [];
  const bot = {
    selfId: 3592217365,
    async sendText(_chatType: string, _targetId: number, text: string) {
      sent.push(text);
      return { message_id: 8000 + sent.length };
    },
  } as unknown as NapcatBot;
  const context: ToolContext = {
    bot,
    repo,
    archive: null as never,
    relations: null as never,
    tasks: null as never,
    masterQq: 241898129,
    isMaster: false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    workspace: new Workspace(join(dir, 'workspace')),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
    describeImage: async () => '',
    scopeType: 'private',
    scopeId: '123',
    executedTools: [],
  };
  return { context, repo, sent, cleanup: () => rm(dir, { recursive: true, force: true }) };
}

test('file_write → file_read → file_search round trip inside the sandbox', async () => {
  const { context, cleanup } = await makeActionContext();
  try {
    const written = await executeActionTool(
      { name: 'file_write', arguments: { path: 'novel.txt', content: '第一章 风起\n\n他走进雨里。\n第二章 灯火' } },
      context,
    );
    assert.ok(written?.ok);

    const read = await executeActionTool({ name: 'file_read', arguments: { path: 'novel.txt', from: 1, to: 3 } }, context);
    assert.ok(read?.output.includes('1: 第一章 风起'));
    assert.ok(read?.output.includes('3: 他走进雨里。'));

    const found = await executeActionTool({ name: 'file_search', arguments: { path: 'novel.txt', pattern: '第.章' } }, context);
    assert.ok(found?.output.includes('1: 第一章 风起'));
    assert.ok(found?.output.includes('4: 第二章 灯火'));
  } finally {
    await cleanup();
  }
});

test('file tools refuse paths outside the workspace', async () => {
  const { context, cleanup } = await makeActionContext();
  try {
    const result = await executeActionTool({ name: 'file_read', arguments: { path: '../../etc/passwd' } }, context);
    assert.equal(result?.ok, false);
    assert.ok(result?.output.includes('越界'));
  } finally {
    await cleanup();
  }
});

test('send_file_excerpt delivers verbatim lines as separate messages and persists them', async () => {
  const { context, repo, sent, cleanup } = await makeActionContext();
  try {
    await executeActionTool(
      { name: 'file_write', arguments: { path: 'novel.txt', content: '他走进雨里，没有回头。\n\n灯火在远处亮了。' } },
      context,
    );
    const result = await executeActionTool(
      { name: 'send_file_excerpt', arguments: { path: 'novel.txt', from: 1, to: 3 } },
      context,
    );
    assert.ok(result?.ok);
    assert.equal(result?.sentToUser, true);
    // blank line skipped, two verbatim lines sent as two messages
    assert.deepEqual(sent, ['他走进雨里，没有回头。', '灯火在远处亮了。']);
    const history = repo.getMessages('private', '123');
    const excerpts = history.filter((entry) => entry.source_label === 'file-excerpt');
    assert.equal(excerpts.length, 2);
    assert.ok(excerpts.every((entry) => entry.message_ref !== undefined));
  } finally {
    await cleanup();
  }
});

test('web_fetch refuses the bot host internal services', async () => {
  const { context, cleanup } = await makeActionContext();
  try {
    const result = await executeActionTool({ name: 'web_fetch', arguments: { url: 'http://127.0.0.1:7822/get_status' } }, context);
    assert.equal(result?.ok, false);
    assert.ok(result?.output.includes('内网'));
  } finally {
    await cleanup();
  }
});

test('network budget caps calls within one turn', async () => {
  const { context, cleanup } = await makeActionContext();
  try {
    context.networkBudget.used = 8;
    const result = await executeActionTool({ name: 'web_fetch', arguments: { url: 'https://8.8.8.8/' } }, context);
    assert.equal(result?.ok, false);
    assert.ok(result?.output.includes('上限'));
  } finally {
    await cleanup();
  }
});

test('extract_archive via tool unpacks a zip written into the workspace', async () => {
  const { context, cleanup } = await makeActionContext();
  try {
    const zip = buildZip([{ name: 'a/b.txt', data: Buffer.from('解出来的'), method: 8 }]);
    const abs = context.workspace.resolvePath('pack.zip');
    await context.workspace.ensure();
    await writeFile(abs, zip);
    const result = await executeActionTool({ name: 'extract_archive', arguments: { path: 'pack.zip' } }, context);
    assert.ok(result?.ok);
    assert.ok(result?.output.includes('pack/a/b.txt'));
    const content = await readFile(context.workspace.resolvePath('pack/a/b.txt'), 'utf-8');
    assert.equal(content, '解出来的');
  } finally {
    await cleanup();
  }
});
