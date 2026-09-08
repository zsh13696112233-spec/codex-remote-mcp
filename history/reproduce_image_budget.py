"""从仓库根运行：PYTHONPATH=services/python-workflow/src:services/python-workflow .venv/bin/python history/reproduce_image_budget.py"""
import asyncio
import tempfile
import uuid
from pathlib import Path
from tests.test_input_images import PNG, png_chunk
from tests.test_workflow_store import serial_workflow
from workflow_store import WorkflowStore
from codex_orchestrator_mcp import AgentConfig, Orchestrator

async def main():
    with tempfile.TemporaryDirectory() as directory:
        store = WorkflowStore(Path(directory) / "review.db")
        spec = serial_workflow()
        spec["taskDefinitionId"] = "review"
        store.create_workflow(spec)
        for number in range(2):
            content = PNG[:-12] + png_chunk(b"tEXt", b"Comment\0" + bytes([65 + number]) * 11_000_000) + PNG[-12:]
            image_id = store.save_input_image("serial-demo", content)["imageId"]
            message_id = str(uuid.uuid4())
            store.accept_chat_message("serial-demo", message_id, "按新图修改", [image_id], "review:alice")
            action = store.propose_control("serial-demo", "restart_from", "a", message_id)
            confirmation = str(uuid.uuid4())
            store.accept_chat_message("serial-demo", confirmation, "确认执行", [], "review:alice", action["actionId"])
            store.confirm_control("serial-demo", action["actionId"], confirmation)
            store.start_control_execution(action["actionId"])
            store.restart_from_node("serial-demo", "a", action_id=action["actionId"])
            store.finish_control_execution(action["actionId"])
        images = store.node_input_images("serial-demo", "a")
        print("已接受两轮各 11 MB 图片返工；步骤累计字节：", sum(len(x["content"]) for x in images))
        print("已扣返工额度：", store.get_workflow("serial-demo")["retryPolicy"]["usedRetries"])
        orchestrator = Orchestrator(Path(directory) / "unused.json")
        agent = AgentConfig.from_dict("local", {"url": "ws://127.0.0.1:1", "cwd": directory, "artifact_root": directory, "allow_write": True})
        orchestrator.load_agents = lambda: {"local": agent}
        try:
            await orchestrator.dispatch(agent_id="local", prompt="修改", write=False, thread_id=None, cwd=None, model=None, timeout_sec=10, input_images=images)
        except ValueError as error:
            print("派发失败：", error)
            assert "20 MB" in str(error)
        else:
            raise AssertionError("未复现预期缺陷")

asyncio.run(main())
