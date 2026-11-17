import json
import unittest
import uuid
from tests.test_workflow_documents import DocumentStoreTests
from workflow_document_chat import bind, accept, read
from workflow_resources import release_if_stopped


class DocumentChatTests(unittest.TestCase):
    setUp = DocumentStoreTests.setUp
    sync = DocumentStoreTests.sync

    def ready(self):
        self.sync(self.item)
        doc=self.store.documents('serial-demo','a')[0]['id']
        with self.store._connect() as db:
            bind(db,'serial-demo','a',None,{'cwd':'/work','thread_id':'original','model':'model'}, {'document':{'name':'方案.md','path':'plan.md'}})
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='serial-demo'")
            db.execute("UPDATE workflow_execution_guards SET stopped=1")
            release_if_stopped(db,'serial-demo')
        return doc

    def test_original_binding_and_idempotent_message(self):
        doc=self.ready()
        body={'messageId':str(uuid.uuid4()),'text':'修改后请上传'}
        message,target=accept(self.store,'serial-demo',doc,body)
        self.assertEqual(target['threadId'],'original')
        self.assertEqual(target['path'],'/work/plan.md')
        self.assertIsNone(accept(self.store,'serial-demo',doc,body)[1])
        with self.assertRaises(ValueError):
            accept(self.store,'serial-demo',doc,{**body,'text':'不同内容'})
        with self.assertRaises(RuntimeError):
            accept(self.store,'serial-demo',doc,{**body,'messageId':str(uuid.uuid4())})
        self.assertTrue(read(self.store,'serial-demo',doc)['busy'])
        self.assertEqual(self.store.unresolved_resource_executions(),[])
        with self.store._connect() as db:
            self.assertFalse(release_if_stopped(db,'serial-demo'))
        self.assertEqual(self.store.get_workflow('serial-demo')['status'],'completed')

    def test_old_document_stays_read_only(self):
        doc=self.ready()
        with self.store._connect() as db:
            db.execute('DELETE FROM document_chat_origins')
        self.sync(self.item,'second')
        self.assertFalse(read(self.store,'serial-demo',doc)['available'])
        with self.assertRaises(ValueError):
            accept(self.store,'serial-demo',doc,{'messageId':str(uuid.uuid4()),'text':'修改'})

    def test_active_workflow_and_public_data(self):
        doc=self.ready()
        with self.store._connect() as db:
            db.execute("UPDATE workflows SET status='running'")
        public=read(self.store,'serial-demo',doc)
        self.assertFalse(public['available'])
        self.assertNotIn('original',json.dumps(public))
        self.assertNotIn('/work',json.dumps(public))
        with self.assertRaises(ValueError):
            accept(self.store,'serial-demo',doc,{'messageId':str(uuid.uuid4()),'text':'修改'})

    def test_execution_resumes_original_thread_and_refreshes_document(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from workflow_document_chat import run
        doc=self.ready()
        body={'messageId':str(uuid.uuid4()),'text':'修改后重新上传'}
        message,target=accept(self.store,'serial-demo',doc,body)
        async def execute():
            done=asyncio.Event();done.set()
            snapshot={'status':'completed','execution_stopped':True,'job_id':'revision-job','thread_id':'original','turn_id':'revision-turn'}
            job=SimpleNamespace(status='completed',completed=done,job_id='revision-job',turn_id='revision-turn',response=json.dumps({'text':'已完成修改','document':{'name':'新方案.md','path':'updated.md'}}),snapshot=lambda:snapshot)
            client=AsyncMock()
            orchestrator=SimpleNamespace(get_agent=lambda _:SimpleNamespace(cwd='/work',allow_cwd_override=False,url='fixture',token_file=None),dispatch=AsyncMock(return_value=job),_client_factory=lambda *a,**k:client,_resolve_agent_token=lambda _:None)
            gateway=SimpleNamespace(store=self.store,assistant_orchestrator=orchestrator,_schedule_pending=AsyncMock())
            with patch('workflow_documents.collect',AsyncMock(return_value={'name':'新方案.md','format':'markdown','content':'# 修订'})):
                await run(gateway,'serial-demo',doc,message,body['text'],target)
            kwargs=orchestrator.dispatch.call_args.kwargs
            self.assertEqual(kwargs['thread_id'],'original')
            self.assertEqual(kwargs['agent_id'],target['agentId'])
            self.assertIn(body['text'],kwargs['prompt'])
            client.__aexit__.assert_awaited_once()
        asyncio.run(execute())
        self.assertEqual(self.store.documents('serial-demo',document_id=doc)['content'],'# 修订')
        view=read(self.store,'serial-demo',doc)
        self.assertFalse(view['busy'])
        self.assertEqual(view['messages'][0]['reply'],'已完成修改')
        self.assertEqual(self.store.machine_claims(),{})

    def test_attachment_identity_scope_and_versions(self):
        from workflow_document_chat import snapshot_version
        doc = self.ready()
        with self.store._connect() as db:
            self.assertEqual(snapshot_version(db, 'serial-demo', doc), 1)
            self.assertIsNone(snapshot_version(db, 'serial-demo', doc))
            db.execute('INSERT INTO document_chat_files VALUES(?,?,?,?,?,?,?)', ('serial-demo', doc, 'file-a', '说明.txt', 'text/plain', '中文资料'.encode(), 'now'))
        body = {'messageId': str(uuid.uuid4()), 'text': '', 'attachmentIds': ['file-a']}
        _, target = accept(self.store, 'serial-demo', doc, body)
        self.assertEqual(target['attachmentIds'], ['file-a'])
        self.assertIsNone(accept(self.store, 'serial-demo', doc, body)[1])
        with self.assertRaises(ValueError):
            accept(self.store, 'serial-demo', doc, {**body, 'attachmentIds': []})
        with self.store._connect() as db:
            db.execute("UPDATE workflow_documents SET content='新版' WHERE workflow_id='serial-demo'")
            self.assertEqual(snapshot_version(db, 'serial-demo', doc), 2)
            original = db.execute('SELECT content FROM document_chat_versions WHERE version=1').fetchone()[0]
            self.assertNotEqual(original, '新版')
        self.assertEqual(read(self.store, 'serial-demo', doc)['messages'][0]['attachmentIds'], ['file-a'])

    def test_workspace_upload_read_delete_and_version(self):
        from types import SimpleNamespace
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from workflow_document_chat import workspace
        doc = self.ready()
        app = Starlette(routes=[Route('/{workflow_id}/{document_id}', workspace, methods=['GET','POST','DELETE'])])
        app.state.gateway = SimpleNamespace(store=self.store)
        client = TestClient(app)
        url = '/serial-demo/' + doc
        self.assertEqual(client.post(url).status_code, 200)
        uploaded = client.post(url+'?action=upload&name=notes.txt',content='资料'.encode())
        self.assertEqual(uploaded.status_code, 201)
        identifier = uploaded.json()['id']
        self.assertEqual(client.get(url+'?action=file&id='+identifier).text,'资料')
        self.assertEqual(client.get(url+'?action=version&version=1').status_code,200)
        self.assertEqual(client.post(url+'?action=upload&name=bad.pdf',content=b'bad').status_code,400)
        self.assertEqual(client.delete(url+'?action=file&id='+identifier).status_code,200)
        self.assertEqual(client.get(url+'?action=file&id='+identifier).status_code,400)
