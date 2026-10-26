/**
 * 领域主级 AI（domain scopes）——三层架构里的「主级层」实体。
 *
 * 每个领域中枢是一个虚拟 scope（scopeKey = `domain:<id>`，如 domain:mc）：
 * 它没有 QQ 对话窗口，只经由主AI delegate_to_child('domain', '<id>', …)
 * 或主级互聊 domain_message 注入任务运转；它的 send_message 经 orchestrator
 * 改道 relay 回中枢（见 orchestrator 的 sendTextOverride），汇报走 notify_master。
 *
 * 领域下级 AI（bili:/mcclient: 虚拟 scope，每 B 站视频 / 每 MC client 一个）
 * 由对应中枢经中枢委派建立，汇报经 HUB_ROUTING 先送到领域中枢消化，
 * 再由领域中枢汇总上报最高级中枢（见 hubForScope）。
 *
 * 工具面：插件工具可用 manifest 里的 `scopes: ["domain:mc", …]` 声明可见
 * scope（"master" 是占位符=主AI scope；裸类型名如 "bili"/"mcclient" = 该类型
 * 全部 scope）；内置工具经 builtinTools 挂进对应领域
 * （如 mc_view 只挂 domain:mc，主AI 收敛后不再持有 MC 工具）。
 *
 * 提示词：data/prompt/domain/<id>.txt（PromptStore.domainPrompts）。
 */

import type { ToolSchema } from '../models/types.js';

export interface DomainDef {
  /** scope id（scopeKey = domain:<id>）。 */
  id: string;
  /** 人读名。 */
  title: string;
  /** 一句话职责（面板/文档用）。 */
  description: string;
  /** 该领域专属的内置工具（在 CHILD 工具集之上追加）。 */
  builtinTools: ToolSchema[];
}

const MC_VIEW_TOOL: ToolSchema = {
  name: 'mc_view',
  description:
    '看真实 Minecraft 世界：边车把周围渲染成「俯视高度图 + 朝向剖面图」交给视觉模型。'
    + '想知道眼前地形/矿/生物方位、决定去哪挖、确认有没有怪时用；前提是已用 mc_join 进服。'
    + '实体坐标和聊天请用 mc_status / mc_events，这里是「看」。',
  parameters: {
    type: 'object',
    properties: {
      question: { type: 'string', description: '可选，想让视觉模型重点看什么' },
      radius: { type: 'number', description: '俯视半径（格），默认 20，范围 8-32' },
    },
  },
};

export const DOMAIN_DEFS: DomainDef[] = [
  {
    id: 'onebot',
    title: 'Onebot 中枢',
    description:
      'QQ 领域主级 AI：承接全部 QQ 下级会话（群/私聊子AI）的上报，聚合消化后上报最高级中枢；'
      + '跨会话调度与委派由中枢执行。',
    builtinTools: [],
  },
  {
    id: 'mc',
    title: 'Minecraft 中枢',
    description: 'MC 领域主级 AI：持有全部 mc_* 工具与 mc_view，管理 MC client 下级 AI，向中枢汇报。',
    builtinTools: [MC_VIEW_TOOL],
  },
  {
    id: 'bili',
    title: '哔哩中枢',
    description: 'B 站领域主级 AI：持有 bili_* 工具，管理每视频下级 AI（bili:<bvid> scope），向中枢汇报。',
    builtinTools: [],
  },
];

export function domainDefForScope(scopeKey: string): DomainDef | null {
  if (!scopeKey.startsWith('domain:')) return null;
  const id = scopeKey.slice('domain:'.length);
  return DOMAIN_DEFS.find((def) => def.id === id) ?? null;
}

/** 内置工具是否对某 scope 可见（domain 专属工具用）。 */
export function domainScopeHasBuiltin(scopeKey: string, toolName: string): boolean {
  const def = domainDefForScope(scopeKey);
  return def !== null && def.builtinTools.some((tool) => tool.name === toolName);
}

/**
 * 下级 scope → 领域中枢的路由表（三层架构汇报链）。
 * group/private（QQ 会话）归 Onebot 中枢；mcclient 归 Minecraft 中枢；
 * bili 归哔哩中枢。domain:* 与未知类型直报最高级中枢（null）。
 */
const HUB_ROUTING: Record<string, string> = {
  group: 'domain:onebot',
  private: 'domain:onebot',
  mcclient: 'domain:mc',
  bili: 'domain:bili',
};

/**
 * 某 scope 的汇报应送达哪个中枢 scopeKey；返回 null = 直报最高级中枢（主AI）。
 * 主AI 自己的 scope（private:masterQq）永远直报自身之外——它不发汇报。
 */
export function hubForScope(scopeKey: string, masterScopeKey: string): string | null {
  if (scopeKey === masterScopeKey) return null;
  const separator = scopeKey.indexOf(':');
  const scopeType = separator === -1 ? scopeKey : scopeKey.slice(0, separator);
  return HUB_ROUTING[scopeType] ?? null;
}
