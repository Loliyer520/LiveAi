/**
 * AI 看视频：用 ffmpeg 抽指定时刻的帧（可多帧），竖向贴成一张长图，
 * 每帧左上角烧录 t=<秒>s 水印（贴图后 AI 才知道每格对应哪个时刻），
 * 交给 vision 角色多模态模型看。视频本身不喂模型（太大），只看帧。
 */

import { execFile } from 'node:child_process';
import { mkdir, rm } from 'node:fs/promises';
import { join } from 'node:path';

const FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf';
const FRAME_WIDTH = 480;

function run(bin: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: 120_000 }, (error, _stdout, stderr) => {
      if (error !== null) reject(new Error(`${bin} 失败: ${String(stderr).slice(-300) || error.message}`));
      else resolve();
    });
  });
}

/** 视频时长（秒，ffprobe）；探测失败返回 null。 */
export async function videoDuration(videoAbs: string): Promise<number | null> {
  return new Promise((resolve) => {
    execFile('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', videoAbs], { timeout: 30_000 }, (error, stdout) => {
      const value = error === null ? Number(String(stdout).trim()) : Number.NaN;
      resolve(Number.isFinite(value) && value > 0 ? value : null);
    });
  });
}

/**
 * 抽 moments（秒，已去重排序）各一帧并竖贴成 jpg。返回长图绝对路径。
 * 帧间自然相邻不加分隔条——左上角的时间戳水印就是格子边界。
 */
export async function extractFrameTiled(videoAbs: string, moments: number[], workDir: string): Promise<string> {
  await rm(workDir, { recursive: true, force: true }).catch(() => {});
  await mkdir(workDir, { recursive: true });
  const frames: string[] = [];
  for (const [index, t] of moments.entries()) {
    const frame = join(workDir, `f${index}.png`);
    // drawtext 的 text 里 '=' 要转义；scale 宽 480 高自适应偶数（yuv420p 要求）
    await run('ffmpeg', [
      '-y', '-loglevel', 'error',
      '-ss', String(t), '-i', videoAbs,
      '-frames:v', '1',
      '-vf', `scale=${FRAME_WIDTH}:-2,drawtext=fontfile=${FONT}:text='t\\=${Math.round(t)}s':x=8:y=8:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6`,
      frame,
    ]);
    frames.push(frame);
  }
  const tiled = join(workDir, 'tiled.jpg');
  if (frames.length === 1) {
    await run('ffmpeg', ['-y', '-loglevel', 'error', '-i', frames[0], '-q:v', '4', tiled]);
  } else {
    const inputs = frames.flatMap((f) => ['-i', f]);
    const chain = `${frames.map((_, i) => `[${i}:v]`).join('')}vstack=inputs=${frames.length}`;
    await run('ffmpeg', ['-y', '-loglevel', 'error', ...inputs, '-filter_complex', chain, '-q:v', '4', tiled]);
  }
  return tiled;
}
