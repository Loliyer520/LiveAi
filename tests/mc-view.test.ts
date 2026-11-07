/** mc_view：真实 Minecraft 边车视觉工具的 schema 门控（MC 中枢 domain:mc 专属）。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { chatToolSchemas } from '../src/chat/tools.js';

test('mc_view 仅 Minecraft 中枢（domain:mc）可见', () => {
  const master = chatToolSchemas('private:241898129', true).map((schema) => schema.name);
  const mcHub = chatToolSchemas('domain:mc', false).map((schema) => schema.name);
  const child = chatToolSchemas('group:1', false).map((schema) => schema.name);
  assert.ok(!master.includes('mc_view'), '主AI 已收敛，不应持有 mc_view');
  assert.ok(mcHub.includes('mc_view'), 'MC 中枢 schema 缺 mc_view');
  assert.ok(!child.includes('mc_view'), '普通子AI 不应看到 mc_view');
  // 边车插件的工具由插件框架 scopes 白名单保证（见 plugins.test.ts），这里只断言命名约定没被内置占用
  assert.ok(!mcHub.includes('mc_join'), 'mc_join 属于 mc 插件而非内置工具');
});
