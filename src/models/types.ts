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
  /**
   * true = when thinkingLevel is 'off', explicitly send `thinking: {type:'disabled'}`
   * instead of omitting the field. Needed by gateways whose models think by
   * default (glm-5.3 eats max_tokens on a thinking block and returns EMPTY
   * content otherwise); harmful on relays that reject the thinking param
   * (apipp opus) — hence opt-in per upstream.
   */
  thinking_disabled?: boolean;
  /**
   * true = 请求体不带 temperature 字段。有的中转渠道按上游型号弃用了该参数
   * （apipp 的 claude-haiku-5-5 带 temperature 直接 400「该模型已弃用
   * temperature 参数」），只能整个字段不发。
   */
  drop_temperature?: boolean;
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
  /** See Upstream.thinking_disabled. */
  thinkingDisabled: boolean;
  /** See Upstream.drop_temperature. */
  dropTemperature: boolean;
}

export interface ChatRequest {
  system: { text: string; cacheControl?: { type: 'ephemeral' } }[];
  messages: import('../chat/types.js').ModelMessage[];
  tools: ToolSchema[];
  temperature: number;
  maxTokens: number;
  thinkingLevel: 'off' | 'low' | 'medium' | 'high';
  signal?: AbortSignal;
  /**
   * 流式开关。协议层改用 SSE 逐块解析，调用方能拿到首 token 时间，
   * 且中转挂起时能提前发现（而非等满 180s 超时）。
   * 默认由上游配置 `stream` 决定；显式置 false 可关。
   */
  stream?: boolean;
  /**
   * 流式增量回调。每收到一段正文调用一次（仅正文，不含工具参数）。
   * 用于首 token 计时与增量输出；不实现则退化为非流式行为。
   */
  onDelta?: (delta: string) => void;
}

export interface ToolSchema {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface ChatReply {
  text: string;
  toolCalls: { id: string; name: string; arguments: Record<string, unknown> }[];
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    /** 提示词缓存命中部分（anthropic cache_read / openai cached_tokens）。 */
    cacheReadTokens?: number;
    /** 提示词缓存写入部分（anthropic cache_creation）。 */
    cacheWriteTokens?: number;
  };
}

export const PROTOCOL_PATHS: Record<Protocol, string> = {
  anthropic: '/v1/messages',
  completions: '/v1/chat/completions',
  responses: '/v1/responses',
};
