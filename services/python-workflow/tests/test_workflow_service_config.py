import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_service_config as config


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'service.json'
        override = patch.object(config, 'SERVICE_CONFIG_PATH', self.path)
        override.start()
        self.addCleanup(override.stop)
        self.addCleanup(config._load.cache_clear)
        config._load.cache_clear()

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        config._load.cache_clear()

    def test_file_overrides_environment_and_omitted_fields_use_defaults(self):
        self.write({})
        with patch.dict(os.environ, {'CODEX_AGENT_SOURCE': 'file', 'CODEX_MACHINE_MODEL': 'stale'}):
            self.assertIsNone(config.setting('machine_defaults.model'))
            self.assertEqual(config.setting('machine_defaults.model', 'default'), 'default')

    def test_missing_file_does_not_read_old_environment(self):
        with patch.dict(os.environ, {'CODEX_MACHINE_MODEL': 'stale'}):
            self.assertEqual(config.setting('machine_defaults.model', 'default'), 'default')

    def test_paths_are_relative_to_repository(self):
        self.write({'workflow_db': 'state/runtime.db'})
        self.assertEqual(Path(config.setting('workflow_db')), config.REPOSITORY_ROOT / 'state/runtime.db')

    def test_invalid_config(self):
        cases = [[], {'token': 'secret'}, {'agent_source': 'registry'}, {'agents_file': 'ignored.json'},
                 {'machine_defaults': []}, {'machine_defaults': {'token': 'secret'}},
                 {'machine_defaults': {'allow_write': 'true'}},
                 {'machine_defaults': {'allow_full_access': 'true'}},
                 {'machine_defaults': {'allow_full_access': True, 'allow_write': False}},
                 {'machine_defaults': {'protocol': 'http'}},
                 {'sidecar': {'port': True}}, {'sidecar': {'port': 65536}},
                 {'sidecar': {'token_file': 'relative.token'}},
                 {'sidecar': {'token_file': str(self.path), 'token_env': 'TEST_TOKEN'}}]
        for value in cases:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    config.setting('workflow_db')

    def test_malformed_json_does_not_echo_content(self):
        self.path.write_text('{secret-value', encoding='utf-8')
        with self.assertRaises(ValueError) as raised:
            config.setting('workflow_db')
        self.assertNotIn('secret-value', str(raised.exception))

    def test_sidecar_parser_reads_file(self):
        from workflow_sidecar import build_argument_parser
        self.write({'sidecar': {
            'gateway_url': 'http://192.0.2.10:8080', 'token_file': str(self.path.parent / 'not-read.token'),
            'host': '127.0.0.1', 'port': 8083}})
        args = build_argument_parser().parse_args([])
        self.assertEqual(args.gateway_url, 'http://192.0.2.10:8080')
        self.assertEqual(args.port, 8083)
        self.assertEqual(args.token_file, str(self.path.parent / 'not-read.token'))
        self.assertEqual(args.agent_id, 'registered-machine')

    def test_gateway_and_registry_use_file_defaults(self):
        from workflow_gateway import create_app
        self.write({'machine_defaults': {
            'cwd': str(self.path.parent), 'allow_write': True, 'allow_full_access': True,
            'sidecar_token_template': str(self.path.parent / '{ip}-{port}.token')}})
        with patch.dict(os.environ, {'CODEX_AGENT_SOURCE': 'file'}):
            registry = create_app(db_path=self.path.parent / 'runtime.db').state.gateway.registry
            group = registry.save_group({'name': '测试组'})['id']
            machine = registry.save_agent({'ip': '192.0.2.2', 'port': 4500, 'groupId': group,
                                           'capabilities': ['supervisor', 'executor']})
            self.assertTrue(registry.configs()[machine['agentId']].allow_write)
            self.assertTrue(registry.configs()[machine['agentId']].allow_full_access)

    def test_removed_list_arguments_are_rejected(self):
        import io
        from contextlib import redirect_stderr
        from workflow_gateway import build_argument_parser as gateway_parser
        from workflow_sidecar import build_argument_parser as sidecar_parser
        self.write({})
        for parser in (gateway_parser(), sidecar_parser()):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                parser.parse_args(['--agents', 'not-used.json'])
            self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
