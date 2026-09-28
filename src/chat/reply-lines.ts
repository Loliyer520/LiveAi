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
  return splitLongReplyLines(text)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}
