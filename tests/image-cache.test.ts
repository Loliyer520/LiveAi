import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cachedImageUri, configureImageCache, qqImageHeaders } from '../src/chat/image-cache.js';
import { ScopeRepository } from '../src/store/repository.js';

test('qqImageHeaders: QQ 域名带 im.qq.com Referer，外域不带（image_edit 400 之鉴）', () => {
  assert.equal(qqImageHeaders('https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=x').Referer, 'https://im.qq.com/');
  assert.equal(qqImageHeaders('https://gchat.qpic.cn/x').Referer, 'https://im.qq.com/');
  assert.deepEqual(qqImageHeaders('https://cloudflarer2.example.com/png/a.png'), {});
});

test('cachedImageUri: 未配置/未命中返回 null，命中返回 data URI', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-imgcache-'));
  try {
    configureImageCache(dir);
    assert.equal(await cachedImageUri('https://x.example/a.png'), null);
    const base = join(dir, 'deadbeef');
    mkdirSync(dir, { recursive: true });
    writeFileSync(`${base}.bin`, Buffer.from('fakepng'));
    writeFileSync(`${base}.mime`, 'image/png');
    writeFileSync(`${base}.ts`, String(Date.now()));
    // keyFor 是 sha256 前 20 位——直接算出来对上
    const { createHash } = await import('node:crypto');
    const key = createHash('sha256').update('https://x.example/a.png', 'utf8').digest('hex').slice(0, 20);
    writeFileSync(join(dir, `${key}.bin`), Buffer.from('fakepng'));
    writeFileSync(join(dir, `${key}.mime`), 'image/png');
    writeFileSync(join(dir, `${key}.ts`), String(Date.now()));
    const uri = await cachedImageUri('https://x.example/a.png');
    assert.ok(uri !== null && uri.startsWith('data:image/png;base64,'));
    // 过期的缓存不算命中
    writeFileSync(join(dir, `${key}.ts`), String(Date.now() - 25 * 3600 * 1000));
    assert.equal(await cachedImageUri('https://x.example/a.png'), null);
  } finally {
    configureImageCache('');
    rmSync(dir, { recursive: true, force: true });
  }
});

test('recentImageRefs: 新→旧列带图消息短ID，空 ref/无图消息跳过', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-repo-'));
  try {
    const repo = new ScopeRepository(join(dir, 'state'));
    const now = Date.now() / 1000; // HistoryEntry.timestamp 是 epoch 秒
    repo.appendMessage('private', '42', {
      user_id: 1, nickname: 'a', text: 'no image', message_id: 1, message_ref: 'AAAA',
      timestamp: now - 60, source_kind: 'friend_private', source_label: '', image_refs: [], file_refs: [],
    });
    repo.appendMessage('private', '42', {
      user_id: 1, nickname: 'a', text: '', message_id: 2, message_ref: 'BBBB',
      timestamp: now - 30, source_kind: 'friend_private', source_label: '', image_refs: ['https://x/1.png'], file_refs: [],
    });
    const refs = repo.recentImageRefs('private', '42', 5);
    assert.equal(refs.length, 1);
    assert.equal(refs[0].ref, 'BBBB');
    assert.ok(refs[0].ageMinutes <= 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
