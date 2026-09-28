/**
 * Chat-mode tool schemas + executor. Ported core set from legacy
 * core/ai_tools_schema.py / _run_ai_tool_call: the send/stay_silent protocol
 * pair, recall, notes (memory), master relay, alarms.
 */

import type { ToolSchema } from '../models/types.js';
import type { NapcatBot } from '../bot/napcat.js';
import type { ScopeRepository } from '../store/repository.js';
import type { MessageArchive } from '../store/archive.js';
import type { RelationGraph } from '../relations/graph.js';
import type { TaskScheduler } from './scheduler.js';
import type { ChatMessage, HistoryEntry } from './types.js';
import { replyMessageLines } from './reply-lines.js';
import { stripThinking } from './thinking.js';
import { ACTION_TOOL_SCHEMAS, executeActionTool } from './actions.js';
import { pluginRegistry } from './plugins.js';
import type { Workspace } from './workspace.js';

export interface ToolContext {
  bot: NapcatBot;
  repo: ScopeRepository;
  /** Append-only archive backing memory_search. */
  archive: MessageArchive;
  /** Shared cross-scope relation graph. */
  relations: RelationGraph;
  /** Persisted alarm/recurring-task scheduler (timers survive restarts). */
  tasks: TaskScheduler;
  masterQq: number;
  /** True when the current turn runs in the master (主AI) scope. */
  isMaster: boolean;
  /** Called with the master-relay payload; orchestrator routes it. */
  notifyMaster: (payload: { text: string; requestType?: string; fromScope: string }) => void;
  /** Per-scope sandboxed filesystem root for the autonomous-action tools. */
  workspace: Workspace;
  /** Per-turn network call counter (web_* / download_* share the budget). */
  networkBudget: { used: number };
  /** Master only: inject an instruction turn into another scope's child AI. */
  delegateToChild: (targetScopeType: string, targetScopeId: string, instruction: string) => string;
  /** Master only: send a message directly to users in another scope. */
  sendToScope: (targetScopeType: string, targetScopeId: string, content: string) => Promise<string>;
  /** Image URLs attached to this turn's trigger messages (view_image fallback). */
  triggerImageRefs: string[];
  /** Vision-model describe call (role 'vision', falls back to main). */
  describeImage: (url: string, question: string) => Promise<string>;
  scopeType: string;
  scopeId: string;
  executedTools: string[];
}

export interface ToolResult {
  ok: boolean;
  output: string;
  /** Turn-level side effects the orchestrator must honor. */
  staySilent?: boolean;
  /** The tool delivered user-visible content (suppresses the re-prompt rescue). */
  sentToUser?: boolean;
}

export const CHAT_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'send_message',
    description:
      '给用户发送消息（唯一发言途径，普通文字不会被发送）。content 里可用 <thinking>...</thinking> 包裹思考，会自动过滤。用 \\n 分行即可分条发送：每一行作为一条独立的 QQ 短消息发出（像人连续发几条消息那样）；超长行会自动按标点断成 ≤36 字的短条。',
    parameters: {
      type: 'object',
      properties: {
        content: { type: 'string', description: '要发送的内容（thinking 标签会被过滤）' },
        reply_to_id: { type: 'string', description: '可选，要引用回复的消息短ID，如 A1B2' },
      },
      required: ['content'],
    },
  },
  {
    name: 'stay_silent',
    description: '决定本轮不说话时调用，直接结束本回合。不要把沉默理由写成普通文字。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'recall_message',
    description: '撤回自己 2 分钟内发出的消息（传消息短ID）。',
    parameters: {
      type: 'object',
      properties: { message_ref: { type: 'string', description: '消息短ID，如 A1B2' } },
      required: ['message_ref'],
    },
  },
  {
    name: 'memory_list',
    description: '列出当前会话最近的 AI 备忘（短条目）。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'memory_write',
    description: '写一条备忘（会被压缩进提示词的"最近备注"区，不发给用户）。不要用它在聊天里说话。',
    parameters: {
      type: 'object',
      properties: { text: { type: 'string', description: '备忘内容，一句话' } },
      required: ['text'],
    },
  },
  {
    name: 'notify_master',
    description:
      '联系主AI：上报情报、跨会话协调、请求高风险授权、系统级操作。request_type 常用: intel / set_user_preference / relation_add_fact / system_operation / cross_session_relay。',
    parameters: {
      type: 'object',
      properties: {
        text: { type: 'string', description: '要告诉主AI的完整内容' },
        request_type: { type: 'string', description: '请求类型' },
      },
      required: ['text'],
    },
  },
  {
    name: 'create_task',
    description:
      '创建任务。kind=set_alarm 一次性闹钟（at 传 Unix 秒或"+90s"/"+5m"/"+2h"/"+1d"）；'
      + 'kind=recurring_task 周期任务（every 传"+30m"/"+6h"等间隔，note 写每次触发时要执行的完整指令）。'
      + '任务持久化保存，重启不丢。',
    parameters: {
      type: 'object',
      properties: {
        kind: { type: 'string', description: 'set_alarm / recurring_task' },
        at: { type: 'string', description: '闹钟时间（Unix 秒或 +90s/+5m/+2h/+1d）' },
        every: { type: 'string', description: '周期间隔（+30m/+6h/+1d，最短 1 分钟）' },
        note: { type: 'string', description: '提醒内容 / 周期任务每次要执行的指令' },
      },
      required: ['kind', 'note'],
    },
  },
  {
    name: 'list_tasks',
    description: '列出还未触发的闹钟/周期任务（主AI可见所有会话的任务）。',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'cancel_task',
    description: '取消一个还没触发的任务（传 task_id，可从 list_tasks 查到）。',
    parameters: {
      type: 'object',
      properties: { task_id: { type: 'string', description: '要取消的任务 ID' } },
      required: ['task_id'],
    },
  },
  {
    name: 'view_image',
    description:
      '查看图片：默认看本轮消息带的图（index 从 1 开始），也可传 message_ref 看历史某条消息里的图。'
      + 'question 写你想从图里知道什么（默认整体描述）。不要对每张图都调，先看提示文字判断需不需要。',
    parameters: {
      type: 'object',
      properties: {
        message_ref: { type: 'string', description: '可选，历史消息短ID，如 A1B2；不传则看本轮消息的图' },
        index: { type: 'number', description: '第几张图，从 1 开始，默认 1' },
        question: { type: 'string', description: '想从图中了解什么' },
      },
    },
  },
  {
    name: 'send_image',
    description:
      '给用户发送一张图片。file 传 http(s) 图片链接、base64:// 数据或 NapCat 可访问的本地绝对路径。'
      + '需要配文字说明时用 send_message 另发一条。',
    parameters: {
      type: 'object',
      properties: {
        file: { type: 'string', description: '图片地址：http(s)://… / base64://… / 绝对路径' },
      },
      required: ['file'],
    },
  },
  {
    name: 'trigger_config',
    description:
      '调整自己在本会话群里的触发设置（私聊必回，只在群聊有意义）。'
      + 'action=set_rate 设随机接话率（rate 0~1，越小越安静，默认 0.01）；'
      + 'action=add_word / remove_word 增删自己的触发词（word）；'
      + 'action=list 查看当前设置。被嫌吵就调低，混熟了想活跃再调高。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'set_rate / add_word / remove_word / list' },
        rate: { type: 'number', description: 'set_rate：随机接话率 0~1' },
        word: { type: 'string', description: 'add_word/remove_word：触发词（≤20字）' },
      },
      required: ['action'],
    },
  },
  {
    name: 'memory_search',
    description:
      '精确检索历史聊天记录（含你发过的消息和很久以前的内容，不限于当前上下文窗口）。'
      + 'query 用空格分隔多个关键词（全部命中才返回），用引号包住必须连续出现的短语，如 "北京 见面" 周末。'
      + '需要回忆具体细节、时间、承诺、谁说过什么时优先用它，别凭印象编。',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '搜索词：空格分词=AND，引号=精确短语' },
        scope: { type: 'string', description: '"current"（默认，只搜当前会话）或 "all"（搜所有会话）' },
        user_id: { type: 'number', description: '可选，只搜某人（QQ号）发的消息' },
        days: { type: 'number', description: '可选，只搜最近 N 天' },
        limit: { type: 'number', description: '可选，返回条数上限，默认 12，最大 50' },
      },
      required: ['query'],
    },
  },
  {
    name: 'intel_report',
    description:
      '向全局关系网络上报情报：某个QQ号的一条客观事实（身份/偏好/关系/重要事件等）。'
      + '所有会话的子AI共享这份网络，你在任何群里了解到某人的信息，都会进入他在其他会话的档案。'
      + '只上报确认过或高概率的事，主观臆测不要报。这条信息用户看不到。',
    parameters: {
      type: 'object',
      properties: {
        fact_subject_id: { type: 'number', description: '情报主体的 QQ 号' },
        fact_category: {
          type: 'string',
          description: '分类：identity（身份）/ preference（偏好）/ relationship（关系）/ event（事件）/ emotion（情绪倾向）/ other',
        },
        fact_text: { type: 'string', description: '一句话客观陈述，≤60字，带具体锚点' },
        confidence: { type: 'number', description: '把握 0~1，默认 0.8' },
      },
      required: ['fact_subject_id', 'fact_category', 'fact_text'],
    },
  },
  {
    name: 'relation_query',
    description:
      '查询全局关系网络：某人的人物档案（跨所有会话的观察）、按关键词搜情报、或列出本会话已知成员概况。'
      + '想不起来某个人是谁、谁和谁什么关系、以前聊过什么相关的事，先查这里再回答。',
    parameters: {
      type: 'object',
      properties: {
        user_id: { type: 'number', description: '查这个 QQ 号的人物档案' },
        query: { type: 'string', description: '关键词搜情报（空格分词=AND），如 "北京 见面"' },
        list_members: { type: 'boolean', description: 'true 时列出本会话已知成员一句话概况' },
      },
    },
  },
  {
    name: 'impression_write',
    description:
      '更新你对当前会话的整体印象（用途/氛围/关键人物/说话风格），一句话 60 字内。'
      + '印象会写进全局关系网络的会话档案，之后每次唤醒都会带在提示词里。'
      + '只有当你对会话的理解有实质更新时才写，不要每轮都写。',
    parameters: {
      type: 'object',
      properties: { text: { type: 'string', description: '新的会话印象，一句话' } },
      required: ['text'],
    },
  },
];

/** 主AI scope 专属工具：关系网写入、跨会话委派、直接发消息。 */
const MASTER_ONLY_TOOL_SCHEMAS: ToolSchema[] = [
  {
    name: 'relation_write',
    description:
      '写入/修订全局关系网络（仅主AI）。action 说明：'
      + 'set_person_impression=更新某人整体印象（user_id + text）；'
      + 'add_fact=给某人追加一条事实（user_id + category + text，confidence 可选）；'
      + 'supersede_fact=用新表述取代一条旧情报（fact_id + text）；'
      + 'retract_fact=撤回一条错误情报（fact_id）；'
      + 'set_scope_impression=修订某会话的印象（target_scope_type + target_scope_id + text）。'
      + '写入前先用 relation_query 查已有档案做增量更新，避免重复和覆盖。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          description: 'set_person_impression / add_fact / supersede_fact / retract_fact / set_scope_impression',
        },
        user_id: { type: 'number', description: '目标人物 QQ 号（人物类 action 必填）' },
        category: { type: 'string', description: 'add_fact 分类：identity/preference/event/relationship/emotion/other' },
        text: { type: 'string', description: '印象/事实/新表述的内容' },
        confidence: { type: 'number', description: 'add_fact 把握 0~1，默认 1（主AI亲自录入）' },
        fact_id: { type: 'string', description: 'supersede/retract 的目标情报 ID' },
        target_scope_type: { type: 'string', description: 'set_scope_impression 目标会话类型 group/private' },
        target_scope_id: { type: 'string', description: 'set_scope_impression 目标会话 ID' },
      },
      required: ['action'],
    },
  },
  {
    name: 'delegate_to_child',
    description:
      '委派另一个会话的子AI做事（仅主AI）：instruction 会被注入目标会话，由那边的子AI自主执行'
      + '（它可以自然地和用户说话、查证、回报）。跨会话联系/转达/查情况优先用它，而不是自己直接出面。'
      + 'instruction 要写全背景：谁要求的、要做什么、说完要不要回报。子AI完成后会通过 notify_master 回报结果。',
    parameters: {
      type: 'object',
      properties: {
        target_scope_type: { type: 'string', description: '目标会话类型 group/private' },
        target_scope_id: { type: 'string', description: '目标会话 ID（群号或 QQ 号）' },
        instruction: { type: 'string', description: '给目标子AI的完整指令' },
      },
      required: ['target_scope_type', 'target_scope_id', 'instruction'],
    },
  },
  {
    name: 'message_scope',
    description:
      '以机器人身份直接向指定会话的用户发一条消息（仅主AI）。'
      + '仅在必须绕过子AI直接说话时使用（系统通知、紧急干预）；日常联系一律用 delegate_to_child 让子AI自然转达。',
    parameters: {
      type: 'object',
      properties: {
        target_scope_type: { type: 'string', description: '目标会话类型 group/private' },
        target_scope_id: { type: 'string', description: '目标会话 ID' },
        content: { type: 'string', description: '要发送的内容' },
      },
      required: ['target_scope_type', 'target_scope_id', 'content'],
    },
  },
  {
    name: 'plugin_manage',
    description:
      '管理 API 插件（仅主AI）。插件是给冰糖增加能力的 JSON 声明式 HTTP 工具（data/plugins/<名>/plugin.json）。'
      + 'action：list=列出所有插件及其工具/状态/加载错误；enable/disable 加 name=插件名 开关某个插件（立即生效并持久化）；'
      + 'reload=重新扫描插件目录（主人或卡西改完插件文件后调用，热加载不用重启）。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', description: 'list / enable / disable / reload' },
        name: { type: 'string', description: '插件名（enable/disable 时必填）' },
      },
      required: ['action'],
    },
  },
];

/** 主AI scope 不用的子AI工具（notify_master 会中继给自己形成自环）。 */
const CHILD_ONLY_TOOL_NAMES = new Set(['notify_master', 'intel_report', 'impression_write']);

/**
 * Scope-dependent tool list: children get the chat set; the master scope swaps
 * the report-to-master tools for direct relation-write and delegation tools.
 */
export function chatToolSchemas(isMaster: boolean): ToolSchema[] {
  const builtin: ToolSchema[] = isMaster
    ? [
      ...CHAT_TOOL_SCHEMAS.filter((schema) => !CHILD_ONLY_TOOL_NAMES.has(schema.name)),
      ...ACTION_TOOL_SCHEMAS,
      ...MASTER_ONLY_TOOL_SCHEMAS,
    ]
    : [...CHAT_TOOL_SCHEMAS, ...ACTION_TOOL_SCHEMAS];
  const reserved = new Set(builtin.map((schema) => schema.name));
  const pluginSchemas = pluginRegistry()?.toolSchemas(isMaster, reserved) ?? [];
  return [...builtin, ...pluginSchemas];
}

export async function executeChatTool(
  call: { name: string; arguments: Record<string, unknown> },
  context: ToolContext,
): Promise<ToolResult> {
  context.executedTools.push(call.name);
  const args = call.arguments ?? {};
  switch (call.name) {
    case 'send_message': {
      // Outbound normalization (legacy _send_scope_message): drop thinking,
      // [[...]] markers, and CQ:at codes in private chats, then split into
      // per-message lines ("分条").
      let text = stripThinking(String(args.content ?? '')).replace(/\[\[.*?\]\]/g, '');
      if (context.scopeType === 'private') text = text.replace(/\[CQ:at,qq=\d+\]/g, '');
      const lines = replyMessageLines(text);
      if (lines.length === 0) return { ok: false, output: 'send_message: content 过滤 thinking 后为空，未发送。' };
      let replyToMessage: ChatMessage | null = null;
      const replyRef = String(args.reply_to_id ?? '').trim();
      if (replyRef) {
        const messageId = context.repo.resolveRef(context.scopeType, context.scopeId, replyRef);
        if (messageId) {
          replyToMessage = {
            chatType: context.scopeType as 'group' | 'private',
            chatId: Number(context.scopeId),
            userId: 0,
            text: '',
            rawMessage: '',
            sender: {},
            messageId,
            mentionsSelf: false,
            timestamp: Date.now() / 1000,
            rawData: {},
          };
        }
      }
      // Reply quote anchors to the first line only; the rest go out plain.
      const refs: string[] = [];
      let failure = '';
      for (const [index, line] of lines.entries()) {
        try {
          const result = index === 0 && replyToMessage
            ? await context.bot.sendReplyText(replyToMessage, line)
            : await context.bot.sendText(context.scopeType as 'group' | 'private', Number(context.scopeId), line);
          const sentId = (result as { message_id?: number | string }).message_id;
          const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
          refs.push(ref);
          const entry: HistoryEntry = {
            user_id: context.bot.selfId,
            nickname: '冰糖',
            text: line,
            raw_message: line,
            message_id: sentId ?? null,
            message_ref: ref,
            timestamp: Date.now() / 1000,
            source_label: '',
          };
          context.repo.appendMessage(context.scopeType, context.scopeId, entry);
        } catch (error) {
          failure = error instanceof Error ? error.message : String(error);
          break;
        }
      }
      if (refs.length === 0) return { ok: false, output: `send_message 发送失败: ${failure}` };
      const interrupted = failure ? `（后续行发送中断: ${failure}）` : '';
      return { ok: true, output: `已发送 ${refs.length} 条（短ID ${refs.join('、')}）${interrupted}。`, sentToUser: true };
    }
    case 'stay_silent':
      return { ok: true, output: '本轮保持沉默。', staySilent: true };
    case 'recall_message': {
      const ref = String(args.message_ref ?? '').trim();
      const messageId = context.repo.resolveRef(context.scopeType, context.scopeId, ref);
      if (!messageId) return { ok: false, output: `没有找到短ID ${ref} 对应的消息。` };
      try {
        await context.bot.recallMessage(messageId);
        return { ok: true, output: `已撤回短ID ${ref} 的消息。` };
      } catch (error) {
        return { ok: false, output: `撤回失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'memory_list': {
      const notes = context.repo.recentNotes(context.scopeType, context.scopeId, 20);
      return { ok: true, output: notes.length > 0 ? notes.map((note, i) => `${i + 1}. ${note}`).join('\n') : '（暂无备忘）' };
    }
    case 'memory_write': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.repo.addNote(context.scopeType, context.scopeId, text);
      return { ok: true, output: '已记录。' };
    }
    case 'notify_master': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.notifyMaster({
        text,
        requestType: String(args.request_type ?? '') || undefined,
        fromScope: `${context.scopeType}:${context.scopeId}`,
      });
      return { ok: true, output: '已上报主AI。' };
    }
    case 'create_task': {
      const kind = String(args.kind ?? '').trim();
      const note = String(args.note ?? '').trim();
      if (!note) return { ok: false, output: 'note 为空。' };
      const originScope = `${context.scopeType}:${context.scopeId}`;
      if (kind === 'set_alarm') {
        const at = parseRelativeTime(String(args.at ?? ''));
        if (at === null) return { ok: false, output: 'at 格式无法解析（支持 Unix 秒或 +90s/+5m/+2h/+1d）。' };
        if (at <= Date.now() / 1000) return { ok: false, output: '闹钟时间必须在未来。' };
        const taskId = context.tasks.scheduleAlarm(originScope, at, note);
        return { ok: true, output: `闹钟已创建（task ${taskId}），到点会在本会话提醒你。` };
      }
      if (kind === 'recurring_task') {
        const interval = parseInterval(String(args.every ?? ''));
        if (interval === null) return { ok: false, output: 'every 格式无法解析（支持 +30m/+6h/+1d，最短 1 分钟）。' };
        const taskId = context.tasks.scheduleRecurring(originScope, interval, note);
        return { ok: true, output: `周期任务已创建（task ${taskId}），每 ${formatInterval(interval)} 触发一次；用 cancel_task 可停止。` };
      }
      return { ok: false, output: `暂不支持的任务类型: ${kind || '(空)'}（可用: set_alarm / recurring_task）` };
    }
    case 'list_tasks': {
      const tasks = context.isMaster ? context.tasks.list() : context.tasks.list(`${context.scopeType}:${context.scopeId}`);
      if (tasks.length === 0) return { ok: true, output: '（没有待触发的任务）' };
      const lines = tasks.slice(0, 20).map((task) => {
        const when = new Date(task.runAt * 1000);
        const pad = (value: number) => String(value).padStart(2, '0');
        const timeText = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}`;
        const kind = task.kind === 'recurring' ? `周期(每${formatInterval(task.intervalSeconds ?? 0)})` : '闹钟';
        const scope = context.isMaster ? ` ${task.originScope}` : '';
        return `- ${task.taskId} [${kind}]${scope} ${timeText}：${task.note.slice(0, 60)}`;
      });
      return { ok: true, output: `待触发任务（${tasks.length}）：\n${lines.join('\n')}` };
    }
    case 'cancel_task': {
      const taskId = String(args.task_id ?? '').trim();
      if (!taskId) return { ok: false, output: 'task_id 为空。' };
      const task = context.tasks.find(taskId);
      if (!task) return { ok: false, output: `没有找到任务 ${taskId}。` };
      if (!context.isMaster && task.originScope !== `${context.scopeType}:${context.scopeId}`) {
        return { ok: false, output: `任务 ${taskId} 不属于本会话，如需取消请通过 notify_master 找主AI。` };
      }
      if (!context.tasks.cancel(taskId)) return { ok: false, output: `任务 ${taskId} 已触发或已取消。` };
      return { ok: true, output: `已取消任务 ${taskId}。` };
    }
    case 'view_image': {
      let refs = context.triggerImageRefs;
      let sourceNote = '本轮消息';
      const ref = String(args.message_ref ?? '').trim();
      if (ref) {
        const normalized = ref.replace(/^\[#?|\]$/g, '');
        const entry = context.repo.findEntryByRef(context.scopeType, context.scopeId, normalized);
        if (!entry) return { ok: false, output: `没有找到短ID ${ref} 对应的消息。` };
        refs = Array.isArray(entry.image_refs) ? (entry.image_refs as string[]) : [];
        sourceNote = `消息 ${ref}`;
        if (refs.length === 0) return { ok: false, output: `消息 ${ref} 里没有图片。` };
      }
      if (refs.length === 0) return { ok: false, output: '本轮消息没有图片；如果图在历史消息里，传 message_ref。' };
      const index = Math.max(1, Number(args.index ?? 1) || 1);
      if (index > refs.length) {
        return { ok: false, output: `${sourceNote}只有 ${refs.length} 张图，index ${index} 超出范围。` };
      }
      const question = String(args.question ?? '').trim() || '请客观描述这张图片的内容：主体、场景、文字信息（如有）、值得注意的细节。';
      try {
        const description = await context.describeImage(refs[index - 1], question);
        return { ok: true, output: `图片内容（${sourceNote}第 ${index}/${refs.length} 张）：\n${description}` };
      } catch (error) {
        return { ok: false, output: `图片解析失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'send_image': {
      const file = String(args.file ?? '').trim();
      if (!file) return { ok: false, output: 'file 为空。' };
      if (!/^(https?:\/\/|base64:\/\/|\/)/i.test(file)) {
        return { ok: false, output: 'file 必须是 http(s) 链接、base64:// 数据或绝对路径。' };
      }
      try {
        const result = await context.bot.sendImage(context.scopeType as 'group' | 'private', Number(context.scopeId), file);
        const sentId = (result as { message_id?: number | string }).message_id;
        const ref = context.repo.registerMessageRef(context.scopeType, context.scopeId, sentId ?? null);
        context.repo.appendMessage(context.scopeType, context.scopeId, {
          user_id: context.bot.selfId,
          nickname: '冰糖',
          text: '[图片]',
          raw_message: `[CQ:image,file=${file.slice(0, 120)}]`,
          message_id: sentId ?? null,
          message_ref: ref,
          timestamp: Date.now() / 1000,
          source_label: '',
          image_refs: /^https?:\/\//i.test(file) ? [file] : [],
        });
        return { ok: true, output: `图片已发送（短ID ${ref}）。`, sentToUser: true };
      } catch (error) {
        return { ok: false, output: `图片发送失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    case 'trigger_config': {
      const action = String(args.action ?? '').trim();
      const agent = context.repo.getOrCreateAgent(context.scopeType, context.scopeId);
      switch (action) {
        case 'set_rate': {
          const rate = Number(args.rate);
          if (!Number.isFinite(rate) || rate < 0 || rate > 1) {
            return { ok: false, output: 'rate 必须是 0~1 的数字（如 0.05）。' };
          }
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerRate: rate });
          return { ok: true, output: `随机接话率已调整为 ${(rate * 100).toFixed(1)}%（立即生效，跨重启保存）。` };
        }
        case 'add_word': {
          const word = String(args.word ?? '').trim();
          if (!word || word.length > 20) return { ok: false, output: 'word 为空或超过 20 字。' };
          if (agent.triggerWords.some((existing) => existing.toLowerCase() === word.toLowerCase())) {
            return { ok: true, output: `触发词「${word}」已经存在。` };
          }
          const triggerWords = [...agent.triggerWords, word];
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerWords });
          return { ok: true, output: `已添加触发词「${word}」，当前共 ${triggerWords.length} 个：${triggerWords.join('、')}` };
        }
        case 'remove_word': {
          const word = String(args.word ?? '').trim();
          if (!word) return { ok: false, output: 'word 为空。' };
          const triggerWords = agent.triggerWords.filter((existing) => existing.toLowerCase() !== word.toLowerCase());
          if (triggerWords.length === agent.triggerWords.length) {
            return { ok: false, output: `没有触发词「${word}」，当前触发词：${agent.triggerWords.join('、') || '（无）'}` };
          }
          context.repo.updateAgent(context.scopeType, context.scopeId, { triggerWords });
          return { ok: true, output: `已删除触发词「${word}」，剩余：${triggerWords.join('、') || '（无）'}` };
        }
        case 'list':
          return {
            ok: true,
            output:
              `当前触发设置：随机接话率 ${(agent.triggerRate * 100).toFixed(1)}%，`
              + `触发词（${agent.triggerWords.length}）：${agent.triggerWords.join('、') || '（无）'}。`,
          };
        default:
          return { ok: false, output: `未知 action: ${action || '(空)'}，可用: set_rate / add_word / remove_word / list` };
      }
    }
    case 'memory_search': {
      const query = String(args.query ?? '').trim();
      if (!query) return { ok: false, output: 'query 为空。' };
      const scope = String(args.scope ?? 'current').trim().toLowerCase();
      const scopeKeys = scope === 'all' ? [] : [`${context.scopeType}:${context.scopeId}`];
      const days = Number(args.days ?? 0);
      const from = Number.isFinite(days) && days > 0 ? Date.now() / 1000 - days * 86400 : undefined;
      const limit = Number(args.limit ?? 12);
      const hits = await context.archive.search({
        query,
        scopeKeys,
        userId: Number.isFinite(Number(args.user_id)) && args.user_id !== undefined ? Number(args.user_id) : undefined,
        from,
        limit,
      });
      if (hits.length === 0) {
        return { ok: true, output: `没有找到与「${query}」相关的历史消息。可以换更短的关键词、去掉引号短语，或扩大 days/scope 再试。` };
      }
      const lines = hits.map((hit) => {
        const parts = [
          ...hit.contextBefore.map((line) => `  上文 ${line}`),
          `▶ ${hit.line}`,
          ...hit.contextAfter.map((line) => `  下文 ${line}`),
        ];
        return parts.join('\n');
      });
      return { ok: true, output: `命中 ${hits.length} 条（按时间顺序，▶ 是命中行）：\n${lines.join('\n')}` };
    }
    case 'intel_report': {
      const subject = Number(args.fact_subject_id);
      const text = String(args.fact_text ?? '').trim();
      if (!Number.isFinite(subject) || subject <= 0) return { ok: false, output: 'fact_subject_id 必须是有效的 QQ 号。' };
      if (!text) return { ok: false, output: 'fact_text 为空。' };
      const added = context.relations.addFact({
        subject,
        category: String(args.fact_category ?? 'other') as never,
        text,
        sourceScope: `${context.scopeType}:${context.scopeId}`,
        sourceTool: 'intel_report',
        confidence: Number.isFinite(Number(args.confidence)) ? Number(args.confidence) : undefined,
      });
      if (added === null) return { ok: false, output: '情报无效（内容为空或主体不合法）。' };
      const person = context.relations.person(subject);
      const activeCount = person ? person.facts.filter((fact) => fact.status === 'active').length : 1;
      return { ok: true, output: `已录入全局关系网络（该人物现有 ${activeCount} 条情报）。` };
    }
    case 'relation_query': {
      const userId = Number(args.user_id);
      const query = String(args.query ?? '').trim();
      const parts: string[] = [];
      if (Number.isFinite(userId) && userId > 0) {
        const card = context.relations.personCard(userId);
        parts.push(card ? `人物档案：\n${card}` : `关系网络里还没有 ${userId} 的档案。`);
      }
      if (query) {
        const facts = context.relations.searchFacts(query, 10);
        parts.push(
          facts.length > 0
            ? `情报命中：\n${facts.map((fact) => `- [${fact.id}] ${fact.subject}[${fact.category}] ${fact.text}`).join('\n')}`
            : `没有命中「${query}」的情报。`,
        );
      }
      if (args.list_members === true || (!Number.isFinite(userId) && !query)) {
        const members = context.relations.knownMembers(`${context.scopeType}:${context.scopeId}`);
        if (members.length === 0) {
          parts.push('本会话还没有已知成员记录。');
        } else {
          parts.push(`本会话已知成员（${members.length}）：\n${members
            .slice(-10)
            .map((person) => {
              const topFact = person.facts.find((fact) => fact.status === 'active');
              return `- ${person.canonicalName}(${person.userId})${topFact ? `：${topFact.text}` : ''}`;
            })
            .join('\n')}`);
        }
      }
      return { ok: true, output: parts.length > 0 ? parts.join('\n\n') : '请提供 user_id、query 或 list_members 之一。' };
    }
    case 'impression_write': {
      const text = String(args.text ?? '').trim();
      if (!text) return { ok: false, output: 'text 为空。' };
      context.relations.setScopeImpression(`${context.scopeType}:${context.scopeId}`, text);
      return { ok: true, output: '会话印象已更新。' };
    }
    case 'relation_write': {
      if (!context.isMaster) return { ok: false, output: 'relation_write 仅限主AI使用，请通过 notify_master 上报由主AI归档。' };
      return executeRelationWrite(args, context);
    }
    case 'delegate_to_child': {
      if (!context.isMaster) return { ok: false, output: 'delegate_to_child 仅限主AI使用，需要跨会话协调请调用 notify_master。' };
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const instruction = String(args.instruction ?? '').trim();
      if (!targetType || !targetId || !instruction) return { ok: false, output: 'target_scope_type / target_scope_id / instruction 都不能为空。' };
      if (targetType !== 'group' && targetType !== 'private') return { ok: false, output: `target_scope_type 只能是 group 或 private，收到: ${targetType}` };
      if (targetType === context.scopeType && targetId === context.scopeId) return { ok: false, output: '目标是当前会话（主AI自己），直接处理即可，无需委派。' };
      const taskId = context.delegateToChild(targetType, targetId, instruction);
      return { ok: true, output: `已委派 ${targetType}:${targetId} 的子AI（task ${taskId}），它完成后会通过 notify_master 回报。` };
    }
    case 'message_scope': {
      if (!context.isMaster) return { ok: false, output: 'message_scope 仅限主AI使用。' };
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const content = stripThinking(String(args.content ?? '')).trim();
      if (!targetType || !targetId || !content) return { ok: false, output: 'target_scope_type / target_scope_id / content 都不能为空。' };
      if (targetType !== 'group' && targetType !== 'private') return { ok: false, output: `target_scope_type 只能是 group 或 private，收到: ${targetType}` };
      try {
        const output = await context.sendToScope(targetType, targetId, content);
        return { ok: true, output };
      } catch (error) {
        return { ok: false, output: `发送失败: ${error instanceof Error ? error.message : String(error)}` };
      }
    }
    default: {
      if (call.name === 'plugin_manage') {
        const registry = pluginRegistry();
        if (!registry) return { ok: false, output: '插件系统未初始化。' };
        const action = String(args.action ?? '').trim();
        try {
          if (action === 'reload') {
            registry.load();
            const infos = registry.listInfo();
            const good = infos.filter((info) => !info.error);
            const bad = infos.filter((info) => info.error);
            return {
              ok: true,
              output: `已重新扫描：${good.length} 个插件可用${bad.length > 0 ? `，${bad.length} 个加载失败（${bad.map((info) => `${info.name}: ${info.error}`).join('；')}）` : ''}。`,
            };
          }
          if (action === 'enable' || action === 'disable') {
            const name = String(args.name ?? '').trim();
            registry.setEnabled(name, action === 'enable');
            return { ok: true, output: `插件 ${name} 已${action === 'enable' ? '启用' : '停用'}（立即生效，重启后保持）。` };
          }
          // list
          const infos = registry.listInfo();
          if (infos.length === 0) return { ok: true, output: '插件目录是空的（data/plugins/<名>/plugin.json 声明式定义 HTTP API 工具，放好后用 reload 加载）。' };
          const lines = infos.map((info) => {
            if (info.error) return `✗ ${info.name}（加载失败: ${info.error}）`;
            return `${info.enabled ? '●' : '○'} ${info.name}${info.description ? ` — ${info.description}` : ''}\n  工具: ${info.tools.join('、')}`;
          });
          return { ok: true, output: `插件列表（●启用 ○停用）：\n${lines.join('\n')}` };
        } catch (error) {
          return { ok: false, output: `plugin_manage 失败: ${error instanceof Error ? error.message : String(error)}` };
        }
      }
      const actionResult = await executeActionTool(call, context);
      if (actionResult) return actionResult;
      const pluginResult = await pluginRegistry()?.execute(call.name, args, context);
      if (pluginResult) return pluginResult;
      return { ok: false, output: `未知或不可用的工具: ${call.name}，本次未执行。` };
    }
  }
}

function executeRelationWrite(args: Record<string, unknown>, context: ToolContext): ToolResult {
  const action = String(args.action ?? '').trim();
  const relations = context.relations;
  switch (action) {
    case 'set_person_impression': {
      const userId = Number(args.user_id);
      const text = String(args.text ?? '').trim();
      if (!Number.isFinite(userId) || userId <= 0 || !text) return { ok: false, output: 'user_id 和 text 都不能为空。' };
      relations.setPersonImpression(userId, text);
      return { ok: true, output: `已更新 ${userId} 的人物印象。` };
    }
    case 'add_fact': {
      const userId = Number(args.user_id);
      const text = String(args.text ?? '').trim();
      if (!Number.isFinite(userId) || userId <= 0 || !text) return { ok: false, output: 'user_id 和 text 都不能为空。' };
      const added = relations.addFact({
        subject: userId,
        category: String(args.category ?? 'other') as never,
        text,
        sourceScope: `${context.scopeType}:${context.scopeId}`,
        sourceTool: 'master',
        confidence: Number.isFinite(Number(args.confidence)) ? Number(args.confidence) : 1.0,
      });
      if (added === null) return { ok: false, output: '情报无效（内容为空或主体不合法）。' };
      return { ok: true, output: `已为 ${userId} 追加情报（fact_id ${added.id}）。` };
    }
    case 'supersede_fact': {
      const factId = String(args.fact_id ?? '').trim();
      const text = String(args.text ?? '').trim();
      if (!factId || !text) return { ok: false, output: 'fact_id 和 text 都不能为空。' };
      const next = relations.supersedeFact(factId, text);
      if (next === null) return { ok: false, output: `没有找到情报 ${factId}。` };
      return { ok: true, output: `已用新表述取代旧情报（新 fact_id ${next.id}）。` };
    }
    case 'retract_fact': {
      const factId = String(args.fact_id ?? '').trim();
      if (!factId) return { ok: false, output: 'fact_id 为空。' };
      if (!relations.retractFact(factId)) return { ok: false, output: `没有找到情报 ${factId}。` };
      return { ok: true, output: `已撤回情报 ${factId}。` };
    }
    case 'set_scope_impression': {
      const targetType = String(args.target_scope_type ?? '').trim();
      const targetId = String(args.target_scope_id ?? '').trim();
      const text = String(args.text ?? '').trim();
      if (!targetType || !targetId || !text) return { ok: false, output: 'target_scope_type / target_scope_id / text 都不能为空。' };
      relations.setScopeImpression(`${targetType}:${targetId}`, text);
      return { ok: true, output: `已更新会话 ${targetType}:${targetId} 的印象。` };
    }
    default:
      return { ok: false, output: `未知 action: ${action || '(空)'}，可用: set_person_impression / add_fact / supersede_fact / retract_fact / set_scope_impression` };
  }
}

function parseRelativeTime(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const interval = parseInterval(trimmed, 1);
  if (interval !== null) return Date.now() / 1000 + interval;
  const parsed = Number(trimmed);
  if (Number.isFinite(parsed) && parsed > 1e9) return parsed;
  return null;
}

/** "+90s"/"+5m"/"+2h"/"+1d" → seconds (null if unparsable or below minSeconds). */
function parseInterval(value: string, minSeconds = 60): number | null {
  const relative = /^\+(\d+(?:\.\d+)?)([smhd])$/.exec(value.trim().toLowerCase());
  if (!relative) return null;
  const amount = Number(relative[1]);
  const unit = relative[2] === 's' ? 1 : relative[2] === 'm' ? 60 : relative[2] === 'h' ? 3600 : 86400;
  const seconds = amount * unit;
  return seconds >= minSeconds ? seconds : null;
}

function formatInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}天`;
  if (seconds % 3600 === 0) return `${seconds / 3600}小时`;
  if (seconds % 60 === 0) return `${seconds / 60}分钟`;
  return `${seconds}秒`;
}
