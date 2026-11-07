/**
 * 人设参考图库：面板上传的「AI 自己长什么样」参考图，供生图/改图工具以
 * persona-ref://<文件名> 引用（画自己时保持形象一致）。
 *
 * 存放 data/plugins/image/refs/（跟着 image 插件走——插件停用后参考图也就没用了），
 * 仅图片后缀，单张 ≤8MB。
 */

import { existsSync, readdirSync, readFileSync, statSync, writeFileSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const IMAGE_EXT = /\.(png|jpe?g|webp|gif)$/i;
const MAX_REF_BYTES = 8 * 1024 * 1024;

let refsDir = '';

/** 启动时由 chat-module 注入目录（data/prompt/refs）。 */
export function configurePersonaRefs(dir: string): void {
  refsDir = dir;
}

function safeName(raw: string): string | null {
  const name = String(raw ?? '').trim().replace(/^\/+/, '');
  if (name === '' || name.includes('..') || name.includes('/') || name.includes('\\')) return null;
  if (!IMAGE_EXT.test(name)) return null;
  return name;
}

function ensureDir(): void {
  if (refsDir !== '' && !existsSync(refsDir)) mkdirSync(refsDir, { recursive: true });
}

export interface PersonaRefInfo {
  name: string;
  bytes: number;
  savedAt: number;
}

export function listPersonaRefs(): PersonaRefInfo[] {
  if (refsDir === '' || !existsSync(refsDir)) return [];
  return readdirSync(refsDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && IMAGE_EXT.test(entry.name))
    .map((entry) => {
      // 目录里可能有正在写的临时文件：stat 失败的跳过即可，不该炸面板
      try {
        const s = statSync(join(refsDir, entry.name));
        return { name: entry.name, bytes: s.size, savedAt: Math.round(s.mtimeMs) };
      } catch {
        return { name: entry.name, bytes: 0, savedAt: 0 };
      }
    })
    .filter((ref) => ref.bytes > 0)
    .sort((a, b) => b.savedAt - a.savedAt);
}

export function savePersonaRef(name: string, base64Data: string): PersonaRefInfo {
  const safe = safeName(name);
  if (safe === null) throw new Error('文件名不合法（只支持 png/jpg/webp/gif）');
  const data = Buffer.from(String(base64Data ?? ''), 'base64');
  if (data.length === 0) throw new Error('图片数据为空');
  if (data.length > MAX_REF_BYTES) throw new Error('图片超过 8MB 上限');
  ensureDir();
  const full = join(refsDir, safe);
  writeFileSync(full, data);
  return { name: safe, bytes: data.length, savedAt: Date.now() };
}

export function deletePersonaRef(name: string): boolean {
  const safe = safeName(name);
  if (safe === null || refsDir === '' || !existsSync(join(refsDir, safe))) return false;
  unlinkSync(join(refsDir, safe));
  return true;
}

/** 面板预览用：读原图字节。 */
export function readPersonaRef(name: string): { data: Buffer; contentType: string } | null {
  const safe = safeName(name);
  if (safe === null || refsDir === '' || !existsSync(join(refsDir, safe))) return null;
  const ext = (IMAGE_EXT.exec(safe)?.[1] ?? 'png').toLowerCase();
  const contentType = ext === 'png' ? 'image/png' : ext === 'gif' ? 'image/gif' : ext === 'webp' ? 'image/webp' : 'image/jpeg';
  return { data: readFileSync(join(refsDir, safe)), contentType };
}

const PERSONA_REF_SCHEME = 'persona-ref://';

export function isPersonaRefUrl(value: string): boolean {
  return value.startsWith(PERSONA_REF_SCHEME);
}

/** 生图工具用：persona-ref://<名字> → data:<mime>;base64,<…>。找不到返回 null。 */
export function personaRefDataUri(value: string): string | null {
  if (!isPersonaRefUrl(value)) return null;
  const ref = readPersonaRef(value.slice(PERSONA_REF_SCHEME.length));
  if (ref === null) return null;
  return `data:${ref.contentType};base64,${ref.data.toString('base64')}`;
}
