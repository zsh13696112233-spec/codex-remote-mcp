"""常驻远程 Orchestrator Sidecar 与 Streamable HTTP MCP 入口。"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import codex_orchestrator_mcp as mcp_module
from codex_orchestrator_mcp import Orchestrator, configure_workflow_runtime, mcp
from workflow_runtime_client import InternalApiClient, resolve_token


LOGGER = logging.getLogger(__name__)


def run_heartbeat_loop(
    runtime: InternalApiClient,
    stopped: threading.Event,
    host: str,
    port: int,
) -> None:
    """等 HTTP MCP 监听成功后再上线，避免中央过早启动主监督。"""
    while not stopped.is_set():
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            stopped.wait(0.1)
    interval = 5.0
    while not stopped.is_set():
        try:
            result = runtime.heartbeat()
            interval = float(result.get("heartbeatIntervalSec") or 5)
        except Exception:
            LOGGER.warning("Sidecar 心跳失败；将在下一周期重试。")
        stopped.wait(max(1.0, min(interval, 60.0)))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex 工作流远程主监督 Sidecar")
    parser.add_argument(
        "--host", default=os.getenv("CODEX_SIDECAR_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("CODEX_SIDECAR_PORT", "8082"))
    )
    parser.add_argument(
        "--agent-id", default=os.getenv("CODEX_SIDECAR_AGENT_ID", "")
    )
    parser.add_argument(
        "--gateway-url", default=os.getenv("CODEX_GATEWAY_INTERNAL_URL", "")
    )
    parser.add_argument(
        "--token-env", default=os.getenv("CODEX_GATEWAY_TOKEN_ENV")
    )
    parser.add_argument(
        "--token-file", default=os.getenv("CODEX_GATEWAY_TOKEN_FILE")
    )
    parser.add_argument(
        "--agents",
        default=os.getenv("CODEX_AGENTS_FILE", str(mcp_module.CONFIG_PATH)),
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Sidecar 只能监听本机回环地址。")
    if not 1 <= args.port <= 65535:
        raise ValueError("Sidecar 端口必须在 1 到 65535 之间。")
    if not args.agent_id:
        raise ValueError("必须配置 CODEX_SIDECAR_AGENT_ID。")
    if not args.gateway_url:
        raise ValueError("必须配置 CODEX_GATEWAY_INTERNAL_URL。")
    # 启动时先验证令牌来源；请求时仍会重新读取，以支持不重启轮换。
    resolve_token(
        token_env=args.token_env,
        token_file=args.token_file,
        label="中央 API",
    )

    started_at = datetime.now(UTC).isoformat()
    runtime = InternalApiClient(
        args.gateway_url,
        args.agent_id,
        token_env=args.token_env,
        token_file=args.token_file,
        started_at=started_at,
    )
    configure_workflow_runtime(runtime)
    mcp_module.orchestrator = Orchestrator(Path(args.agents).expanduser())

    stopped = threading.Event()

    heartbeat_thread = threading.Thread(
        target=run_heartbeat_loop,
        args=(runtime, stopped, args.host, args.port),
        name="workflow-sidecar-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = "/mcp"
    try:
        mcp.run(transport="streamable-http")
    finally:
        stopped.set()
        heartbeat_thread.join(timeout=2)


if __name__ == "__main__":
    main()
