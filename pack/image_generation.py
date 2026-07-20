from pathlib import Path
from urllib.parse import urlencode
import random
import string

import requests


class NormalDrawingService:
    def __init__(self, base_url: str, token: str, output_dir: str = 'data/pict'):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _random_name(self, length: int = 8) -> str:
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    def generate(self, prompt: str) -> Path:
        params = {
            'tag': prompt,
            'token': self.token,
            'model': 'nai-diffusion-4-5-full',
            'artist': '@[[[artist:dishwasher1910]]], {yd_(orange_maru)}, [artist:ciloranko], [artist:sho_(sho_lwlw)], [ningen mame], year 2024',
            'size': '竖图',
            'steps': 23,
            'scale': 5,
            'cfg': 0,
            'sampler': 'k_euler_ancestral',
            'nocache': 1,
            'noise_schedule': 'karras',
        }
        target = self.output_dir / f'{self._random_name()}.png'
        response = requests.get(f'{self.base_url}?{urlencode(params)}', timeout=90)
        response.raise_for_status()
        target.write_bytes(response.content)
        return target
