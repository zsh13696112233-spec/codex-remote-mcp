import asyncio
import base64
import tempfile
import unittest
import uuid
import struct
import zlib
from pathlib import Path

from codex_orchestrator_mcp import AgentConfig, Orchestrator
from workflow_gateway import WorkflowGateway, create_app
from workflow_input_images import image_media_type
from workflow_store import WorkflowStore
from starlette.testclient import TestClient
from tests.mock_app_server import MockAppServer
from tests.test_workflow_store import serial_workflow

def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


PNG = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + png_chunk(b"IEND", b""))


class InputImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = WorkflowStore(Path(self.directory.name, "workflow.db"))
        spec = serial_workflow()
        spec["taskDefinitionId"] = "task"
        self.store.create_workflow(spec)

    def upload(self):
        return self.store.save_input_image("serial-demo", PNG)["imageId"]

    def message(self, text, actor="app:alice", images=None, action=None):
        mid = str(uuid.uuid4())
        return self.store.accept_chat_message("serial-demo", mid, text, images or [], actor, action)

    def test_original_bytes_deduplicate_and_remain_workflow_scoped(self):
        image_id = self.upload()
        self.assertEqual(self.upload(), image_id)
        self.assertEqual(self.store.get_input_image("serial-demo", image_id)["content"], PNG)
        with self.assertRaises(LookupError):
            self.store.get_input_image("other", image_id)
        with self.assertRaises(ValueError):
            self.message("图片", images=["f" * 64])

    def test_corrupt_and_unsupported_images_fail(self):
        fake_jpeg = b"\xff\xd8\xff" + b"x" * 30 + b"\xff\xd9"
        fake_webp = b"RIFF" + struct.pack("<I", 24) + b"WEBPVP8 " + struct.pack("<I", 12) + b"x" * 12
        for content in (b"", b"GIF89a", PNG[:-1], PNG[:32] + b"x" + PNG[33:], fake_jpeg, fake_webp):
            with self.subTest(content=content[:8]), self.assertRaises(ValueError):
                image_media_type(content)

    def test_message_identity_includes_actor_images_and_action(self):
        image_id = self.upload()
        accepted = self.message("看图", images=[image_id])
        self.assertEqual(accepted["imageIds"], [image_id])
        with self.assertRaises(RuntimeError):
            self.store.accept_chat_message("serial-demo", accepted["messageId"], "看图", [], "app:alice")
        with self.assertRaises(RuntimeError):
            self.store.accept_chat_message("serial-demo", accepted["messageId"], "看图", [image_id], "app:bob")
        with self.assertRaises(ValueError):
            self.message("看图", images=[image_id] * 6)

    def test_confirmation_checks_exact_action_and_person(self):
        request = self.message("停止")
        proposal = self.store.propose_control("serial-demo", "stop", None, request["messageId"])
        pending = self.store.get_workflow("serial-demo")["pendingControl"]
        self.assertEqual(pending["actorId"], "app:alice")
        for actor, action in (("app:bob", proposal["actionId"]), ("app:alice", "old"), (None, None)):
            confirmation = self.message("确认执行", actor=actor, action=action)
            with self.assertRaises(ValueError):
                self.store.confirm_control("serial-demo", proposal["actionId"], confirmation["messageId"])
        confirmation = self.message("确认执行", action=proposal["actionId"])
        self.store.confirm_control("serial-demo", proposal["actionId"], confirmation["messageId"])
        with self.assertRaises(ValueError):
            self.store.confirm_control("serial-demo", proposal["actionId"], confirmation["messageId"])

    def test_images_only_reach_business_steps_after_confirmed_rework(self):
        image_id = self.upload()
        request = self.message("按图修改", images=[image_id])
        proposal = self.store.propose_control("serial-demo", "restart_from", "a", request["messageId"], "按参考图修改")
        self.assertEqual(self.store.node_input_images("serial-demo", "a"), [])
        confirmation = self.message("确认执行", action=proposal["actionId"])
        self.store.confirm_control("serial-demo", proposal["actionId"], confirmation["messageId"])
        self.store.start_control_execution(proposal["actionId"])
        self.store.restart_from_node("serial-demo", "a", action_id=proposal["actionId"], revision_instruction="按参考图修改", source_message_id=request["messageId"])
        for node in ("a", "b", "c"):
            self.assertEqual(self.store.node_input_images("serial-demo", node)[0]["content"], PNG)
        self.assertNotIn("imageIds", self.store.get_workflow_spec("serial-demo"))

    def test_cancelled_proposal_never_adds_step_images(self):
        request = self.message("按图修改", images=[self.upload()])
        proposal = self.store.propose_control("serial-demo", "restart_from", "a", request["messageId"])
        cancel = self.message("取消操作", action=proposal["actionId"])
        self.store.cancel_pending_control("serial-demo", cancel["messageId"])
        self.assertEqual(self.store.node_input_images("serial-demo", "a"), [])

    def test_upload_and_read_http_preserve_original_content(self):
        app = create_app(db_path=self.store.path, config_path=Path(self.directory.name, "unused.json"))
        client = TestClient(app)
        self.addCleanup(client.close)
        uploaded = client.post("/workflows/serial-demo/input-images", content=PNG)
        self.assertEqual(uploaded.status_code, 201)
        image_id = uploaded.json()["imageId"]
        response = client.get("/workflows/serial-demo/input-images/" + image_id)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(client.get("/workflows/other/input-images/" + image_id).status_code, 404)
        self.assertEqual(client.post("/workflows/serial-demo/input-images", content=b"bad").status_code, 400)


class ImageDispatchTests(unittest.IsolatedAsyncioTestCase):
    def orchestrator(self, agent):
        value = Orchestrator(Path("unused-test-config"))
        value.load_agents = lambda: {"remote": agent}
        return value

    async def test_remote_server_receives_original_bytes_before_local_image_turn(self):
        async with MockAppServer() as server:
            agent = AgentConfig.from_dict("remote", {"url": server.url, "cwd": "C:/tasks", "artifact_root": "C:/managed", "allow_write": True})
            orchestrator = self.orchestrator(agent)
            job = await orchestrator.dispatch(agent_id="remote", prompt="看图", write=False, thread_id=None, cwd=None, model=None,
                input_images=[{"mediaType": "image/png", "content": PNG}], timeout_sec=10)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "completed", job.error)
            turn = next(item for item in server.requests if item["method"] == "turn/start")
            image = turn["params"]["input"][1]
            self.assertEqual(image["type"], "localImage")
            self.assertEqual(server.file_writes[image["path"]], PNG)
            self.assertIn("C:/managed/", image["path"].replace("\\", "/"))
            self.assertNotIn("dataBase64", job.prompt)

    async def test_file_api_failure_never_starts_text_only_turn(self):
        async with MockAppServer() as server:
            server.reject_file_writes = True
            agent = AgentConfig.from_dict("remote", {"url": server.url, "cwd": "/tasks", "artifact_root": "/managed", "allow_write": True})
            orchestrator = self.orchestrator(agent)
            job = await orchestrator.dispatch(agent_id="remote", prompt="看图", write=False, thread_id=None, cwd=None, model=None,
                input_images=[{"mediaType": "image/png", "content": PNG}], timeout_sec=10)
            await asyncio.wait_for(job.completed.wait(), 5)
            self.assertEqual(job.status, "failed")
            self.assertFalse(any(item["method"] == "turn/start" for item in server.requests))

    async def test_read_only_executor_cannot_be_used_to_write_input_files(self):
        agent = AgentConfig.from_dict("remote", {"url": "ws://127.0.0.1:1", "cwd": "/tasks", "artifact_root": "/managed", "allow_write": False})
        with self.assertRaises(ValueError):
            await self.orchestrator(agent).dispatch(agent_id="remote", prompt="看图", write=False, thread_id=None, cwd=None, model=None,
                input_images=[{"mediaType": "image/png", "content": PNG}], timeout_sec=10)

    async def test_gateway_assistant_receives_uploaded_image(self):
        async with MockAppServer() as server:
            with tempfile.TemporaryDirectory() as directory:
                store = WorkflowStore(Path(directory, "test.db"))
                spec = serial_workflow()
                spec["supervisorAgentId"] = "remote"
                store.create_workflow(spec)
                image_id = store.save_input_image("serial-demo", PNG)["imageId"]
                message = store.accept_chat_message("serial-demo", str(uuid.uuid4()), "看图", [image_id])
                agent = AgentConfig.from_dict("remote", {"url": server.url, "cwd": "/tasks", "artifact_root": "/managed", "allow_write": True})
                orchestrator = self.orchestrator(agent)
                gateway = WorkflowGateway(store, orchestrator, orchestrator)
                answer = await gateway._run_assistant_turn("serial-demo", message["messageId"], store.get_workflow("serial-demo"), message)
                self.assertEqual(answer["kind"], "answer")
                self.assertEqual(list(server.file_writes.values()), [PNG])
