# Bot Refactor Architecture

## Current Split

- `adapters/napcat.py`: robot framework adapter. Keep this file isolated so it can be replaced later.
- `modules/satangyun.py`: only handles group `750742812`, including welcome, account query, account bind, normal drawing.
- `modules/ai/runtime.py`: AI runtime and async task queue.
- `modules/ai/repository.py`: AI data access layer built on JSON storage.
- `storage/json_store.py`: storage abstraction. Future database replacement only needs a new storage implementation.
- `services/chat_model.py`: OpenAI-compatible model client.
- `services/*`: external API and utility wrappers.
- `old/`: archived legacy implementation.

## AI Layer Design

### Master AI

- Uses global scope `master:global`.
- Can read group list and friend list through toolbox.
- Receives `notify_master` tasks from child agents.
- Stores long-term notes and relationship clues.

### Child AI

- One child agent per group/private conversation.
- Owns independent context, notes, trigger words and trigger rate.
- Trigger conditions: private chat, `@bot`, keyword hit, or random activation.
- Chat profiles (`flash`/`pro`/`claude`/`opus`) all talk to the Anthropic-native `/v1/messages` API via `pack/anthropic_chat_model.py`; directives use real `tool_use`/`tool_result` (`core/ai_tools_schema.py`) instead of `[[...]]` text markers. `send_message` replaces `[[message]]`/`[[silent]]` (not calling it = silence); plain assistant text is the think-note. Vision parsing and the satangyun welcome message still use the OpenAI-compatible client.

### Background Tasks

- Tasks are queued in asyncio workers.
- Current task framework is ready for:
  - image description
  - forward summary
  - delayed callbacks
  - cross-chat coordination
- This round keeps task handlers as skeletons so later expansion does not require rewiring the app.

## Suggested Next Steps

1. Add real tool-calling protocol instead of text directives.
2. Add image parsing and merged-forward summarizer agents.
3. Add relation graph and user profile extraction.
4. Add delayed callback to specific child agents when tasks finish.
5. Add per-agent config editor for trigger rate, persona and taboo rules.
