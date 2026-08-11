import json
import threading
import time
from dataclasses import dataclass, field

import requests

_SPEED_STATS = None
_SPEED_STATS_LOCK = threading.Lock()


def configure_speed_stats(stats) -> None:
    global _SPEED_STATS
    with _SPEED_STATS_LOCK:
        _SPEED_STATS = stats


def _get_speed_stats():
    global _SPEED_STATS
    if _SPEED_STATS is None:
        with _SPEED_STATS_LOCK:
            if _SPEED_STATS is None:
                from core.model_speed_stats import ModelSpeedStats
                _SPEED_STATS = ModelSpeedStats()
    return _SPEED_STATS

try:
    import tiktoken
except ImportError:  # Token estimation is optional; native upstream usage still works.
    tiktoken = None


# OpenAI 兼容协议（/chat/completions、/responses）下，推理类模型（gpt-5 系、
# deepseek-reasoner 等）开启 reasoning/thinking 时不传 max_tokens/max_output_tokens，
# 上游会按极小的默认值（如 16 token）截断输出——推理内容吃光预算后正文为空，
# 表现为主链路反复报"模型返回空内容（stop_reason='max_tokens'）"。这里给推理
# 请求显式一个较大的输出上限；非推理请求仍保持"不传 = 不限制"的约定。
_OPENAI_REASONING_DEFAULT_MAX_TOKENS = 16384

# Anthropic 原生协议 max_tokens 必填；调用方不限制输出时给到 64K 量级上限。
_ANTHROPIC_DEFAULT_MAX_TOKENS = 65536


@dataclass
class ToolCall:
    call_id: str
    name: str
    input: dict


@dataclass
class AnthropicReply:
    text: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ''
    raw_content: list[dict] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_estimated: bool = False


class AnthropicChatModel:
    def __init__(
        self,
        base_url: str,
        api_key: str = '',
        model_name: str = '[REDACTED]',
        messages_path: str = '/messages',
        request_timeout: int = 120,
    ):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name
        self.messages_path = messages_path if messages_path.startswith('/') else f'/{messages_path}'
        self.request_timeout = request_timeout
        # 上游走 OpenAI 兼容协议（/v1/chat/completions 或 /v1/responses）时，请求侧需做 Anthropic→OpenAI 翻译
        self.is_openai_protocol = '/chat/completions' in (self.messages_path or '') or '/responses' in (self.messages_path or '')
        # 检测是否使用 OpenAI Responses API（GPT 模型优先使用）
        self.is_responses_api = '/responses' in (self.messages_path or '')
        self._speed_stats = _get_speed_stats()
        # 某些模型（Claude Opus 4.5+ 等）已弃用 temperature，带上就 400。首次被拒后记下来，
        # 后续请求直接不传，避免每轮都白跑一次被拒的请求。
        self._temperature_rejected = False

    def with_config(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        messages_path: str | None = None,
        request_timeout: int | None = None,
    ) -> "AnthropicChatModel":
        return AnthropicChatModel(
            base_url=base_url or self.base_url,
            api_key=self.api_key if api_key is None else api_key,
            model_name=model_name or self.model_name,
            messages_path=messages_path or self.messages_path,
            request_timeout=self.request_timeout if request_timeout is None else request_timeout,
        )

    def complete(
        self,
        system_blocks: list[dict] | str,
        messages: list[dict],
        tools: list[dict] | None = None,
        model_name: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        thinking: str | None = None,
    ) -> AnthropicReply | None:
        thinking_level = self._normalize_thinking_level(thinking)
        # Anthropic 协议 max_tokens 必填且必须是整数；解析一次，thinking 回退请求复用同一个值，
        # 否则回退会写回 None，上游报 "max_tokens: Input should be a valid integer"。
        resolved_max_tokens = max_tokens if max_tokens is not None else _ANTHROPIC_DEFAULT_MAX_TOKENS
        if self.is_responses_api:
            headers, payload = self._build_openai_responses_request(
                system_blocks=system_blocks,
                messages=messages,
                tools=tools,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking_level,
            )
        elif self.is_openai_protocol:
            headers, payload = self._build_openai_request(
                system_blocks=system_blocks,
                messages=messages,
                tools=tools,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking_level,
            )
        else:
            headers = {
                'Content-Type': 'application/json',
                'anthropic-version': '2023-06-01',
            }
            if self.api_key:
                # 不同中转对鉴权头要求不一致，两种都带上
                headers['x-api-key'] = self.api_key
                headers['Authorization'] = f'Bearer {self.api_key}'

            payload = {
                'model': model_name or self.model_name,
                'max_tokens': resolved_max_tokens,
                'temperature': temperature,
                'messages': self._normalize_anthropic_messages(messages),
                'stream': True,
            }
            if thinking_level:
                budget_tokens = self._anthropic_thinking_budget(thinking_level)
                payload['thinking'] = {'type': 'enabled', 'budget_tokens': budget_tokens}
                # Anthropic extended thinking requires temperature=1. Keep the
                # caller's max_tokens unchanged; budgets are mapped below the
                # runtime's normal 2048-token output budget.
                payload['temperature'] = 1.0
            normalized_system = self._normalize_anthropic_system(system_blocks)
            if normalized_system:
                payload['system'] = normalized_system
            if tools:
                payload['tools'] = self._normalize_anthropic_tools(tools)
                # 强制工具调用：模型的对外动作只能经工具，普通文字不会发送，放开 auto
                # 会让模型把要发的话写成纯文本而丢失。扩展思考下 Anthropic 只接受
                # auto/none，此时退回 auto，由 runtime 的 loop_guard re-prompt 兜底。
                payload['tool_choice'] = {'type': 'auto'} if thinking_level else {'type': 'any'}
        if self._temperature_rejected:
            payload.pop('temperature', None)
        _request_url = f'{self.base_url}{self.messages_path}'
        _request_start = time.perf_counter()
        first_token_at = None
        response = requests.post(
            _request_url,
            headers=headers,
            json=payload,
            timeout=self.request_timeout,
            stream=True,
        )
        response.encoding = 'utf-8'
        _request_ms = int((time.perf_counter() - _request_start) * 1000)
        print(f'[HTTP] POST {_request_url} status={response.status_code} ms={_request_ms}')
        # Some OpenAI-compatible relays reject the optional stream_options field.
        # Retry exactly once, locally, only when the 400/422 body explicitly names
        # that unsupported parameter. The fallback request then continues through
        # the existing reasoning fallback and outer retry/failover semantics.
        if (
            self.is_openai_protocol
            and 'stream_options' in payload
            and self._stream_options_unsupported(response)
        ):
            fallback_payload = dict(payload)
            fallback_payload.pop('stream_options', None)
            response.close()
            response = requests.post(
                _request_url,
                headers=headers,
                json=fallback_payload,
                timeout=self.request_timeout,
                stream=True,
            )
            response.encoding = 'utf-8'
            print(f'[HTTP] stream_options fallback POST {_request_url} status={response.status_code}')
            payload = fallback_payload
        if response.status_code in (400, 422) and thinking_level:
            # Some compatible relays reject optional reasoning extensions. Retry
            # once without them instead of permanently interrupting the scope.
            fallback_payload = dict(payload)
            fallback_payload.pop('reasoning_effort', None)
            fallback_payload.pop('reasoning', None)
            fallback_payload.pop('thinking', None)
            if not self.is_openai_protocol:
                if self._temperature_rejected:
                    fallback_payload.pop('temperature', None)
                else:
                    fallback_payload['temperature'] = temperature
                fallback_payload['max_tokens'] = resolved_max_tokens
            response.close()
            response = requests.post(
                _request_url,
                headers=headers,
                json=fallback_payload,
                timeout=self.request_timeout,
                stream=True,
            )
            response.encoding = 'utf-8'
            print(f'[HTTP] reasoning fallback POST {_request_url} status={response.status_code}')
            payload = fallback_payload
        # 必须排在 reasoning 回退之后：那条分支会把 temperature 重新写回 payload。
        if 'temperature' in payload and self._temperature_unsupported(response):
            self._temperature_rejected = True
            fallback_payload = dict(payload)
            fallback_payload.pop('temperature', None)
            response.close()
            response = requests.post(
                _request_url,
                headers=headers,
                json=fallback_payload,
                timeout=self.request_timeout,
                stream=True,
            )
            response.encoding = 'utf-8'
            print(f'[HTTP] temperature fallback POST {_request_url} status={response.status_code}')
            payload = fallback_payload
        try:
            if response.status_code >= 400:
                raise RuntimeError(
                    f'anthropic request failed status={response.status_code} body={response.text[:500]}'
                )
            def _mark_first_token():
                nonlocal first_token_at
                if first_token_at is None:
                    first_token_at = time.perf_counter()

            if self.is_responses_api:
                data = self._parse_openai_responses_stream(response, on_first_token=_mark_first_token)
            elif self.is_openai_protocol:
                data = self._parse_openai_stream(response, on_first_token=_mark_first_token)
            else:
                data = self._parse_anthropic_stream(response, on_first_token=_mark_first_token)

            input_tokens, output_tokens = self._extract_native_usage(data)
            usage_estimated = False
            if input_tokens is None or output_tokens is None:
                estimated_input, estimated_output = self._estimate_usage(
                    payload, data, model_name or self.model_name
                )
                input_tokens = estimated_input if input_tokens is None else input_tokens
                output_tokens = estimated_output if output_tokens is None else output_tokens
                usage_estimated = True

            # 优先用 choices 字段判断是否 OpenAI 格式，避免 content 为空列表时误路由到 OpenAI 解析
            if 'output' in data:
                # OpenAI Responses API 格式
                content, stop_reason = self._parse_openai_responses_response(data)
            elif 'choices' in data:
                # OpenAI 格式（choices[0].message）
                content, stop_reason = self._parse_openai_response(data)
            else:
                # Anthropic 格式（content 为 block 列表，允许为空列表或 null）
                raw = data.get('content') or []
                content = [b for b in raw if isinstance(b, dict) and b.get('type') != 'thinking']
                stop_reason = str(data.get('stop_reason') or '')
        finally:
            response.close()

        if self._speed_stats is not None:
            self._speed_stats.record(
                model=model_name or self.model_name,
                endpoint=f'{self.base_url}{self.messages_path}',
                total_ms=(time.perf_counter() - _request_start) * 1000,
                first_token_ms=((first_token_at - _request_start) * 1000) if first_token_at is not None else None,
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            block_type = block.get('type')
            if block_type == 'text':
                text_parts.append(str(block.get('text') or ''))
            elif block_type == 'tool_use':
                tool_input = block.get('input')
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except (ValueError, TypeError):
                        tool_input = {'raw': tool_input}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                tool_calls.append(
                    ToolCall(
                        call_id=str(block.get('id') or ''),
                        name=str(block.get('name') or ''),
                        input=tool_input,
                    )
                )
        result_text = '\n'.join(part for part in text_parts if part).strip()
        if not result_text and not tool_calls:
            # 两种协议解析均未拿到任何内容：主动抛错触发上层重试，不静默返回空对象。
            raise RuntimeError(
                f'模型返回空内容（stop_reason={stop_reason!r}），'
                f'可能是协议不兼容或上游异常，触发重试。'
            )
        return AnthropicReply(
            text=result_text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_estimated=usage_estimated,
        )

    @staticmethod
    def _stream_options_unsupported(response) -> bool:
        """Return true only for an explicit unsupported stream_options error."""
        if response.status_code not in (400, 422):
            return False
        try:
            body = str(response.text or '').lower()
        except Exception:
            return False
        mentions_parameter = 'stream_options' in body or 'stream options' in body
        unsupported = any(marker in body for marker in (
            'unsupported', 'not supported', 'unknown parameter', 'unknown field',
            'unrecognized', 'unrecognised', 'extra inputs are not permitted',
            'unexpected keyword', 'invalid parameter',
        ))
        return mentions_parameter and unsupported

    @staticmethod
    def _temperature_unsupported(response) -> bool:
        """仅在 400/422 正文明确点名 temperature 被弃用/不支持时才回退。"""
        if response.status_code not in (400, 422):
            return False
        try:
            body = str(response.text or '').lower()
        except Exception:
            return False
        if 'temperature' not in body:
            return False
        return any(marker in body for marker in (
            'deprecated', 'unsupported', 'not supported', 'unknown parameter',
            'unknown field', 'unrecognized', 'unrecognised',
            'extra inputs are not permitted', 'unexpected keyword',
            'invalid parameter', 'not permitted', 'cannot be specified',
            'may not be specified', 'is not allowed',
        ))

    @staticmethod
    def _coerce_token_count(value) -> int | None:
        try:
            if value is None:
                return None
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _extract_native_usage(cls, data: dict) -> tuple[int | None, int | None]:
        """Normalize Anthropic, Chat Completions and Responses API usage shapes."""
        usage = data.get('usage') or {}
        if not isinstance(usage, dict):
            return None, None
        input_tokens = cls._coerce_token_count(
            usage.get('input_tokens', usage.get('prompt_tokens'))
        )
        output_tokens = cls._coerce_token_count(
            usage.get('output_tokens', usage.get('completion_tokens'))
        )
        return input_tokens, output_tokens

    @staticmethod
    def _token_estimation_text(value) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    def _estimate_usage(cls, payload: dict, data: dict, model_name: str) -> tuple[int | None, int | None]:
        """Estimate semantic request/response content when native usage is absent."""
        if tiktoken is None:
            return None, None
        try:
            try:
                encoding = tiktoken.encoding_for_model(str(model_name or ''))
            except (KeyError, ValueError):
                encoding = tiktoken.get_encoding('cl100k_base')

            # Include fixed system/instructions and tool definitions, not just chat messages.
            request_parts = []
            for key in ('system', 'instructions', 'messages', 'input', 'tools'):
                value = payload.get(key)
                if value not in (None, '', []):
                    request_parts.append(f'{key}:{cls._token_estimation_text(value)}')

            response_value = data.get('output')
            if response_value is None:
                response_value = data.get('content')
            if response_value is None:
                response_value = data.get('choices')

            return (
                len(encoding.encode('\n'.join(request_parts))),
                len(encoding.encode(cls._token_estimation_text(response_value))),
            )
        except Exception:
            # Accounting must never turn a valid model response into an API failure/retry.
            return None, None

    @staticmethod
    def _normalize_thinking_level(thinking: str | None) -> str | None:
        level = str(thinking or '').strip().lower()
        return level if level in {'low', 'medium', 'high'} else None

    @staticmethod
    def _anthropic_thinking_budget(level: str) -> int:
        return {'low': 1024, 'medium': 1280, 'high': 1536}[level]

    @staticmethod
    def _normalize_tool_result_content(content):
        """tool_result.content 只保留 Anthropic 可接受的文本/文本块格式。"""
        if content is None:
            return ''
        if isinstance(content, str):
            return content
        if isinstance(content, (int, float, bool)):
            return str(content)
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False)
        if isinstance(content, list):
            blocks: list[dict] = []
            for item in content:
                if isinstance(item, str):
                    blocks.append({'type': 'text', 'text': item})
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'text':
                    blocks.append({'type': 'text', 'text': str(item.get('text') or '')})
            return blocks if blocks else ''
        return str(content)

    def _normalize_anthropic_message_content(self, content):
        """把历史消息规整成合法的 Anthropic content，避免把中转站附加字段回传。"""
        if content is None:
            return ''
        if isinstance(content, str):
            return content

        normalized: list[dict] = []
        for block in content or []:
            if isinstance(block, str):
                normalized.append({'type': 'text', 'text': block})
                continue
            if not isinstance(block, dict):
                continue

            block_type = block.get('type')
            if block_type == 'text':
                item = {'type': 'text', 'text': str(block.get('text') or '')}
                cache_control = self._normalize_cache_control(block.get('cache_control'))
                if cache_control is not None:
                    item['cache_control'] = cache_control
                normalized.append(item)
            elif block_type == 'tool_use':
                tool_input = block.get('input')
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except (ValueError, TypeError):
                        tool_input = {'raw': tool_input}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                normalized.append(
                    {
                        'type': 'tool_use',
                        'id': str(block.get('id') or ''),
                        'name': str(block.get('name') or ''),
                        'input': tool_input,
                    }
                )
            elif block_type == 'tool_result':
                normalized.append(
                    {
                        'type': 'tool_result',
                        'tool_use_id': str(block.get('tool_use_id') or ''),
                        'content': self._normalize_tool_result_content(block.get('content')),
                    }
                )

        if len(normalized) == 1 and normalized[0].get('type') == 'text':
            return normalized[0].get('text', '')
        return normalized if normalized else ''

    @staticmethod
    def _normalize_cache_control(cache_control):
        """仅透传 Anthropic 合法、且由本地显式设置的 cache_control。"""
        if not isinstance(cache_control, dict):
            return None
        if str(cache_control.get('type') or '').strip() != 'ephemeral':
            return None
        return {'type': 'ephemeral'}

    def _normalize_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        normalized_messages: list[dict] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get('role') or '').strip()
            if role not in {'user', 'assistant'}:
                continue
            normalized_messages.append(
                {
                    'role': role,
                    'content': self._normalize_anthropic_message_content(msg.get('content')),
                }
            )
        return normalized_messages

    @staticmethod
    def _normalize_anthropic_system(system_blocks: list[dict] | str):
        """system 只保留 Anthropic 合法字段；cache_control 可保留。"""
        if not system_blocks:
            return ''
        if isinstance(system_blocks, str):
            return system_blocks

        normalized: list[dict] = []
        for block in system_blocks or []:
            if isinstance(block, str):
                normalized.append({'type': 'text', 'text': block})
                continue
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'text':
                continue
            item = {'type': 'text', 'text': str(block.get('text') or '')}
            cache_control = AnthropicChatModel._normalize_cache_control(block.get('cache_control'))
            if cache_control is not None:
                item['cache_control'] = cache_control
            normalized.append(item)
        return normalized if normalized else ''

    @staticmethod
    def _normalize_anthropic_tools(tools: list[dict]) -> list[dict]:
        """工具定义只保留 Anthropic 需要的字段，避免代理层严格校验时报 400。"""
        normalized: list[dict] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            item = {
                'name': str(tool.get('name') or ''),
                'description': str(tool.get('description') or ''),
                'input_schema': tool.get('input_schema') or {},
            }
            cache_control = AnthropicChatModel._normalize_cache_control(tool.get('cache_control'))
            if cache_control is not None:
                item['cache_control'] = cache_control
            normalized.append(item)
        return normalized

    @staticmethod
    def _normalize_anthropic_response_block(block: dict) -> dict:
        """把上游响应 block 收敛成干净结构，避免下一轮原样回填触发参数错误。"""
        if not isinstance(block, dict):
            return {}
        block_type = block.get('type')
        if block_type == 'text':
            return {'type': 'text', 'text': str(block.get('text') or '')}
        if block_type == 'tool_use':
            tool_input = block.get('input')
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except (ValueError, TypeError):
                    tool_input = {'raw': tool_input}
            if not isinstance(tool_input, dict):
                tool_input = {}
            return {
                'type': 'tool_use',
                'id': str(block.get('id') or ''),
                'name': str(block.get('name') or ''),
                'input': tool_input,
            }
        if block_type == 'thinking':
            return {
                'type': 'thinking',
                'thinking': str(block.get('thinking') or ''),
                'signature': str(block.get('signature') or ''),
            }
        return {}


    def _build_openai_responses_request(
        self,
        system_blocks: list[dict] | str,
        messages: list[dict],
        tools: list[dict] | None,
        model_name: str | None,
        temperature: float,
        max_tokens: int | None,
        thinking: str | None = None,
    ) -> tuple[dict, dict]:
        """将 Anthropic 请求翻译成 OpenAI Responses API 格式，返回 (headers, payload)。"""
        headers = {
            'Content-Type': 'application/json',
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        # Responses API 使用 input 字段，是扁平的 item 列表
        input_items: list[dict] = []

        # system prompt
        system_text = self._blocks_to_text(system_blocks)
        if system_text:
            input_items.append({'role': 'system', 'content': system_text})

        # 翻译对话消息
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content')

            if role == 'assistant':
                input_items.extend(self._translate_assistant_message_to_responses(content))
            elif role == 'user':
                input_items.extend(self._translate_user_message_to_responses(content))
            else:
                input_items.append({'role': role, 'content': self._blocks_to_text(content)})

        payload: dict = {
            'model': model_name or self.model_name,
            'input': input_items,
            'temperature': temperature,
            'stream': True,
        }
        # max_tokens 为 None 时不传：输出上限交给上游自行决定（不限制）
        # 但推理模型（开启 reasoning/thinking 时）上游默认仅 ~16 token，
        # 推理内容会吃光预算导致正文为空，需显式给出较大的输出上限。
        if max_tokens is not None:
            payload['max_output_tokens'] = max_tokens
        elif thinking:
            payload['max_output_tokens'] = _OPENAI_REASONING_DEFAULT_MAX_TOKENS
        if thinking:
            payload['reasoning'] = {'effort': thinking}

        if tools:
            openai_tools: list[dict] = []
            for tool in tools:
                openai_tools.append({
                    'type': 'function',
                    'name': tool.get('name'),
                    'description': tool.get('description', ''),
                    'parameters': tool.get('input_schema') or {},
                })
            payload['tools'] = openai_tools

        return headers, payload

    def _translate_assistant_message_to_responses(self, content: list[dict] | str | None) -> list[dict]:
        """assistant 消息：text + tool_use → Responses API items。"""
        if isinstance(content, str):
            return [{'role': 'assistant', 'content': content}]

        items: list[dict] = []
        text_parts: list[str] = []

        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'text':
                text_parts.append(str(block.get('text') or ''))
            elif btype == 'tool_use':
                # 如果有累积的文本，先输出
                if text_parts:
                    items.append({'role': 'assistant', 'content': '\n'.join(p for p in text_parts if p)})
                    text_parts = []
                # 添加 function_call item
                tool_input = block.get('input')
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except (ValueError, TypeError):
                        tool_input = {'raw': tool_input}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                items.append({
                    'type': 'function_call',
                    'call_id': str(block.get('id') or ''),
                    'name': str(block.get('name') or ''),
                    'arguments': json.dumps(tool_input, ensure_ascii=False),
                })

        if text_parts:
            items.append({'role': 'assistant', 'content': '\n'.join(p for p in text_parts if p)})

        return items

    def _translate_user_message_to_responses(self, content: list[dict] | str | None) -> list[dict]:
        """user 消息：tool_result + text → Responses API items。"""
        if isinstance(content, str):
            return [{'role': 'user', 'content': content}]

        items: list[dict] = []
        text_parts: list[str] = []

        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'tool_result':
                # 如果有累积的文本，先输出
                if text_parts:
                    items.append({'role': 'user', 'content': '\n'.join(p for p in text_parts if p)})
                    text_parts = []
                # 添加 function_call_output item
                items.append({
                    'type': 'function_call_output',
                    'call_id': str(block.get('tool_use_id') or ''),
                    'output': self._blocks_to_text(block.get('content')),
                })
            elif btype == 'text':
                text_parts.append(str(block.get('text') or ''))

        if text_parts:
            items.append({'role': 'user', 'content': '\n'.join(p for p in text_parts if p)})

        return items

    def _parse_openai_responses_response(self, data: dict) -> tuple[list[dict], str]:
        """将 OpenAI Responses API 格式响应转换为 Anthropic content blocks 列表。"""
        output = data.get('output') or []
        status = str(data.get('status') or '')

        if status == 'incomplete':
            details = data.get('incomplete_details') or {}
            reason = str(details.get('reason') or '') if isinstance(details, dict) else ''
            stop_reason = 'max_tokens' if reason in ('max_output_tokens', 'max_tokens') else (reason or 'incomplete')
        else:
            stop_reason = {
                'completed': 'end_turn',
                'requires_action': 'tool_use',
            }.get(status, status)

        blocks: list[dict] = []

        for item in output:
            item_type = item.get('type')

            if item_type == 'message':
                # 提取文本内容
                content_list = item.get('content') or []
                for content_item in content_list:
                    if content_item.get('type') == 'output_text':
                        text = content_item.get('text') or ''
                        if text.strip():
                            blocks.append({'type': 'text', 'text': text})

            elif item_type == 'function_call':
                # 提取工具调用
                arguments = item.get('arguments') or '{}'
                try:
                    tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
                except (ValueError, TypeError):
                    tool_input = {'raw': arguments}
                if not isinstance(tool_input, dict):
                    tool_input = {}

                blocks.append({
                    'type': 'tool_use',
                    'id': str(item.get('call_id') or ''),
                    'name': str(item.get('name') or ''),
                    'input': tool_input,
                })

        return blocks, stop_reason

    def _parse_openai_responses_stream(self, response, on_first_token=None) -> dict:
        """解析 OpenAI Responses API SSE 流，返回模拟非流式 JSON 响应的 dict。

        30 秒内未收到第一个有效 data 行则抛出 requests.exceptions.Timeout。
        """
        import time as _time
        first_deadline = _time.time() + 30
        received_first = False

        response.encoding = 'utf-8'
        output_items: list[dict] = []
        current_item: dict | None = None
        current_text = ''
        current_function_args = ''
        status = 'in_progress'
        usage: dict = {}
        final_response: dict = {}

        for raw_line in response.iter_lines(decode_unicode=False):
            if raw_line is None:
                continue
            try:
                line = raw_line.decode('utf-8').strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue

            if line.startswith(':'):
                continue

            if not line.startswith('data: '):
                continue

            data_str = line[6:]

            if data_str == '[DONE]':
                break

            if not received_first:
                if _time.time() > first_deadline:
                    raise requests.exceptions.Timeout(
                        '首token超时：30秒内未收到有效响应'
                    )

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            received_first = True

            event_type = event.get('type', '')

            if event_type == 'response.output_item.added':
                # 新的输出项
                item = event.get('item', {})
                item_type = item.get('type')
                if item_type == 'message':
                    current_item = {'type': 'message', 'content': []}
                    for c in item.get('content') or []:
                        if isinstance(c, dict) and c.get('type') == 'output_text':
                            text = str(c.get('text') or '')
                            if text and on_first_token:
                                on_first_token()
                            current_text += text
                elif item_type == 'function_call':
                    initial_args = str(item.get('arguments') or '')
                    if (item.get('name') or initial_args) and on_first_token:
                        on_first_token()
                    current_item = {
                        'type': 'function_call',
                        'call_id': item.get('call_id', ''),
                        'name': item.get('name', ''),
                        'arguments': initial_args,
                    }
                    current_function_args = initial_args

            elif event_type == 'response.output_item.done':
                # 输出项完成
                if current_item:
                    if current_item['type'] == 'message' and current_text:
                        current_item['content'].append({
                            'type': 'output_text',
                            'text': current_text,
                        })
                        current_text = ''
                    elif current_item['type'] == 'function_call':
                        current_item['arguments'] = current_function_args
                        current_function_args = ''
                    output_items.append(current_item)
                    current_item = None

            elif event_type == 'response.output_text.delta':
                if isinstance(current_item, dict) and current_item.get('type') == 'message':
                    delta = event.get('delta', '')
                    if delta and on_first_token:
                        on_first_token()
                    current_text += delta

            elif event_type == 'response.function_call_arguments.delta':
                delta = event.get('delta', '')
                if delta and on_first_token:
                    on_first_token()
                current_function_args += delta

            elif event_type == 'response.completed':
                status = 'completed'
                response_data = event.get('response') or {}
                if isinstance(response_data, dict):
                    final_response = response_data
                    usage = response_data.get('usage') or usage

            elif event_type == 'response.failed':
                status = 'failed'
                response_data = event.get('response') or {}
                if isinstance(response_data, dict):
                    final_response = response_data
                    usage = response_data.get('usage') or usage

            elif event_type == 'response.incomplete':
                status = 'incomplete'
                response_data = event.get('response') or {}
                if isinstance(response_data, dict):
                    final_response = response_data
                    usage = response_data.get('usage') or usage

        # 如果还有未完成的 item，也加入
        if current_item:
            if current_item['type'] == 'message' and current_text:
                current_item['content'].append({
                    'type': 'output_text',
                    'text': current_text,
                })
            elif current_item['type'] == 'function_call':
                current_item['arguments'] = current_function_args
            output_items.append(current_item)

        if not output_items and isinstance(final_response.get('output'), list):
            output_items = final_response['output']
        result = dict(final_response)
        result['output'] = output_items
        result['status'] = str(final_response.get('status') or status)
        result['usage'] = usage
        return result

    def _build_openai_request(
        self,
        system_blocks: list[dict] | str,
        messages: list[dict],
        tools: list[dict] | None,
        model_name: str | None,
        temperature: float,
        max_tokens: int | None,
        thinking: str | None = None,
    ) -> tuple[dict, dict]:
        """将 Anthropic 请求翻译成 OpenAI /chat/completions 格式，返回 (headers, payload)。"""
        headers = {
            'Content-Type': 'application/json',
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        openai_messages: list[dict] = []

        # system：字符串或 text block 列表 → 单条 system 消息，丢弃 cache_control
        system_text = self._blocks_to_text(system_blocks)
        if system_text:
            openai_messages.append({'role': 'system', 'content': system_text})

        # 逐条翻译对话消息
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content')

            if role == 'assistant':
                openai_messages.extend(self._translate_assistant_message(content))
            elif role == 'user':
                openai_messages.extend(self._translate_user_message(content))
            else:
                # 其它角色按纯文本处理
                openai_messages.append({'role': role, 'content': self._blocks_to_text(content)})

        payload: dict = {
            'model': model_name or self.model_name,
            'temperature': temperature,
            'messages': openai_messages,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        # max_tokens 为 None 时不传：输出上限交给上游自行决定（不限制）
        # 推理模型（reasoning_effort 开启）上游默认输出上限极小，推理内容吃光
        # 预算后正文为空，需显式给出较大的输出上限。
        if max_tokens is not None:
            payload['max_tokens'] = max_tokens
        elif thinking:
            payload['max_tokens'] = _OPENAI_REASONING_DEFAULT_MAX_TOKENS
        if thinking:
            payload['reasoning_effort'] = thinking

        if tools:
            openai_tools: list[dict] = []
            for tool in tools:
                openai_tools.append({
                    'type': 'function',
                    'function': {
                        'name': tool.get('name'),
                        'description': tool.get('description', ''),
                        'parameters': tool.get('input_schema') or {},
                    },
                })
            payload['tools'] = openai_tools
            # 同 Anthropic 分支：默认强制工具调用；推理模式下部分上游拒绝 required。
            payload['tool_choice'] = 'auto' if thinking else 'required'

        return headers, payload

    @staticmethod
    def _blocks_to_text(content: list[dict] | str | None) -> str:
        """把字符串或 block 列表内容拼成纯文本，丢弃 cache_control 等元信息。"""
        if content is None:
            return ''
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(str(block.get('text') or ''))
            elif isinstance(block, str):
                parts.append(block)
        return '\n'.join(p for p in parts if p)

    def _translate_assistant_message(self, content: list[dict] | str | None) -> list[dict]:
        """assistant 消息：text + tool_use → OpenAI content + tool_calls。"""
        if isinstance(content, str):
            return [{'role': 'assistant', 'content': content}]

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'text':
                text_parts.append(str(block.get('text') or ''))
            elif btype == 'tool_use':
                tool_calls.append({
                    'id': str(block.get('id') or ''),
                    'type': 'function',
                    'function': {
                        'name': str(block.get('name') or ''),
                        'arguments': json.dumps(block.get('input') or {}, ensure_ascii=False),
                    },
                })

        message: dict = {'role': 'assistant'}
        message['content'] = '\n'.join(p for p in text_parts if p)
        if tool_calls:
            message['tool_calls'] = tool_calls
        return [message]

    def _translate_user_message(self, content: list[dict] | str | None) -> list[dict]:
        """user 消息：tool_result → OpenAI tool 消息；普通文本 → user 消息。"""
        if isinstance(content, str):
            return [{'role': 'user', 'content': content}]

        result_messages: list[dict] = []
        text_parts: list[str] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'tool_result':
                result_messages.append({
                    'role': 'tool',
                    'tool_call_id': str(block.get('tool_use_id') or ''),
                    'content': self._blocks_to_text(block.get('content')),
                })
            elif btype == 'text':
                text_parts.append(str(block.get('text') or ''))

        messages: list[dict] = []
        text = '\n'.join(p for p in text_parts if p)
        # OpenAI 协议要求 role=tool 消息紧跟 assistant 的 tool_calls，不能有 user 消息插在中间
        messages.extend(result_messages)
        if text:
            messages.append({'role': 'user', 'content': text})
        return messages

    def _parse_openai_response(self, data: dict) -> tuple[list[dict], str]:
        """将 OpenAI 格式响应转换为 Anthropic content blocks 列表。"""
        choices = data.get('choices') or []
        if not choices:
            return [], ''
        choice = choices[0]
        msg = choice.get('message') or {}
        finish_reason = str(choice.get('finish_reason') or '')
        stop_reason = {'stop': 'end_turn', 'tool_calls': 'tool_use', 'length': 'max_tokens'}.get(finish_reason, finish_reason)

        blocks: list[dict] = []

        # 文本内容
        text_content = msg.get('content')
        if isinstance(text_content, str) and text_content.strip():
            blocks.append({'type': 'text', 'text': text_content})
        elif isinstance(text_content, list):
            for item in text_content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    blocks.append({'type': 'text', 'text': str(item.get('text') or '')})

        # 工具调用
        for tc in msg.get('tool_calls') or []:
            fn = tc.get('function') or {}
            arguments = fn.get('arguments') or '{}'
            try:
                tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
            except (ValueError, TypeError):
                tool_input = {'raw': arguments}
            if not isinstance(tool_input, dict):
                tool_input = {}
            blocks.append({
                'type': 'tool_use',
                'id': str(tc.get('id') or ''),
                'name': str(fn.get('name') or ''),
                'input': tool_input,
            })

        return blocks, stop_reason

    def _parse_anthropic_stream(self, response, on_first_token=None) -> dict:
        """解析 Anthropic SSE 流，返回模拟非流式 JSON 响应的 dict。

        30 秒内未收到第一个有效 data 行则抛出 requests.exceptions.Timeout。
        """
        import time as _time
        first_deadline = _time.time() + 30
        received_first = False

        response.encoding = 'utf-8'
        content_blocks: list[dict] = []
        current_block_index = -1
        current_tool_input_json = ''
        stop_reason = ''
        usage: dict = {}

        for raw_line in response.iter_lines(decode_unicode=False):
            if raw_line is None:
                continue
            try:
                line = raw_line.decode('utf-8').strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue

            if line.startswith(':'):
                continue

            if not line.startswith('data: '):
                continue

            data_str = line[6:]

            if not received_first:
                if _time.time() > first_deadline:
                    raise requests.exceptions.Timeout(
                        '首token超时：30秒内未收到有效响应'
                    )

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            received_first = True

            event_type = event.get('type')

            if event_type == 'message_start':
                message_data = event.get('message') or {}
                if isinstance(message_data, dict):
                    usage.update(message_data.get('usage') or {})
            elif event_type == 'content_block_start':
                block = self._normalize_anthropic_response_block(event.get('content_block', {}))
                if block:
                    content_blocks.append(block)
                    current_block_index = len(content_blocks) - 1
                else:
                    current_block_index = -1
                current_tool_input_json = ''
            elif event_type == 'content_block_delta':
                delta = event.get('delta', {})
                delta_type = delta.get('type')
                if delta_type == 'text_delta':
                    text = delta.get('text', '')
                    if text and on_first_token:
                        on_first_token()
                    if current_block_index >= 0 and content_blocks[current_block_index].get('type') == 'text':
                        content_blocks[current_block_index]['text'] = (
                            content_blocks[current_block_index].get('text', '') + text
                        )
                    else:
                        content_blocks.append({'type': 'text', 'text': text})
                        current_block_index = len(content_blocks) - 1
                elif delta_type == 'input_json_delta':
                    partial = delta.get('partial_json', '')
                    if partial and on_first_token:
                        on_first_token()
                    current_tool_input_json += partial
                elif delta_type == 'thinking_delta':
                    # Preserve the block for protocol continuity only; complete()
                    # filters thinking blocks before exposing reply text.
                    thinking_text = str(delta.get('thinking') or '')
                    if current_block_index >= 0 and content_blocks[current_block_index].get('type') == 'thinking':
                        content_blocks[current_block_index]['thinking'] = (
                            content_blocks[current_block_index].get('thinking', '') + thinking_text
                        )
            elif event_type == 'content_block_stop':
                if current_block_index >= 0:
                    block = content_blocks[current_block_index]
                    if block.get('type') == 'tool_use' and current_tool_input_json:
                        try:
                            block['input'] = json.loads(current_tool_input_json)
                        except (json.JSONDecodeError, TypeError):
                            block['input'] = {'raw': current_tool_input_json}
                    current_tool_input_json = ''
            elif event_type == 'message_delta':
                sr = event.get('delta', {}).get('stop_reason', '')
                if sr:
                    stop_reason = sr
                usage.update(event.get('usage') or {})
            elif event_type == 'message_stop':
                break

        return {
            'content': content_blocks,
            'stop_reason': stop_reason,
            'usage': usage,
        }

    def _parse_openai_stream(self, response, on_first_token=None) -> dict:
        """解析 OpenAI SSE 流（/v1/chat/completions），返回模拟非流式 JSON 响应的 dict。

        30 秒内未收到第一个有效 data 行则抛出 requests.exceptions.Timeout。
        """
        import time as _time
        first_deadline = _time.time() + 30
        received_first = False

        response.encoding = 'utf-8'
        text_content = ''
        tool_calls_by_idx: dict[int, dict] = {}
        finish_reason = ''
        usage: dict = {}

        for raw_line in response.iter_lines(decode_unicode=False):
            if raw_line is None:
                continue
            try:
                line = raw_line.decode('utf-8').strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue

            if line.startswith(':'):
                continue

            if not line.startswith('data: '):
                continue

            data_str = line[6:]

            if data_str == '[DONE]':
                break

            if not received_first:
                if _time.time() > first_deadline:
                    raise requests.exceptions.Timeout(
                        '首token超时：30秒内未收到有效响应'
                    )

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            received_first = True

            event_usage = event.get('usage')
            if isinstance(event_usage, dict):
                usage = event_usage

            choices = event.get('choices', [])
            if not choices:
                continue

            delta = choices[0].get('delta', {})

            content = delta.get('content')
            if content:
                if on_first_token:
                    on_first_token()
                text_content += content

            tool_call_deltas = delta.get('tool_calls', [])
            if tool_call_deltas and on_first_token:
                on_first_token()
            for tc in tool_call_deltas:
                idx = tc.get('index', 0)
                if idx not in tool_calls_by_idx:
                    tool_calls_by_idx[idx] = {
                        'id': '',
                        'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                if tc.get('id'):
                    tool_calls_by_idx[idx]['id'] = tc['id']
                fn = tc.get('function', {})
                if fn.get('name'):
                    tool_calls_by_idx[idx]['function']['name'] += fn['name']
                if fn.get('arguments'):
                    tool_calls_by_idx[idx]['function']['arguments'] += fn['arguments']

            fr = choices[0].get('finish_reason', '')
            if fr:
                finish_reason = fr

        tool_calls_list = [
            tool_calls_by_idx[i]
            for i in sorted(tool_calls_by_idx.keys())
        ]

        message: dict = {'content': text_content}
        if tool_calls_list:
            message['tool_calls'] = tool_calls_list

        return {
            'choices': [{
                'message': message,
                'finish_reason': finish_reason or 'stop',
            }],
            'usage': usage,
        }
