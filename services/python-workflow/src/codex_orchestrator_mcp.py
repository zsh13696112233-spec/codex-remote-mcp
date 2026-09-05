import asyncio
import hashlib
import inspect
import json
import logging
import ntpath
import os
import posixpath
import shutil
import time
import uuid
import warnings
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

# MCP 与部分 pydantic-settings 版本组合在导入 FastMCP 时会对未使用的
# lifespan 字段产生兼容性警告；它不影响 stdio Server，但会污染 MCP stderr。
warnings.filterwarnings(
    "ignore",
    message="Field 'lifespan' has an incomplete definition.*",
)

from mcp.server.fastmcp import FastMCP
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from workflow_event_batcher import AsyncEventBatcher
from workflow_store import ARTIFACT_LIMIT, WorkflowStore
from workflow_runtime_client import InternalApiClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "agents.json"
CONFIG_PATH = Path(os.getenv("CODEX_AGENTS_FILE", DEFAULT_CONFIG_PATH)).expanduser()
DEFAULT_WORKFLOW_DB_PATH = Path(__file__).with_name("workflows.db")
WORKFLOW_DB_PATH = Path(
    os.getenv("CODEX_WORKFLOW_DB", DEFAULT_WORKFLOW_DB_PATH)
).expanduser()
MAX_PROMPT_LENGTH = 100_000
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0
FINAL_ANSWER_COMPLETION_GRACE_SEC = 5.0
TURN_RECONCILIATION_FAILURE_LIMIT = 3
INTERRUPT_TIMEOUT_SEC = 10.0
LOGGER = logging.getLogger(__name__)
FULL_ACCESS_PERMISSION_PROFILE = "full_access"
PERMISSION_PROFILES = (
    "read_only",
    "workspace_write",
    "auto_review",
    FULL_ACCESS_PERMISSION_PROFILE,
)
AGENT_CAPABILITIES = ("supervisor", "executor")
MAX_TOKEN_FILE_BYTES = 8 * 1024


def permission_settings(
    profile: str,
) -> tuple[
    bool,
    Literal["read-only", "workspace-write", "danger-full-access"],
    Literal["never", "on-request"],
    Literal["auto_review"] | None,
]:
    """把业务权限档位集中映射到 App Server 的写入、审批和审核者参数。"""
    if profile == "read_only":
        return False, "read-only", "never", None
    if profile == "workspace_write":
        return True, "workspace-write", "never", None
    if profile == "auto_review":
        return True, "workspace-write", "on-request", "auto_review"
    if profile == FULL_ACCESS_PERMISSION_PROFILE:
        return True, "danger-full-access", "never", None
    raise ValueError(
        "permission_profile 只能是 read_only、workspace_write、auto_review 或 full_access。"
    )


def is_absolute_remote_path(path: str) -> bool:
    """接受 Unix、Windows 盘符和 UNC 形式的远端绝对路径。"""
    return path.startswith("/") or ntpath.isabs(path)


def remote_path_join(root: str, *parts: str) -> str:
    """使用执行机路径风格拼接受控目录，不依赖编排器所在系统。"""
    path_module = ntpath if ntpath.isabs(root) and not root.startswith("/") else posixpath
    safe_parts: list[str] = []
    for part in parts:
        value = str(part)
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("托管目录名称无效。")
        safe_parts.append(value)
    return path_module.join(root, *safe_parts)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    url: str
    cwd: str
    enabled: bool = True
    capabilities: tuple[str, ...] = ("executor",)
    capacity: int = 0
    token_env: str | None = None
    token_file: str | None = None
    orchestration_mode: str = "local_db"
    sidecar_token_env: str | None = None
    sidecar_token_file: str | None = None
    allow_write: bool = False
    allow_full_access: bool = False
    allow_cwd_override: bool = False
    model: str | None = None
    artifact_root: str | None = None

    @classmethod
    def from_dict(cls, agent_id: str, value: dict[str, Any]) -> "AgentConfig":
        if "token" in value:
            raise ValueError(
                f"{agent_id}.token 不受支持；请使用 token_env 或 token_file。"
            )
        url = str(value.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"{agent_id}.url 必须是有效的 ws:// 或 wss:// 地址。")

        cwd = str(value.get("cwd", "")).strip()
        if not is_absolute_remote_path(cwd):
            raise ValueError(f"{agent_id}.cwd 必须是执行机上的绝对路径。")

        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{agent_id}.enabled 必须是布尔值。")
        raw_capabilities = value.get("capabilities")
        if raw_capabilities is None:
            capabilities = (
                ("supervisor", "executor") if agent_id == "local" else ("executor",)
            )
        else:
            if not isinstance(raw_capabilities, list) or not raw_capabilities:
                raise ValueError(f"{agent_id}.capabilities 必须是非空数组。")
            normalized_capabilities: list[str] = []
            for raw_capability in raw_capabilities:
                capability = str(raw_capability).strip().lower()
                if capability not in AGENT_CAPABILITIES:
                    raise ValueError(
                        f"{agent_id}.capabilities 只能包含 supervisor 或 executor。"
                    )
                if capability not in normalized_capabilities:
                    normalized_capabilities.append(capability)
            capabilities = tuple(normalized_capabilities)

        raw_capacity = value.get("capacity")
        if "supervisor" in capabilities:
            capacity = 1 if raw_capacity is None else raw_capacity
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity != 1:
                raise ValueError(f"{agent_id}.capacity 第一阶段只能是 1。")
        else:
            if raw_capacity is not None:
                raise ValueError(
                    f"{agent_id} 不具备 supervisor 能力，不能配置 capacity。"
                )
            capacity = 0

        token_env = value.get("token_env")
        if token_env is not None and not str(token_env).strip():
            token_env = None
        token_file = value.get("token_file")
        if token_file is not None and not str(token_file).strip():
            token_file = None
        if token_env and token_file:
            raise ValueError(
                f"{agent_id}.token_env 和 token_file 只能配置一个。"
            )
        if token_file:
            token_path = Path(str(token_file).strip())
            if not token_path.is_absolute():
                raise ValueError(
                    f"{agent_id}.token_file 必须是网关所在机器上的绝对路径。"
                )
            token_file = str(token_path)

        orchestration_mode = str(
            value.get("orchestration_mode", "local_db")
        ).strip().lower()
        if orchestration_mode not in {"local_db", "remote_sidecar"}:
            raise ValueError(
                f"{agent_id}.orchestration_mode 只能是 local_db 或 remote_sidecar。"
            )
        sidecar_token_env = value.get("sidecar_token_env")
        if sidecar_token_env is not None and not str(sidecar_token_env).strip():
            sidecar_token_env = None
        sidecar_token_file = value.get("sidecar_token_file")
        if sidecar_token_file is not None and not str(sidecar_token_file).strip():
            sidecar_token_file = None
        if sidecar_token_env and sidecar_token_file:
            raise ValueError(
                f"{agent_id}.sidecar_token_env 和 sidecar_token_file 只能配置一个。"
            )
        if orchestration_mode == "remote_sidecar":
            if "supervisor" not in capabilities:
                raise ValueError(
                    f"{agent_id} 使用 remote_sidecar 时必须具备 supervisor 能力。"
                )
            if not sidecar_token_env and not sidecar_token_file:
                raise ValueError(
                    f"{agent_id} 使用 remote_sidecar 时必须配置 Sidecar 令牌来源。"
                )
        elif sidecar_token_env or sidecar_token_file:
            raise ValueError(
                f"{agent_id} 只有使用 remote_sidecar 时才能配置 Sidecar 令牌。"
            )
        if sidecar_token_file:
            sidecar_token_path = Path(str(sidecar_token_file).strip())
            if not sidecar_token_path.is_absolute():
                raise ValueError(
                    f"{agent_id}.sidecar_token_file 必须是网关所在机器上的绝对路径。"
                )
            sidecar_token_file = str(sidecar_token_path)
        artifact_root_value = value.get("artifact_root")
        artifact_root = None
        if artifact_root_value is not None:
            artifact_root = str(artifact_root_value).strip()
            if not is_absolute_remote_path(artifact_root):
                raise ValueError(f"{agent_id}.artifact_root 必须是执行机上的绝对路径。")
            path_module = (
                ntpath
                if ntpath.isabs(artifact_root) and not artifact_root.startswith("/")
                else posixpath
            )
            artifact_root = path_module.normpath(artifact_root)

        allow_write = bool(value.get("allow_write", False))
        allow_full_access = value.get("allow_full_access", False)
        if not isinstance(allow_full_access, bool):
            raise ValueError(f"{agent_id}.allow_full_access 必须是布尔值。")
        if allow_full_access and not allow_write:
            raise ValueError(
                f"{agent_id}.allow_full_access=true 时必须同时启用 allow_write。"
            )

        return cls(
            agent_id=agent_id,
            url=url,
            cwd=cwd,
            enabled=enabled,
            capabilities=capabilities,
            capacity=capacity,
            token_env=str(token_env).strip() if token_env else None,
            token_file=str(token_file) if token_file else None,
            orchestration_mode=orchestration_mode,
            sidecar_token_env=(
                str(sidecar_token_env).strip() if sidecar_token_env else None
            ),
            sidecar_token_file=(
                str(sidecar_token_file) if sidecar_token_file else None
            ),
            allow_write=allow_write,
            allow_full_access=allow_full_access,
            allow_cwd_override=bool(value.get("allow_cwd_override", False)),
            model=str(value["model"]) if value.get("model") else None,
            artifact_root=artifact_root,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "url": self.url,
            "cwd": self.cwd,
            "enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "supervisor_capacity": self.capacity,
            "orchestration_mode": self.orchestration_mode,
            "sidecar_authenticated": (
                self.sidecar_token_env is not None
                or self.sidecar_token_file is not None
            ),
            "authenticated": (
                self.token_env is not None or self.token_file is not None
            ),
            "allow_write": self.allow_write,
            "permission_profiles": list(
                (
                    PERMISSION_PROFILES
                    if self.allow_full_access
                    else PERMISSION_PROFILES[:-1]
                )
                if self.allow_write
                else ("read_only",)
            ),
            "allow_cwd_override": self.allow_cwd_override,
            "model": self.model,
            "artifact_transfer_enabled": self.artifact_root is not None,
        }


@dataclass
class Job:
    job_id: str
    agent_id: str
    prompt: str
    requested_thread_id: str | None
    cwd: str
    write: bool
    permission_profile: str | None
    model: str | None
    timeout_sec: int
    output_schema: dict[str, Any] | None = None
    sandbox_mode: Literal[
        "read-only", "workspace-write", "danger-full-access"
    ] = "read-only"
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
    event_callback: Callable[
        [dict[str, Any], str], None | Awaitable[None]
    ] | None = field(
        default=None, repr=False
    )
    notification_subscribers: set[asyncio.Queue[dict[str, Any]]] = field(
        default_factory=set, repr=False
    )
    managed_attempt_dir: str | None = field(default=None, repr=False)
    managed_output_dir: str | None = field(default=None, repr=False)
    staged_artifacts: list[dict[str, Any]] = field(default_factory=list, repr=False)
    captured_files: list[dict[str, Any]] = field(default_factory=list, repr=False)
    artifact_contract: bool = field(default=False, repr=False)

    def record_event(self, method: str, received_at: str) -> None:
        self.events_seen += 1
        self.last_event_method = method
        self.last_event_at = received_at

    async def record_notification(self, message: dict[str, Any], received_at: str) -> None:
        for subscriber in tuple(self.notification_subscribers):
            await subscriber.put(message)
        if self.event_callback is None:
            return
        try:
            result = self.event_callback(message, received_at)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # 监控落库失败不能中断 Codex 的协议读取循环。
            LOGGER.exception("记录 App Server 监控事件失败。")

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
            "permission_profile": self.permission_profile,
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
        on_message: Callable[
            [dict[str, Any], str], None | Awaitable[None]
        ] | None = None,
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
                max_size=64 * 1024 * 1024,
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
                        result = self._on_message(message, received_at)
                        if inspect.isawaitable(result):
                            await result
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
        max_retained_jobs: int = 1000,
        serialize_agent_jobs: bool = True,
    ) -> None:
        if max_retained_jobs < 1:
            raise ValueError("max_retained_jobs 必须大于 0。")
        self.config_path = config_path
        self.jobs: dict[str, Job] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._client_factory = client_factory
        self._max_retained_jobs = max_retained_jobs
        self._serialize_agent_jobs = serialize_agent_jobs
        self._agents_cache: dict[str, AgentConfig] | None = None
        self._agents_cache_signature: tuple[int, int] | None = None

    def load_agents(self) -> dict[str, AgentConfig]:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"找不到执行机配置：{self.config_path}。请复制 config/agents.example.json 为 config/agents.json。"
            )
        stat = self.config_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if (
            self._agents_cache is not None
            and self._agents_cache_signature == signature
        ):
            return dict(self._agents_cache)
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
        self._agents_cache = agents
        self._agents_cache_signature = signature
        return dict(agents)

    def list_agents(self) -> list[dict[str, Any]]:
        return [agent.public_dict() for agent in self.load_agents().values()]

    def get_agent(self, agent_id: str) -> AgentConfig:
        """读取单个执行机的完整私有配置，不对外序列化。"""
        agents = self.load_agents()
        if agent_id not in agents:
            raise ValueError(f"未知执行机：{agent_id}")
        return agents[agent_id]

    async def probe_agent(self, agent_id: str, *, timeout_sec: float = 2.5) -> None:
        """连接并初始化指定 app-server，不创建 thread 或 turn。"""
        agents = self.load_agents()
        if agent_id not in agents:
            raise ValueError(f"未知执行机：{agent_id}")
        agent = agents[agent_id]
        if not agent.enabled:
            raise PermissionError(f"执行机 {agent_id} 已停用。")
        token = self._resolve_agent_token(agent)
        client = self._client_factory(
            agent.url,
            token=token,
            request_timeout_sec=timeout_sec,
        )
        async with client:
            return

    @staticmethod
    def _resolve_agent_token(agent: AgentConfig) -> str | None:
        return Orchestrator._resolve_token_source(
            agent.agent_id,
            "执行机",
            agent.token_env,
            agent.token_file,
        )

    @staticmethod
    def resolve_sidecar_token(agent: AgentConfig) -> str:
        """解析中央网关用于认证指定远程 Sidecar 的独立令牌。"""
        token = Orchestrator._resolve_token_source(
            agent.agent_id,
            "Sidecar",
            agent.sidecar_token_env,
            agent.sidecar_token_file,
        )
        if token is None:
            raise RuntimeError(f"主监督 {agent.agent_id} 未配置 Sidecar 令牌。")
        return token

    @staticmethod
    def _resolve_token_source(
        agent_id: str,
        label: str,
        token_env: str | None,
        token_file: str | None,
    ) -> str | None:
        if token_env:
            token = os.getenv(token_env)
            if not token:
                raise RuntimeError(f"环境变量 {token_env} 未设置。")
            token = token.strip()
            if (
                not token
                or len(token.encode("utf-8")) > MAX_TOKEN_FILE_BYTES
                or "\r" in token
                or "\n" in token
                or "\0" in token
            ):
                raise RuntimeError(f"环境变量 {token_env} 必须包含一个有效的单行令牌。")
            return token
        if not token_file:
            return None

        token_path = Path(token_file)
        try:
            if not token_path.is_file():
                raise RuntimeError(
                    f"{label} {agent_id} 的令牌文件不存在或不是普通文件。"
                )
            if token_path.stat().st_size > MAX_TOKEN_FILE_BYTES:
                raise RuntimeError(
                    f"{label} {agent_id} 的令牌文件不能超过 {MAX_TOKEN_FILE_BYTES} 字节。"
                )
            content = token_path.read_bytes()
        except RuntimeError:
            raise
        except OSError as error:
            raise RuntimeError(
                f"无法读取{label} {agent_id} 的令牌文件。"
            ) from error
        if len(content) > MAX_TOKEN_FILE_BYTES:
            raise RuntimeError(
                f"{label} {agent_id} 的令牌文件不能超过 {MAX_TOKEN_FILE_BYTES} 字节。"
            )
        try:
            token = content.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError(
                f"{label} {agent_id} 的令牌文件必须使用 UTF-8 编码。"
            ) from error
        if not token:
            raise RuntimeError(f"{label} {agent_id} 的令牌文件为空。")
        if "\r" in token or "\n" in token or "\0" in token:
            raise RuntimeError(
                f"{label} {agent_id} 的令牌文件必须只包含一行令牌。"
            )
        return token

    async def dispatch(
        self,
        *,
        agent_id: str,
        prompt: str,
        thread_id: str | None,
        cwd: str | None,
        write: bool,
        permission_profile: str | None = None,
        model: str | None,
        timeout_sec: int,
        approval_policy: Literal["never", "on-request", "untrusted"] = "never",
        approvals_reviewer: Literal["user", "auto_review"] | None = None,
        output_schema: dict[str, Any] | None = None,
        event_callback: Callable[
            [dict[str, Any], str], None | Awaitable[None]
        ] | None = None,
        artifact_handoff: dict[str, Any] | None = None,
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
        if not agent.enabled:
            raise PermissionError(f"执行机 {agent_id} 已停用。")

        sandbox_mode: Literal[
            "read-only", "workspace-write", "danger-full-access"
        ] = ("workspace-write" if write else "read-only")
        if permission_profile is not None:
            normalized_profile = permission_profile.strip().lower()
            (
                profile_write,
                sandbox_mode,
                approval_policy,
                approvals_reviewer,
            ) = permission_settings(normalized_profile)
            if write != profile_write:
                raise ValueError("permission_profile 与 write 字段矛盾。")
            permission_profile = normalized_profile

        selected_cwd = agent.cwd
        if cwd is not None:
            if not agent.allow_cwd_override:
                raise PermissionError(f"{agent_id} 不允许覆盖 cwd。")
            if not is_absolute_remote_path(cwd):
                raise ValueError("cwd 必须是执行机上的绝对路径。")
            selected_cwd = cwd
        if write and not agent.allow_write:
            raise PermissionError(f"{agent_id} 未启用写权限。")
        if (
            permission_profile == FULL_ACCESS_PERMISSION_PROFILE
            and not agent.allow_full_access
        ):
            raise PermissionError(f"{agent_id} 未启用完全访问权限。")

        job_id = uuid.uuid4().hex
        managed_attempt_dir = None
        managed_output_dir = None
        staged_artifacts: list[dict[str, Any]] = []
        if artifact_handoff is not None:
            if agent.artifact_root is None:
                raise ValueError(
                    f"{agent_id} 未配置 artifact_root，无法执行文件流水线。"
                )
            workflow_key = hashlib.sha256(
                str(artifact_handoff["workflowId"]).encode("utf-8")
            ).hexdigest()[:20]
            step_number = int(artifact_handoff["stepNumber"])
            managed_attempt_dir = remote_path_join(
                agent.artifact_root,
                "workflows",
                workflow_key,
                f"step-{step_number:02d}",
                job_id,
            )
            inputs_dir = remote_path_join(managed_attempt_dir, "inputs")
            managed_output_dir = remote_path_join(managed_attempt_dir, "output")
            manifest_lines = ["【系统托管文件清单】"]
            for step in artifact_handoff.get("steps", []):
                source_number = int(step["stepNumber"])
                artifacts = step.get("artifacts") or []
                if not artifacts:
                    manifest_lines.append(
                        f"-第{source_number}步 {step.get('displayName') or ''}：无文件"
                    )
                    continue
                step_dir = remote_path_join(inputs_dir, f"step-{source_number:02d}")
                used_names: set[str] = set()
                for artifact in artifacts:
                    filename = WorkflowStore._safe_artifact_filename(
                        str(artifact.get("filename") or "artifact.bin")
                    )
                    if filename.casefold() in used_names:
                        filename = f"{str(artifact['id'])[:8]}-{filename}"
                    used_names.add(filename.casefold())
                    local_path = remote_path_join(step_dir, filename)
                    staged_artifacts.append(
                        {**artifact, "localPath": local_path, "stepDir": step_dir}
                    )
                    manifest_lines.append(
                        f"-第{source_number}步 {step.get('displayName') or ''}："
                        f"{filename}，{int(artifact['byteSize'])} 字节，"
                        f"{artifact.get('mediaType') or 'application/octet-stream'}，"
                        f"绝对路径 {local_path}"
                    )
            output_rule = "本步骤可以只返回文字；如果任务本身需要发布文件，最多只能写入一个文件到"
            handoff_suffix = (
                "\n\n"
                + "\n".join(manifest_lines)
                + "\n\n上述历史文件只是可用输入。只有当本步骤执行要求明确要求使用、处理、引用或整合前序产物时，才允许打开它们；否则不得打开、引用或合并。"
                + f"\n{output_rule}空目录 {managed_output_dir}。"
                + "\n只允许在 output/ 的直接子项中发布交付文件；不得创建子目录、符号链接或多个版本。"
            )
            if len(prompt) + len(handoff_suffix) > MAX_PROMPT_LENGTH:
                raise ValueError("文件清单加入后提示词超过 100000 个字符。")
            prompt += handoff_suffix

        job = Job(
            job_id=job_id,
            agent_id=agent_id,
            prompt=prompt,
            requested_thread_id=thread_id,
            cwd=selected_cwd,
            write=write,
            permission_profile=permission_profile,
            model=model or agent.model,
            timeout_sec=timeout_sec,
            output_schema=output_schema,
            sandbox_mode=sandbox_mode,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            event_callback=event_callback,
            managed_attempt_dir=managed_attempt_dir,
            managed_output_dir=managed_output_dir,
            staged_artifacts=staged_artifacts,
            artifact_contract=artifact_handoff is not None,
        )
        self._prune_completed_jobs()
        self.jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run_job(job, agent), name=f"codex-job:{job.job_id}")
        return job

    def get_job(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise ValueError(f"找不到任务：{job_id}") from None

    def _prune_completed_jobs(self) -> None:
        overflow = len(self.jobs) - self._max_retained_jobs + 1
        if overflow <= 0:
            return
        for job_id, job in list(self.jobs.items()):
            if overflow <= 0:
                break
            if job.completed.is_set():
                self.jobs.pop(job_id, None)
                overflow -= 1

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
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
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

    async def interrupt_turn(
        self,
        *,
        agent_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        """通过持久化标识中止不一定由当前进程持有的活动 turn。"""
        agents = self.load_agents()
        if agent_id not in agents:
            raise ValueError(f"未知执行机 {agent_id}，可用值：{', '.join(agents)}")
        if not thread_id.strip() or not turn_id.strip():
            raise ValueError("thread_id 和 turn_id 不能为空。")
        agent = agents[agent_id]
        token = self._resolve_agent_token(agent)
        client = self._client_factory(agent.url, token=token)
        try:
            await client.open()
            await client.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout_sec=INTERRUPT_TIMEOUT_SEC,
            )
        finally:
            await client.close()

    async def cleanup_managed_artifacts(self, job: Job) -> None:
        """尽力删除已落库的单次托管目录。"""
        if not job.managed_attempt_dir:
            return
        agent = self.load_agents().get(job.agent_id)
        if agent is None:
            raise ValueError(f"未知执行机 {job.agent_id}。")
        if not agent.artifact_root:
            raise ValueError(f"{job.agent_id} 未配置 artifact_root。")
        root = Path(agent.artifact_root).resolve()
        attempt = Path(job.managed_attempt_dir).resolve()
        if attempt == root or root not in attempt.parents:
            raise ValueError("托管临时目录超出 artifact_root，拒绝清理。")
        if attempt.exists():
            await asyncio.to_thread(shutil.rmtree, attempt)
        job.managed_attempt_dir = None

    async def _run_job(self, job: Job, agent: AgentConfig) -> None:
        stage = "queued"
        try:
            async with AsyncExitStack() as stack:
                if self._serialize_agent_jobs:
                    lock = self._agent_locks.setdefault(agent.agent_id, asyncio.Lock())
                    await stack.enter_async_context(lock)
                job.status = "running"
                job.started_at = utc_now()
                stage = "configuration"
                token = self._resolve_agent_token(agent)

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

                        if job.permission_profile is not None:
                            stage = "permission/check"
                            await self._check_config_requirements(client, job, deadline)

                        if job.artifact_contract:
                            stage = "artifact/stage"
                            await self._stage_artifacts(job)

                        thread_params: dict[str, Any] = {
                            "cwd": job.cwd,
                            "approvalPolicy": job.approval_policy,
                            "sandbox": job.sandbox_mode,
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
                        if job.artifact_contract and job.managed_output_dir:
                            if job.sandbox_mode == "danger-full-access":
                                turn_params["sandboxPolicy"] = {
                                    "type": "dangerFullAccess"
                                }
                            else:
                                writable_roots = [job.managed_output_dir]
                                if job.write:
                                    writable_roots.insert(0, job.cwd)
                                turn_params["sandboxPolicy"] = {
                                    "type": "workspaceWrite",
                                    "writableRoots": writable_roots,
                                    "networkAccess": False,
                                }
                        if job.approvals_reviewer:
                            turn_params["approvalsReviewer"] = job.approvals_reviewer
                        if job.output_schema is not None:
                            turn_params["outputSchema"] = job.output_schema
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
                        if job.status == "completed" and job.artifact_contract:
                            stage = "artifact/capture"
                            await self._capture_artifacts(job)
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
            job.task = None

    async def _stage_artifacts(self, job: Job) -> None:
        await asyncio.to_thread(self._stage_artifacts_sync, job)

    @staticmethod
    def _stage_artifacts_sync(job: Job) -> None:
        assert job.managed_attempt_dir is not None
        assert job.managed_output_dir is not None
        attempt_dir = Path(job.managed_attempt_dir)
        inputs_dir = attempt_dir / "inputs"
        output_dir = Path(job.managed_output_dir)
        try:
            attempt_dir.mkdir(parents=True, exist_ok=False)
            inputs_dir.mkdir()
            output_dir.mkdir()
            for artifact in job.staged_artifacts:
                step_dir = Path(str(artifact["stepDir"]))
                step_dir.mkdir(parents=True, exist_ok=True)
                local_path = Path(str(artifact["localPath"]))
                content = artifact["content"]
                if not isinstance(content, bytes):
                    raise RuntimeError("前序步骤文件内容无效。")
                local_path.write_bytes(content)
        except FileExistsError as error:
            raise RuntimeError("本步骤的托管临时目录已存在，拒绝覆盖。") from error
        except OSError as error:
            raise RuntimeError("无法在 artifact_root 中准备本步骤文件。") from error

    async def _capture_artifacts(self, job: Job) -> None:
        await asyncio.to_thread(self._capture_artifacts_sync, job)

    @staticmethod
    def _capture_artifacts_sync(job: Job) -> None:
        assert job.managed_output_dir is not None
        output_dir = Path(job.managed_output_dir)
        try:
            entries = sorted(output_dir.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            raise RuntimeError("无法读取本步骤的托管输出目录。") from error
        if len(entries) > 1:
            raise RuntimeError("本步骤发布了多个不同文件，要求最多一个。")
        if not entries:
            return
        entry = entries[0]
        filename = entry.name
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
        ):
            raise RuntimeError("本步骤的输出文件名无效。")
        if entry.is_symlink():
            raise RuntimeError("本步骤输出不允许符号链接。")
        try:
            if not entry.is_file():
                raise RuntimeError("本步骤输出只允许包含直接文件，不允许目录。")
            size = entry.stat().st_size
            if size > ARTIFACT_LIMIT:
                raise RuntimeError("本步骤输出文件超过 20 MB。")
            content = entry.read_bytes()
        except RuntimeError:
            raise
        except OSError as error:
            raise RuntimeError("无法读取本步骤输出文件。") from error
        if not content:
            raise RuntimeError("本步骤输出文件为空。")
        if len(content) > ARTIFACT_LIMIT:
            raise RuntimeError("本步骤输出文件超过 20 MB。")
        digest = hashlib.sha256(content).hexdigest()
        job.captured_files = [
            {
                "sourceItemId": f"output:{digest}",
                "filename": filename,
                "content": content,
            }
        ]

    @staticmethod
    async def _check_config_requirements(
        client: AppServerClient,
        job: Job,
        deadline: float,
    ) -> None:
        """在启动步骤前尊重执行机的托管审批与沙箱限制。"""
        try:
            result = await Orchestrator._request_with_deadline(
                client,
                "configRequirements/read",
                {},
                deadline,
                job.timeout_sec,
            )
        except AppServerRpcError as error:
            if error.code == -32601:
                return
            raise
        if not isinstance(result, dict):
            return
        requirements = result.get("requirements")
        if not isinstance(requirements, dict):
            return

        def normalized(values: Any) -> set[str] | None:
            if not isinstance(values, list):
                return None
            return {
                "".join(
                    character
                    for character in str(value).lower()
                    if character.isalnum()
                )
                for value in values
            }

        allowed_policies = normalized(requirements.get("allowedApprovalPolicies"))
        required_policy = "".join(
            character
            for character in job.approval_policy.lower()
            if character.isalnum()
        )
        if allowed_policies is not None and required_policy not in allowed_policies:
            raise PermissionError(
                f"执行机管理策略不允许权限档位 {job.permission_profile or '当前任务'} "
                f"使用审批策略 {job.approval_policy}。"
            )

        allowed_sandboxes = normalized(requirements.get("allowedSandboxModes"))
        required_sandbox = "".join(
            character
            for character in job.sandbox_mode.lower()
            if character.isalnum()
        )
        if allowed_sandboxes is not None and required_sandbox not in allowed_sandboxes:
            raise PermissionError(
                f"执行机管理策略不允许权限档位 {job.permission_profile or '当前任务'} "
                f"使用沙箱 {job.sandbox_mode}。"
            )

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
        final_answer_received_at: float | None = None
        reconciliation_failures = 0
        last_reconciliation_error: Exception | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                details = None
                if last_reconciliation_error is not None:
                    details = {
                        "completion_reconciliation_error": str(
                            last_reconciliation_error
                        )
                    }
                raise JobTotalTimeout(job.timeout_sec, details)

            notification_timeout = remaining
            if final_answer_received_at is not None:
                grace_remaining = FINAL_ANSWER_COMPLETION_GRACE_SEC - (
                    time.monotonic() - final_answer_received_at
                )
                if grace_remaining <= 0:
                    try:
                        if await self._reconcile_turn(
                            job, client, deadline, agent_messages
                        ):
                            return
                    except (AppServerRpcError, AppServerRpcTimeout) as error:
                        reconciliation_failures += 1
                        last_reconciliation_error = error
                        if (
                            reconciliation_failures
                            >= TURN_RECONCILIATION_FAILURE_LIMIT
                        ):
                            interrupt = await self._interrupt_after_timeout(job, client)
                            job.status = "failed"
                            job.error = "已收到最终回答，但无法确认 Codex 执行是否结束。"
                            job.error_kind = "completion_unconfirmed"
                            job.error_stage = "turn/status"
                            job.error_details = {
                                "attempts": reconciliation_failures,
                                "query_error": str(error),
                                "interrupt": interrupt,
                            }
                            return
                    else:
                        reconciliation_failures = 0
                        last_reconciliation_error = None
                    final_answer_received_at = time.monotonic()
                    continue
                notification_timeout = min(notification_timeout, grace_remaining)

            try:
                event = await client.next_notification(notification_timeout)
            except TimeoutError:
                if final_answer_received_at is not None:
                    final_answer_received_at = (
                        time.monotonic() - FINAL_ANSWER_COMPLETION_GRACE_SEC
                    )
                    continue
                raise JobTotalTimeout(job.timeout_sec) from None
            method = event.get("method")
            params = event.get("params") or {}
            event_turn_id = params.get("turnId")
            if event_turn_id and event_turn_id != job.turn_id:
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    agent_messages.append((item.get("phase"), str(item["text"])))
                    if (
                        item.get("phase") == "final_answer"
                        and final_answer_received_at is None
                    ):
                        final_answer_received_at = time.monotonic()
            elif method == "turn/diff/updated":
                job.diff = params.get("diff")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                completed_turn_id = turn.get("id")
                if completed_turn_id and completed_turn_id != job.turn_id:
                    continue
                self._apply_terminal_turn(
                    job, turn, agent_messages, source="turn/completed"
                )
                return

    async def _reconcile_turn(
        self,
        job: Job,
        client: AppServerClient,
        deadline: float,
        agent_messages: list[tuple[str | None, str]],
    ) -> bool:
        turn, source = await self._read_turn(job, client, deadline)
        if str(turn.get("status", "")) == "inProgress":
            return False
        self._apply_terminal_turn(job, turn, agent_messages, source=source)
        return True

    async def _read_turn(
        self,
        job: Job,
        client: AppServerClient,
        deadline: float,
    ) -> tuple[dict[str, Any], str]:
        if not job.thread_id or not job.turn_id:
            raise AppServerRpcError("缺少 thread 或 turn 标识，无法确认执行终态。")

        list_error: AppServerRpcError | None = None
        try:
            result = await self._request_with_deadline(
                client,
                "thread/turns/list",
                {
                    "threadId": job.thread_id,
                    "limit": 20,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
                deadline,
                job.timeout_sec,
            )
            turn = self._find_turn(
                result.get("data") if isinstance(result, dict) else None,
                job.turn_id,
            )
            if turn is not None:
                return turn, "thread/turns/list"
        except AppServerRpcError as error:
            list_error = error

        try:
            result = await self._request_with_deadline(
                client,
                "thread/read",
                {"threadId": job.thread_id, "includeTurns": True},
                deadline,
                job.timeout_sec,
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            turns = thread.get("turns") if isinstance(thread, dict) else None
            turn = self._find_turn(turns, job.turn_id)
            if turn is not None:
                return turn, "thread/read"
        except AppServerRpcError as error:
            if list_error is not None:
                raise AppServerRpcError(
                    "无法通过 thread/turns/list 或 thread/read 查询执行终态："
                    f"{list_error}；{error}"
                ) from error
            raise

        raise AppServerRpcError(f"无法在 thread 中找到 turn：{job.turn_id}")

    @staticmethod
    def _find_turn(values: Any, turn_id: str) -> dict[str, Any] | None:
        if not isinstance(values, list):
            return None
        for value in values:
            if isinstance(value, dict) and value.get("id") == turn_id:
                return value
        return None

    @staticmethod
    def _apply_terminal_turn(
        job: Job,
        turn: dict[str, Any],
        agent_messages: list[tuple[str | None, str]],
        *,
        source: str,
    ) -> None:
        final_messages = [
            text for phase, text in agent_messages if phase == "final_answer"
        ]
        if not final_messages:
            final_messages = [text for _, text in agent_messages]
        job.response = final_messages[-1] if final_messages else None

        turn_status = str(turn.get("status") or "unknown")
        if turn_status == "completed":
            job.status = "completed"
        elif turn_status == "interrupted":
            job.status = "interrupted"
        else:
            job.status = "failed"
            error = turn.get("error") or {}
            if isinstance(error, dict):
                job.error = str(
                    error.get("message") or f"turn 状态为 {turn_status}。"
                )
            else:
                job.error = str(error or f"turn 状态为 {turn_status}。")
            job.error_kind = "turn_failed"
            job.error_stage = source
            job.error_details = {
                "turn_status": turn_status,
                "turn_error": error,
            }

    @staticmethod
    def _extract_id(result: Any, entity: str) -> str:
        if not isinstance(result, dict):
            raise AppServerRpcError(f"{entity} 响应格式错误。")
        value = result.get(entity)
        if not isinstance(value, dict) or not value.get("id"):
            raise AppServerRpcError(f"{entity} 响应缺少 id。")
        return str(value["id"])


orchestrator = Orchestrator(CONFIG_PATH)
_workflow_store: WorkflowStore | InternalApiClient | None = None
_workflow_event_batcher: AsyncEventBatcher | None = None
_workflow_monitors: set[asyncio.Task[None]] = set()


def configure_workflow_runtime(
    runtime: WorkflowStore | InternalApiClient,
) -> None:
    """为常驻 Sidecar 注入远程运行时；stdio 兼容模式继续按需打开 SQLite。"""
    global _workflow_store, _workflow_event_batcher
    _workflow_store = runtime
    _workflow_event_batcher = None


def get_workflow_store() -> WorkflowStore | InternalApiClient:
    global _workflow_store
    if _workflow_store is None:
        _workflow_store = WorkflowStore(WORKFLOW_DB_PATH)
    return _workflow_store


def get_workflow_event_batcher() -> AsyncEventBatcher:
    global _workflow_event_batcher
    store = get_workflow_store()
    if _workflow_event_batcher is None or _workflow_event_batcher.store is not store:
        _workflow_event_batcher = AsyncEventBatcher(store)
    return _workflow_event_batcher


async def flush_workflow_events() -> None:
    try:
        await get_workflow_event_batcher().flush()
    except Exception:
        LOGGER.exception("刷新 App Server 监控事件失败。")


def _workflow_node_snapshot(workflow_id: str, node_id: str) -> dict[str, Any]:
    return get_workflow_store().get_node(workflow_id, node_id)


def _sync_workflow_job(workflow_id: str, node_id: str, job: Job) -> dict[str, Any]:
    store = get_workflow_store()
    snapshot = job.snapshot()
    current_node = store.get_node(workflow_id, node_id)
    if current_node.get("jobId") not in {None, job.job_id}:
        return snapshot
    if job.status == "completed" and job.artifact_contract:
        try:
            for captured in job.captured_files:
                store.save_artifact_bytes(
                    workflow_id,
                    node_id,
                    str(captured["sourceItemId"]),
                    str(captured["filename"]),
                    captured["content"],
                )
        except Exception as error:
            snapshot["status"] = "failed"
            snapshot["error"] = str(error)
            snapshot["finished_at"] = snapshot.get("finished_at") or utc_now()
    store.sync_node_job(workflow_id, node_id, snapshot)
    return snapshot


async def _monitor_workflow_node(workflow_id: str, node_id: str, job: Job) -> None:
    await job.completed.wait()
    await flush_workflow_events()
    _sync_workflow_job(workflow_id, node_id, job)
    if job.artifact_contract and job.managed_attempt_dir:
        try:
            await orchestrator.cleanup_managed_artifacts(job)
        except Exception as error:
            LOGGER.warning(
                "清理执行机托管目录失败：workflow_id=%s node_id=%s error=%s",
                workflow_id,
                node_id,
                error,
            )
            get_workflow_store().add_event(
                workflow_id,
                node_id=node_id,
                source="worker",
                event_type="artifact.cleanup_failed",
                payload={"message": "执行机托管目录清理失败。"},
            )


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
    while True:
        gate = store.pending_advance_for_node(workflow_id, node_id)
        if gate is None:
            break
        if gate["state"] == "held":
            await asyncio.sleep(0.25)
            continue
        remaining = (
            datetime.fromisoformat(gate["expiresAt"]) - datetime.now(UTC)
        ).total_seconds()
        if remaining <= 0:
            store.release_timed_out_advance(workflow_id, gate["gateId"])
            break
        await asyncio.sleep(min(0.25, remaining))
    node = store.prepare_node_dispatch(workflow_id, node_id)
    if node["alreadyDispatched"]:
        job_id = node.get("jobId")
        if job_id and job_id in orchestrator.jobs:
            return orchestrator.get_job(job_id).snapshot()
        return _workflow_node_snapshot(workflow_id, node_id)

    async def record(message: dict[str, Any], received_at: str) -> None:
        method = str(message.get("method") or "unknown")
        params = message.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        if not isinstance(item, dict):
            item = {}
        if (
            not getattr(store, "supports_artifacts", True)
        ):
            pass
        elif (
            method == "item/completed"
            and item.get("type") == "imageGeneration"
            and item.get("status") == "completed"
            and isinstance(item.get("result"), str)
            and item.get("result")
        ):
            try:
                await asyncio.to_thread(
                    store.save_image_artifact,
                    workflow_id,
                    node_id,
                    str(item.get("id") or ""),
                    str(item["result"]),
                )
            except Exception:
                LOGGER.exception(
                    "保存工作流生成图片失败：workflow_id=%s node_id=%s",
                    workflow_id,
                    node_id,
                )
        await get_workflow_event_batcher().add(
            workflow_id,
            node_id=node_id,
            source="worker",
            event_type=f"appserver.{method}",
            payload={"receivedAt": received_at, "message": message},
        )

    try:
        artifact_handoff = None
        if (
            node.get("handoffMode") == "cumulative_files"
            and getattr(store, "supports_artifacts", True)
        ):
            artifact_handoff = {
                "workflowId": workflow_id,
                "nodeId": node_id,
                "stepNumber": node["stepNumber"],
                "steps": store.get_cumulative_artifact_inputs(workflow_id, node_id),
            }
        job = await orchestrator.dispatch(
            agent_id=node["agentId"],
            prompt=node["prompt"],
            thread_id=None,
            cwd=node["cwd"],
            write=node["write"],
            permission_profile=node["permissionProfile"],
            model=node["model"],
            timeout_sec=node["timeoutSec"],
            event_callback=record,
            artifact_handoff=artifact_handoff,
        )
        if artifact_handoff is not None:
            store.update_node_actual_prompt(workflow_id, node_id, job.prompt)
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
        job = orchestrator.get_job(job_id)
        snapshot = _sync_workflow_job(workflow_id, node_id, job)
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
    if job.completed.is_set():
        await flush_workflow_events()
    return _sync_workflow_job(workflow_id, node_id, job)


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
    return _sync_workflow_job(workflow_id, node_id, job)


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
        "retryPolicy": snapshot["retryPolicy"],
        "advanceMode": snapshot["advanceMode"],
        "handoffMode": snapshot["handoffMode"],
        "pendingAdvance": snapshot["pendingAdvance"],
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


def propose_workflow_control(
    workflow_id: str, action_type: str, message_id: str, node_id: str | None = None
) -> dict[str, Any]:
    """网关内部兼容入口；不会暴露为主监督可调用的 MCP 工具。"""
    snapshot = get_workflow_store().get_workflow(workflow_id)
    if action_type == "stop":
        if snapshot["status"] in {"completed", "cancelled"}:
            raise ValueError("当前任务已经结束，不能停止。")
        node_id = None
    elif action_type in {"retry", "restart_from", "skip"}:
        node = next((item for item in snapshot["nodes"] if item["id"] == node_id), None)
        if node is None:
            raise ValueError("必须指定存在的步骤。")
        if action_type == "skip" and node["status"] in {"completed", "skipped"}:
            raise ValueError("已完成或已跳过的步骤不能跳过。")
    else:
        raise ValueError("只支持停止任务、重试步骤和跳过步骤。")
    return get_workflow_store().propose_control(
        workflow_id, action_type, node_id, message_id
    )


def cancel_workflow_control(workflow_id: str, message_id: str) -> dict[str, Any]:
    """取消当前等待确认的聊天控制操作。"""
    return get_workflow_store().cancel_pending_control(workflow_id, message_id)


async def execute_workflow_control(
    workflow_id: str, action_id: str, confirmation_message_id: str
) -> dict[str, Any]:
    """执行已经由另一条“确认执行”消息确认的控制操作。"""
    store = get_workflow_store()
    confirmed = store.confirm_control(workflow_id, action_id, confirmation_message_id)
    action = store.start_control_execution(action_id)
    try:
        snapshot = store.get_workflow(workflow_id)
        if action["actionType"] in {"retry", "restart_from"}:
            targets = store.get_nodes_from(workflow_id, str(action["nodeId"]))
        elif action["actionType"] == "stop":
            targets = snapshot["nodes"]
        else:
            targets = [
                next(item for item in snapshot["nodes"] if item["id"] == action["nodeId"])
            ]
        for node in targets:
            job_id = node.get("jobId")
            is_active = node["status"] in {"queued", "running", "cancelling"}
            if is_active and job_id not in orchestrator.jobs:
                if not node.get("threadId") or not node.get("turnId"):
                    raise RuntimeError("当前无法安全中止正在执行的步骤，请稍后重试控制操作。")
                await orchestrator.interrupt_turn(
                    agent_id=node["agentId"],
                    thread_id=node["threadId"],
                    turn_id=node["turnId"],
                )
            if job_id and job_id in orchestrator.jobs:
                job = await orchestrator.cancel(job_id)
                if not job.completed.is_set():
                    job = await orchestrator.wait(job_id, 10)
                if not job.completed.is_set() and action["actionType"] in {
                    "retry", "restart_from", "skip"
                }:
                    raise RuntimeError("步骤尚未完全停止，暂不能重试或跳过，请稍后再试。")
                _sync_workflow_job(workflow_id, node["id"], job)

        if action["actionType"] == "stop":
            result = store.stop_workflow(workflow_id)
        elif action["actionType"] in {"retry", "restart_from"}:
            result = store.restart_from_node(
                workflow_id, str(action["nodeId"]), action_id=action_id
            )
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
