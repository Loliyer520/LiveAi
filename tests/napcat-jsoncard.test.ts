/**
 * QQ 小程序分享卡（json 段 / CQ:json 字符串两种形态）必须转成 AI 看得懂的
 * 文字，B 站卡片要带标题和 b23 链接——2026-11-03 前这类消息 text 为空，
 * AI 完全看不见用户分享了什么。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildChatMessage } from '../src/bot/napcat.js';

const BILI_CARD = JSON.stringify({
  ver: '1.0.0.19',
  prompt: '[QQ小程序]大模型坦克动荡 Astra vs Fable',
  app: 'com.tencent.miniapp_01',
  meta: {
    detail_1: {
      appid: '1109937557',
      title: '哔哩哔哩',
      desc: '大模型坦克动荡 Astra vs Fable',
      qqdocurl: 'https://b23.tv/TCNlCWg?share_medium=android',
    },
  },
});

function baseEvent(message: unknown, rawMessage: string): Record<string, unknown> {
  return {
    message_type: 'private',
    sub_type: 'friend',
    user_id: 1059681169,
    self_id: 3592217365,
    message_id: 1,
    time: 1793721179,
    sender: { user_id: 1059681169, nickname: '冰糖' },
    message,
    raw_message: rawMessage,
  };
}

test('json 段 B 站分享卡：text 带标题和 b23 链接', () => {
  const msg = buildChatMessage(
    baseEvent([{ type: 'json', data: { data: BILI_CARD } }], '[CQ:json,data=...]'),
    'private',
    1059681169,
  );
  assert.ok(msg.text.includes('[分享B站视频]'), msg.text);
  assert.ok(msg.text.includes('大模型坦克动荡 Astra vs Fable'), msg.text);
  assert.ok(msg.text.includes('https://b23.tv/TCNlCWg'), msg.text);
});

test('json 段 B 站卡：标题+文字混排时文字保留', () => {
  const msg = buildChatMessage(
    baseEvent(
      [{ type: 'text', data: { text: '看这个' } }, { type: 'json', data: { data: BILI_CARD } }],
      '看这个[CQ:json,data=...]',
    ),
    'private',
    1059681169,
  );
  assert.ok(msg.text.startsWith('看这个'), msg.text);
  assert.ok(msg.text.includes('[分享B站视频]'), msg.text);
});

test('字符串形态 CQ:json（实体转义）也能解出卡片', () => {
  const escaped = BILI_CARD.replace(/,/g, '&#44;').replace(/\[/g, '&#91;').replace(/\]/g, '&#93;');
  const msg = buildChatMessage(baseEvent('[CQ:json,data=' + escaped + ']', '[CQ:json,data=' + escaped + ']'), 'private', 1059681169);
  assert.ok(msg.text.includes('[分享B站视频]'), msg.text);
  assert.ok(msg.text.includes('b23.tv/TCNlCWg'), msg.text);
});

test('非 B 站 json 卡：给卡片摘要而不是空白', () => {
  const card = JSON.stringify({ prompt: '[QQ小程序]某个应用', meta: { detail_1: { title: '某应用', desc: '内容' } } });
  const msg = buildChatMessage(
    baseEvent([{ type: 'json', data: { data: card } }], ''),
    'private',
    1059681169,
  );
  assert.ok(msg.text.includes('某个应用'), msg.text);
});

test('坏 json 段不挡消息', () => {
  const msg = buildChatMessage(
    baseEvent([{ type: 'json', data: { data: '{oops' } }], ''),
    'private',
    1059681169,
  );
  assert.equal(msg.text, '');
});
