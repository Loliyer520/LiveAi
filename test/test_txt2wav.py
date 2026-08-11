import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pack.txt2wav import (
    BertVits2Txt2WavProvider,
    FishAudioTxt2WavProvider,
    MansuiUnifiedTxt2WavProvider,
    SynthesizedAudio,
    TiaxTxt2WavProvider,
    Txt2WavError,
    Txt2WavRequest,
    Txt2WavService,
    create_default_txt2wav_service,
    create_txt2wav_provider,
    register_txt2wav_provider,
)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({
            'url': url,
            'headers': headers,
            'json': json,
            'timeout': timeout,
        })
        return self.response

    def get(self, url, params=None, timeout=None):
        self.calls.append({
            'url': url,
            'params': params,
            'timeout': timeout,
            'method': 'GET',
        })
        return self.response


class Txt2WavTests(unittest.TestCase):
    def test_service_writes_wav_using_provider_contract(self):
        class FakeProvider:
            name = 'fake'

            def synthesize(self, request):
                self.last_request = request
                return SynthesizedAudio(audio_bytes=b'RIFFdemo', format='wav', provider='fake')

        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            service = Txt2WavService(provider, output_dir=tmpdir)
            output = service.text_to_wav('你好', output_path='voice')
            self.assertEqual(output.suffix, '.wav')
            self.assertEqual(output.read_bytes(), b'RIFFdemo')
            self.assertEqual(provider.last_request.text, '你好')

    def test_fish_provider_posts_expected_payload(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'RIFFdata',
            headers={'Content-Type': 'audio/wav'},
            json=lambda: {'unused': True},
            text='',
        )
        session = FakeSession(response)
        provider = FishAudioTxt2WavProvider(
            api_key='secret',
            reference_id='voice-1',
            session=session,
        )

        audio = provider.synthesize(Txt2WavRequest(text='测试', speed=1.1, volume=2.0))

        self.assertEqual(audio.audio_bytes, b'RIFFdata')
        self.assertEqual(audio.format, 'wav')
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call['url'], 'https://api.fish.audio/v1/tts')
        self.assertEqual(call['headers']['Authorization'], 'Bearer secret')
        self.assertEqual(call['headers']['model'], 's2-pro')
        self.assertEqual(call['json']['reference_id'], 'voice-1')
        self.assertEqual(call['json']['format'], 'wav')
        self.assertEqual(call['json']['prosody']['speed'], 1.1)
        self.assertEqual(call['json']['prosody']['volume'], 2.0)

    def test_fish_provider_raises_clean_error(self):
        response = SimpleNamespace(
            status_code=401,
            content=b'',
            headers={'Content-Type': 'application/json'},
            json=lambda: {'message': 'bad token'},
            text='{"message":"bad token"}',
        )
        provider = FishAudioTxt2WavProvider(
            api_key='secret',
            reference_id='voice-1',
            session=FakeSession(response),
        )
        with self.assertRaisesRegex(Txt2WavError, 'bad token'):
            provider.synthesize(Txt2WavRequest(text='测试'))

    def test_tiax_provider_uses_researched_query_shape(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'ID3mockmp3',
            headers={'Content-Type': 'audio/mpeg'},
            text='',
        )
        session = FakeSession(response)
        provider = TiaxTxt2WavProvider(
            api_key='public-key',
            reference_id='130',
            base_url='https://www.tiax.pw/API/yuyin.php',
            session=session,
        )

        audio = provider.synthesize(Txt2WavRequest(text='你好', speaker_id='132'))

        self.assertEqual(audio.audio_bytes, b'ID3mockmp3')
        self.assertEqual(audio.format, 'mp3')
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call['method'], 'GET')
        self.assertEqual(call['url'], 'https://www.tiax.pw/API/yuyin.php')
        self.assertEqual(call['params']['msg'], '你好')
        self.assertEqual(call['params']['apikey'], 'public-key')
        self.assertEqual(call['params']['ys'], '132')

    def test_service_keeps_provider_audio_suffix(self):
        class FakeMp3Provider:
            name = 'fake-mp3'

            def synthesize(self, request):
                return SynthesizedAudio(audio_bytes=b'ID3demo', format='mp3', provider='fake-mp3')

        with tempfile.TemporaryDirectory() as tmpdir:
            service = Txt2WavService(FakeMp3Provider(), output_dir=tmpdir)
            output = service.text_to_audio('你好', output_path='voice.wav')
            self.assertEqual(output.suffix, '.mp3')
            self.assertEqual(output.read_bytes(), b'ID3demo')

    def test_bert_vits2_provider_posts_json_payload(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'RIFFdemoWAVE',
            headers={'Content-Type': 'audio/wav'},
            text='',
        )
        session = FakeSession(response)
        provider = BertVits2Txt2WavProvider(
            reference_id='1',
            base_url='http://127.0.0.1:23456/voice/bert-vits2',
            session=session,
        )

        audio = provider.synthesize(
            Txt2WavRequest(
                text='你好，我是满穗',
                speaker_id='mansui',
                speed=1.25,
            )
        )

        self.assertEqual(audio.audio_bytes, b'RIFFdemoWAVE')
        self.assertEqual(audio.format, 'wav')
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call['url'], 'http://127.0.0.1:23456/voice/bert-vits2')
        self.assertIsNone(call['headers'])
        self.assertEqual(call['json']['text'], '你好，我是满穗')
        self.assertEqual(call['json']['id'], 1)
        self.assertEqual(call['json']['lang'], 'zh')
        self.assertEqual(call['json']['format'], 'wav')
        self.assertAlmostEqual(call['json']['length'], 0.8)
        self.assertEqual(call['json']['text_prompt'], 'Happy')

    def test_bert_vits2_provider_rejects_text_response(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'not found',
            headers={'Content-Type': 'text/plain; charset=utf-8'},
            text='not found',
        )
        provider = BertVits2Txt2WavProvider(session=FakeSession(response))

        with self.assertRaisesRegex(Txt2WavError, '非音频响应'):
            provider.synthesize(Txt2WavRequest(text='测试'))

    def test_mansui_gateway_provider_posts_gateway_payload(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'RIFFdemoWAVE',
            headers={'Content-Type': 'audio/wav'},
            text='',
            json=lambda: {'unused': True},
        )
        session = FakeSession(response)
        provider = MansuiUnifiedTxt2WavProvider(
            reference_id='Sui_Full',
            base_url='http://127.0.0.1:23458/tts',
            session=session,
        )

        audio = provider.synthesize(
            Txt2WavRequest(
                text='你好，我是满穗',
                speaker_id='angry',
                speed=3.0,
                provider_options={
                    'seed': 7,
                    'top_k': 25,
                    'top_p': 0.8,
                    'temperature': 1.3,
                },
            )
        )

        self.assertEqual(audio.audio_bytes, b'RIFFdemoWAVE')
        self.assertEqual(audio.format, 'wav')
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call['url'], 'http://127.0.0.1:23458/tts')
        self.assertEqual(call['json']['text'], '你好，我是满穗')
        self.assertEqual(call['json']['emotion'], 'angry')
        self.assertEqual(call['json']['speed'], 2.0)
        self.assertEqual(call['json']['seed'], 7)
        self.assertEqual(call['json']['top_k'], 25)
        self.assertEqual(call['json']['top_p'], 0.8)
        self.assertEqual(call['json']['temperature'], 1.3)

    def test_mansui_gateway_provider_reads_detail_error(self):
        response = SimpleNamespace(
            status_code=400,
            content=b'',
            headers={'Content-Type': 'application/json'},
            json=lambda: {'detail': 'emotion 无效'},
            text='{"detail":"emotion 无效"}',
        )
        provider = MansuiUnifiedTxt2WavProvider(session=FakeSession(response))

        with self.assertRaisesRegex(Txt2WavError, 'emotion 无效'):
            provider.synthesize(Txt2WavRequest(text='测试'))

    def test_provider_registry_supports_swap(self):
        class SwapProvider:
            name = 'swap'

            def __init__(self, marker=None):
                self.marker = marker

            def synthesize(self, request):
                return SynthesizedAudio(audio_bytes=b'RIFFswap', format='wav', provider='swap')

        register_txt2wav_provider('swap', SwapProvider)
        provider = create_txt2wav_provider('swap', marker='ok')
        self.assertEqual(provider.marker, 'ok')

    def test_default_service_reads_env_for_fish_audio(self):
        old_key = os.environ.get('FISH_AUDIO_API_KEY')
        old_ref = os.environ.get('MANSUI_TTS_REFERENCE_ID')
        try:
            os.environ['FISH_AUDIO_API_KEY'] = 'env-key'
            os.environ['MANSUI_TTS_REFERENCE_ID'] = 'env-ref'
            service = create_default_txt2wav_service(output_dir='data/tmp')
            self.assertIsInstance(service.provider, FishAudioTxt2WavProvider)
            self.assertEqual(service.provider.api_key, 'env-key')
            self.assertEqual(service.provider.reference_id, 'env-ref')
        finally:
            if old_key is None:
                os.environ.pop('FISH_AUDIO_API_KEY', None)
            else:
                os.environ['FISH_AUDIO_API_KEY'] = old_key
            if old_ref is None:
                os.environ.pop('MANSUI_TTS_REFERENCE_ID', None)
            else:
                os.environ['MANSUI_TTS_REFERENCE_ID'] = old_ref

    def test_default_service_supports_tiax(self):
        service = create_default_txt2wav_service(
            provider='tiax',
            output_dir='data/tmp',
            api_key='cfg-key',
            reference_id='130',
            base_url='https://www.tiax.pw/API/yuyin.php',
        )
        self.assertIsInstance(service.provider, TiaxTxt2WavProvider)
        self.assertEqual(service.provider.api_key, 'cfg-key')
        self.assertEqual(service.provider.reference_id, '130')
        self.assertEqual(service.provider.base_url, 'https://www.tiax.pw/API/yuyin.php')

    def test_default_service_supports_bert_vits2(self):
        service = create_default_txt2wav_service(
            provider='bert_vits2',
            output_dir='data/tmp',
            reference_id='1',
            base_url='http://127.0.0.1:23456/voice/bert-vits2',
        )
        self.assertIsInstance(service.provider, BertVits2Txt2WavProvider)
        self.assertEqual(service.provider.reference_id, '1')
        self.assertEqual(service.provider.base_url, 'http://127.0.0.1:23456/voice/bert-vits2')

    def test_default_service_supports_cosyvoice_gateway(self):
        service = create_default_txt2wav_service(
            provider='cosyvoice',
            output_dir='data/tmp',
            reference_id='Sui_Full',
            base_url='http://127.0.0.1:23458/tts',
            model='cosyvoice',
        )
        self.assertIsInstance(service.provider, MansuiUnifiedTxt2WavProvider)
        self.assertEqual(service.provider.reference_id, 'Sui_Full')
        self.assertEqual(service.provider.base_url, 'http://127.0.0.1:23458/tts')


if __name__ == '__main__':
    unittest.main()
