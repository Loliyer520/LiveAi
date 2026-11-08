/**
 * Turn checkpoint: persists in-progress turn state to disk so crash recovery
 * can resume with tool results and history intact. Checkpoint files are keyed
 * by scope and auto-pruned on successful turn completion.
 */

import { mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { HistoryEntry, ModelMessage } from '../chat/types.js';

export interface TurnCheckpoint {
  scopeKey: string;
  /** Timestamp when this checkpoint was written. */
  at: number;
  /** History entries accumulated before this turn started. */
  historyBeforeTrigger: HistoryEntry[];
  /** Tool round extension (assistant + tool_result messages). */
  extension: ModelMessage[];
  /** Executed tools log (for crash-recovery note). */
  executedTools: string[];
  /** Last tool round number. */
  toolRound: number;
}

export class TurnCheckpointStore {
  constructor(private readonly checkpointDir: string) {
    if (!existsSync(checkpointDir)) {
      try {
        mkdirSync(checkpointDir, { recursive: true });
      } catch (error) {
        console.warn(`[checkpoint] failed to create dir ${checkpointDir}:`, error);
      }
    }
  }

  /**
   * Writes a checkpoint for the given scope. Overwrites any existing checkpoint
   * for that scope (only the latest matters).
   */
  async write(checkpoint: TurnCheckpoint): Promise<void> {
    const filename = this.filenameFor(checkpoint.scopeKey);
    const path = join(this.checkpointDir, filename);
    try {
      await writeFile(path, JSON.stringify(checkpoint, null, 2), 'utf8');
    } catch (error) {
      console.warn(`[checkpoint] write failed scope=${checkpoint.scopeKey}:`, error);
    }
  }

  /**
   * Reads the checkpoint for a scope, if one exists. Returns null if none found
   * or if the checkpoint is stale (older than 10 minutes, which means the process
   * hung or the checkpoint is a leftover from a much older crash).
   */
  async read(scopeKey: string): Promise<TurnCheckpoint | null> {
    const filename = this.filenameFor(scopeKey);
    const path = join(this.checkpointDir, filename);
    try {
      const text = await readFile(path, 'utf8');
      const checkpoint = JSON.parse(text) as TurnCheckpoint;
      const age = Date.now() - checkpoint.at;
      const MAX_AGE_MS = 10 * 60 * 1000; // 10 minutes
      if (age > MAX_AGE_MS) {
        console.warn(`[checkpoint] stale checkpoint scope=${scopeKey} age=${Math.round(age / 1000)}s, ignoring`);
        await this.remove(scopeKey); // Clean up
        return null;
      }
      return checkpoint;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        console.warn(`[checkpoint] read failed scope=${scopeKey}:`, error);
      }
      return null;
    }
  }

  /**
   * Removes the checkpoint for a scope (called on successful turn completion).
   */
  async remove(scopeKey: string): Promise<void> {
    const filename = this.filenameFor(scopeKey);
    const path = join(this.checkpointDir, filename);
    try {
      await rm(path, { force: true });
    } catch (error) {
      // Ignore ENOENT
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        console.warn(`[checkpoint] remove failed scope=${scopeKey}:`, error);
      }
    }
  }

  /**
   * Prunes all checkpoints older than 10 minutes (leftover from long-dead processes).
   */
  async pruneStale(): Promise<void> {
    try {
      const files = await readdir(this.checkpointDir);
      const now = Date.now();
      const MAX_AGE_MS = 10 * 60 * 1000;
      for (const file of files) {
        if (!file.endsWith('.json')) continue;
        const path = join(this.checkpointDir, file);
        try {
          const text = await readFile(path, 'utf8');
          const checkpoint = JSON.parse(text) as TurnCheckpoint;
          if (now - checkpoint.at > MAX_AGE_MS) {
            await rm(path, { force: true });
          }
        } catch {
          // Ignore read/parse errors
        }
      }
    } catch (error) {
      console.warn('[checkpoint] pruneStale failed:', error);
    }
  }

  private filenameFor(scopeKey: string): string {
    // Sanitize scope key for filesystem (replace : with _)
    return `${scopeKey.replace(/:/g, '_')}.json`;
  }
}
