/**
 * Outbound reply line splitting — ported from legacy _split_long_reply_lines.
 * A reply is delivered as several short QQ messages ("分条"): each line of
 * the content becomes its own message, and over-long lines are re-broken at
 * Chinese punctuation into ≤36-char chunks so the bot chats like a person
 * instead of pasting walls of text.
 */

const BLANK_RUN = /\n{3,}/g;
const MAX_LINE_CHARS = 36;
const SENTENCE_BREAK = /(?<=[，。！？；])/;

/**
 * glm-5.3 sometimes emits JSON-style escapes as literal text ("\n" instead of
 * a real newline) and leaks pseudo XML tags like </content>. Undoes that
 * before splitting so QQ never shows raw escapes. Only used on model chat
 * replies — verbatim file sends bypass this on purpose.
 */
export function unescapeModelArtifacts(text: string): string {
  return String(text ?? '')
    .replace(/\\n/g, '\n')
    // deepseek-flash 关了思考也会把 <thinking> 当字面文本漏出来——整块剔除
    //（含未闭合的尾巴），免得用户看到原始标签。
    .replace(/<\s*thinking\s*>[\s\S]*?(<\s*\/\s*thinking\s*>|$)/gi, '')
    .replace(/<\/?\s*content\s*>/gi, '');
}

export function splitLongReplyLines(text: string): string {
  const normalized = String(text ?? '').replace(BLANK_RUN, '\n\n').trim();
  if (!normalized) return normalized;
  const result: string[] = [];
  for (const line of normalized.split('\n')) {
    const stripped = line.trim();
    if (!stripped) {
      if (result.length > 0 && result[result.length - 1] !== '') result.push('');
      continue;
    }
    if (stripped.length <= MAX_LINE_CHARS) {
      result.push(stripped);
      continue;
    }
    let current = '';
    for (const part of stripped.split(SENTENCE_BREAK)) {
      const piece = part.trim();
      if (!piece) continue;
      if (!current) {
        current = piece;
        continue;
      }
      if (current.length + piece.length <= MAX_LINE_CHARS) current += piece;
      else {
        result.push(current);
        current = piece;
      }
    }
    if (current) result.push(current);
  }
  return result.join('\n').trim();
}

/** Split into the per-message lines actually delivered (empty lines dropped). */
export function replyMessageLines(text: string): string[] {
  return splitLongReplyLines(unescapeModelArtifacts(text))
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}
