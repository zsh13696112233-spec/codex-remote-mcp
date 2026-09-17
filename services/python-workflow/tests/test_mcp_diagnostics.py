import json
import unittest

from mcp_diagnostics import diagnostic, tools_discovered


class DiagnosticTests(unittest.TestCase):
    def test_catalog_without_optional_runtime_status_is_compatible(self):
        for row in ({'tools': {'ping': {}}}, {'tools': {'ping': {}}, 'runtimeStatus': None}):
            self.assertTrue(tools_discovered(row))
            self.assertIn('工具发现成功', diagnostic('检测', row)['reason'])
        for row in ({'tools': {}}, {'tools': {'ping': {}}, 'toolsError': 'failed'},
                    {'tools': {'ping': {}}, 'runtimeStatus': 'failed'},
                    {'tools': {'ping': {}}, 'runtimeStatus': 'starting'},
                    {'tools': {'ping': {}}, 'runtimeStatus': 'unknown'}):
            self.assertFalse(tools_discovered(row))
    def test_no_raw_error_or_unknown_state_is_exposed(self):
        raw = 'initialize failed https://secret:password@example.invalid token=private'
        value = diagnostic('检测', {'runtimeStatus': raw, 'toolsError': raw, 'tools': {}})
        encoded = json.dumps(value)
        for secret in ('password', 'example.invalid', 'private', 'https://'):
            self.assertNotIn(secret, encoded)
        self.assertIn('握手', value['reason'])

    def test_absent_unknown_empty_and_connected_are_distinct(self):
        self.assertFalse(diagnostic('检测')['found'])
        value = diagnostic('检测', {'tools': {'ping': {}}})
        self.assertTrue(value['found'])
        self.assertEqual(value['toolCount'], 1)
        self.assertIn('未提供', value['connection'])
        self.assertIn('未返回', diagnostic('检测', {'runtimeStatus': 'connected', 'tools': {}})['reason'])
        self.assertIn('尚未执行', diagnostic('检测', {'runtimeStatus': 'connected', 'tools': {'ping': {}}})['reason'])

    def test_pending_and_transport_failure_are_not_server_absence(self):
        self.assertIsNone(diagnostic('连接', pending=True)['found'])
        result = diagnostic('连接', error=TimeoutError('secret'))
        self.assertIsNone(result['found'])
        self.assertIn('超时', result['reason'])

    def test_known_address_failure_gets_actionable_summary(self):
        value = diagnostic('检测', {'toolsError': 'Jira server address is required; configure base-url'})
        self.assertIn('缺少服务地址', value['reason'])


if __name__ == '__main__':
    unittest.main()
