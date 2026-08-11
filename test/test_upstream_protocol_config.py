import json
import tempfile
import unittest
from pathlib import Path

from core.model_manager import ModelManager


class UpstreamProtocolConfigTests(unittest.TestCase):
    def _manager(self, upstream):
        config = {
            'upstreams': [upstream],
            'channels': [{
                'name': 'main',
                'strategy': 'fallback',
                'models': [{'upstream': upstream['name'], 'model_id': 'model'}],
            }],
            'roles': {'main': 'main'},
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'models_config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return ModelManager(str(path)), path

    def test_protocols_map_to_unversioned_endpoint_paths(self):
        expected = {
            'anthropic': '/messages',
            'completions': '/chat/completions',
            'responses': '/responses',
        }
        for protocol, path in expected.items():
            with self.subTest(protocol=protocol):
                mm, _config_path = self._manager({
                    'name': protocol,
                    'base_url': 'https://api.example/v2',
                    'api_key': 'key',
                    'protocol': protocol,
                })
                model = mm.get_model_for_role('main')
                self.assertEqual(model['base_url'], 'https://api.example/v2')
                self.assertEqual(model['messages_path'], path)
                self.assertEqual(model['base_url'] + model['messages_path'], f'https://api.example/v2{path}')

    def test_new_protocol_config_does_not_add_v1(self):
        mm, _config_path = self._manager({
            'name': 'plain',
            'base_url': 'https://api.example',
            'api_key': 'key',
            'protocol': 'responses',
        })
        model = mm.get_model_for_role('main')
        self.assertEqual(model['base_url'] + model['messages_path'], 'https://api.example/responses')

    def test_legacy_versioned_path_moves_prefix_to_base_url(self):
        mm, config_path = self._manager({
            'name': 'legacy',
            'base_url': 'https://api.example',
            'api_key': 'key',
            'messages_path': '/v2/chat/completions',
        })
        model = mm.get_model_for_role('main')
        self.assertEqual(model['base_url'], 'https://api.example/v2')
        self.assertEqual(model['messages_path'], '/chat/completions')
        saved = json.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['upstreams'][0]['protocol'], 'completions')
        self.assertNotIn('messages_path', saved['upstreams'][0])

    def test_legacy_base_with_version_is_not_duplicated(self):
        mm, _config_path = self._manager({
            'name': 'legacy',
            'base_url': 'https://api.example/v1',
            'api_key': 'key',
            'messages_path': '/v1/responses',
        })
        model = mm.get_model_for_role('main')
        self.assertEqual(model['base_url'], 'https://api.example/v1')
        self.assertEqual(model['messages_path'], '/responses')

    def test_add_and_update_upstream_accept_protocol_only(self):
        mm, config_path = self._manager({
            'name': 'first',
            'base_url': 'https://api.example/v1',
            'api_key': 'key',
            'protocol': 'anthropic',
        })
        ok, _message = mm.add_upstream(
            name='second',
            base_url='https://api.example/v2',
            api_key='key',
            protocol='responses',
        )
        self.assertTrue(ok)
        ok, _message = mm.update_upstream('second', protocol='completions')
        self.assertTrue(ok)
        saved = json.loads(config_path.read_text(encoding='utf-8'))
        second = next(item for item in saved['upstreams'] if item['name'] == 'second')
        self.assertEqual(second['protocol'], 'completions')
        self.assertNotIn('messages_path', second)

    def test_invalid_protocol_is_rejected(self):
        mm, _config_path = self._manager({
            'name': 'first',
            'base_url': 'https://api.example/v1',
            'api_key': 'key',
            'protocol': 'anthropic',
        })
        ok, message = mm.add_upstream(
            name='bad',
            base_url='https://api.example/v1',
            api_key='key',
            protocol='custom',
        )
        self.assertFalse(ok)
        self.assertIn('protocol', message)


if __name__ == '__main__':
    unittest.main()
