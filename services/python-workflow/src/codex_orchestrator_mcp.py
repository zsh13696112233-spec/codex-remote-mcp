import asyncio
import json
import ntpath
import os
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal
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

from workflow_store import WorkflowStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "agents.json"
CONFIG_PATH = Path(os.getenv("CODEX_AGENTS_FILE", DEFAULT_CONFIG_PATH)).expanduser()
DEFAULT_WORKFLOW_DB_PATH = Path(__file__).with_name("workflows.db")
WORKFLOW_DB_PATH = Path(
    os.getenv("CODEX_WORKFLOW_DB", DEFAULT_WORKFLOW_DB_PATH)
).expanduser()
MAX_PROMPT_LENGTH = 100_000
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0
INTERRUPT_TIMEOUT_SEC = 10.0


def is_absolute_remote_path(path: str) -> bool:
    """接受 Unix、Windows 盘符和 UNC 形式的远端绝对路径。"""
    return path.startswith("/") or ntpath.isabs(path)


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
        if "token" in value:
            raise ValueError(
                f"{agent_id}.token 不受支持；请在 token_env 中填写环境变量名。"
            )
        url = str(value.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"{agent_id}.url 必须是有效的 ws:// 或 wss:// 地址。")

        cwd = str(value.get("cwd", "")).strip()
        if not is_absolute_remote_path(cwd):
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
    approval_policy: Literal["never", "on-request", "untrusted"] = "never"
    approvals_reviewer: Literal["user", "auto_review"] | None = None
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
    last_event_method: str | None = None
    last_event_at: str | None = None
    ws_close_code: int | None = None
    ws_close_reason: str | None = None
    error_kind: str | None = None
    error_stage: str | None = None
    error_details: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    client: "AppServerClient | None" = field(default=None, repr=False)
    completed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    event_callback: Callable[[dict[str, Any], str], None] | None = field(
        default=None, repr=False
    )
    notification_subscribers: set[asyncio.Queue[dict[str, Any]]] = field(
        default_factory=set, repr=False
    )

    def record_event(self, method: str, received_at: str) -> None:
        self.events_seen += 1
        self.last_event_method = method
        self.last_event_at = received_at

    def record_notification(self, message: dict[str, Any], received_at: str) -> None:
        for subscriber in tuple(self.notification_subscribers):
            subscriber.put_nowait(message)
        if self.event_callback is None:
            return
        try:
            self.event_callback(message, received_at)
        except Exception:
            # 监控落库失败不能中断 Codex 的协议读取循环。
            pass

    def record_disconnect(self, code: int | None, reason: str | None) -> None:
        self.ws_close_code = code
        self.ws_close_reason = reason

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
            "last_event_method": self.last_event_method,
            "last_event_at": self.last_event_at,
            "ws_close_code": self.ws_close_code,
            "ws_close_reason": self.ws_close_reason,
            "error_kind": self.error_kind,
            "error_stage": self.error_stage,
            "error_details": self.error_details,
        }
        if include_prompt:
            result["prompt"] = self.prompt
        return result


class AppServerRpcError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)


class TurnNotActiveError(AppServerRpcError):
    pass


class AppServerRpcTimeout(TimeoutError):
    def __init__(self, method: str, timeout_sec: float) -> None:
        self.method = method
        self.timeout_sec = timeout_sec
        super().__init__(f"App Server 方法 {method} 在 {timeout_sec:g} 秒内未响应。")


class AppServerDisconnected(ConnectionError):
    def __init__(
        self,
        *,
        code: int | None,
        reason: str | None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.detail = detail
        description = detail or reason or "连接已关闭"
        close_description = f"close_code={code}" if code is not None else "无 close code"
        if reason:
            close_description += f"，close_reason={reason}"
        super().__init__(f"App Server 连接断开（{close_description}）：{description}")


class JobTotalTimeout(TimeoutError):
    def __init__(self, timeout_sec: int, details: dict[str, Any] | None = None) -> None:
        self.timeout_sec = timeout_sec
        self.details = details or {}
        super().__init__(f"任务超过 {timeout_sec} 秒。")


class AppServerClient:
    """一个连接只承载一个编排任务，避免不同 thread 的事件互相污染。"""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
        on_notification: Callable[[str, str], None] | None = None,
        on_message: Callable[[dict[str, Any], str], None] | None = None,
        on_disconnect: Callable[[int | None, str | None], None] | None = None,
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
        self._closed = asyncio.Event()
        self._disconnect_error: AppServerDisconnected | None = None
        self._closing = False
        self._on_notification = on_notification
        self._on_message = on_message
        self._on_disconnect = on_disconnect

    async def __aenter__(self) -> "AppServerClient":
        return await self.open()

    async def open(self) -> "AppServerClient":
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        try:
            self._socket = await connect(
                self.url,
                additional_headers=headers,
                open_timeout=self.request_timeout_sec,
                max_size=16 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            )
            self._reader_task = asyncio.create_task(
                self._reader(), name=f"app-server:{self.url}"
            )
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
                    },
                },
            )
            await self.notify("initialized", {})
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        self._closing = True
        close_code: int | None = None
        close_reason: str | None = None
        if self._socket is not None:
            socket = self._socket
            try:
                await socket.close()
            except Exception:
                # 清理失败不应覆盖触发清理的 RPC 超时或任务总超时。
                pass
            close_code = getattr(socket, "close_code", None)
            close_reason = getattr(socket, "close_reason", None) or None
            self._socket = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._disconnect_error is None:
            self._disconnect_error = AppServerDisconnected(
                code=close_code,
                reason=close_reason,
                detail="客户端主动关闭连接。",
            )
            self._closed.set()
        self._fail_pending(self._disconnect_error)

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
        effective_timeout = timeout_sec if timeout_sec is not None else self.request_timeout_sec
        try:
            await self._send({"method": method, "id": request_id, "params": params or {}})
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=effective_timeout,
            )
        except asyncio.CancelledError:
            self._discard_pending(request_id, future)
            raise
        except TimeoutError:
            self._discard_pending(request_id, future)
            raise AppServerRpcTimeout(method, effective_timeout) from None
        except BaseException:
            self._discard_pending(request_id, future)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def next_notification(self, timeout_sec: float) -> dict[str, Any]:
        if not self._notifications.empty():
            return self._notifications.get_nowait()
        if self._closed.is_set():
            raise self._current_disconnect_error()

        notification_task = asyncio.create_task(self._notifications.get())
        closed_task = asyncio.create_task(self._closed.wait())
        try:
            done, _ = await asyncio.wait(
                {notification_task, closed_task},
                timeout=timeout_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if notification_task in done:
                return notification_task.result()
            if closed_task in done:
                if not self._notifications.empty():
                    return self._notifications.get_nowait()
                raise self._current_disconnect_error()
            raise TimeoutError("等待 Codex App Server 执行事件超时。")
        finally:
            for task in (notification_task, closed_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(notification_task, closed_task, return_exceptions=True)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise ConnectionError("App Server 尚未连接。")
        async with self._send_lock:
            try:
                await self._socket.send(json.dumps(message, ensure_ascii=False))
            except ConnectionClosed as error:
                disconnect = self._disconnect_from_exception(error)
                self._signal_disconnect(disconnect)
                raise disconnect from error

    async def _reader(self) -> None:
        assert self._socket is not None
        disconnect: AppServerDisconnected | None = None
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
                    method = str(message["method"])
                    received_at = utc_now()
                    if self._on_notification is not None:
                        self._on_notification(method, received_at)
                    if self._on_message is not None:
                        self._on_message(message, received_at)
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as error:
            disconnect = self._disconnect_from_exception(error)
        except Exception as error:
            disconnect = self._disconnect_from_socket(detail=str(error))
        finally:
            if not self._closing:
                disconnect = disconnect or self._disconnect_from_socket()
                self._signal_disconnect(disconnect)

    def _handle_response(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
                code = error.get("code") if isinstance(error.get("code"), int) else None
            else:
                detail = str(error)
                code = None
            future.set_exception(AppServerRpcError(detail, code=code))
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

    def _discard_pending(self, request_id: int, future: asyncio.Future[Any]) -> None:
        self._pending.pop(request_id, None)
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()

    def _current_disconnect_error(self) -> AppServerDisconnected:
        return self._disconnect_error or self._disconnect_from_socket()

    def _signal_disconnect(self, error: AppServerDisconnected) -> None:
        if self._disconnect_error is not None:
            return
        self._disconnect_error = error
        if self._on_disconnect is not None:
            self._on_disconnect(error.code, error.reason)
        self._fail_pending(error)
        self._closed.set()

    def _disconnect_from_exception(self, error: ConnectionClosed) -> AppServerDisconnected:
        received = getattr(error, "rcvd", None)
        code = getattr(received, "code", None)
        reason = getattr(received, "reason", None)
        if code is None:
            return self._disconnect_from_socket(detail=str(error))
        return AppServerDisconnected(code=code, reason=reason or None, detail=str(error))

    def _disconnect_from_socket(self, detail: str | None = None) -> AppServerDisconnected:
        socket = self._socket
        code = getattr(socket, "close_code", None) if socket is not None else None
        reason = getattr(socket, "close_reason", None) if socket is not None else None
        return AppServerDisconnected(code=code, reason=reason or None, detail=detail)


class Orchestrator:
    def __init__(
        self,
        config_path: Path,
        *,
        client_factory: Callable[..., AppServerClient] = AppServerClient,
    ) -> None:
        self.config_path = config_path
        self.jobs: dict[str, Job] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._client_factory = client_factory

    def load_agents(self) -> dict[str, AgentConfig]:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"找不到执行机配置：{self.config_path}。请复制 config/agents.example.json 为 config/agents.json。"
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
        approval_policy: Literal["never", "on-request", "untrusted"] = "never",
        approvals_reviewer: Literal["user", "auto_review"] | None = None,
        event_callback: Callable[[dict[str, Any], str], None] | None = None,
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
            if not is_absolute_remote_path(cwd):
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
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            event_callback=event_callback,
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
        if not job.completed.is_set():
            try:
                await asyncio.wait_for(job.completed.wait(), timeout=timeout_sec)
            except TimeoutError:
                pass
        return job

    async def steer(self, job_id: str, text: str, client_message_id: str) -> Any:
        job = self.get_job(job_id)
        if job.status != "running" or job.client is None or not job.thread_id or not job.turn_id:
            raise TurnNotActiveError("当前监督回复已经结束。")
        try:
            return await job.client.request(
                "turn/steer",
                {
                    "threadId": job.thread_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": job.turn_id,
                    "clientUserMessageId": client_message_id,
                },
            )
        except AppServerRpcError as error:
            detail = str(error).lower()
            if any(value in detail for value in (
                "no active turn", "turn is not active", "turn completed",
                "already completed", "expectedturnid",
            )):
                raise TurnNotActiveError(str(error), code=error.code) from error
            raise

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.get_job(job_id).notification_subscribers.add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if job_id in self.jobs:
            self.jobs[job_id].notification_subscribers.discard(queue)

    async def cancel(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.status == "queued" and job.task is not None:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            if not job.completed.is_set():
                job.status = "cancelled"
                job.error = "任务在开始执行前被取消。"
                job.error_kind = "cancelled"
                job.error_stage = "queued"
                job.finished_at = utc_now()
                job.completed.set()
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
        stage = "queued"
        try:
            async with lock:
                job.status = "running"
                job.started_at = utc_now()
                stage = "configuration"
                token = None
                if agent.token_env:
                    token = os.getenv(agent.token_env)
                    if not token:
                        raise RuntimeError(f"环境变量 {agent.token_env} 未设置。")

                deadline = time.monotonic() + job.timeout_sec
                stage = "connect"
                client = self._client_factory(
                    agent.url,
                    token=token,
                    on_notification=job.record_event,
                    on_message=job.record_notification,
                    on_disconnect=job.record_disconnect,
                )
                job.client = client
                try:
                    try:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise JobTotalTimeout(job.timeout_sec)
                        try:
                            await asyncio.wait_for(client.open(), timeout=remaining)
                        except AppServerRpcTimeout:
                            raise
                        except TimeoutError:
                            raise JobTotalTimeout(job.timeout_sec) from None

                        thread_params: dict[str, Any] = {
                            "cwd": job.cwd,
                            "approvalPolicy": job.approval_policy,
                            "sandbox": "workspace-write" if job.write else "read-only",
                        }
                        if job.approvals_reviewer:
                            thread_params["approvalsReviewer"] = job.approvals_reviewer
                        if job.model:
                            thread_params["model"] = job.model

                        if job.requested_thread_id:
                            stage = "thread/resume"
                            thread_params["threadId"] = job.requested_thread_id
                        else:
                            stage = "thread/start"
                        thread_result = await self._request_with_deadline(
                            client, stage, thread_params, deadline, job.timeout_sec
                        )

                        job.thread_id = self._extract_id(thread_result, "thread")
                        stage = "turn/start"
                        turn_params: dict[str, Any] = {
                            "threadId": job.thread_id,
                            "input": [{"type": "text", "text": job.prompt}],
                            "approvalPolicy": job.approval_policy,
                        }
                        if job.approvals_reviewer:
                            turn_params["approvalsReviewer"] = job.approvals_reviewer
                        turn_result = await self._request_with_deadline(
                            client,
                            "turn/start",
                            turn_params,
                            deadline,
                            job.timeout_sec,
                        )
                        job.turn_id = self._extract_id(turn_result, "turn")
                        stage = "turn/completed"
                        await self._consume_turn(job, client, deadline)
                    except JobTotalTimeout as error:
                        error.details["interrupt"] = await self._interrupt_after_timeout(
                            job, client
                        )
                        raise
                finally:
                    await client.close()
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "任务被取消。"
            job.error_kind = "cancelled"
            job.error_stage = stage
        except JobTotalTimeout as error:
            job.status = "failed"
            job.error = str(error)
            job.error_kind = "job_timeout"
            job.error_stage = stage
            job.error_details = {
                "timeout_sec": error.timeout_sec,
                "thread_id": job.thread_id,
                "turn_id": job.turn_id,
                **error.details,
            }
        except AppServerRpcTimeout as error:
            job.status = "failed"
            job.error = str(error)
            job.error_kind = "rpc_timeout"
            job.error_stage = error.method
            job.error_details = {
                "method": error.method,
                "timeout_sec": error.timeout_sec,
            }
        except AppServerDisconnected as error:
            job.status = "failed"
            job.record_disconnect(error.code, error.reason)
            job.error = str(error)
            job.error_kind = "network_disconnect"
            job.error_stage = stage
            job.error_details = {
                "close_code": error.code,
                "close_reason": error.reason,
                "detail": error.detail,
            }
        except (ConnectionError, OSError) as error:
            job.status = "failed"
            job.error = str(error)
            job.error_kind = "network_disconnect"
            job.error_stage = stage
            job.error_details = {
                "close_code": job.ws_close_code,
                "close_reason": job.ws_close_reason,
                "detail": str(error),
            }
        except AppServerRpcError as error:
            job.status = "failed"
            job.error = str(error)
            job.error_kind = "rpc_error"
            job.error_stage = stage
            job.error_details = {"detail": str(error)}
        except Exception as error:
            job.status = "failed"
            job.error = str(error)
            job.error_kind = "configuration_error" if stage == "configuration" else "internal_error"
            job.error_stage = stage
            job.error_details = {"detail": str(error)}
        finally:
            job.client = None
            job.finished_at = utc_now()
            job.completed.set()

    @staticmethod
    async def _request_with_deadline(
        client: AppServerClient,
        method: str,
        params: dict[str, Any],
        deadline: float,
        job_timeout_sec: int,
    ) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JobTotalTimeout(job_timeout_sec)
        deadline_is_limit = remaining < client.request_timeout_sec
        timeout_sec = min(remaining, client.request_timeout_sec)
        try:
            return await client.request(method, params, timeout_sec=timeout_sec)
        except AppServerRpcTimeout:
            if deadline_is_limit:
                raise JobTotalTimeout(job_timeout_sec) from None
            raise

    @staticmethod
    async def _interrupt_after_timeout(
        job: Job,
        client: AppServerClient,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"attempted": False, "succeeded": False}
        if not job.thread_id or not job.turn_id:
            return result
        result["attempted"] = True
        try:
            await client.request(
                "turn/interrupt",
                {"threadId": job.thread_id, "turnId": job.turn_id},
                timeout_sec=INTERRUPT_TIMEOUT_SEC,
            )
            result["succeeded"] = True
        except Exception as error:
            result["error_type"] = type(error).__name__
            result["error"] = str(error)
        return result

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
                raise JobTotalTimeout(job.timeout_sec)

            try:
                event = await client.next_notification(remaining)
            except TimeoutError:
                raise JobTotalTimeout(job.timeout_sec) from None
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
                    if isinstance(error, dict):
                        job.error = str(error.get("message") or f"turn 状态为 {turn_status}。")
                    else:
                        job.error = str(error or f"turn 状态为 {turn_status}。")
                    job.error_kind = "turn_failed"
                    job.error_stage = "turn/completed"
                    job.error_details = {
                        "turn_status": turn_status,
                        "turn_error": error,
                    }
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
_workflow_store: WorkflowStore | None = None
_workflow_monitors: set[asyncio.Task[None]] = set()


def get_workflow_store() -> WorkflowStore:
    global _workflow_store
    if _workflow_store is None:
        _workflow_store = WorkflowStore(WORKFLOW_DB_PATH)
    return _workflow_store


def _workflow_node_snapshot(workflow_id: str, node_id: str) -> dict[str, Any]:
    workflow = get_workflow_store().get_workflow(workflow_id)
    for node in workflow["nodes"]:
        if node["id"] == node_id:
            return node
    raise ValueError(f"找不到节点：{node_id}")


async def _monitor_workflow_node(workflow_id: str, node_id: str, job: Job) -> None:
    await job.completed.wait()
    get_workflow_store().sync_node_job(workflow_id, node_id, job.snapshot())


def _track_workflow_node(workflow_id: str, node_id: str, job: Job) -> None:
    task = asyncio.create_task(
        _monitor_workflow_node(workflow_id, node_id, job),
        name=f"workflow-node:{workflow_id}:{node_id}",
    )
    _workflow_monitors.add(task)
    task.add_done_callback(_workflow_monitors.discard)


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


@mcp.tool()
async def dispatch_node(workflow_id: str, node_id: str) -> dict[str, Any]:
    """派发工作流节点；节点配置来自共享数据库，且依赖未完成时拒绝启动。"""
    store = get_workflow_store()
    node = store.prepare_node_dispatch(workflow_id, node_id)
    if node["alreadyDispatched"]:
        job_id = node.get("jobId")
        if job_id and job_id in orchestrator.jobs:
            return orchestrator.get_job(job_id).snapshot()
        return _workflow_node_snapshot(workflow_id, node_id)

    def record(message: dict[str, Any], received_at: str) -> None:
        method = str(message.get("method") or "unknown")
        store.add_event(
            workflow_id,
            node_id=node_id,
            source="worker",
            event_type=f"appserver.{method}",
            payload={"receivedAt": received_at, "message": message},
        )

    try:
        job = await orchestrator.dispatch(
            agent_id=node["agentId"],
            prompt=node["prompt"],
            thread_id=None,
            cwd=node["cwd"],
            write=node["write"],
            model=node["model"],
            timeout_sec=node["timeoutSec"],
            event_callback=record,
        )
    except Exception as error:
        store.sync_node_job(
            workflow_id,
            node_id,
            {
                "status": "failed",
                "error": str(error),
                "finished_at": utc_now(),
            },
        )
        raise
    store.attach_node_job(workflow_id, node_id, job.snapshot())
    _track_workflow_node(workflow_id, node_id, job)
    return job.snapshot()


@mcp.tool()
def node_status(workflow_id: str, node_id: str) -> dict[str, Any]:
    """查询工作流节点当前状态、执行机、thread/turn id 和结果。"""
    node = _workflow_node_snapshot(workflow_id, node_id)
    job_id = node.get("jobId")
    if job_id and job_id in orchestrator.jobs:
        snapshot = orchestrator.get_job(job_id).snapshot()
        get_workflow_store().sync_node_job(workflow_id, node_id, snapshot)
        return snapshot
    return node


@mcp.tool()
async def wait_node(
    workflow_id: str, node_id: str, timeout_sec: int = 300
) -> dict[str, Any]:
    """等待节点；超时仅返回当前状态，主监督会话可以继续调用本工具。"""
    node = _workflow_node_snapshot(workflow_id, node_id)
    job_id = node.get("jobId")
    if not job_id:
        raise ValueError(f"节点尚未派发：{node_id}")
    if job_id not in orchestrator.jobs:
        return node
    job = await orchestrator.wait(job_id, timeout_sec)
    snapshot = job.snapshot()
    get_workflow_store().sync_node_job(workflow_id, node_id, snapshot)
    return snapshot


@mcp.tool()
async def cancel_node(workflow_id: str, node_id: str) -> dict[str, Any]:
    """取消指定工作流节点。"""
    node = _workflow_node_snapshot(workflow_id, node_id)
    job_id = node.get("jobId")
    if not job_id:
        raise ValueError(f"节点尚未派发：{node_id}")
    if job_id not in orchestrator.jobs:
        raise ValueError("节点由另一个编排器进程持有，当前进程无法直接取消。")
    job = await orchestrator.cancel(job_id)
    snapshot = job.snapshot()
    get_workflow_store().sync_node_job(workflow_id, node_id, snapshot)
    return snapshot


@mcp.tool()
def workflow_status(workflow_id: str) -> dict[str, Any]:
    """只读获取工作流最新状态；结果不包含内部 thread、turn、job 等标识。"""
    snapshot = get_workflow_store().get_workflow(workflow_id)
    return {
        "workflowId": snapshot["workflowId"],
        "name": snapshot["name"],
        "status": snapshot["status"],
        "stateVersion": snapshot["stateVersion"],
        "progress": snapshot["progress"],
        "currentSteps": snapshot["currentNodes"],
        "steps": [
            {
                "id": node["id"],
                "displayName": node["displayName"],
                "status": node["status"],
                "response": node["response"],
                "error": node["error"],
                "attemptCount": node["attemptCount"],
            }
            for node in snapshot["nodes"]
        ],
    }


@mcp.tool()
def propose_workflow_control(
    workflow_id: str, action_type: str, message_id: str, node_id: str | None = None
) -> dict[str, Any]:
    """仅提议停止、重试或跳过操作；不会改变任务，用户必须另发“确认执行”。"""
    snapshot = get_workflow_store().get_workflow(workflow_id)
    if action_type == "stop":
        if snapshot["status"] in {"completed", "cancelled"}:
            raise ValueError("当前任务已经结束，不能停止。")
        node_id = None
    elif action_type in {"retry", "skip"}:
        node = next((item for item in snapshot["nodes"] if item["id"] == node_id), None)
        if node is None:
            raise ValueError("必须指定存在的步骤。")
        if action_type == "retry" and node["status"] not in {
            "queued", "running", "cancelling", "failed", "cancelled", "interrupted"
        }:
            raise ValueError("只能重试正在执行或未成功的步骤。")
        if action_type == "skip" and node["status"] in {"completed", "skipped"}:
            raise ValueError("已完成或已跳过的步骤不能跳过。")
    else:
        raise ValueError("只支持停止任务、重试步骤和跳过步骤。")
    return get_workflow_store().propose_control(
        workflow_id, action_type, node_id, message_id
    )


@mcp.tool()
def cancel_workflow_control(workflow_id: str, message_id: str) -> dict[str, Any]:
    """取消当前等待确认的聊天控制操作。"""
    return get_workflow_store().cancel_pending_control(workflow_id, message_id)


@mcp.tool()
async def execute_workflow_control(
    workflow_id: str, action_id: str, confirmation_message_id: str
) -> dict[str, Any]:
    """执行已经由另一条“确认执行”消息确认的控制操作。"""
    store = get_workflow_store()
    confirmed = store.confirm_control(workflow_id, action_id, confirmation_message_id)
    action = store.start_control_execution(action_id)
    try:
        snapshot = store.get_workflow(workflow_id)
        targets = snapshot["nodes"] if action["actionType"] == "stop" else [
            next(item for item in snapshot["nodes"] if item["id"] == action["nodeId"])
        ]
        for node in targets:
            job_id = node.get("jobId")
            is_active = node["status"] in {"queued", "running", "cancelling"}
            if is_active and (not job_id or job_id not in orchestrator.jobs):
                raise RuntimeError("当前无法安全中止正在执行的步骤，请稍后重试控制操作。")
            if job_id and job_id in orchestrator.jobs:
                job = await orchestrator.cancel(job_id)
                if not job.completed.is_set():
                    job = await orchestrator.wait(job_id, 10)
                if not job.completed.is_set() and action["actionType"] in {"retry", "skip"}:
                    raise RuntimeError("步骤尚未完全停止，暂不能重试或跳过，请稍后再试。")
                store.sync_node_job(workflow_id, node["id"], job.snapshot())

        if action["actionType"] == "stop":
            result = store.stop_workflow(workflow_id)
        elif action["actionType"] == "retry":
            result = store.reset_node_for_retry(workflow_id, str(action["nodeId"]))
        else:
            result = store.skip_node(workflow_id, str(action["nodeId"]))
        public_result = {
            "workflowId": workflow_id,
            "status": result["status"],
            "actionType": action["actionType"],
            "nodeId": action["nodeId"],
        }
        store.finish_control_execution(action_id, result=public_result)
        return public_result
    except Exception as error:
        store.finish_control_execution(action_id, error=str(error))
        raise


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
