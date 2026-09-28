/** Model config types — mirrors legacy data/models_config.json structure. */

export type Protocol = 'anthropic' | 'completions' | 'responses';
export type ChannelStrategy = 'fallback' | 'random' | 'roundrobin' | 'fallback_reset';

export interface Upstream {
  name: string;
  base_url: string;
  api_key: string;
  protocol: Protocol;
  /** Optional per-upstream override of the protocol's message endpoint path
   * (e.g. bigmodel's OpenAI-compatible API lives at /api/paas/v4/chat/completions,
   * not …/v1/chat/completions). */
  messages_path?: string;
}

export interface ChannelModel {
  upstream: string;
  model_id: string;
}

export interface Channel {
  name: string;
  strategy: ChannelStrategy;
  models: ChannelModel[];
}

export interface ResolvedModel {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  messagesPath: string;
  protocol: 'anthropic' | 'openai';
  displayName: string;
  channelName: string;
  upstreamName: string;
}

export interface ChatRequest {
  system: { text: string; cacheControl?: { type: 'ephemeral' } }[];
  messages: import('../chat/types.js').ModelMessage[];
  tools: ToolSchema[];
  temperature: number;
  maxTokens: number;
  thinkingLevel: 'off' | 'low' | 'medium' | 'high';
  signal?: AbortSignal;
}

export interface ToolSchema {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface ChatReply {
  text: string;
  toolCalls: { id: string; name: string; arguments: Record<string, unknown> }[];
  usage?: { inputTokens?: number; outputTokens?: number };
}

export const PROTOCOL_PATHS: Record<Protocol, string> = {
  anthropic: '/v1/messages',
  completions: '/v1/chat/completions',
  responses: '/v1/responses',
};
