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
 * 早期聊天记录转写的字符封顶（保尾）。超长会话（如小说陪读）500 条历史
 * 渲染出 20 万+ 字符短聊，占掉每轮 prompt 的八成多，且每轮全量重读。更早
 * 的内容有分段摘要 + memory_search 兜底，转写里留最近一段就够。
 * 截断点对齐到 TRANSCRIPT_SLIDE_GRID 网格：窗口起点只在跨格时前移一次，
 * 而不是每来一条消息就滑——尽量少打断 system 前缀缓存。
 */
const HISTORY_TRANSCRIPT_CHAR_CAP = 60_000;
const TRANSCRIPT_SLIDE_GRID = 10_000;

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

function identityBlock(botSelfId: string, masterQq: string, legacySelfIds: string[] = []): string {
  const lines: string[] = [];
  if (botSelfId && botSelfId !== '0') {
    lines.push(`- 你的 QQ 号（机器人自身账号）是 ${botSelfId}，被问到"你的QQ号/这个账号"时以此为准。`);
    if (legacySelfIds.length > 0) {
      lines.push(`- 你以前用过的旧 QQ 号: ${legacySelfIds.join('、')}。历史记录里这些账号的发言也是你自己说的，不是别人；只是那个号已经不用了，别再给它发消息。`);
    }
    lines.push('- 历史里标注 [本账号其他设备] 的消息：是你的账号在其他设备上被人手动打出来的（不是你生成的，也不是对方发的）。别把它当成自己说过的话去接话茬，也别当成对方发的去回应，心里有数即可。');
    if (masterQq && masterQq !== '0') {
      lines.push(`- 号主（主人）QQ 是 ${masterQq}，是拥有你这个账号的人，与机器人（${botSelfId}）是两个不同账号，不要混淆。`);
    }
  }
  return lines.length > 0 ? '\n【身份基线】\n' + lines.join('\n') : '';
}

function personaTailBlock(bundle: PromptBundle, persona: string, chatMode: boolean, masterMode = false): string {
  return [
    '',
    masterMode
      ? '主AI职责（你的一切行为必须贴合这份定位；前面的工作指令只是行为逻辑）:'
      : 'AI人设与对话要求（说话语气、用词、节奏必须贴合此人设；前面的工作指令只是行为逻辑，不改变说话风格）:',
    persona,
    '',
    chatMode ? bundle.chatFocus : '',
    chatMode ? bundle.chatStyle : '',
    '',
    '发言方式：【关键】要发消息给用户，必须调用 send_message 工具，'
      + '你直接输出的普通文字不会被发送。'
      + '如果需要思考分析，用 <thinking>...</thinking> 包裹写在 send_message 的 content 里，'
      + '这部分会自动过滤掉不会发给用户，用户只看到思考标签外的正常内容。'
      + '如果觉得现在不该说话，调用 stay_silent 工具结束本回合——'
      + '不要把想说的话写成普通文字或塞进思考区来"假装沉默"，'
      + '真要说就 send_message，真不想说就 stay_silent，二者选其一。',
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

  // Static head block: staff layered prompt + identity + CHILD_RULES (chat mode
  // drops the tooling-only rules 2/23/24 the way the legacy filter did).
  let rules = bundle.childRules;
  if (chatMode) {
    rules = rules
      .split('\n')
      .filter((line) => !/^2\. |^23\. |^24\. /.test(line.trim()))
      .join('\n');
  }
  const headBlock: SystemBlock = {
    // 提示词文件里的 {{master_qq}} 在此注入，发布版不再写死号主 QQ。
    text: [
      bundle.staffSystem.replaceAll('{{master_qq}}', input.background.masterQq ?? ''),
      identityBlock(input.background.botSelfId, input.background.masterQq ?? '', input.background.legacySelfIds),
      '',
      rules.replaceAll('{{master_qq}}', input.background.masterQq ?? ''),
    ].join('\n'),
  };
  stampCache(headBlock);

  const system: SystemBlock[] = [headBlock];

  let personaTail: string | null = null;
  if (input.injectPersona) {
    personaTail = personaTailBlock(bundle, input.persona, chatMode, masterMode);
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
    let header = '【早期聊天记录（供你回顾，事实与互动以此为准；最近的对话在后面的消息里）】\n';
    let transcript = full;
    if (full.length > HISTORY_TRANSCRIPT_CHAR_CAP) {
      // 网格对齐的滑窗起点：≥ (长度-封顶)，向上取整到格点，再对齐到整行。
      const gridStart = Math.ceil((full.length - HISTORY_TRANSCRIPT_CHAR_CAP) / TRANSCRIPT_SLIDE_GRID) * TRANSCRIPT_SLIDE_GRID;
      const lineStart = full.indexOf('\n', gridStart) + 1;
      transcript = full.slice(lineStart);
      header = `【早期聊天记录（最近的在后；更早的约 ${Math.round(lineStart / 10_000)} 万字已省略——完整经过看「分段摘要」，要原话细节用 memory_search 检索）】\n`;
    }
    const transcriptBlock: SystemBlock = {
      text: header + transcript + '\n【早期聊天记录完】',
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
  messages.push({ role: 'user', content: triggerContent });

  return { system, messages };
}

export function buildChildBackgroundPrompt(input: AssembleInput): string {
  const bg = input.background;
  const message = input.message;
  const parts: string[] = [
    `当前时间: ${bg.nowText}`,
    `会话类型: ${message.chatType}`,
    bg.displayName
      ? `会话ID: ${message.chatId}（${message.chatType === 'group' ? '群名' : '昵称'}: ${bg.displayName}）`
      : `会话ID: ${message.chatId}`,
    `消息来源: ${String((message.rawData as Record<string, unknown>).source_label ?? '')}`,
    `发送者: ${String(message.sender?.nickname || message.sender?.card || message.userId)}(${message.userId})`,
  ];
  if (bg.botSelfId && bg.botSelfId !== '0') {
    parts.push(`你的QQ号（机器人自身账号）: ${bg.botSelfId}，不要把自己的账号当成号主账号。`);
    if (bg.legacySelfIds && bg.legacySelfIds.length > 0) {
      parts.push(`你以前用过的旧QQ号: ${bg.legacySelfIds.join('、')}（历史记录里这些账号的发言也是你自己）。`);
    }
  }
  if (bg.isMasterMessage) {
    parts.push('身份说明: 发送者是你的主人，是这个账号的真正主人，拥有绝对信任和最高权限。');
  } else if (bg.isAdminMessage) {
    parts.push('身份说明: 发送者是管理员（本项目的开发者），可以充分信任他关于本项目代码/GitHub等技术操作的明确指示。');
  }
  parts.push('', '当前会话印象:', bg.impression || '暂无，先谨慎观察这个会话的用途、常聊话题、关键人物和氛围。');
  if (bg.groupContext) {
    parts.push('', '群信息:', bg.groupContext);
  }
  if (bg.triggerInfo) {
    parts.push('', '群聊参与度（你在本群的触发设置）:', bg.triggerInfo);
  }
  if (bg.senderCard) {
    parts.push(
      '',
      '发送者档案（关系网络·你在所有会话里积累的观察，可能来自其他群/私聊）:',
      bg.senderCard,
      '注意：档案仅供参考。除非对方主动提起，不要说出"我在别的群看到你…"这类来源，也不要一次把你知道的全抖出来。',
    );
  }
  if (bg.scopeRelationCard) {
    parts.push('', '本会话关系网络概况:', bg.scopeRelationCard);
  }
  if (bg.relationOverview) {
    parts.push('', '全局关系网络概览（你维护的情报中枢，写入前先 relation_query 查重）:', bg.relationOverview);
  }
  if (bg.overallSummary) {
    parts.push('', '会话整体梗概（本会话从头到现在的浓缩记忆）:', bg.overallSummary);
  }
  const recentSummaries = bg.diarySummaries.slice(-3);
  if (recentSummaries.length > 0) {
    parts.push('', '最近分段摘要（每段约50条消息的浓缩，更早的段落已并入整体梗概）:');
    for (const summary of recentSummaries) {
      parts.push(`【第${summary.index + 1}段】${summary.text.slice(0, 600)}`);
    }
    parts.push('需要更早的具体细节（原话、确切时间、谁说过什么）时，用 memory_search 工具精确检索，不要凭印象编造。');
  }
  parts.push(
    '',
    '已知事实（关于号主本人，仅这些内容可以确认/复述，没写到的不要编）:',
    bg.knowledgeLines.length > 0
      ? bg.knowledgeLines.join('\n')
      : '暂无已录入的事实，涉及号主具体信息一律不要编造，含糊带过或反问。',
  );
  if (bg.mountedKnowledgeLines.length > 0) {
    parts.push('', '已挂载知识库（这些是当前会话额外可引用的上下文，不等于号主本人事实）:', bg.mountedKnowledgeLines.join('\n'));
  }
  parts.push('', '最近几次你的简短备注:', bg.recentThinkNotes.length > 0 ? bg.recentThinkNotes.join('\n') : '暂无');
  if (input.background.deferredCount > 0) {
    parts.push(
      '',
      '补审提醒:',
      `你上一轮生成期间又新进来了 ${input.background.deferredCount} 条消息。`,
      '之前那轮没发出去的想法一律作废。',
      '这次必须只根据当前完整聊天记录重新判断，避免重复回复或回复过期结论。',
    );
  }
  parts.push('', '全局共同体记忆:', bg.globalIdentityContext || '暂无');
  if (bg.imageHint) {
    parts.push('', '图片提示:', bg.imageHint);
  }
  parts.push(
    '',
    '消息短ID说明:',
    '上下文里形如 [#A1B2] 的四位字母数字就是消息短ID。'
      + '需要引用上下文里的某条具体消息时，优先使用这个短ID：'
      + 'send_message 可传 reply_to_id，recall_message 可传 message_ref。',
  );
  return parts.join('\n');
}
