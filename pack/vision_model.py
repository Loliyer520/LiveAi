import base64
import html
import mimetypes
from pathlib import Path

import requests


class OpenAICompatibleVisionModel:
    def __init__(self, base_url: str, api_key: str, model_name: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model_name = model_name

    def describe_images(self, image_refs: list[str], prompt: str | None = None) -> str | None:
        if not image_refs:
            print('[Vision] no image refs provided')
            return None

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        content = [
            {
                'type': 'text',
                'text': prompt or '请详细描述这些图片中的内容，提取人物、场景、文字、情绪、动作和关键细节。',
            }
        ]
        accepted_count = 0
        for ref in image_refs:
            image_url = self._to_image_url(ref)
            if not image_url:
                print(f'[Vision] skipped image ref={ref}')
                continue
            accepted_count += 1
            content.append({'type': 'image_url', 'image_url': {'url': image_url}})

        if len(content) == 1:
            print('[Vision] no valid image refs after normalization')
            return None

        print(
            f'[Vision] request model={self.model_name} '
            f'images={accepted_count} endpoint={self.base_url}/chat/completions'
        )
        payload = {
            'model': self.model_name,
            'messages': [{'role': 'user', 'content': content}],
            'temperature': 0.2,
            'stream': False,
        }
        try:
            response = requests.post(
                f'{self.base_url}/chat/completions',
                headers=headers,
                json=payload,
                timeout=90,
            )
        except Exception as exc:
            print(f'[Vision] request failed error={exc}')
            raise
        print(f'[Vision] response status={response.status_code}')
        if response.status_code >= 400:
            print(f'[Vision] response body={response.text[:800]}')
        response.raise_for_status()
        data = response.json()
        choices = data.get('choices') or []
        if not choices:
            print('[Vision] response has no choices')
            return None
        message = choices[0].get('message') or {}
        content = message.get('content')
        if isinstance(content, str):
            print(f'[Vision] response text chars={len(content.strip())}')
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if item.get('type') == 'text':
                    parts.append(item.get('text', ''))
            merged = ''.join(parts).strip()
            print(f'[Vision] response list-text chars={len(merged)}')
            return merged
        print(f'[Vision] unsupported content type={type(content).__name__}')
        return None

    def _to_image_url(self, ref: str) -> str | None:
        ref = html.unescape(ref.strip())
        if ref.startswith('data:'):
            print(f'[Vision] using direct image ref={ref[:96]}')
            return ref
        if ref.startswith('http://') or ref.startswith('https://'):
            return self._download_remote_image(ref)
        path = Path(ref)
        if not path.exists() or not path.is_file():
            print(f'[Vision] local file missing ref={ref}')
            return None
        mime_type, _ = mimetypes.guess_type(path.name)
        if not mime_type:
            mime_type = 'image/png'
        data = base64.b64encode(path.read_bytes()).decode('utf-8')
        print(f'[Vision] encoded local file path={path} mime={mime_type}')
        return f'data:{mime_type};base64,{data}'

    def _download_remote_image(self, ref: str) -> str | None:
        try:
            response = requests.get(
                ref,
                timeout=60,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Referer': 'https://im.qq.com/',
                },
            )
            print(f'[Vision] fetched remote image status={response.status_code} ref={ref[:96]}')
            response.raise_for_status()
        except Exception as exc:
            print(f'[Vision] remote fetch failed ref={ref[:96]} error={exc}')
            return ref
        mime_type = response.headers.get('content-type', '').split(';', 1)[0].strip()
        if not mime_type.startswith('image/'):
            guessed, _ = mimetypes.guess_type(ref)
            mime_type = guessed or 'image/jpeg'
        data = base64.b64encode(response.content).decode('utf-8')
        print(f'[Vision] encoded remote image mime={mime_type} bytes={len(response.content)}')
        return f'data:{mime_type};base64,{data}'
