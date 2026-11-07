/**
 * Protocol adapters: Anthropic Messages / OpenAI Chat Completions / OpenAI
 * Responses, plus the retry semantics the legacy runtime depended on:
 *
 *  - transient errors (network / 5xx / 429 / timeout / empty content) retry
 *    up to 3 times before surfacing — empty-shell replies (no status) get 5;
 *  - 400/422 only re-tried once without an optional field the upstream
 *    explicitly complained about (stream_options / reasoning extensions);
 *  - empty content without tool calls raises (never silently returns);
 *  - Anthropic extended thinking forces temperature=1.0.
 */

import { mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import type { ChatReply, ChatRequest, ResolvedModel, ToolSchema } from './types.js';
import type { ContentBlock, ModelMessage } from '../chat/types.js';
import { sanitizeUnicode } from '../unicode.js';
import { consumeSse } from './stream.js';
import { ProtocolError } from './errors.js';

export { ProtocolError };

const TRANSIENT_STATUS = new Set([408, 409, 429, 500, 502, 503, 504, 529]);

/** 单次模型调用的硬超时（大上下文+推理型模型 120s 都不够就真出事了）。 */
const MODEL_FETCH_TIMEOUT_MS = 180_000;

export async function completeChat(model: ResolvedModel, request: ChatRequest): Promise<ChatReply> {
  const url = `${model.baseUrl}${model.messagesPath}`;
  let attempt = 0;
  let dropStreamOptions = false;
  let dropReasoning = false;
  let dropStream = false;
  // 总时长预算：单次 180s × 最多 5 次 = 15 分钟会把 scope actor 钉死、
  // 全场沉默（2026-10-02 之鉴的另一半）。超过预算后不再重试，直接抛出。
  const deadline = Date.now() + 6 * 60_000;

  for (;;) {
    attempt += 1;
    try {
      return await callOnce(url, model, request, { dropStreamOptions, dropReasoning, dropStream });
    } catch (error) {
      if (!(error instanceof ProtocolError)) throw error;
      const optional400 =
        error.status !== undefined && (error.status === 400 || error.status === 422);
      // 上游抱怨的若是 stream_options，那是"参数名不被接受"，与"不支持流式"
      // 是两件事——前者只摘掉 stream_options 保留流式能力。
      if (optional400 && !dropStreamOptions && /stream_options/i.test(error.body ?? '') && Date.now() < deadline) {
        dropStreamOptions = true;
        continue;
      }
      // 抱怨的是 stream 本身（"stream not supported" / "unknown parameter:
      // stream"）——整个退回非流式，功能不降级，只是没有增量输出。
      const complainsAboutStream = /stream/i.test(error.body ?? '')
        && !/stream_options/i.test(error.body ?? '');
      if (optional400 && !dropStream && complainsAboutStream && Date.now() < deadline) {
        dropStream = true;
        continue;
      }
      if (optional400 && !dropReasoning && /reasoning/i.test(error.body ?? '') && Date.now() < deadline) {
        dropReasoning = true;
        continue;
      }
      // 空壳回复（无 status）比真限流更常见地一次性抽风——多给它两次机会，
      // 不然主AI回合会死在「连续 3 个空壳」上（gpt-6-sol 经中转实测会间歇吐空）。
      const maxAttempts = error.status === undefined ? 5 : 3;
      if (error.transient && attempt < maxAttempts && Date.now() < deadline) continue;
      // 空壳重试耗尽 = 粘滞故障（中转可能对同 payload 吐缓存空壳）——
      // 把原始请求落盘，事后可以 curl 重放定位（2026-10-02 雪球回合连崩之鉴）。
      // 只给真·空壳落盘：普通网络错误（超时/断连）没有重放价值，曾经把
      // data/state/debug 堆到 95MB/400 个文件，所以要带轮换上限。
      if (error.status === undefined && error.message.startsWith('empty content') && request.signal?.aborted !== true) {
        void dumpEmptyShellRequest(url, model, request);
      }
      throw error;
    }
  }
}

/** 空壳故障的原始请求落盘（best-effort，绝不阻塞/影响主流程）。 */
const EMPTY_SHELL_DUMP_KEEP = 50;

async function dumpEmptyShellRequest(url: string, model: ResolvedModel, request: ChatRequest): Promise<void> {
  try {
    const dir = 'data/state/debug';
    await mkdir(dir, { recursive: true });
    const body = {
      at: new Date().toISOString(),
      url,
      model: model.displayName,
      request: { ...request, signal: undefined },
    };
    await writeFile(`${dir}/empty-shell-${Date.now()}.json`, JSON.stringify(body), 'utf-8');
    // 轮换：只留最近 50 份，多余的从旧到新删——转中长期抽风时曾堆出 400 个文件。
    const dumps = (await readdir(dir))
      .filter((name) => /^empty-shell-\d+\.json$/.test(name))
      .sort();
    for (const stale of dumps.slice(0, Math.max(0, dumps.length - EMPTY_SHELL_DUMP_KEEP))) {
      await rm(`${dir}/${stale}`, { force: true });
    }
  } catch {
    // 调试落盘失败不值得任何动静
  }
}

interface CallOptions {
  dropStreamOptions: boolean;
  dropReasoning: boolean;
  /** 上游不接受 stream 参数时置位——回退到非流式请求。 */
  dropStream: boolean;
}

async function callOnce(
  url: string,
  model: ResolvedModel,
  request: ChatRequest,
  options: CallOptions,
): Promise<ChatReply> {
  const isAnthropic = model.protocol === 'anthropic';
  const body = isAnthropic
    ? buildAnthropicBody(model, request, options)
    : buildOpenAiBody(model, request, options, url);

  // 流式开关：显式 false 关闭；否则默认开启（legacy 运行时就是流式的）。
  // 中转不支持 stream 时会 400 → 由 completeChat 的重试逻辑回退到非流式。
  const useStream = request.stream !== false && !options.dropStream;
  if (useStream) {
    if (isAnthropic) body.stream = true;
    else if (model.messagesPath.endsWith('/responses')) body.stream = true;
    else {
      body.stream = true;
      // OpenAI 兼容端点默认不在流里回 usage——显式要，否则面板的 token 统计
      // 在流式下全是 0。中转不支持时会 400 抱怨，由上层回退重试。
      if (!options.dropStreamOptions) body.stream_options = { include_usage: true };
    }
  }

  // 硬性超时：中转偶尔把连接挂住不死不回（2026-10-02 主AI回合因此卡死、
  // 作用域 actor 被占住，全场沉默）——没有它 await 会等到天荒地老。
  const timeoutSignal = AbortSignal.timeout(MODEL_FETCH_TIMEOUT_MS);
  const signal = request.signal !== undefined
    ? AbortSignal.any([request.signal, timeoutSignal])
    : timeoutSignal;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: isAnthropic
        ? {
            'content-type': 'application/json',
            'x-api-key': model.apiKey,
            'anthropic-version': '2023-06-01',
          }
        : {
            'content-type': 'application/json',
            authorization: `Bearer ${model.apiKey}`,
          },
      // A persisted prompt may contain a lone UTF-16 surrogate (for example
      // from an older truncation). Escaping it as \uD800 makes some upstream
      // JSON parsers reject the whole request, so sanitize every outbound string.
      body: JSON.stringify(body, (_key, value) => typeof value === 'string' ? sanitizeUnicode(value) : value),
      signal,
    });
  } catch (error) {
    // Caller aborts (shutdown / reload / superseded turn) must never be
    // retried: retrying can take minutes and the result is already unwanted.
    // Our own 180s timeout stays transient — a fresh attempt may succeed.
    const abortedByCaller = request.signal?.aborted === true;
    throw new ProtocolError(
      `network error: ${error instanceof Error ? error.message : String(error)}`,
      undefined,
      undefined,
      !abortedByCaller,
    );
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new ProtocolError(
      `upstream ${response.status}: ${text.slice(0, 400)}`,
      response.status,
      text,
      TRANSIENT_STATUS.has(response.status),
    );
  }

  // 流式响应：body 是 SSE，按事件累积成与非流式相同的 ChatReply。
  if (useStream && response.body !== null) {
    const isResponses = !isAnthropic && model.messagesPath.endsWith('/responses');
    const contentType = response.headers.get('content-type') ?? '';
    // 有些中转对 stream 请求仍回 application/json（非流式载荷）——
    // 按 content-type 判断，别硬解 SSE。
    if (contentType.includes('text/event-stream')) {
      return await consumeSse(response.body, model, isResponses, request.onDelta);
    }
  }

  const rawText = await response.text();
  let data: unknown;
  try {
    data = JSON.parse(rawText);
  } catch {
    throw new ProtocolError('invalid JSON response', undefined, rawText.slice(0, 400), true);
  }
  return parseReply(data, model, request.tools);
}

// ── Anthropic Messages ──────────────────────────────────────────────────────

const ANTHROPIC_THINKING_BUDGET: Record<string, number> = {
  low: 4096,
  medium: 8192,
  high: 16384,
};

function buildAnthropicBody(model: ResolvedModel, request: ChatRequest, options: CallOptions): Record<string, unknown> {
  const maxTokens = Math.max(1, Math.trunc(Number(request.maxTokens) || 0));
  const thinkingEnabled = request.thinkingLevel !== 'off' && !options.dropReasoning;
  // budget 必须严格小于 max_tokens；且 anthropic 的最小预算是 1024。
  // max_tokens 太小时（例如 1024 以下的短任务）无法同时满足两条 → 直接关思考，
  // 否则请求会被上游 400 拒绝。
  const thinkingBudget = Math.min(
    ANTHROPIC_THINKING_BUDGET[request.thinkingLevel] ?? 4096,
    maxTokens - 1,
  );
  const useThinking = thinkingEnabled && thinkingBudget >= 1024;
  const body: Record<string, unknown> = {
    model: model.modelName,
    max_tokens: maxTokens,
    messages: renderAnthropicMessages(request.messages),
    tools: request.tools.map((tool) => ({
      name: tool.name,
      description: tool.description,
      input_schema: tool.parameters,
    })),
  };
  if (useThinking) {
    body.thinking = {
      type: 'enabled',
      budget_tokens: thinkingBudget,
    };
    if (!model.dropTemperature) body.temperature = 1.0; // required with extended thinking
  } else {
    if (model.thinkingDisabled) body.thinking = { type: 'disabled' }; // glm-5.3 thinks by default otherwise
    if (!model.dropTemperature) body.temperature = request.temperature;
  }
  if (request.system.length > 0) {
    body.system = request.system.map((block) => ({
      type: 'text',
      text: block.text,
      ...(block.cacheControl ? { cache_control: block.cacheControl } : {}),
    }));
  }
  return body;
}

function renderAnthropicMessages(messages: ModelMessage[]): Record<string, unknown>[] {
  const rendered: Record<string, unknown>[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      let content = renderAnthropicContent(message.content);
      if ((message as { cacheMark?: boolean }).cacheMark === true) {
        // 滚动缓存断点：assemble 打在历史尾部前几条，命中提示词缓存。
        if (typeof content === 'string') content = [{ type: 'text', text: content }];
        const last = content[content.length - 1];
        if (last) last.cache_control = { type: 'ephemeral' };
      }
      rendered.push({ role: 'user', content });
    } else if (message.role === 'assistant') {
      const content: Record<string, unknown>[] = [];
      if (typeof message.content === 'string' && message.content.trim()) {
        content.push({ type: 'text', text: message.content });
      }
      for (const call of message.toolCalls ?? []) {
        content.push({ type: 'tool_use', id: call.id, name: call.name, input: call.arguments });
      }
      if ((message as { cacheMark?: boolean }).cacheMark === true && content.length > 0) {
        content[content.length - 1].cache_control = { type: 'ephemeral' };
      }
      rendered.push({ role: 'assistant', content: content.length > 0 ? content : [{ type: 'text', text: '' }] });
    } else {
      rendered.push({
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: message.toolCallId,
            content: renderAnthropicContent(message.content),
          },
        ],
      });
    }
  }
  return rendered;
}

/** Text/image blocks → Anthropic content blocks ({type:'image', source:{url}}). */
function renderAnthropicContent(content: string | ContentBlock[]): string | Record<string, unknown>[] {
  if (typeof content === 'string') return content;
  return content.map((block) =>
    block.type === 'image'
      ? { type: 'image', source: anthropicImageSource(block.url) }
      : {
          type: 'text',
          text: block.text,
          ...(block.cacheControl ? { cache_control: block.cacheControl } : {}),
        },
  );
}

/** data: URLs embed as base64 sources; everything else passes as a url source. */
function anthropicImageSource(url: string): Record<string, unknown> {
  const match = /^data:(image\/[a-z0-9.+-]+);base64,(.*)$/is.exec(url);
  if (match) return { type: 'base64', media_type: match[1], data: match[2] };
  return { type: 'url', url };
}

function parseAnthropicReply(data: Record<string, unknown>): ChatReply {
  const contentBlocks = (data.content ?? []) as Record<string, unknown>[];
  const text = contentBlocks
    .filter((block) => block.type === 'text')
    .map((block) => String(block.text ?? ''))
    .join('');
  const toolCalls = contentBlocks
    .filter((block) => block.type === 'tool_use')
    .map((block) => ({
      id: String(block.id ?? ''),
      name: String(block.name ?? ''),
      arguments: (block.input ?? {}) as Record<string, unknown>,
    }));
  const usage = data.usage as Record<string, number> | undefined;
  const reply: ChatReply = {
    text,
    toolCalls,
    usage: {
      inputTokens: usage?.input_tokens,
      outputTokens: usage?.output_tokens,
      cacheReadTokens: usage?.cache_read_input_tokens,
      // apipp 中转有时只给 5m/1h 分项而不给总计，兜底求和。
      cacheWriteTokens:
        usage?.cache_creation_input_tokens ??
        ((usage?.claude_cache_creation_5_m_tokens ?? 0) + (usage?.claude_cache_creation_1_h_tokens ?? 0) || undefined),
    },
  };
  assertNonEmpty(reply, `stop=${String(data.stop_reason ?? '?')} blocks=${contentBlocks.map((b) => String(b.type)).join('+') || 'none'} out=${usage?.output_tokens ?? '?'}`);
  return reply;
}

// ── OpenAI Chat Completions / Responses ─────────────────────────────────────

function buildOpenAiBody(
  model: ResolvedModel,
  request: ChatRequest,
  options: CallOptions,
  url: string,
): Record<string, unknown> {
  const isResponses = model.messagesPath.endsWith('/responses');
  if (isResponses) return buildResponsesBody(model, request, options);
  const body: Record<string, unknown> = {
    model: model.modelName,
    messages: [
      ...request.system.map((block) => ({ role: 'system', content: block.text })),
      ...renderCompletionsMessages(request.messages),
    ],
    tools: request.tools.map((tool) => ({
      type: 'function',
      function: { name: tool.name, description: tool.description, parameters: tool.parameters },
    })),
    max_tokens: request.maxTokens,
  };
  if (!model.dropTemperature) body.temperature = request.temperature;
  if (model.thinkingDisabled) {
    // 智谱 openai 端点（glm-4.5v 等）默认开思考，off 时要显式关——否则
    // 推理 token 可能吃光 max_tokens 返回空正文（与 anthropic 侧 glm-5.3 同病）。
    body.thinking = { type: 'disabled' };
  } else if (!options.dropReasoning && request.thinkingLevel !== 'off') {
    body.reasoning_effort = request.thinkingLevel === 'high' ? 'high' : request.thinkingLevel === 'medium' ? 'medium' : 'low';
  }
  return body;
}

function buildResponsesBody(model: ResolvedModel, request: ChatRequest, options: CallOptions): Record<string, unknown> {
  const input: Record<string, unknown>[] = [];
  for (const block of request.system) {
    input.push({ role: 'system', content: block.text });
  }
  for (const message of request.messages) {
    if (message.role === 'user') {
      input.push({ role: 'user', content: renderResponsesContent(message.content) });
    } else if (message.role === 'assistant') {
      const text =
        typeof message.content === 'string'
          ? message.content
          : message.content
              .filter((block) => block.type === 'text')
              .map((block) => (block as { text: string }).text)
              .join('\n');
      if (text.trim()) input.push({ role: 'assistant', content: text });
      // function_call 项必须回传——否则后面的 function_call_output 对着
      // 一个从没出现过的 call_id，服务端直接 400（或模型上下文断裂）。
      for (const call of message.toolCalls ?? []) {
        input.push({
          type: 'function_call',
          call_id: call.id,
          name: call.name,
          arguments: JSON.stringify(call.arguments ?? {}),
        });
      }
    } else {
      input.push({
        type: 'function_call_output',
        call_id: message.toolCallId,
        output: typeof message.content === 'string' ? message.content : JSON.stringify(message.content),
      });
    }
  }
  const body: Record<string, unknown> = {
    model: model.modelName,
    input,
    tools: request.tools.map((tool) => ({
      type: 'function',
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    })),
    max_output_tokens: request.maxTokens,
  };
  if (!options.dropReasoning && request.thinkingLevel !== 'off') {
    body.reasoning = { effort: request.thinkingLevel === 'high' ? 'high' : request.thinkingLevel === 'medium' ? 'medium' : 'low' };
  }
  return body;
}

/** Text/image blocks → OpenAI Responses input parts ({type:'input_image'}). */
function renderResponsesContent(content: string | ContentBlock[]): string | Record<string, unknown>[] {
  if (typeof content === 'string') return content;
  return content.map((block) =>
    block.type === 'image'
      ? { type: 'input_image', image_url: block.url }
      : { type: 'input_text', text: block.text },
  );
}

function renderCompletionsMessages(messages: ModelMessage[]): Record<string, unknown>[] {
  const rendered: Record<string, unknown>[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      rendered.push({ role: 'user', content: renderCompletionsContent(message.content) });
    } else if (message.role === 'assistant') {
      const entry: Record<string, unknown> = { role: 'assistant', content: message.content };
      if (message.toolCalls && message.toolCalls.length > 0) {
        entry.tool_calls = message.toolCalls.map((call) => ({
          id: call.id,
          type: 'function',
          function: { name: call.name, arguments: JSON.stringify(call.arguments) },
        }));
      }
      rendered.push(entry);
    } else {
      rendered.push({ role: 'tool', tool_call_id: message.toolCallId, content: message.content });
    }
  }
  return rendered;
}

/** Text/image blocks → OpenAI chat-completions parts ({type:'image_url'}). */
function renderCompletionsContent(content: string | ContentBlock[]): string | Record<string, unknown>[] {
  if (typeof content === 'string') return content;
  return content.map((block) =>
    block.type === 'image'
      ? { type: 'image_url', image_url: { url: block.url } }
      : { type: 'text', text: block.text },
  );
}

function parseReply(data: unknown, model: ResolvedModel, tools: ToolSchema[]): ChatReply {
  // Dispatch on the configured protocol, not URL substrings: a custom
  // messages_path (e.g. bigmodel /api/paas/v4, relays with /v1/messages)
  // can contain neither '/chat/completions' nor '/responses', and a wrong
  // fallback parsed OpenAI payloads as Anthropic (empty text → retries).
  if (model.protocol === 'anthropic') return parseAnthropicReply(data as Record<string, unknown>);
  // Must match buildOpenAiBody's detection exactly, or request/response
  // formats diverge for paths like "/responses/" (misparsed as completions).
  if (model.messagesPath.endsWith('/responses')) return parseResponsesReply(data as Record<string, unknown>);
  return parseCompletionsReply(data as Record<string, unknown>);
}

function parseCompletionsReply(data: Record<string, unknown>): ChatReply {
  const choices = (data.choices ?? []) as Record<string, unknown>[];
  const choice = choices[0] ?? {};
  const message = (choice.message ?? {}) as Record<string, unknown>;
  const rawCalls = (message.tool_calls ?? []) as Record<string, unknown>[];
  const toolCalls = rawCalls.map((call) => {
    const function_ = (call.function ?? {}) as Record<string, unknown>;
    let args: Record<string, unknown> = {};
    try {
      args = JSON.parse(String(function_.arguments ?? '{}')) as Record<string, unknown>;
    } catch {
      args = {};
    }
    return { id: String(call.id ?? ''), name: String(function_.name ?? ''), arguments: args };
  });
  const usage = data.usage as Record<string, number> | undefined;
  const reply: ChatReply = {
    text: String(message.content ?? ''),
    toolCalls,
    usage: {
      inputTokens: usage?.prompt_tokens,
      outputTokens: usage?.completion_tokens,
      cacheReadTokens: (usage?.prompt_tokens_details as Record<string, number> | undefined)?.cached_tokens,
    },
  };
  assertNonEmpty(reply, `finish=${String(((data.choices as Record<string, unknown>[])?.[0]?.finish_reason) ?? '?')} out=${usage?.output_tokens ?? '?'}`);
  return reply;
}

function parseResponsesReply(data: Record<string, unknown>): ChatReply {
  const output = (data.output ?? []) as Record<string, unknown>[];
  const text = output
    .filter((item) => item.type === 'message')
    .map((item) => {
      const content = (item.content ?? []) as Record<string, unknown>[];
      return content.map((block) => String(block.text ?? '')).join('');
    })
    .join('');
  const toolCalls = output
    .filter((item) => item.type === 'function_call')
    .map((item) => {
      let args: Record<string, unknown> = {};
      try {
        args = JSON.parse(String(item.arguments ?? '{}')) as Record<string, unknown>;
      } catch {
        args = {};
      }
      return { id: String(item.call_id ?? item.id ?? ''), name: String(item.name ?? ''), arguments: args };
    });
  const usage = data.usage as Record<string, number> | undefined;
  const reply: ChatReply = {
    text,
    toolCalls,
    usage: {
      inputTokens: usage?.input_tokens,
      outputTokens: usage?.output_tokens,
      cacheReadTokens: (usage?.input_tokens_details as Record<string, number> | undefined)?.cached_tokens,
    },
  };
  assertNonEmpty(reply, `status=${String(data.status ?? '?')} out=${usage?.output_tokens ?? '?'}`);
  return reply;
}

function assertNonEmpty(reply: ChatReply, detail = ''): void {
  if (reply.text.trim() === '' && reply.toolCalls.length === 0) {
    // 附带诊断：stop 原因 + 内容块类型 + 输出 token 数。
    // stop=max_tokens 且 out 打满 = 思考烧光预算；stop=end_turn 且 out≈0 = 中转吐了空壳。
    throw new ProtocolError(`empty content without tool calls${detail ? ` (${detail})` : ''}`, undefined, undefined, true);
  }
}
