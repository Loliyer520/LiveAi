/** Replace lone UTF-16 surrogates while preserving valid supplementary characters. */
export function sanitizeUnicode(text: string): string {
  return text.replace(
    /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g,
    '�',
  );
}

/** UTF-16 length limit that cannot persist half of an emoji/supplementary character. */
export function truncateUnicode(text: string, maxChars: number): string {
  const sliced = text.slice(0, maxChars);
  const last = sliced.charCodeAt(sliced.length - 1);
  return sanitizeUnicode(last >= 0xD800 && last <= 0xDBFF ? sliced.slice(0, -1) : sliced);
}
