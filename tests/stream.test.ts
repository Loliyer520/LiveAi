import test from 'node:test';
import assert from 'node:assert/strict';
import { consumeSse, readSse } from '../src/models/stream.js';
import { ProtocolError } from '../src/models/errors.js';
import type { ResolvedModel } from '../src/models/types.js';

function sseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[index]!));
      index += 1;
    },
  });
}

const anthropicModel: ResolvedModel = {
  baseUrl: 'https://x',
  apiKey: 'k',
  modelName: 'm',
  messagesPath: '/v1/messages',
  protocol: 'anthropic',
  displayName: 'm',
  channelName: 'c',
  upstreamName: 'u',
  thinkingDisabled: false,
  dropTemperature: false,
};

const openaiModel: ResolvedModel = { ...anthropicModel, protocol: 'openai' };
const responsesModel: ResolvedModel = { ...openaiModel, messagesPath: '/v1/responses' };

test('readSse splits frames on blank lines and keeps event names', async () => {
  const stream = sseStream([
    'event: a\ndata: {"x":1}\n\n',
    'event: b\ndata: line1\ndata: line2\n\n',
    ': heartbeat\n\n',
    'data: [DONE]\n\n',
  ]);
  const events = [];
  for await (const event of readSse(stream)) events.push(event);
  assert.equal(events.length, 3);
  assert.equal(events[0]!.event, 'a');
  assert.equal(events[0]!.data, '{"x":1}');
  assert.equal(events[1]!.event, 'b');
  assert.equal(events[1]!.data, 'line1\nline2');
  assert.equal(events[2]!.data, '[DONE]');
});

test('readSse handles CRLF and frames split across chunk boundaries', async () => {
  const stream = sseStream(['event: a\r\ndata: {"x"', ':1}\r\n\r\n', 'data: tail\r\n\r\n']);
  const events = [];
  for await (const event of readSse(stream)) events.push(event);
  assert.equal(events.length, 2);
  assert.equal(events[0]!.data, '{"x":1}');
  assert.equal(events[1]!.data, 'tail');
});

test('anthropic stream accumulates text and reports deltas', async () => {
  const stream = sseStream([
    'event: message_start\ndata: {"message":{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n',
    'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"你好"}}\n\n',
    'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"世界"}}\n\n',
    'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n',
  ]);
  const deltas: string[] = [];
  const reply = await consumeSse(stream, anthropicModel, false, (d) => deltas.push(d));
  assert.equal(reply.text, '你好世界');
  assert.deepEqual(deltas, ['你好', '世界']);
  assert.equal(reply.usage?.inputTokens, 10);
  assert.equal(reply.usage?.outputTokens, 5);
});

test('anthropic stream accumulates tool_use input_json_delta fragments', async () => {
  const stream = sseStream([
    'event: content_block_start\ndata: {"content_block":{"type":"tool_use","id":"tu_1","name":"send_message"}}\n\n',
    'event: content_block_delta\ndata: {"delta":{"type":"input_json_delta","partial_json":"{\\"text\\":"}}\n\n',
    'event: content_block_delta\ndata: {"delta":{"type":"input_json_delta","partial_json":"\\"hi\\"}"}}\n\n',
  ]);
  const reply = await consumeSse(stream, anthropicModel, false, undefined);
  assert.equal(reply.toolCalls.length, 1);
  assert.equal(reply.toolCalls[0]!.name, 'send_message');
  assert.equal(reply.toolCalls[0]!.id, 'tu_1');
  assert.deepEqual(reply.toolCalls[0]!.arguments, { text: 'hi' });
});

test('openai completions stream accumulates text, usage and tool calls', async () => {
  const stream = sseStream([
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"stay_silent","arguments":"{\\"a\\":"}}]}}]}\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}\n\n',
    'data: {"usage":{"prompt_tokens":7,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}}}\n\n',
    'data: [DONE]\n\n',
  ]);
  const reply = await consumeSse(stream, openaiModel, false, undefined);
  assert.equal(reply.text, 'Hello');
  assert.equal(reply.toolCalls.length, 1);
  assert.equal(reply.toolCalls[0]!.name, 'stay_silent');
  assert.deepEqual(reply.toolCalls[0]!.arguments, { a: 1 });
  assert.equal(reply.usage?.inputTokens, 7);
  assert.equal(reply.usage?.outputTokens, 3);
  assert.equal(reply.usage?.cacheReadTokens, 2);
});

test('openai responses stream accumulates output text and function call', async () => {
  const stream = sseStream([
    'event: response.output_text.delta\ndata: {"delta":"abc"}\n\n',
    'event: response.output_item.added\ndata: {"item":{"type":"function_call","call_id":"fc1","name":"send_message"}}\n\n',
    'event: response.function_call_arguments.delta\ndata: {"delta":"{\\"x\\":2}"}\n\n',
    'event: response.completed\ndata: {"response":{"usage":{"input_tokens":5,"output_tokens":9}}}\n\n',
  ]);
  const reply = await consumeSse(stream, responsesModel, true, undefined);
  assert.equal(reply.text, 'abc');
  assert.equal(reply.toolCalls.length, 1);
  assert.deepEqual(reply.toolCalls[0]!.arguments, { x: 2 });
  assert.equal(reply.usage?.outputTokens, 9);
});

test('empty stream raises a transient ProtocolError like the non-streaming path', async () => {
  const stream = sseStream(['data: [DONE]\n\n']);
  await assert.rejects(
    () => consumeSse(stream, openaiModel, false, undefined),
    (error: unknown) => error instanceof ProtocolError && error.transient === true,
  );
});

test('stream error event surfaces as ProtocolError', async () => {
  const stream = sseStream(['event: error\ndata: {"error":{"message":"boom"}}\n\n']);
  await assert.rejects(
    () => consumeSse(stream, anthropicModel, false, undefined),
    (error: unknown) => error instanceof ProtocolError && /boom/.test(error.message),
  );
});
