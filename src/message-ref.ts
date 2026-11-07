/** Accept the short-ID forms models actually emit: A1B2, #A1B2, [A1B2], [#A1B2]. */
export function normalizeMessageRef(value: unknown): string {
  return String(value ?? '').trim().replace(/^(?:\[#?|#)/, '').replace(/\]$/, '').trim();
}
