/**
 * Layered prompt file store. Ported from legacy core/prompt_store.py.
 *
 * Layout under data/prompt/:
 *   char.txt           — persona (冰糖/洛天成)
 *   char_prefill.txt   — assistant's persona acknowledgement (fake exchange)
 *   chat_focus.txt     — chat-mode duties (watch group, report intel)
 *   chat_style.txt     — pre-send self check list
 *   child_rules.txt    — 25 CHILD_RULES for the session AI
 *   main.txt           — 主AI (coordinator) system prompt
 *   agent.txt          — 常驻后台 agent system prompt
 *   staff/10..50.txt   — layered session-AI system blocks, sorted concat
 *
 * staff blocks support the {{char_prompt}} placeholder.
 */

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readdir } from 'node:fs/promises';

const PROJECT_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const PROMPT_DIR = join(PROJECT_ROOT, 'data', 'prompt');

export const DEFAULT_CHAR_PROMPT = 'liveAi，由洛神赋开发的自运行数字生命项目。';

async function readText(path: string, fallback: string): Promise<string> {
  try {
    const text = (await readFile(path, 'utf-8')).trim();
    return text || fallback;
  } catch {
    return fallback;
  }
}

/** Directory-first layered read: sorted *.txt concat, else single file. */
async function readDirOrFile(dirPath: string, filePath: string, fallback: string): Promise<string> {
  try {
    const names = (await readdir(dirPath)).filter((name) => name.endsWith('.txt')).sort();
    if (names.length > 0) {
      const chunks: string[] = [];
      for (const name of names) {
        const text = (await readFile(join(dirPath, name), 'utf-8')).trim();
        if (text) chunks.push(text);
      }
      if (chunks.length > 0) return chunks.join('\n\n');
    }
  } catch {
    // fall through to single file
  }
  return readText(filePath, fallback);
}

export interface PromptBundle {
  char: string;
  charPrefill: string;
  chatFocus: string;
  chatStyle: string;
  childRules: string;
  main: string;
  agent: string;
  staffSystem: string;
}

export class PromptStore {
  private cache: PromptBundle | null = null;
  private loadedAt = 0;
  private readonly ttlMs: number;
  private readonly promptDir: string;

  constructor(promptDir: string = PROMPT_DIR, ttlMs = 30_000) {
    this.promptDir = promptDir;
    this.ttlMs = ttlMs;
  }

  async bundle(): Promise<PromptBundle> {
    const now = Date.now();
    if (this.cache !== null && now - this.loadedAt < this.ttlMs) return this.cache;
    const char = await readText(join(this.promptDir, 'char.txt'), DEFAULT_CHAR_PROMPT);
    const [charPrefill, chatFocus, chatStyle, childRules, main, agent] = await Promise.all([
      readText(join(this.promptDir, 'char_prefill.txt'), ''),
      readText(join(this.promptDir, 'chat_focus.txt'), ''),
      readText(join(this.promptDir, 'chat_style.txt'), ''),
      readText(join(this.promptDir, 'child_rules.txt'), ''),
      readText(join(this.promptDir, 'main.txt'), ''),
      readText(join(this.promptDir, 'agent.txt'), ''),
    ]);
    const staffTemplate = await readDirOrFile(
      join(this.promptDir, 'staff'),
      join(this.promptDir, 'staff.txt'),
      '',
    );
    this.cache = {
      char,
      charPrefill,
      chatFocus,
      chatStyle,
      childRules,
      main,
      agent,
      staffSystem: staffTemplate.replaceAll('{{char_prompt}}', char),
    };
    this.loadedAt = now;
    return this.cache;
  }

  invalidate(): void {
    this.cache = null;
  }
}
