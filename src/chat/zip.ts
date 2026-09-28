/**
 * Minimal archive extraction with zero dependencies (node:zlib only):
 *  - ZIP: EOCD scan → central directory → per-entry local header; stored and
 *    deflated entries only;
 *  - GZ: single-file gunzip.
 * Guards against zip-slip, entry-count bombs, and decompression bombs.
 */

import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join, normalize, sep } from 'node:path';
import { gunzipSync, inflateRawSync } from 'node:zlib';

const EOCD_SIG = 0x06054b50;
const CENTRAL_SIG = 0x02014b50;
const LOCAL_SIG = 0x04034b50;

const MAX_ENTRIES = 200;
const MAX_TOTAL_BYTES = 64 * 1024 * 1024;

export function isZip(buffer: Buffer): boolean {
  return buffer.length >= 4 && buffer.readUInt32LE(0) === LOCAL_SIG;
}

export function isGzip(buffer: Buffer): boolean {
  return buffer.length >= 2 && buffer[0] === 0x1f && buffer[1] === 0x8b;
}

/** Refuse absolute paths, drive letters, and parent traversal. */
function safeEntryName(name: string): string {
  const normalized = normalize(name.replace(/\\/g, '/'));
  if (
    normalized.startsWith('/') ||
    /^[a-zA-Z]:/.test(normalized) ||
    normalized.split('/').some((part) => part === '..')
  ) {
    throw new Error(`压缩包内含越界路径: ${name}`);
  }
  return normalized;
}

/** Extract a zip buffer into destDir; returns the relative paths written. */
export async function extractZip(buffer: Buffer, destDir: string): Promise<string[]> {
  // EOCD lives in the last ≤64KB (max comment length 65535 + 22-byte record).
  const scanStart = Math.max(0, buffer.length - 65557);
  let eocd = -1;
  for (let index = buffer.length - 22; index >= scanStart; index -= 1) {
    if (buffer.readUInt32LE(index) === EOCD_SIG) {
      eocd = index;
      break;
    }
  }
  if (eocd < 0) throw new Error('不是有效的 ZIP 文件（找不到目录结尾记录）');
  const entryCount = buffer.readUInt16LE(eocd + 10);
  const cdOffset = buffer.readUInt32LE(eocd + 16);
  if (entryCount > MAX_ENTRIES) throw new Error(`条目太多（${entryCount} > ${MAX_ENTRIES}）`);

  const written: string[] = [];
  let totalBytes = 0;
  let cursor = cdOffset;
  for (let entryIndex = 0; entryIndex < entryCount; entryIndex += 1) {
    if (cursor + 46 > buffer.length || buffer.readUInt32LE(cursor) !== CENTRAL_SIG) {
      throw new Error('ZIP 中央目录损坏');
    }
    const method = buffer.readUInt16LE(cursor + 10);
    const compressedSize = buffer.readUInt32LE(cursor + 20);
    const uncompressedSize = buffer.readUInt32LE(cursor + 24);
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const localOffset = buffer.readUInt32LE(cursor + 42);
    const rawName = buffer.subarray(cursor + 46, cursor + 46 + nameLength).toString('utf-8');
    cursor += 46 + nameLength + extraLength + commentLength;

    const name = safeEntryName(rawName);
    if (name.endsWith('/')) continue; // directory marker

    totalBytes += uncompressedSize;
    if (totalBytes > MAX_TOTAL_BYTES) throw new Error('解压后总量超过 64MB 上限');
    if (method !== 0 && method !== 8) throw new Error(`不支持的压缩方式 ${method}（条目 ${name}）`);
    if (localOffset + 30 > buffer.length || buffer.readUInt32LE(localOffset) !== LOCAL_SIG) {
      throw new Error(`条目 ${name} 的本地头损坏`);
    }
    const localNameLength = buffer.readUInt16LE(localOffset + 26);
    const localExtraLength = buffer.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    const compressed = buffer.subarray(dataStart, dataStart + compressedSize);
    const content = method === 0 ? Buffer.from(compressed) : inflateRawSync(compressed);
    if (content.byteLength > MAX_TOTAL_BYTES) throw new Error(`条目 ${name} 解压后过大`);

    const target = join(destDir, name);
    if (!target.startsWith(destDir + sep)) throw new Error(`压缩包内含越界路径: ${name}`);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, content);
    written.push(name);
  }
  return written;
}

/** Gunzip a single-file .gz buffer → write to destPath (name sans .gz). */
export async function extractGzip(buffer: Buffer, destPath: string): Promise<number> {
  const content = gunzipSync(buffer);
  if (content.byteLength > MAX_TOTAL_BYTES) throw new Error('解压后超过 64MB 上限');
  await mkdir(dirname(destPath), { recursive: true });
  await writeFile(destPath, content);
  return content.byteLength;
}
