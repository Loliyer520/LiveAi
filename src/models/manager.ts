/**
 * Model manager — upstreams / channels / roles with pick strategies.
 * Ported from legacy core/model_manager.py (the parts the chat runtime used).
 *
 * roles support the tiered fallback chain: tiered_chat/exec/decision →
 * tiered → main, so `/role set tiered <channel>` covers all three tiers.
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';
import {
  PROTOCOL_PATHS,
  type Channel,
  type ChannelStrategy,
  type Protocol,
  type ResolvedModel,
  type Upstream,
} from './types.js';

const VALID_STRATEGIES: ChannelStrategy[] = ['fallback', 'random', 'roundrobin', 'fallback_reset'];

const TIER_ROLE_FALLBACK: Record<string, string> = {
  tiered_chat: 'tiered',
  tiered_exec: 'tiered',
  tiered_decision: 'tiered',
};

interface ModelsConfig {
  upstreams: Upstream[];
  channels: Channel[];
  roles: Record<string, string>;
}

function emptyConfig(): ModelsConfig {
  return { upstreams: [], channels: [], roles: {} };
}

function normalizeStrategy(value: unknown): ChannelStrategy {
  const name = String(value ?? 'fallback').toLowerCase() as ChannelStrategy;
  return VALID_STRATEGIES.includes(name) ? name : 'fallback';
}

export function normalizeProtocol(value: unknown): Protocol {
  const name = String(value ?? '').trim().toLowerCase();
  const aliases: Record<string, Protocol> = {
    anthropic: 'anthropic',
    messages: 'anthropic',
    completions: 'completions',
    completion: 'completions',
    chat: 'completions',
    chat_completions: 'completions',
    openai: 'completions',
    responses: 'responses',
    response: 'responses',
  };
  return aliases[name] ?? 'anthropic';
}

export class ModelManager {
  private config: ModelsConfig = emptyConfig();
  private fallbackIndexes = new Map<string, number>();
  private requestFallbackIndexes = new Map<string, number>();
  private roundRobinCounters = new Map<string, number>();

  constructor(private readonly configPath: string) {}

  async load(): Promise<void> {
    try {
      const raw = JSON.parse(await readFile(this.configPath, 'utf-8')) as Partial<ModelsConfig>;
      this.config = {
        upstreams: raw.upstreams ?? [],
        channels: raw.channels ?? [],
        roles: raw.roles ?? {},
      };
    } catch {
      this.config = emptyConfig();
    }
  }

  async save(): Promise<void> {
    await mkdir(dirname(this.configPath), { recursive: true });
    await writeFile(this.configPath, JSON.stringify(this.config, null, 2), 'utf-8');
  }

  /** Pick the current model for a role, advancing round-robin counters. */
  getModelForRole(role: string, advance = true): ResolvedModel | null {
    const channelName = this.resolveRoleChannel(role);
    if (channelName === null) return null;
    const channel = this.findChannel(channelName);
    if (!channel) return null;
    return this.pickFromChannel(channel, advance);
  }

  /** Report a failure so `fallback` channels advance to the next model. */
  notifyFailure(role: string): void {
    const channelName = this.resolveRoleChannel(role);
    if (channelName === null) return;
    const channel = this.findChannel(channelName);
    if (!channel || channel.models.length === 0) return;
    const strategy = normalizeStrategy(channel.strategy);
    if (strategy === 'fallback') {
      this.fallbackIndexes.set(channel.name, (this.fallbackIndexes.get(channel.name) ?? 0) + 1);
    } else if (strategy === 'fallback_reset') {
      this.requestFallbackIndexes.set(channel.name, (this.requestFallbackIndexes.get(channel.name) ?? 0) + 1);
    }
  }

  resolveRoleChannel(role: string): string | null {
    const wanted = String(role ?? '').trim();
    let channelName = String(this.config.roles[wanted] ?? '').trim();
    if (!channelName) {
      const fallback = TIER_ROLE_FALLBACK[wanted];
      if (fallback) channelName = String(this.config.roles[fallback] ?? '').trim();
      if (!channelName && wanted !== 'main') {
        channelName = String(this.config.roles.main ?? '').trim();
      }
    }
    return channelName || null;
  }

  private findChannel(name: string): Channel | undefined {
    return this.config.channels.find((channel) => channel.name === name);
  }

  private findUpstream(name: string): Upstream | undefined {
    return this.config.upstreams.find((upstream) => upstream.name === name);
  }

  private pickFromChannel(channel: Channel, advance: boolean): ResolvedModel | null {
    const models = channel.models ?? [];
    if (models.length === 0) return null;
    const strategy = normalizeStrategy(channel.strategy);
    let entry: { upstream: string; model_id: string };
    if (strategy === 'random') {
      entry = models[Math.floor(Math.random() * models.length)];
    } else if (strategy === 'roundrobin') {
      const index = (this.roundRobinCounters.get(channel.name) ?? 0) % models.length;
      if (advance) this.roundRobinCounters.set(channel.name, index + 1);
      entry = models[index];
    } else if (strategy === 'fallback_reset') {
      entry = models[(this.requestFallbackIndexes.get(channel.name) ?? 0) % models.length];
    } else {
      entry = models[(this.fallbackIndexes.get(channel.name) ?? 0) % models.length];
    }

    const upstream = this.findUpstream(String(entry.upstream ?? '').trim());
    const modelId = String(entry.model_id ?? '').trim();
    if (!upstream || !modelId) return null;
    const protocol = normalizeProtocol(upstream.protocol);
    return {
      baseUrl: String(upstream.base_url ?? '').trim().replace(/\/+$/, ''),
      apiKey: String(upstream.api_key ?? '').trim(),
      modelName: modelId,
      messagesPath: String(upstream.messages_path ?? '').trim() || PROTOCOL_PATHS[protocol],
      protocol: protocol === 'anthropic' ? 'anthropic' : 'openai',
      displayName: `${upstream.name}/${modelId}`,
      channelName: channel.name,
      upstreamName: upstream.name,
    };
  }

  summaryText(): string {
    const parts = this.config.channels.map((channel) => {
      const models = channel.models.map((model) => `${model.upstream}/${model.model_id}`).join(' → ');
      return `${channel.name} [${normalizeStrategy(channel.strategy)}]: ${models}`;
    });
    const roles = Object.entries(this.config.roles).map(([role, channel]) => `${role}=${channel}`).join(', ');
    return `渠道:\n${parts.join('\n') || '（无）'}\n角色: ${roles || '（无）'}`;
  }
}
