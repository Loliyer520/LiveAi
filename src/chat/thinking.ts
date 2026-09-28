/**
 * <thinking> tag filtering — the legacy contract: send_message content may
 * carry a <thinking>…</thinking> block for private reasoning; everything
 * inside is stripped before the text reaches the user.
 */

const THINKING_BLOCK = /<thinking>[\s\S]*?<\/thinking>/g;
const UNCLOSED_THINKING = /<thinking>[\s\S]*$/;

export function stripThinking(content: string): string {
  let text = String(content ?? '');
  text = text.replace(THINKING_BLOCK, '');
  // Unclosed tag (model ran out of tokens): drop everything from the tag on.
  text = text.replace(UNCLOSED_THINKING, '');
  return text.trim();
}
