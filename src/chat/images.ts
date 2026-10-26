/**
 * Image intake — ported from the legacy runtime's image pipeline:
 *
 *  NapCat delivers images either as OneBot array segments
 *  (rawData.message = [{type:'image', data:{url, file}}]) or inline CQ codes
 *  in raw_message (`[CQ:image,file=...,url=...]`). Text extraction drops both,
 *  so the URLs are recovered here and stashed on the history/trigger entries
 *  (`image_refs`) — the model then views them on demand via the view_image
 *  tool instead of burning vision tokens on every message.
 */

import type { ChatMessage } from './types.js';
import { assertPublicUrl, fetchGuarded, readCapped } from './net.js';

const CQ_IMAGE_PATTERN = /\[CQ:image,([^\]]*)\]/g;

/** Collect image URLs from an inbound message (array segments + CQ codes). */
export function extractImageRefs(message: ChatMessage): string[] {
  const refs: string[] = [];

  const segments = (message.rawData as { message?: unknown }).message;
  if (Array.isArray(segments)) {
    for (const segment of segments) {
      if (typeof segment !== 'object' || segment === null) continue;
      const typed = segment as { type?: unknown; data?: { url?: unknown; file?: unknown } };
      if (typed.type !== 'image') continue;
      const url = pickImageUrl(typed.data?.url, typed.data?.file);
      if (url) refs.push(url);
    }
  }

  const raw = String(message.rawMessage ?? '');
  for (const match of raw.matchAll(CQ_IMAGE_PATTERN)) {
    const params = parseCqParams(match[1]);
    const url = pickImageUrl(params.url, params.file);
    if (url) refs.push(url);
  }

  return [...new Set(refs)];
}

function pickImageUrl(url: unknown, file: unknown): string | null {
  const asUrl = String(url ?? '').trim();
  if (/^https?:\/\//i.test(asUrl)) return asUrl;
  const asFile = String(file ?? '').trim();
  if (/^https?:\/\//i.test(asFile)) return asFile;
  return null;
}

/** `file=a,url=b,summary=[...]` → {file, url, …}; CQ escaping undone. */
function parseCqParams(body: string): Record<string, string> {
  const params: Record<string, string> = {};
  for (const pair of body.split(',')) {
    const eq = pair.indexOf('=');
    if (eq <= 0) continue;
    const key = pair.slice(0, eq).trim();
    const value = pair
      .slice(eq + 1)
      .replace(/&#44;/g, ',')
      .replace(/&#91;/g, '[')
      .replace(/&#93;/g, ']')
      .replace(/&amp;/g, '&');
    params[key] = value;
  }
  return params;
}

const MAX_EMBED_BYTES = 6 * 1024 * 1024;

/**
 * Download a remote image and return it as a `data:<mime>;base64,…` URL.
 * Vision providers generally refuse to fetch remote URLs themselves (and QQ
 * CDN images need browser-ish headers), so the legacy runtime embedded them
 * locally before sending — same semantics here. data:/non-http inputs pass
 * through untouched for the caller's protocol to handle.
 */
export async function embedImageAsDataUrl(url: string): Promise<string> {
  if (url.startsWith('data:') || !/^https?:\/\//i.test(url)) return url;
  // QQ CDN 是公网地址，过 SSRF 守卫不影响正常用图；重定向逐跳复查，读体走硬上限。
  const checked = await assertPublicUrl(url);
  const response = await fetchGuarded(checked, {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://im.qq.com/' },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`图片下载失败 HTTP ${response.status}`);
  const buffer = await readCapped(response, MAX_EMBED_BYTES);
  const mime = (response.headers.get('content-type') ?? '').split(';', 1)[0].trim() || 'image/jpeg';
  if (!mime.startsWith('image/')) throw new Error(`不是图片内容: ${mime}`);
  return `data:${mime};base64,${buffer.toString('base64')}`;
}
