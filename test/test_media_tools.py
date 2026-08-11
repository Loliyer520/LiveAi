import os
import tempfile
import unittest
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools
from pack.napcat import NapcatBot


class MediaToolTests(unittest.IsolatedAsyncioTestCase):
    def test_immediate_tools_expose_file_and_voice_senders(self):
        names = {item['name'] for item in build_tools(immediate_mode=True)}
        self.assertIn('send_file', names)
        self.assertIn('send_voice', names)

    async def test_send_voice_tool_synthesizes_then_sends_record(self):
        fd, temp_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        Path(temp_path).write_bytes(b'RIFFvoice')
        try:
            runtime = object.__new__(AIOrchestrator)
            runtime.config = SimpleNamespace(history_limit=20)
            runtime._short_text = lambda text, _limit=0: str(text or '')
            runtime._txt2wav_service = SimpleNamespace(
                text_to_audio=Mock(return_value=Path(temp_path)),
            )
            runtime.tools = SimpleNamespace(
                send_chat_record=Mock(return_value={'data': {'message_id': 'voice-1'}}),
                record_tool_use=Mock(),
            )

            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'send_voice',
                {'text': '你好呀', 'emotion': 'angry', 'speaker_id': 'mansui', 'speed': 1.2, 'volume': 3},
            )

            runtime._txt2wav_service.text_to_audio.assert_called_once_with(
                '你好呀',
                speaker_id='mansui',
                speed=1.2,
                volume=3.0,
                provider_options={'emotion': 'angry'},
            )
            runtime.tools.send_chat_record.assert_called_once()
            args = runtime.tools.send_chat_record.call_args.args
            self.assertEqual(args[0], 'group')
            self.assertEqual(args[1], 7)
            self.assertTrue(args[2].startswith('base64://'))
            self.assertEqual(b64decode(args[2][9:]), b'RIFFvoice')
            self.assertIn('已发送语音', result)
            self.assertIn('message_id: voice-1', result)
        finally:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass

    def test_get_txt2wav_service_prefers_config_values(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(
            tts_provider='fish_audio',
            tts_api_key='cfg-key',
            tts_reference_id='cfg-ref',
            tts_base_url='https://tts.example.com',
            tts_model='cfg-model',
        )
        runtime._txt2wav_service = None

        service = AIOrchestrator._get_txt2wav_service(runtime)

        self.assertEqual(service.provider.api_key, 'cfg-key')
        self.assertEqual(service.provider.reference_id, 'cfg-ref')
        self.assertEqual(service.provider.base_url, 'https://tts.example.com')
        self.assertEqual(service.provider.model, 'cfg-model')

    async def test_send_file_tool_dispatches_to_contact_layer(self):
        fd, temp_path = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        Path(temp_path).write_text('hello', encoding='utf-8')
        resolved = str(Path(temp_path).resolve())
        try:
            runtime = object.__new__(AIOrchestrator)
            runtime.config = SimpleNamespace(history_limit=20)
            runtime._short_text = lambda text, _limit=0: str(text or '')
            runtime.tools = SimpleNamespace(
                send_chat_file=Mock(return_value={'data': {'message_id': 'file-1'}}),
                record_tool_use=Mock(),
            )

            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'send_file',
                {'path': resolved, 'name': 'demo.txt'},
            )

            runtime.tools.send_chat_file.assert_called_once()
            args = runtime.tools.send_chat_file.call_args.args
            self.assertEqual(args[0], 'group')
            self.assertEqual(args[1], 7)
            self.assertTrue(args[2].startswith('base64://'))
            self.assertEqual(b64decode(args[2][9:]).decode('utf-8'), 'hello')
            self.assertEqual(args[3], 'demo.txt')
            self.assertIn('已发送文件', result)
            self.assertIn('message_id: file-1', result)
        finally:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass

    async def test_send_sticker_tool_prefers_mface_over_image(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(history_limit=20)
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime._sticker_cache = []
        runtime._sticker_cache_at = 0.0
        runtime._sticker_cache_ttl = 300.0
        runtime.bot = SimpleNamespace(
            fetch_custom_face=Mock(return_value=[{
                'emoji_id': 'emo-1',
                'emoji_package_id': 'pkg-2',
                'key': 'key-3',
                'url': 'https://example.com/preview.gif',
            }]),
            send_mface=Mock(return_value={'data': {'message_id': 'mface-1'}}),
            send_image=Mock(),
        )
        runtime.repo = SimpleNamespace(get_setting=Mock(return_value={}), set_setting=Mock())
        runtime.tools = SimpleNamespace(record_tool_use=Mock())

        result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '7',
            'agent-1',
            'send_sticker',
            {'index': 1},
        )

        runtime.bot.send_mface.assert_called_once_with('group', 7, 'emo-1', 'pkg-2', 'key-3', '[表情包]')
        runtime.bot.send_image.assert_not_called()
        self.assertIn('已发送第 1 个表情', result)

    async def test_download_file_tool_uses_url_when_napcat_local_path_is_unreachable(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(history_limit=20)
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime.bot = SimpleNamespace(
            get_file=Mock(return_value={
                'file': '/app/napcat/tmp/demo.txt',
                'url': '/get_file/demo',
                'size': 5,
            }),
            download_file_to=Mock(side_effect=lambda _url, dest: Path(dest).write_bytes(b'hello')),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        saved_path = Path('c:/Users/loliyc/Documents/Code/LiveAi/data/file/7/demo.txt')
        try:
            if saved_path.exists():
                saved_path.unlink()
            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'download_file',
                {'file_id': 'file-1', 'file_name': 'demo.txt'},
            )

            runtime.bot.download_file_to.assert_called_once()
            args = runtime.bot.download_file_to.call_args.args
            self.assertEqual(args[0], '/get_file/demo')
            self.assertEqual(Path(args[1]), saved_path)
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.read_bytes(), b'hello')
            self.assertIn('文件已保存', result)
        finally:
            try:
                saved_path.unlink()
            except FileNotFoundError:
                pass

    def test_napcat_send_file_accepts_base64_content(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        calls = []

        def fake_post(action, params):
            calls.append((action, params))
            return {'status': 'ok', 'retcode': 0, 'data': None}

        bot.post = fake_post
        result = bot.send_file('group', 7, 'base64://aGVsbG8=', 'demo.txt')

        self.assertEqual(result, {'status': 'ok', 'retcode': 0, 'data': None})
        self.assertEqual(calls, [
            ('upload_group_file', {'group_id': 7, 'file': 'base64://aGVsbG8=', 'name': 'demo.txt'})
        ])

    def test_napcat_recall_message_accepts_optional_scope_args(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        calls = []

        def fake_post(action, params):
            calls.append((action, params))
            return {'status': 'ok', 'retcode': 0, 'data': None}

        bot.post = fake_post
        result = bot.recall_message('123', 'group', 7)

        self.assertEqual(result, {'status': 'ok', 'retcode': 0, 'data': None})
        self.assertEqual(calls, [('delete_msg', {'message_id': '123'})])

    def test_napcat_send_mface_uses_mface_segment(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        calls = []

        def fake_post(action, params):
            calls.append((action, params))
            return {'status': 'ok', 'retcode': 0, 'data': {'message_id': 99}}

        bot.post = fake_post
        result = bot.send_mface('group', 7, 'emo-1', 'pkg-2', 'key-3', '[表情包]')

        self.assertEqual(result, {'status': 'ok', 'retcode': 0, 'data': {'message_id': 99}})
        self.assertEqual(calls, [
            ('send_group_msg', {
                'group_id': 7,
                'message': [{
                    'type': 'mface',
                    'data': {
                        'emoji_id': 'emo-1',
                        'emoji_package_id': 'pkg-2',
                        'key': 'key-3',
                        'summary': '[表情包]',
                    },
                }],
            }),
        ])

    def test_napcat_fetch_custom_face_preserves_structured_fields(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'ok',
            'retcode': 0,
            'data': [
                {'emoji_id': 'emo-1', 'emoji_package_id': 'pkg-2', 'key': 'key-3', 'url': 'https://a/img.gif'},
                'https://b/img.gif',
            ],
        }

        result = bot.fetch_custom_face(12)

        self.assertEqual(result, [
            {'emoji_id': 'emo-1', 'emoji_package_id': 'pkg-2', 'key': 'key-3', 'url': 'https://a/img.gif'},
            {'url': 'https://b/img.gif'},
        ])

    def test_napcat_get_file_normalizes_path_and_url_aliases(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1, http_access_token='secret')
        bot.post = lambda *_args, **_kwargs: {
            'status': 'ok',
            'retcode': 0,
            'data': {
                'file_path': '/app/napcat/files/demo.txt',
                'download_url': '/download/demo.txt',
                'name': 'demo.txt',
                'size': 12,
            },
        }

        result = bot.get_file('file-1')

        self.assertEqual(result['file'], '/app/napcat/files/demo.txt')
        self.assertEqual(result['url'], 'http://napcat:3000/download/demo.txt')
        self.assertEqual(result['name'], 'demo.txt')

    def test_napcat_download_file_to_uses_auth_for_same_origin_url(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1, http_access_token='secret')

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                _ = chunk_size
                yield b'abc'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as tmp, patch('pack.napcat.requests.get', return_value=_FakeResponse()) as mock_get:
            dest = Path(tmp) / 'demo.bin'
            bot.download_file_to('/files/demo.bin', str(dest))

            self.assertEqual(dest.read_bytes(), b'abc')
            mock_get.assert_called_once()
            self.assertEqual(mock_get.call_args.args[0], 'http://napcat:3000/files/demo.bin')
            self.assertEqual(
                mock_get.call_args.kwargs['headers'],
                {'Authorization': 'Bearer secret'},
            )


    def test_napcat_get_file_normalizes_size_aliases(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'ok',
            'retcode': 0,
            'data': {'file_path': '/x/y.txt', 'download_url': '/dl/y.txt', 'file_size': '12345'},
        }
        result = bot.get_file('file-1')
        self.assertEqual(result['file'], '/x/y.txt')
        self.assertEqual(result['url'], 'http://napcat:3000/dl/y.txt')
        self.assertEqual(result['size'], 12345)

    async def test_download_file_deletes_partial_file_when_download_fails(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(history_limit=20)
        runtime._short_text = lambda text, _limit=0: str(text or '')
        failed_path = Path('c:/Users/loliyc/Documents/Code/LiveAi/data/file/7/partial.bin')

        def _fail_after_partial(_url, dest):
            Path(dest).write_bytes(b'partial')
            raise RuntimeError('network down')

        runtime.bot = SimpleNamespace(
            get_file=Mock(return_value={'url': '/get_file/partial', 'size': 5}),
            download_file_to=Mock(side_effect=_fail_after_partial),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        try:
            if failed_path.exists():
                failed_path.unlink()
            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'download_file',
                {'file_id': 'file-x', 'file_name': 'partial.bin'},
            )
            self.assertIn('文件保存失败', result)
            self.assertFalse(failed_path.exists(), '半成品必须被清理（落盘即清）')
        finally:
            try:
                failed_path.unlink()
            except FileNotFoundError:
                pass

    async def test_download_file_rejects_oversized_actual_content(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(history_limit=20)
        runtime._short_text = lambda text, _limit=0: str(text or '')
        big_path = Path('c:/Users/loliyc/Documents/Code/LiveAi/data/file/7/huge.bin')

        def _write_huge(_url, dest):
            # 实际写入超过 20MB：NapCat size=0 时下载前检查失效，必须靠下载后二次校验兜底
            with open(dest, 'wb') as fh:
                fh.write(b'\0' * (21 * 1024 * 1024))

        runtime.bot = SimpleNamespace(
            get_file=Mock(return_value={'url': '/get_file/huge', 'size': 0}),
            download_file_to=Mock(side_effect=_write_huge),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        try:
            if big_path.exists():
                big_path.unlink()
            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'download_file',
                {'file_id': 'file-h', 'file_name': 'huge.bin'},
            )
            self.assertIn('超过 20MB 限制', result)
            self.assertFalse(big_path.exists(), '超限文件必须删除（落盘即清）')
        finally:
            try:
                big_path.unlink()
            except FileNotFoundError:
                pass

    async def test_download_file_sanitizes_path_traversal_name(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(history_limit=20)
        runtime._short_text = lambda text, _limit=0: str(text or '')
        saved_path = Path('c:/Users/loliyc/Documents/Code/LiveAi/data/file/7/evil.txt')
        runtime.bot = SimpleNamespace(
            get_file=Mock(return_value={'url': '/get_file/evil', 'size': 5}),
            download_file_to=Mock(side_effect=lambda _url, dest: Path(dest).write_bytes(b'x')),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        try:
            if saved_path.exists():
                saved_path.unlink()
            result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'group',
                '7',
                'agent-1',
                'download_file',
                {'file_id': 'file-e', 'file_name': '../../evil.txt'},
            )
            self.assertIn('文件已保存', result)
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.name, 'evil.txt')
        finally:
            try:
                saved_path.unlink()
            except FileNotFoundError:
                pass


    def test_napcat_get_file_raises_on_failed_status(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'failed',
            'retcode': 200,
            'data': None,
            'message': '文件不存在',
        }
        with self.assertRaises(RuntimeError):
            bot.get_file('file-missing')

    def test_napcat_get_file_falls_back_to_get_file_v2(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1)
        calls = []

        def fake_post(action, params):
            calls.append((action, params))
            if action == 'get_file':
                return {'status': 'failed', 'retcode': 200, 'data': None, 'message': '不支持的Api app'}
            return {
                'status': 'ok',
                'retcode': 0,
                'data': {'file': '/x/y.txt', 'url': '/dl/y.txt', 'file_name': 'y.txt', 'size': '12345'},
            }

        bot.post = fake_post
        result = bot.get_file('file-1')
        self.assertEqual(calls, [('get_file', {'file_id': 'file-1'}), ('get_file_v2', {'file_id': 'file-1'})])
        self.assertEqual(result['file'], '/x/y.txt')
        self.assertEqual(result['url'], 'http://napcat:3000/dl/y.txt')
        self.assertEqual(result['size'], 12345)

    def test_napcat_download_file_to_rejects_json_content_type(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1)

        class _JsonResponse:
            headers = {'Content-Type': 'application/json'}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                _ = chunk_size
                yield b'{"status":"failed"}'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as tmp, patch('pack.napcat.requests.get', return_value=_JsonResponse()):
            dest = Path(tmp) / 'x.bin'
            with self.assertRaises(RuntimeError):
                bot.download_file_to('/files/x.bin', str(dest))
            self.assertFalse(dest.exists())

    def test_napcat_download_file_to_rejects_protocol_error_body(self):
        bot = NapcatBot('ws://invalid', 'http://napcat:3000', 1)

        class _ErrorBodyResponse:
            headers = {'Content-Type': 'application/octet-stream'}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                _ = chunk_size
                yield b'{"status":"failed","retcode":200,"data":null,"message":"not supported"}'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as tmp, patch('pack.napcat.requests.get', return_value=_ErrorBodyResponse()):
            dest = Path(tmp) / 'x.bin'
            with self.assertRaises(RuntimeError):
                bot.download_file_to('/files/x.bin', str(dest))
            self.assertFalse(dest.exists(), '占位文件必须被删除（落盘即清）')


if __name__ == '__main__':
    unittest.main()
