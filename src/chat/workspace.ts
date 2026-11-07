/**
 * Per-scope filesystem sandbox for autonomous file actions. Every file tool
 * the model can call resolves paths through here: the workspace root is the
 * only place it may read/write, and traversal outside it is rejected.
 */

import { mkdir } from 'node:fs/promises';
import { resolve, sep } from 'node:path';

export class Workspace {
  constructor(readonly root: string) {}

  async ensure(): Promise<void> {
    await mkdir(this.root, { recursive: true });
  }

  /** Resolve a model-supplied relative path inside the sandbox, or throw. */
  resolvePath(rel: string): string {
    const cleaned = String(rel ?? '')
      .trim()
      .replace(/\\/g, '/') // treat backslashes as separators on every platform
      .replace(/^\/+/, '');
    if (!cleaned) throw new Error('路径为空');
    const abs = resolve(this.root, cleaned);
    if (abs !== this.root && !abs.startsWith(this.root + sep)) {
      throw new Error('路径越界：只能访问本会话工作区内的文件');
    }
    return abs;
  }

  /** Absolute path → workspace-relative display path. */
  displayPath(abs: string): string {
    return abs.startsWith(this.root + sep) ? abs.slice(this.root.length + 1) : abs;
  }
}

/** Sanitize a download filename: basename only, no control chars / dotfiles. */
export function safeFilename(name: string, fallback = 'file'): string {
  const base = String(name ?? '').split(/[/\\]/).pop() ?? '';
  const cleaned = base
    .replace(/[\x00-\x1f"<>|?*]/g, '')
    .replace(/^\.+/, '')
    .trim();
  return cleaned || fallback;
}
