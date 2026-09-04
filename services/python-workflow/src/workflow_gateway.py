import argparse
import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from codex_orchestrator_mcp import (
    CONFIG_PATH,
    Orchestrator,
    utc_now,
)
from workflow_store import (
    REVISION_INSTRUCTION_LIMIT,
    AsyncEventBatcher,
    WorkflowStore,
)


DEFAULT_DB_PATH = Path(__file__).with_name("workflows.db")
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}
SUPERVISOR_PROBE_INTERVAL_SEC = 10.0
SUPERVISOR_PROBE_TIMEOUT_SEC = 2.5
SUPERVISOR_OFFLINE_FAILURE_THRESHOLD = 2
SIDECAR_HEARTBEAT_INTERVAL_SEC = 5
SIDECAR_LEASE_TIMEOUT_SEC = 20
SIDECAR_WATCHDOG_INTERVAL_SEC = 1.0
LOGGER = logging.getLogger(__name__)


class WorkflowGateway:
    def __init__(
        self,
        store: WorkflowStore,
        orchestrator: Orchestrator,
        assistant_orchestrator: Orchestrator | None = None,
    ) -> None:
        self.store = store
        self.orchestrator = orchestrator
        if assistant_orchestrator is not None:
            self.assistant_orchestrator = assistant_orchestrator
        elif isinstance(orchestrator, Orchestrator):
            self.assistant_orchestrator = Orchestrator(
                orchestrator.config_path,
                client_factory=orchestrator._client_factory,
                serialize_agent_jobs=False,
            )
        else:
            self.assistant_orchestrator = orchestrator
        self.event_batcher = AsyncEventBatcher(store)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._chat_tasks: dict[str, asyncio.Task[None]] = {}
        self._control_locks: dict[str, asyncio.Lock] = {}
        self._control_in_progress: set[str] = set()
        self._schedule_lock = asyncio.Lock()
        self._supervisor_probe_task: asyncio.Task[None] | None = None
        self._sidecar_watchdog_task: asyncio.Task[None] | None = None
        self._supervisor_runtime: dict[str, dict[str, Any]] = {}
        self._closing = False

    async def start(self) -> None:
        recovered = await asyncio.to_thread(
            self.store.recover_active_workflows_after_restart
        )
        if recovered:
            LOGGER.warning("网关启动时已将 %s 个遗留运行标记为失败。", len(recovered))
        self.store.recover_processing_chat_messages()
        try:
            imported = await asyncio.to_thread(self.store.import_legacy_generated_images)
            if imported:
                LOGGER.info("已回填 %s 个历史工作流图片附件。", imported)
        except Exception:
            LOGGER.exception("回填历史工作流图片附件失败。")
        for workflow_id in self.store.list_chat_workflows():
            self._ensure_chat_worker(workflow_id)
        await self._schedule_pending()
        self._supervisor_probe_task = asyncio.create_task(
            self._supervisor_probe_loop(), name="supervisor-reachability-probe"
        )
        self._sidecar_watchdog_task = asyncio.create_task(
            self._sidecar_watchdog_loop(), name="supervisor-sidecar-watchdog"
        )

    async def stop(self) -> None:
        self._closing = True
        if self._supervisor_probe_task is not None:
            self._supervisor_probe_task.cancel()
            await asyncio.gather(self._supervisor_probe_task, return_exceptions=True)
            self._supervisor_probe_task = None
        if self._sidecar_watchdog_task is not None:
            self._sidecar_watchdog_task.cancel()
            await asyncio.gather(self._sidecar_watchdog_task, return_exceptions=True)
            self._sidecar_watchdog_task = None
        async with self._schedule_lock:
            pass
        await asyncio.to_thread(self.store.recover_active_workflows_after_restart)
        tasks = list(self._tasks.values()) + list(self._chat_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def flush_events(self) -> None:
        try:
            await self.event_batcher.flush()
        except Exception:
            LOGGER.exception("刷新主监督监控事件失败。")

    async def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        spec = WorkflowStore.normalize_spec(raw_spec)
        agent_values = self.orchestrator.list_agents()
        agents_by_id = {item["agent_id"]: item for item in agent_values}
        available_agents = set(agents_by_id)
        requested_agents = {spec["supervisorAgentId"]} | {
            node["agentId"] for node in spec["nodes"]
        }
        unknown = sorted(requested_agents - available_agents)
        if unknown:
            raise ValueError(f"工作流引用了未知执行机：{', '.join(unknown)}")
        supervisor = agents_by_id[spec["supervisorAgentId"]]
        supervisor_capabilities = set(
            supervisor.get("capabilities")
            or (
                ["supervisor", "executor"]
                if spec["supervisorAgentId"] == "local"
                else ["executor"]
            )
        )
        if not bool(supervisor.get("enabled", True)):
            raise PermissionError(
                f"主监督执行机 {spec['supervisorAgentId']} 已停用。"
            )
        if "supervisor" not in supervisor_capabilities:
            raise PermissionError(
                f"执行机 {spec['supervisorAgentId']} 不具备主监督能力。"
            )
        if (
            supervisor.get("orchestration_mode") == "remote_sidecar"
            and spec.get("handoffMode") != "legacy_text"
        ):
            raise ValueError("远程主监督当前只支持 legacy_text 文字交接。")
        profiles_by_agent = {
            item["agent_id"]: set(item.get("permission_profiles") or ["read_only"])
            for item in agent_values
        }
        for node in spec["nodes"]:
            agent = agents_by_id[node["agentId"]]
            capabilities = set(
                agent.get("capabilities")
                or (["supervisor", "executor"] if node["agentId"] == "local" else ["executor"])
            )
            if not bool(agent.get("enabled", True)):
                raise PermissionError(f"执行机 {node['agentId']} 已停用。")
            if "executor" not in capabilities:
                raise PermissionError(
                    f"执行机 {node['agentId']} 不具备步骤执行能力。"
                )
            profile = node["permissionProfile"]
            if profile not in profiles_by_agent[node["agentId"]]:
                raise PermissionError(
                    f"执行机 {node['agentId']} 不允许节点 {node['id']} 使用权限档位 {profile}。"
                )

        snapshot = await asyncio.to_thread(self.store.create_workflow, spec)
        await self._schedule_pending()
        return await asyncio.to_thread(
            self.store.get_workflow, str(snapshot["workflowId"])
        )

    async def _schedule_pending(self) -> None:
        if self._closing:
            return
        async with self._schedule_lock:
            for agent in self.orchestrator.list_agents():
                agent_id = str(agent["agent_id"])
                capabilities = set(
                    agent.get("capabilities")
                    or (
                        ["supervisor", "executor"]
                        if agent_id == "local"
                        else ["executor"]
                    )
                )
                if not bool(agent.get("enabled", True)) or "supervisor" not in capabilities:
                    continue
                if agent.get("orchestration_mode") == "remote_sidecar":
                    sidecar = await asyncio.to_thread(
                        self.store.sidecar_status,
                        agent_id,
                        timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC,
                    )
                    if sidecar["connectionStatus"] != "online":
                        while await asyncio.to_thread(
                            self.store.fail_next_queued_for_offline_sidecar, agent_id
                        ):
                            pass
                        continue
                    spec = await asyncio.to_thread(
                        self.store.claim_next_workflow,
                        agent_id,
                        sidecar_instance_id=str(sidecar["instanceId"]),
                        lease_timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC,
                    )
                else:
                    spec = await asyncio.to_thread(
                        self.store.claim_next_workflow, agent_id
                    )
                if spec is None:
                    continue
                workflow_id = str(spec["workflowId"])
                try:
                    task = asyncio.create_task(
                        self._run_supervisor(spec),
                        name=f"workflow-supervisor:{workflow_id}",
                    )
                except BaseException:
                    await asyncio.to_thread(
                        self.store.release_supervisor_claim, workflow_id
                    )
                    raise
                self._tasks[workflow_id] = task
                task.add_done_callback(
                    lambda done, selected=workflow_id: self._drop_task(selected, done)
                )

    def _drop_task(self, workflow_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(workflow_id) is task:
            self._tasks.pop(workflow_id, None)
        if not self._closing:
            asyncio.create_task(
                self._schedule_after_task(), name="workflow-schedule-after-finish"
            )

    async def _schedule_after_task(self) -> None:
        try:
            await self._schedule_pending()
        except Exception:
            LOGGER.exception("工作流终态后调度下一项失败。")

    def _drop_chat_task(self, workflow_id: str, task: asyncio.Task[Any]) -> None:
        if self._chat_tasks.get(workflow_id) is task:
            self._chat_tasks.pop(workflow_id, None)

    def public_agents(self) -> list[dict[str, Any]]:
        leased_supervisors = (
            self.store.leased_supervisor_ids()
            if self.store is not None
            else set()
        )
        result: list[dict[str, Any]] = []
        for item in self.orchestrator.list_agents():
            agent_id = str(item["agent_id"])
            capabilities = list(
                item.get("capabilities")
                or (
                    ["supervisor", "executor"]
                    if agent_id == "local"
                    else ["executor"]
                )
            )
            value = {
                "agentId": item["agent_id"],
                "defaultCwd": item["cwd"],
                "defaultModel": item.get("model"),
                "enabled": bool(item.get("enabled", True)),
                "capabilities": capabilities,
                "supervisorCapacity": int(
                    item.get("supervisor_capacity")
                    or (
                        1
                        if "supervisor"
                        in set(capabilities)
                        else 0
                    )
                ),
                "allowWrite": bool(item.get("allow_write")),
                "allowFullAccess": "full_access"
                in set(item.get("permission_profiles") or []),
                "allowCwdOverride": bool(item.get("allow_cwd_override")),
                "permissionProfiles": list(item.get("permission_profiles") or []),
            }
            if "supervisor" in capabilities:
                runtime = (
                    self.store.sidecar_status(
                        agent_id, timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC
                    )
                    if item.get("orchestration_mode") == "remote_sidecar"
                    else self._supervisor_runtime.get(agent_id, {})
                )
                value.update(
                    {
                        "connectionStatus": runtime.get("connectionStatus", "unknown"),
                        "availability": (
                            "busy" if agent_id in leased_supervisors else "idle"
                        ),
                        "checkedAt": runtime.get("checkedAt"),
                        "lastOnlineAt": runtime.get("lastOnlineAt"),
                    }
                )
            result.append(value)
        return result

    async def _supervisor_probe_loop(self) -> None:
        while not self._closing:
            try:
                await self._probe_supervisors_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("刷新主监督在线状态失败。")
            await asyncio.sleep(SUPERVISOR_PROBE_INTERVAL_SEC)

    async def _probe_supervisors_once(self) -> None:
        probe = getattr(self.orchestrator, "probe_agent", None)
        if not callable(probe):
            return
        supervisor_ids: list[str] = []
        for item in self.orchestrator.list_agents():
            agent_id = str(item["agent_id"])
            capabilities = set(
                item.get("capabilities")
                or (["supervisor", "executor"] if agent_id == "local" else ["executor"])
            )
            if bool(item.get("enabled", True)) and "supervisor" in capabilities:
                if item.get("orchestration_mode") == "remote_sidecar":
                    continue
                supervisor_ids.append(agent_id)
        configured = set(supervisor_ids)
        self._supervisor_runtime = {
            agent_id: value
            for agent_id, value in self._supervisor_runtime.items()
            if agent_id in configured
        }

        async def check(agent_id: str) -> None:
            checked_at = utc_now()
            previous = self._supervisor_runtime.get(agent_id, {})
            try:
                await asyncio.wait_for(
                    probe(agent_id, timeout_sec=SUPERVISOR_PROBE_TIMEOUT_SEC),
                    timeout=SUPERVISOR_PROBE_TIMEOUT_SEC + 0.5,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures = int(previous.get("consecutiveFailures", 0)) + 1
                self._supervisor_runtime[agent_id] = {
                    "connectionStatus": (
                        "offline"
                        if failures >= SUPERVISOR_OFFLINE_FAILURE_THRESHOLD
                        else "unknown"
                    ),
                    "checkedAt": checked_at,
                    "lastOnlineAt": previous.get("lastOnlineAt"),
                    "consecutiveFailures": failures,
                }
            else:
                self._supervisor_runtime[agent_id] = {
                    "connectionStatus": "online",
                    "checkedAt": checked_at,
                    "lastOnlineAt": checked_at,
                    "consecutiveFailures": 0,
                }

        await asyncio.gather(*(check(agent_id) for agent_id in supervisor_ids))

    async def _sidecar_watchdog_loop(self) -> None:
        """及时终止心跳过期的远程租约，并停止中央监督任务。"""
        while not self._closing:
            try:
                expired = await asyncio.to_thread(self.store.expire_sidecar_leases)
                for item in expired:
                    task = self._tasks.get(item["workflowId"])
                    if task is not None and not task.done():
                        task.cancel()
                if expired:
                    await self._schedule_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("清理过期 Sidecar 租约失败。")
            await asyncio.sleep(SIDECAR_WATCHDOG_INTERVAL_SEC)

    def authenticate_sidecar(self, authorization: str | None) -> str:
        """使用独立 Bearer Token 将内部请求绑定到唯一主监督。"""
        if not authorization:
            raise PermissionError("Sidecar 认证失败。")
        scheme, separator, presented = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise PermissionError("Sidecar 认证失败。")
        presented = presented.strip()
        if not presented or len(presented) > 8192:
            raise PermissionError("Sidecar 认证失败。")
        matched: list[str] = []
        for agent in self.orchestrator.load_agents().values():
            if agent.orchestration_mode != "remote_sidecar" or not agent.enabled:
                continue
            try:
                expected = self.orchestrator.resolve_sidecar_token(agent)
            except RuntimeError:
                continue
            if hmac.compare_digest(presented, expected):
                matched.append(agent.agent_id)
        if len(matched) != 1:
            raise PermissionError("Sidecar 认证失败。")
        return matched[0]

    async def accept_sidecar_heartbeat(
        self, supervisor_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        instance_id = str(payload.get("instanceId") or "").strip()
        started_at = str(payload.get("startedAt") or "").strip()
        if not 1 <= len(instance_id) <= 128:
            raise ValueError("instanceId 必须是 1 到 128 个字符。")
        try:
            parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("startedAt 必须是 ISO-8601 时间。") from error
        if parsed_started_at.tzinfo is None:
            raise ValueError("startedAt 必须包含时区。")
        result = await asyncio.to_thread(
            self.store.record_sidecar_heartbeat,
            supervisor_id,
            instance_id,
            started_at,
            lease_timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC,
        )
        failed_id = result.get("failedWorkflowId")
        if failed_id:
            task = self._tasks.get(str(failed_id))
            if task is not None and not task.done():
                task.cancel()
        await self._schedule_pending()
        result["heartbeatIntervalSec"] = SIDECAR_HEARTBEAT_INTERVAL_SEC
        result["leaseTimeoutSec"] = SIDECAR_LEASE_TIMEOUT_SEC
        return result

    async def _run_supervisor(
        self, spec: dict[str, Any], *, resume_thread_id: str | None = None
    ) -> None:
        workflow_id = spec["workflowId"]
        current_thread_id = resume_thread_id
        consecutive_no_progress_turns = 0
        try:
            while True:
                before = self.store.get_workflow(workflow_id)
                before_fingerprint = self._node_progress_fingerprint(before)
                message_buffer = ""
                last_message_flush_at = 0.0

                async def record(message: dict[str, Any], received_at: str) -> None:
                    nonlocal message_buffer, last_message_flush_at
                    method = str(message.get("method") or "unknown")
                    params = message.get("params") or {}
                    await self.event_batcher.add(
                        workflow_id,
                        node_id=None,
                        source="supervisor",
                        event_type=f"appserver.{method}",
                        payload={"receivedAt": received_at, "message": message},
                    )
                    if method == "item/agentMessage/delta":
                        delta = params.get("delta")
                        if isinstance(delta, str):
                            message_buffer = (message_buffer + delta)[-20_000:]
                            now = time.monotonic()
                            if now - last_message_flush_at >= 0.25:
                                await asyncio.to_thread(
                                    self.store.set_supervisor_message,
                                    workflow_id,
                                    message_buffer,
                                )
                                last_message_flush_at = now
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("text"):
                            message_buffer = str(item["text"])[-20_000:]
                            await asyncio.to_thread(
                                self.store.set_supervisor_message,
                                workflow_id,
                                message_buffer,
                            )
                            last_message_flush_at = time.monotonic()

                prompt = (
                    self._supervisor_prompt(spec)
                    if current_thread_id is None
                    else self._supervisor_resume_prompt(workflow_id)
                )
                job = await self.orchestrator.dispatch(
                    agent_id=spec["supervisorAgentId"],
                    prompt=prompt,
                    thread_id=current_thread_id,
                    cwd=spec.get("supervisorCwd"),
                    write=bool(spec.get("supervisorWrite", False)),
                    model=spec.get("supervisorModel"),
                    timeout_sec=int(spec.get("supervisorTimeoutSec", 7200)),
                    approval_policy="on-request",
                    approvals_reviewer="auto_review",
                    event_callback=record,
                )
                self.store.update_supervisor(workflow_id, job.snapshot())
                last_supervisor_fingerprint = self._supervisor_job_fingerprint(
                    job.snapshot()
                )
                self.store.add_event(
                    workflow_id,
                    node_id=None,
                    source="gateway",
                    event_type="supervisor.started",
                    payload={"jobId": job.job_id, "agentId": job.agent_id},
                )
                while not job.completed.is_set():
                    current_snapshot = job.snapshot()
                    current_fingerprint = self._supervisor_job_fingerprint(
                        current_snapshot
                    )
                    if current_fingerprint != last_supervisor_fingerprint:
                        self.store.update_supervisor(workflow_id, current_snapshot)
                        last_supervisor_fingerprint = current_fingerprint
                    try:
                        await asyncio.wait_for(job.completed.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
                job_snapshot = job.snapshot()
                await self.flush_events()
                if message_buffer:
                    await asyncio.to_thread(
                        self.store.set_supervisor_message, workflow_id, message_buffer
                    )
                self.store.update_supervisor(workflow_id, job_snapshot)

                latest = self.store.get_workflow(workflow_id)
                if self._advance_is_held(latest):
                    return
                if workflow_id in self._control_in_progress:
                    return
                if (
                    job.status == "completed"
                    and self._workflow_can_continue(spec, latest)
                ):
                    after_fingerprint = self._node_progress_fingerprint(latest)
                    if after_fingerprint == before_fingerprint:
                        consecutive_no_progress_turns += 1
                    else:
                        consecutive_no_progress_turns = 0
                    if consecutive_no_progress_turns >= 3:
                        self.store.finish_workflow(
                            workflow_id,
                            supervisor_status="failed",
                            response=job_snapshot.get("response"),
                            error="主监督连续结束且没有推进任何步骤。",
                        )
                        return
                    current_thread_id = job.thread_id or current_thread_id
                    self.store.add_event(
                        workflow_id,
                        node_id=None,
                        source="gateway",
                        event_type="supervisor.resume_requested",
                        payload={
                            "reason": "turn_completed_with_unfinished_nodes",
                            "attempt": consecutive_no_progress_turns + 1,
                        },
                    )
                    continue

                self.store.finish_workflow(
                    workflow_id,
                    supervisor_status=str(job_snapshot["status"]),
                    response=job_snapshot.get("response"),
                    error=job_snapshot.get("error"),
                )
                return
        except asyncio.CancelledError:
            held = False
            active = False
            try:
                current = self.store.get_workflow(workflow_id)
                held = self._advance_is_held(current)
                active = current["status"] in {"running", "cancelling"}
            except ValueError:
                pass
            if workflow_id not in self._control_in_progress and not held and active:
                self.store.finish_workflow(
                    workflow_id,
                    supervisor_status="cancelled",
                    response=None,
                    error="工作流监督任务被取消。",
                )
            raise
        except Exception as error:
            try:
                if self._advance_is_held(self.store.get_workflow(workflow_id)):
                    return
            except ValueError:
                pass
            self.store.add_event(
                workflow_id,
                node_id=None,
                source="gateway",
                event_type="supervisor.failed_to_start",
                payload={"error": str(error), "errorType": type(error).__name__},
            )
            self.store.finish_workflow(
                workflow_id,
                supervisor_status="failed",
                response=None,
                error=str(error),
            )

    @staticmethod
    def _node_progress_fingerprint(snapshot: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            (
                node["id"], node["status"], node.get("jobId"),
                node.get("response"), node.get("error"),
            )
            for node in snapshot.get("nodes", [])
        )

    @staticmethod
    def _advance_is_held(snapshot: dict[str, Any]) -> bool:
        pending = snapshot.get("pendingAdvance")
        return isinstance(pending, dict) and pending.get("state") == "held"

    @staticmethod
    def _supervisor_job_fingerprint(snapshot: dict[str, Any]) -> tuple[Any, ...]:
        return (
            snapshot.get("status"),
            snapshot.get("thread_id"),
            snapshot.get("turn_id"),
            snapshot.get("response"),
            snapshot.get("error"),
        )

    @staticmethod
    def _workflow_can_continue(spec: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        if snapshot["status"] != "running":
            return False
        nodes = snapshot.get("nodes", [])
        if spec.get("failurePolicy") == "stop" and any(
            node["status"] in {"failed", "cancelled", "interrupted"} for node in nodes
        ):
            return False
        return any(
            node["status"] in {"pending", "queued", "running", "cancelling"}
            for node in nodes
        )

    @staticmethod
    def _supervisor_resume_prompt(workflow_id: str) -> str:
        return (
            "继续监督此前的工作流。先调用 workflow_status 读取最新状态。"
            "已经完成或跳过的步骤不得重新执行；有正在执行的步骤就继续 wait_node；"
            "有依赖已满足的待执行步骤就继续 dispatch_node。"
            "不要因为刚刚回答了监控页面的用户咨询而结束工作。"
            "只要仍有可继续的步骤，就必须持续调度和等待；全部步骤结束后再给最终总结。\n"
            f"工作流：{workflow_id}"
        )

    @staticmethod
    def _supervisor_prompt(spec: dict[str, Any]) -> str:
        workflow_view = {
            "workflowId": spec["workflowId"],
            "failurePolicy": spec["failurePolicy"],
            "advanceMode": spec.get("advanceMode", "automatic"),
            "nodes": [
                {
                    "id": node["id"],
                    "displayName": node.get("displayName") or node["id"],
                    "roleName": node.get("roleName") or "未指定角色",
                    "agentId": node["agentId"],
                    "executorType": node["executorType"],
                    "dependsOn": node["dependsOn"],
                    # 完整 prompt 保存在 SQLite，dispatch_node 会读取原文。主会话只需
                    # 看摘要来做依赖调度，避免大量节点把监督 turn 的输入撑爆。
                    "objective": node["prompt"][:2_000],
                }
                for node in spec["nodes"]
            ],
        }
        return (
            "你是这个工作流的主监督会话，不要亲自执行任何节点的业务任务。\n"
            "仅使用 Codex Orchestrator MCP 提供的 dispatch_node、wait_node、"
            "node_status、cancel_node 工具调度节点。\n"
            "每次只能派发依赖已经 completed 的节点；数据库也会强制检查依赖。\n"
            "节点派发后必须调用 wait_node，直到得到 completed、failed、cancelled "
            "或 interrupted。每次调用 wait_node 时 timeout_sec 使用 10 秒，"
            "等待超时时继续调用，以便及时响应用户咨询。\n"
            "如果 failurePolicy=stop，任一节点失败后不得启动后续节点。\n"
            "如果 advanceMode=semi_automatic，dispatch_node 会在成功步骤之间等待最多"
            "30 秒；用户也可以选择暂停且暂不进入下一步。等待或暂停期间应告诉用户"
            "尚未进入下一步，不得声称下一步已经开始。\n"
            "你面对的是完全不懂技术的普通用户。所有对外可见消息必须使用简单中文，"
            "像耐心的助手一样说明进度。\n"
            "对外把 node 称为“步骤”，按工作流中的顺序称为“第1步、第2步……”。\n"
            "对外消息不要出现 MCP、工具调用、dispatch、wait、status、thread、turn、"
            "agent、executor、job、queued、completed、failed 等技术词或英文状态码。\n"
            "不要讲内部如何调度，只告诉用户：正在进行第几步、这一步是否完成、"
            "得到什么结果、接下来做什么。\n"
            "推荐的进度表达是：“现在开始第1步。”“第1步已完成，结果是……"
            "接下来开始第2步。”\n"
            "如果失败，要用日常语言说明“第几步没有完成”和可理解的原因，"
            "不要直接抛出内部错误信息。\n"
            "全部步骤完成后，用“任务已全部完成”开头，按步骤汇总结果，"
            "不要展示内部编号、会话编号或调用记录。\n\n"
            f"工作流定义：\n{json.dumps(workflow_view, ensure_ascii=False, indent=2)}"
        )

    async def accept_message(
        self, workflow_id: str, message_id: str, text: str
    ) -> dict[str, Any]:
        accepted = await asyncio.to_thread(
            self.store.accept_chat_message, workflow_id, message_id, text
        )
        self._ensure_chat_worker(workflow_id)
        return accepted

    async def confirm_advance(self, workflow_id: str, gate_id: str) -> dict[str, Any]:
        if not gate_id or len(gate_id) > 128:
            raise ValueError("gateId 必须是 1 到 128 个字符。")
        lock = self._control_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            result = await asyncio.to_thread(
                self.store.confirm_advance, workflow_id, gate_id
            )
            if result.get("resumedFromHold"):
                await self._resume_supervisor_if_needed(workflow_id)
            return result

    async def hold_advance(self, workflow_id: str, gate_id: str) -> dict[str, Any]:
        if not gate_id or len(gate_id) > 128:
            raise ValueError("gateId 必须是 1 到 128 个字符。")
        lock = self._control_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            self._control_in_progress.add(workflow_id)
            try:
                result = await asyncio.to_thread(
                    self.store.hold_advance, workflow_id, gate_id
                )
                await self._pause_supervisor(workflow_id)
                return result
            finally:
                self._control_in_progress.discard(workflow_id)

    def _ensure_chat_worker(self, workflow_id: str) -> None:
        current = self._chat_tasks.get(workflow_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_chat_queue(workflow_id), name=f"workflow-chat:{workflow_id}"
        )
        self._chat_tasks[workflow_id] = task
        task.add_done_callback(lambda done: self._drop_chat_task(workflow_id, done))

    async def _run_chat_queue(self, workflow_id: str) -> None:
        while True:
            message = self.store.claim_next_chat_message(workflow_id)
            if message is None:
                return
            try:
                await self._process_chat_message(workflow_id, message)
            except asyncio.CancelledError:
                self.store.fail_chat_message(
                    workflow_id, message["messageId"], "消息处理任务被中断，请安全重试。"
                )
                raise
            except Exception as error:
                self.store.fail_chat_message(workflow_id, message["messageId"], str(error))

    async def _process_chat_message(
        self, workflow_id: str, message: dict[str, Any]
    ) -> None:
        message_id = message["messageId"]
        assistant_message_id = str(uuid.uuid4())
        text = message["text"].strip()
        pending = self.store.get_pending_control(workflow_id)
        if text == "确认执行":
            if pending is None:
                answer = "当前没有等待确认的操作。"
            else:
                try:
                    confirmed = self.store.confirm_control(
                        workflow_id, pending["actionId"], message_id
                    )
                    answer = await self._execute_control(workflow_id, confirmed)
                except (RuntimeError, ValueError) as error:
                    answer = f"操作未执行：{error}任务状态和重跑额度均未改变。"
        elif text == "取消操作":
            if pending is None:
                answer = "当前没有等待取消的操作。"
            else:
                self.store.cancel_pending_control(workflow_id, message_id)
                answer = "已取消刚才提出的操作，任务状态没有改变。"
        else:
            snapshot = self.store.get_workflow(workflow_id)
            decision = await self._run_assistant_turn(
                workflow_id, message_id, snapshot, message
            )
            answer = self._apply_assistant_decision(workflow_id, message_id, decision)
        answer = answer.strip()
        if not answer:
            raise RuntimeError("任务助手没有生成可显示的回复。")
        self.store.complete_chat_message(
            workflow_id, message_id, assistant_message_id, answer
        )
        await self._resume_supervisor_if_needed(workflow_id)

    async def _run_assistant_turn(
        self,
        workflow_id: str,
        message_id: str,
        snapshot: dict[str, Any],
        message: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = self._chat_prompt(snapshot, message)
        spec = self.store.get_workflow_spec(workflow_id)
        thread_id = snapshot.get("assistant", {}).get("threadId")
        job = await self.assistant_orchestrator.dispatch(
            agent_id=spec["supervisorAgentId"],
            prompt=prompt,
            thread_id=thread_id,
            cwd=spec.get("supervisorCwd"),
            write=False,
            model=spec.get("supervisorModel"),
            timeout_sec=min(600, int(spec.get("supervisorTimeoutSec", 7200))),
            approval_policy="never",
            output_schema=self._assistant_output_schema(),
        )
        self.store.update_assistant(workflow_id, job.snapshot())
        self.store.mark_chat_forwarded(workflow_id, message_id)
        while not job.completed.is_set():
            self.store.update_assistant(workflow_id, job.snapshot())
            try:
                await asyncio.wait_for(job.completed.wait(), timeout=0.25)
            except TimeoutError:
                pass
        self.store.update_assistant(workflow_id, job.snapshot())
        if job.status != "completed":
            raise RuntimeError(job.error or "任务助手连接失败。")
        raw = (job.response or "").strip()
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("任务助手返回了无法识别的结果，请安全重试。") from error
        return self._validate_assistant_decision(decision, snapshot)

    @staticmethod
    def _assistant_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "kind",
                "text",
                "actionType",
                "nodeId",
                "revisionInstruction",
            ],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["answer", "clarify", "propose_control"],
                },
                "text": {"type": "string", "maxLength": 20_000},
                "actionType": {
                    "type": ["string", "null"],
                    "enum": ["stop", "skip", "restart_from", None],
                },
                "nodeId": {"type": ["string", "null"], "maxLength": 128},
                "revisionInstruction": {
                    "type": ["string", "null"],
                    "maxLength": REVISION_INSTRUCTION_LIMIT,
                },
            },
        }

    @staticmethod
    def _validate_assistant_decision(
        value: Any, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError("任务助手返回格式无效。")
        kind = value.get("kind")
        text = str(value.get("text") or "").strip()
        action_type = value.get("actionType")
        node_id = value.get("nodeId")
        if "revisionInstruction" not in value:
            raise RuntimeError("任务助手返回格式无效。")
        raw_revision_instruction = value.get("revisionInstruction")
        if raw_revision_instruction is not None and not isinstance(
            raw_revision_instruction, str
        ):
            raise RuntimeError("任务助手返回格式无效。")
        revision_instruction = (
            raw_revision_instruction.strip()
            if isinstance(raw_revision_instruction, str)
            else None
        )
        if revision_instruction == "":
            revision_instruction = None
        if (
            revision_instruction is not None
            and len(revision_instruction) > REVISION_INSTRUCTION_LIMIT
        ):
            raise RuntimeError("任务助手总结的返工要求过长，请缩短后重试。")
        if kind not in {"answer", "clarify", "propose_control"} or not text:
            raise RuntimeError("任务助手返回格式无效。")
        if kind != "propose_control":
            if revision_instruction is not None:
                raise RuntimeError("任务助手返回格式无效。")
            return {
                "kind": kind,
                "text": text,
                "actionType": None,
                "nodeId": None,
                "revisionInstruction": None,
            }
        if action_type not in {"stop", "skip", "restart_from"}:
            raise RuntimeError("任务助手提出了不支持的操作。")
        if action_type != "stop":
            valid_ids = {node["id"] for node in snapshot.get("nodes", [])}
            if node_id not in valid_ids:
                raise RuntimeError("任务助手没有识别出有效的目标步骤，请重新说明。")
        else:
            node_id = None
        if action_type != "restart_from" and revision_instruction is not None:
            raise RuntimeError("任务助手返回格式无效。")
        return {
            "kind": kind,
            "text": text,
            "actionType": action_type,
            "nodeId": node_id,
            "revisionInstruction": revision_instruction,
        }

    def _apply_assistant_decision(
        self, workflow_id: str, message_id: str, decision: dict[str, Any]
    ) -> str:
        if decision["kind"] != "propose_control":
            return str(decision["text"])
        try:
            proposal = self.store.propose_control(
                workflow_id,
                str(decision["actionType"]),
                decision.get("nodeId"),
                message_id,
                decision.get("revisionInstruction"),
            )
        except ValueError as error:
            if "重跑次数已经用完" in str(error):
                return "本任务的重跑额度已经用完，仍可以继续咨询任务状态和结果。"
            raise
        action_type = proposal["actionType"]
        if action_type == "restart_from":
            names = "、".join(
                item["displayName"] for item in proposal.get("affectedNodes", [])
            )
            policy = proposal["retryPolicy"]
            revision_instruction = proposal.get("revisionInstruction")
            revision_copy = (
                f"\n\n本次返工要求：\n{revision_instruction}"
                if revision_instruction
                else "\n\n本次没有新增返工要求，将按原有要求重新执行。"
            )
            return (
                f"准备重新执行：{names}。更早步骤的结果会保留，本次会消耗1次重跑额度；"
                f"当前还剩{policy['remainingRetries']}次。{revision_copy}\n\n"
                "如要继续，请另发一条仅包含“确认执行”的消息；10分钟内有效。"
            )
        if action_type == "skip":
            node = next(
                item for item in self.store.get_workflow(workflow_id)["nodes"]
                if item["id"] == proposal["nodeId"]
            )
            return (
                f"准备跳过{node['displayName']}。如要继续，请另发一条仅包含“确认执行”"
                "的消息；10分钟内有效。"
            )
        return (
            "准备停止整个任务。已完成的结果会保留，未完成步骤不会继续。"
            "如要继续，请另发一条仅包含“确认执行”的消息；10分钟内有效。"
        )

    async def _execute_control(
        self, workflow_id: str, confirmed: dict[str, Any]
    ) -> str:
        lock = self._control_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            action = self.store.start_control_execution(confirmed["actionId"])
            self._control_in_progress.add(workflow_id)
            try:
                await self._pause_supervisor(workflow_id)
                action_type = action["actionType"]
                node_id = action.get("nodeId")
                if action_type == "restart_from":
                    assert node_id is not None
                    await self._interrupt_nodes(
                        workflow_id, self.store.get_nodes_from(workflow_id, node_id)
                    )
                    result = self.store.restart_from_node(
                        workflow_id,
                        node_id,
                        action_id=action["actionId"],
                        revision_instruction=action.get("revisionInstruction"),
                        source_message_id=action.get("proposedByMessageId"),
                    )
                    self.store.finish_control_execution(
                        action["actionId"], result={"retryPolicy": result["retryPolicy"]}
                    )
                    await self._resume_supervisor_if_needed(workflow_id)
                    revision_copy = (
                        "已将确认的返工要求加入本次步骤提示词。"
                        if action.get("revisionInstruction")
                        else ""
                    )
                    return (
                        "已重新打开任务，将从所选步骤继续执行。"
                        f"{revision_copy}"
                        f"本任务还可重跑{result['retryPolicy']['remainingRetries']}次。"
                    )
                if action_type == "skip":
                    assert node_id is not None
                    await self._interrupt_nodes(
                        workflow_id, [self.store.get_node(workflow_id, node_id)]
                    )
                    result = self.store.skip_node(workflow_id, node_id)
                    self.store.finish_control_execution(
                        action["actionId"], result={"status": result["status"]}
                    )
                    await self._resume_supervisor_if_needed(workflow_id)
                    return "已跳过所选步骤，任务会继续执行后续步骤。"
                await self._interrupt_nodes(
                    workflow_id,
                    [
                        node for node in self.store.get_workflow(workflow_id)["nodes"]
                        if node["status"] in {"queued", "running", "cancelling"}
                    ]
                )
                result = self.store.stop_workflow(workflow_id)
                self.store.finish_control_execution(
                    action["actionId"], result={"status": result["status"]}
                )
                await self._schedule_pending()
                return "任务已停止，已经完成的步骤结果会保留。"
            except Exception as error:
                self.store.finish_control_execution(
                    action["actionId"], error=str(error)
                )
                self._control_in_progress.discard(workflow_id)
                await self._resume_supervisor_if_needed(workflow_id)
                raise
            finally:
                self._control_in_progress.discard(workflow_id)

    async def _pause_supervisor(self, workflow_id: str) -> None:
        snapshot = self.store.get_workflow(workflow_id)
        job_id = snapshot["supervisor"].get("jobId")
        if job_id and job_id in self.orchestrator.jobs:
            job = self.orchestrator.get_job(job_id)
            if not job.completed.is_set():
                await self.orchestrator.cancel(job_id)
                await asyncio.wait_for(job.completed.wait(), timeout=15)
        task = self._tasks.get(workflow_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _interrupt_nodes(
        self, workflow_id: str, nodes: list[dict[str, Any]]
    ) -> None:
        active = [
            node for node in nodes
            if node["status"] in {"queued", "running", "cancelling"}
        ]
        for node in active:
            if not node.get("threadId") or not node.get("turnId"):
                raise RuntimeError(f"{node['displayName']}尚未建立可安全停止的执行会话。")
            await self.orchestrator.interrupt_turn(
                agent_id=node["agentId"],
                thread_id=node["threadId"],
                turn_id=node["turnId"],
            )
        deadline = time.monotonic() + 10
        while active and time.monotonic() < deadline:
            refreshed = [self.store.get_node(workflow_id, node["id"]) for node in active]
            active = [
                node for node in refreshed
                if node["status"] in {"queued", "running", "cancelling"}
            ]
            if active:
                await asyncio.sleep(0.1)
        if active:
            raise RuntimeError("等待运行中的步骤安全停止超时，任务没有被重置。")

    @staticmethod
    def _chat_prompt(snapshot: dict[str, Any], message: dict[str, Any]) -> str:
        remaining = 40_000
        steps = []
        for index, node in enumerate(snapshot["nodes"]):
            result = str(node.get("response") or "")
            item_limit = min(20_000, remaining)
            if len(result) > item_limit:
                result = result[: max(0, item_limit - 16)] + "【内容过长，已省略】"
            remaining -= len(result)
            steps.append({
                "number": index + 1,
                "id": node["id"],
                "name": node["displayName"],
                "status": node["status"],
                "result": result,
            })
        public_snapshot = {
            "statusAtAcceptance": message.get("workflowStatusAtAcceptance"),
            "stateVersionAtAcceptance": message.get("stateVersionAtAcceptance"),
            "currentStatus": snapshot["status"],
            "stateVersion": snapshot["stateVersion"],
            "retryPolicy": snapshot.get("retryPolicy"),
            "pendingControl": snapshot.get("pendingControl"),
            "steps": steps,
        }
        return (
            "你是独立的任务助手，只回答咨询或识别用户的控制意图，不执行任务、"
            "不调用任何工具，也不直接改变状态。必须按输出结构返回。\n"
            "普通咨询返回 kind=answer；信息不足返回 kind=clarify。"
            "用户明确要求停止整个任务时返回 propose_control/stop；要求跳过某一步时"
            "返回 propose_control/skip；要求重试、重新执行、从某一步重新开始时统一返回"
            "propose_control/restart_from，并把步骤序号或名称映射为快照里的真实 id。"
            "restart_from 表示该步到最后全部重跑，已完成任务也允许提出。"
            "输出中的 revisionInstruction 字段始终必须存在。只有 restart_from 可以填写"
            "该字段：如果用户说明了上一版的问题或修改要求，请将其总结为独立、完整、"
            "可直接执行的中文返工要求，保留所有关键约束，去掉重跑步骤等控制措辞，"
            "不得虚构品牌、颜色或其他细节；如果用户只是要求重新尝试而没有新增修改要求，"
            "则填写 null。其他 kind 和 actionType 一律填写 null。"
            "如果关键要求存在歧义，应返回 clarify，不得猜测。"
            "不确定目标步骤时必须澄清，不能猜测。达到重跑上限时说明不能再重跑。"
            "不要暴露会话、工具、执行机、内部英文状态或原始异常。\n"
            f"最新任务快照：{json.dumps(public_snapshot, ensure_ascii=False)}\n"
            f"用户消息：{message['text']}"
        )

    async def _resume_supervisor_if_needed(self, workflow_id: str) -> None:
        snapshot = self.store.get_workflow(workflow_id)
        if snapshot["status"] == "queued":
            await self._schedule_pending()
            return
        if snapshot["status"] != "running" or self._advance_is_held(snapshot):
            return
        if not self.store.has_supervisor_lease(workflow_id):
            return
        job_id = snapshot["supervisor"].get("jobId")
        if job_id and job_id in self.orchestrator.jobs:
            if not self.orchestrator.get_job(job_id).completed.is_set():
                return
        if workflow_id in self._tasks and not self._tasks[workflow_id].done():
            return
        spec = self.store.get_workflow_spec(workflow_id)
        thread_id = snapshot["supervisor"].get("threadId")
        task = asyncio.create_task(
            self._run_supervisor(spec, resume_thread_id=thread_id),
            name=f"workflow-supervisor-resume:{workflow_id}",
        )
        self._tasks[workflow_id] = task
        task.add_done_callback(lambda done: self._drop_task(workflow_id, done))

    async def cancel(self, workflow_id: str) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self.store.get_workflow, workflow_id)
        task = self._tasks.get(workflow_id)
        job_id = snapshot["supervisor"].get("jobId")
        if job_id and job_id in self.orchestrator.jobs:
            await self.orchestrator.cancel(job_id)
        if task is not None and not task.done():
            task.cancel()
        result = await asyncio.to_thread(
            self.store.cancel_workflow,
            workflow_id,
            reason="用户已取消任务。",
            source="gateway",
            event_reason="user_requested",
        )
        await self._schedule_pending()
        return result


def _error_response(error: Exception, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": str(error), "errorType": type(error).__name__},
        status_code=status_code,
    )


async def create_workflow(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        payload = await request.json()
        snapshot = await gateway.submit(payload)
        return JSONResponse(snapshot, status_code=202)
    except (ValueError, PermissionError, json.JSONDecodeError) as error:
        return _error_response(error)


async def get_workflow(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        snapshot = await asyncio.to_thread(
            gateway.store.get_workflow, request.path_params["workflow_id"]
        )
        return JSONResponse(snapshot)
    except ValueError as error:
        return _error_response(error, 404)


async def get_workflow_artifact(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        artifact = await asyncio.to_thread(
            gateway.store.get_artifact,
            request.path_params["workflow_id"],
            request.path_params["artifact_id"],
        )
        disposition = (
            "inline" if str(artifact["mediaType"]).startswith("image/") else "attachment"
        )
        return Response(
            content=artifact["content"],
            media_type=artifact["mediaType"],
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Disposition": f'{disposition}; filename="{artifact["filename"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ValueError as error:
        return _error_response(error, 404)


async def post_workflow_message(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        result = await gateway.accept_message(
            request.path_params["workflow_id"],
            str(payload.get("messageId") or "").strip(),
            str(payload.get("text") or ""),
        )
        return JSONResponse(result, status_code=202)
    except LookupError as error:
        return _error_response(error, 404)
    except RuntimeError as error:
        return _error_response(error, 409)
    except (ValueError, json.JSONDecodeError) as error:
        return _error_response(error, 400)


async def confirm_workflow_advance(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        result = await gateway.confirm_advance(
            request.path_params["workflow_id"], request.path_params["gate_id"]
        )
        return JSONResponse(result)
    except LookupError as error:
        return _error_response(error, 404)
    except RuntimeError as error:
        return _error_response(error, 409)
    except ValueError as error:
        return _error_response(error, 404 if "找不到" in str(error) else 400)


async def hold_workflow_advance(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        result = await gateway.hold_advance(
            request.path_params["workflow_id"], request.path_params["gate_id"]
        )
        return JSONResponse(result)
    except LookupError as error:
        return _error_response(error, 404)
    except RuntimeError as error:
        return _error_response(error, 409)
    except ValueError as error:
        return _error_response(error, 404 if "找不到" in str(error) else 400)


async def get_event_history(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        after = int(request.query_params.get("after", "0"))
        limit = int(request.query_params.get("limit", "200"))
        events = await asyncio.to_thread(
            gateway.store.list_events,
            request.path_params["workflow_id"],
            after=after,
            limit=limit,
        )
        return JSONResponse({"events": events})
    except ValueError as error:
        return _error_response(error, 404 if "找不到" in str(error) else 400)


async def stream_events(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    workflow_id = request.path_params["workflow_id"]
    try:
        await asyncio.to_thread(gateway.store.get_workflow, workflow_id)
        after = int(request.query_params.get("after", "0"))
    except ValueError as error:
        return _error_response(error, 404)

    async def generate():
        cursor = after
        idle_cycles = 0
        while True:
            if await request.is_disconnected():
                return
            events = await asyncio.to_thread(
                gateway.store.list_events, workflow_id, after=cursor, limit=200
            )
            if events:
                idle_cycles = 0
                for event in events:
                    cursor = int(event["sequence"])
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"
            else:
                idle_cycles += 1
                if idle_cycles >= 15:
                    idle_cycles = 0
                    yield ": keep-alive\n\n"
                snapshot = await asyncio.to_thread(
                    gateway.store.get_workflow, workflow_id
                )
                if (
                    snapshot["status"] in TERMINAL_WORKFLOW_STATUSES
                    and snapshot.get("pendingChatCount", 0) == 0
                ):
                    return
                await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def cancel_workflow(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    try:
        return JSONResponse(await gateway.cancel(request.path_params["workflow_id"]))
    except ValueError as error:
        return _error_response(error, 404)


async def ready(_: Request) -> Response:
    return JSONResponse({"ready": True})


async def list_agents(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    return JSONResponse({"agents": gateway.public_agents()})


def _sidecar_identity(request: Request) -> tuple[WorkflowGateway, str]:
    gateway: WorkflowGateway = request.app.state.gateway
    supervisor_id = gateway.authenticate_sidecar(request.headers.get("Authorization"))
    return gateway, supervisor_id


def _internal_error_response(error: Exception) -> JSONResponse:
    if isinstance(error, PermissionError):
        status = 403 if "其他主监督" in str(error) else 401
    elif isinstance(error, LookupError):
        status = 404
    elif isinstance(error, RuntimeError):
        status = 409
    elif isinstance(error, ValueError) and "找不到" in str(error):
        status = 404
    else:
        status = 400
    message = str(error)
    if status == 401:
        message = "Sidecar 认证失败。"
    return JSONResponse({"error": message}, status_code=status)


def _sidecar_node_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """内部 API 不回传 app-server 的底层 thread/turn 标识。"""
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"threadId", "turnId"}
    }


def _sidecar_workflow_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只返回远程 MCP 工具编排步骤所需的工作流上下文。"""
    keys = {
        "workflowId",
        "name",
        "status",
        "failurePolicy",
        "advanceMode",
        "handoffMode",
        "pendingAdvance",
        "currentNodes",
        "progress",
        "retryPolicy",
        "response",
        "error",
        "createdAt",
        "startedAt",
        "finishedAt",
        "stateVersion",
        "nodes",
    }
    value = {key: snapshot[key] for key in keys if key in snapshot}
    value["nodes"] = [
        _sidecar_node_view(node) for node in snapshot.get("nodes", [])
    ]
    return value


def _sidecar_job_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """验证 Sidecar 上报的最小任务快照，忽略运行时私有诊断字段。"""
    allowed_statuses = {
        "queued",
        "running",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
    result: dict[str, Any] = {}
    for key in ("job_id", "thread_id", "turn_id"):
        raw = snapshot.get(key)
        if raw is not None:
            value = str(raw)
            if not 1 <= len(value) <= 512:
                raise ValueError(f"{key} 必须是 1 到 512 个字符。")
            result[key] = value
    if snapshot.get("status") is not None:
        status = str(snapshot["status"])
        if status not in allowed_statuses:
            raise ValueError("任务状态无效。")
        result["status"] = status
    for key in ("response", "error"):
        raw = snapshot.get(key)
        if raw is not None:
            value = str(raw)
            if len(value) > 20_000:
                raise ValueError(f"{key} 不能超过 20000 个字符。")
            result[key] = value
    for key in ("started_at", "finished_at"):
        raw = snapshot.get(key)
        if raw is not None:
            value = str(raw)
            if len(value) > 128:
                raise ValueError(f"{key} 不能超过 128 个字符。")
            _validate_internal_timestamp(value, key)
            result[key] = value
    return result


def _validate_internal_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} 必须是 ISO-8601 时间。") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 必须包含时区。")


def _sanitize_sidecar_event_payload(value: Any, *, parent_key: str = "") -> Any:
    """事件可诊断但不得把令牌或底层会话标识带入公开事件流。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.replace("_", "").replace("-", "").lower()
            if normalized in {
                "authorization",
                "accesstoken",
                "bearertoken",
                "refreshtoken",
                "token",
                "threadid",
                "turnid",
            }:
                continue
            if key == "id" and parent_key.lower() in {"thread", "turn"}:
                continue
            result[key] = _sanitize_sidecar_event_payload(item, parent_key=key)
        return result
    if isinstance(value, list):
        return [
            _sanitize_sidecar_event_payload(item, parent_key=parent_key)
            for item in value
        ]
    return value


async def sidecar_heartbeat(request: Request) -> Response:
    try:
        gateway, supervisor_id = _sidecar_identity(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return JSONResponse(
            await gateway.accept_sidecar_heartbeat(supervisor_id, payload)
        )
    except (PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


async def internal_get_workflow(request: Request) -> Response:
    try:
        gateway, supervisor_id = _sidecar_identity(request)
        workflow_id = request.path_params["workflow_id"]
        await asyncio.to_thread(
            gateway.store.validate_sidecar_access, supervisor_id, workflow_id
        )
        snapshot = await asyncio.to_thread(gateway.store.get_workflow, workflow_id)
        return JSONResponse(_sidecar_workflow_view(snapshot))
    except (PermissionError, RuntimeError, ValueError) as error:
        return _internal_error_response(error)


async def internal_get_node(request: Request) -> Response:
    try:
        gateway, supervisor_id = _sidecar_identity(request)
        workflow_id = request.path_params["workflow_id"]
        await asyncio.to_thread(
            gateway.store.validate_sidecar_access, supervisor_id, workflow_id
        )
        snapshot = await asyncio.to_thread(
            gateway.store.get_node,
            workflow_id,
            request.path_params["node_id"],
        )
        return JSONResponse(_sidecar_node_view(snapshot))
    except (PermissionError, RuntimeError, ValueError) as error:
        return _internal_error_response(error)


def _lease_header(request: Request) -> str:
    token = str(request.headers.get("X-Workflow-Lease") or "").strip()
    if not token or len(token) > 256:
        raise RuntimeError("请求缺少有效的工作流租约。")
    return token


async def _validate_internal_write(
    request: Request,
) -> tuple[WorkflowGateway, str, str, str]:
    gateway, supervisor_id = _sidecar_identity(request)
    workflow_id = request.path_params["workflow_id"]
    lease_token = _lease_header(request)
    await asyncio.to_thread(
        gateway.store.validate_sidecar_access,
        supervisor_id,
        workflow_id,
        lease_token=lease_token,
        require_lease=True,
    )
    return gateway, supervisor_id, workflow_id, lease_token


async def internal_get_advance(request: Request) -> Response:
    try:
        gateway, _, workflow_id, _ = await _validate_internal_write(request)
        value = await asyncio.to_thread(
            gateway.store.pending_advance_for_node,
            workflow_id,
            request.path_params["node_id"],
        )
        return JSONResponse({"advance": value})
    except (PermissionError, RuntimeError, ValueError) as error:
        return _internal_error_response(error)


async def internal_release_advance(request: Request) -> Response:
    try:
        gateway, supervisor_id, workflow_id, lease_token = (
            await _validate_internal_write(request)
        )
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        gate_id = str(payload.get("gateId") or "").strip()
        if not 1 <= len(gate_id) <= 128:
            raise ValueError("gateId 必须是 1 到 128 个字符。")
        released = await asyncio.to_thread(
            gateway.store.release_timed_out_advance,
            workflow_id,
            gate_id,
            sidecar_supervisor_id=supervisor_id,
            lease_token=lease_token,
        )
        return JSONResponse({"released": released})
    except (PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


async def internal_prepare_node(request: Request) -> Response:
    try:
        gateway, supervisor_id, workflow_id, lease_token = (
            await _validate_internal_write(request)
        )
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        dispatch_id = str(payload.get("dispatchId") or "").strip()
        if not 1 <= len(dispatch_id) <= 128:
            raise ValueError("dispatchId 必须是 1 到 128 个字符。")
        result = await asyncio.to_thread(
            gateway.store.prepare_node_dispatch,
            workflow_id,
            request.path_params["node_id"],
            sidecar_supervisor_id=supervisor_id,
            lease_token=lease_token,
            sidecar_dispatch_id=dispatch_id,
        )
        return JSONResponse(result)
    except (PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


async def internal_update_node(request: Request) -> Response:
    try:
        gateway, supervisor_id, workflow_id, lease_token = (
            await _validate_internal_write(request)
        )
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        operation = str(payload.get("operation") or "")
        node_id = request.path_params["node_id"]
        if operation == "attach":
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("snapshot 必须是 JSON 对象。")
            await asyncio.to_thread(
                gateway.store.attach_node_job,
                workflow_id,
                node_id,
                _sidecar_job_snapshot(snapshot),
                sidecar_supervisor_id=supervisor_id,
                lease_token=lease_token,
            )
        elif operation == "sync":
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("snapshot 必须是 JSON 对象。")
            await asyncio.to_thread(
                gateway.store.sync_node_job,
                workflow_id,
                node_id,
                _sidecar_job_snapshot(snapshot),
                sidecar_supervisor_id=supervisor_id,
                lease_token=lease_token,
            )
        elif operation == "actual_prompt":
            prompt = str(payload.get("actualPrompt") or "")
            await asyncio.to_thread(
                gateway.store.update_node_actual_prompt,
                workflow_id,
                node_id,
                prompt,
                sidecar_supervisor_id=supervisor_id,
                lease_token=lease_token,
            )
        else:
            raise ValueError("operation 只能是 attach、sync 或 actual_prompt。")
        result = await asyncio.to_thread(gateway.store.get_node, workflow_id, node_id)
        return JSONResponse(_sidecar_node_view(result))
    except (PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


async def internal_add_events(request: Request) -> Response:
    try:
        gateway, supervisor_id, workflow_id, lease_token = (
            await _validate_internal_write(request)
        )
        payload = await request.json()
        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(raw_events, list) or not 1 <= len(raw_events) <= 64:
            raise ValueError("events 必须包含 1 到 64 项。")
        events: list[dict[str, Any]] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise ValueError("事件必须是 JSON 对象。")
            external_id = str(raw.get("eventId") or "").strip()
            if not 1 <= len(external_id) <= 128:
                raise ValueError("eventId 必须是 1 到 128 个字符。")
            event_payload = raw.get("payload")
            if not isinstance(event_payload, dict):
                raise ValueError("事件 payload 必须是 JSON 对象。")
            node_id = raw.get("nodeId")
            if node_id is not None and not 1 <= len(str(node_id)) <= 128:
                raise ValueError("事件 nodeId 必须是 1 到 128 个字符。")
            source = str(raw.get("source") or "worker")
            if source != "worker":
                raise ValueError("Sidecar 只能上报 worker 来源的事件。")
            event_type = str(raw.get("type") or "").strip()
            if not 1 <= len(event_type) <= 128:
                raise ValueError("事件 type 必须是 1 到 128 个字符。")
            events.append(
                {
                    "workflow_id": workflow_id,
                    "node_id": str(node_id) if node_id is not None else None,
                    "source": source,
                    "event_type": event_type,
                    "payload": _sanitize_sidecar_event_payload(event_payload),
                    "created_at": str(raw.get("createdAt") or utc_now()),
                    "external_event_id": external_id,
                }
            )
            _validate_internal_timestamp(events[-1]["created_at"], "事件 createdAt")
        sequences = await asyncio.to_thread(
            gateway.store.add_events,
            events,
            sidecar_supervisor_id=supervisor_id,
            lease_token=lease_token,
        )
        return JSONResponse({"sequences": sequences})
    except (PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


def create_app(
    *,
    db_path: Path | str | None = None,
    config_path: Path | str = CONFIG_PATH,
    orchestrator: Orchestrator | None = None,
) -> Starlette:
    selected_db_path = Path(
        db_path or os.getenv("CODEX_WORKFLOW_DB", DEFAULT_DB_PATH)
    ).expanduser()
    store = WorkflowStore(selected_db_path)
    selected_config_path = Path(config_path).expanduser()
    supervisor_orchestrator = orchestrator or Orchestrator(selected_config_path)
    gateway = WorkflowGateway(store, supervisor_orchestrator)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await gateway.start()
        yield
        await gateway.stop()
        try:
            await gateway.event_batcher.close()
        except Exception:
            LOGGER.exception("关闭监控事件批量写入器失败。")

    app = Starlette(
        routes=[
            Route("/readyz", ready, methods=["GET"]),
            Route("/agents", list_agents, methods=["GET"]),
            Route(
                "/internal/v1/sidecars/heartbeat",
                sidecar_heartbeat,
                methods=["POST"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}",
                internal_get_workflow,
                methods=["GET"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/nodes/{node_id}",
                internal_get_node,
                methods=["GET"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/nodes/{node_id}/advance",
                internal_get_advance,
                methods=["GET"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/nodes/{node_id}/advance/release",
                internal_release_advance,
                methods=["POST"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/nodes/{node_id}/prepare",
                internal_prepare_node,
                methods=["POST"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/nodes/{node_id}/state",
                internal_update_node,
                methods=["POST"],
            ),
            Route(
                "/internal/v1/workflows/{workflow_id}/events:batch",
                internal_add_events,
                methods=["POST"],
            ),
            Route("/workflows", create_workflow, methods=["POST"]),
            Route("/workflows/{workflow_id}", get_workflow, methods=["GET"]),
            Route(
                "/workflows/{workflow_id}/artifacts/{artifact_id}",
                get_workflow_artifact,
                methods=["GET"],
            ),
            Route(
                "/workflows/{workflow_id}/messages",
                post_workflow_message,
                methods=["POST"],
            ),
            Route(
                "/workflows/{workflow_id}/advance/{gate_id}/confirm",
                confirm_workflow_advance,
                methods=["POST"],
            ),
            Route(
                "/workflows/{workflow_id}/advance/{gate_id}/hold",
                hold_workflow_advance,
                methods=["POST"],
            ),
            Route(
                "/workflows/{workflow_id}/events/history",
                get_event_history,
                methods=["GET"],
            ),
            Route(
                "/workflows/{workflow_id}/events",
                stream_events,
                methods=["GET"],
            ),
            Route(
                "/workflows/{workflow_id}/cancel",
                cancel_workflow,
                methods=["POST"],
            ),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    return app


app = create_app()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex 工作流 HTTP/SSE 网关")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db", default=os.getenv("CODEX_WORKFLOW_DB", str(DEFAULT_DB_PATH))
    )
    parser.add_argument(
        "--agents", default=os.getenv("CODEX_AGENTS_FILE", str(CONFIG_PATH))
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        create_app(db_path=args.db, config_path=args.agents),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
