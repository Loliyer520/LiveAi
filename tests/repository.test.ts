/** ScopeRepository 持久化行为：损坏文件备份、同 scope 写入顺序。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ScopeRepository } from '../src/store/repository.js';

function entry(n: number) {
  return { user_id: 100, nickname: 'user100', text: `消息 ${n}`, timestamp: 1_700_000_000 + n, message_id: n };
}

test('corrupt scope JSON is backed up to .bak instead of silently overwritten', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-repo-'));
  try {
    const stateDir = join(dir, 'state');
    const repo = new ScopeRepository(stateDir, 500);
    // 先有正常状态落盘
    repo.appendMessage('group', '9', entry(1));
    await repo.flush();

    // 手动写坏
    const scopeFile = join(stateDir, 'group_9.json');
    await writeFile(scopeFile, '{not json!!!', 'utf-8');

    // 新实例读它：应该备份损坏文件并以全新状态启动
    const repo2 = new ScopeRepository(stateDir, 500);
    repo2.appendMessage('group', '9', entry(2));
    await repo2.flush();

    const files = await readdir(stateDir);
    const backups = files.filter((name) => name.startsWith('group_9.json.bak-'));
    assert.equal(backups.length, 1, '损坏文件必须留 .bak');
    const backupRaw = await readFile(join(stateDir, backups[0]), 'utf-8');
    assert.equal(backupRaw, '{not json!!!', '备份保留原始损坏内容');

    const fresh = JSON.parse(await readFile(scopeFile, 'utf-8')) as { messages: unknown[] };
    assert.equal(fresh.messages.length, 1, '新状态从零开始而不是崩掉');
  } finally {
    await rm(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  }
});

test('rapid persists land on disk in call order (last write wins)', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'liveai-repo-'));
  try {
    const repo = new ScopeRepository(join(dir, 'state'), 500);
    for (let index = 0; index < 30; index += 1) {
      repo.appendMessage('private', '7', entry(index));
    }
    await repo.flush();
    const raw = JSON.parse(await readFile(join(dir, 'state', 'private_7.json'), 'utf-8')) as {
      messages: { message_id: number }[];
    };
    assert.equal(raw.messages.length, 30);
    assert.equal(raw.messages[raw.messages.length - 1].message_id, 29, '磁盘必须是最后一次 persist 的状态');
  } finally {
    await rm(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  }
});
