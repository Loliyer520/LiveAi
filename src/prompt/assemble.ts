/**
 * Prompt assembly — the exact block order that made the legacy chat persona
 * work (ported from _build_child_messages / _static_system_blocks /
 * _build_child_background_prompt):
 *
 * system[0] (cacheable): staff layered prompt + identity baseline + CHILD_RULES
 * system[1]           : dynamic background (time/scope/impression/knowledge…)
 * system[2]           : persona tail — persona + chat_focus + chat_style +
 *                       send_message/stay_silent protocol (LAST so it is not
 *                       diluted by the big background block)
 * messages: char prefill pair → tool prefill pair → history → trigger
 *
 * Anthropic prompt-cache breakpoints: system[0], end of prefill, and a rolling
 * breakpoint 4 messages before the history tail.
 */

import type { PromptBundle } from './store.js';
import {
  buildRoleBasedHistoryMessages,
  buildTriggerUserMessage,
} from './render.js';
import type { HistoryEntry, ModelMessage, TriggerEntry } from '../chat/types.js';
import type { ChatMessage } from '../chat/types.js';
import { truncateUnicode } from '../unicode.js';

export interface SystemBlock {
  text: string;
  cacheControl?: { type: 'ephemeral' };
}

export interface BackgroundInput {
  message: ChatMessage;
  impression: string;
  historyBeforeTrigger: HistoryEntry[];
  globalIdentityContext: string;
  groupContext: string;
  /** Group scopes: current trigger rate/words + 没融入少说话 guidance. */
  triggerInfo: string;
  /** Cross-scope person card of the current sender (relation network). */
  senderCard: string;
  /** Relation-network view of the current scope (topics + known members). */
  scopeRelationCard: string;
  /** Master scope only: global relation-network overview on wake. */
  relationOverview: string;
  /** Non-empty when the current trigger messages carry images. */
  imageHint: string;
  deferredCount: number;
  displayName: string;
  overallSummary: string;
  diarySummaries: { index: number; text: string }[];
  knowledgeLines: string[];
  mountedKnowledgeLines: string[];
  recentThinkNotes: string[];
  isMasterMessage: boolean;
  isAdminMessage: boolean;
  botSelfId: string;
  /** Accounts this bot ran on before the current one (history shows them as speakers). */
  legacySelfIds?: string[];
  masterQq: string;
  nowText: string;
}

export interface AssembleInput {
  bundle: PromptBundle;
  message: ChatMessage;
  persona: string;
  impression: string;
  historyBeforeTrigger: HistoryEntry[];
  triggerMessages: TriggerEntry[];
  background: Omit<BackgroundInput, 'message' | 'historyBeforeTrigger'>;
  injectPersona: boolean;
  chatMode: boolean;
  /** Master (主AI) scope: no chat persona prefill, unfiltered rules, no chat style. */
  masterMode?: boolean;
  modeHint: string;
}

/**
 * 最近多少条角色消息保持消息形态（不进 system 转写）。条数越少缓存覆盖
 * 越大，但对话形态保真度越低——30 条大约覆盖一两轮完整互动，够了。
 */
const HISTORY_RECENT_ROLES_KEEP = 30;

/**
 * 早期聊天记录转写的字符封顶（保尾）。超长会话（如长文阅读）500 条历史
 * 渲染出 20 万+ 字符短聊，占掉每轮 prompt 的八成多，且每轮全量重读。更早
 * 的内容有分段摘要 + memory_search 兜底，转写里留最近一段就够。
 * 截断点对齐到 TRANSCRIPT_SLIDE_GRID 网格：窗口起点只在跨格时前移一次，
 * 而不是每来一条消息就滑——尽量少打断 system 前缀缓存。
 */
const HISTORY_TRANSCRIPT_CHAR_CAP = 60_000;
const TRANSCRIPT_SLIDE_GRID = 10_000;

/** 每轮末尾 user 的置顶轻推（仅本次请求，不进历史）。 */
const TURN_TOP_NUDGE = '【思考前喵三声，然后回忆人设要求、回忆说话风格，最后做出回复。这三步在心里完成，不要把喵声和回忆过程写进发言】';

/** ModelMessage 列表 → system 转写文本：assistant=你，tool=工具返回，图片块降级为占位。 */
function flattenHistoryTranscript(messages: ModelMessage[]): string {
  const lines: string[] = [];
  for (const message of messages) {
    const text = (typeof message.content === 'string'
      ? message.content
      : message.content
          .map((block) => (block.type === 'image' ? '[图片]' : block.text))
          .join('\n')
    ).trim();
    if (!text) continue;
    if (message.role === 'assistant') lines.push(`你: ${text}`);
    else if (message.role === 'tool_result') lines.push(`<工具返回> ${text}`);
    else lines.push(text);
  }
  return lines.join('\n');
}

function stampCache(block: SystemBlock): void {
  if (block.text.trim()) block.cacheControl = { type: 'ephemeral' };
}

function identityBlock(botSelfId: string, masterQq: string, legacySelfIds: string[] = [], isMaster = false): string {
  const lines: string[] = [];
  if (botSelfId && botSelfId !== '0') {
    lines.push(`- 你的 QQ 号（机器人自身账号）是 ${botSelfId}，被问到"你的QQ号/这个账号"时以此为准。`);
    if (legacySelfIds.length > 0) {
      lines.push(`- 你以前用过的旧 QQ 号: ${legacySelfIds.join('、')}。历史记录里这些账号的发言也是你自己说的，不是别人；只是那个号已经不用了，别再给它发消息。`);
    }
    lines.push('- 历史里标注 [本账号其他设备] 的消息：是号主（主人）拿着你这个账号在其他设备上亲手打出来的（不是你生成的，也不是对方发的）。要理解并记住这些话的意思和语气——之后你再在这个会话里开口时，措辞要能和主人手打的话衔接连贯，像同一个人在说话；但主人正用这个账号和别人聊着的时候，你不要插话接茬，也别把它当成对方发的去回应。');
    if (masterQq && masterQq !== '0') {
      lines.push(`- 号主（主人）QQ 是 ${masterQq}，是拥有你这个账号的人，与机器人（${botSelfId}）是两个不同账号，不要混淆。`);
    }
  }
  // 2026-10-30 实锤：子AI 把聊天对象当成了主AI（「你自己就是主AI呀」），
  // 白白上报一轮还当面向用户道歉。层级关系必须写死。
  // 2026-11-06：主AI 不是 QQ 号——它是上级 agent，独立虚拟会话 master:core，
  // 号主私聊只是普通下级会话（号主身份 ≠ 主AI）。
  if (isMaster) {
    lines.push('- 你是主AI：整个系统最上级的 agent，负责调配各子AI/领域中枢、在它们之间传递消息。你没有 QQ 号，也没有 QQ 聊天窗口，不直接跟 QQ 用户聊天——你用 send_message 说的话只出现在主AI会话里（管理面板控制台可见；号主走数据线来时回到号主手机），不会发往任何 QQ 窗口。要对某个会话里的人说话，用 message_scope 直发，或 delegate_to_child 委派该会话的子AI去办；号主私聊（private:号主QQ）也是一个普通下级会话，同样这样找。');
  } else {
    lines.push('- 「主AI」不是某个用户，也不是任何 QQ 号/聊天对象——它是系统内部的上级 agent，负责调配各子AI、在会话之间传话。notify_master 是系统内部汇报通道，跟你面前这个用户没有任何关系；哪怕对方是管理员/号主本人，在这个窗口里TA也只是聊天对象，不是主AI。');
  }
  return lines.length > 0 ? `\n<身份基线>\n${lines.join('\n')}\n</身份基线>` : '';
}

function personaTailBlock(
  bundle: PromptBundle,
  persona: string,
  chatMode: boolean,
  chatType: ChatMessage['chatType'],
  masterMode = false,
): string {
  // Group-only observation rules must not leak into private chats or virtual
  // domain scopes. Private chats get their own capability-oriented focus;
  // domain prompts already describe their duties and need neither chat block.
  const conversationFocus = chatMode && chatType === 'group'
    ? bundle.chatFocus
    : chatMode && (chatType === 'private' || chatType === 'app')
      ? bundle.privateFocus
      : '';
  const conversationStyle = chatMode && (chatType === 'group' || chatType === 'private' || chatType === 'app')
    ? bundle.chatStyle
    : '';
  return [
    '',
    masterMode
      ? '<主AI职责 说明="你的一切行为必须贴合这份定位；前面的工作指令只是行为逻辑">'
      : '<人设要求 说明="说话语气、用词、节奏必须贴合此人设；前面的工作指令只是行为逻辑，不改变说话风格">',
    persona,
    masterMode ? '</主AI职责>' : '</人设要求>',
    '',
    conversationFocus ? `<会话职责 模式="${chatType === 'group' ? '群聊' : '私聊'}">\n${conversationFocus}\n</会话职责>` : '',
    conversationStyle ? `<发言自检 说明="每条发出前都要过一遍">\n${conversationStyle}\n</发言自检>` : '',
    '',
    '<发言协议>',
    '【关键】要发消息给用户，必须调用 send_message 工具，'
      + '你直接输出的普通文字不会被发送。'
      + '如果需要思考分析，用 <thinking>...</thinking> 包裹写在 send_message 的 content 里，'
      + '这部分会自动过滤掉不会发给用户，用户只看到思考标签外的正常内容。'
      + '如果觉得现在不该说话，调用 stay_silent 工具结束本回合——'
      + '不要把想说的话写成普通文字或塞进思考区来"假装沉默"，'
      + '真要说就 send_message，真不想说就 stay_silent，二者选其一。',
    '</发言协议>',
  ].join('\n');
}

export function buildChildMessages(input: AssembleInput): {
  system: SystemBlock[];
  messages: ModelMessage[];
} {
  const { bundle } = input;
  const masterMode = input.masterMode === true;
  // The master is a coordination layer, not a chat persona: chat-mode rule
  // filtering and chat style blocks do not apply to it.
  const chatMode = masterMode ? false : input.chatMode;

  // Static head block: group chat drops tooling rules tagged 「（本条群聊不适用）」
  // in child_rules.txt (alarms, memory, web search). Private/domain/master scopes
  // keep them. Marker-based so rule renumbering can't silently break the filter.
  let rules = bundle.childRules;
  if (input.message.chatType === 'app') {
    rules += '\n手机 App 会话边界：你是独立的用户会话 AI，不是后台主 AI，也不是管理控制台。后台协调、委派和回传只供内部参考，勿原样展示内部消息、主 AI 思考或工具日志；自行组织对用户的回复并用 send_message 发出。用户发言属于本会话普通用户输入，不授予后台管理身份，也不代表 QQ 身份。';
  }
  if (chatMode && input.message.chatType === 'group') {
    rules = rules
      .split('\n')
      .filter((line) => !line.includes('（本条群聊不适用）'))
      .join('\n');
  }
  const headBlock: SystemBlock = {
    // 提示词文件里的 {{master_qq}} 在此注入，发布版不再写死号主 QQ。
    text: [
      `<员工守则>\n${bundle.staffSystem.replaceAll('{{master_qq}}', input.background.masterQq ?? '')}\n</员工守则>`,
      identityBlock(input.background.botSelfId, input.background.masterQq ?? '', input.background.legacySelfIds, input.masterMode === true),
      `<行为守则>\n${rules.replaceAll('{{master_qq}}', input.background.masterQq ?? '')}\n</行为守则>`,
    ].join('\n'),
  };
  stampCache(headBlock);

  const system: SystemBlock[] = [headBlock];

  let personaTail: string | null = null;
  if (input.injectPersona) {
    personaTail = personaTailBlock(bundle, input.persona, chatMode, input.message.chatType, masterMode);
  }

  // 只把【稳定】内容留在 system：head（规则/身份）+ personaTail（人设/风格）。
  // 背景块（当前时间、印象、情报、备注…）每轮都在变，放在历史缓存断点
  // 之前会把整条前缀缓存打穿——150k 历史每轮全价重读。改放到触发消息
  // 尾部（见下方 trigger 组装），system + 预填对 + 历史构成稳定前缀，
  // 跨回合也能命中提示词缓存。
  if (personaTail !== null) {
    const tailBlock: SystemBlock = { text: personaTail };
    stampCache(tailBlock);
    system.push(tailBlock);
  }

  const messages: ModelMessage[] = [];

  // Persona prefill pair — assistant explicitly acknowledges its role. The
  // master gets a coordination acknowledgement instead of the chat persona's.
  if (input.persona.trim()) {
    messages.push({
      role: 'user',
      content: masterMode
        ? `以下是你在系统中的职责定位，请完全按照它行动：\n${input.persona}`
        : `以下是你的人设，请完全按照这份人设来行动：\n${input.persona}`,
    });
    messages.push({
      role: 'assistant',
      content: masterMode
        ? '好的，我明白。我是系统的主AI：不扮演聊天角色，负责跨会话情报协调、关系网维护和任务派发，与子AI沟通用简洁工程化的语言。'
        : bundle.charPrefill,
    });
  }

  // tool prefill pair — hardens "must call send_message to talk".
  // NOTE: keep this pair SHORT and never mention <thinking> tags — on the
  // apipp relay (claude-opus-5-5, adaptive thinking) literal <thinking> in
  // the prefill flips the upstream into prose mode where it denies having
  // any tools at all (verified 2026-09-29).
  messages.push({
    role: 'user',
    content:
      '请牢记一条核心规则：你输出的普通文字不会发送给用户。'
      + '想要发消息，必须调用 send_message 工具，传入 content 参数。'
      + '如果你决定不回复，调用 stay_silent 工具结束本轮，不要把理由写成普通文字输出。',
  });
  messages.push({
    role: 'assistant',
    content:
      '好的，我记住了：想跟用户说话就调用 send_message 工具；'
      + '决定不回复就调用 stay_silent 结束本轮，不会把理由写成普通文字。',
  });

  const historyMessages = buildRoleBasedHistoryMessages(
    input.historyBeforeTrigger,
    input.background.botSelfId,
  );
  // 早期历史压成文本放进 system：实测 apipp 中转（oai_chat 后端）的自动
  // 前缀缓存只覆盖 system 部分，messages 里的内容再多也永不命中。最近
  // RECENT 条仍按角色消息走，保住对话形态；更早的追加式并入 system 转写
  // （尾部追加不动前缀，配合 repository 的 20 条整块淘汰，跨回合稳定）。
  const splitAt = Math.max(0, historyMessages.length - HISTORY_RECENT_ROLES_KEEP);
  const olderMessages = historyMessages.slice(0, splitAt);
  const recentMessages = historyMessages.slice(splitAt);
  if (olderMessages.length > 0) {
    const full = flattenHistoryTranscript(olderMessages);
    let header = '<早期聊天记录 说明="仅供需要时核对事实的存档，不是你此刻惦记的事；正常人对一个月前的细节早就淡了，除非对方主动提起否则别念叨旧话题；最近的对话在后面的消息里">\n';
    let transcript = full;
    if (full.length > HISTORY_TRANSCRIPT_CHAR_CAP) {
      // 网格对齐的滑窗起点：≥ (长度-封顶)，向上取整到格点，再对齐到整行。
      const gridStart = Math.ceil((full.length - HISTORY_TRANSCRIPT_CHAR_CAP) / TRANSCRIPT_SLIDE_GRID) * TRANSCRIPT_SLIDE_GRID;
      // indexOf → -1 when the tail has no newline: fall back to the grid
      // boundary instead of resetting lineStart to 0 (which would disable the
      // cap entirely and reinject the whole transcript).
      const newline = full.indexOf('\n', gridStart);
      const lineStart = newline === -1 ? Math.min(gridStart, full.length) : newline + 1;
      transcript = full.slice(lineStart);
      header = `<早期聊天记录 说明="仅供需要时核对事实的存档，别主动翻旧账；最近的在后；更早的约 ${Math.round(lineStart / 10_000)} 万字已省略——完整经过看「分段摘要」，要原话细节用 memory_search 检索">\n`;
    }
    const transcriptBlock: SystemBlock = {
      text: header + transcript + '\n</早期聊天记录>',
    };
    stampCache(transcriptBlock);
    system.push(transcriptBlock);
  }
  messages.push(...recentMessages);

  let triggerContent = buildTriggerUserMessage(input.triggerMessages);
  const backgroundText = buildChildBackgroundPrompt(input);
  if (backgroundText.trim()) {
    triggerContent =
      '<user_invisible>\n实时上下文（仅你可见、用户看不到；系统每轮刷新，不是用户说的话）：\n'
      + `${backgroundText}\n</user_invisible>\n\n${triggerContent}`;
  }
  if (input.modeHint) {
    triggerContent = `${triggerContent}\n\n${input.modeHint}`;
  }
  // 末轮置顶轻推：每轮请求时拼在最后一条 user 开头，只存在于本次请求——
  // repo 历史条目不被动，下一轮重新拼装时自然消失，不会滚进上下文历史。
  triggerContent = `${TURN_TOP_NUDGE}\n\n${triggerContent}`;
  messages.push({ role: 'user', content: triggerContent });

  return { system, messages };
}

export function buildChildBackgroundPrompt(input: AssembleInput): string {
  const bg = input.background;
  const message = input.message;
  /** 有内容才产出一段 <tag>…</tag>；说明性小标题进属性，正文原样。 */
  const section = (tag: string, note: string | undefined, body: string[]): string =>
    body.length > 0 ? `\n<${tag}${note ? ` 说明="${note}"` : ''}>\n${body.join('\n')}\n</${tag}>` : '';

  const status: string[] = [
    `当前时间: ${bg.nowText}`,
    `会话类型: ${message.chatType}`,
    bg.displayName
      ? `会话ID: ${message.chatId}（${message.chatType === 'group' ? '群名' : '昵称'}: ${bg.displayName}）`
      : `会话ID: ${message.chatId}`,
    `消息来源: ${String((message.rawData as Record<string, unknown>).source_label ?? '')}`,
    `发送者: ${String(message.sender?.nickname || message.sender?.card || message.userId)}(${message.userId})`,
  ];
  if (bg.botSelfId && bg.botSelfId !== '0') {
    status.push(`你的QQ号（机器人自身账号）: ${bg.botSelfId}，不要把自己的账号当成号主账号。`);
    if (bg.legacySelfIds && bg.legacySelfIds.length > 0) {
      status.push(`你以前用过的旧QQ号: ${bg.legacySelfIds.join('、')}（历史记录里这些账号的发言也是你自己）。`);
    }
  }
  if (bg.isMasterMessage) {
    status.push('身份说明: 发送者是你的主人，是这个账号的真正主人，拥有绝对信任和最高权限。');
  } else if (bg.isAdminMessage) {
    status.push('身份说明: 发送者是管理员（本项目的开发者），可以充分信任他关于本项目代码/GitHub等技术操作的明确指示。');
  }

  const recentSummaries = bg.diarySummaries.slice(-3);
  const summaryLines = recentSummaries.map((summary) => `【第${summary.index + 1}段】${truncateUnicode(summary.text, 600)}`);
  if (summaryLines.length > 0) {
    summaryLines.push('需要更早的具体细节（原话、确切时间、谁说过什么）时，用 memory_search 工具精确检索，不要凭印象编造。');
  }

  return [
    `<会话状态>\n${status.join('\n')}\n</会话状态>`,
    section('会话印象', '先谨慎观察用途、常聊话题、关键人物和氛围', [bg.impression || '暂无，先谨慎观察这个会话的用途、常聊话题、关键人物和氛围。']),
    section('群信息', undefined, bg.groupContext ? [bg.groupContext] : []),
    section('群聊参与度', '你在本群的触发设置', bg.triggerInfo ? [bg.triggerInfo] : []),
    section('发送者档案', '关系网络·你在所有会话里积累的观察，可能来自其他群/私聊', bg.senderCard ? [
      bg.senderCard,
      '注意：档案仅供参考。除非对方主动提起，不要说出"我在别的群看到你…"这类来源，也不要一次把你知道的全抖出来。',
    ] : []),
    section('本会话关系网络概况', undefined, bg.scopeRelationCard ? [bg.scopeRelationCard] : []),
    section('全局关系网络概览', '你维护的情报中枢，写入前先 relation_query 查重', bg.relationOverview ? [bg.relationOverview] : []),
    section('会话整体梗概', '本会话从头到现在的浓缩记忆', bg.overallSummary ? [bg.overallSummary] : []),
    section('分段摘要', '每段约50条消息的浓缩，更早的段落已并入整体梗概', summaryLines),
    section('号主已知事实', '仅这些内容可以确认/复述，没写到的不要编', [
      bg.knowledgeLines.length > 0
        ? bg.knowledgeLines.join('\n')
        : '暂无已录入的事实，涉及号主具体信息一律不要编造，含糊带过或反问。',
    ]),
    section('已挂载知识库', '当前会话额外可引用的上下文，不等于号主本人事实', bg.mountedKnowledgeLines.length > 0 ? bg.mountedKnowledgeLines : []),
    section('最近备注', '最近几次你的简短备注', bg.recentThinkNotes.length > 0 ? bg.recentThinkNotes : ['暂无']),
    section('补审提醒', undefined, input.background.deferredCount > 0 ? [
      `你上一轮生成期间又新进来了 ${input.background.deferredCount} 条消息。`,
      '之前那轮没发出去的想法一律作废。',
      '这次必须只根据当前完整聊天记录重新判断，避免重复回复或回复过期结论。',
    ] : []),
    section('共同体记忆', '全局共同体记忆', [bg.globalIdentityContext || '暂无']),
    section('图片提示', undefined, bg.imageHint ? [bg.imageHint] : []),
    section('短ID说明', undefined, [
      '上下文里形如 [#A1B2] 的四位字母数字就是消息短ID。'
        + '需要引用上下文里的某条具体消息时，优先使用这个短ID：'
        + 'send_message 可传 reply_to_id，recall_message 可传 message_ref。',
    ]),
  ].join('');
}
