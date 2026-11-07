/**
 * 媒体外吐池：NapCat 反向 WS 模式下跑在号主本地电脑，看不到服务器的
 * file:// 路径——发视频/语音前先把文件拷进 spool 目录，经面板 HTTP 端口
 * 的 /media/<id>/<name> 能力路由（uuid 当鉴权，不设 token）让 NapCat 自己
 * 来拉。条目 1 小时 TTL，每次 spool 顺手清理。
 */

import { copyFile, mkdir, readdir, rm, stat } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';

const TTL_MS = 60 * 60_000;

interface SpoolConfig {
  dir: string;
  /** 对外基址（NapCat 必须能访问，如 http://189.24.69.130:7650）。 */
  publicBase: string;
}

let spool: SpoolConfig | null = null;

export function configureMediaSpool(config: SpoolConfig | null): void {
  spool = config;
}

/** 拷文件进池并返回可下载 URL；未启用（NapCat 本机同磁盘）返回 null。 */
export async function spoolToUrl(absPath: string, filename: string): Promise<string | null> {
  if (spool === null) return null;
  await mkdir(spool.dir, { recursive: true });
  void cleanup(spool.dir);
  const id = randomUUID().replace(/-/g, '').slice(0, 16);
  await copyFile(absPath, join(spool.dir, `${id}_${filename}`));
  return `${spool.publicBase}/media/${id}/${filename}`;
}

/**
 * /media/<id>/<name> 路由的文件定位：id 限 16 位十六进制（uuid 截断），
 * name 限字母数字点下划线，两个都过白名单防目录穿越。
 */
export async function resolveSpoolFile(id: string, name: string): Promise<string | null> {
  if (spool === null) return null;
  if (!/^[0-9a-f]{16}$/.test(id) || !/^[\w.-]{1,80}$/.test(name) || name.includes('..')) return null;
  const dir = spool.dir;
  let names: string[];
  try {
    names = await readdir(dir);
  } catch {
    return null;
  }
  const hit = names.find((n) => n.startsWith(`${id}_`) && n.slice(id.length + 1) === name);
  if (hit === undefined) return null;
  return join(dir, hit);
}

async function cleanup(dir: string): Promise<void> {
  try {
    const names = await readdir(dir);
    const now = Date.now();
    for (const name of names) {
      const full = join(dir, name);
      const info = await stat(full).catch(() => null);
      if (info !== null && now - info.mtimeMs > TTL_MS) {
        await rm(full, { force: true }).catch(() => {});
      }
    }
  } catch {
    // 清理失败不影响主流程
  }
}
