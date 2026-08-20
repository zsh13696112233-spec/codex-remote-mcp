import argparse
import asyncio
import json
import os
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
    AppServerDisconnected,
    AppServerRpcError,
    Orchestrator,
    TurnNotActiveError,
    utc_now,
)
from workflow_store import WorkflowStore


DEFAULT_DB_PATH = Path(__file__).with_name("workflows.db")
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}


class WorkflowGateway:
    def __init__(self, store: WorkflowStore, orchestrator: Orchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._chat_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        self.store.recover_processing_chat_messages()
        for workflow_id in self.store.list_chat_workflows():
            self._ensure_chat_worker(workflow_id)

    async def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        spec = WorkflowStore.normalize_spec(raw_spec)
        available_agents = {item["agent_id"] for item in self.orchestrator.list_agents()}
        requested_agents = {spec["supervisorAgentId"]} | {
            node["agentId"] for node in spec["nodes"]
        }
        unknown = sorted(requested_agents - available_agents)
        if unknown:
            raise ValueError(f"工作流引用了未知执行机：{', '.join(unknown)}")

        snapshot = self.store.create_workflow(spec)
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

                def record(message: dict[str, Any], received_at: str) -> None:
                    nonlocal message_buffer
                    method = str(message.get("method") or "unknown")
                    params = message.get("params") or {}
                    self.store.add_event(
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
                            self.store.set_supervisor_message(workflow_id, message_buffer)
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("text"):
                            message_buffer = str(item["text"])[-20_000:]
                            self.store.set_supervisor_message(workflow_id, message_buffer)

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
                self.store.add_event(
                    workflow_id,
                    node_id=None,
                    source="gateway",
                    event_type="supervisor.started",
                    payload={"jobId": job.job_id, "agentId": job.agent_id},
                )
                while not job.completed.is_set():
                    self.store.update_supervisor(workflow_id, job.snapshot())
                    await asyncio.sleep(0.25)
                job_snapshot = job.snapshot()
                self.store.update_supervisor(workflow_id, job_snapshot)

                latest = self.store.get_workflow(workflow_id)
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
        accepted = self.store.accept_chat_message(workflow_id, message_id, text)
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
        snapshot = self.store.get_workflow(workflow_id)
        prompt = self._chat_prompt(snapshot, message)
        answer = (
            await self._deliver_chat_prompt(
                workflow_id, message_id, assistant_message_id, prompt
            )
        ).strip()

        if not answer:
            raise RuntimeError("任务助手没有生成可显示的回复。")
        self.store.complete_chat_message(
            workflow_id, message_id, assistant_message_id, answer
        )
        await self._resume_supervisor_if_needed(workflow_id)

    async def _deliver_chat_prompt(
        self,
        workflow_id: str,
        message_id: str,
        assistant_message_id: str,
        prompt: str,
    ) -> str:
        snapshot = self.store.get_workflow(workflow_id)
        job_id = snapshot["supervisor"].get("jobId")
        if job_id and job_id in self.orchestrator.jobs:
            job = self.orchestrator.get_job(job_id)
            if job.status == "running" and job.client is not None:
                queue = self.orchestrator.subscribe(job_id)
                try:
                    await self.orchestrator.steer(job_id, prompt, message_id)
                    self.store.mark_chat_forwarded(workflow_id, message_id)
                    reply = await self._collect_chat_reply(
                        queue, workflow_id, message_id, assistant_message_id, job.completed
                    )
                    if reply:
                        return reply
                    if job.completed.is_set() and job.response:
                        return job.response
                except TurnNotActiveError:
                    pass
                finally:
                    self.orchestrator.unsubscribe(job_id, queue)

        thread_id = self.store.get_workflow(workflow_id)["supervisor"].get("threadId")
        if not thread_id:
            raise RuntimeError("主监督会话尚未建立，请稍后安全重试。")
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def record(event: dict[str, Any], _: str) -> None:
            event_queue.put_nowait(event)

        spec = self.store.get_workflow_spec(workflow_id)
        job = await self.orchestrator.dispatch(
            agent_id=spec["supervisorAgentId"],
            prompt=prompt,
            thread_id=thread_id,
            cwd=spec.get("supervisorCwd"),
            write=bool(spec.get("supervisorWrite", False)),
            model=spec.get("supervisorModel"),
            timeout_sec=int(spec.get("supervisorTimeoutSec", 7200)),
            approval_policy="on-request",
            approvals_reviewer="auto_review",
            event_callback=record,
        )
        self.store.mark_chat_forwarded(workflow_id, message_id)
        reply = await self._collect_chat_reply(
            event_queue, workflow_id, message_id, assistant_message_id, job.completed
        )
        if not job.completed.is_set():
            await self.orchestrator.wait(job.job_id, min(600, job.timeout_sec))
        if not reply:
            reply = job.response or ""
        if job.status == "failed":
            raise RuntimeError(job.error or "任务助手连接失败。")
        return reply

    async def _collect_chat_reply(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        workflow_id: str,
        message_id: str,
        assistant_message_id: str,
        turn_completed: asyncio.Event,
    ) -> str:
        chunks: list[str] = []
        while True:
            event_task = asyncio.create_task(queue.get())
            completed_task = asyncio.create_task(turn_completed.wait())
            done, pending = await asyncio.wait(
                {event_task, completed_task}, timeout=180, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                raise RuntimeError("等待任务助手回复超时，请安全重试。")
            if event_task not in done:
                return "".join(chunks)
            event = event_task.result()
            method = event.get("method")
            params = event.get("params") or {}
            if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
                delta = params["delta"]
                chunks.append(delta)
                self.store.add_chat_delta(
                    workflow_id, message_id, assistant_message_id, delta
                )
            elif method == "item/completed":
                item = params.get("item") or {}
                if (
                    item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    and item.get("text")
                ):
                    final = str(item["text"])
                    if not chunks:
                        self.store.add_chat_delta(
                            workflow_id, message_id, assistant_message_id, final
                        )
                    return final
            elif method == "turn/completed":
                return ""

    @staticmethod
    def _chat_prompt(snapshot: dict[str, Any], message: dict[str, Any]) -> str:
        workflow_id = snapshot["workflowId"]
        pending = snapshot.get("pendingControl")
        text = message["text"].strip()
        control_instruction = ""
        if text == "确认执行" and pending:
            control_instruction = (
                "这是对待确认操作的独立确认消息。调用 execute_workflow_control，参数为："
                f"workflow_id={workflow_id}, action_id={pending['actionId']}, "
                f"confirmation_message_id={message['messageId']}。执行后用简单中文说明结果。"
            )
        elif text == "取消操作" and pending:
            control_instruction = (
                "调用 cancel_workflow_control 取消当前待确认操作，然后说明已经取消。"
            )
        else:
            control_instruction = (
                "如果用户只是咨询，先调用 workflow_status 再回答。"
                "如果用户要求停止任务、重试步骤或跳过步骤，只调用 "
                "propose_workflow_control 创建提议，不得直接执行；说明影响并要求用户另发一条"
                "内容完全为“确认执行”的消息。步骤序号必须按下方快照转换成真实步骤 id。"
                "修改工作流、增加或删除步骤、重新执行已完成步骤必须拒绝。"
            )
        public_snapshot = {
            "statusAtAcceptance": message.get("workflowStatusAtAcceptance"),
            "stateVersionAtAcceptance": message.get("stateVersionAtAcceptance"),
            "currentStatus": snapshot["status"],
            "stateVersion": snapshot["stateVersion"],
            "steps": [
                {"number": index + 1, "id": node["id"], "name": node["displayName"],
                 "status": node["status"]}
                for index, node in enumerate(snapshot["nodes"])
            ],
        }
        return (
            "这是用户在任务运行监控页面发送的消息。回答前读取当前工作流的最新状态。"
            "如果发送后状态发生变化，明确说明发送时和现在的差异。"
            "除经过两条消息二次确认的停止、重试、跳过外，不得改变执行状态。"
            "不要展示 thread、turn、job、MCP、agent、工具调用等内部术语。\n"
            f"{control_instruction}\n工作流：{workflow_id}\n"
            f"接收快照：{json.dumps(public_snapshot, ensure_ascii=False)}\n"
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
        snapshot = self.store.get_workflow(workflow_id)
        task = self._tasks.get(workflow_id)
        job_id = snapshot["supervisor"].get("jobId")
        if job_id and job_id in self.orchestrator.jobs:
            await self.orchestrator.cancel(job_id)
        if task is not None and not task.done():
            task.cancel()
        self.store.add_event(
            workflow_id,
            node_id=None,
            source="gateway",
            event_type="workflow.cancel_requested",
            payload={"requestedAt": utc_now()},
        )
        return self.store.get_workflow(workflow_id)


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
        return JSONResponse(gateway.store.get_workflow(request.path_params["workflow_id"]))
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
        events = gateway.store.list_events(
            request.path_params["workflow_id"], after=after, limit=limit
        )
        return JSONResponse({"events": events})
    except ValueError as error:
        return _error_response(error, 404 if "找不到" in str(error) else 400)


async def stream_events(request: Request) -> Response:
    gateway: WorkflowGateway = request.app.state.gateway
    workflow_id = request.path_params["workflow_id"]
    try:
        gateway.store.get_workflow(workflow_id)
        after = int(request.query_params.get("after", "0"))
    except ValueError as error:
        return _error_response(error, 404)

    async def generate():
        cursor = after
        idle_cycles = 0
        while True:
            if await request.is_disconnected():
                return
            events = gateway.store.list_events(workflow_id, after=cursor, limit=200)
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
                snapshot = gateway.store.get_workflow(workflow_id)
                if (
                    snapshot["status"] == "completed"
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
    gateway = WorkflowGateway(
        store,
        orchestrator or Orchestrator(Path(config_path).expanduser()),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await gateway.start()
        yield
        tasks = list(gateway._chat_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = Starlette(
        routes=[
            Route("/readyz", ready, methods=["GET"]),
            Route("/agents", list_agents, methods=["GET"]),
            Route("/workflows", create_workflow, methods=["POST"]),
            Route("/workflows/{workflow_id}", get_workflow, methods=["GET"]),
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
