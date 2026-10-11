import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from reasoning_effort import ModelsUnavailable, read_models
from tests.registry_fixtures import FixtureWorkflowStore, fixture_gateway
from workflow_gateway import agent_models, create_workflow


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_paginates_filters_and_rejects_repeated_cursor(self):
        client = SimpleNamespace(request=AsyncMock(side_effect=[
            {"data": [{"model": "a", "url": "secret", "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "future"}]}], "nextCursor": "next"},
            {"data": [{"model": "b"}], "nextCursor": None}]))
        result = await read_models(client)
        self.assertEqual(result[0], {"model": "a", "supportedReasoningEfforts": ["high"], "defaultReasoningEffort": None})
        self.assertEqual(result[1]["model"], "b")
        self.assertEqual(client.request.call_args.args[1]["cursor"], "next")
        client.request = AsyncMock(return_value={"data": [], "nextCursor": "same"})
        with self.assertRaisesRegex(ValueError, "分页异常"):
            await read_models(client)

    async def test_http_response_and_unavailable_status(self):
        orchestrator = SimpleNamespace(agent_models=AsyncMock(return_value=[]))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway=SimpleNamespace(orchestrator=orchestrator))), path_params={"agent_id": "local"})
        self.assertEqual((await agent_models(request)).status_code, 200)
        orchestrator.agent_models.side_effect = RuntimeError("无法读取执行机支持的思考强度，请检查连接后重试。")
        self.assertEqual((await agent_models(request)).status_code, 503)

    async def test_submission_capability_failure_is_stable_http_error(self):
        request = SimpleNamespace(json=AsyncMock(return_value={}), app=SimpleNamespace(state=SimpleNamespace(
            gateway=SimpleNamespace(submit=AsyncMock(side_effect=ModelsUnavailable("暂不可用"))))))
        response = await create_workflow(request)
        self.assertEqual(response.status_code, 503)
        self.assertIn("暂不可用", response.body.decode())

    async def test_submission_validates_before_persisting_and_default_skips_query(self):
        orchestrator = SimpleNamespace(
            list_agents=lambda: [{"agent_id": "local", "enabled": True, "capabilities": ["supervisor", "executor"], "permission_profiles": ["read_only"]}],
            agent_models=AsyncMock(return_value=[{"model": "test", "supportedReasoningEfforts": ["high"]}]))
        with tempfile.TemporaryDirectory() as directory:
            store = FixtureWorkflowStore(Path(directory, "state.db"))
            gateway = fixture_gateway(store, orchestrator)
            gateway._schedule_pending = AsyncMock()
            spec = {"workflowId": "effort", "nodes": [{"id": "a", "prompt": "执行", "model": "test", "reasoningEffort": "ultra"}]}
            with self.assertRaisesRegex(ValueError, "不支持"):
                await gateway.submit(spec)
            with self.assertRaisesRegex(ValueError, "找不到工作流"):
                store.get_spec("effort")
            spec["nodes"][0]["reasoningEffort"] = "high"
            await gateway.submit(spec)
            self.assertEqual(store.get_spec("effort")["nodes"][0]["reasoningEffort"], "high")
            orchestrator.agent_models.reset_mock()
            spec["workflowId"] = "default"
            del spec["nodes"][0]["reasoningEffort"]
            await gateway.submit(spec)
            orchestrator.agent_models.assert_not_awaited()
