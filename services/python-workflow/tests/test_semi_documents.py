import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_orchestrator_mcp import Orchestrator
from workflow_store import WorkflowStore, utc_now
from workflow_documents import collect_many, save_many, ERROR
from workflow_discussion import validate as validate_discussion
from workflow_outcomes import schema, validate, instruction
from workflow_gateway import _sidecar_job_snapshot
from tests.registry_fixtures import seed_agents, fixture_gateway
from tests.test_workflow_store import serial_workflow
from tests.test_automatic_documents import captured, outcome
from tests.mock_app_server import MockAppServer


def manifest(primary=True, path='plan.md'):
    return {'name': '正式方案' if primary else '交接总结', 'path': path, 'primary': primary}


def snapshot(primary=True, content='# 初稿', key='plan'):
    return {**captured(name='正式方案' if primary else '交接总结', content=content, key=key), 'primary': primary}


class SemiDocumentsTests(unittest.IsolatedAsyncioTestCase):
    version = 4

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkflowStore(Path(self.temp.name) / 'state.db')
        seed_agents(self.store, {'local': {'capabilities': ['supervisor', 'executor'], 'allow_write': True}})
        spec = serial_workflow()
        spec.update(advanceMode='semi_automatic', resultProtocolVersion=self.version, waitPolicy={'version': 2})
        self.store.create_workflow(spec)

    def complete_first(self, status='success'):
        self.store.prepare_node_dispatch('serial-demo', 'a')
        result = outcome([manifest(False, 'summary.md'), manifest()], status)
        snap = _sidecar_job_snapshot({'status': 'completed', 'job_id': 'first',
            'thread_id': 'original-thread', 'turn_id': 'original-turn', 'cwd': '/work',
            'businessResult': result, 'documents': [snapshot(False, '总结正文', 'summary'), snapshot()]})
        self.store.sync_node_job('serial-demo', 'a', snap)
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='running' WHERE workflow_id='serial-demo'")
        return snap

    def test_primary_first_stable_link_failure_and_partial_update(self):
        original = self.complete_first()
        docs = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        self.assertEqual([d['primary'] for d in docs], [True, False])
        self.assertEqual(docs[0]['name'], '正式方案')
        self.assertNotIn('source_key', json.dumps(docs))
        self.assertNotIn('sourceKey', json.dumps(docs))
        self.store.sync_node_job('serial-demo', 'a', original)
        with self.store._connect() as db:
            # 改名/移动仍沿用主文档链接；未交付的总结不更新。
            save_many(db, 'serial-demo', 'a', 'edit1', [snapshot(content='# 修订', key='new-path')], utc_now())
            save_many(db, 'serial-demo', 'a', 'edit1', [snapshot(content='重复')], utc_now())
            failed = {'name': '正式方案', 'primary': True, 'sourceKey': snapshot()['sourceKey'], 'error': ERROR}
            save_many(db, 'serial-demo', 'a', 'edit2', [failed], utc_now())
        current = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        self.assertEqual(current[0]['id'], docs[0]['id'])
        self.assertEqual(current[0]['revision'], 3)
        self.assertEqual(current[1], docs[1])
        self.assertEqual(self.store.documents('serial-demo', document_id=docs[0]['id'])['content'], '# 修订')
        self.assertEqual(current[0]['error'], ERROR)
        self.assertEqual(WorkflowStore(self.store.path).get_workflow('serial-demo')['nodes'][0]['documents'], current)

    def test_blocked_documents_and_capture_scope(self):
        self.complete_first('blocked')
        view = self.store.get_workflow('serial-demo')
        self.assertEqual(len(view['nodes'][0]['documents']), 2)
        self.assertEqual(view['pendingAdvance']['kind'], 'blocked')
        with self.store._connect() as db:
            with self.assertRaises(ValueError):
                save_many(db, 'serial-demo', 'b', 'bad', [snapshot()], utc_now())

    def test_manifest_validation_and_protocol_separation(self):
        self.assertEqual(validate(outcome([manifest()]), 4)['documents'][0]['primary'], True)
        for docs in ([manifest(), manifest(path='other.md')], [{**manifest(), 'primary': 'true'}], [manifest(), manifest(False, 'plan.md')]):
            with self.assertRaises(ValueError):
                validate(outcome(docs), 4)
        with self.assertRaises(ValueError):
            validate(outcome([manifest()]), 3)
        with self.assertRaises(RuntimeError):
            validate_discussion(json.dumps({'text': '答复', 'summary': None, 'documents': [manifest()]}), True, multi=True)
        self.assertIn('正式方案必须', instruction('w', 'n', True, 4))
        self.assertIn('documents 必须为空数组', instruction('w', 'n', False, 4))
        self.store.prepare_node_dispatch('serial-demo', 'a')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.store.sync_node_job('serial-demo', 'a', {'status': 'completed', 'businessResult': outcome([manifest(), manifest(False, 'summary.md')]), 'documents': [snapshot(False)]})

    async def test_collection_preserves_roles_and_rejects_normalized_duplicates(self):
        with patch('workflow_documents.collect', AsyncMock(return_value={'name': '文档', 'content': '正文', 'format': 'markdown'})) as collector:
            result = await collect_many(None, 'C:\\work', [manifest(path='plan.markdown')], semi=True)
            self.assertTrue(result[0]['primary'])
            self.assertNotIn('primary', collector.call_args.args[2])
            with self.assertRaisesRegex(ValueError, '规范化路径重复'):
                await collect_many(None, 'C:\\work', [manifest(), manifest(False, 'C:\\WORK\\PLAN.md')], semi=True)

    async def test_discussion_changes_only_plan_and_question_does_not_capture(self):
        self.complete_first()
        before = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        for text, reply in [
            ('修改方案，只保留登录', {'text': '已修改方案', 'summary': '新方案只保留登录', 'documents': [manifest()]}),
            ('解释一下方案', {'text': '方案说明', 'summary': None, 'documents': []}),
        ]:
            async with MockAppServer(structured_reply=json.dumps(reply)) as server:
                seed_agents(self.store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor'], 'allow_write': True}})
                gateway = fixture_gateway(self.store, Orchestrator())
                try:
                    self.store.accept_chat_message('serial-demo', str(uuid.uuid4()), text)
                    message = self.store.claim_next_chat_message('serial-demo')
                    with patch('workflow_documents.collect_many', AsyncMock(return_value=[snapshot(content='只保留登录')])) as collect:
                        await gateway._process_chat_message('serial-demo', message)
                    if reply['summary']:
                        collect.assert_awaited_once()
                    else:
                        collect.assert_not_awaited()
                    turns = [r for r in server.requests if r['method'] == 'turn/start']
                    self.assertIn('不改写交接总结文件', json.dumps(turns, ensure_ascii=False))
                finally:
                    await gateway.event_batcher.close()
        after = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[0]['revision'], 2)
        self.assertEqual(self.store.get_node('serial-demo', 'a')['response'], '新方案只保留登录')
        self.assertEqual(self.store.documents('serial-demo', document_id=after[0]['id'])['content'], '只保留登录')

    def test_poll_during_capture_does_not_complete_step_early(self):
        from codex_orchestrator_mcp import Job, _sync_workflow_job
        self.store.prepare_node_dispatch('serial-demo', 'a')
        job = Job(job_id='capturing', agent_id='local', prompt='执行', requested_thread_id=None,
                  cwd='/work', write=False, permission_profile=None, model=None, timeout_sec=10,
                  output_schema=schema(4), status='completed', response='尚未采集')
        with patch('codex_orchestrator_mcp.get_workflow_store', return_value=self.store):
            value = _sync_workflow_job('serial-demo', 'a', job)
        self.assertEqual(value['status'], 'running')
        self.assertEqual(self.store.get_node('serial-demo', 'a')['status'], 'running')

    async def test_retry_captures_new_plan_and_keeps_other_documents(self):
        from workflow_wait_service import WaitService
        self.complete_first('blocked')
        old = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        gate = self.store.get_workflow('serial-demo')['pendingAdvance']
        self.store.claim_policy_wait('serial-demo', gate['gateId'], 'retry')
        async with MockAppServer(structured_reply=json.dumps(outcome([manifest()])), resume_thread_id='original-thread') as server:
            seed_agents(self.store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor'], 'allow_write': True}})
            gateway = fixture_gateway(self.store, Orchestrator())
            gateway._schedule_pending = AsyncMock()
            try:
                target = self.store.prepare_wait_execution('serial-demo', gate['gateId'])
                with patch('workflow_documents.collect_many', AsyncMock(return_value=[snapshot(content='重试方案')])):
                    await WaitService(gateway).execute('serial-demo', gate['gateId'], target)
                current = self.store.get_workflow('serial-demo')
                self.assertEqual(current['pendingAdvance']['kind'], 'advance')
                docs = current['nodes'][0]['documents']
                self.assertEqual(docs[1], old[1])
                self.assertEqual(docs[0]['revision'], 2)
            finally:
                await gateway.event_batcher.close()

    async def test_runtime_v4_captures_list_without_legacy_single_document(self):
        async with MockAppServer(structured_reply=json.dumps(outcome([manifest()]))) as server:
            registry = seed_agents(self.store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor']}})
            orchestrator = Orchestrator()
            orchestrator.agent_provider = registry.configs
            with patch('workflow_documents.collect_many', AsyncMock(return_value=[snapshot()])) as collector:
                job = await orchestrator.dispatch(agent_id='local', prompt='执行', output_schema=schema(4),
                    timeout_sec=10, thread_id=None, cwd=None, write=False, model=None)
                job.capture_document = True
                await job.completed.wait()
            self.assertEqual(job.status, 'completed', job.error)
            self.assertEqual(job.document_results, [snapshot()])
            self.assertIsNone(job.document_result)
            self.assertTrue(collector.call_args.kwargs['semi'])


class EveryStepDocumentsTests(SemiDocumentsTests):
    version = 5

    def test_blocked_documents_and_capture_scope(self):
        self.complete_first('blocked')
        view = self.store.get_workflow('serial-demo')
        self.assertEqual(len(view['nodes'][0]['documents']), 2)
        self.assertEqual(view['pendingAdvance']['kind'], 'blocked')
        self.assertTrue(self.store.document_enabled('serial-demo', 'b'))

    def test_every_step_and_final_confirmation(self):
        self.complete_first()
        for node_id in ('b', 'c'):
            gate = self.store.get_workflow('serial-demo')['pendingAdvance']
            self.store.confirm_advance('serial-demo', gate['gateId'])
            prepared = self.store.prepare_node_dispatch('serial-demo', node_id)
            self.assertEqual(prepared['resultProtocolVersion'], 5)
            self.assertTrue(prepared['captureDocument'])
            self.store.sync_node_job('serial-demo', node_id, _sidecar_job_snapshot({
                'status': 'completed', 'job_id': node_id,
                'thread_id': 'original-thread', 'turn_id': 'original-turn', 'cwd': '/work',
                'businessResult': outcome([manifest(False, 'summary.md')]),
                'documents': [snapshot(False, node_id, 'summary')]}))
        view = self.store.get_workflow('serial-demo')
        self.assertEqual([len(n['documents']) for n in view['nodes']], [2, 1, 1])
        self.assertEqual(view['pendingAdvance']['completedNodeId'], 'c')
        self.assertEqual(self.store.documents('serial-demo', document_id=view['nodes'][1]['documents'][0]['id'])['content'], 'b')
        self.assertEqual(self.store.documents('serial-demo', document_id=view['nodes'][2]['documents'][0]['id'])['content'], 'c')

    def test_primary_failure_never_uses_another_step(self):
        self.complete_first()
        failed = {'name': '正式方案', 'primary': True, 'sourceKey': snapshot()['sourceKey'], 'error': ERROR}
        with self.store._connect() as db:
            save_many(db, 'serial-demo', 'b', 'failed', [failed], utc_now())
        doc = self.store.documents('serial-demo', 'b')[0]
        self.assertFalse(doc['available'])
        with self.store._connect() as db:
            save_many(db, 'serial-demo', 'b', 'success', [snapshot(content='第二步正文')], utc_now())
            save_many(db, 'serial-demo', 'b', 'failed-again', [failed], utc_now())
        doc = self.store.documents('serial-demo', 'b')[0]
        self.assertEqual(self.store.documents('serial-demo', document_id=doc['id'])['content'], '第二步正文')
        self.assertEqual(doc['error'], ERROR)

    def test_single_step_and_empty_manifest(self):
        for has_documents in (False, True):
            spec = serial_workflow()
            spec.update(workflowId=str(uuid.uuid4()), advanceMode='semi_automatic', resultProtocolVersion=5, waitPolicy={'version': 2})
            spec['nodes'] = spec['nodes'][:1]
            wid = spec['workflowId']
            self.store.create_workflow(spec)
            self.assertTrue(self.store.prepare_node_dispatch(wid, 'a')['captureDocument'])
            self.store.sync_node_job(wid, 'a', {'status': 'completed', 'job_id': wid,
                'businessResult': outcome([manifest()] if has_documents else []),
                'documents': [snapshot()] if has_documents else []})
            self.assertEqual(len(self.store.get_workflow(wid)['nodes'][0]['documents']), int(has_documents))
            self.store.cancel_workflow(wid)

    def test_v5_requires_semi_automatic_and_uses_multi_document_contract(self):
        spec = serial_workflow()
        spec.update(workflowId='invalid-auto', resultProtocolVersion=5)
        with self.assertRaises(ValueError):
            self.store.create_workflow(spec)
        self.assertEqual(validate(outcome([manifest(False)]), 5)['documents'], [manifest(False)])
        self.assertIn('独立总结', instruction('w', 'b', True, 5))
        self.assertNotIn('必须为空数组', instruction('w', 'b', True, 5))

    async def test_last_step_discussion_updates_its_documents_only(self):
        self.test_every_step_and_final_confirmation()
        before = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        reply = {'text': '已修改总结', 'summary': '末步新版总结', 'documents': [manifest(False, 'summary.md')]}
        async with MockAppServer(structured_reply=json.dumps(reply)) as server:
            seed_agents(self.store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor'], 'allow_write': True}})
            gateway = fixture_gateway(self.store, Orchestrator())
            try:
                self.store.accept_chat_message('serial-demo', str(uuid.uuid4()), '修改最后一步总结')
                message = self.store.claim_next_chat_message('serial-demo')
                with patch('workflow_documents.collect_many', AsyncMock(return_value=[snapshot(False, '末步修订正文', 'summary')])) as collector:
                    await gateway._process_chat_message('serial-demo', message)
                collector.assert_awaited_once()
                view = self.store.get_workflow('serial-demo')
                self.assertEqual(view['nodes'][0]['documents'], before)
                doc = view['nodes'][2]['documents'][0]
                self.assertEqual(self.store.documents('serial-demo', document_id=doc['id'])['content'], '末步修订正文')
            finally:
                await gateway.event_batcher.close()

    async def test_executor_collects_v5_documents(self):
        async with MockAppServer(structured_reply=json.dumps(outcome([manifest(False)]))) as server:
            registry = seed_agents(self.store, {'local': {'url': server.url, 'cwd': '/work', 'capabilities': ['supervisor', 'executor']}})
            orchestrator = Orchestrator()
            orchestrator.agent_provider = registry.configs
            with patch('workflow_documents.collect_many', AsyncMock(return_value=[snapshot(False)])) as collector:
                job = await orchestrator.dispatch(agent_id='local', prompt=instruction('w', 'b', True, 5),
                    output_schema=schema(5), timeout_sec=10, thread_id=None, cwd=None, write=False, model=None)
                job.capture_document = True
                job.result_protocol_version = 5
                await job.completed.wait()
            self.assertEqual(job.status, 'completed', job.error)
            self.assertEqual(job.document_results, [snapshot(False)])
            collector.assert_awaited_once()
            self.assertTrue(collector.call_args.kwargs['semi'])

    def test_no_task_keeps_delivered_documents(self):
        self.complete_first('no_task')
        view = self.store.get_workflow('serial-demo')
        self.assertEqual(len(view['nodes'][0]['documents']), 2)
        self.assertTrue(all(doc['available'] for doc in view['nodes'][0]['documents']))
