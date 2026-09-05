"""远程 Sidecar 使用的中央工作流内部 API 客户端。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


MAX_TOKEN_FILE_BYTES = 8 * 1024


def resolve_token(
    *, token_env: str | None, token_file: str | None, label: str = "Sidecar"
) -> str:
    """从环境变量或绝对文件读取一行令牌，并允许运行时轮换。"""
    if bool(token_env) == bool(token_file):
        raise ValueError(f"{label} 令牌必须且只能配置环境变量或文件其中一种。")
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
    assert token_file is not None
    path = Path(token_file).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} 令牌文件必须是绝对路径。")
    try:
        if not path.is_file() or path.stat().st_size > MAX_TOKEN_FILE_BYTES:
            raise RuntimeError(f"{label} 令牌文件不存在或超过 8 KiB。")
        content = path.read_bytes()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"无法读取{label}令牌文件。") from error
    try:
        token = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{label}令牌文件必须使用 UTF-8 编码。") from error
    if not token or "\r" in token or "\n" in token or "\0" in token:
        raise RuntimeError(f"{label}令牌文件必须只包含一行非空令牌。")
    return token


class InternalApiClient:
    """把远程 MCP 所需的状态操作映射到中央内部 API。"""

    supports_artifacts = False

    def __init__(
        self,
        gateway_url: str,
        supervisor_id: str,
        *,
        token_env: str | None = None,
        token_file: str | None = None,
        timeout_sec: float = 10.0,
        instance_id: str | None = None,
        started_at: str,
    ) -> None:
        parsed = urlparse(gateway_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("中央内部 API 地址必须是有效的 http:// 或 https:// 地址。")
        if parsed.query or parsed.fragment:
            raise ValueError("中央内部 API 地址不能包含查询参数或片段。")
        if not supervisor_id.strip() or len(supervisor_id.strip()) > 128:
            raise ValueError("Sidecar 主监督 ID 必须是 1 到 128 个字符。")
        if timeout_sec <= 0 or timeout_sec > 60:
            raise ValueError("内部 API 超时时间必须在 0 到 60 秒之间。")
        if bool(token_env) == bool(token_file):
            raise ValueError("中央 API 令牌必须且只能配置环境变量或文件其中一种。")
        self.gateway_url = gateway_url.rstrip("/")
        self.supervisor_id = supervisor_id.strip()
        self.token_env = token_env
        self.token_file = token_file
        self.timeout_sec = timeout_sec
        self.instance_id = instance_id or str(uuid.uuid4())
        self.started_at = started_at
        self.current_lease: dict[str, Any] | None = None
        self._dispatch_ids: dict[tuple[str, str], str] = {}
        self._state_lock = threading.Lock()

    def heartbeat(self) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/internal/v1/sidecars/heartbeat",
            {"instanceId": self.instance_id, "startedAt": self.started_at},
        )
        lease = result.get("lease")
        with self._state_lock:
            self.current_lease = lease if isinstance(lease, dict) else None
        return result

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", self._workflow_path(workflow_id))

    def get_node(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        return self._request("GET", self._node_path(workflow_id, node_id))

    def pending_advance_for_node(
        self, workflow_id: str, node_id: str
    ) -> dict[str, Any] | None:
        result = self._lease_request(
            "GET", workflow_id, self._node_path(workflow_id, node_id) + "/advance"
        )
        advance = result.get("advance")
        return advance if isinstance(advance, dict) else None

    def release_timed_out_advance(
        self, workflow_id: str, gate_id: str
    ) -> bool:
        node = self.get_workflow(workflow_id).get("pendingAdvance")
        node_id = str(node.get("nextNodeId") or "") if isinstance(node, dict) else ""
        if not node_id:
            return False
        result = self._lease_request(
            "POST",
            workflow_id,
            self._node_path(workflow_id, node_id) + "/advance/release",
            {"gateId": gate_id},
        )
        return bool(result.get("released"))

    def prepare_node_dispatch(
        self, workflow_id: str, node_id: str
    ) -> dict[str, Any]:
        key = (workflow_id, node_id)
        with self._state_lock:
            dispatch_id = self._dispatch_ids.setdefault(key, str(uuid.uuid4()))
        result = self._lease_request(
            "POST",
            workflow_id,
            self._node_path(workflow_id, node_id) + "/prepare",
            {"dispatchId": dispatch_id},
        )
        if result.get("alreadyDispatched") and result.get("jobId"):
            with self._state_lock:
                self._dispatch_ids.pop(key, None)
        return result

    def attach_node_job(
        self, workflow_id: str, node_id: str, snapshot: dict[str, Any]
    ) -> None:
        self._update_node(workflow_id, node_id, "attach", snapshot=snapshot)
        with self._state_lock:
            self._dispatch_ids.pop((workflow_id, node_id), None)

    def sync_node_job(
        self, workflow_id: str, node_id: str, snapshot: dict[str, Any]
    ) -> None:
        self._update_node(workflow_id, node_id, "sync", snapshot=snapshot)

    def update_node_actual_prompt(
        self, workflow_id: str, node_id: str, actual_prompt: str
    ) -> None:
        self._update_node(
            workflow_id, node_id, "actual_prompt", actualPrompt=actual_prompt
        )

    def add_events(self, events: list[dict[str, Any]]) -> list[int]:
        if not events:
            return []
        workflow_ids = {str(event["workflow_id"]) for event in events}
        if len(workflow_ids) != 1:
            raise ValueError("一次远程事件批次只能属于一个工作流。")
        workflow_id = next(iter(workflow_ids))
        payload = {
            "events": [
                {
                    "eventId": str(event.get("external_event_id") or uuid.uuid4()),
                    "nodeId": event.get("node_id"),
                    "source": str(event["source"]),
                    "type": str(event["event_type"]),
                    "payload": event["payload"],
                    "createdAt": event.get("created_at"),
                }
                for event in events
            ]
        }
        result = self._lease_request(
            "POST",
            workflow_id,
            self._workflow_path(workflow_id) + "/events:batch",
            payload,
        )
        return [int(value) for value in result.get("sequences", [])]

    def add_event(
        self,
        workflow_id: str,
        *,
        node_id: str | None,
        source: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        return self.add_events(
            [
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "source": source,
                    "event_type": event_type,
                    "payload": payload,
                    "external_event_id": str(uuid.uuid4()),
                }
            ]
        )[0]

    def get_cumulative_artifact_inputs(
        self, workflow_id: str, node_id: str
    ) -> list[dict[str, Any]]:
        raise RuntimeError("远程 Sidecar 不支持 cumulative_files 文件交接。")

    def _update_node(
        self, workflow_id: str, node_id: str, operation: str, **values: Any
    ) -> None:
        self._lease_request(
            "POST",
            workflow_id,
            self._node_path(workflow_id, node_id) + "/state",
            {"operation": operation, **values},
        )

    def _lease_request(
        self,
        method: str,
        workflow_id: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            lease = self.current_lease
        if not isinstance(lease, dict) or lease.get("workflowId") != workflow_id:
            self.heartbeat()
            with self._state_lock:
                lease = self.current_lease
        if not isinstance(lease, dict) or lease.get("workflowId") != workflow_id:
            raise RuntimeError("中央网关没有为当前 Sidecar 分配该工作流租约。")
        return self._request(
            method,
            path,
            payload,
            headers={"X-Workflow-Lease": str(lease.get("leaseToken") or "")},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = resolve_token(
            token_env=self.token_env,
            token_file=self.token_file,
            label="中央 API",
        )
        body = None
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(
            self.gateway_url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                content = response.read()
        except HTTPError as error:
            message = "中央工作流 API 请求失败。"
            try:
                parsed = json.loads(error.read().decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get("error"):
                    message = str(parsed["error"])
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            if error.code == 401:
                raise PermissionError("Sidecar 认证失败。") from error
            if error.code == 403:
                raise PermissionError(message) from error
            if error.code == 404:
                raise LookupError(message) from error
            if error.code == 409:
                with self._state_lock:
                    self.current_lease = None
                raise RuntimeError(message) from error
            raise ValueError(message) from error
        except URLError as error:
            raise RuntimeError("无法连接中央工作流 API。") from error
        if not content:
            return {}
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("中央工作流 API 返回了无效响应。") from error
        if not isinstance(value, dict):
            raise RuntimeError("中央工作流 API 响应必须是 JSON 对象。")
        return value

    @staticmethod
    def _workflow_path(workflow_id: str) -> str:
        return "/internal/v1/workflows/" + quote(workflow_id, safe="")

    @classmethod
    def _node_path(cls, workflow_id: str, node_id: str) -> str:
        return cls._workflow_path(workflow_id) + "/nodes/" + quote(node_id, safe="")
