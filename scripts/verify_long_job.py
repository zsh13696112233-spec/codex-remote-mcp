"""通过真实 stdio MCP 子进程验证超过 60 秒的远端 turn。

默认运行约 65 秒：
    python scripts/verify_long_job.py

开发时可缩短：
    python scripts/verify_long_job.py --delay-sec 3 --wait-sec 1
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYTHON_SERVICE = REPOSITORY_ROOT / "services" / "python-workflow"
PYTHON_SOURCE = PYTHON_SERVICE / "src"
for import_path in (PYTHON_SOURCE, PYTHON_SERVICE):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from tests.mock_app_server import MockAppServer


def tool_payload(result: Any) -> dict[str, Any]:
    if result.isError:
        raise RuntimeError(f"MCP 工具调用失败：{result.content}")
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    for content in result.content:
        if getattr(content, "type", None) == "text":
            value = json.loads(content.text)
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"MCP 工具未返回对象：{result.content}")


async def verify(delay_sec: float, wait_sec: int) -> dict[str, Any]:
    secret = "long-job-verification-token"
    async with MockAppServer(delay_sec=delay_sec) as app_server:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "agents.json")
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "mock-remote": {
                                "url": app_server.url,
                                "cwd": r"D:\codex",
                                "token_env": "VERIFY_REMOTE_CODEX_TOKEN",
                                "allow_write": False,
                                "allow_cwd_override": False,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            child_env = os.environ.copy()
            child_env["CODEX_AGENTS_FILE"] = str(config_path)
            child_env["VERIFY_REMOTE_CODEX_TOKEN"] = secret
            server = StdioServerParameters(
                command=sys.executable,
                args=[str(PYTHON_SOURCE / "codex_orchestrator_mcp.py")],
                cwd=PYTHON_SERVICE,
                env=child_env,
            )

            calls = 0
            statuses: list[str] = []
            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    dispatched = tool_payload(
                        await session.call_tool(
                            "dispatch",
                            {
                                "agent_id": "mock-remote",
                                "prompt": "Return the long-running verification reply.",
                                "timeout_sec": max(10, int(delay_sec) + 30),
                            },
                        )
                    )
                    job_id = str(dispatched["job_id"])

                    while True:
                        status_result = tool_payload(
                            await session.call_tool("status", {"job_id": job_id})
                        )
                        calls += 1
                        statuses.append(str(status_result["status"]))
                        if status_result["status"] in {
                            "completed",
                            "failed",
                            "cancelled",
                            "interrupted",
                        }:
                            final = status_result
                            break

                        final = tool_payload(
                            await session.call_tool(
                                "wait_result",
                                {"job_id": job_id, "timeout_sec": wait_sec},
                                read_timeout_seconds=timedelta(seconds=wait_sec + 5),
                            )
                        )
                        calls += 1
                        statuses.append(str(final["status"]))
                        if final["status"] in {
                            "completed",
                            "failed",
                            "cancelled",
                            "interrupted",
                        }:
                            break

            assert final["job_id"] == job_id
            assert final["status"] == "completed", final
            assert final["response"] == "mock final reply", final
            assert final["last_event_method"] == "turn/completed", final
            assert final["events_seen"] == 2, final
            assert calls >= 3, (calls, statuses)
            assert "running" in statuses, statuses
            assert app_server.authorization == f"Bearer {secret}"
            assert secret not in json.dumps(final)
            return {
                "job_id": job_id,
                "delay_sec": delay_sec,
                "tool_calls_after_dispatch": calls,
                "statuses": statuses,
                "final_status": final["status"],
                "response": final["response"],
                "events_seen": final["events_seen"],
                "last_event_method": final["last_event_method"],
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-sec", type=float, default=65.0)
    parser.add_argument("--wait-sec", type=int, default=5)
    args = parser.parse_args()
    if args.delay_sec <= 0:
        parser.error("--delay-sec 必须大于 0")
    if not 1 <= args.wait_sec <= 600:
        parser.error("--wait-sec 必须在 1 到 600 之间")
    result = asyncio.run(verify(args.delay_sec, args.wait_sec))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
