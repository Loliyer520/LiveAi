import json
import time
from dataclasses import dataclass, field

import requests


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


class AnthropicChatModel:
    def __init__(
        self,
        base_url: str,
        api_key: str = '',
        model_name: str = 'claude-sonnet-4-6',
        messages_path: str = '/messages',
    ):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name
        self.messages_path = messages_path if messages_path.startswith('/') else f'/{messages_path}'
        # 上游走 OpenAI 兼容协议（/v1/chat/completions 之类）时，请求侧需做 Anthropic→OpenAI 翻译
        self.is_openai_protocol = '/chat/completions' in (self.messages_path or '')

    def with_config(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        messages_path: str | None = None,
    ) -> "AnthropicChatModel":
        return AnthropicChatModel(
            base_url=base_url or self.base_url,
            api_key=self.api_key if api_key is None else api_key,
            model_name=model_name or self.model_name,
            messages_path=messages_path or self.messages_path,
        )

    def complete(
        self,
        system_blocks: list[dict] | str,
        messages: list[dict],
        tools: list[dict] | None = None,
        model_name: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AnthropicReply | None:
        if self.is_openai_protocol:
            headers, payload = self._build_openai_request(
                system_blocks=system_blocks,
                messages=messages,
                tools=tools,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
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
                'max_tokens': max_tokens,
                'temperature': temperature,
                'messages': self._normalize_anthropic_messages(messages),
                'stream': True,
            }
            normalized_system = self._normalize_anthropic_system(system_blocks)
            if normalized_system:
                payload['system'] = normalized_system
            if tools:
                payload['tools'] = self._normalize_anthropic_tools(tools)
                payload['tool_choice'] = {'type': 'auto'}
        _request_url = f'{self.base_url}{self.messages_path}'
        _request_start = time.perf_counter()
        response = requests.post(
            _request_url,
            headers=headers,
            json=payload,
            timeout=120,
            stream=True,
        )
        response.encoding = 'utf-8'
        _request_ms = int((time.perf_counter() - _request_start) * 1000)
        print(f'[HTTP] POST {_request_url} status={response.status_code} ms={_request_ms}')
        if response.status_code >= 400:
            raise RuntimeError(
                f'anthropic request failed status={response.status_code} body={response.text[:500]}'
            )
        if self.is_openai_protocol:
            data = self._parse_openai_stream(response)
        else:
            data = self._parse_anthropic_stream(response)

        # 优先用 choices 字段判断是否 OpenAI 格式，避免 content 为空列表时误路由到 OpenAI 解析
        if 'choices' in data:
            # OpenAI 格式（choices[0].message）
            content, stop_reason = self._parse_openai_response(data)
        else:
            # Anthropic 格式（content 为 block 列表，允许为空列表或 null）
            raw = data.get('content') or []
            content = [b for b in raw if isinstance(b, dict) and b.get('type') != 'thinking']
            stop_reason = str(data.get('stop_reason') or '')

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
        return AnthropicReply(
            text='\n'.join(part for part in text_parts if part).strip(),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=content,
        )

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
                normalized.append({'type': 'text', 'text': str(block.get('text') or '')})
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
            cache_control = block.get('cache_control')
            if isinstance(cache_control, dict):
                item['cache_control'] = dict(cache_control)
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
            cache_control = tool.get('cache_control')
            if isinstance(cache_control, dict):
                item['cache_control'] = dict(cache_control)
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

    def _build_openai_request(
        self,
        system_blocks: list[dict] | str,
        messages: list[dict],
        tools: list[dict] | None,
        model_name: str | None,
        temperature: float,
        max_tokens: int,
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
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': openai_messages,
            'stream': True,
        }

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
            payload['tool_choice'] = 'auto'

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

    def _parse_anthropic_stream(self, response) -> dict:
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

            if event_type == 'content_block_start':
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
                    if current_block_index >= 0 and content_blocks[current_block_index].get('type') == 'text':
                        content_blocks[current_block_index]['text'] = (
                            content_blocks[current_block_index].get('text', '') + text
                        )
                    else:
                        content_blocks.append({'type': 'text', 'text': text})
                        current_block_index = len(content_blocks) - 1
                elif delta_type == 'input_json_delta':
                    current_tool_input_json += delta.get('partial_json', '')
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
            elif event_type == 'message_stop':
                break

        return {
            'content': content_blocks,
            'stop_reason': stop_reason,
        }

    def _parse_openai_stream(self, response) -> dict:
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

            choices = event.get('choices', [])
            if not choices:
                continue

            delta = choices[0].get('delta', {})

            content = delta.get('content')
            if content:
                text_content += content

            for tc in delta.get('tool_calls', []):
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
            }]
        }
