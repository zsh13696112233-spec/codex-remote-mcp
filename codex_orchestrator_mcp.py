import asyncio
import json
import os
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

# MCP 1.12 与新版 pydantic-settings 在导入 FastMCP 时会对未使用的
# lifespan 字段产生兼容性警告；它不影响 stdio Server，但会污染 MCP stderr。
from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning

warnings.filterwarnings(
    "ignore",
    message="Field 'lifespan' has an incomplete definition.*",
    category=IncompleteFieldDefinitionWarning,
)

from mcp.server.fastmcp import FastMCP
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

DEFAULT_CONFIG_PATH = Path(__file__).with_name("agents.json")
CONFIG_PATH = Path(os.getenv("CODEX_AGENTS_FILE", DEFAULT_CONFIG_PATH)).expanduser()
MAX_PROMPT_LENGTH = 100_000


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    url: str
    cwd: str
    token_env: str | None = None
    allow_write: bool = False
    allow_cwd_override: bool = False
    model: str | None = None

    @classmethod
    def from_dict(cls, agent_id: str, value: dict[str, Any]) -> "AgentConfig":
        url = str(value.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"{agent_id}.url 必须是有效的 ws:// 或 wss:// 地址。")

        cwd = str(value.get("cwd", "")).strip()
        if not cwd.startswith("/"):
            raise ValueError(f"{agent_id}.cwd 必须是执行机上的绝对路径。")

        token_env = value.get("token_env")
        if token_env is not None and not str(token_env).strip():
            token_env = None

        return cls(
            agent_id=agent_id,
            url=url,
            cwd=cwd,
            token_env=str(token_env) if token_env else None,
            allow_write=bool(value.get("allow_write", False)),
            allow_cwd_override=bool(value.get("allow_cwd_override", False)),
            model=str(value["model"]) if value.get("model") else None,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "url": self.url,
            "cwd": self.cwd,
            "authenticated": self.token_env is not None,
            "allow_write": self.allow_write,
            "allow_cwd_override": self.allow_cwd_override,
            "model": self.model,
        }


@dataclass
class Job:
    job_id: str
    agent_id: str
    prompt: str
    requested_thread_id: str | None
    cwd: str
    write: bool
    model: str | None
    timeout_sec: int
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    ] = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    response: str | None = None
    error: str | None = None
    diff: str | None = None
    events_seen: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    client: "AppServerClient | None" = field(default=None, repr=False)

    def snapshot(self, *, include_prompt: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "job_id": self.job_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "cwd": self.cwd,
            "write": self.write,
            "model": self.model,
            "response": self.response,
            "error": self.error,
            "diff": self.diff,
            "events_seen": self.events_seen,
        }
        if include_prompt:
            result["prompt"] = self.prompt
        return result


class AppServerRpcError(RuntimeError):
    pass


class AppServerClient:
    """一个连接只承载一个编排任务，避免不同 thread 的事件互相污染。"""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        request_timeout_sec: float = 30,
    ) -> None:
        self.url = url
        self.token = token
        self.request_timeout_sec = request_timeout_sec
        self._socket: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._next_request_id = 1
        self._send_lock = asyncio.Lock()

    async def __aenter__(self) -> "AppServerClient":
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        self._socket = await connect(
            self.url,
            additional_headers=headers,
            open_timeout=self.request_timeout_sec,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        self._reader_task = asyncio.create_task(self._reader(), name=f"app-server:{self.url}")
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_orchestrator_mcp",
                    "title": "Codex Orchestrator MCP",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "optOutNotificationMethods": ["item/agentMessage/delta"],
                },
            },
        )
        await self.notify("initialized", {})
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        self._fail_pending(ConnectionError("App Server 连接已关闭。"))

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> Any:
        if self._socket is None:
            raise ConnectionError("App Server 尚未连接。")

        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=timeout_sec or self.request_timeout_sec,
            )
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"App Server 方法 {method} 响应超时。") from None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def next_notification(self, timeout_sec: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._notifications.get(), timeout=timeout_sec)
        except TimeoutError:
            raise TimeoutError("等待 Codex App Server 执行事件超时。") from None

    async def _send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise ConnectionError("App Server 尚未连接。")
        async with self._send_lock:
            await self._socket.send(json.dumps(message, ensure_ascii=False))

    async def _reader(self) -> None:
        assert self._socket is not None
        try:
            async for raw_message in self._socket:
                if not isinstance(raw_message, str):
                    continue
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    continue

                if "id" in message and "method" in message:
                    await self._handle_server_request(message)
                elif "id" in message:
                    self._handle_response(message)
                elif "method" in message:
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as error:
            self._fail_pending(ConnectionError(f"App Server 连接断开：{error}"))
        except Exception as error:
            self._fail_pending(error)

    def _handle_response(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                detail = str(error)
            future.set_exception(AppServerRpcError(detail))
        else:
            future.set_result(message.get("result"))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """无人值守第一版：拒绝所有审批与交互请求，避免任务永久挂起。"""
        method = str(message.get("method", ""))
        request_id = message["id"]
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            await self._send({"id": request_id, "result": {"decision": "decline"}})
        elif method == "item/permissions/requestApproval":
            await self._send({"id": request_id, "result": {"permissions": []}})
        else:
            await self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"编排器不支持交互式请求：{method}",
                    },
                }
            )

    def _fail_pending(self, error: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)


class Orchestrator:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.jobs: dict[str, Job] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}

    def load_agents(self) -> dict[str, AgentConfig]:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"找不到执行机配置：{self.config_path}。请复制 agents.example.json 为 agents.json。"
            )
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"执行机配置不是有效 JSON：{error}") from error

        values = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(values, dict) or not values:
            raise ValueError("配置必须包含非空的 agents 对象。")

        agents: dict[str, AgentConfig] = {}
        for agent_id, value in values.items():
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError("agent_id 必须是非空字符串。")
            if not isinstance(value, dict):
                raise ValueError(f"{agent_id} 的配置必须是对象。")
            agents[agent_id] = AgentConfig.from_dict(agent_id, value)
        return agents

    def list_agents(self) -> list[dict[str, Any]]:
        return [agent.public_dict() for agent in self.load_agents().values()]

    async def dispatch(
        self,
        *,
        agent_id: str,
        prompt: str,
        thread_id: str | None,
        cwd: str | None,
        write: bool,
        model: str | None,
        timeout_sec: int,
    ) -> Job:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空。")
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise ValueError(f"prompt 不能超过 {MAX_PROMPT_LENGTH} 个字符。")
        if not 10 <= timeout_sec <= 7200:
            raise ValueError("timeout_sec 必须在 10 到 7200 秒之间。")

        agents = self.load_agents()
        if agent_id not in agents:
            raise ValueError(f"未知执行机 {agent_id}，可用值：{', '.join(agents)}")
        agent = agents[agent_id]

        selected_cwd = agent.cwd
        if cwd is not None:
            if not agent.allow_cwd_override:
                raise PermissionError(f"{agent_id} 不允许覆盖 cwd。")
            if not cwd.startswith("/"):
                raise ValueError("cwd 必须是执行机上的绝对路径。")
            selected_cwd = cwd
        if write and not agent.allow_write:
            raise PermissionError(f"{agent_id} 未启用写权限。")

        job = Job(
            job_id=uuid.uuid4().hex,
            agent_id=agent_id,
            prompt=prompt,
            requested_thread_id=thread_id,
            cwd=selected_cwd,
            write=write,
            model=model or agent.model,
            timeout_sec=timeout_sec,
        )
        self.jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run_job(job, agent), name=f"codex-job:{job.job_id}")
        return job

    def get_job(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise ValueError(f"找不到任务：{job_id}") from None

    async def wait(self, job_id: str, timeout_sec: int) -> Job:
        if not 1 <= timeout_sec <= 600:
            raise ValueError("wait timeout_sec 必须在 1 到 600 秒之间。")
        job = self.get_job(job_id)
        if job.task is not None and not job.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=timeout_sec)
            except TimeoutError:
                pass
        return job

    async def cancel(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.status == "queued" and job.task is not None:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            return job
        if job.status == "running" and job.client and job.thread_id and job.turn_id:
            job.status = "cancelling"
            await job.client.request(
                "turn/interrupt",
                {"threadId": job.thread_id, "turnId": job.turn_id},
            )
        return job

    async def _run_job(self, job: Job, agent: AgentConfig) -> None:
        lock = self._agent_locks.setdefault(agent.agent_id, asyncio.Lock())
        try:
            async with lock:
                job.status = "running"
                job.started_at = utc_now()
                token = None
                if agent.token_env:
                    token = os.getenv(agent.token_env)
                    if not token:
                        raise RuntimeError(f"环境变量 {agent.token_env} 未设置。")

                deadline = time.monotonic() + job.timeout_sec
                client = AppServerClient(agent.url, token=token)
                job.client = client
                async with client:
                    thread_params: dict[str, Any] = {
                        "cwd": job.cwd,
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write" if job.write else "read-only",
                    }
                    if job.model:
                        thread_params["model"] = job.model

                    if job.requested_thread_id:
                        thread_params["threadId"] = job.requested_thread_id
                        thread_result = await client.request("thread/resume", thread_params)
                    else:
                        thread_result = await client.request("thread/start", thread_params)

                    job.thread_id = self._extract_id(thread_result, "thread")
                    turn_result = await client.request(
                        "turn/start",
                        {
                            "threadId": job.thread_id,
                            "input": [{"type": "text", "text": job.prompt}],
                            "approvalPolicy": "never",
                        },
                    )
                    job.turn_id = self._extract_id(turn_result, "turn")
                    await self._consume_turn(job, client, deadline)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "任务在开始执行前被取消。"
        except Exception as error:
            job.status = "failed"
            job.error = str(error)
        finally:
            job.client = None
            job.finished_at = utc_now()

    async def _consume_turn(
        self,
        job: Job,
        client: AppServerClient,
        deadline: float,
    ) -> None:
        agent_messages: list[tuple[str | None, str]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if job.thread_id and job.turn_id:
                    try:
                        await client.request(
                            "turn/interrupt",
                            {"threadId": job.thread_id, "turnId": job.turn_id},
                            timeout_sec=10,
                        )
                    except Exception:
                        pass
                raise TimeoutError(f"任务超过 {job.timeout_sec} 秒。")

            event = await client.next_notification(remaining)
            job.events_seen += 1
            method = event.get("method")
            params = event.get("params") or {}

            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    agent_messages.append((item.get("phase"), str(item["text"])))
            elif method == "turn/diff/updated":
                job.diff = params.get("diff")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                event_turn_id = turn.get("id")
                if event_turn_id and event_turn_id != job.turn_id:
                    continue
                final_messages = [text for phase, text in agent_messages if phase == "final_answer"]
                if not final_messages:
                    final_messages = [text for _, text in agent_messages]
                job.response = final_messages[-1] if final_messages else None

                turn_status = str(turn.get("status", "completed"))
                if turn_status == "completed":
                    job.status = "completed"
                elif turn_status == "interrupted":
                    job.status = "interrupted"
                else:
                    job.status = "failed"
                    error = turn.get("error") or {}
                    job.error = error.get("message") if isinstance(error, dict) else str(error)
                return

    @staticmethod
    def _extract_id(result: Any, entity: str) -> str:
        if not isinstance(result, dict):
            raise AppServerRpcError(f"{entity} 响应格式错误。")
        value = result.get(entity)
        if not isinstance(value, dict) or not value.get("id"):
            raise AppServerRpcError(f"{entity} 响应缺少 id。")
        return str(value["id"])


orchestrator = Orchestrator(CONFIG_PATH)
mcp = FastMCP("Codex Orchestrator")


@mcp.tool()
def list_agents() -> dict[str, Any]:
    """读取当前可调度的 Codex 执行机白名单。配置文件修改后会自动重新读取。"""
    return {
        "config_path": str(orchestrator.config_path),
        "agents": orchestrator.list_agents(),
    }


@mcp.tool()
async def dispatch(
    agent_id: str,
    prompt: str,
    thread_id: str | None = None,
    cwd: str | None = None,
    write: bool = False,
    model: str | None = None,
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    """向指定远程 Codex 派发任务并立即返回 job_id，不阻塞等待任务完成。"""
    job = await orchestrator.dispatch(
        agent_id=agent_id,
        prompt=prompt,
        thread_id=thread_id,
        cwd=cwd,
        write=write,
        model=model,
        timeout_sec=timeout_sec,
    )
    return job.snapshot()


@mcp.tool()
def status(job_id: str) -> dict[str, Any]:
    """查询远程 Codex 任务的当前状态和已产生的结果。"""
    return orchestrator.get_job(job_id).snapshot()


@mcp.tool()
async def wait_result(job_id: str, timeout_sec: int = 300) -> dict[str, Any]:
    """等待任务完成；等待超时只返回当前状态，不会取消远程任务。"""
    job = await orchestrator.wait(job_id, timeout_sec)
    return job.snapshot()


@mcp.tool()
async def cancel(job_id: str) -> dict[str, Any]:
    """取消排队任务，或请求远程 App Server 中断正在执行的 turn。"""
    job = await orchestrator.cancel(job_id)
    return job.snapshot()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
