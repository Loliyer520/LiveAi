/**
 * 入站图片本地缓存：QQ 图链（multimedia.nt.qq.com.cn 等）带时效 rkey，几分钟到
 * 几小时就失效——模型事后用短ID 回看/编辑历史图时链接早已 400。消息进站时就把
 * 图抓下来存本地（带 QQ 官方客户端同款 Referer 头），之后所有取图路径优先吃缓存，
 * 链接过期也能出图。TTL 24h，超龄即弃；磁盘超限整库清空重来。
 */

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, statSync, mkdirSync, readFileSync } from "node:fs";
import { mkdir, readFile, writeFile, rm } from "node:fs/promises";
import { join } from 'node:path';
import { downloadToFile } from './net.js';

const TTL_MS = 24 * 60 * 60 * 1000;
const MAX_TOTAL_BYTES = 512 * 1024 * 1024;
const MAX_SINGLE_BYTES = 12 * 1024 * 1024;

let cacheDir = '';
const inflight = new Map<string, Promise<boolean>>();

export function configureImageCache(dir: string): void {
  cacheDir = dir;
}

/** QQ CDN 对陌生 UA/缺 Referer 的请求回 400（image_edit 实锤、view_image 同链同图带 im.qq.com Referer 就 200）——取图统一走这套头。 */
export function qqImageHeaders(url: string): Record<string, string> {
  let host = '';
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return {};
  }
  const qqHosts = ['qq.com', 'qq.cn', 'qq.com.cn', 'gtimg.cn', 'qlogo.cn', 'qpic.cn'];
  if (qqHosts.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) {
    return { 'User-Agent': 'Mozilla/5.0', Referer: 'https://im.qq.com/' };
  }
  return {};
}

function keyFor(url: string): string {
  return createHash('sha256').update(url, 'utf8').digest('hex').slice(0, 20);
}

function paths(url: string): { data: string; mime: string; time: string } {
  const base = join(cacheDir, keyFor(url));
  return { data: `${base}.bin`, mime: `${base}.mime`, time: `${base}.ts` };
}

function fresh(url: string): boolean {
  if (cacheDir === '' || !existsSync(paths(url).data)) return false;
  try {
    // 读时间戳文件内容（mtime 会因 .mime/.ts 回写等操作漂移，不可信）。
    const savedAt = Number(readFileSync(paths(url).time, 'utf-8').trim());
    return Number.isFinite(savedAt) && Date.now() - savedAt < TTL_MS;
  } catch {
    return false;
  }
}

/**
 * 站外 CDN 下载头（file_save / download_file / send_video 共用）：B 站要
 * Referer+Range（裸 GET+浏览器 UA 会被风控 567，2026-11-04 实测加 Range 即 206），
 * QQ 图链要官方客户端同款 Referer。都不沾返回 undefined。
 */
export function cdnDownloadHeaders(url: string): Record<string, string> | undefined {
  if (/bilivideo|akamaized\.net|hdslb\.com/i.test(url)) {
    return {
      referer: 'https://www.bilibili.com/',
      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
      range: 'bytes=0-',
    };
  }
  const qq = qqImageHeaders(url);
  return Object.keys(qq).length > 0 ? qq : undefined;
}

/** 命中且新鲜时返回本地缓存文件路径与 MIME（只读勿改）；未命中/过期 null。file_save 用它把入站图拷进长期工作区。 */
export function cachedImageFileInfo(url: string): { path: string; contentType: string } | null {
  if (!fresh(url)) return null;
  const p = paths(url);
  try {
    const contentType = readFileSync(p.mime, 'utf-8').trim() || 'image/png';
    if (!contentType.startsWith('image/')) return null;
    return { path: p.data, contentType };
  } catch {
    return null;
  }
}

/** 取缓存：命中返回 data URI，未命中/过期返回 null（调用方回落原 URL）。 */
export async function cachedImageUri(url: string): Promise<string | null> {  if (cacheDir === '' || !fresh(url)) return null;
  const p = paths(url);
  try {
    const [buffer, mime] = await Promise.all([readFile(p.data), readFile(p.mime, 'utf-8')]);
    const contentType = mime.trim() || 'image/png';
    if (!contentType.startsWith('image/')) return null;
    return `data:${contentType};base64,${buffer.toString('base64')}`;
  } catch {
    return null;
  }
}

/** 进站预抓：失败静默（普通回看仍可回落原 URL）。 */
export function cacheImage(url: string): void {
  void ensureImageCached(url).then((ok) => {
    if (ok) return;
    let host = '(invalid URL)';
    try { host = new URL(url).hostname; } catch { /* 保持诊断日志不中断收图 */ }
    console.warn(`[imgcache] inbound prefetch failed host=${host} key=${keyFor(url)}`);
  });
}

/**
 * 定时发图等需要确认原图已落盘的场景：共享进站预抓，下载完成才返回。
 * 只有 fresh 的本地副本才算成功，调用方不能在 false 时承诺“重启不丢”。
 */
export function ensureImageCached(url: string): Promise<boolean> {
  if (cacheDir === '' || !/^https?:\/\//i.test(url)) return Promise.resolve(false);
  if (fresh(url)) return Promise.resolve(true);
  const pending = inflight.get(url);
  if (pending) return pending;
  const task = (async (): Promise<boolean> => {
    const p = paths(url);
    await mkdir(cacheDir, { recursive: true });
    const temp = join(cacheDir, `.tmp-${keyFor(url)}`);
    try {
      const { contentType } = await downloadToFile(url, temp, MAX_SINGLE_BYTES, { timeoutMs: 30_000, headers: qqImageHeaders(url) });
      const { renameSync } = await import('node:fs');
      renameSync(temp, p.data);
      await writeFile(p.mime, contentType.startsWith('image/') ? contentType : 'image/png', 'utf-8');
      await writeFile(p.time, String(Date.now()), 'utf-8');
      await enforceQuota();
      return fresh(url);
    } catch {
      await rm(temp, { force: true }).catch(() => {});
      return false;
    } finally {
      inflight.delete(url);
    }
  })();
  inflight.set(url, task);
  return task;
}

async function enforceQuota(): Promise<void> {
  if (cacheDir === '' || !existsSync(cacheDir)) return;
  let entries;
  try {
    entries = readdirSync(cacheDir).filter((name) => name.endsWith('.bin'));
  } catch {
    return;
  }
  let total = 0;
  const sized: { name: string; bytes: number; mtime: number }[] = [];
  for (const name of entries) {
    try {
      const s = statSync(join(cacheDir, name));
      total += s.size;
      sized.push({ name, bytes: s.size, mtime: s.mtimeMs });
    } catch { /* 正被并发清理的条目，跳过 */ }
  }
  if (total <= MAX_TOTAL_BYTES) return;
  sized.sort((a, b) => a.mtime - b.mtime);
  for (const entry of sized) {
    if (total <= MAX_TOTAL_BYTES) break;
    const base = join(cacheDir, entry.name.replace(/\.bin$/, ''));
    await Promise.all([rm(`${base}.bin`, { force: true }), rm(`${base}.mime`, { force: true }), rm(`${base}.ts`, { force: true })]).catch(() => {});
    total -= entry.bytes;
  }
}
