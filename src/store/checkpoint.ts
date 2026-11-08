/**
 * Turn checkpoints — evidence of in-flight turns for hard-crash recovery.
 *
 * The orchestrator's crash guard (persistInterruptNote) only runs when a turn
 * throws. If the process itself dies mid-turn (kill, OOM, power loss) the
 * catch never runs, and the next turn has no idea which side effects already
 * happened — so it may resend or redo them. Each tool round therefore writes
 * a tiny per-scope checkpoint; a normal turn end deletes it. Whatever survives
 * to the next boot belongs to a turn that died, and is turned into an
 * interrupt note before any new turn starts.
 *
 * Writes are synchronous on purpose: they are a few hundred bytes, and an
 * async write racing the end-of-turn delete could resurrect a finished turn.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export interface TurnCheckpoint {
  scopeKey: string;
  scopeType: string;
  scopeId: string;
  /** Epoch ms of the latest write. */
  at: number;
  /** Tools that really executed before the crash (side-effect inventory). */
  executedTools: string[];
  toolRound: number;
  triggerPreview: string;
}

/** Older leftovers are noise, not evidence (e.g. a data dir copied around). */
const MAX_RECOVER_AGE_MS = 24 * 60 * 60 * 1000;

export class TurnCheckpointStore {
  constructor(
    private readonly dir: string,
    private readonly now: () => number = Date.now,
  ) {}

  write(checkpoint: TurnCheckpoint): void {
    try {
      if (!existsSync(this.dir)) mkdirSync(this.dir, { recursive: true });
      writeFileSync(this.pathFor(checkpoint.scopeKey), JSON.stringify(checkpoint), 'utf8');
    } catch (error) {
      console.warn(`[checkpoint] write failed scope=${checkpoint.scopeKey}:`, error);
    }
  }

  read(scopeKey: string): TurnCheckpoint | null {
    return parseCheckpoint(this.pathFor(scopeKey));
  }

  remove(scopeKey: string): void {
    try {
      rmSync(this.pathFor(scopeKey), { force: true });
    } catch (error) {
      console.warn(`[checkpoint] remove failed scope=${scopeKey}:`, error);
    }
  }

  /** Reads and deletes every checkpoint; returns the recent, well-formed ones. */
  takeAll(): TurnCheckpoint[] {
    let files: string[];
    try {
      files = readdirSync(this.dir).filter((file) => file.endsWith('.json'));
    } catch {
      return []; // no dir yet = nothing ever crashed
    }
    const recovered: TurnCheckpoint[] = [];
    for (const file of files) {
      const path = join(this.dir, file);
      const checkpoint = parseCheckpoint(path);
      try {
        rmSync(path, { force: true });
      } catch {
        // best-effort
      }
      if (checkpoint !== null && this.now() - checkpoint.at <= MAX_RECOVER_AGE_MS) recovered.push(checkpoint);
    }
    return recovered;
  }

  private pathFor(scopeKey: string): string {
    return join(this.dir, `${encodeURIComponent(scopeKey)}.json`);
  }
}

function parseCheckpoint(path: string): TurnCheckpoint | null {
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8')) as Partial<TurnCheckpoint>;
    if (typeof raw.scopeKey !== 'string' || typeof raw.scopeType !== 'string' || typeof raw.scopeId !== 'string') return null;
    if (typeof raw.at !== 'number' || !Array.isArray(raw.executedTools)) return null;
    return {
      scopeKey: raw.scopeKey,
      scopeType: raw.scopeType,
      scopeId: raw.scopeId,
      at: raw.at,
      executedTools: raw.executedTools.map(String),
      toolRound: Number(raw.toolRound ?? 0),
      triggerPreview: String(raw.triggerPreview ?? ''),
    };
  } catch {
    return null;
  }
}
