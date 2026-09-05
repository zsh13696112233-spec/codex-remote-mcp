import asyncio
import base64
import binascii
import hashlib
import json
import logging
import mimetypes
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
REVISION_INSTRUCTION_LIMIT = 4_000
REVISION_CONTEXT_LIMIT = 20_000
PROMPT_LIMIT = 100_000
TRUNCATION_NOTICE = "\n\n【内容过长，已在此处省略】"
SINGLE_OUTPUT_CONSTRAINT = """

【系统单次产物约束】

本步骤每次执行只允许生成或修改一个面向用户交付的产物版本。

首次生成完成后必须立即停止生成，不得自行重绘、重写、修正、优化、覆盖原文件、生成备选版本或再次调用生成工具。

允许对首次产物进行只读检查；如果发现问题，只能在步骤结果中如实说明，禁止自行修复。

首次产物无论质量如何，都必须交由人工审核。只有用户确认返工并开始新一轮步骤执行后，才允许重新生成一次。
"""
EVENT_PAYLOAD_LIMIT = 262_144
ARTIFACT_LIMIT = 20_000_000
ARTIFACTS_PER_WORKFLOW_LIMIT = 50
IMAGE_ARTIFACT_LIMIT = ARTIFACT_LIMIT
IMAGE_ARTIFACTS_PER_WORKFLOW_LIMIT = ARTIFACTS_PER_WORKFLOW_LIMIT
ADVANCE_TIMEOUT_SEC = 30
LEGACY_IMAGE_LINK_PATTERN = re.compile(
    r"\[[^\]]*\]\((?P<path>[^)]+\.(?:png|jpe?g|gif|webp))\)", re.IGNORECASE
)
LOGGER = logging.getLogger(__name__)
PERMISSION_PROFILES = {"read_only", "workspace_write", "auto_review", "full_access"}
MONITOR_EVENTS_SQL = """(source = 'chat' OR (source = 'supervisor' AND
    (event_type IN ('appserver.item/agentMessage/delta', 'appserver.item/completed')
     OR event_type NOT LIKE 'appserver.%')))"""
BOT_EVENTS_SQL = """event_type IN (
    'chat.assistant.completed', 'chat.message.failed',
    'node.started', 'node.completed', 'node.failed', 'node.cancelled', 'node.timed_out',
    'step.advance.waiting', 'step.advance.held', 'step.advance.confirmed',
    'step.advance.resumed', 'step.advance.timed_out',
    'workflow.completed', 'workflow.failed', 'workflow.cancelled')"""


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
                    advance_mode TEXT NOT NULL DEFAULT 'automatic',
                    max_retry_count INTEGER NOT NULL DEFAULT 10,
                    used_retry_count INTEGER NOT NULL DEFAULT 0,
                    handoff_mode TEXT NOT NULL DEFAULT 'legacy_text',
                    response TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    spec_json TEXT NOT NULL,
                    spec_zlib BLOB
                );

                CREATE TABLE IF NOT EXISTS supervisor_leases (
                    supervisor_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL UNIQUE,
                    leased_at TEXT NOT NULL,
                    lease_token TEXT,
                    sidecar_instance_id TEXT,
                    renewed_at TEXT,
                    expires_at TEXT,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS supervisor_sidecars (
                    supervisor_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    last_online_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS supervisor_sidecar_instances (
                    supervisor_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    retired_at TEXT,
                    PRIMARY KEY (supervisor_id, instance_id)
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
                    permission_profile TEXT NOT NULL DEFAULT 'read_only',
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
                    external_event_id TEXT,
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
                    revision_instruction TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_control_pending
                    ON workflow_control_actions(workflow_id, status, created_at);

                CREATE TABLE IF NOT EXISTS workflow_node_revision_instructions (
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    retry_ordinal INTEGER NOT NULL,
                    action_id TEXT,
                    source_message_id TEXT,
                    instruction TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, node_id, retry_ordinal),
                    FOREIGN KEY (workflow_id, node_id)
                        REFERENCES workflow_nodes(workflow_id, node_id) ON DELETE CASCADE,
                    FOREIGN KEY (action_id)
                        REFERENCES workflow_control_actions(action_id) ON DELETE SET NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS workflow_node_revision_action
                    ON workflow_node_revision_instructions(action_id)
                    WHERE action_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS workflow_advance_gates (
                    gate_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    completed_node_id TEXT NOT NULL,
                    next_node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    held_at TEXT,
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_advance_gate_lookup
                    ON workflow_advance_gates(workflow_id, next_node_id, status, created_at);

                CREATE TABLE IF NOT EXISTS workflow_node_attempts (
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    dispatch_token TEXT,
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
            event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(workflow_events)")}
            if "payload_zlib" not in event_columns:
                connection.execute("ALTER TABLE workflow_events ADD COLUMN payload_zlib BLOB")
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_task_bindings (
                workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id) ON DELETE CASCADE,
                task_definition_id TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS workflow_task_binding_lookup "
                               "ON workflow_task_bindings(task_definition_id, workflow_id)")
            for view, predicate in (("monitor", MONITOR_EVENTS_SQL), ("bot", BOT_EVENTS_SQL)):
                connection.execute(f"CREATE INDEX IF NOT EXISTS workflow_events_{view} "
                                   f"ON workflow_events(workflow_id, sequence) WHERE {predicate}")
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_revisions ("
                               "workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id) "
                               "ON DELETE CASCADE, revision INTEGER NOT NULL DEFAULT 0)")
            connection.execute("INSERT OR IGNORE INTO workflow_revisions(workflow_id) "
                               "SELECT workflow_id FROM workflows")
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_node_revisions ("
                               "workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                               "PRIMARY KEY(workflow_id, node_id), FOREIGN KEY(workflow_id, node_id) "
                               "REFERENCES workflow_nodes(workflow_id, node_id) ON DELETE CASCADE)")
            connection.execute("INSERT OR IGNORE INTO workflow_node_revisions "
                               "SELECT workflow_id, node_id, 0 FROM workflow_nodes")
            for table in ("workflow_nodes", "workflow_artifacts"):
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    row = "OLD" if operation == "DELETE" else "NEW"
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS node_revision_{table}_{operation}
                        AFTER {operation} ON {table}
                        WHEN EXISTS (SELECT 1 FROM workflow_nodes WHERE workflow_id = {row}.workflow_id
                                     AND node_id = {row}.node_id)
                        BEGIN
                          INSERT INTO workflow_node_revisions VALUES ({row}.workflow_id, {row}.node_id, 1)
                          ON CONFLICT(workflow_id, node_id) DO UPDATE SET revision = revision + 1;
                        END""")
            for table in ("workflows", "workflow_nodes", "workflow_artifacts",
                          "workflow_chat_messages", "workflow_control_actions", "workflow_advance_gates"):
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    if table == "workflows" and operation == "DELETE":
                        continue
                    row = "OLD" if operation == "DELETE" else "NEW"
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS revision_{table}_{operation}
                        AFTER {operation} ON {table}
                        WHEN EXISTS (SELECT 1 FROM workflows WHERE workflow_id = {row}.workflow_id)
                        BEGIN
                          INSERT INTO workflow_revisions(workflow_id, revision) VALUES ({row}.workflow_id, 1)
                          ON CONFLICT(workflow_id) DO UPDATE SET revision = revision + 1;
                        END""")
            for name, definition in {
                "display_name": "TEXT",
                "role_name": "TEXT",
                "original_prompt": "TEXT",
                "actual_prompt": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "permission_profile": "TEXT NOT NULL DEFAULT 'read_only'",
                "dispatch_token": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE workflow_nodes ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                UPDATE workflow_nodes
                SET permission_profile = CASE
                    WHEN write_enabled = 1 THEN 'workspace_write'
                    ELSE 'read_only'
                END
                WHERE permission_profile IS NULL
                   OR permission_profile = ''
                   OR permission_profile = 'read_only' AND write_enabled = 1
                """
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
                "advance_mode": "TEXT NOT NULL DEFAULT 'automatic'",
                "max_retry_count": "INTEGER NOT NULL DEFAULT 10",
                "used_retry_count": "INTEGER NOT NULL DEFAULT 0",
                "handoff_mode": "TEXT NOT NULL DEFAULT 'legacy_text'",
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
            if "revision_instruction" not in control_columns:
                connection.execute(
                    "ALTER TABLE workflow_control_actions ADD COLUMN revision_instruction TEXT"
                )
            advance_gate_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_advance_gates)"
                ).fetchall()
            }
            if "held_at" not in advance_gate_columns:
                connection.execute(
                    "ALTER TABLE workflow_advance_gates ADD COLUMN held_at TEXT"
                )
            lease_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(supervisor_leases)"
                ).fetchall()
            }
            for name, definition in {
                "lease_token": "TEXT",
                "sidecar_instance_id": "TEXT",
                "renewed_at": "TEXT",
                "expires_at": "TEXT",
            }.items():
                if name not in lease_columns:
                    connection.execute(
                        f"ALTER TABLE supervisor_leases ADD COLUMN {name} {definition}"
                    )
            event_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(workflow_events)"
                ).fetchall()
            }
            if "external_event_id" not in event_columns:
                connection.execute(
                    "ALTER TABLE workflow_events ADD COLUMN external_event_id TEXT"
                )
            # 幂等键只在一个工作流内唯一，避免另一工作流复用相同键时误命中。
            connection.execute("DROP INDEX IF EXISTS workflow_events_external_id")
            connection.execute(
                "CREATE UNIQUE INDEX workflow_events_external_id "
                "ON workflow_events(workflow_id, external_event_id) "
                "WHERE external_event_id IS NOT NULL"
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
        task_id = value.get("taskDefinitionId")
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128
        ):
            raise ValueError("taskDefinitionId 必须是 1 到 128 个字符。")
        if not workflow_id or len(workflow_id) > 128:
            raise ValueError("workflowId 必须是 1 到 128 个字符。")
        supervisor_agent_id = str(
            value.get("supervisorAgentId") or value.get("supervisor_agent_id") or "local"
        ).strip()
        if not supervisor_agent_id or len(supervisor_agent_id) > 128:
            raise ValueError("supervisorAgentId 必须是 1 到 128 个字符。")

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
            if len(agent_id) > 128:
                raise ValueError(f"节点 {node_id} 的 agentId 不能超过 128 个字符。")

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

            raw_profile = raw_node.get(
                "permissionProfile", raw_node.get("permission_profile")
            )
            has_legacy_write = "write" in raw_node
            legacy_write = bool(raw_node.get("write", False))
            if raw_profile is None:
                permission_profile = (
                    "workspace_write" if legacy_write else "read_only"
                )
            else:
                permission_profile = str(raw_profile).strip().lower()
                if permission_profile not in PERMISSION_PROFILES:
                    raise ValueError(
                        f"节点 {node_id} 的 permissionProfile 只能是 "
                        "read_only、workspace_write、auto_review 或 full_access。"
                    )
                profile_write = permission_profile != "read_only"
                if has_legacy_write and legacy_write != profile_write:
                    raise ValueError(
                        f"节点 {node_id} 的 permissionProfile 与 write 字段矛盾。"
                    )

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
                    "write": permission_profile != "read_only",
                    "permissionProfile": permission_profile,
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
        advance_mode = str(value.get("advanceMode") or "automatic").strip().lower()
        if advance_mode not in {"automatic", "semi_automatic"}:
            raise ValueError("advanceMode 只能是 automatic 或 semi_automatic。")
        if advance_mode == "semi_automatic":
            for position, node in enumerate(nodes):
                expected = [] if position == 0 else [nodes[position - 1]["id"]]
                if node["dependsOn"] != expected:
                    raise ValueError("semi_automatic 只支持严格串行工作流。")
        handoff_mode = str(value.get("handoffMode") or "legacy_text").strip().lower()
        if handoff_mode not in {"legacy_text", "cumulative_files"}:
            raise ValueError("handoffMode 只能是 legacy_text 或 cumulative_files。")

        return {
            "workflowId": workflow_id,
            "taskDefinitionId": task_id.strip() if task_id is not None else None,
            "name": value.get("name"),
            "failurePolicy": failure_policy,
            "supervisorAgentId": supervisor_agent_id,
            "supervisorCwd": value.get("supervisorCwd"),
            "supervisorWrite": bool(value.get("supervisorWrite", False)),
            "supervisorModel": value.get("supervisorModel"),
            "supervisorTimeoutSec": supervisor_timeout_sec,
            "maxRetryCount": max_retry_count,
            "advanceMode": advance_mode,
            "handoffMode": handoff_mode,
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
            connection.execute("BEGIN IMMEDIATE")
            if spec.get("taskDefinitionId"):
                self._require_task_idle(connection, spec["taskDefinitionId"], spec["workflowId"])
            try:
                connection.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, name, status, failure_policy,
                        supervisor_agent_id, supervisor_status, created_at,
                        advance_mode, max_retry_count, handoff_mode,
                        spec_json, spec_zlib
                    ) VALUES (?, ?, 'queued', ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec["workflowId"],
                        spec["name"],
                        spec["failurePolicy"],
                        spec["supervisorAgentId"],
                        timestamp,
                        spec["advanceMode"],
                        spec["maxRetryCount"],
                        spec["handoffMode"],
                        compact_spec,
                        compressed_spec,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"工作流已存在：{spec['workflowId']}") from error
            if spec.get("taskDefinitionId"):
                connection.execute("INSERT INTO workflow_task_bindings VALUES (?, ?)",
                                   (spec["workflowId"], spec["taskDefinitionId"]))
            for node in spec["nodes"]:
                connection.execute(
                    """
                    INSERT INTO workflow_nodes (
                        workflow_id, node_id, position, agent_id, executor_type,
                        prompt, display_name, role_name, original_prompt,
                        depends_on_json, cwd, write_enabled, permission_profile, model,
                        timeout_sec, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
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
                        node["permissionProfile"],
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

    @staticmethod
    def _require_task_idle(connection: sqlite3.Connection, task_id: str, workflow_id: str) -> None:
        active = connection.execute(
            "SELECT 1 FROM workflow_task_bindings b JOIN workflows w USING(workflow_id) "
            "WHERE b.task_definition_id = ? AND w.workflow_id <> ? "
            "AND w.status IN ('queued', 'running', 'cancelling') LIMIT 1",
            (task_id, workflow_id),
        ).fetchone()
        if active is not None:
            raise ValueError("当前任务已有其他运行，暂不能启动或返工。")

    def register_task_bindings(self, task_id: str, workflow_ids: list[str]) -> None:
        """配置中心升级时补齐历史归属，不修改冻结快照，也不复活已丢失的运行。"""
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
            raise ValueError("taskDefinitionId 必须是 1 到 128 个字符。")
        if not isinstance(workflow_ids, list) or not 1 <= len(workflow_ids) <= 200:
            raise ValueError("每批必须包含 1 到 200 个工作流编号。")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 128
               for value in workflow_ids):
            raise ValueError("工作流编号必须是 1 到 128 个字符。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for workflow_id in workflow_ids:
                existing = connection.execute(
                    "SELECT task_definition_id FROM workflow_task_bindings WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing is not None and existing[0] != task_id:
                    raise ValueError("运行的任务归属不能修改。")
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_task_bindings "
                    "SELECT workflow_id, ? FROM workflows WHERE workflow_id = ?",
                    (task_id, workflow_id),
                )

    def recover_active_workflows_after_restart(self) -> list[str]:
        """阶段 A 不重新附着旧会话，网关重启后直接终止遗留运行。"""
        timestamp = utc_now()
        error = "工作流网关已重启，任务已中断。"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT workflow_id FROM workflows "
                "WHERE status IN ('running', 'cancelling') "
                "ORDER BY created_at, workflow_id"
            ).fetchall()
            workflow_ids = [str(row["workflow_id"]) for row in rows]
            for workflow_id in workflow_ids:
                self._supersede_pending_advances(
                    connection, workflow_id, "gateway_restarted", timestamp
                )
                connection.execute(
                    "UPDATE workflow_nodes SET status = 'interrupted', "
                    "error = COALESCE(error, ?), finished_at = COALESCE(finished_at, ?) "
                    "WHERE workflow_id = ? "
                    "AND status IN ('queued', 'running', 'cancelling')",
                    (error, timestamp, workflow_id),
                )
                connection.execute(
                    "UPDATE workflows SET status = 'failed', "
                    "supervisor_status = 'failed', error = ?, finished_at = ?, "
                    "state_version = state_version + 1 WHERE workflow_id = ?",
                    (error, timestamp, workflow_id),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    None,
                    "gateway",
                    "workflow.failed",
                    {"status": "failed", "reason": "gateway_restarted", "error": error},
                    timestamp,
                )
            connection.execute("DELETE FROM supervisor_leases")
        return workflow_ids

    def claim_next_workflow(
        self,
        supervisor_id: str,
        *,
        sidecar_instance_id: str | None = None,
        lease_timeout_sec: int | None = None,
    ) -> dict[str, Any] | None:
        """以固定 FIFO 顺序为一个主监督领取至多一个工作流。"""
        timestamp = utc_now()
        if (sidecar_instance_id is None) != (lease_timeout_sec is None):
            raise ValueError("远程租约必须同时提供 Sidecar 实例和超时时间。")
        if lease_timeout_sec is not None and lease_timeout_sec < 5:
            raise ValueError("远程租约超时时间不能小于 5 秒。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT l.workflow_id, w.status FROM supervisor_leases l "
                "JOIN workflows w ON w.workflow_id = l.workflow_id "
                "WHERE l.supervisor_id = ?",
                (supervisor_id,),
            ).fetchone()
            if lease is not None and lease["status"] in {
                "completed",
                "failed",
                "cancelled",
            }:
                connection.execute(
                    "DELETE FROM supervisor_leases WHERE supervisor_id = ?",
                    (supervisor_id,),
                )
                lease = None
            if lease is not None:
                return None

            sidecar_renewed_at: str | None = None
            sidecar_expires_at: str | None = None
            if sidecar_instance_id is not None:
                sidecar = connection.execute(
                    "SELECT instance_id, last_heartbeat_at FROM supervisor_sidecars "
                    "WHERE supervisor_id = ?",
                    (supervisor_id,),
                ).fetchone()
                if sidecar is None or str(sidecar["instance_id"]) != sidecar_instance_id:
                    return None
                heartbeat_at = datetime.fromisoformat(str(sidecar["last_heartbeat_at"]))
                sidecar_deadline = heartbeat_at + timedelta(seconds=lease_timeout_sec)
                if datetime.now(UTC) > sidecar_deadline:
                    return None
                sidecar_renewed_at = str(sidecar["last_heartbeat_at"])
                sidecar_expires_at = sidecar_deadline.isoformat()

            row = connection.execute(
                "SELECT workflow_id FROM workflows "
                "WHERE supervisor_agent_id = ? AND status = 'queued' "
                "ORDER BY created_at, workflow_id LIMIT 1",
                (supervisor_id,),
            ).fetchone()
            if row is None:
                return None
            workflow_id = str(row["workflow_id"])
            lease_token = uuid.uuid4().hex if sidecar_instance_id else None
            expires_at = sidecar_expires_at
            connection.execute(
                "INSERT INTO supervisor_leases ("
                "supervisor_id, workflow_id, leased_at, lease_token, "
                "sidecar_instance_id, renewed_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    supervisor_id,
                    workflow_id,
                    timestamp,
                    lease_token,
                    sidecar_instance_id,
                    sidecar_renewed_at,
                    expires_at,
                ),
            )
            connection.execute(
                "UPDATE workflows SET status = 'running', supervisor_status = 'queued', "
                "started_at = COALESCE(started_at, ?), finished_at = NULL, "
                "state_version = state_version + 1 WHERE workflow_id = ?",
                (timestamp, workflow_id),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                None,
                "gateway",
                "supervisor.lease_acquired",
                {
                    "supervisorAgentId": supervisor_id,
                    "leasedAt": timestamp,
                    "remoteSidecar": sidecar_instance_id is not None,
                },
                timestamp,
            )
        return self.get_spec(workflow_id)

    def record_sidecar_heartbeat(
        self,
        supervisor_id: str,
        instance_id: str,
        started_at: str,
        *,
        lease_timeout_sec: int,
    ) -> dict[str, Any]:
        """登记 Sidecar 实例、续租，并返回只属于该主监督的活动租约。"""
        if not supervisor_id or not instance_id:
            raise ValueError("Sidecar 身份和实例 ID 不能为空。")
        if lease_timeout_sec < 5:
            raise ValueError("租约超时时间不能小于 5 秒。")
        timestamp = utc_now()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=lease_timeout_sec)
        ).isoformat()
        failed_workflow_id: str | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT instance_id, started_at FROM supervisor_sidecars "
                "WHERE supervisor_id = ?",
                (supervisor_id,),
            ).fetchone()
            incoming_started_at = self._parse_sidecar_started_at(started_at)
            if current is not None:
                current_instance_id = str(current["instance_id"])
                current_started_at = self._parse_sidecar_started_at(
                    str(current["started_at"])
                )
                if current_instance_id == instance_id:
                    if incoming_started_at != current_started_at:
                        raise RuntimeError("Sidecar 实例的启动时间与已登记信息不一致。")
                else:
                    known_instance = connection.execute(
                        "SELECT retired_at FROM supervisor_sidecar_instances "
                        "WHERE supervisor_id = ? AND instance_id = ?",
                        (supervisor_id, instance_id),
                    ).fetchone()
                    if known_instance is not None:
                        raise RuntimeError("旧 Sidecar 实例已经失效，不能重新登记。")
            if current is not None and str(current["instance_id"]) != instance_id:
                connection.execute(
                    "INSERT OR IGNORE INTO supervisor_sidecar_instances ("
                    "supervisor_id, instance_id, started_at, first_seen_at, "
                    "last_seen_at, retired_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        supervisor_id,
                        str(current["instance_id"]),
                        str(current["started_at"]),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE supervisor_sidecar_instances SET retired_at = ? "
                    "WHERE supervisor_id = ? AND instance_id = ?",
                    (timestamp, supervisor_id, str(current["instance_id"])),
                )
                lease = connection.execute(
                    "SELECT workflow_id, sidecar_instance_id FROM supervisor_leases "
                    "WHERE supervisor_id = ?",
                    (supervisor_id,),
                ).fetchone()
                if lease is not None and lease["sidecar_instance_id"] is not None:
                    failed_workflow_id = str(lease["workflow_id"])
                    self._fail_workflow_with_connection(
                        connection,
                        failed_workflow_id,
                        "主监督 Sidecar 已重启，旧任务已中断。",
                        "sidecar_instance_replaced",
                        timestamp,
                    )
            connection.execute(
                """
                INSERT INTO supervisor_sidecars (
                    supervisor_id, instance_id, started_at,
                    last_heartbeat_at, last_online_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(supervisor_id) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    started_at = excluded.started_at,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_online_at = excluded.last_online_at
                """,
                (supervisor_id, instance_id, started_at, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO supervisor_sidecar_instances (
                    supervisor_id, instance_id, started_at,
                    first_seen_at, last_seen_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(supervisor_id, instance_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (supervisor_id, instance_id, started_at, timestamp, timestamp),
            )
            connection.execute(
                "UPDATE supervisor_leases SET renewed_at = ?, expires_at = ? "
                "WHERE supervisor_id = ? AND sidecar_instance_id = ?",
                (timestamp, expires_at, supervisor_id, instance_id),
            )
            lease = connection.execute(
                "SELECT workflow_id, lease_token, expires_at "
                "FROM supervisor_leases WHERE supervisor_id = ? "
                "AND sidecar_instance_id = ?",
                (supervisor_id, instance_id),
            ).fetchone()
        return {
            "supervisorId": supervisor_id,
            "instanceId": instance_id,
            "heartbeatAt": timestamp,
            "failedWorkflowId": failed_workflow_id,
            "lease": (
                {
                    "workflowId": str(lease["workflow_id"]),
                    "leaseToken": str(lease["lease_token"]),
                    "expiresAt": str(lease["expires_at"]),
                }
                if lease is not None
                else None
            ),
        }

    @staticmethod
    def _parse_sidecar_started_at(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Sidecar 启动时间必须是 ISO-8601 时间。") from error
        if parsed.tzinfo is None:
            raise ValueError("Sidecar 启动时间必须包含时区。")
        return parsed.astimezone(UTC)

    def sidecar_status(
        self, supervisor_id: str, *, timeout_sec: int
    ) -> dict[str, Any]:
        """读取持久化心跳，并按当前时间计算远程主监督在线状态。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_sidecars WHERE supervisor_id = ?",
                (supervisor_id,),
            ).fetchone()
        if row is None:
            return {
                "connectionStatus": "offline",
                "checkedAt": None,
                "lastOnlineAt": None,
                "instanceId": None,
            }
        checked_at = str(row["last_heartbeat_at"])
        deadline = datetime.fromisoformat(checked_at) + timedelta(seconds=timeout_sec)
        return {
            "connectionStatus": (
                "online" if datetime.now(UTC) <= deadline else "offline"
            ),
            "checkedAt": checked_at,
            "lastOnlineAt": str(row["last_online_at"]),
            "instanceId": str(row["instance_id"]),
        }

    def fail_next_queued_for_offline_sidecar(self, supervisor_id: str) -> str | None:
        """远程主监督离线时立即失败最早排队项，不创建租约。"""
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT workflow_id FROM workflows "
                "WHERE supervisor_agent_id = ? AND status = 'queued' "
                "ORDER BY created_at, workflow_id LIMIT 1",
                (supervisor_id,),
            ).fetchone()
            if row is None:
                return None
            workflow_id = str(row["workflow_id"])
            self._fail_workflow_with_connection(
                connection,
                workflow_id,
                "主监督 Sidecar 当前离线，任务无法启动。",
                "sidecar_offline",
                timestamp,
            )
        return workflow_id

    def expire_sidecar_leases(self) -> list[dict[str, str]]:
        """终止已超过心跳期限的远程租约，并拒绝旧实例继续写入。"""
        timestamp = utc_now()
        expired: list[dict[str, str]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT supervisor_id, workflow_id FROM supervisor_leases "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                workflow_id = str(row["workflow_id"])
                supervisor_id = str(row["supervisor_id"])
                self._fail_workflow_with_connection(
                    connection,
                    workflow_id,
                    "主监督 Sidecar 心跳超时，任务已中断。",
                    "sidecar_heartbeat_timeout",
                    timestamp,
                )
                expired.append(
                    {"workflowId": workflow_id, "supervisorId": supervisor_id}
                )
        return expired

    def validate_sidecar_access(
        self,
        supervisor_id: str,
        workflow_id: str,
        *,
        lease_token: str | None = None,
        require_lease: bool = False,
    ) -> None:
        """限制 Sidecar 只能访问自己的工作流，写操作必须持有当前租约。"""
        with self._connect() as connection:
            self._validate_sidecar_access_with_connection(
                connection,
                supervisor_id,
                workflow_id,
                lease_token=lease_token,
                require_lease=require_lease,
            )

    @staticmethod
    def _validate_sidecar_access_with_connection(
        connection: sqlite3.Connection,
        supervisor_id: str,
        workflow_id: str,
        *,
        lease_token: str | None,
        require_lease: bool,
    ) -> None:
        workflow = connection.execute(
            "SELECT supervisor_agent_id FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if workflow is None:
            raise ValueError(f"找不到工作流：{workflow_id}")
        if str(workflow["supervisor_agent_id"]) != supervisor_id:
            raise PermissionError("Sidecar 无权访问其他主监督的工作流。")
        if not require_lease:
            return
        lease = connection.execute(
            "SELECT l.lease_token, l.expires_at, l.sidecar_instance_id, "
            "s.instance_id AS current_instance_id "
            "FROM supervisor_leases l "
            "LEFT JOIN supervisor_sidecars s ON s.supervisor_id = l.supervisor_id "
            "WHERE l.supervisor_id = ? AND l.workflow_id = ?",
            (supervisor_id, workflow_id),
        ).fetchone()
        if (
            lease is None
            or not lease_token
            or str(lease["lease_token"] or "") != lease_token
            or lease["expires_at"] is None
            or str(lease["expires_at"]) <= utc_now()
            or not lease["sidecar_instance_id"]
            or lease["sidecar_instance_id"] != lease["current_instance_id"]
        ):
            raise RuntimeError("Sidecar 租约不存在、已过期或已被替换。")

    def _fail_workflow_with_connection(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        error: str,
        reason: str,
        timestamp: str,
    ) -> None:
        self._supersede_pending_advances(connection, workflow_id, reason, timestamp)
        connection.execute(
            "UPDATE workflow_nodes SET status = 'interrupted', error = COALESCE(error, ?), "
            "finished_at = COALESCE(finished_at, ?) WHERE workflow_id = ? "
            "AND status IN ('queued', 'running', 'cancelling')",
            (error, timestamp, workflow_id),
        )
        connection.execute(
            "UPDATE workflows SET status = 'failed', supervisor_status = 'failed', "
            "error = ?, finished_at = ?, state_version = state_version + 1 "
            "WHERE workflow_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')",
            (error, timestamp, workflow_id),
        )
        connection.execute(
            "DELETE FROM supervisor_leases WHERE workflow_id = ?", (workflow_id,)
        )
        self._add_event_with_connection(
            connection,
            workflow_id,
            None,
            "gateway",
            "workflow.failed",
            {"status": "failed", "reason": reason, "error": error},
            timestamp,
        )

    def has_supervisor_lease(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM supervisor_leases WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        return row is not None

    def leased_supervisor_ids(self) -> set[str]:
        """返回当前持有活动工作流租约的主监督 ID。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT supervisor_id FROM supervisor_leases"
            ).fetchall()
        return {str(row["supervisor_id"]) for row in rows}

    def release_supervisor_claim(self, workflow_id: str) -> None:
        """仅用于主监督任务尚未创建成功时，将领取恢复为排队。"""
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT supervisor_id FROM supervisor_leases WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if lease is None:
                return
            connection.execute(
                "DELETE FROM supervisor_leases WHERE workflow_id = ?", (workflow_id,)
            )
            connection.execute(
                "UPDATE workflows SET status = 'queued', supervisor_status = 'queued', "
                "started_at = NULL, state_version = state_version + 1 "
                "WHERE workflow_id = ? AND status = 'running'",
                (workflow_id,),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                None,
                "gateway",
                "supervisor.lease_released",
                {
                    "supervisorAgentId": lease["supervisor_id"],
                    "reason": "task_start_failed",
                },
                timestamp,
            )

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

    def poll_workflow(self, workflow_id: str, known_revision: str | None = None,
                      known_results: list[int] | None = None) -> dict[str, Any]:
        """无变化时只查询版本与事件游标，不加载步骤正文及附件元数据。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.revision, (SELECT COALESCE(MAX(sequence), 0) FROM workflow_events "
                "WHERE workflow_id = r.workflow_id) FROM workflow_revisions r WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise ValueError("找不到工作流。")
        if known_results is not None and (not isinstance(known_results, list)
            or len(known_results) > 100 or any(type(value) is not int or value < 0 for value in known_results)):
            raise ValueError("步骤结果版本无效。")
        revision = str(row[0])
        if revision == known_revision:
            return {"unchanged": True, "revision": revision, "lastEventSequence": row[1]}
        # 先读版本再读快照；并发写入最多导致下一次多取一次，不会遗漏变化。
        snapshot = self.get_workflow(workflow_id, known_results=known_results)
        snapshot["revision"] = revision
        return snapshot

    def workflow_statuses(self, workflow_ids: list[str]) -> dict[str, str]:
        if not isinstance(workflow_ids, list) or not 1 <= len(workflow_ids) <= 200:
            raise ValueError("每批必须包含 1 到 200 个工作流编号。")
        if any(not isinstance(value, str) or not value or len(value) > 128 for value in workflow_ids):
            raise ValueError("工作流编号无效。")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id, status FROM workflows WHERE workflow_id IN ("
                + ",".join("?" for _ in workflow_ids) + ")", workflow_ids,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_workflow(self, workflow_id: str, *, known_results: list[int] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            workflow = connection.execute(
                """
                SELECT workflow_id, name, status, failure_policy,
                       supervisor_agent_id, supervisor_job_id,
                       supervisor_thread_id, supervisor_turn_id,
                       supervisor_status, supervisor_last_message,
                       response, error, created_at, started_at, finished_at,
                       state_version, assistant_job_id, assistant_thread_id,
                       assistant_turn_id, assistant_status, advance_mode,
                       max_retry_count, used_retry_count, handoff_mode
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
                       thread_id, turn_id,
                       CASE WHEN ? THEN NULL ELSE response END AS response,
                       CASE WHEN ? THEN NULL ELSE error END AS error, started_at,
                       finished_at, attempt_count
                FROM workflow_nodes
                WHERE workflow_id = ? ORDER BY position
                """,
                (known_results is not None, known_results is not None, workflow_id),
            ).fetchall()
            revisions = {row["node_id"]: row["revision"] for row in connection.execute(
                "SELECT node_id, revision FROM workflow_node_revisions WHERE workflow_id = ?", (workflow_id,)
            ).fetchall()}
            unchanged = {row["node_id"] for row in node_rows if known_results is not None
                         and row["position"] < len(known_results)
                         and known_results[row["position"]] == revisions.get(row["node_id"], 0)}
            changed = [row["node_id"] for row in node_rows if row["node_id"] not in unchanged]
            placeholders = ",".join("?" for _ in changed) or "NULL"
            bodies = {}
            if known_results is not None and changed:
                bodies = {row["node_id"]: row for row in connection.execute(
                    f"SELECT node_id, response, error FROM workflow_nodes WHERE workflow_id = ? "
                    f"AND node_id IN ({placeholders})", (workflow_id, *changed),
                ).fetchall()}
            artifact_rows = connection.execute(
                f"""
                SELECT artifact_id, node_id, media_type, filename, byte_size, created_at
                FROM workflow_artifacts
                WHERE workflow_id = ? AND node_id IN ({placeholders}) ORDER BY created_at, artifact_id
                """,
                (workflow_id, *changed),
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
                SELECT action_id, action_type, node_id, status, expires_at,
                       revision_instruction
                FROM workflow_control_actions
                WHERE workflow_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id, utc_now()),
            ).fetchone()
            pending_advance_row = connection.execute(
                """
                SELECT gate_id, completed_node_id, next_node_id, status,
                       expires_at, held_at, confirmed_at
                FROM workflow_advance_gates
                WHERE workflow_id = ?
                  AND ((status = 'pending' AND expires_at > ?) OR status = 'held')
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
            node["resultRevision"] = revisions.get(node["id"], 0)
            if node["id"] in unchanged:
                node.pop("response", None)
                node.pop("error", None)
                node["resultUnchanged"] = True
            else:
                if node["id"] in bodies:
                    node["response"] = bodies[node["id"]]["response"]
                    node["error"] = bodies[node["id"]]["error"]
                node["artifacts"] = artifacts_by_node.get(str(node["id"]), [])
        current_nodes = [node["id"] for node in nodes if node["status"] in ACTIVE_NODE_STATUSES]
        completed_count = sum(node["status"] == "completed" for node in nodes)
        return {
            "workflowId": workflow["workflow_id"],
            "name": workflow["name"],
            "status": workflow["status"],
            "failurePolicy": workflow["failure_policy"],
            "advanceMode": workflow["advance_mode"],
            "handoffMode": workflow["handoff_mode"],
            "pendingAdvance": self._advance_gate_snapshot(pending_advance_row),
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
        image_type = self._image_type(content)
        if image_type is None:
            raise ValueError("仅支持 PNG、JPEG、GIF 和 WebP 图片。")
        media_type, extension = image_type
        source_item_id = str(source_item_id or "").strip()
        filename_seed = hashlib.sha256(
            f"{workflow_id}:{node_id}:{source_item_id}".encode("utf-8")
        ).hexdigest()[:8]
        return self.save_artifact_bytes(
            workflow_id,
            node_id,
            source_item_id,
            f"generated-image-{filename_seed}.{extension}",
            content,
            media_type,
        )

    @staticmethod
    def _safe_artifact_filename(filename: str) -> str:
        normalized = str(filename or "").replace("\\", "/").split("/")[-1].strip()
        normalized = "".join(
            "_" if ord(character) < 32 or character in '<>:"|?*' else character
            for character in normalized
        )
        if normalized in {"", ".", ".."}:
            normalized = "artifact.bin"
        return normalized[:240]

    def save_artifact_bytes(
        self,
        workflow_id: str,
        node_id: str,
        source_item_id: str,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        """保存任意格式的托管文件，并按来源和 SHA-256 保持幂等。"""
        source_item_id = str(source_item_id or "").strip()
        if not source_item_id or len(source_item_id) > 200:
            raise ValueError("文件来源编号无效。")
        if not isinstance(content, bytes) or not content:
            raise ValueError("文件内容不能为空。")
        if len(content) > ARTIFACT_LIMIT:
            raise ValueError("单个文件大小不能超过 20 MB。")
        filename = self._safe_artifact_filename(filename)
        media_type = str(media_type or "").strip().lower()
        if not re.fullmatch(r"[\w!#$&^_.+-]+/[\w!#$&^_.+-]+", media_type):
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
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
            if count >= ARTIFACTS_PER_WORKFLOW_LIMIT:
                raise ValueError("单个工作流最多保存 50 个文件。")
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
            raise ValueError("找不到工作流图片或文件。")
        result = self._artifact_snapshot(row)
        result["content"] = bytes(row["content"])
        result["nodeId"] = row["node_id"]
        return result

    def get_cumulative_artifact_inputs(
        self, workflow_id: str, node_id: str
    ) -> list[dict[str, Any]]:
        """返回当前步骤之前所有步骤的当前有效文件，包含无文件步骤。"""
        with self._connect() as connection:
            target = connection.execute(
                "SELECT position FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if target is None:
                raise ValueError(f"找不到步骤：{node_id}")
            rows = connection.execute(
                """
                SELECT n.node_id, n.position, n.display_name, n.status,
                       a.artifact_id, a.media_type, a.filename, a.content,
                       a.byte_size, a.sha256, a.created_at
                FROM workflow_nodes n
                LEFT JOIN workflow_artifacts a
                  ON a.workflow_id = n.workflow_id AND a.node_id = n.node_id
                WHERE n.workflow_id = ? AND n.position < ?
                ORDER BY n.position, a.created_at, a.artifact_id
                """,
                (workflow_id, target["position"]),
            ).fetchall()
        steps: list[dict[str, Any]] = []
        by_node: dict[str, dict[str, Any]] = {}
        for row in rows:
            source_node_id = str(row["node_id"])
            step = by_node.get(source_node_id)
            if step is None:
                step = {
                    "nodeId": source_node_id,
                    "stepNumber": int(row["position"]) + 1,
                    "displayName": row["display_name"],
                    "status": row["status"],
                    "artifacts": [],
                }
                by_node[source_node_id] = step
                steps.append(step)
            if row["artifact_id"] is not None:
                step["artifacts"].append(
                    {
                        "id": row["artifact_id"],
                        "mediaType": row["media_type"],
                        "filename": row["filename"],
                        "byteSize": int(row["byte_size"]),
                        "sha256": row["sha256"],
                        "createdAt": row["created_at"],
                        "content": bytes(row["content"]),
                    }
                )
        return steps

    def update_node_actual_prompt(
        self,
        workflow_id: str,
        node_id: str,
        actual_prompt: str,
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        """在远程文件路径已确定后保存本次真正派发的提示词。"""
        if len(actual_prompt) > PROMPT_LIMIT:
            raise ValueError("实际提示词不能超过 100000 个字符。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    workflow_id,
                    lease_token=lease_token,
                    require_lease=True,
                )
            cursor = connection.execute(
                "UPDATE workflow_nodes SET actual_prompt = ? "
                "WHERE workflow_id = ? AND node_id = ?",
                (actual_prompt, workflow_id, node_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"找不到步骤：{node_id}")

    def count_current_artifacts(self, workflow_id: str, node_id: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_artifacts "
                    "WHERE workflow_id = ? AND node_id = ?",
                    (workflow_id, node_id),
                ).fetchone()[0]
            )

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
                SELECT action_id, action_type, node_id, status, expires_at,
                       revision_instruction
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

    @staticmethod
    def _normalize_revision_instruction(value: str | None) -> str | None:
        if value is None:
            return None
        instruction = str(value).strip()
        if not instruction:
            return None
        if len(instruction) > REVISION_INSTRUCTION_LIMIT:
            raise ValueError(
                f"返工要求不能超过 {REVISION_INSTRUCTION_LIMIT} 个字符。"
            )
        return instruction

    def propose_control(
        self,
        workflow_id: str,
        action_type: str,
        node_id: str | None,
        message_id: str,
        revision_instruction: str | None = None,
    ) -> dict[str, Any]:
        if action_type == "retry":
            action_type = "restart_from"
        if action_type not in {"stop", "restart_from", "skip"}:
            raise ValueError("控制类型只能是 stop、restart_from 或 skip。")
        revision_instruction = self._normalize_revision_instruction(revision_instruction)
        if action_type != "restart_from" and revision_instruction is not None:
            raise ValueError("只有返工操作可以包含返工要求。")
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
                    revision_instruction, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (action_id, workflow_id, action_type, node_id, message_id,
                 workflow["state_version"], revision_instruction, now, expires, now),
            )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "chat", "chat.control.proposed",
                {"messageId": message_id, "actionId": action_id,
                 "actionType": action_type, "nodeId": node_id,
                 "revisionInstruction": revision_instruction}, now,
            )
        result = {"actionId": action_id, "actionType": action_type, "nodeId": node_id,
                  "expiresAt": expires, "requiredConfirmation": "确认执行",
                  "revisionInstruction": revision_instruction}
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
        return {
            "actionId": action_id,
            "actionType": action["action_type"],
            "nodeId": action["node_id"],
            "revisionInstruction": action["revision_instruction"],
            "proposedByMessageId": action["proposed_by_message_id"],
        }

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
        return {
            "actionId": action_id,
            "workflowId": row["workflow_id"],
            "actionType": row["action_type"],
            "nodeId": row["node_id"],
            "confirmedByMessageId": row["confirmed_by_message_id"],
            "proposedByMessageId": row["proposed_by_message_id"],
            "revisionInstruction": row["revision_instruction"],
        }

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
        self,
        workflow_id: str,
        node_id: str,
        *,
        action_id: str | None = None,
        revision_instruction: str | None = None,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        revision_instruction = self._normalize_revision_instruction(revision_instruction)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT task_definition_id FROM workflow_task_bindings WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if binding is not None:
                self._require_task_idle(connection, binding[0], workflow_id)
            else:
                legacy = connection.execute(
                    "SELECT spec_json, spec_zlib FROM workflows WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()
                if legacy is not None:
                    legacy_spec = json.loads(zlib.decompress(legacy["spec_zlib"]).decode("utf-8")
                                             if legacy["spec_zlib"] is not None else legacy["spec_json"])
                    if "taskDefinitionId" not in legacy_spec:
                        raise ValueError("历史运行尚未完成升级登记，请等待配置中心完成登记后重试。")
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
            self._supersede_pending_advances(connection, workflow_id, "restart", now)
            retry_ordinal = used + 1
            if revision_instruction is not None:
                connection.execute(
                    """
                    INSERT INTO workflow_node_revision_instructions (
                        workflow_id, node_id, retry_ordinal, action_id,
                        source_message_id, instruction, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        node_id,
                        retry_ordinal,
                        action_id,
                        source_message_id,
                        revision_instruction,
                        now,
                    ),
                )
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
                    finished_at = NULL, actual_prompt = NULL, dispatch_token = NULL,
                    attempt_count = attempt_count + 1
                WHERE workflow_id = ? AND position >= ?
                """,
                (workflow_id, row["position"]),
            )
            connection.execute(
                "UPDATE workflows SET status = CASE WHEN EXISTS ("
                "SELECT 1 FROM supervisor_leases WHERE workflow_id = ?"
                ") THEN 'running' ELSE 'queued' END, supervisor_status = 'queued', "
                "finished_at = NULL, response = NULL, error = NULL, "
                "used_retry_count = used_retry_count + 1, "
                "state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id, workflow_id),
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
                    "revisionInstructionApplied": revision_instruction is not None,
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
            self._supersede_pending_advances(connection, workflow_id, "skip", now)
            connection.execute(
                "UPDATE workflow_nodes SET status = 'skipped', response = NULL, "
                "error = NULL, finished_at = ? WHERE workflow_id = ? AND node_id = ?",
                (now, workflow_id, node_id),
            )
            connection.execute(
                "UPDATE workflows SET status = CASE WHEN EXISTS ("
                "SELECT 1 FROM supervisor_leases WHERE workflow_id = ?"
                ") THEN 'running' ELSE 'queued' END, supervisor_status = 'queued', "
                "finished_at = NULL, error = NULL, "
                "state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id, workflow_id),
            )
            self._add_event_with_connection(
                connection, workflow_id, node_id, "chat", "node.skipped",
                {"nodeId": node_id}, now,
            )
        return self.get_workflow(workflow_id)

    def stop_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.cancel_workflow(
            workflow_id,
            reason="用户已停止任务。",
            source="chat",
            event_reason="chat_control",
        )

    def cancel_workflow(
        self,
        workflow_id: str,
        *,
        reason: str = "用户已取消任务。",
        source: str = "gateway",
        event_reason: str = "user_requested",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            if workflow["status"] not in {"completed", "failed", "cancelled"}:
                self._supersede_pending_advances(connection, workflow_id, "stop", now)
                connection.execute(
                    "UPDATE workflow_nodes SET status = 'cancelled', "
                    "error = COALESCE(error, ?), "
                    "finished_at = COALESCE(finished_at, ?) WHERE workflow_id = ? "
                    "AND status NOT IN "
                    "('completed', 'skipped', 'failed', 'cancelled', 'interrupted')",
                    (reason, now, workflow_id),
                )
                connection.execute(
                    "UPDATE workflows SET status = 'cancelled', "
                    "supervisor_status = 'cancelled', error = ?, finished_at = ?, "
                    "state_version = state_version + 1 WHERE workflow_id = ?",
                    (reason, now, workflow_id),
                )
                self._release_supervisor_lease_with_connection(
                    connection, workflow_id, event_reason, now
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    None,
                    source,
                    "workflow.cancelled",
                    {"status": "cancelled", "reason": event_reason},
                    now,
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
            "revisionInstruction": row["revision_instruction"],
        }

    @staticmethod
    def _advance_gate_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "gateId": row["gate_id"],
            "completedNodeId": row["completed_node_id"],
            "nextNodeId": row["next_node_id"],
            "state": "held" if row["status"] == "held" else "countdown",
            "expiresAt": row["expires_at"],
            "heldAt": row["held_at"],
        }

    def pending_advance_for_node(
        self, workflow_id: str, node_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT gate_id, completed_node_id, next_node_id, status,
                       expires_at, held_at, confirmed_at
                FROM workflow_advance_gates
                WHERE workflow_id = ? AND next_node_id = ?
                  AND status IN ('pending', 'held')
                ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id, node_id),
            ).fetchone()
        return self._advance_gate_snapshot(row)

    def hold_advance(self, workflow_id: str, gate_id: str) -> dict[str, Any]:
        now = utc_now()
        expired = False
        stale = False
        result: dict[str, Any] | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_advance_gates WHERE workflow_id = ? AND gate_id = ?",
                (workflow_id, gate_id),
            ).fetchone()
            if row is None:
                raise ValueError("找不到对应的步骤确认请求。")
            if row["status"] == "held":
                return {
                    "gateId": gate_id,
                    "status": "held",
                    "heldAt": row["held_at"],
                }
            if row["status"] != "pending":
                raise RuntimeError("这次步骤确认已经失效，请刷新页面。")
            workflow = connection.execute(
                "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            next_node = connection.execute(
                "SELECT status FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, row["next_node_id"]),
            ).fetchone()
            if (
                workflow is None
                or workflow["status"] in {"completed", "failed", "cancelled"}
                or next_node is None
                or next_node["status"] != "pending"
            ):
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'superseded', updated_at = ? "
                    "WHERE gate_id = ?",
                    (now, gate_id),
                )
                stale = True
            elif row["expires_at"] <= now:
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'timed_out', updated_at = ? "
                    "WHERE gate_id = ?",
                    (now, gate_id),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    row["completed_node_id"],
                    "gateway",
                    "step.advance.timed_out",
                    {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                    now,
                )
                connection.execute(
                    "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                    (workflow_id,),
                )
                expired = True
            else:
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'held', held_at = ?, "
                    "updated_at = ? WHERE gate_id = ?",
                    (now, now, gate_id),
                )
                connection.execute(
                    "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                    (workflow_id,),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    row["completed_node_id"],
                    "gateway",
                    "step.advance.held",
                    {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                    now,
                )
                result = {"gateId": gate_id, "status": "held", "heldAt": now}
        if stale:
            raise RuntimeError("任务状态已经变化，请刷新页面。")
        if expired:
            raise RuntimeError("30 秒倒计时已经结束，系统正在自动进入下一步。")
        assert result is not None
        return result

    def confirm_advance(self, workflow_id: str, gate_id: str) -> dict[str, Any]:
        now = utc_now()
        expired = False
        stale = False
        result: dict[str, Any] | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_advance_gates WHERE workflow_id = ? AND gate_id = ?",
                (workflow_id, gate_id),
            ).fetchone()
            if row is None:
                raise ValueError("找不到对应的步骤确认请求。")
            if row["status"] == "confirmed":
                return {
                    "gateId": gate_id,
                    "status": "confirmed",
                    "confirmedAt": row["confirmed_at"],
                    "resumedFromHold": row["held_at"] is not None,
                }
            if row["status"] not in {"pending", "held"}:
                raise RuntimeError("这次步骤确认已经失效，请刷新页面。")
            was_held = row["status"] == "held"
            workflow = connection.execute(
                "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            next_node = connection.execute(
                "SELECT status FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, row["next_node_id"]),
            ).fetchone()
            if (
                workflow is None
                or workflow["status"] in {"completed", "failed", "cancelled"}
                or next_node is None
                or next_node["status"] != "pending"
            ):
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'superseded', updated_at = ? "
                    "WHERE gate_id = ?",
                    (now, gate_id),
                )
                stale = True
            elif not was_held and row["expires_at"] <= now:
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'timed_out', updated_at = ? "
                    "WHERE gate_id = ?",
                    (now, gate_id),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    row["completed_node_id"],
                    "gateway",
                    "step.advance.timed_out",
                    {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                    now,
                )
                connection.execute(
                    "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                    (workflow_id,),
                )
                expired = True
            else:
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'confirmed', confirmed_at = ?, "
                    "updated_at = ? WHERE gate_id = ?",
                    (now, now, gate_id),
                )
                connection.execute(
                    "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                    (workflow_id,),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    row["completed_node_id"],
                    "gateway",
                    "step.advance.confirmed",
                    {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                    now,
                )
                if was_held:
                    self._add_event_with_connection(
                        connection,
                        workflow_id,
                        row["completed_node_id"],
                        "gateway",
                        "step.advance.resumed",
                        {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                        now,
                    )
                result = {
                    "gateId": gate_id,
                    "status": "confirmed",
                    "confirmedAt": now,
                    "resumedFromHold": was_held,
                }
        if stale:
            raise RuntimeError("任务状态已经变化，请刷新页面。")
        if expired:
            raise RuntimeError("30 秒倒计时已经结束，系统正在自动进入下一步。")
        assert result is not None
        return result

    def release_timed_out_advance(
        self,
        workflow_id: str,
        gate_id: str,
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
    ) -> bool:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    workflow_id,
                    lease_token=lease_token,
                    require_lease=True,
                )
            row = connection.execute(
                "SELECT * FROM workflow_advance_gates WHERE workflow_id = ? AND gate_id = ?",
                (workflow_id, gate_id),
            ).fetchone()
            if row is None or row["status"] != "pending" or row["expires_at"] > now:
                return False
            connection.execute(
                "UPDATE workflow_advance_gates SET status = 'timed_out', updated_at = ? "
                "WHERE gate_id = ?",
                (now, gate_id),
            )
            connection.execute(
                "UPDATE workflows SET state_version = state_version + 1 WHERE workflow_id = ?",
                (workflow_id,),
            )
            self._add_event_with_connection(
                connection,
                workflow_id,
                row["completed_node_id"],
                "gateway",
                "step.advance.timed_out",
                {"gateId": gate_id, "nextNodeId": row["next_node_id"]},
                now,
            )
            return True

    def _supersede_pending_advances(
        self, connection: sqlite3.Connection, workflow_id: str, reason: str, now: str
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM workflow_advance_gates WHERE workflow_id = ? "
            "AND status IN ('pending', 'held')",
            (workflow_id,),
        ).fetchall()
        if not rows:
            return
        connection.execute(
            "UPDATE workflow_advance_gates SET status = 'superseded', updated_at = ? "
            "WHERE workflow_id = ? AND status IN ('pending', 'held')",
            (now, workflow_id),
        )
        for row in rows:
            self._add_event_with_connection(
                connection,
                workflow_id,
                row["completed_node_id"],
                "gateway",
                "step.advance.superseded",
                {"gateId": row["gate_id"], "reason": reason},
                now,
            )

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

    def prepare_node_dispatch(
        self,
        workflow_id: str,
        node_id: str,
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
        sidecar_dispatch_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    workflow_id,
                    lease_token=lease_token,
                    require_lease=True,
                )
            workflow = connection.execute(
                "SELECT status, handoff_mode FROM workflows WHERE workflow_id = ?", (workflow_id,)
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
                same_remote_prepare = (
                    sidecar_dispatch_id is not None
                    and row["status"] == "queued"
                    and row["job_id"] is None
                    and row["dispatch_token"] == sidecar_dispatch_id
                )
                result = self._node_dispatch_spec(
                    row, already_dispatched=not same_remote_prepare
                )
                result["handoffMode"] = workflow["handoff_mode"]
                return result

            advance_gate = connection.execute(
                "SELECT * FROM workflow_advance_gates WHERE workflow_id = ? "
                "AND next_node_id = ? AND status IN ('pending', 'held') "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id, node_id),
            ).fetchone()
            if advance_gate is not None:
                timestamp = utc_now()
                if advance_gate["status"] == "held":
                    raise RuntimeError("任务已暂停，正在等待用户决定是否进入下一步。")
                if advance_gate["expires_at"] > timestamp:
                    raise RuntimeError("正在等待用户确认进入下一步。")
                connection.execute(
                    "UPDATE workflow_advance_gates SET status = 'timed_out', updated_at = ? "
                    "WHERE gate_id = ?",
                    (timestamp, advance_gate["gate_id"]),
                )
                self._add_event_with_connection(
                    connection,
                    workflow_id,
                    advance_gate["completed_node_id"],
                    "gateway",
                    "step.advance.timed_out",
                    {"gateId": advance_gate["gate_id"], "nextNodeId": node_id},
                    timestamp,
                )

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
            revision_rows = connection.execute(
                """
                SELECT retry_ordinal, instruction
                FROM workflow_node_revision_instructions
                WHERE workflow_id = ? AND node_id = ?
                ORDER BY retry_ordinal, created_at
                """,
                (workflow_id, node_id),
            ).fetchall()
            actual_prompt = self._build_actual_prompt(
                original_prompt,
                dependencies,
                dependency_rows,
                revision_rows,
                str(workflow["handoff_mode"]),
            )

            timestamp = utc_now()
            connection.execute(
                """
                UPDATE workflow_nodes
                SET status = 'queued', started_at = ?, actual_prompt = ?,
                    dispatch_token = ?
                WHERE workflow_id = ? AND node_id = ?
                """,
                (
                    timestamp,
                    actual_prompt,
                    sidecar_dispatch_id,
                    workflow_id,
                    node_id,
                ),
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
            result = self._node_dispatch_spec(refreshed, already_dispatched=False)
            result["handoffMode"] = workflow["handoff_mode"]
            return result

    @staticmethod
    def _build_actual_prompt(
        original_prompt: str,
        dependencies: list[str],
        dependency_rows: list[sqlite3.Row],
        revision_rows: list[sqlite3.Row],
        handoff_mode: str = "legacy_text",
    ) -> str:
        dependency_suffix = (
            WorkflowStore._build_dependency_suffix(dependencies, dependency_rows)
            if handoff_mode == "legacy_text"
            else ""
        )
        revision_suffix = WorkflowStore._build_revision_suffix(revision_rows)
        suffix = dependency_suffix + revision_suffix + SINGLE_OUTPUT_CONSTRAINT
        available = PROMPT_LIMIT - len(suffix)
        if available < len(original_prompt):
            base = original_prompt[: max(0, available - len(TRUNCATION_NOTICE))] + TRUNCATION_NOTICE
        else:
            base = original_prompt
        return (base + suffix)[:PROMPT_LIMIT]

    @staticmethod
    def _build_dependency_suffix(
        dependencies: list[str], dependency_rows: list[sqlite3.Row]
    ) -> str:
        if not dependencies:
            return ""
        responses = {
            row["node_id"]: (
                int(row["position"]) + 1,
                "该前置步骤已跳过，没有可用结果。"
                if row["status"] == "skipped"
                else str(row["response"] or ""),
            )
            for row in dependency_rows
        }
        blocks: list[str] = []
        remaining = DEPENDENCY_RESULTS_LIMIT
        for dependency in dependencies:
            step_number, result = responses.get(dependency, (1, ""))
            item_limit = min(RESULT_LIMIT, remaining)
            if len(result) > item_limit:
                result = (
                    result[: max(0, item_limit - len(TRUNCATION_NOTICE))]
                    + TRUNCATION_NOTICE
                )
            block = f"【第{step_number}步结果】\n{result}"
            if len(block) > remaining:
                block = (
                    block[: max(0, remaining - len(TRUNCATION_NOTICE))]
                    + TRUNCATION_NOTICE
                )
            blocks.append(block)
            remaining -= len(block)
            if remaining <= 0:
                break
        return (
            "\n\n前一步已经完成，下面是它提供的结果：\n\n"
            + "\n\n".join(blocks)
            + "\n\n请基于以上结果完成你当前负责的步骤。"
        )

    @staticmethod
    def _build_revision_suffix(revision_rows: list[sqlite3.Row]) -> str:
        if not revision_rows:
            return ""
        header = (
            "\n\n【本次及历史返工要求】\n"
            "以下要求按确认顺序排列；较新要求与较早要求冲突时，以较新要求为准。\n\n"
        )
        remaining = REVISION_CONTEXT_LIMIT - len(header)
        selected: list[str] = []
        omitted = False
        for row in reversed(revision_rows):
            block = f"【第{int(row['retry_ordinal'])}次返工】\n{row['instruction']}"
            required = len(block) + (2 if selected else 0)
            if required > remaining:
                omitted = True
                break
            selected.append(block)
            remaining -= required
        selected.reverse()
        if omitted:
            notice = "【较早返工要求因内容过长已省略】"
            required = len(notice) + (2 if selected else 0)
            while len(selected) > 1 and required > remaining:
                removed = selected.pop(0)
                remaining += len(removed) + 2
            if required <= remaining:
                selected.insert(0, notice)
        return header + "\n\n".join(selected)

    @staticmethod
    def _node_dispatch_spec(row: sqlite3.Row, *, already_dispatched: bool) -> dict[str, Any]:
        return {
            "workflowId": row["workflow_id"],
            "nodeId": row["node_id"],
            "stepNumber": int(row["position"]) + 1,
            "agentId": row["agent_id"],
            "prompt": row["actual_prompt"] or row["original_prompt"] or row["prompt"],
            "cwd": row["cwd"],
            "write": bool(row["write_enabled"]),
            "permissionProfile": row["permission_profile"],
            "model": row["model"],
            "timeoutSec": row["timeout_sec"],
            "status": row["status"],
            "jobId": row["job_id"],
            "alreadyDispatched": already_dispatched,
        }

    def attach_node_job(
        self,
        workflow_id: str,
        node_id: str,
        snapshot: dict[str, Any],
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    workflow_id,
                    lease_token=lease_token,
                    require_lease=True,
                )
            connection.execute(
                """
                UPDATE workflow_nodes
                SET job_id = ?, thread_id = ?, turn_id = ?, status = ?,
                    dispatch_token = NULL
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

    def sync_node_job(
        self,
        workflow_id: str,
        node_id: str,
        snapshot: dict[str, Any],
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        status = str(snapshot.get("status") or "running")
        finished_at = snapshot.get("finished_at") if status in TERMINAL_NODE_STATUSES else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    workflow_id,
                    lease_token=lease_token,
                    require_lease=True,
                )
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
                if status == "completed":
                    self._create_advance_gate_with_connection(
                        connection, workflow_id, node_id
                    )

    def _create_advance_gate_with_connection(
        self, connection: sqlite3.Connection, workflow_id: str, node_id: str
    ) -> None:
        workflow = connection.execute(
            "SELECT status, advance_mode FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if (
            workflow is None
            or workflow["status"] in {"completed", "failed", "cancelled"}
            or workflow["advance_mode"] != "semi_automatic"
        ):
            return
        completed = connection.execute(
            "SELECT position FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
            (workflow_id, node_id),
        ).fetchone()
        if completed is None:
            return
        next_node = connection.execute(
            "SELECT node_id, status, depends_on_json FROM workflow_nodes "
            "WHERE workflow_id = ? AND position > ? ORDER BY position LIMIT 1",
            (workflow_id, completed["position"]),
        ).fetchone()
        if (
            next_node is None
            or next_node["status"] != "pending"
            or json.loads(next_node["depends_on_json"]) != [node_id]
        ):
            return
        now = utc_now()
        self._supersede_pending_advances(connection, workflow_id, "new_gate", now)
        gate_id = uuid.uuid4().hex
        expires_at = (datetime.now(UTC) + timedelta(seconds=ADVANCE_TIMEOUT_SEC)).isoformat()
        connection.execute(
            """
            INSERT INTO workflow_advance_gates (
                gate_id, workflow_id, completed_node_id, next_node_id, status,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (gate_id, workflow_id, node_id, next_node["node_id"], expires_at, now, now),
        )
        self._add_event_with_connection(
            connection,
            workflow_id,
            node_id,
            "gateway",
            "step.advance.waiting",
            {
                "gateId": gate_id,
                "completedNodeId": node_id,
                "nextNodeId": next_node["node_id"],
                "expiresAt": expires_at,
            },
            now,
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
                  AND (supervisor_job_id IS NOT COALESCE(?1, supervisor_job_id)
                    OR supervisor_thread_id IS NOT COALESCE(?2, supervisor_thread_id)
                    OR supervisor_turn_id IS NOT COALESCE(?3, supervisor_turn_id)
                    OR supervisor_status IS NOT ?4 OR status = 'queued'
                    OR started_at IS NULL
                    OR response IS NOT COALESCE(?6, response)
                    OR error IS NOT COALESCE(?7, error))
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
                  AND (assistant_job_id IS NOT COALESCE(?1, assistant_job_id)
                    OR assistant_thread_id IS NOT COALESCE(?2, assistant_thread_id)
                    OR assistant_turn_id IS NOT COALESCE(?3, assistant_turn_id)
                    OR assistant_status IS NOT ?4)
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
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"找不到工作流：{workflow_id}")
            if current["status"] in {"completed", "failed", "cancelled"}:
                return
            self._supersede_pending_advances(
                connection, workflow_id, "workflow_finished", timestamp
            )
            connection.execute(
                """
                UPDATE workflows
                SET status = ?, supervisor_status = ?, response = ?, error = ?, finished_at = ?,
                    state_version = state_version + 1
                WHERE workflow_id = ?
                """,
                (status, supervisor_status, response, error, timestamp, workflow_id),
            )
            self._release_supervisor_lease_with_connection(
                connection, workflow_id, f"workflow_{status}", timestamp
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

    def _release_supervisor_lease_with_connection(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        reason: str,
        timestamp: str,
    ) -> str | None:
        lease = connection.execute(
            "SELECT supervisor_id FROM supervisor_leases WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if lease is None:
            return None
        supervisor_id = str(lease["supervisor_id"])
        connection.execute(
            "DELETE FROM supervisor_leases WHERE workflow_id = ?", (workflow_id,)
        )
        self._add_event_with_connection(
            connection,
            workflow_id,
            None,
            "gateway",
            "supervisor.lease_released",
            {"supervisorAgentId": supervisor_id, "reason": reason},
            timestamp,
        )
        return supervisor_id

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

    def add_events(
        self,
        events: list[dict[str, Any]],
        *,
        sidecar_supervisor_id: str | None = None,
        lease_token: str | None = None,
    ) -> list[int]:
        if not events:
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                workflow_ids = {str(event["workflow_id"]) for event in events}
                if len(workflow_ids) != 1:
                    raise ValueError("Sidecar 事件批次只能属于一个工作流。")
                self._validate_sidecar_access_with_connection(
                    connection,
                    sidecar_supervisor_id,
                    next(iter(workflow_ids)),
                    lease_token=lease_token,
                    require_lease=True,
                )
                for event in events:
                    node_id = event.get("node_id")
                    if node_id is None:
                        continue
                    node = connection.execute(
                        "SELECT 1 FROM workflow_nodes "
                        "WHERE workflow_id = ? AND node_id = ?",
                        (str(event["workflow_id"]), str(node_id)),
                    ).fetchone()
                    if node is None:
                        raise ValueError(f"找不到节点：{node_id}")
            return [
                self._add_event_with_connection(
                    connection,
                    str(event["workflow_id"]),
                    event.get("node_id"),
                    str(event["source"]),
                    str(event["event_type"]),
                    event["payload"],
                    str(event.get("created_at") or utc_now()),
                    (
                        str(event["external_event_id"])
                        if event.get("external_event_id")
                        else None
                    ),
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
        external_event_id: str | None = None,
    ) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) > EVENT_PAYLOAD_LIMIT:
            encoded = json.dumps(
                {"truncated": True, "preview": encoded[:262_000]}, ensure_ascii=False
            )
        compressed = zlib.compress(encoded.encode("utf-8"), level=3) if len(encoded) >= 1024 else None
        if compressed is not None and len(compressed) >= len(encoded.encode("utf-8")) * 0.75:
            compressed = None
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO workflow_events (
                workflow_id, node_id, source, event_type, external_event_id,
                payload_json, created_at, payload_zlib
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                node_id,
                source,
                event_type,
                external_event_id,
                "{}" if compressed is not None else encoded,
                created_at,
                compressed,
            ),
        )
        if cursor.rowcount == 0 and external_event_id:
            existing = connection.execute(
                "SELECT sequence FROM workflow_events "
                "WHERE workflow_id = ? AND external_event_id = ?",
                (workflow_id, external_event_id),
            ).fetchone()
            if existing is not None:
                return int(existing["sequence"])
        return int(cursor.lastrowid)

    def event_page(
        self, workflow_id: str, *, after: int = 0, limit: int = 200,
        view: str = "all", before: int | None = None, tail: bool = False,
    ) -> dict[str, Any]:
        if view not in {"all", "monitor", "bot"}:
            raise ValueError("事件视图无效。")
        if after < 0 or not 1 <= limit <= 1000 or (before is not None and before <= 0):
            raise ValueError("事件游标或分页大小无效。")
        if (tail or before is not None) and after:
            raise ValueError("向前和向后游标不能同时使用。")
        predicate = {"all": "1 = 1", "monitor": MONITOR_EVENTS_SQL, "bot": BOT_EVENTS_SQL}[view]
        descending = tail or before is not None
        with self._connect() as connection:
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone() is None:
                raise ValueError("找不到工作流。")
            watermark = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM workflow_events WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM workflow_events WHERE workflow_id = ? AND {predicate} "
                f"AND sequence > ? AND sequence <= ? AND sequence < ? ORDER BY sequence "
                f"{'DESC' if descending else 'ASC'} LIMIT ?",
                (workflow_id, after, watermark, before if before is not None else watermark + 1, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        if descending:
            rows.reverse()
        events = [self._event_snapshot(row) for row in rows]
        next_cursor = (rows[-1]["sequence"] if more and not descending and rows else watermark)
        return {"events": events, "nextCursor": next_cursor,
                "hasMore": more if not descending else False,
                "hasOlder": more if descending else False,
                "oldestCursor": rows[0]["sequence"] if rows else before}

    @staticmethod
    def _event_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {"sequence": row["sequence"], "workflowId": row["workflow_id"],
                "nodeId": row["node_id"], "source": row["source"], "type": row["event_type"],
                "payload": json.loads(zlib.decompress(row["payload_zlib"]).decode("utf-8")
                                      if row["payload_zlib"] is not None else row["payload_json"]),
                "createdAt": row["created_at"]}

    def compact_terminal_events(self, before: str, *, after: int = 0, limit: int = 100) -> dict[str, int]:
        """分批无损压缩已结束运行的旧事件，游标和公开查询结果保持不变。"""
        parsed = datetime.fromisoformat(before)
        if parsed.tzinfo is None or after < 0 or not 1 <= limit <= 100:
            raise ValueError("压缩截止时间、游标或批量大小无效。")
        before = parsed.astimezone(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT e.sequence, e.payload_json FROM workflow_events e JOIN workflows w USING(workflow_id) "
                "WHERE e.sequence > ? AND e.payload_zlib IS NULL AND LENGTH(e.payload_json) >= 1024 "
                "AND w.status IN ('completed', 'failed', 'cancelled') AND w.finished_at < ? "
                "ORDER BY e.sequence LIMIT ?", (after, before, limit),
            ).fetchall()
            saved = 0
            compacted = 0
            for row in rows:
                original = row["payload_json"].encode("utf-8")
                packed = zlib.compress(original, level=3)
                if len(packed) < len(original) * 0.75:
                    connection.execute("UPDATE workflow_events SET payload_json = '{}', payload_zlib = ? "
                                       "WHERE sequence = ?", (packed, row["sequence"]))
                    compacted += 1
                    saved += len(original) - len(packed) - 2
        return {"scanned": len(rows), "compacted": compacted, "savedBytes": saved,
                "nextCursor": rows[-1]["sequence"] if rows else after}

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
        return [self._event_snapshot(row) for row in rows]
