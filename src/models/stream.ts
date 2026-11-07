/**
 * SSE streaming support for the three protocols.
 *
 * The legacy Python runtime streamed every reply: first-token latency was
 * visible in groups (the bot "woke up" while the model was still writing) and
 * a hung relay surfaced as an early empty delta instead of a 180s stall.
 *
 * This module owns the wire format only. It accumulates text + tool calls
 * into the same `ChatReply` shape the non-streaming path produces, so callers
 * (orchestrator, usage tracker) need no changes beyond passing `stream: true`.
 *
 * Wire formats:
 *  - Anthropic Messages: named events, `content_block_delta` carries either
 *    text (`text_delta`) or tool-argument fragments (`input_json_delta`).
 *  - OpenAI Chat Completions: `data:` frames with `choices[].delta`, tool
 *    calls arrive as `delta.tool_calls[]` fragments keyed by `index`.
 *  - OpenAI Responses: named events (`response.output_text.delta`,
 *    `response.function_call_arguments.delta`), final `response.completed`.
 */

import type { ChatReply, ResolvedModel } from './types.js';
import { ProtocolError } from './errors.js';

/** A single parsed SSE event. `event` is '' for plain `data:` frames. */
interface SseEvent {
  event: string;
  data: string;
}

/**
 * Read an SSE byte stream into events. Handles CRLF, multi-line `data:` and
 * the `[DONE]` sentinel (returned as data '[DONE]').
 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let eventName = '';
  let dataLines: string[] = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Events are separated by a blank line (\n\n or \r\n\r\n).
      let sep = buffer.search(/\r?\n\r?\n/);
      while (sep !== -1) {
        const raw = buffer.slice(0, sep);
        buffer = buffer.slice(sep + (buffer[sep] === '\r' ? 4 : 2));
        const parsed = parseFrame(raw, eventName, dataLines);
        eventName = '';
        dataLines = [];
        if (parsed !== null) yield parsed;
        sep = buffer.search(/\r?\n\r?\n/);
      }
    }
    const tail = parseFrame(buffer, eventName, dataLines);
    if (tail !== null) yield tail;
  } finally {
    reader.releaseLock();
  }
}

function parseFrame(raw: string, eventName: string, dataLines: string[]): SseEvent | null {
  let event = eventName;
  const data: string[] = [...dataLines];
  for (const line of raw.split(/\r?\n/)) {
    if (line.length === 0) continue;
    if (line.startsWith(':')) continue; // SSE comment / heartbeat
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (data.length === 0) return null;
  return { event, data: data.join('\n') };
}

// ── accumulators ─────────────────────────────────────────────────────────────

interface ToolCallAccumulator {
  id: string;
  name: string;
  argsJson: string;
}

class ReplyAccumulator {
  text = '';
  /** Anthropic thinking blocks — kept out of `text` but surfaced in usage. */
  thinking = '';
  toolCalls: ToolCallAccumulator[] = [];
  inputTokens = 0;
  outputTokens = 0;
  cacheReadTokens = 0;
  cacheWriteTokens = 0;
  stopReason = '';
  sawAnything = false;

  toReply(): ChatReply {
    const toolCalls = this.toolCalls
      .filter((call) => call.name !== '')
      .map((call, index) => {
        let args: Record<string, unknown> = {};
        if (call.argsJson.trim()) {
          try {
            args = JSON.parse(call.argsJson) as Record<string, unknown>;
          } catch {
            args = {};
          }
        }
        return { id: call.id || `call_${index}`, name: call.name, arguments: args };
      });
    return {
      text: this.text,
      toolCalls,
      usage: {
        inputTokens: this.inputTokens || undefined,
        outputTokens: this.outputTokens || undefined,
        cacheReadTokens: this.cacheReadTokens || undefined,
        cacheWriteTokens: this.cacheWriteTokens || undefined,
      },
    };
  }
}

// ── Anthropic ────────────────────────────────────────────────────────────────

function applyAnthropicEvent(event: SseEvent, acc: ReplyAccumulator, onDelta: ((delta: string) => void) | undefined): void {
  if (event.event === 'message_start') {
    const message = safeJson(event.data);
    const usage = (message?.message as Record<string, unknown> | undefined)?.usage as Record<string, unknown> | undefined;
    if (usage) {
      acc.inputTokens = num(usage.input_tokens) ?? 0;
      acc.outputTokens = num(usage.output_tokens) ?? 0;
      acc.cacheReadTokens = num(usage.cache_read_input_tokens) ?? 0;
      acc.cacheWriteTokens = num(usage.cache_creation_input_tokens) ?? 0;
    }
    return;
  }
  if (event.event === 'content_block_start') {
    const block = safeJson(event.data)?.content_block as Record<string, unknown> | undefined;
    if (block?.type === 'tool_use') {
      acc.toolCalls.push({ id: String(block.id ?? ''), name: String(block.name ?? ''), argsJson: '' });
    }
    return;
  }
  if (event.event === 'content_block_delta') {
    const delta = safeJson(event.data)?.delta as Record<string, unknown> | undefined;
    if (!delta) return;
    const type = String(delta.type ?? '');
    if (type === 'text_delta') {
      const text = String(delta.text ?? '');
      if (text) {
        acc.text += text;
        acc.sawAnything = true;
        onDelta?.(text);
      }
    } else if (type === 'thinking_delta') {
      acc.thinking += String(delta.thinking ?? '');
    } else if (type === 'input_json_delta') {
      const last = acc.toolCalls[acc.toolCalls.length - 1];
      if (last) last.argsJson += String(delta.partial_json ?? '');
    }
    return;
  }
  if (event.event === 'message_delta') {
    const payload = safeJson(event.data);
    const delta = payload?.delta as Record<string, unknown> | undefined;
    if (delta?.stop_reason) acc.stopReason = String(delta.stop_reason);
    const usage = payload?.usage as Record<string, unknown> | undefined;
    const outTokens = num(usage?.output_tokens);
    if (outTokens !== undefined) acc.outputTokens = outTokens;
    return;
  }
  if (event.event === 'error') {
    const message = safeJson(event.data);
    const error = (message?.error as Record<string, unknown> | undefined) ?? message;
    throw new ProtocolError(
      `stream error: ${String(error?.message ?? event.data).slice(0, 300)}`,
      undefined,
      event.data.slice(0, 400),
      true,
    );
  }
}

// ── OpenAI Chat Completions ──────────────────────────────────────────────────

function applyCompletionsEvent(event: SseEvent, acc: ReplyAccumulator, onDelta: ((delta: string) => void) | undefined): void {
  if (event.data === '[DONE]') return;
  const payload = safeJson(event.data);
  if (!payload) return;
  if (payload.error) {
    const error = payload.error as Record<string, unknown>;
    throw new ProtocolError(
      `stream error: ${String(error.message ?? event.data).slice(0, 300)}`,
      undefined,
      event.data.slice(0, 400),
      true,
    );
  }
  const usage = payload.usage as Record<string, unknown> | undefined;
  if (usage) {
    acc.inputTokens = num(usage.prompt_tokens) ?? acc.inputTokens;
    acc.outputTokens = num(usage.completion_tokens) ?? acc.outputTokens;
    const details = usage.prompt_tokens_details as Record<string, unknown> | undefined;
    acc.cacheReadTokens = num(details?.cached_tokens) ?? acc.cacheReadTokens;
  }
  const choice = (payload.choices as Record<string, unknown>[] | undefined)?.[0];
  if (!choice) return;
  if (choice.finish_reason) acc.stopReason = String(choice.finish_reason);
  const delta = choice.delta as Record<string, unknown> | undefined;
  if (!delta) return;
  const text = String(delta.content ?? '');
  if (text) {
    acc.text += text;
    acc.sawAnything = true;
    onDelta?.(text);
  }
  const reasoning = String((delta as Record<string, unknown>).reasoning_content ?? '');
  if (reasoning) acc.thinking += reasoning;
  const rawCalls = delta.tool_calls as Record<string, unknown>[] | undefined;
  if (rawCalls) {
    for (const raw of rawCalls) {
      const index = Number(raw.index ?? acc.toolCalls.length);
      let slot = acc.toolCalls[index];
      if (!slot) {
        slot = { id: '', name: '', argsJson: '' };
        acc.toolCalls[index] = slot;
      }
      if (raw.id) slot.id = String(raw.id);
      const fn = raw.function as Record<string, unknown> | undefined;
      if (fn?.name) slot.name = String(fn.name);
      if (fn?.arguments) slot.argsJson += String(fn.arguments);
    }
  }
}

// ── OpenAI Responses ─────────────────────────────────────────────────────────

function applyResponsesEvent(event: SseEvent, acc: ReplyAccumulator, onDelta: ((delta: string) => void) | undefined): void {
  if (event.event === 'response.output_text.delta') {
    const delta = String(safeJson(event.data)?.delta ?? '');
    if (delta) {
      acc.text += delta;
      acc.sawAnything = true;
      onDelta?.(delta);
    }
    return;
  }
  if (event.event === 'response.reasoning_summary_text.delta') {
    acc.thinking += String(safeJson(event.data)?.delta ?? '');
    return;
  }
  if (event.event === 'response.output_item.added') {
    const item = safeJson(event.data)?.item as Record<string, unknown> | undefined;
    if (item?.type === 'function_call') {
      acc.toolCalls.push({ id: String(item.call_id ?? item.id ?? ''), name: String(item.name ?? ''), argsJson: '' });
    }
    return;
  }
  if (event.event === 'response.function_call_arguments.delta') {
    const last = acc.toolCalls[acc.toolCalls.length - 1];
    if (last) last.argsJson += String(safeJson(event.data)?.delta ?? '');
    return;
  }
  if (event.event === 'response.completed') {
    const response = safeJson(event.data)?.response as Record<string, unknown> | undefined;
    const usage = response?.usage as Record<string, unknown> | undefined;
    if (usage) {
      acc.inputTokens = num(usage.input_tokens) ?? acc.inputTokens;
      acc.outputTokens = num(usage.output_tokens) ?? acc.outputTokens;
      const details = usage.input_tokens_details as Record<string, unknown> | undefined;
      acc.cacheReadTokens = num(details?.cached_tokens) ?? acc.cacheReadTokens;
    }
    return;
  }
  if (event.event === 'response.failed' || event.event === 'error') {
    const payload = safeJson(event.data);
    const error = (payload?.response as Record<string, unknown> | undefined)?.error as Record<string, unknown> | undefined
      ?? (payload?.error as Record<string, unknown> | undefined);
    throw new ProtocolError(
      `stream error: ${String(error?.message ?? event.data).slice(0, 300)}`,
      undefined,
      event.data.slice(0, 400),
      true,
    );
  }
}

// ── entry point ──────────────────────────────────────────────────────────────

/**
 * Consume an SSE response body into a ChatReply.
 *
 * `isResponses` distinguishes the OpenAI Responses wire format from Chat
 * Completions; both arrive as `data:` frames but with different payload
 * shapes, so they get separate accumulators.
 */
export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  model: ResolvedModel,
  isResponses: boolean,
  onDelta: ((delta: string) => void) | undefined,
): Promise<ChatReply> {
  const acc = new ReplyAccumulator();
  for await (const event of readSse(body)) {
    if (model.protocol === 'anthropic') {
      applyAnthropicEvent(event, acc, onDelta);
    } else if (isResponses) {
      applyResponsesEvent(event, acc, onDelta);
    } else {
      applyCompletionsEvent(event, acc, onDelta);
    }
  }
  const reply = acc.toReply();
  if (reply.text.trim() === '' && reply.toolCalls.length === 0) {
    // An upstream that closes the stream without a single frame is the same
    // "empty shell" fault the non-streaming path retries — surface it the
    // same way so the caller's retry loop sees a transient error.
    throw new ProtocolError(
      `empty content without tool calls (stream stop=${acc.stopReason || '?'})`,
      undefined,
      undefined,
      true,
    );
  }
  return reply;
}

/** Coerce an unknown JSON value to a finite number, or undefined. */
function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function safeJson(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text) as unknown;
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}
