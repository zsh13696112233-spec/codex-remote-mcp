import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
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
from workflow_store import AsyncEventBatcher, WorkflowStore


DEFAULT_DB_PATH = Path(__file__).with_name("workflows.db")
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}
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

    async def start(self) -> None:
        self.store.recover_processing_chat_messages()
        try:
            imported = await asyncio.to_thread(self.store.import_legacy_generated_images)
            if imported:
                LOGGER.info("已回填 %s 个历史工作流图片附件。", imported)
        except Exception:
            LOGGER.exception("回填历史工作流图片附件失败。")
        for workflow_id in self.store.list_chat_workflows():
            self._ensure_chat_worker(workflow_id)

    async def flush_events(self) -> None:
        try:
            await self.event_batcher.flush()
        except Exception:
            LOGGER.exception("刷新主监督监控事件失败。")

    async def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        spec = WorkflowStore.normalize_spec(raw_spec)
        available_agents = {item["agent_id"] for item in self.orchestrator.list_agents()}
        requested_agents = {spec["supervisorAgentId"]} | {
            node["agentId"] for node in spec["nodes"]
        }
        unknown = sorted(requested_agents - available_agents)
        if unknown:
            raise ValueError(f"工作流引用了未知执行机：{', '.join(unknown)}")

        snapshot = await asyncio.to_thread(self.store.create_workflow, spec)
        workflow_id = spec["workflowId"]
        task = asyncio.create_task(
            self._run_supervisor(spec), name=f"workflow-supervisor:{workflow_id}"
        )
        self._tasks[workflow_id] = task
        task.add_done_callback(lambda done: self._drop_task(workflow_id, done))
        return snapshot

    def _drop_task(self, workflow_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(workflow_id) is task:
            self._tasks.pop(workflow_id, None)

    def _drop_chat_task(self, workflow_id: str, task: asyncio.Task[Any]) -> None:
        if self._chat_tasks.get(workflow_id) is task:
            self._chat_tasks.pop(workflow_id, None)

    def public_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "agentId": item["agent_id"],
                "defaultCwd": item["cwd"],
                "defaultModel": item.get("model"),
                "allowWrite": bool(item.get("allow_write")),
                "allowCwdOverride": bool(item.get("allow_cwd_override")),
            }
            for item in self.orchestrator.list_agents()
        ]

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
            if workflow_id not in self._control_in_progress:
                self.store.finish_workflow(
                    workflow_id,
                    supervisor_status="cancelled",
                    response=None,
                    error="工作流监督任务被取消。",
                )
            raise
        except Exception as error:
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
            "required": ["kind", "text", "actionType", "nodeId"],
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
        if kind not in {"answer", "clarify", "propose_control"} or not text:
            raise RuntimeError("任务助手返回格式无效。")
        if kind != "propose_control":
            return {"kind": kind, "text": text, "actionType": None, "nodeId": None}
        if action_type not in {"stop", "skip", "restart_from"}:
            raise RuntimeError("任务助手提出了不支持的操作。")
        if action_type != "stop":
            valid_ids = {node["id"] for node in snapshot.get("nodes", [])}
            if node_id not in valid_ids:
                raise RuntimeError("任务助手没有识别出有效的目标步骤，请重新说明。")
        else:
            node_id = None
        return {
            "kind": kind,
            "text": text,
            "actionType": action_type,
            "nodeId": node_id,
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
            return (
                f"准备重新执行：{names}。更早步骤的结果会保留，本次会消耗1次重跑额度；"
                f"当前还剩{policy['remainingRetries']}次。"
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
                        workflow_id, node_id, action_id=action["actionId"]
                    )
                    self.store.finish_control_execution(
                        action["actionId"], result={"retryPolicy": result["retryPolicy"]}
                    )
                    return (
                        "已重新打开任务，将从所选步骤继续执行。"
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
            "不确定目标步骤时必须澄清，不能猜测。达到重跑上限时说明不能再重跑。"
            "不要暴露会话、工具、执行机、内部英文状态或原始异常。\n"
            f"最新任务快照：{json.dumps(public_snapshot, ensure_ascii=False)}\n"
            f"用户消息：{message['text']}"
        )

    async def _resume_supervisor_if_needed(self, workflow_id: str) -> None:
        snapshot = self.store.get_workflow(workflow_id)
        if snapshot["status"] != "running":
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
        await asyncio.to_thread(
            self.store.add_event,
            workflow_id,
            node_id=None,
            source="gateway",
            event_type="workflow.cancel_requested",
            payload={"requestedAt": utc_now()},
        )
        return await asyncio.to_thread(self.store.get_workflow, workflow_id)


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
        return Response(
            content=artifact["content"],
            media_type=artifact["mediaType"],
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Disposition": f'inline; filename="{artifact["filename"]}"',
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
        tasks = list(gateway._chat_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await gateway.event_batcher.close()
        except Exception:
            LOGGER.exception("关闭监控事件批量写入器失败。")

    app = Starlette(
        routes=[
            Route("/readyz", ready, methods=["GET"]),
            Route("/agents", list_agents, methods=["GET"]),
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 工作流 HTTP/SSE 网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db", default=os.getenv("CODEX_WORKFLOW_DB", str(DEFAULT_DB_PATH))
    )
    parser.add_argument(
        "--agents", default=os.getenv("CODEX_AGENTS_FILE", str(CONFIG_PATH))
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(db_path=args.db, config_path=args.agents),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
