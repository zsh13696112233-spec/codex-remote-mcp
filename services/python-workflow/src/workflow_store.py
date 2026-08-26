import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


TERMINAL_NODE_STATUSES = {"completed", "failed", "cancelled", "interrupted", "skipped"}
ACTIVE_NODE_STATUSES = {"queued", "running", "cancelling"}
CHAT_PENDING_STATUSES = {"accepted", "processing"}
RESULT_LIMIT = 20_000
DEPENDENCY_RESULTS_LIMIT = 40_000
PROMPT_LIMIT = 100_000
TRUNCATION_NOTICE = "\n\n【内容过长，已在此处省略】"
EVENT_PAYLOAD_LIMIT = 262_144
IMAGE_ARTIFACT_LIMIT = 20_000_000
IMAGE_ARTIFACTS_PER_WORKFLOW_LIMIT = 50
LEGACY_IMAGE_LINK_PATTERN = re.compile(
    r"\[[^\]]*\]\((?P<path>[^)]+\.(?:png|jpe?g|gif|webp))\)", re.IGNORECASE
)
LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class AsyncEventBatcher:
    """在事件循环外批量提交高频监控事件。"""

    def __init__(
        self,
        store: "WorkflowStore",
        *,
        batch_size: int = 64,
        flush_interval: float = 0.05,
        max_pending: int = 4096,
    ) -> None:
        self.store = store
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_pending = max_pending
        self._pending: list[dict[str, Any]] = []
        self._flush_lock = asyncio.Lock()
        self._timer_handle: asyncio.TimerHandle | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    async def add(
        self,
        workflow_id: str,
        *,
        node_id: str | None,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("事件批量写入器已经关闭。")
        if len(self._pending) >= self.max_pending:
            await self.flush()
        self._pending.append(
            {
                "workflow_id": workflow_id,
                "node_id": node_id,
                "source": source,
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at or utc_now(),
            }
        )
        if len(self._pending) >= self.batch_size:
            await self.flush()
        else:
            self._schedule_flush()

    async def flush(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        task = self._flush_task
        if task is not None and task is not asyncio.current_task():
            await asyncio.shield(task)
        await self._flush_batch()

    async def close(self) -> None:
        self._closed = True
        await self.flush()

    def _schedule_flush(self) -> None:
        if (
            self._closed
            or not self._pending
            or self._timer_handle is not None
            or (self._flush_task is not None and not self._flush_task.done())
        ):
            return
        self._timer_handle = asyncio.get_running_loop().call_later(
            self.flush_interval, self._start_scheduled_flush
        )

    def _start_scheduled_flush(self) -> None:
        self._timer_handle = None
        if self._closed or not self._pending:
            return
        task = asyncio.create_task(
            self._run_scheduled_flush(), name="workflow-event-flush"
        )
        self._flush_task = task
        task.add_done_callback(self._log_background_failure)

    async def _run_scheduled_flush(self) -> None:
        current = asyncio.current_task()
        cancelled = False
        try:
            await self._flush_batch()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if self._flush_task is current:
                self._flush_task = None
            if not cancelled:
                self._schedule_flush()

    @staticmethod
    def _log_background_failure(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            LOGGER.error(
                "批量写入工作流事件失败。",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _flush_batch(self) -> None:
        async with self._flush_lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
            try:
                await asyncio.to_thread(self.store.add_events, batch)
            except BaseException:
                self._pending = batch + self._pending
                raise


class WorkflowStore:
    """跨 HTTP 网关和 MCP 子进程共享的 SQLite 工作流状态库。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT,
                    status TEXT NOT NULL,
                    failure_policy TEXT NOT NULL,
                    supervisor_agent_id TEXT NOT NULL,
                    supervisor_job_id TEXT,
                    supervisor_thread_id TEXT,
                    supervisor_turn_id TEXT,
                    supervisor_status TEXT NOT NULL,
                    supervisor_last_message TEXT,
                    assistant_job_id TEXT,
                    assistant_thread_id TEXT,
                    assistant_turn_id TEXT,
                    assistant_status TEXT NOT NULL DEFAULT 'idle',
                    max_retry_count INTEGER NOT NULL DEFAULT 10,
                    used_retry_count INTEGER NOT NULL DEFAULT 0,
                    response TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    spec_json TEXT NOT NULL,
                    spec_zlib BLOB
                );

                CREATE TABLE IF NOT EXISTS workflow_nodes (
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    executor_type TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    display_name TEXT,
                    role_name TEXT,
                    original_prompt TEXT,
                    actual_prompt TEXT,
                    depends_on_json TEXT NOT NULL,
                    cwd TEXT,
                    write_enabled INTEGER NOT NULL,
                    model TEXT,
                    timeout_sec INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    job_id TEXT,
                    thread_id TEXT,
                    turn_id TEXT,
                    response TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workflow_id, node_id),
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    node_id TEXT,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_events_lookup
                    ON workflow_events(workflow_id, sequence);

                CREATE TABLE IF NOT EXISTS workflow_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    source_item_id TEXT,
                    media_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content BLOB NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id, node_id)
                        REFERENCES workflow_nodes(workflow_id, node_id) ON DELETE CASCADE,
                    UNIQUE (workflow_id, node_id, source_item_id),
                    UNIQUE (workflow_id, node_id, sha256)
                );

                CREATE INDEX IF NOT EXISTS workflow_artifacts_lookup
                    ON workflow_artifacts(workflow_id, node_id, created_at);

                CREATE TABLE IF NOT EXISTS workflow_chat_messages (
                    message_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    workflow_status_at_acceptance TEXT,
                    state_version_at_acceptance INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, message_id),
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_chat_queue
                    ON workflow_chat_messages(workflow_id, role, status, created_at);

                CREATE TABLE IF NOT EXISTS workflow_control_actions (
                    action_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    node_id TEXT,
                    status TEXT NOT NULL,
                    proposed_by_message_id TEXT NOT NULL,
                    confirmed_by_message_id TEXT,
                    proposed_state_version INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    retry_ordinal INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_control_pending
                    ON workflow_control_actions(workflow_id, status, created_at);

                CREATE TABLE IF NOT EXISTS workflow_node_attempts (
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    job_id TEXT,
                    thread_id TEXT,
                    turn_id TEXT,
                    response TEXT,
                    error TEXT,
                    actual_prompt TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, node_id, attempt_number),
                    FOREIGN KEY (workflow_id, node_id)
                        REFERENCES workflow_nodes(workflow_id, node_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS workflow_attempt_artifacts (
                    artifact_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    source_item_id TEXT,
                    media_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content BLOB NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, node_id, attempt_number, artifact_id),
                    FOREIGN KEY (workflow_id, node_id)
                        REFERENCES workflow_nodes(workflow_id, node_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workflow_nodes)").fetchall()
            }
            for name, definition in {
                "display_name": "TEXT",
                "role_name": "TEXT",
                "original_prompt": "TEXT",
                "actual_prompt": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE workflow_nodes ADD COLUMN {name} {definition}"
                    )
            workflow_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workflows)").fetchall()
            }
            if "state_version" not in workflow_columns:
                connection.execute(
                    "ALTER TABLE workflows ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0"
                )
            if "spec_zlib" not in workflow_columns:
                connection.execute("ALTER TABLE workflows ADD COLUMN spec_zlib BLOB")
            for name, definition in {
                "assistant_job_id": "TEXT",
                "assistant_thread_id": "TEXT",
                "assistant_turn_id": "TEXT",
                "assistant_status": "TEXT NOT NULL DEFAULT 'idle'",
                "max_retry_count": "INTEGER NOT NULL DEFAULT 10",
                "used_retry_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in workflow_columns:
                    connection.execute(f"ALTER TABLE workflows ADD COLUMN {name} {definition}")
            artifact_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workflow_artifacts)").fetchall()
            }
            if "attempt_number" not in artifact_columns:
                connection.execute(
                    "ALTER TABLE workflow_artifacts ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 0"
                )
            control_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_control_actions)"
                ).fetchall()
            }
            if "retry_ordinal" not in control_columns:
                connection.execute(
                    "ALTER TABLE workflow_control_actions ADD COLUMN retry_ordinal INTEGER"
                )
            connection.execute(
                """
                UPDATE workflow_nodes
                SET original_prompt = COALESCE(original_prompt, prompt)
                WHERE original_prompt IS NULL
                """
            )

    @staticmethod
    def normalize_spec(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("工作流请求必须是 JSON 对象。")
        raw_nodes = value.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("nodes 必须是非空数组。")
        if len(raw_nodes) > 100:
            raise ValueError("单个工作流最多允许 100 个节点。")

        workflow_id = str(value.get("workflowId") or uuid.uuid4().hex).strip()
        if not workflow_id or len(workflow_id) > 128:
            raise ValueError("workflowId 必须是 1 到 128 个字符。")
        supervisor_agent_id = str(
            value.get("supervisorAgentId") or value.get("supervisor_agent_id") or "local"
        ).strip()
        if not supervisor_agent_id:
            raise ValueError("supervisorAgentId 不能为空。")

        nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        for position, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ValueError(f"nodes[{position}] 必须是对象。")
            node_id = str(raw_node.get("id") or raw_node.get("nodeId") or "").strip()
            if not node_id or len(node_id) > 128:
                raise ValueError(f"nodes[{position}].id 必须是 1 到 128 个字符。")
            if node_id in node_ids:
                raise ValueError(f"节点 id 重复：{node_id}")
            node_ids.add(node_id)

            executor = raw_node.get("executor") or {}
            if not isinstance(executor, dict):
                raise ValueError(f"节点 {node_id} 的 executor 必须是对象。")
            executor_type = str(executor.get("type") or "local").strip().lower()
            if executor_type not in {"local", "remote"}:
                raise ValueError(f"节点 {node_id} 的 executor.type 只能是 local 或 remote。")
            agent_id = str(
                raw_node.get("agentId")
                or raw_node.get("agent_id")
                or executor.get("agentId")
                or executor.get("agent_id")
                or ("local" if executor_type == "local" else "")
            ).strip()
            if not agent_id:
                raise ValueError(f"远程节点 {node_id} 必须指定 executor.agentId。")

            prompt = str(raw_node.get("prompt") or "").strip()
            if not prompt:
                raise ValueError(f"节点 {node_id} 的 prompt 不能为空。")
            if len(prompt) > 100_000:
                raise ValueError(f"节点 {node_id} 的 prompt 不能超过 100000 个字符。")

            depends_on = raw_node.get("dependsOn", raw_node.get("depends_on", []))
            if not isinstance(depends_on, list) or not all(
                isinstance(item, str) and item.strip() for item in depends_on
            ):
                raise ValueError(f"节点 {node_id} 的 dependsOn 必须是字符串数组。")
            normalized_dependencies = [item.strip() for item in depends_on]
            if len(set(normalized_dependencies)) != len(normalized_dependencies):
                raise ValueError(f"节点 {node_id} 的 dependsOn 包含重复项。")

            timeout_sec = int(raw_node.get("timeoutSec", raw_node.get("timeout_sec", 1800)))
            if not 10 <= timeout_sec <= 7200:
                raise ValueError(f"节点 {node_id} 的 timeoutSec 必须在 10 到 7200 之间。")

            nodes.append(
                {
                    "id": node_id,
                    "position": position,
                    "agentId": agent_id,
                    "executorType": executor_type,
                    "prompt": prompt,
                    "displayName": str(raw_node.get("displayName") or f"第{position + 1}步").strip(),
                    "roleName": str(raw_node.get("roleName") or "未指定角色").strip(),
                    "dependsOn": normalized_dependencies,
                    "cwd": raw_node.get("cwd"),
                    "write": bool(raw_node.get("write", False)),
                    "model": raw_node.get("model"),
                    "timeoutSec": timeout_sec,
                }
            )

        for node in nodes:
            for dependency in node["dependsOn"]:
                if dependency not in node_ids:
                    raise ValueError(f"节点 {node['id']} 依赖不存在的节点 {dependency}。")
                if dependency == node["id"]:
                    raise ValueError(f"节点 {node['id']} 不能依赖自身。")
        WorkflowStore._validate_acyclic(nodes)

        failure_policy = str(value.get("failurePolicy") or "stop").strip().lower()
        if failure_policy not in {"stop", "continue"}:
            raise ValueError("failurePolicy 只能是 stop 或 continue。")
        supervisor_timeout_sec = int(value.get("supervisorTimeoutSec", 7200))
        if not 10 <= supervisor_timeout_sec <= 7200:
            raise ValueError("supervisorTimeoutSec 必须在 10 到 7200 之间。")
        max_retry_count = int(value.get("maxRetryCount", 10))
        if not 0 <= max_retry_count <= 100:
            raise ValueError("maxRetryCount 必须在 0 到 100 之间。")

        return {
            "workflowId": workflow_id,
            "name": value.get("name"),
            "failurePolicy": failure_policy,
            "supervisorAgentId": supervisor_agent_id,
            "supervisorCwd": value.get("supervisorCwd"),
            "supervisorWrite": bool(value.get("supervisorWrite", False)),
            "supervisorModel": value.get("supervisorModel"),
            "supervisorTimeoutSec": supervisor_timeout_sec,
            "maxRetryCount": max_retry_count,
            "nodes": nodes,
        }

    @staticmethod
    def _validate_acyclic(nodes: list[dict[str, Any]]) -> None:
        dependencies = {node["id"]: set(node["dependsOn"]) for node in nodes}
        ready = [node_id for node_id, deps in dependencies.items() if not deps]
        visited = 0
        while ready:
            completed = ready.pop()
            visited += 1
            for node_id, deps in dependencies.items():
                if completed in deps:
                    deps.remove(completed)
                    if not deps:
                        ready.append(node_id)
        if visited != len(nodes):
            raise ValueError("工作流节点依赖存在环。")

    def create_workflow(self, value: dict[str, Any]) -> dict[str, Any]:
        spec = self.normalize_spec(value)
        timestamp = utc_now()
        encoded_spec = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
        compressed_spec = zlib.compress(encoded_spec.encode("utf-8"), level=6)
        compact_spec = json.dumps(
            {"workflowId": spec["workflowId"], "compressed": True},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, name, status, failure_policy,
                        supervisor_agent_id, supervisor_status, created_at,
                        max_retry_count, spec_json, spec_zlib
                    ) VALUES (?, ?, 'queued', ?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (
                        spec["workflowId"],
                        spec["name"],
                        spec["failurePolicy"],
                        spec["supervisorAgentId"],
                        timestamp,
                        spec["maxRetryCount"],
                        compact_spec,
                        compressed_spec,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"工作流已存在：{spec['workflowId']}") from error
            for node in spec["nodes"]:
                connection.execute(
                    """
                    INSERT INTO workflow_nodes (
                        workflow_id, node_id, position, agent_id, executor_type,
                        prompt, display_name, role_name, original_prompt,
                        depends_on_json, cwd, write_enabled, model,
                        timeout_sec, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        spec["workflowId"],
                        node["id"],
                        node["position"],
                        node["agentId"],
                        node["executorType"],
                        "",
                        node["displayName"],
                        node["roleName"],
                        node["prompt"],
                        json.dumps(node["dependsOn"], ensure_ascii=False),
                        node["cwd"],
                        int(node["write"]),
                        node["model"],
                        node["timeoutSec"],
                        timestamp,
                    ),
                )
            self._add_event_with_connection(
                connection,
                spec["workflowId"],
                None,
                "gateway",
                "workflow.created",
                {"workflowId": spec["workflowId"], "nodeCount": len(spec["nodes"])},
                timestamp,
            )
        return self.get_workflow(spec["workflowId"])

    def get_spec(self, workflow_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json, spec_zlib FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"找不到工作流：{workflow_id}")
        if row["spec_zlib"] is not None:
            return json.loads(zlib.decompress(row["spec_zlib"]).decode("utf-8"))
        return json.loads(row["spec_json"])

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            workflow = connection.execute(
                """
                SELECT workflow_id, name, status, failure_policy,
                       supervisor_agent_id, supervisor_job_id,
                       supervisor_thread_id, supervisor_turn_id,
                       supervisor_status, supervisor_last_message,
                       response, error, created_at, started_at, finished_at,
                       state_version, assistant_job_id, assistant_thread_id,
                       assistant_turn_id, assistant_status,
                       max_retry_count, used_retry_count
                FROM workflows WHERE workflow_id = ?
                """,
                (workflow_id,),
            ).fetchone()
            if workflow is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            node_rows = connection.execute(
                """
                SELECT workflow_id, node_id, position, agent_id, executor_type,
                       display_name, role_name, depends_on_json, status, job_id,
                       thread_id, turn_id, response, error, started_at,
                       finished_at, attempt_count
                FROM workflow_nodes
                WHERE workflow_id = ? ORDER BY position
                """,
                (workflow_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT artifact_id, node_id, media_type, filename, byte_size, created_at
                FROM workflow_artifacts
                WHERE workflow_id = ? ORDER BY created_at, artifact_id
                """,
                (workflow_id,),
            ).fetchall()
            last_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM workflow_events WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()[0]
            pending_chat_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM workflow_chat_messages
                    WHERE workflow_id = ? AND role = 'user'
                      AND status IN ('accepted', 'processing')
                    """,
                    (workflow_id,),
                ).fetchone()[0]
            )
            pending_control_row = connection.execute(
                """
                SELECT action_id, action_type, node_id, status, expires_at
                FROM workflow_control_actions
                WHERE workflow_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id, utc_now()),
            ).fetchone()

        artifacts_by_node: dict[str, list[dict[str, Any]]] = {}
        for row in artifact_rows:
            artifacts_by_node.setdefault(str(row["node_id"]), []).append(
                self._artifact_snapshot(row)
            )
        nodes = [self._node_snapshot(row) for row in node_rows]
        for node in nodes:
            node["artifacts"] = artifacts_by_node.get(str(node["id"]), [])
        current_nodes = [node["id"] for node in nodes if node["status"] in ACTIVE_NODE_STATUSES]
        completed_count = sum(node["status"] == "completed" for node in nodes)
        return {
            "workflowId": workflow["workflow_id"],
            "name": workflow["name"],
            "status": workflow["status"],
            "failurePolicy": workflow["failure_policy"],
            "currentNodes": current_nodes,
            "progress": {"completed": completed_count, "total": len(nodes)},
            "supervisor": {
                "agentId": workflow["supervisor_agent_id"],
                "jobId": workflow["supervisor_job_id"],
                "threadId": workflow["supervisor_thread_id"],
                "turnId": workflow["supervisor_turn_id"],
                "status": workflow["supervisor_status"],
                "lastMessage": workflow["supervisor_last_message"],
            },
            "assistant": {
                "jobId": workflow["assistant_job_id"],
                "threadId": workflow["assistant_thread_id"],
                "turnId": workflow["assistant_turn_id"],
                "status": workflow["assistant_status"],
            },
            "retryPolicy": {
                "maxRetries": int(workflow["max_retry_count"] or 0),
                "usedRetries": int(workflow["used_retry_count"] or 0),
                "remainingRetries": max(
                    0,
                    int(workflow["max_retry_count"] or 0)
                    - int(workflow["used_retry_count"] or 0),
                ),
            },
            "response": workflow["response"],
            "error": workflow["error"],
            "createdAt": workflow["created_at"],
            "startedAt": workflow["started_at"],
            "finishedAt": workflow["finished_at"],
            "stateVersion": int(workflow["state_version"] or 0),
            "lastEventSequence": last_sequence,
            "pendingChatCount": pending_chat_count,
            "pendingControl": self._pending_control_snapshot(pending_control_row),
            "nodes": nodes,
        }

    @staticmethod
    def _image_type(content: bytes) -> tuple[str, str] | None:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "jpg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", "gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp", "webp"
        return None

    @staticmethod
    def _artifact_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["artifact_id"],
            "mediaType": row["media_type"],
            "filename": row["filename"],
            "byteSize": int(row["byte_size"]),
            "createdAt": row["created_at"],
        }

    def save_image_artifact(
        self,
        workflow_id: str,
        node_id: str,
        source_item_id: str,
        encoded_result: str,
    ) -> dict[str, Any]:
        """验证并持久化图片生成结果，返回不包含图片正文的附件元数据。"""
        source_item_id = str(source_item_id or "").strip()
        if not source_item_id or len(source_item_id) > 200:
            raise ValueError("图片来源编号无效。")
        encoded_result = str(encoded_result or "").strip()
        if encoded_result.startswith("data:"):
            marker = encoded_result.find(",")
            if marker < 0 or ";base64" not in encoded_result[:marker].lower():
                raise ValueError("图片数据格式无效。")
            encoded_result = encoded_result[marker + 1 :]
        try:
            content = base64.b64decode(encoded_result, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("图片数据不是有效的 Base64。") from error
        return self.save_image_bytes(
            workflow_id, node_id, source_item_id, content
        )

    def save_image_bytes(
        self,
        workflow_id: str,
        node_id: str,
        source_item_id: str,
        content: bytes,
    ) -> dict[str, Any]:
        """保存经过签名识别的有限大小图片，并按来源和内容保持幂等。"""
        source_item_id = str(source_item_id or "").strip()
        if not source_item_id or len(source_item_id) > 200:
            raise ValueError("图片来源编号无效。")
        if not content:
            raise ValueError("图片内容不能为空。")
        if len(content) > IMAGE_ARTIFACT_LIMIT:
            raise ValueError("图片大小不能超过 20 MB。")
        image_type = self._image_type(content)
        if image_type is None:
            raise ValueError("仅支持 PNG、JPEG、GIF 和 WebP 图片。")
        media_type, extension = image_type
        digest = hashlib.sha256(content).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            node = connection.execute(
                "SELECT attempt_count FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if node is None:
                raise ValueError(f"找不到步骤：{node_id}")
            attempt_number = int(node["attempt_count"] or 0)
            artifact_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "codex-workflow-artifact:"
                    f"{workflow_id}:{node_id}:{attempt_number}:{source_item_id}:{digest}",
                )
            )
            filename = f"generated-image-{artifact_id[:8]}.{extension}"
            existing = connection.execute(
                """
                SELECT artifact_id, node_id, media_type, filename, byte_size, created_at
                FROM workflow_artifacts
                WHERE workflow_id = ? AND node_id = ?
                  AND (source_item_id = ? OR sha256 = ?)
                LIMIT 1
                """,
                (workflow_id, node_id, source_item_id, digest),
            ).fetchone()
            if existing is not None:
                return self._artifact_snapshot(existing)
            count = int(
                connection.execute(
                    "SELECT (SELECT COUNT(*) FROM workflow_artifacts WHERE workflow_id = ?) "
                    "+ (SELECT COUNT(*) FROM workflow_attempt_artifacts WHERE workflow_id = ?)",
                    (workflow_id, workflow_id),
                ).fetchone()[0]
            )
            if count >= IMAGE_ARTIFACTS_PER_WORKFLOW_LIMIT:
                raise ValueError("单个工作流最多保存 50 张图片。")
            connection.execute(
                """
                INSERT INTO workflow_artifacts (
                    artifact_id, workflow_id, node_id, source_item_id,
                    media_type, filename, content, byte_size, sha256,
                    attempt_number, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    workflow_id,
                    node_id,
                    source_item_id,
                    media_type,
                    filename,
                    content,
                    len(content),
                    digest,
                    attempt_number,
                    now,
                ),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                node_id,
                "worker",
                "artifact.created",
                {
                    "artifactId": artifact_id,
                    "mediaType": media_type,
                    "byteSize": len(content),
                },
                now,
            )
            row = connection.execute(
                """
                SELECT artifact_id, node_id, media_type, filename, byte_size, created_at
                FROM workflow_artifacts WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        assert row is not None
        return self._artifact_snapshot(row)

    def get_artifact(self, workflow_id: str, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT artifact_id, node_id, media_type, filename, content,
                       byte_size, created_at
                FROM workflow_artifacts
                WHERE workflow_id = ? AND artifact_id = ?
                """,
                (workflow_id, artifact_id),
            ).fetchone()
        if row is None:
            raise ValueError("找不到工作流图片。")
        result = self._artifact_snapshot(row)
        result["content"] = bytes(row["content"])
        result["nodeId"] = row["node_id"]
        return result

    def import_legacy_generated_images(self, generated_images_root: Path | None = None) -> int:
        """从历史步骤文本中回填仍位于受信生成目录内的图片。"""
        root = generated_images_root
        if root is None:
            codex_home = Path(os.getenv("CODEX_HOME", Path.home() / ".codex"))
            root = codex_home / "generated_images"
        try:
            trusted_root = root.expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError):
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT workflow_id, node_id, response FROM workflow_nodes
                WHERE response IS NOT NULL AND response <> ''
                """
            ).fetchall()
        imported = 0
        for row in rows:
            for match in LEGACY_IMAGE_LINK_PATTERN.finditer(str(row["response"])):
                raw_path = match.group("path").replace(r"\_", "_").strip()
                try:
                    candidate = Path(raw_path).expanduser().resolve(strict=True)
                    if not candidate.is_file() or not candidate.is_relative_to(trusted_root):
                        continue
                    if candidate.stat().st_size > IMAGE_ARTIFACT_LIMIT:
                        continue
                    source_item_id = "legacy:" + hashlib.sha256(
                        str(candidate).encode("utf-8")
                    ).hexdigest()
                    with self._connect() as connection:
                        exists = connection.execute(
                            """
                            SELECT 1 FROM workflow_artifacts
                            WHERE workflow_id = ? AND node_id = ? AND source_item_id = ?
                            """,
                            (row["workflow_id"], row["node_id"], source_item_id),
                        ).fetchone()
                    if exists is not None:
                        continue
                    content = candidate.read_bytes()
                    self.save_image_bytes(
                        str(row["workflow_id"]), str(row["node_id"]), source_item_id, content
                    )
                    imported += 1
                except (OSError, ValueError):
                    continue
        return imported

    def get_workflow_spec(self, workflow_id: str) -> dict[str, Any]:
        return self.get_spec(workflow_id)

    def accept_chat_message(self, workflow_id: str, message_id: str, text: str) -> dict[str, Any]:
        text = text.strip()
        try:
            parsed = uuid.UUID(message_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("messageId 必须是有效的 UUID。") from error
        if str(parsed) != message_id.lower():
            message_id = str(parsed)
        if not text:
            raise ValueError("text 去除首尾空格后不能为空。")
        if len(text) > 4_000:
            raise ValueError("text 不能超过 4000 个字符。")

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workflow_chat_messages WHERE workflow_id = ? AND message_id = ?",
                (workflow_id, message_id),
            ).fetchone()
            if existing is not None:
                if existing["content"] != text:
                    raise RuntimeError("同一 messageId 不能提交不同内容。")
                if existing["status"] == "failed":
                    connection.execute(
                        "UPDATE workflow_chat_messages SET status = 'accepted', error = NULL, updated_at = ? "
                        "WHERE workflow_id = ? AND message_id = ?",
                        (now, workflow_id, message_id),
                    )
                    self._add_event_with_connection(
                        connection, workflow_id, None, "chat", "chat.user.accepted",
                        {"messageId": message_id, "text": text, "retry": True}, now,
                    )
                    existing = connection.execute(
                        "SELECT * FROM workflow_chat_messages WHERE workflow_id = ? AND message_id = ?",
                        (workflow_id, message_id),
                    ).fetchone()
                return self._chat_snapshot(existing)

            workflow = connection.execute(
                "SELECT status, state_version FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise LookupError(f"找不到工作流：{workflow_id}")
            connection.execute(
                """
                INSERT INTO workflow_chat_messages (
                    message_id, workflow_id, role, content, status,
                    workflow_status_at_acceptance, state_version_at_acceptance,
                    created_at, updated_at
                ) VALUES (?, ?, 'user', ?, 'accepted', ?, ?, ?, ?)
                """,
                (
                    message_id, workflow_id, text, workflow["status"],
                    workflow["state_version"], now, now,
                ),
            )
            self._add_event_with_connection(
                connection, workflow_id, None, "chat", "chat.user.accepted",
                {
                    "messageId": message_id,
                    "text": text,
                    "workflowStatusAtAcceptance": workflow["status"],
                    "stateVersionAtAcceptance": workflow["state_version"],
                },
                now,
            )
            row = connection.execute(
                "SELECT * FROM workflow_chat_messages WHERE workflow_id = ? AND message_id = ?",
                (workflow_id, message_id),
            ).fetchone()
        return self._chat_snapshot(row)

    @staticmethod
    def _chat_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "messageId": row["message_id"],
            "workflowId": row["workflow_id"],
            "role": row["role"],
            "text": row["content"],
            "status": row["status"],
            "replyToMessageId": row["reply_to_message_id"],
            "workflowStatusAtAcceptance": row["workflow_status_at_acceptance"],
            "stateVersionAtAcceptance": row["state_version_at_acceptance"],
            "error": row["error"],
            "acceptedAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def claim_next_chat_message(self, workflow_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM workflow_chat_messages
                WHERE workflow_id = ? AND role = 'user' AND status = 'accepted'
                ORDER BY created_at, rowid LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE workflow_chat_messages SET status = 'processing', updated_at = ? "
                "WHERE workflow_id = ? AND message_id = ? AND status = 'accepted'",
                (now, workflow_id, row["message_id"]),
            )
            refreshed = connection.execute(
                "SELECT * FROM workflow_chat_messages WHERE workflow_id = ? AND message_id = ?",
                (workflow_id, row["message_id"]),
            ).fetchone()
        return self._chat_snapshot(refreshed)

    def mark_chat_forwarded(self, workflow_id: str, message_id: str) -> None:
        self.add_event(
            workflow_id, node_id=None, source="chat", event_type="chat.user.forwarded",
            payload={"messageId": message_id},
        )

    def add_chat_delta(
        self, workflow_id: str, message_id: str, assistant_message_id: str, delta: str
    ) -> None:
        if not delta:
            return
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_chat_messages (
                    message_id, workflow_id, role, content, status, reply_to_message_id,
                    created_at, updated_at
                ) VALUES (?, ?, 'assistant', ?, 'processing', ?, ?, ?)
                ON CONFLICT(workflow_id, message_id) DO UPDATE SET
                    content = content || excluded.content, updated_at = excluded.updated_at
                """,
                (assistant_message_id, workflow_id, delta, message_id, now, now),
            )
            self._add_event_with_connection(
                connection, workflow_id, None, "chat", "chat.assistant.delta",
                {"messageId": message_id, "assistantMessageId": assistant_message_id, "delta": delta},
                now,
            )

    def complete_chat_message(
        self, workflow_id: str, message_id: str, assistant_message_id: str, content: str
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_chat_messages (
                    message_id, workflow_id, role, content, status, reply_to_message_id,
                    created_at, updated_at
                ) VALUES (?, ?, 'assistant', ?, 'completed', ?, ?, ?)
                ON CONFLICT(workflow_id, message_id) DO UPDATE SET
                    content = excluded.content, status = 'completed', updated_at = excluded.updated_at
                """,
                (assistant_message_id, workflow_id, content, message_id, now, now),
            )
            connection.execute(
                "UPDATE workflow_chat_messages SET status = 'completed', error = NULL, updated_at = ? "
                "WHERE workflow_id = ? AND message_id = ?",
                (now, workflow_id, message_id),
            )
            self._add_event_with_connection(
                connection, workflow_id, None, "chat", "chat.assistant.completed",
                {"messageId": message_id, "assistantMessageId": assistant_message_id, "text": content},
                now,
            )

    def fail_chat_message(self, workflow_id: str, message_id: str, error: str) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE workflow_chat_messages SET status = 'failed', error = ?, updated_at = ? "
                "WHERE workflow_id = ? AND message_id = ?",
                (error, now, workflow_id, message_id),
            )
            connection.execute(
                "UPDATE workflow_chat_messages SET status = 'failed', error = ?, updated_at = ? "
                "WHERE workflow_id = ? AND role = 'assistant' AND reply_to_message_id = ? "
                "AND status = 'processing'",
                (error, now, workflow_id, message_id),
            )
            self._add_event_with_connection(
                connection, workflow_id, None, "chat", "chat.message.failed",
                {"messageId": message_id, "error": error}, now,
            )

    def pending_chat_count(self, workflow_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM workflow_chat_messages WHERE workflow_id = ? "
                "AND role = 'user' AND status IN ('accepted', 'processing')",
                (workflow_id,),
            ).fetchone()[0])

    def list_chat_workflows(self) -> list[str]:
        with self._connect() as connection:
            return [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT workflow_id FROM workflow_chat_messages "
                "WHERE role = 'user' AND status IN ('accepted', 'processing')"
            ).fetchall()]

    def recover_processing_chat_messages(self) -> None:
        with self._connect() as connection:
            now = utc_now()
            connection.execute(
                "UPDATE workflow_chat_messages SET status = 'accepted', updated_at = ? "
                "WHERE role = 'user' AND status = 'processing'",
                (now,),
            )
            connection.execute(
                "UPDATE workflows SET assistant_status = 'idle' "
                "WHERE assistant_status = 'running'"
            )

    def get_pending_control(self, workflow_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT action_id, action_type, node_id, status, expires_at
                FROM workflow_control_actions
                WHERE workflow_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id, now),
            ).fetchone()
        return self._pending_control_snapshot(row)

    def cancel_pending_control(self, workflow_id: str, message_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_control_actions WHERE workflow_id = ? "
                "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise ValueError("没有等待确认的控制操作。")
            message = connection.execute(
                "SELECT content FROM workflow_chat_messages WHERE workflow_id = ? "
                "AND message_id = ? AND role = 'user'",
                (workflow_id, message_id),
            ).fetchone()
            if message is None or message["content"].strip() != "取消操作":
                raise ValueError("必须单独回复“取消操作”。")
            connection.execute(
                "UPDATE workflow_control_actions SET status = 'cancelled', updated_at = ? "
                "WHERE action_id = ?",
                (now, row["action_id"]),
            )
            self._add_event_with_connection(
                connection, workflow_id, row["node_id"], "chat", "chat.control.cancelled",
                {"messageId": message_id, "actionId": row["action_id"]}, now,
            )
        return {"actionId": row["action_id"], "status": "cancelled"}

    def propose_control(
        self, workflow_id: str, action_type: str, node_id: str | None, message_id: str
    ) -> dict[str, Any]:
        if action_type == "retry":
            action_type = "restart_from"
        if action_type not in {"stop", "restart_from", "skip"}:
            raise ValueError("控制类型只能是 stop、restart_from 或 skip。")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(minutes=10)).isoformat()
        action_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT state_version, max_retry_count, used_retry_count "
                "FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            if action_type == "stop":
                status = connection.execute(
                    "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()["status"]
                if status in {"completed", "cancelled"}:
                    raise ValueError("当前任务已经结束，不能停止。")
            affected_nodes: list[dict[str, Any]] = []
            if action_type in {"restart_from", "skip"}:
                node = connection.execute(
                    "SELECT node_id, position, display_name FROM workflow_nodes "
                    "WHERE workflow_id = ? AND node_id = ?",
                    (workflow_id, node_id),
                ).fetchone()
                if node is None:
                    raise ValueError(f"找不到步骤：{node_id}")
                if action_type == "skip":
                    node_status = connection.execute(
                        "SELECT status FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                        (workflow_id, node_id),
                    ).fetchone()["status"]
                    if node_status in {"completed", "skipped"}:
                        raise ValueError("已完成或已跳过的步骤不能再次跳过。")
                if action_type == "restart_from":
                    if int(workflow["used_retry_count"] or 0) >= int(
                        workflow["max_retry_count"] or 0
                    ):
                        raise ValueError("本任务的重跑次数已经用完。")
                    affected_nodes = [
                        {"id": row["node_id"], "displayName": row["display_name"] or row["node_id"]}
                        for row in connection.execute(
                            "SELECT node_id, display_name FROM workflow_nodes "
                            "WHERE workflow_id = ? AND position >= ? ORDER BY position",
                            (workflow_id, node["position"]),
                        ).fetchall()
                    ]
            old_pending = connection.execute(
                "SELECT action_id, node_id, proposed_by_message_id FROM workflow_control_actions "
                "WHERE workflow_id = ? AND status = 'pending'",
                (workflow_id,),
            ).fetchall()
            connection.execute(
                "UPDATE workflow_control_actions SET status = 'cancelled', updated_at = ? "
                "WHERE workflow_id = ? AND status = 'pending'",
                (now, workflow_id),
            )
            for old in old_pending:
                self._add_event_with_connection(
                    connection, workflow_id, old["node_id"], "chat", "chat.control.cancelled",
                    {"messageId": old["proposed_by_message_id"],
                     "actionId": old["action_id"], "reason": "superseded"}, now,
                )
            connection.execute(
                """
                INSERT INTO workflow_control_actions (
                    action_id, workflow_id, action_type, node_id, status,
                    proposed_by_message_id, proposed_state_version,
                    created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (action_id, workflow_id, action_type, node_id, message_id,
                 workflow["state_version"], now, expires, now),
            )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "chat", "chat.control.proposed",
                {"messageId": message_id, "actionId": action_id,
                 "actionType": action_type, "nodeId": node_id}, now,
            )
        result = {"actionId": action_id, "actionType": action_type, "nodeId": node_id,
                  "expiresAt": expires, "requiredConfirmation": "确认执行"}
        if action_type == "restart_from":
            used = int(workflow["used_retry_count"] or 0)
            maximum = int(workflow["max_retry_count"] or 0)
            result.update({
                "affectedNodes": affected_nodes,
                "retryCost": 1,
                "retryPolicy": {
                    "maxRetries": maximum,
                    "usedRetries": used,
                    "remainingRetries": max(0, maximum - used),
                },
            })
        return result

    def confirm_control(self, workflow_id: str, action_id: str, message_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            action = connection.execute(
                "SELECT * FROM workflow_control_actions WHERE workflow_id = ? AND action_id = ?",
                (workflow_id, action_id),
            ).fetchone()
            if action is None or action["status"] != "pending":
                raise ValueError("没有可确认的控制操作。")
            confirmation = connection.execute(
                "SELECT content, created_at FROM workflow_chat_messages "
                "WHERE workflow_id = ? AND message_id = ? AND role = 'user'",
                (workflow_id, message_id),
            ).fetchone()
            if (
                confirmation is None
                or confirmation["content"].strip() != "确认执行"
                or confirmation["created_at"] <= action["created_at"]
                or message_id == action["proposed_by_message_id"]
            ):
                raise ValueError("必须在另一条新消息中单独回复“确认执行”。")
            if action["expires_at"] <= now:
                connection.execute(
                    "UPDATE workflow_control_actions SET status = 'expired', updated_at = ? WHERE action_id = ?",
                    (now, action_id),
                )
                raise ValueError("控制确认已过期，请重新提出操作。")
            workflow = connection.execute(
                "SELECT state_version FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if int(workflow["state_version"]) != int(action["proposed_state_version"]):
                connection.execute(
                    "UPDATE workflow_control_actions SET status = 'cancelled', error = ?, updated_at = ? "
                    "WHERE action_id = ?",
                    ("任务状态已变化，需要重新确认。", now, action_id),
                )
                self._add_event_with_connection(
                    connection, workflow_id, action["node_id"], "chat",
                    "chat.control.cancelled",
                    {"messageId": message_id, "actionId": action_id,
                     "reason": "state_changed"}, now,
                )
                raise ValueError("任务状态已经变化，请重新提出操作并确认。")
            connection.execute(
                "UPDATE workflow_control_actions SET status = 'confirmed', confirmed_by_message_id = ?, "
                "updated_at = ? WHERE action_id = ?",
                (message_id, now, action_id),
            )
            self._add_event_with_connection(
                connection, workflow_id, action["node_id"], "chat", "chat.control.confirmed",
                {"messageId": message_id, "actionId": action_id}, now,
            )
        return {"actionId": action_id, "actionType": action["action_type"],
                "nodeId": action["node_id"]}

    def start_control_execution(self, action_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_control_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None or row["status"] != "confirmed":
                raise ValueError("控制操作尚未确认或已经处理。")
            connection.execute(
                "UPDATE workflow_control_actions SET status = 'executing', updated_at = ? "
                "WHERE action_id = ?",
                (now, action_id),
            )
        return {"actionId": action_id, "workflowId": row["workflow_id"],
                "actionType": row["action_type"], "nodeId": row["node_id"],
                "confirmedByMessageId": row["confirmed_by_message_id"]}

    def finish_control_execution(
        self, action_id: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_control_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                return
            status = "failed" if error else "completed"
            connection.execute(
                "UPDATE workflow_control_actions SET status = ?, result_json = ?, error = ?, "
                "updated_at = ? WHERE action_id = ?",
                (status, json.dumps(result or {}, ensure_ascii=False), error, now, action_id),
            )
            self._add_event_with_connection(
                connection, row["workflow_id"], row["node_id"], "chat",
                f"chat.control.{status}",
                {"messageId": row["confirmed_by_message_id"], "actionId": action_id,
                 "result": result, "error": error}, now,
            )

    def restart_from_node(
        self, workflow_id: str, node_id: str, *, action_id: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到步骤：{node_id}")
            workflow = connection.execute(
                "SELECT max_retry_count, used_retry_count FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            maximum = int(workflow["max_retry_count"] or 0)
            used = int(workflow["used_retry_count"] or 0)
            if used >= maximum:
                raise ValueError("本任务的重跑次数已经用完。")
            predecessors = connection.execute(
                "SELECT display_name, node_id, status FROM workflow_nodes "
                "WHERE workflow_id = ? AND position < ? ORDER BY position",
                (workflow_id, row["position"]),
            ).fetchall()
            unfinished = [
                item["display_name"] or item["node_id"]
                for item in predecessors
                if item["status"] not in {"completed", "skipped"}
            ]
            if unfinished:
                raise ValueError("选中步骤之前仍有未完成步骤：" + "、".join(unfinished))
            tail = connection.execute(
                "SELECT * FROM workflow_nodes WHERE workflow_id = ? AND position >= ? "
                "ORDER BY position",
                (workflow_id, row["position"]),
            ).fetchall()
            active = [item["display_name"] or item["node_id"] for item in tail
                      if item["status"] in ACTIVE_NODE_STATUSES]
            if active:
                raise RuntimeError("仍有步骤没有安全停止：" + "、".join(active))
            retry_ordinal = used + 1
            for item in tail:
                attempt_number = int(item["attempt_count"] or 0)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO workflow_node_attempts (
                        workflow_id, node_id, attempt_number, status, job_id,
                        thread_id, turn_id, response, error, actual_prompt,
                        started_at, finished_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id, item["node_id"], attempt_number, item["status"],
                        item["job_id"], item["thread_id"], item["turn_id"],
                        item["response"], item["error"], item["actual_prompt"],
                        item["started_at"], item["finished_at"], now,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO workflow_attempt_artifacts (
                        artifact_id, workflow_id, node_id, attempt_number,
                        source_item_id, media_type, filename, content, byte_size,
                        sha256, created_at, archived_at
                    )
                    SELECT artifact_id, workflow_id, node_id, ?, source_item_id,
                           media_type, filename, content, byte_size, sha256,
                           created_at, ?
                    FROM workflow_artifacts
                    WHERE workflow_id = ? AND node_id = ?
                    """,
                    (attempt_number, now, workflow_id, item["node_id"]),
                )
                connection.execute(
                    "DELETE FROM workflow_artifacts WHERE workflow_id = ? AND node_id = ?",
                    (workflow_id, item["node_id"]),
                )
            connection.execute(
                """
                UPDATE workflow_nodes SET status = 'pending', job_id = NULL, thread_id = NULL,
                    turn_id = NULL, response = NULL, error = NULL, started_at = NULL,
                    finished_at = NULL, actual_prompt = NULL,
                    attempt_count = attempt_count + 1
                WHERE workflow_id = ? AND position >= ?
                """,
                (workflow_id, row["position"]),
            )
            connection.execute(
                "UPDATE workflows SET status = 'running', finished_at = NULL, response = NULL, "
                "error = NULL, used_retry_count = used_retry_count + 1, "
                "state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id,),
            )
            if action_id is not None:
                connection.execute(
                    "UPDATE workflow_control_actions SET retry_ordinal = ?, updated_at = ? "
                    "WHERE action_id = ?",
                    (retry_ordinal, now, action_id),
                )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "chat", "node.restart_from_requested",
                {
                    "nodeId": node_id,
                    "affectedNodeIds": [item["node_id"] for item in tail],
                    "retryOrdinal": retry_ordinal,
                }, now,
            )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "gateway", "workflow.retry_budget.updated",
                {
                    "maxRetries": maximum,
                    "usedRetries": retry_ordinal,
                    "remainingRetries": max(0, maximum - retry_ordinal),
                }, now,
            )
        return self.get_workflow(workflow_id)

    def reset_node_for_retry(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        """兼容旧调用；新语义统一为从所选步骤重跑到末尾。"""
        return self.restart_from_node(workflow_id, node_id)

    def skip_node(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到步骤：{node_id}")
            if row["status"] in {"completed", "skipped"}:
                raise ValueError("已完成或已跳过的步骤不能再次跳过。")
            connection.execute(
                "UPDATE workflow_nodes SET status = 'skipped', response = NULL, "
                "error = NULL, finished_at = ? WHERE workflow_id = ? AND node_id = ?",
                (now, workflow_id, node_id),
            )
            connection.execute(
                "UPDATE workflows SET status = 'running', finished_at = NULL, error = NULL, "
                "state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id,),
            )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "chat", "node.skipped",
                {"nodeId": node_id}, now,
            )
        return self.get_workflow(workflow_id)

    def stop_workflow(self, workflow_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            connection.execute(
                "UPDATE workflow_nodes SET status = 'cancelled', error = COALESCE(error, ?), "
                "finished_at = COALESCE(finished_at, ?) WHERE workflow_id = ? "
                "AND status NOT IN ('completed', 'skipped', 'failed', 'cancelled', 'interrupted')",
                ("用户已停止任务。", now, workflow_id),
            )
            connection.execute(
                "UPDATE workflows SET status = 'cancelled', supervisor_status = 'cancelled', "
                "error = ?, finished_at = ?, state_version = state_version + 1 "
                "WHERE workflow_id = ?",
                ("用户已停止任务。", now, workflow_id),
            )
            self._add_event_with_connection(
                connection, workflow_id, None, "chat", "workflow.cancelled",
                {"status": "cancelled", "reason": "chat_control"}, now,
            )
        return self.get_workflow(workflow_id)

    @staticmethod
    def _node_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["node_id"],
            "displayName": row["display_name"] or row["node_id"],
            "roleName": row["role_name"] or "未指定角色",
            "agentId": row["agent_id"],
            "executorType": row["executor_type"],
            "dependsOn": json.loads(row["depends_on_json"]),
            "status": row["status"],
            "jobId": row["job_id"],
            "threadId": row["thread_id"],
            "turnId": row["turn_id"],
            "response": row["response"],
            "error": row["error"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "attemptCount": int(row["attempt_count"] or 0),
        }

    @staticmethod
    def _pending_control_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "actionId": row["action_id"],
            "type": row["action_type"],
            "nodeId": row["node_id"],
            "status": row["status"],
            "expiresAt": row["expires_at"],
        }

    def get_node(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_id, node_id, position, agent_id, executor_type,
                       display_name, role_name, depends_on_json, status, job_id,
                       thread_id, turn_id, response, error, started_at,
                       finished_at, attempt_count
                FROM workflow_nodes
                WHERE workflow_id = ? AND node_id = ?
                """,
                (workflow_id, node_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"找不到节点：{node_id}")
        return self._node_snapshot(row)

    def get_nodes_from(self, workflow_id: str, node_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            target = connection.execute(
                "SELECT position FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if target is None:
                raise ValueError(f"找不到步骤：{node_id}")
            rows = connection.execute(
                """
                SELECT workflow_id, node_id, position, agent_id, executor_type,
                       display_name, role_name, depends_on_json, status, job_id,
                       thread_id, turn_id, response, error, started_at,
                       finished_at, attempt_count
                FROM workflow_nodes
                WHERE workflow_id = ? AND position >= ? ORDER BY position
                """,
                (workflow_id, target["position"]),
            ).fetchall()
        return [self._node_snapshot(row) for row in rows]

    def prepare_node_dispatch(self, workflow_id: str, node_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            if workflow["status"] in {"completed", "failed", "cancelled"}:
                raise ValueError(f"工作流已结束，不能派发节点：{workflow['status']}")
            row = connection.execute(
                """
                SELECT * FROM workflow_nodes
                WHERE workflow_id = ? AND node_id = ?
                """,
                (workflow_id, node_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到节点：{node_id}")
            if row["status"] != "pending":
                return self._node_dispatch_spec(row, already_dispatched=True)

            dependencies = json.loads(row["depends_on_json"])
            dependency_rows: list[sqlite3.Row] = []
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                dependency_rows = connection.execute(
                    f"""
                    SELECT node_id, position, status, response FROM workflow_nodes
                    WHERE workflow_id = ? AND node_id IN ({placeholders})
                    """,
                    (workflow_id, *dependencies),
                ).fetchall()
                statuses = {item["node_id"]: item["status"] for item in dependency_rows}
                incomplete = [
                    item for item in dependencies
                    if statuses.get(item) not in {"completed", "skipped"}
                ]
                if incomplete:
                    raise ValueError(
                        f"节点 {node_id} 的依赖尚未完成：{', '.join(incomplete)}"
                    )

            original_prompt = row["original_prompt"] or row["prompt"]
            actual_prompt = self._build_actual_prompt(original_prompt, dependencies, dependency_rows)

            timestamp = utc_now()
            connection.execute(
                """
                UPDATE workflow_nodes
                SET status = 'queued', started_at = ?, actual_prompt = ?
                WHERE workflow_id = ? AND node_id = ?
                """,
                (timestamp, actual_prompt, workflow_id, node_id),
            )
            connection.execute(
                """
                UPDATE workflows
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    state_version = state_version + 1
                WHERE workflow_id = ?
                """,
                (timestamp, workflow_id),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                node_id,
                "supervisor",
                "node.dispatch_requested",
                {"nodeId": node_id, "agentId": row["agent_id"]},
                timestamp,
            )
            refreshed = connection.execute(
                """
                SELECT * FROM workflow_nodes
                WHERE workflow_id = ? AND node_id = ?
                """,
                (workflow_id, node_id),
            ).fetchone()
            return self._node_dispatch_spec(refreshed, already_dispatched=False)

    @staticmethod
    def _build_actual_prompt(
        original_prompt: str,
        dependencies: list[str],
        dependency_rows: list[sqlite3.Row],
    ) -> str:
        if not dependencies:
            return original_prompt[:PROMPT_LIMIT]
        responses = {
            row["node_id"]: (
                int(row["position"]) + 1,
                "该前置步骤已跳过，没有可用结果。"
                if row["status"] == "skipped" else str(row["response"] or ""),
            )
            for row in dependency_rows
        }
        blocks: list[str] = []
        remaining = DEPENDENCY_RESULTS_LIMIT
        for dependency in dependencies:
            step_number, result = responses.get(dependency, (1, ""))
            item_limit = min(RESULT_LIMIT, remaining)
            if len(result) > item_limit:
                result = result[: max(0, item_limit - len(TRUNCATION_NOTICE))] + TRUNCATION_NOTICE
            block = f"【第{step_number}步结果】\n{result}"
            if len(block) > remaining:
                block = block[: max(0, remaining - len(TRUNCATION_NOTICE))] + TRUNCATION_NOTICE
            blocks.append(block)
            remaining -= len(block)
            if remaining <= 0:
                break
        suffix = (
            "\n\n前一步已经完成，下面是它提供的结果：\n\n"
            + "\n\n".join(blocks)
            + "\n\n请基于以上结果完成你当前负责的步骤。"
        )
        available = PROMPT_LIMIT - len(suffix)
        if available < len(original_prompt):
            base = original_prompt[: max(0, available - len(TRUNCATION_NOTICE))] + TRUNCATION_NOTICE
        else:
            base = original_prompt
        return (base + suffix)[:PROMPT_LIMIT]

    @staticmethod
    def _node_dispatch_spec(row: sqlite3.Row, *, already_dispatched: bool) -> dict[str, Any]:
        return {
            "workflowId": row["workflow_id"],
            "nodeId": row["node_id"],
            "agentId": row["agent_id"],
            "prompt": row["actual_prompt"] or row["original_prompt"] or row["prompt"],
            "cwd": row["cwd"],
            "write": bool(row["write_enabled"]),
            "model": row["model"],
            "timeoutSec": row["timeout_sec"],
            "status": row["status"],
            "jobId": row["job_id"],
            "alreadyDispatched": already_dispatched,
        }

    def attach_node_job(self, workflow_id: str, node_id: str, snapshot: dict[str, Any]) -> None:
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflow_nodes
                SET job_id = ?, thread_id = ?, turn_id = ?, status = ?
                WHERE workflow_id = ? AND node_id = ?
                """,
                (
                    snapshot.get("job_id"),
                    snapshot.get("thread_id"),
                    snapshot.get("turn_id"),
                    snapshot.get("status", "queued"),
                    workflow_id,
                    node_id,
                ),
            )
            connection.execute(
                "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id,),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                node_id,
                "worker",
                "node.started",
                {"nodeId": node_id, "jobId": snapshot.get("job_id")},
                timestamp,
            )

    def sync_node_job(self, workflow_id: str, node_id: str, snapshot: dict[str, Any]) -> None:
        status = str(snapshot.get("status") or "running")
        finished_at = snapshot.get("finished_at") if status in TERMINAL_NODE_STATUSES else None
        with self._connect() as connection:
            old = connection.execute(
                """
                SELECT status, job_id, thread_id, turn_id, response, error, finished_at
                FROM workflow_nodes
                WHERE workflow_id = ? AND node_id = ?
                """,
                (workflow_id, node_id),
            ).fetchone()
            if old is None:
                return
            values = {
                "status": status,
                "job_id": snapshot.get("job_id") or old["job_id"],
                "thread_id": snapshot.get("thread_id") or old["thread_id"],
                "turn_id": snapshot.get("turn_id") or old["turn_id"],
                "response": snapshot.get("response"),
                "error": snapshot.get("error"),
                "finished_at": finished_at or old["finished_at"],
            }
            if all(old[key] == value for key, value in values.items()):
                return
            connection.execute(
                """
                UPDATE workflow_nodes SET status = ?, job_id = ?, thread_id = ?,
                    turn_id = ?, response = ?, error = ?, finished_at = ?
                WHERE workflow_id = ? AND node_id = ?
                """,
                (
                    values["status"],
                    values["job_id"],
                    values["thread_id"],
                    values["turn_id"],
                    values["response"],
                    values["error"],
                    values["finished_at"],
                    workflow_id,
                    node_id,
                ),
            )
            if old["status"] != status:
                connection.execute(
                    "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                    (workflow_id,),
                )
                event_type = "node.completed" if status == "completed" else f"node.{status}"
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    node_id,
                    "worker",
                    event_type,
                    {
                        "nodeId": node_id,
                        "status": status,
                        "response": snapshot.get("response"),
                        "error": snapshot.get("error"),
                    },
                    utc_now(),
                )

    def update_supervisor(self, workflow_id: str, snapshot: dict[str, Any]) -> None:
        status = str(snapshot.get("status") or "running")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET supervisor_job_id = COALESCE(?, supervisor_job_id),
                    supervisor_thread_id = COALESCE(?, supervisor_thread_id),
                    supervisor_turn_id = COALESCE(?, supervisor_turn_id),
                    supervisor_status = ?,
                    status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
                    started_at = COALESCE(started_at, ?),
                    response = COALESCE(?, response), error = COALESCE(?, error),
                    state_version = state_version + CASE
                        WHEN supervisor_status <> ? THEN 1 ELSE 0 END
                WHERE workflow_id = ?
                """,
                (
                    snapshot.get("job_id"),
                    snapshot.get("thread_id"),
                    snapshot.get("turn_id"),
                    status,
                    snapshot.get("started_at") or utc_now(),
                    snapshot.get("response"),
                    snapshot.get("error"),
                    status,
                    workflow_id,
                ),
            )

    def update_assistant(self, workflow_id: str, snapshot: dict[str, Any]) -> None:
        status = str(snapshot.get("status") or "idle")
        if status in {"completed", "failed", "cancelled", "interrupted"}:
            status = "idle"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET assistant_job_id = COALESCE(?, assistant_job_id),
                    assistant_thread_id = COALESCE(?, assistant_thread_id),
                    assistant_turn_id = COALESCE(?, assistant_turn_id),
                    assistant_status = ?
                WHERE workflow_id = ?
                """,
                (
                    snapshot.get("job_id"),
                    snapshot.get("thread_id"),
                    snapshot.get("turn_id"),
                    status,
                    workflow_id,
                ),
            )

    def set_supervisor_message(self, workflow_id: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows SET supervisor_last_message = ?
                WHERE workflow_id = ? AND supervisor_last_message IS NOT ?
                """,
                (message, workflow_id, message),
            )

    def finish_workflow(
        self,
        workflow_id: str,
        *,
        supervisor_status: str,
        response: str | None,
        error: str | None,
    ) -> None:
        snapshot = self.get_workflow(workflow_id)
        if snapshot["status"] == "cancelled":
            return
        nodes = snapshot["nodes"]
        all_completed = bool(nodes) and all(
            node["status"] in {"completed", "skipped"} for node in nodes
        )
        if supervisor_status == "completed" and all_completed:
            status = "completed"
        elif supervisor_status in {"cancelled", "interrupted"}:
            status = "cancelled"
        else:
            status = "failed"
            if error is None and not all_completed:
                incomplete = [node["id"] for node in nodes if node["status"] != "completed"]
                error = f"主监督线程已结束，但节点未全部完成：{', '.join(incomplete)}"
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET status = ?, supervisor_status = ?, response = ?, error = ?, finished_at = ?,
                    state_version = state_version + 1
                WHERE workflow_id = ?
                """,
                (status, supervisor_status, response, error, timestamp, workflow_id),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                None,
                "supervisor",
                f"workflow.{status}",
                {"status": status, "response": response, "error": error},
                timestamp,
            )

    def add_event(
        self,
        workflow_id: str,
        *,
        node_id: str | None,
        source: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        with self._connect() as connection:
            return self._add_event_with_connection(
                connection, workflow_id, node_id, source, event_type, payload, utc_now()
            )

    def add_events(self, events: list[dict[str, Any]]) -> list[int]:
        if not events:
            return []
        with self._connect() as connection:
            return [
                self._add_event_with_connection(
                    connection,
                    str(event["workflow_id"]),
                    event.get("node_id"),
                    str(event["source"]),
                    str(event["event_type"]),
                    event["payload"],
                    str(event.get("created_at") or utc_now()),
                )
                for event in events
            ]

    @staticmethod
    def _add_event_with_connection(
        connection: sqlite3.Connection,
        workflow_id: str,
        node_id: str | None,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) > EVENT_PAYLOAD_LIMIT:
            encoded = json.dumps(
                {"truncated": True, "preview": encoded[:262_000]}, ensure_ascii=False
            )
        cursor = connection.execute(
            """
            INSERT INTO workflow_events (
                workflow_id, node_id, source, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow_id, node_id, source, event_type, encoded, created_at),
        )
        return int(cursor.lastrowid)

    def list_events(
        self, workflow_id: str, *, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间。")
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE workflow_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (workflow_id, after, limit),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "workflowId": row["workflow_id"],
                "nodeId": row["node_id"],
                "source": row["source"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]
