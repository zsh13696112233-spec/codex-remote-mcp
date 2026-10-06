import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from workflow_documents import collect, path_for, ERROR, FILE_LIMIT, save, validate_result, failure
from workflow_store import utc_now
from tests.registry_fixtures import FixtureWorkflowStore
from tests.test_workflow_store import serial_workflow


class DocumentCollectionTests(unittest.IsolatedAsyncioTestCase):
    def client(self, body=b'# hello', linked=False):
        async def request(method, params):
            if method == 'fs/readFile':
                return {'dataBase64': base64.b64encode(body).decode()}
            file = params['path'].endswith('.md')
            return {'isSymlink': linked, 'isFile': file, 'isDirectory': not file, 'size': len(body) if file else 0}
        client = AsyncMock()
        client.request.side_effect = request
        return client

    async def test_read_only_explicit_manifest_and_decode(self):
        client = self.client('# 方案'.encode())
        values = await collect(client, '/work', {'name':'方案.md','path':'方案.md'})
        self.assertEqual(values['content'], '# 方案')
        self.assertNotIn('path', values)
        self.assertTrue(all(c.args[0] in {'fs/getMetadata','fs/readFile'} for c in client.request.call_args_list))

    async def test_reject_links_large_files_encoding_and_escape(self):
        for client, path in [(self.client(linked=True),'plan.md'), (self.client(b'x'*(FILE_LIMIT+1)),'plan.md'),
                             (self.client(b'\xff'),'plan.md'), (self.client(),'../plan.md'),
                             (self.client(),'config/secret.md')]:
            with self.subTest(path=path):
                result = await collect(client,'/work',{'name':'方案','path':path})
                self.assertEqual(result['error'],ERROR)
                self.assertNotIn('content',result)

    def test_windows_paths_and_relative_boundaries(self):
        self.assertEqual(path_for('C:\\work', 'docs\\plan.md')[1], 'docs\\plan.md')
        self.assertEqual(path_for('C:\\work', 'C:\\WORK\\Docs\\PLAN.md')[1], 'docs\\plan.md')
        for path in ['D:\\work\\plan.md', 'C:\\elsewhere\\plan.md', 'C:plan.md', '../plan.md', '.env.txt', 'token.txt']:
            with self.assertRaises(ValueError, msg=path):
                path_for('C:\\work',path)


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'workflows.db'
        self.store = FixtureWorkflowStore(self.path)
        spec = serial_workflow()
        spec['advanceMode'] = 'semi_automatic'
        self.store.create_workflow(spec)
        self.store.prepare_node_dispatch('serial-demo','a')
        self.item = {'name':'方案.md','content':'# 初稿','format':'markdown'}

    def sync(self, items, source='source'):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            save(db,'serial-demo','a',source,items,utc_now())

    def test_eligibility_ignores_legacy_switch(self):
        self.assertTrue(self.store.document_enabled('serial-demo', 'a'))
        self.assertTrue(self.store.prepare_node_dispatch('serial-demo', 'a')['captureDocument'])
        self.assertFalse(self.store.document_enabled('serial-demo', 'b'))
        for mode, count in [('automatic', 3), ('semi_automatic', 1)]:
            spec = serial_workflow()
            spec['workflowId'] = mode + str(count)
            spec['advanceMode'] = mode
            spec['nodes'] = spec['nodes'][:count]
            spec['nodes'][0]['displayDocuments'] = True
            self.store.create_workflow(spec)
            self.assertFalse(self.store.document_enabled(spec['workflowId'], 'a'))
            self.assertFalse(self.store.prepare_node_dispatch(spec['workflowId'], 'a')['captureDocument'])
            with self.store._connect() as db, self.assertRaises(ValueError):
                save(db, spec['workflowId'], 'a', 'bad', self.item, utc_now())
        with self.store._connect() as db, self.assertRaises(ValueError):
            save(db, 'serial-demo', 'b', 'bad', self.item, utc_now())

    def test_document_query_rejects_invalid_ids_and_missing_step(self):
        for workflow_id, node_id in [('x' * 129, 'a'), ('serial-demo', 'missing'), ('serial-demo', 'a\n')]:
            with self.assertRaises(ValueError):
                self.store.documents(workflow_id, node_id)

    def test_stable_latest_link_idempotency_failure_and_rename(self):
        self.sync(self.item)
        first=self.store.documents('serial-demo','a')[0]
        self.sync(self.item)
        self.assertEqual(self.store.documents('serial-demo','a')[0]['revision'],1)
        self.sync({**self.item,'name':'新版.md','content':'# 新版'}, 'second')
        updated=self.store.documents('serial-demo',document_id=first['id'])
        self.assertEqual(updated['content'],'# 新版')
        self.assertEqual(updated['revision'],2)
        self.sync(failure(), 'failed')
        failed=self.store.documents('serial-demo',document_id=first['id'])
        self.assertEqual(failed['content'],'# 新版')
        self.assertEqual(failed['error'],ERROR)
        self.assertNotIn('source_key',failed)
        self.assertEqual(FixtureWorkflowStore(self.path).documents('serial-demo','a')[0]['id'],first['id'])
        self.assertEqual(failed['name'], '新版.md')
        with self.assertRaises(ValueError):
            self.store.documents('wrong-workflow',document_id=first['id'])

    def test_snapshot_and_terminal_replay(self):
        snapshot={'job_id':'job-a','status':'completed','response':'总结','document':self.item}
        self.store.sync_node_job('serial-demo','a',snapshot)
        result=self.store.get_workflow('serial-demo')['nodes'][0]
        self.assertEqual(result['response'],'总结')
        self.assertEqual(len(result['documents']),1)
        self.assertNotIn('content',result['documents'][0])
        self.store.sync_node_job('serial-demo','a',snapshot)
        self.assertEqual(self.store.documents('serial-demo','a')[0]['revision'],1)
        with self.assertRaises(RuntimeError):
            self.store.sync_node_job('serial-demo','a',{**snapshot,'job_id':'old-job'})

    def test_boundary_validation(self):
        for item in [{**self.item, 'content': 'x'*(FILE_LIMIT+1)}, [self.item],
                     {**self.item, 'error': 'raw exception'}, {**self.item, 'removed': True}]:
            with self.assertRaises(ValueError):
                validate_result(item)

    def test_missing_document_preserves_previous_version(self):
        self.sync(failure())
        first = self.store.documents('serial-demo', 'a')[0]
        self.assertFalse(first['available'])
        self.sync(self.item, 'success')
        self.sync(failure(), 'missing')
        latest = self.store.documents('serial-demo', document_id=first['id'])
        self.assertEqual(latest['content'], self.item['content'])
        self.assertEqual(latest['error'], ERROR)

    def test_legacy_links_remain_readable_without_multiple_display_entries(self):
        self.sync(self.item)
        first = self.store.documents('serial-demo', 'a')[0]
        with self.store._connect() as db:
            db.execute("UPDATE workflow_documents SET source_key='old-plan.md' WHERE document_id=?", (first['id'],))
            db.execute("""INSERT INTO workflow_documents
                SELECT workflow_id,node_id,'legacy-extra','extra.md','历史附件',content,format,checksum,
                       revision,updated_at,error,removed FROM workflow_documents WHERE document_id=?""", (first['id'],))
        self.sync({**self.item, 'name': '重命名.md', 'content': '新版'}, 'renamed')
        displayed = self.store.get_workflow('serial-demo')['nodes'][0]['documents']
        self.assertEqual([doc['id'] for doc in displayed], [first['id']])
        self.assertEqual(self.store.documents('serial-demo', document_id=first['id'])['content'], '新版')
        self.assertEqual(self.store.documents('serial-demo', document_id='legacy-extra')['content'], '# 初稿')



class DocumentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_worker_result_keeps_summary_and_private_document_body(self):
        import json
        from unittest.mock import patch
        from codex_orchestrator_mcp import Orchestrator
        from workflow_documents import SCHEMA
        from tests.mock_app_server import MockAppServer
        from tests.registry_fixtures import seed_agents
        with tempfile.TemporaryDirectory() as directory:
            store = FixtureWorkflowStore(Path(directory)/'state.db')
            async with MockAppServer(structured_reply=json.dumps({'summary':'交接总结','document':None})) as server:
                registry = seed_agents(store, {'local':{'url':server.url,'cwd':'/work','capabilities':['supervisor','executor']}})
                orchestrator = Orchestrator()
                orchestrator.agent_provider = registry.configs
                with patch('workflow_documents.collect', AsyncMock(return_value={'name':'方案','content':'私有正文','format':'markdown'})):
                    job = await orchestrator.dispatch(agent_id='local',prompt='生成方案',output_schema=SCHEMA,timeout_sec=10,thread_id=None,cwd=None,write=False,model=None)
                    await job.completed.wait()
                self.assertEqual(job.status,'completed',job.error)
                self.assertEqual(job.response,'交接总结')
                self.assertEqual(job.document_result['content'],'私有正文')
                self.assertNotIn('document',job.snapshot())

    async def test_gateway_document_reads_and_sidecar_field_validation(self):
        from starlette.testclient import TestClient
        from workflow_gateway import _sidecar_job_snapshot
        from workflow_gateway import create_app
        from contextlib import closing
        with tempfile.TemporaryDirectory() as directory:
            store=FixtureWorkflowStore(Path(directory)/'state.db')
            spec=serial_workflow();spec['advanceMode']='semi_automatic'
            store.create_workflow(spec);store.prepare_node_dispatch('serial-demo','a')
            snapshot={'status':'completed','response':'总结','document':{'name':'方案.md','content':'# 正文','format':'markdown'}}
            store.sync_node_job('serial-demo','a',_sidecar_job_snapshot(snapshot))
            doc=store.documents('serial-demo','a')[0]
            with closing(TestClient(create_app(db_path=Path(directory)/'state.db'))) as client:
                result=client.get(f'/workflows/serial-demo/documents/{doc["id"]}')
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json()['content'],'# 正文')
                self.assertEqual(result.headers['cache-control'],'no-store')
                self.assertEqual(client.get(f'/workflows/other/documents/{doc["id"]}').status_code,404)
            with self.assertRaises(ValueError):
                _sidecar_job_snapshot({**snapshot,'document':{'name':'x','content':'x'*(FILE_LIMIT+1),'format':'text'}})
