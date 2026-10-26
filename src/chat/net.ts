/**
 * Network primitives for autonomous actions, with SSRF guardrails:
 *
 *  - only http(s) URLs, no credentials-in-URL;
 *  - loopback / private / link-local hosts are refused (literal IPs and DNS
 *    results alike — the bot host also runs internal services on localhost);
 *  - redirects are followed manually (max 3) so each hop is re-checked;
 *  - response bodies are read with a hard byte cap.
 *
 * web_search is a DuckDuckGo HTML scrape (no API key needed); the provider is
 * deliberately a tiny function so a SearXNG backend can be swapped in later.
 */

import { lookup } from 'node:dns/promises';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const MAX_REDIRECTS = 3;
const DEFAULT_TIMEOUT_MS = 20_000;

/** 提取 IPv4-mapped IPv6（::ffff:a.b.c.d / ::ffff:7f00:1 / 全展开形式）里的 v4 地址，非映射返回 null。 */
function mappedV4Address(ip: string): string | null {
  let addr = ip.toLowerCase();
  const quad = /(\d{1,3}(?:\.\d{1,3}){3})$/.exec(addr);
  if (quad !== null) {
    const nums = quad[1].split('.').map(Number);
    const hi = ((nums[0]! << 8) | nums[1]!).toString(16);
    const lo = ((nums[2]! << 8) | nums[3]!).toString(16);
    addr = addr.slice(0, addr.length - quad[1].length) + hi + ':' + lo;
  }
  const groups = addr.split(':');
  let full: string[];
  const emptyAt = groups.findIndex((group) => group === '');
  if (emptyAt >= 0) {
    const head = groups.slice(0, emptyAt);
    const tail = groups.slice(emptyAt + 1).filter((group) => group !== '');
    const missing = 8 - head.length - tail.length;
    if (missing < 0) return null;
    full = [...head, ...Array<string>(missing).fill('0'), ...tail];
  } else {
    full = groups;
  }
  if (full.length !== 8) return null;
  if (!full.slice(0, 5).every((group) => parseInt(group, 16) === 0)) return null;
  if (parseInt(full[5]!, 16) !== 0xffff) return null;
  const hi = parseInt(full[6]!, 16);
  const lo = parseInt(full[7]!, 16);
  if (Number.isNaN(hi) || Number.isNaN(lo)) return null;
  return `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
}

export function isPrivateAddress(address: string, family: number): boolean {
  if (family === 6) {
    const ip = address.toLowerCase();
    const mapped = mappedV4Address(ip);
    if (mapped !== null) return isPrivateAddress(mapped, 4);
    return ip === '::1' || ip.startsWith('fe80:') || ip.startsWith('fc') || ip.startsWith('fd') || ip === '::';
  }
  const parts = address.split('.').map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return true;
  const [a, b] = parts;
  return (
    a === 0 || a === 10 || a === 127 ||
    (a === 172 && b! >= 16 && b! <= 31) ||
    (a === 192 && b === 168) ||
    (a === 169 && b === 254) ||
    a >= 224 // multicast / reserved
  );
}

/** Parse + basic-validate a URL: http(s) only, no credentials. No address check. */
function parseHttpUrl(rawUrl: string): URL {
  let url: URL;
  try {
    url = new URL(String(rawUrl ?? '').trim());
  } catch {
    throw new Error('URL 无法解析');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('只支持 http(s) 链接');
  if (url.username || url.password) throw new Error('不支持带凭据的 URL');
  return url;
}

/** Parse + validate a URL, refusing internal destinations. Throws on refusal. */
export async function assertPublicUrl(rawUrl: string): Promise<URL> {
  const url = parseHttpUrl(rawUrl);
  const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  if (hostname === 'localhost' || hostname.endsWith('.localhost') || hostname.endsWith('.internal') || hostname.endsWith('.local')) {
    throw new Error('禁止访问内网地址');
  }
  const literalFamily = hostname.includes(':') || /^\d+\.\d+\.\d+\.\d+$/.test(hostname) ? (hostname.includes(':') ? 6 : 4) : 0;
  if (literalFamily !== 0) {
    if (isPrivateAddress(hostname, literalFamily)) throw new Error('禁止访问内网地址');
    return url;
  }
  let addresses: { address: string; family: number }[];
  try {
    addresses = await lookup(hostname, { all: true });
  } catch {
    throw new Error(`域名解析失败: ${hostname}`);
  }
  if (addresses.length === 0 || addresses.some((entry) => isPrivateAddress(entry.address, entry.family))) {
    throw new Error('禁止访问内网地址');
  }
  return url;
}

export async function fetchGuarded(url: URL, init: RequestInit, redirectsLeft = MAX_REDIRECTS, allowInternal = false): Promise<Response> {
  const response = await fetch(url, {
    ...init,
    redirect: 'manual',
    signal: init.signal ?? AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
  });
  if (response.status >= 300 && response.status < 400) {
    const location = response.headers.get('location');
    if (location && redirectsLeft > 0) {
      const next = allowInternal
        ? parseHttpUrl(new URL(location, url).toString())
        : await assertPublicUrl(new URL(location, url).toString());
      return fetchGuarded(next, { ...init, signal: init.signal }, redirectsLeft - 1, allowInternal);
    }
  }
  return response;
}

/** Read a response body with a hard byte cap (declared length is pre-checked, then enforced while streaming). */
export async function readCapped(response: Response, maxBytes: number): Promise<Buffer> {
  const declared = Number(response.headers.get('content-length') ?? 0);
  if (declared > maxBytes) throw new Error(`响应太大（${Math.round(declared / 1024)}KB > ${Math.round(maxBytes / 1024)}KB 上限）`);
  const reader = response.body?.getReader();
  if (!reader) return Buffer.alloc(0);
  const chunks: Buffer[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel().catch(() => undefined);
      throw new Error(`响应超过 ${Math.round(maxBytes / 1024)}KB 上限`);
    }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks);
}

export interface WebFetchResult {
  status: number;
  contentType: string;
  body: string;
  truncated: boolean;
}

/** GET/POST a URL and return text, capped. The model's generic HTTP tool.
 *  `allowInternal` skips the SSRF address check — only for URLs declared in
 *  master-controlled plugin manifests (`allow_internal: true`), never for
 *  model-supplied URLs. */
export async function webFetch(
  rawUrl: string,
  options: { method?: string; headers?: Record<string, string>; body?: string; maxBytes?: number; displayLimit?: number; allowInternal?: boolean } = {},
): Promise<WebFetchResult> {
  const url = options.allowInternal ? parseHttpUrl(rawUrl) : await assertPublicUrl(rawUrl);
  const maxBytes = options.maxBytes ?? 512 * 1024;
  const method = (options.method ?? 'GET').toUpperCase();
  if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'].includes(method)) throw new Error(`不支持的方法: ${method}`);
  const headers: Record<string, string> = { 'user-agent': 'Mozilla/5.0 (DeepLive bot)', ...options.headers };
  const response = await fetchGuarded(url, {
    method,
    headers,
    body: options.body === undefined || method === 'GET' || method === 'HEAD' ? undefined : options.body,
  }, MAX_REDIRECTS, options.allowInternal === true);
  const contentType = (response.headers.get('content-type') ?? '').split(';', 1)[0].trim();
  const buffer = await readCapped(response, maxBytes);
  const text = buffer.toString('utf-8');
  // Display truncation is for human/model consumption; callers that must
  // PARSE the body (plugins) pass a displayLimit ≥ maxBytes to get it whole.
  const limit = options.displayLimit ?? 8000;
  return {
    status: response.status,
    contentType,
    body: text.length > limit ? text.slice(0, limit) : text,
    truncated: text.length > limit,
  };
}

/** Download a URL to a local path with a byte cap. Returns bytes written. */
export async function downloadToFile(rawUrl: string, destPath: string, maxBytes: number, options: { allowInternal?: boolean; timeoutMs?: number; headers?: Record<string, string> } = {}): Promise<{ bytes: number; contentType: string }> {
  const url = options.allowInternal ? parseHttpUrl(rawUrl) : await assertPublicUrl(rawUrl);
  const response = await fetchGuarded(url, {
    method: 'GET',
    headers: { 'user-agent': 'Mozilla/5.0 (DeepLive bot)', ...options.headers },
    ...(options.timeoutMs !== undefined ? { signal: AbortSignal.timeout(options.timeoutMs) } : {}),
  }, MAX_REDIRECTS, options.allowInternal === true);
  if (!response.ok) throw new Error(`下载失败 HTTP ${response.status}`);
  const contentType = (response.headers.get('content-type') ?? '').split(';', 1)[0].trim();
  const buffer = await readCapped(response, maxBytes);
  await mkdir(dirname(destPath), { recursive: true });
  await writeFile(destPath, buffer);
  return { bytes: buffer.byteLength, contentType };
}

// ── DuckDuckGo HTML search ──────────────────────────────────────────────────

export interface SearchHit {
  title: string;
  url: string;
  snippet: string;
}

const RESULT_LINK = /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
const RESULT_SNIPPET = /<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;

function stripTags(html: string): string {
  return html
    .replace(/<[^>]+>/g, '')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Parse DDG HTML results; exported for tests. */
export function parseSearchHtml(html: string, maxResults: number): SearchHit[] {
  const links = [...html.matchAll(RESULT_LINK)];
  const snippets = [...html.matchAll(RESULT_SNIPPET)];
  const hits: SearchHit[] = [];
  for (const [index, link] of links.entries()) {
    if (hits.length >= maxResults) break;
    let url = link[1] ?? '';
    // DDG wraps targets: //duckduckgo.com/l/?uddg=<encoded>&rut=…
    const uddg = /[?&]uddg=([^&]+)/.exec(url);
    if (uddg) url = decodeURIComponent(uddg[1]!);
    else if (url.startsWith('//')) url = `https:${url}`;
    if (!/^https?:\/\//i.test(url)) continue;
    hits.push({
      title: stripTags(link[2] ?? ''),
      url,
      snippet: stripTags(snippets[index]?.[1] ?? ''),
    });
  }
  return hits;
}

export async function webSearch(query: string, maxResults = 5): Promise<SearchHit[]> {
  const url = await assertPublicUrl(`https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`);
  const response = await fetchGuarded(url, {
    method: 'GET',
    headers: { 'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36' },
  });
  if (!response.ok) throw new Error(`搜索请求失败 HTTP ${response.status}`);
  const buffer = await readCapped(response, 512 * 1024);
  return parseSearchHtml(buffer.toString('utf-8'), maxResults);
}
