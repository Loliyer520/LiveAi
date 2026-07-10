import json
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
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        if self.api_key:
            # 不同中转对鉴权头要求不一致，两种都带上
            headers['x-api-key'] = self.api_key
            headers['Authorization'] = f'Bearer {self.api_key}'

        payload: dict = {
            'model': model_name or self.model_name,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': messages,
            'stream': False,
        }
        if system_blocks:
            payload['system'] = system_blocks
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = {'type': 'any'}

        response = requests.post(
            f'{self.base_url}{self.messages_path}',
            headers=headers,
            json=payload,
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f'anthropic request failed status={response.status_code} body={response.text[:500]}'
            )
        data = response.json()

        # 优先尝试 Anthropic 格式（content 字段为 block 列表）
        raw = data.get('content')
        if isinstance(raw, list) and raw:
            content = [b for b in raw if isinstance(b, dict) and b.get('type') != 'thinking']
            stop_reason = str(data.get('stop_reason') or '')
        else:
            # 降级：OpenAI 格式（choices[0].message）
            content, stop_reason = self._parse_openai_response(data)

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
