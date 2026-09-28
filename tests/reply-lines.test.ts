/** Line-splitting for outbound replies (ported legacy 分条 sending). */
import test from 'node:test';
import assert from 'node:assert/strict';
import { replyMessageLines, splitLongReplyLines } from '../src/chat/reply-lines.js';

test('short lines pass through untouched, one per line', () => {
  assert.equal(splitLongReplyLines('你好\n我在'), '你好\n我在');
});

test('runs of blank lines collapse to a single separator', () => {
  assert.equal(splitLongReplyLines('第一段\n\n\n\n\n第二段'), '第一段\n\n第二段');
});

test('leading/trailing whitespace is trimmed', () => {
  assert.equal(splitLongReplyLines('  你好  \n'), '你好');
});

test('lines within 36 chars are never re-broken', () => {
  const line = '甲乙丙丁戊己庚辛壬癸，子丑寅卯辰巳午未申酉。'; // 22 chars
  assert.equal(splitLongReplyLines(line), line);
});

test('long lines re-break at Chinese punctuation and repack to ≤36', () => {
  // 39 chars total → 11+11+11 pack to 33, remainder starts a new line.
  const long = '甲乙丙丁戊己庚辛壬癸，子丑寅卯辰巳午未申酉，一二三四五六七八九，甲子乙丑丙寅';
  const split = splitLongReplyLines(long);
  assert.deepEqual(split.split('\n'), [
    '甲乙丙丁戊己庚辛壬癸，子丑寅卯辰巳午未申酉，一二三四五六七八九，',
    '甲子乙丑丙寅',
  ]);
});

test('a long line with no punctuation stays whole', () => {
  const wall = 'x'.repeat(60);
  assert.equal(splitLongReplyLines(wall), wall);
});

test('replyMessageLines drops empty lines', () => {
  assert.deepEqual(replyMessageLines('第一行\n\n\n第二行\n  \n'), ['第一行', '第二行']);
});

test('replyMessageLines on empty input yields nothing to send', () => {
  assert.deepEqual(replyMessageLines('   \n \n'), []);
});
