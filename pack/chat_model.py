import requests


class OpenAICompatibleChatModel:
    def __init__(self, base_url: str, api_key: str = '', model_name: str = 'gpt-4o-mini'):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name

    def with_config(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
    ) -> "OpenAICompatibleChatModel":
        return OpenAICompatibleChatModel(
            base_url=base_url or self.base_url,
            api_key=self.api_key if api_key is None else api_key,
            model_name=model_name or self.model_name,
        )

    def complete(self, messages: list[dict], model_name: str | None = None, temperature: float = 0.7) -> str | None:
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        payload = {
            'model': model_name or self.model_name,
            'messages': messages,
            'temperature': temperature,
            'stream': False,
        }

        response = requests.post(f'{self.base_url}/chat/completions', headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        data = response.json()
        choices = data.get('choices') or []
        if not choices:
            return None
        message = choices[0].get('message') or {}
        content = message.get('content')
        if isinstance(content, list):
            parts = []
            for item in content:
                if item.get('type') == 'text':
                    parts.append(item.get('text', ''))
            return ''.join(parts).strip()
        if isinstance(content, str):
            return content.strip()
        return None
