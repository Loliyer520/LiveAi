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
 *
 * ── 声明式注册（ROADMAP P0）─────────────────────────────────────────────
 * 领域有两层来源：
 *   1. 代码内置（下方 CODE_DOMAINS，含 builtinTools——工具实现只能在代码里）；
 *   2. data/domains/<id>.json 声明式覆盖/新增：
 *      { "title": "小说中枢", "description": "…", "child_types": ["novel"] }
 *      JSON 不能携带 builtinTools（那需要代码），但足够注册一个由插件工具
 *      驱动的新领域：改 title/description/childTypes、挂汇报路由，零代码。
 * 加新 JSON + initDomainRegistry 重扫即可，与插件 manifest 同范式。
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
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
  /** 下级 scope 类型（汇报路由用，如 mcclient/bili）。 */
  childTypes: string[];
  /** true = 来自 data/domains/*.json 而非代码内置（面板标记用）。 */
  custom?: boolean;
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

/** 代码内置领域（builtinTools 只能在这里声明——工具实现是代码）。 */
const CODE_DOMAINS: DomainDef[] = [
  {
    id: 'onebot',
    title: 'Onebot 中枢',
    description:
      'QQ 领域主级 AI：承接全部 QQ 下级会话（群/私聊子AI）的上报，聚合消化后上报最高级中枢；'
      + '跨会话调度与委派由中枢执行。',
    builtinTools: [],
    childTypes: ['group', 'private'],
  },
  {
    id: 'mc',
    title: 'Minecraft 中枢',
    description: 'MC 领域主级 AI：持有全部 mc_* 工具与 mc_view，管理 MC client 下级 AI，向中枢汇报。',
    builtinTools: [MC_VIEW_TOOL],
    childTypes: ['mcclient'],
  },
  {
    id: 'bili',
    title: '哔哩中枢',
    description: 'B 站领域主级 AI：持有 bili_* 工具，管理每视频下级 AI（bili:<bvid> scope），向中枢汇报。',
    builtinTools: [],
    childTypes: ['bili'],
  },
];

const DOMAIN_ID_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;

/** 注册表状态：内置领域 + JSON 声明的覆盖/新增。 */
let registry: DomainDef[] = CODE_DOMAINS.map((def) => ({ ...def, childTypes: [...def.childTypes] }));
let registryErrors: { file: string; error: string }[] = [];

/** (Re)scan data/domains/*.json; called at startup（与插件 manifest 同范式）。 */
export function loadDomains(dir: string): DomainDef[] {
  const merged = new Map(CODE_DOMAINS.map((def) => [def.id, { ...def, childTypes: [...def.childTypes] }]));
  registryErrors = [];
  if (existsSync(dir)) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.endsWith('.json') || entry.name.startsWith('_') || entry.name.startsWith('.')) continue;
      const file = entry.name;
      let raw: unknown;
      try {
        raw = JSON.parse(readFileSync(join(dir, file), 'utf-8'));
      } catch (error) {
        registryErrors.push({ file, error: `不是合法 JSON: ${error instanceof Error ? error.message : String(error)}` });
        continue;
      }
      const id = file.slice(0, -'.json'.length);
      if (!DOMAIN_ID_PATTERN.test(id)) {
        registryErrors.push({ file, error: `领域 id 须匹配 ${DOMAIN_ID_PATTERN}（文件名即 id）` });
        continue;
      }
      const record = (raw ?? {}) as Record<string, unknown>;
      const title = typeof record.title === 'string' && record.title.trim() !== '' ? record.title.trim() : null;
      const description = typeof record.description === 'string' ? record.description : '';
      const childTypes = Array.isArray(record.child_types)
        ? record.child_types.map((value) => String(value).trim()).filter((value) => /^[\w-]{1,32}$/.test(value))
        : null;
      if (title === null && description === '' && childTypes === null) {
        registryErrors.push({ file, error: '至少要声明 title / description / child_types 之一' });
        continue;
      }
      const base = merged.get(id) ?? { id, title: id, description: '', builtinTools: [], childTypes: [], custom: true };
      merged.set(id, {
        ...base,
        title: title ?? base.title,
        description: description !== '' ? description : base.description,
        childTypes: childTypes ?? base.childTypes,
        custom: !CODE_DOMAINS.some((def) => def.id === id),
      });
    }
  }
  registry = [...merged.values()].sort((a, b) => a.id.localeCompare(b.id));
  const custom = registry.filter((def) => def.custom).length;
  console.info(`[domains] ${registry.length} domain(s) loaded${custom > 0 ? ` (${custom} from data/domains)` : ''}`);
  for (const err of registryErrors) console.error(`[domains] ${err.file}: ${err.error}`);
  return registry;
}

/** 当前生效的领域清单（含 JSON 声明的；未 loadDomains 时只有代码内置）。 */
export function domainDefs(): DomainDef[] {
  return registry;
}

/** data/domains 的解析错误（面板/排障用）。 */
export function domainLoadErrors(): { file: string; error: string }[] {
  return registryErrors;
}

export function domainDefForScope(scopeKey: string): DomainDef | null {
  if (!scopeKey.startsWith('domain:')) return null;
  const id = scopeKey.slice('domain:'.length);
  return registry.find((def) => def.id === id) ?? null;
}

/** 内置工具是否对某 scope 可见（domain 专属工具用）。 */
export function domainScopeHasBuiltin(scopeKey: string, toolName: string): boolean {
  const def = domainDefForScope(scopeKey);
  return def !== null && def.builtinTools.some((tool) => tool.name === toolName);
}

/**
 * 下级 scope → 领域中枢的路由表（三层架构汇报链）。
 * 由各领域声明的 childTypes 推导（group/private 归 Onebot 中枢；
 * mcclient 归 Minecraft 中枢；bili 归哔哩中枢）。domain:* 与未知类型
 * 直报最高级中枢（null）。同一 childType 被多个领域声明时，后注册的领域
 * 覆盖（记录在案）——保持确定性。
 */
function hubRouting(): Record<string, string> {
  const routing: Record<string, string> = {};
  for (const def of registry) {
    for (const type of def.childTypes) routing[type] = `domain:${def.id}`;
  }
  return routing;
}

/**
 * 某 scope 的汇报应送达哪个中枢 scopeKey；返回 null = 直报最高级中枢（主AI）。
 * 主AI 自己的 scope（private:masterQq）永远直报自身之外——它不发汇报。
 */
export function hubForScope(scopeKey: string, masterScopeKey: string): string | null {
  if (scopeKey === masterScopeKey) return null;
  const separator = scopeKey.indexOf(':');
  const scopeType = separator === -1 ? scopeKey : scopeKey.slice(0, separator);
  return hubRouting()[scopeType] ?? null;
}
