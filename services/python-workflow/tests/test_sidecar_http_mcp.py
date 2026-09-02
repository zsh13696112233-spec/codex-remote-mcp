import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import codex_orchestrator_mcp as service
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tests.mock_app_server import MockAppServer
from workflow_gateway import create_app
from workflow_runtime_client import InternalApiClient


async def start_http_server(app, *, lifespan: str) -> tuple[str, uvicorn.Server, asyncio.Task]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan=lifespan,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            return f"http://127.0.0.1:{port}", server, task
        if task.done():
            await task
        await asyncio.sleep(0.01)
    server.should_exit = True
    await task
    raise RuntimeError("测试 HTTP 服务未能启动。")


def start_threaded_http_server(
    app, *, lifespan: str
) -> tuple[str, uvicorn.Server, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan=lifespan,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="phase-b-central-api",
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if server.started:
            return f"http://127.0.0.1:{port}", server, thread
        if not thread.is_alive():
            break
        time.sleep(0.01)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("测试中央 API 未能启动。")


class SidecarHttpMcpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamable_http_mcp_dispatches_through_central_api(self) -> None:
        async with MockAppServer(delay_sec=0.01) as app_server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                central_config = root / "central-agents.json"
                central_config.write_text(
                    json.dumps(
                        {
                            "agents": {
                                "supervisor-a": {
                                    "url": app_server.url,
                                    "cwd": "/srv/work",
                                    "capabilities": ["supervisor", "executor"],
                                    "capacity": 1,
                                    "orchestration_mode": "remote_sidecar",
                                    "sidecar_token_env": "PHASE_B_HTTP_TOKEN",
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                sidecar_config = root / "sidecar-agents.json"
                sidecar_config.write_text(
                    json.dumps(
                        {
                            "agents": {
                                "supervisor-a": {
                                    "url": app_server.url,
                                    "cwd": "/srv/work",
                                    "capabilities": ["supervisor", "executor"],
                                    "capacity": 1,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )

                with patch.dict(os.environ, {"PHASE_B_HTTP_TOKEN": "test-token"}):
                    central_app = create_app(
                        db_path=root / "workflows.db", config_path=central_config
                    )
                    central_url, central_server, central_thread = await asyncio.to_thread(
                        start_threaded_http_server, central_app, lifespan="off"
                    )
                    try:
                        store = central_app.state.gateway.store
                        started_at = datetime.now(UTC).isoformat()
                        store.record_sidecar_heartbeat(
                            "supervisor-a",
                            "instance-http",
                            started_at,
                            lease_timeout_sec=20,
                        )
                        store.create_workflow(
                            {
                                "workflowId": "http-mcp-demo",
                                "supervisorAgentId": "supervisor-a",
                                "handoffMode": "legacy_text",
                                "nodes": [
                                    {
                                        "id": "a",
                                        "agentId": "supervisor-a",
                                        "prompt": "只输出 a",
                                        "timeoutSec": 10,
                                    }
                                ],
                            }
                        )
                        store.claim_next_workflow(
                            "supervisor-a",
                            sidecar_instance_id="instance-http",
                            lease_timeout_sec=20,
                        )
                        runtime = InternalApiClient(
                            central_url,
                            "supervisor-a",
                            token_env="PHASE_B_HTTP_TOKEN",
                            started_at=started_at,
                            instance_id="instance-http",
                        )
                        sidecar_orchestrator = service.Orchestrator(sidecar_config)

                        service.mcp.settings.streamable_http_path = "/mcp"
                        mcp_app = service.mcp.streamable_http_app()
                        with (
                            patch.object(service, "orchestrator", sidecar_orchestrator),
                            patch.object(service, "_workflow_store", runtime),
                            patch.object(service, "_workflow_event_batcher", None),
                        ):
                            mcp_url, mcp_server, mcp_task = await start_http_server(
                                mcp_app, lifespan="on"
                            )
                            try:
                                async with streamable_http_client(
                                    mcp_url + "/mcp"
                                ) as (read_stream, write_stream, _):
                                    async with ClientSession(
                                        read_stream, write_stream
                                    ) as session:
                                        await session.initialize()
                                        tools = await session.list_tools()
                                        self.assertIn(
                                            "dispatch_node",
                                            {tool.name for tool in tools.tools},
                                        )
                                        dispatched = await session.call_tool(
                                            "dispatch_node",
                                            {
                                                "workflow_id": "http-mcp-demo",
                                                "node_id": "a",
                                            },
                                        )
                                        self.assertFalse(dispatched.isError)
                                        completed = await session.call_tool(
                                            "wait_node",
                                            {
                                                "workflow_id": "http-mcp-demo",
                                                "node_id": "a",
                                                "timeout_sec": 2,
                                            },
                                        )
                                        self.assertFalse(completed.isError)
                            finally:
                                mcp_server.should_exit = True
                                await mcp_task

                        node = store.get_node("http-mcp-demo", "a")
                        self.assertEqual(node["status"], "completed")
                        self.assertEqual(node["response"], "mock final reply")
                        self.assertTrue(
                            any(
                                request.get("method") == "turn/start"
                                for request in app_server.requests
                            )
                        )
                    finally:
                        central_server.should_exit = True
                        await asyncio.to_thread(central_thread.join, 5)


if __name__ == "__main__":
    unittest.main()
