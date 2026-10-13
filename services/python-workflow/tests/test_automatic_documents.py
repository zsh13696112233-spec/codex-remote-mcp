import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workflow_documents import collect_many, ERROR, validate_many
from workflow_outcomes import validate, SCHEMA_V3
from workflow_store import WorkflowStore
from workflow_gateway import _sidecar_job_snapshot
from tests.registry_fixtures import seed_agents
from tests.test_workflow_store import serial_workflow
from tests.test_workflow_outcomes import result
from tests.test_workflow_documents import DocumentCollectionTests


def outcome(documents=(), status='success'):
    value = result(status)
    del value['document']
    value['documents'] = list(documents)
    return value


def captured(name='方案.md', content='# 初版', key='plan'):
    return {'name': name, 'content': content, 'format': 'markdown',
            'sourceKey': hashlib.sha256(key.encode()).hexdigest()}


class AutomaticDocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkflowStore(Path(self.temp.name) / 'state.db')
        seed_agents(self.store, {'local': {'capabilities': ['supervisor', 'executor']},
                                 'other': {'capabilities': ['executor']}})
        spec = serial_workflow()
        spec['resultProtocolVersion'] = 3
        spec['nodes'][2]['agentId'] = 'other'
        # 数组顺序与依赖顺序不同，不影响最后版本选择。
        spec['nodes'] = [spec['nodes'][2], spec['nodes'][1], spec['nodes'][0]]
        self.store.create_workflow(spec)

    def sync(self, node, items, status='success'):
        self.store.prepare_node_dispatch('serial-demo', node)
        snapshot = _sidecar_job_snapshot({'status': 'completed', 'job_id': 'job-' + node,
                    'businessResult': outcome([{'name': item['name'], 'path': item['sourceKey'] + '.md'} for item in items], status=status), 'documents': items})
        self.store.sync_node_job('serial-demo', node, snapshot)
        return snapshot

    def test_all_documents_latest_same_source_and_distinct_machines(self):
        items = [captured(name=f'{i}.md', key=str(i)) for i in range(7)]
        first = self.sync('a', items)
        self.assertEqual(len(self.store.get_workflow('serial-demo')['documents']), 7)
        self.store.sync_node_job('serial-demo', 'a', first)
        self.assertEqual(self.store.documents('serial-demo', 'a')[0]['revision'], 1)
        self.sync('b', [captured(name='更新.md', content='# 新版', key='0')])
        summary = self.store.get_workflow('serial-demo')['documents']
        self.assertEqual(len(summary), 7)
        self.assertEqual(summary[-1]['nodeId'], 'b')
        self.assertEqual(summary[-1]['stepNumber'], 2)
        self.assertEqual(self.store.documents('serial-demo', document_id=summary[-1]['id'])['stepNumber'], 2)
        self.assertEqual(self.store.documents('serial-demo', document_id=summary[-1]['id'])['content'], '# 新版')
        self.sync('c', [captured(name='更新.md', key='0')])
        self.assertEqual(len(self.store.get_workflow('serial-demo')['documents']), 8)
        public = json.dumps(self.store.get_workflow('serial-demo')['documents'])
        self.assertNotIn('sourceKey', public)
        self.assertNotIn('content', public)

    def test_latest_failure_retains_previous_body_and_terminal_rejects_late_result(self):
        self.sync('a', [captured()])
        failed = {'name': '方案.md', 'sourceKey': captured()['sourceKey'], 'error': ERROR}
        self.sync('b', [failed], status='blocked')
        summary = self.store.get_workflow('serial-demo')['documents']
        self.assertEqual(len(summary), 1)
        self.assertTrue(summary[0]['available'])
        self.assertEqual(summary[0]['error'], ERROR)
        self.assertEqual(summary[0]['nodeId'], 'b')
        self.assertEqual(self.store.documents('serial-demo', document_id=summary[0]['id'])['content'], '# 初版')
        self.store.finish_business_termination('serial-demo')
        self.store.sync_node_job('serial-demo', 'b', {'status': 'completed', 'documents': [captured(content='迟到')], 'businessResult': outcome()})
        self.assertEqual(self.store.get_workflow('serial-demo')['documents'], summary)

    def test_empty_later_step_and_failed_workflow_keep_saved_documents(self):
        self.sync('a', [captured()])
        self.sync('b', [])
        self.store.finish_workflow('serial-demo', supervisor_status='failed', response=None, error='执行失败')
        summary = self.store.get_workflow('serial-demo')['documents']
        self.assertEqual(len(summary), 1)
        reloaded = WorkflowStore(self.store.path)
        self.assertEqual(reloaded.documents('serial-demo', document_id=summary[0]['id'])['content'], '# 初版')

    def test_cancel_keeps_completed_documents_and_drops_late_result(self):
        self.sync('a', [captured()])
        self.store.cancel_workflow('serial-demo')
        before = self.store.get_workflow('serial-demo')['documents']
        self.store.sync_node_job('serial-demo', 'b', {'status': 'completed', 'documents': [captured(content='迟到')], 'businessResult': outcome()})
        self.assertEqual(self.store.get_workflow('serial-demo')['documents'], before)

    def test_manifest_cannot_silently_drop_all_capture_results(self):
        self.store.prepare_node_dispatch('serial-demo', 'a')
        with self.assertRaisesRegex(ValueError, '采集结果'):
            self.store.sync_node_job('serial-demo', 'a', {'status': 'completed', 'businessResult': outcome([{'name': 'a.md', 'path': 'a.md'}])})
        self.assertNotEqual(self.store.get_node('serial-demo', 'a')['status'], 'completed')

    def test_first_capture_failure_has_no_linkable_body(self):
        self.sync('a', [{'name': '缺失.md', 'sourceKey': captured()['sourceKey'], 'error': ERROR}])
        self.assertFalse(self.store.get_workflow('serial-demo')['documents'][0]['available'])

    def test_protocol_and_internal_boundary(self):
        descriptors = [{'name': f'{i}.txt', 'path': f'{i}.txt'} for i in range(8)]
        self.assertEqual(validate(outcome(descriptors), 3)['documents'], descriptors)
        for version in (1, 2):
            with self.assertRaises(ValueError):
                validate(outcome(), version)
        for items in ({}, [captured(), captured()], [{**captured(), 'sourceKey': '/private/file'}],
                      [{**captured(), 'content': 'x' * (512 * 1024 + 1)}]):
            with self.assertRaises(ValueError):
                validate_many(items)
        spec = serial_workflow()
        spec.update(workflowId='semi', resultProtocolVersion=3, advanceMode='semi_automatic')
        with self.assertRaises(ValueError):
            self.store.create_workflow(spec)


class AutomaticDocumentCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_five_partial_failure_and_no_directory_scan(self):
        client = DocumentCollectionTests().client()
        docs = [{'name': f'{i}.md', 'path': f'{i}.md'} for i in range(7)]
        docs += [{'name': '坏路径.md', 'path': '../bad.md'}, {'name': '旧格式.markdown', 'path': 'old.markdown'}]
        items = await collect_many(client, '/work', docs)
        self.assertEqual(len(items), 9)
        self.assertTrue(all('content' in item for item in items[:7]))
        self.assertTrue(all(item['error'] == ERROR for item in items[7:]))
        self.assertEqual(items[-2]['name'], '坏路径.md')
        self.assertTrue(all(call.args[0] in ('fs/getMetadata', 'fs/readFile') for call in client.request.call_args_list))

    async def test_normalized_paths_deduplicate(self):
        collector = AsyncMock(return_value={'name': '方案', 'content': '# text', 'format': 'markdown'})
        with patch('workflow_documents.collect', collector):
            docs = await collect_many(None, 'C:\\work', [{'name': '初名', 'path': 'plan.md'},
                {'name': '后名', 'path': 'C:\\WORK\\PLAN.md'}])
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]['name'], '后名')

    async def test_real_result_parser_keeps_private_snapshot_out_of_job_response(self):
        from codex_orchestrator_mcp import Orchestrator
        from tests.mock_app_server import MockAppServer
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / 'state.db')
            docs = [{'name': '方案.md', 'path': 'plan.md'}]
            async with MockAppServer(structured_reply=json.dumps(outcome(docs))) as server:
                registry = seed_agents(store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor']}})
                orchestrator = Orchestrator()
                orchestrator.agent_provider = registry.configs
                with patch('workflow_documents.collect_many', AsyncMock(return_value=[captured()])) as collect:
                    job = await orchestrator.dispatch(agent_id='local', prompt='执行', output_schema=SCHEMA_V3,
                        timeout_sec=10, thread_id=None, cwd=None, write=False, model=None)
                    await job.completed.wait()
                self.assertEqual(job.status, 'completed', job.error)
                self.assertEqual(job.business_result['documents'], docs)
                self.assertEqual(job.document_results, [captured()])
                self.assertNotIn('documents', job.snapshot())
                collect.assert_awaited_once()
                spec = serial_workflow()
                spec['resultProtocolVersion'] = 3
                spec['nodes'] = spec['nodes'][:1]
                store.create_workflow(spec)
                store.prepare_node_dispatch('serial-demo', 'a')
                from codex_orchestrator_mcp import _sync_workflow_job
                with patch('codex_orchestrator_mcp.get_workflow_store', return_value=store):
                    public = _sync_workflow_job('serial-demo', 'a', job)
                self.assertNotIn('documents', public)
                self.assertNotIn('businessResult', public)
                self.assertEqual(len(store.get_workflow('serial-demo')['documents']), 1)
                self.assertEqual(store.get_workflow('serial-demo')['nodes'][0]['status'], 'completed')
