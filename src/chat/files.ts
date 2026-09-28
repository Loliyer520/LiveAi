/**
 * Inbound file intake — mirrors images.ts. Text extraction drops OneBot file
 * segments, so file metadata (name, url, file_id) is recovered here and
 * stashed on history entries (`file_refs`); the model downloads them on
 * demand via the download_qq_file tool.
 */

import type { ChatMessage } from './types.js';

export interface FileRef {
  name: string;
  url?: string;
  fileId?: string;
  size?: number;
}

const CQ_FILE_PATTERN = /\[CQ:file,([^\]]*)\]/g;

/** Collect file refs from an inbound message (array segments + CQ codes). */
export function extractFileRefs(message: ChatMessage): FileRef[] {
  const refs: FileRef[] = [];
  const seen = new Set<string>();

  const push = (ref: FileRef | null): void => {
    if (!ref || !ref.name) return;
    const key = `${ref.name}:${ref.fileId ?? ref.url ?? ''}`;
    if (seen.has(key)) return;
    seen.add(key);
    refs.push(ref);
  };

  const segments = (message.rawData as { message?: unknown }).message;
  if (Array.isArray(segments)) {
    for (const segment of segments) {
      if (typeof segment !== 'object' || segment === null) continue;
      const typed = segment as {
        type?: unknown;
        data?: { type?: unknown; name?: unknown; file?: unknown; file_id?: unknown; url?: unknown; file_size?: unknown; size?: unknown };
      };
      if (typed.type !== 'file') continue;
      const data = typed.data ?? {};
      const name = String(data.name ?? data.file ?? '').trim();
      push({
        name: name || '未命名文件',
        url: httpOrUndefined(data.url),
        fileId: stringOrUndefined(data.file_id),
        size: numberOrUndefined(data.file_size ?? data.size),
      });
    }
  }

  const raw = String(message.rawMessage ?? '');
  for (const match of raw.matchAll(CQ_FILE_PATTERN)) {
    const params = parseCqParams(match[1]!);
    push({
      name: String(params.name ?? params.file ?? '未命名文件'),
      url: httpOrUndefined(params.url),
      fileId: stringOrUndefined(params.file_id),
      size: numberOrUndefined(params.size),
    });
  }

  return refs;
}

function httpOrUndefined(value: unknown): string | undefined {
  const text = String(value ?? '').trim();
  return /^https?:\/\//i.test(text) ? text : undefined;
}

function stringOrUndefined(value: unknown): string | undefined {
  const text = String(value ?? '').trim();
  return text || undefined;
}

function numberOrUndefined(value: unknown): number | undefined {
  const num = Number(value);
  return Number.isFinite(num) && num > 0 ? num : undefined;
}

function parseCqParams(body: string): Record<string, string> {
  const params: Record<string, string> = {};
  for (const pair of body.split(',')) {
    const eq = pair.indexOf('=');
    if (eq <= 0) continue;
    params[pair.slice(0, eq).trim()] = pair
      .slice(eq + 1)
      .replace(/&#44;/g, ',')
      .replace(/&#91;/g, '[')
      .replace(/&#93;/g, ']')
      .replace(/&amp;/g, '&');
  }
  return params;
}
