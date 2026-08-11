from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import requests


class Txt2WavError(RuntimeError):
    pass


@dataclass(frozen=True)
class SynthesizedAudio:
    audio_bytes: bytes
    format: str = 'wav'
    content_type: str | None = 'audio/wav'
    sample_rate: int | None = 44100
    provider: str = 'unknown'


@dataclass(frozen=True)
class Txt2WavRequest:
    text: str
    speaker_id: str | None = None
    output_path: str | Path | None = None
    speed: float = 1.0
    volume: float = 0.0
    sample_rate: int | None = 44100
    timeout: float = 120.0
    provider_options: dict[str, Any] = field(default_factory=dict)


class Txt2WavProvider(Protocol):
    name: str

    def synthesize(self, request: Txt2WavRequest) -> SynthesizedAudio:
        ...


class Txt2WavService:
    def __init__(self, provider: Txt2WavProvider, output_dir: str | Path = 'data/tmp'):
        self.provider = provider
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, request: Txt2WavRequest) -> Path:
        text = str(request.text or '').strip()
        if not text:
            raise Txt2WavError('text 为空，无法生成语音。')
        audio = self.provider.synthesize(request)
        audio_format = str(audio.format or '').strip().lower()
        if not audio_format:
            raise Txt2WavError(f'provider={self.provider.name} 未返回音频格式。')
        target = self._resolve_output_path(request.output_path, audio_format)
        target.write_bytes(audio.audio_bytes)
        return target

    def text_to_wav(
        self,
        text: str,
        *,
        speaker_id: str | None = None,
        output_path: str | Path | None = None,
        speed: float = 1.0,
        volume: float = 0.0,
        sample_rate: int | None = 44100,
        timeout: float = 120.0,
        provider_options: dict[str, Any] | None = None,
    ) -> Path:
        request = Txt2WavRequest(
            text=text,
            speaker_id=speaker_id,
            output_path=output_path,
            speed=speed,
            volume=volume,
            sample_rate=sample_rate,
            timeout=timeout,
            provider_options=dict(provider_options or {}),
        )
        return self.synthesize(request)

    def text_to_audio(
        self,
        text: str,
        *,
        speaker_id: str | None = None,
        output_path: str | Path | None = None,
        speed: float = 1.0,
        volume: float = 0.0,
        sample_rate: int | None = 44100,
        timeout: float = 120.0,
        provider_options: dict[str, Any] | None = None,
    ) -> Path:
        return self.text_to_wav(
            text,
            speaker_id=speaker_id,
            output_path=output_path,
            speed=speed,
            volume=volume,
            sample_rate=sample_rate,
            timeout=timeout,
            provider_options=provider_options,
        )

    def _resolve_output_path(self, output_path: str | Path | None, audio_format: str) -> Path:
        suffix = f'.{str(audio_format or "").strip().lower() or "wav"}'
        if output_path is None:
            return self.output_dir / f'tts_{uuid.uuid4().hex}{suffix}'
        target = Path(output_path)
        if not target.is_absolute():
            target = self.output_dir / target
        if target.suffix.lower() != suffix:
            target = target.with_suffix(suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


class FishAudioTxt2WavProvider:
    name = 'fish_audio'

    def __init__(
        self,
        api_key: str,
        reference_id: str | None = None,
        *,
        base_url: str = 'https://api.fish.audio',
        model: str = 's2-pro',
        session: requests.sessions.Session | None = None,
    ):
        self.api_key = str(api_key or '').strip()
        self.reference_id = str(reference_id or '').strip() or None
        self.base_url = base_url.rstrip('/')
        self.model = str(model or 's2-pro').strip() or 's2-pro'
        self.session = session or requests.Session()
        if not self.api_key:
            raise Txt2WavError('Fish Audio API key 为空。')

    def synthesize(self, request: Txt2WavRequest) -> SynthesizedAudio:
        speaker_id = str(request.speaker_id or self.reference_id or '').strip()
        if not speaker_id:
            raise Txt2WavError('Fish Audio reference_id/speaker_id 为空。')

        payload: dict[str, Any] = {
            'text': request.text,
            'reference_id': speaker_id,
            'format': 'wav',
            'sample_rate': request.sample_rate,
            'normalize': True,
            'prosody': {
                'speed': request.speed,
                'volume': request.volume,
                'normalize_loudness': True,
            },
        }
        extra = dict(request.provider_options or {})
        if extra:
            payload.update(extra)
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'model': self.model,
        }
        response = self.session.post(
            f'{self.base_url}/v1/tts',
            headers=headers,
            json=payload,
            timeout=request.timeout,
        )
        if response.status_code >= 400:
            raise Txt2WavError(self._build_error_message(response))
        audio_bytes = response.content or b''
        if not audio_bytes:
            raise Txt2WavError('Fish Audio 返回了空音频数据。')
        return SynthesizedAudio(
            audio_bytes=audio_bytes,
            format='wav',
            content_type=response.headers.get('Content-Type'),
            sample_rate=request.sample_rate,
            provider=self.name,
        )

    @staticmethod
    def _build_error_message(response: requests.Response) -> str:
        prefix = f'Fish Audio TTS 失败: HTTP {response.status_code}'
        try:
            data = response.json()
        except ValueError:
            text = (response.text or '').strip()
            return f'{prefix} {text[:300]}'.strip()
        if isinstance(data, dict):
            message = data.get('message') or data.get('error') or json.dumps(data, ensure_ascii=False)
            return f'{prefix} {str(message)[:300]}'.strip()
        return f'{prefix} {str(data)[:300]}'.strip()


class TiaxTxt2WavProvider:
    name = 'tiax'
    # 研究来源里的公开客户端默认 key，作为无显式配置时的兼容默认值。
    DEFAULT_API_KEY = '4692f9ce7f1372f65887e13c7aee3421cbfabaec4307789e3023dc52bd667ea4'

    def __init__(
        self,
        api_key: str | None = None,
        reference_id: str | None = None,
        *,
        base_url: str = 'https://www.tiax.pw/API/yuyin.php',
        session: requests.sessions.Session | None = None,
    ):
        self.api_key = str(api_key or '').strip() or self.DEFAULT_API_KEY
        self.reference_id = str(reference_id or '').strip() or '130'
        self.base_url = str(base_url or 'https://www.tiax.pw/API/yuyin.php').strip()
        self.session = session or requests.Session()

    def synthesize(self, request: Txt2WavRequest) -> SynthesizedAudio:
        speaker_id = str(request.speaker_id or self.reference_id or '').strip()
        if not speaker_id:
            raise Txt2WavError('Tiax ys/speaker_id 为空。')
        response = self.session.get(
            self.base_url,
            params={
                'msg': request.text,
                'apikey': self.api_key,
                'ys': speaker_id,
            },
            timeout=request.timeout,
        )
        if response.status_code >= 400:
            raise Txt2WavError(self._build_error_message(response))
        audio_bytes = response.content or b''
        if not audio_bytes:
            raise Txt2WavError('Tiax 返回了空音频数据。')
        audio_format = self._infer_audio_format(response)
        return SynthesizedAudio(
            audio_bytes=audio_bytes,
            format=audio_format,
            content_type=response.headers.get('Content-Type'),
            sample_rate=request.sample_rate,
            provider=self.name,
        )

    @staticmethod
    def _infer_audio_format(response: requests.Response) -> str:
        content_type = str((response.headers or {}).get('Content-Type') or '').lower()
        if 'mpeg' in content_type or 'mp3' in content_type:
            return 'mp3'
        if 'wav' in content_type or 'wave' in content_type:
            return 'wav'
        data = response.content or b''
        if data.startswith(b'ID3') or data[:2] == b'\xff\xfb':
            return 'mp3'
        if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
            return 'wav'
        # research 客户端默认按 mp3 下载后再转发送
        return 'mp3'

    @staticmethod
    def _build_error_message(response: requests.Response) -> str:
        prefix = f'Tiax TTS 失败: HTTP {response.status_code}'
        text = (response.text or '').strip()
        return f'{prefix} {text[:300]}'.strip()


class BertVits2Txt2WavProvider:
    name = 'bert_vits2'
    DEFAULT_BASE_URL = 'http://127.0.0.1:23456/voice/bert-vits2'
    DEFAULT_SPEAKER_ID = '1'
    _SPEAKER_ALIASES = {
        '0': '0',
        '1': '1',
        'c_sui': '0',
        'c-sui': '0',
        'sui_best': '1',
        'sui-best': '1',
        'mansui': '1',
        '满穗': '1',
    }

    def __init__(
        self,
        api_key: str | None = None,
        reference_id: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str | None = None,
        session: requests.sessions.Session | None = None,
    ):
        self.api_key = str(api_key or '').strip()
        self.reference_id = str(reference_id or self.DEFAULT_SPEAKER_ID).strip() or self.DEFAULT_SPEAKER_ID
        self.base_url = str(base_url or self.DEFAULT_BASE_URL).strip() or self.DEFAULT_BASE_URL
        self.model = str(model or '').strip()
        self.session = session or requests.Session()

    def synthesize(self, request: Txt2WavRequest) -> SynthesizedAudio:
        speaker_id = self._resolve_speaker_id(request.speaker_id)
        extra = dict(request.provider_options or {})
        audio_format = str(extra.pop('format', 'wav') or 'wav').strip().lower() or 'wav'
        payload: dict[str, Any] = {
            'text': request.text,
            'id': speaker_id,
            'lang': str(extra.pop('lang', 'zh') or 'zh').strip() or 'zh',
            'format': audio_format,
            'length': self._speed_to_length(request.speed, extra.pop('length', None)),
            'noise': self._float_or_default(extra.pop('noise', None), 0.33),
            'noisew': self._float_or_default(extra.pop('noisew', None), 0.4),
            'sdp_ratio': self._float_or_default(extra.pop('sdp_ratio', None), 0.2),
            'text_prompt': str(extra.pop('text_prompt', 'Happy') or 'Happy'),
        }
        optional_fields = (
            ('segment_size', int, 50),
            ('emotion', int, 0),
            ('style_weight', float, 0.7),
        )
        for key, caster, default in optional_fields:
            if key in extra:
                try:
                    payload[key] = caster(extra.pop(key))
                except (TypeError, ValueError):
                    payload[key] = default
        style_text = extra.pop('style_text', None)
        if style_text is not None:
            payload['style_text'] = style_text
        if extra:
            payload.update(extra)

        try:
            response = self.session.post(
                self.base_url,
                json=payload,
                timeout=request.timeout,
            )
        except requests.RequestException as exc:
            raise Txt2WavError(f'BERT-VITS2 TTS 连接失败: {exc}') from exc
        if response.status_code >= 400:
            raise Txt2WavError(self._build_error_message(response))
        if self._looks_non_audio_response(response):
            raise Txt2WavError(self._build_error_message(response, prefix='BERT-VITS2 TTS 返回了非音频响应'))
        audio_bytes = response.content or b''
        if not audio_bytes:
            raise Txt2WavError('BERT-VITS2 返回了空音频数据。')
        return SynthesizedAudio(
            audio_bytes=audio_bytes,
            format=self._infer_audio_format(response, audio_format),
            content_type=response.headers.get('Content-Type'),
            sample_rate=request.sample_rate,
            provider=self.name,
        )

    def _resolve_speaker_id(self, speaker_id: str | None) -> int:
        raw = str(speaker_id or self.reference_id or self.DEFAULT_SPEAKER_ID).strip().lower()
        resolved = self._SPEAKER_ALIASES.get(raw, raw)
        try:
            return int(resolved)
        except (TypeError, ValueError) as exc:
            raise Txt2WavError(f'BERT-VITS2 speaker_id 无效: {speaker_id!r}') from exc

    @staticmethod
    def _speed_to_length(speed: float, explicit_length: Any) -> float:
        if explicit_length is not None:
            try:
                value = float(explicit_length)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        try:
            speed_value = float(speed)
        except (TypeError, ValueError):
            speed_value = 1.0
        if speed_value <= 0:
            speed_value = 1.0
        return max(0.1, min(4.0, 1.0 / speed_value))

    @staticmethod
    def _float_or_default(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _looks_non_audio_response(response: requests.Response) -> bool:
        content_type = str((response.headers or {}).get('Content-Type') or '').lower()
        return 'application/json' in content_type or 'text/' in content_type

    @staticmethod
    def _infer_audio_format(response: requests.Response, requested_format: str) -> str:
        content_type = str((response.headers or {}).get('Content-Type') or '').lower()
        if 'mpeg' in content_type or 'mp3' in content_type:
            return 'mp3'
        if 'wav' in content_type or 'wave' in content_type:
            return 'wav'
        data = response.content or b''
        if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
            return 'wav'
        if data.startswith(b'ID3') or data[:2] == b'\xff\xfb':
            return 'mp3'
        return requested_format or 'wav'

    @staticmethod
    def _build_error_message(response: requests.Response, prefix: str | None = None) -> str:
        title = prefix or f'BERT-VITS2 TTS 失败: HTTP {response.status_code}'
        text = (response.text or '').strip()
        return f'{title} {text[:300]}'.strip()


class MansuiUnifiedTxt2WavProvider:
    name = 'mansui_unified'
    DEFAULT_BASE_URL = 'http://127.0.0.1:23458/tts'
    DEFAULT_EMOTION = 'default'
    _EMOTION_ALIASES = {
        '': 'default',
        'default': 'default',
        '1': 'default',
        'mansui': 'default',
        '满穗': 'default',
        'sui_best': 'default',
        'sui-best': 'default',
        'sui_full': 'default',
        'sui-full': 'default',
        'sui': 'default',
        'fear': 'fear',
        'narration': 'narration',
        'pain': 'pain',
        'angry': 'angry',
    }

    def __init__(
        self,
        api_key: str | None = None,
        reference_id: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str | None = None,
        session: requests.sessions.Session | None = None,
    ):
        self.api_key = str(api_key or '').strip()
        self.reference_id = str(reference_id or '').strip() or self.DEFAULT_EMOTION
        self.base_url = str(base_url or '').strip() or self.DEFAULT_BASE_URL
        self.engine = str(model or '').strip() or 'gateway'
        self.session = session or requests.Session()

    def synthesize(self, request: Txt2WavRequest) -> SynthesizedAudio:
        extra = dict(request.provider_options or {})
        payload: dict[str, Any] = {
            'text': request.text,
            'emotion': self._resolve_emotion(
                str(extra.pop('emotion', '') or '').strip() or None,
                request.speaker_id,
            ),
            'speed': self._clamp_float(request.speed, default=1.0, minimum=0.5, maximum=2.0),
        }
        optional_fields = {
            'seed': int,
            'top_k': int,
            'top_p': float,
            'temperature': float,
        }
        for key, caster in optional_fields.items():
            if key not in extra:
                continue
            try:
                payload[key] = caster(extra.pop(key))
            except (TypeError, ValueError):
                continue

        if extra:
            payload.update(extra)

        try:
            response = self.session.post(
                self.base_url,
                json=payload,
                timeout=request.timeout,
            )
        except requests.RequestException as exc:
            raise Txt2WavError(f'满穗 TTS 网关连接失败: {exc}') from exc
            
        if response.status_code >= 400:
            raise Txt2WavError(self._build_error_message(response))
            
        if self._looks_non_audio_response(response):
            raise Txt2WavError(self._build_error_message(response, prefix='满穗 TTS 返回了非音频响应'))
            
        audio_bytes = response.content or b''
        if not audio_bytes:
            raise Txt2WavError('满穗 TTS 返回了空音频数据。')
            
        return SynthesizedAudio(
            audio_bytes=audio_bytes,
            format='wav',
            content_type=response.headers.get('Content-Type'),
            sample_rate=request.sample_rate,
            provider=self.name,
        )

    def _resolve_emotion(self, explicit_emotion: str | None, speaker_id: str | None) -> str:
        raw = str(explicit_emotion or speaker_id or self.reference_id or self.DEFAULT_EMOTION).strip().lower()
        emotion = self._EMOTION_ALIASES.get(raw, raw)
        if emotion not in {'default', 'fear', 'narration', 'pain', 'angry'}:
            return self.DEFAULT_EMOTION
        return emotion

    @staticmethod
    def _clamp_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _looks_non_audio_response(response: requests.Response) -> bool:
        content_type = str((response.headers or {}).get('Content-Type') or '').lower()
        return 'application/json' in content_type or 'text/' in content_type

    @staticmethod
    def _build_error_message(response: requests.Response, prefix: str | None = None) -> str:
        title = prefix or f'满穗 TTS 失败: HTTP {response.status_code}'
        try:
            data = response.json()
        except ValueError:
            text = (response.text or '').strip()
            return f'{title} {text[:300]}'.strip()
        if isinstance(data, dict):
            detail = data.get('detail') or data.get('message') or data.get('error')
            if detail:
                return f'{title} {str(detail)[:300]}'.strip()
            return f'{title} {json.dumps(data, ensure_ascii=False)[:300]}'.strip()
        return f'{title} {str(data)[:300]}'.strip()


ProviderFactory = Callable[..., Txt2WavProvider]

_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    'fish_audio': FishAudioTxt2WavProvider,
    'tiax': TiaxTxt2WavProvider,
    'bert_vits2': BertVits2Txt2WavProvider,
    'vits_api': BertVits2Txt2WavProvider,
    'mansui_vits': BertVits2Txt2WavProvider,
    'mansui_unified': MansuiUnifiedTxt2WavProvider,
    'cosyvoice': MansuiUnifiedTxt2WavProvider,
}


def register_txt2wav_provider(name: str, factory: ProviderFactory) -> None:
    provider_name = str(name or '').strip().lower()
    if not provider_name:
        raise Txt2WavError('provider 名称不能为空。')
    _PROVIDER_FACTORIES[provider_name] = factory


def create_txt2wav_provider(name: str = 'fish_audio', **kwargs) -> Txt2WavProvider:
    provider_name = str(name or '').strip().lower()
    factory = _PROVIDER_FACTORIES.get(provider_name)
    if factory is None:
        available = ', '.join(sorted(_PROVIDER_FACTORIES))
        raise Txt2WavError(f'未知 txt2wav provider: {provider_name!r}，可用: {available}')
    return factory(**kwargs)


def create_default_txt2wav_service(
    *,
    provider: str = 'fish_audio',
    output_dir: str | Path = 'data/tmp',
    api_key: str | None = None,
    reference_id: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> Txt2WavService:
    provider_kwargs: dict[str, Any] = {}
    if provider == 'fish_audio':
        provider_kwargs = {
            'api_key': api_key or os.getenv('FISH_AUDIO_API_KEY', ''),
            'reference_id': reference_id or os.getenv('FISH_AUDIO_REFERENCE_ID') or os.getenv('MANSUI_TTS_REFERENCE_ID'),
            'base_url': base_url or os.getenv('FISH_AUDIO_BASE_URL', 'https://api.fish.audio'),
            'model': model or os.getenv('FISH_AUDIO_MODEL', 's2-pro'),
        }
    elif provider == 'tiax':
        provider_kwargs = {
            'api_key': api_key or os.getenv('AI_TTS_API_KEY', ''),
            'reference_id': reference_id or os.getenv('AI_TTS_REFERENCE_ID') or os.getenv('MANSUI_TTS_REFERENCE_ID') or '130',
            'base_url': base_url or os.getenv('AI_TTS_BASE_URL', 'https://www.tiax.pw/API/yuyin.php'),
        }
    elif provider in {'bert_vits2', 'vits_api', 'mansui_vits'}:
        provider_kwargs = {
            'api_key': api_key or os.getenv('AI_TTS_API_KEY', ''),
            'reference_id': reference_id or os.getenv('AI_TTS_REFERENCE_ID') or os.getenv('MANSUI_TTS_REFERENCE_ID') or '1',
            'base_url': base_url or os.getenv('AI_TTS_BASE_URL', BertVits2Txt2WavProvider.DEFAULT_BASE_URL),
            'model': model or os.getenv('AI_TTS_MODEL', ''),
        }
    elif provider in {'mansui_unified', 'cosyvoice'}:
        provider_kwargs = {
            'api_key': api_key or os.getenv('AI_TTS_API_KEY', ''),
            'reference_id': reference_id or os.getenv('AI_TTS_REFERENCE_ID') or os.getenv('MANSUI_TTS_REFERENCE_ID') or 'Sui_Full',
            'base_url': base_url or os.getenv('AI_TTS_BASE_URL', MansuiUnifiedTxt2WavProvider.DEFAULT_BASE_URL),
            'model': model or os.getenv('AI_TTS_MODEL', 'cosyvoice'),
        }
    engine = create_txt2wav_provider(provider, **provider_kwargs)
    return Txt2WavService(engine, output_dir=output_dir)
