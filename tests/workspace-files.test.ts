/** 长期工作区工具：file_save（url / 消息图缓存两来源）+ file_delete。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { Workspace } from '../src/chat/workspace.js';
import { executeChatTool, type ToolContext } from '../src/chat/tools.js';
import { configureImageCache } from '../src/chat/image-cache.js';
import { ScopeRepository } from '../src/store/repository.js';
import type { NapcatBot } from '../src/bot/napcat.js';

async function makeContext() {
  const dir = await mkdtemp(join(tmpdir(), 'ws-files-'));
  const repo = new ScopeRepository(join(dir, 'state'));
  const bot = {
    selfId: 987654322,
    async sendText() {
      return { message_id: 1 };
    },
  } as unknown as NapcatBot;
  const context: ToolContext = {
    bot,
    repo,
    archive: null as never,
    relations: null as never,
    tasks: null as never,
    masterQq: 123456789,
    isMaster: false,
    notifyMaster: () => undefined,
    delegateToChild: () => '',
    sendToScope: async () => '',
    peerInject: () => undefined,
    workspace: new Workspace(join(dir, 'workspace')),
    networkBudget: { used: 0 },
    triggerImageRefs: [],
    triggerVideoRefs: [],
    describeImage: async () => '',
    scopeType: 'private',
    scopeId: '123',
    executedTools: [],
  };
  return {
    context,
    repo,
    cleanup: async () => {
      await repo.flush();
      await rm(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
    },
  };
}

test('file_save 从消息图（本地缓存命中）存进工作区并自动补扩展名', async () => {
  const { context, repo, cleanup } = await makeContext();
  const cacheDir = await mkdtemp(join(tmpdir(), 'imgcache-'));
  configureImageCache(cacheDir);
  try {
    const url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=TESTIMG';
    // 手工伪造一份新鲜缓存（与 image-cache 的 keyFor/paths 同算法）
    const key = createHash('sha256').update(url, 'utf8').digest('hex').slice(0, 20);
    const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.alloc(200, 7)]);
    await writeFile(join(cacheDir, `${key}.bin`), png);
    await writeFile(join(cacheDir, `${key}.mime`), 'image/png');
    await writeFile(join(cacheDir, `${key}.ts`), String(Date.now()));

    const ref = repo.registerMessageRef('private', '123', 42);
    repo.appendMessage('private', '123', {
      user_id: 123,
      nickname: 'u',
      text: '[图片×1]',
      raw_message: '[图片×1]',
      message_id: 42,
      message_ref: ref,
      timestamp: Date.now() / 1000,
      image_refs: [url],
    });
    await repo.flush();

    const result = await executeChatTool(
      { name: 'file_save', arguments: { message_ref: ref, index: 1, filename: 'images/晚霞' } },
      context,
    );
    assert.equal(result.ok, true, result.output);
    assert.ok(result.output.includes('images/晚霞.png'), result.output);
    const saved = await readFile(join(context.workspace.root, 'images', '晚霞.png'));
    assert.deepEqual(saved, png);
  } finally {
    configureImageCache('');
    await cleanup();
    await rm(cacheDir, { recursive: true, force: true });
  }
});

test('file_save 拒绝路径穿越和缺来源', async () => {
  const { context, cleanup } = await makeContext();
  try {
    const evil = await executeChatTool(
      { name: 'file_save', arguments: { url: 'https://example.com/a.png', filename: '../evil.png' } },
      context,
    );
    assert.equal(evil.ok, false);
    assert.ok(evil.output.includes('越界'), evil.output);

    const noSource = await executeChatTool({ name: 'file_save', arguments: { filename: 'x.png' } }, context);
    assert.equal(noSource.ok, false);
    assert.ok(noSource.output.includes('二选一'), noSource.output);
  } finally {
    await cleanup();
  }
});

test('file_delete 删除与缺文件报错', async () => {
  const { context, cleanup } = await makeContext();
  try {
    await context.workspace.ensure();
    await writeFile(join(context.workspace.root, 'a.txt'), 'hello');
    const gone = await executeChatTool({ name: 'file_delete', arguments: { filename: 'a.txt' } }, context);
    assert.equal(gone.ok, true, gone.output);
    const again = await executeChatTool({ name: 'file_delete', arguments: { filename: 'a.txt' } }, context);
    assert.equal(again.ok, false);
    const evil = await executeChatTool({ name: 'file_delete', arguments: { filename: '../../etc/passwd' } }, context);
    assert.equal(evil.ok, false);
  } finally {
    await cleanup();
  }
});
