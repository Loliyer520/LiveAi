/**
 * Lightweight per-scope turn and tool-call logging.
 *
 * Ported from legacy core/ai_repository.py:
 *   - tool_logs: every tool call the model made (name + args preview + result
 *     preview), capped at 500 entries per scope, stored as JSONL.
 *   - turn_logs: one summary per completed turn (trigger text, tools called,
 *     whether something was sent), capped at 20 per scope.
 *
 * Both live at data/state/turn-logs/<scopeType>_<scopeId>.jsonl.
 * The file is append-only with a rotate-on-cap rewrite. All I/O is
 * fire-and-forget — a logging failure never blocks a turn.
 */

import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export interface ToolLogEntry {
  at: number; // epoch ms
  name: string;
  /** First 200 chars of JSON-serialized arguments. */
  argsPreview: string;
  /** First 300 chars of the result output. */
  resultPreview: string;
  ok: boolean;
  durationMs: number;
}

export interface TurnLogEntry {
  at: number; // epoch ms
  scopeKey: string;
  /** Text of the first trigger message, truncated at 240 chars. */
  triggerPreview: string;
  /** Tool names called this turn (in order). */
  toolsUsed: string[];
  sent: boolean;
  durationMs: number;
}

const MAX_TOOL_LOGS = 500;
const MAX_TURN_LOGS = 20;

export class TurnLogStore {
  private readonly dir: string;
  /** Per-scope write chains: preserves append order. */
  private readonly chains = new Map<string, Promise<void>>();

  constructor(dir: string) {
    this.dir = dir;
  }

  appendToolLog(scopeType: string, scopeId: string, entry: ToolLogEntry): void {
    const key = `tool_${scopeType}_${scopeId}`;
    this.enqueue(key, async () => {
      const path = join(this.dir, `${key}.jsonl`);
      await mkdir(this.dir, { recursive: true });
      const line = JSON.stringify(entry) + '\n';
      await appendFile(path, line, 'utf-8');
      await this.rotateCap(path, MAX_TOOL_LOGS);
    });
  }

  appendTurnLog(scopeType: string, scopeId: string, entry: TurnLogEntry): void {
    const key = `turn_${scopeType}_${scopeId}`;
    this.enqueue(key, async () => {
      const path = join(this.dir, `${key}.jsonl`);
      await mkdir(this.dir, { recursive: true });
      const line = JSON.stringify(entry) + '\n';
      await appendFile(path, line, 'utf-8');
      await this.rotateCap(path, MAX_TURN_LOGS);
    });
  }

  async readToolLogs(scopeType: string, scopeId: string, limit = 50): Promise<ToolLogEntry[]> {
    return this.readJsonl<ToolLogEntry>(join(this.dir, `tool_${scopeType}_${scopeId}.jsonl`), limit);
  }

  async readTurnLogs(scopeType: string, scopeId: string, limit = 20): Promise<TurnLogEntry[]> {
    return this.readJsonl<TurnLogEntry>(join(this.dir, `turn_${scopeType}_${scopeId}.jsonl`), limit);
  }

  // ── helpers ────────────────────────────────────────────────────────────────

  private enqueue(key: string, fn: () => Promise<void>): void {
    const chain = (this.chains.get(key) ?? Promise.resolve())
      .then(fn)
      .catch((error) => {
        console.error(`[turn-log] write failed key=${key}:`, error);
      });
    this.chains.set(key, chain);
  }

  /**
   * If the file grows past `cap` lines, rewrite it keeping the newest `cap`
   * lines. Called after every append; the rewrite is O(cap) not O(N), so it
   * stays fast even on a long-running instance.
   */
  private async rotateCap(path: string, cap: number): Promise<void> {
    let raw: string;
    try {
      raw = await readFile(path, 'utf-8');
    } catch {
      return;
    }
    const lines = raw.split('\n').filter((line) => line.trim().length > 0);
    if (lines.length <= cap) return;
    const kept = lines.slice(lines.length - cap);
    await writeFile(path, kept.join('\n') + '\n', 'utf-8');
  }

  private async readJsonl<T>(path: string, limit: number): Promise<T[]> {
    let raw: string;
    try {
      raw = await readFile(path, 'utf-8');
    } catch {
      return [];
    }
    const lines = raw.split('\n').filter((line) => line.trim().length > 0);
    const result: T[] = [];
    for (const line of lines.slice(-limit)) {
      try {
        result.push(JSON.parse(line) as T);
      } catch {
        // skip corrupt line
      }
    }
    return result;
  }

  /** Wait for all in-flight writes (tests, shutdown). */
  async flush(): Promise<void> {
    await Promise.allSettled([...this.chains.values()]);
  }
}
