"""中央等待扫描与原执行者的受限重新执行/阻断收尾。"""
import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

from workflow_database import database_call as db
from workflow_outcomes import instruction, schema
from workflow_store import PROMPT_LIMIT

if TYPE_CHECKING:
    from workflow_gateway import WorkflowGateway

LOGGER = logging.getLogger(__name__)


class WaitService:
    def __init__(self, gateway: "WorkflowGateway") -> None:
        self.gateway = gateway
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        while not self.gateway._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("扫描半自动等待失败。")
            await asyncio.sleep(1)

    async def tick(self) -> None:
        for workflow_id in await db(self.gateway.store.policy_wait_workflows):
            lock = self.gateway._control_locks.setdefault(workflow_id, asyncio.Lock())
            async with lock:
                state = await db(self.gateway.store.get_workflow, workflow_id)
                gate = state.get("pendingAdvance") or {}
                if gate.get("kind", "legacy") != "legacy" and gate.get("state") in {"countdown", "held"}:
                    await self.gateway._pause_supervisor(workflow_id)
        for row in await db(self.gateway.store.due_policy_waits):
            key = row["gate_id"]
            if key in self.tasks:
                continue
            if row["execution_started"]:
                continue
            task = asyncio.create_task(self.process(row), name="workflow-wait:" + key)
            self.tasks[key] = task
            task.add_done_callback(lambda done, key=key: self.tasks.pop(key, None))
        # A confirm can arrive just before the old supervisor exits. Reconcile centrally.
        ready = await db(self.gateway.store.policy_wait_workflows, True)
        for workflow_id in ready:
            await self.gateway._resume_supervisor_if_needed(workflow_id)

    async def stop(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def process(self, row: dict[str, Any]) -> None:
        gateway = self.gateway
        workflow_id, gate_id = row["workflow_id"], row["gate_id"]
        try:
            lock = gateway._control_locks.setdefault(workflow_id, asyncio.Lock())
            async with lock:
                if row["status"] == "pending":
                    try:
                        result = await db(gateway.store.claim_policy_wait, workflow_id, gate_id,
                            "timeout" if row["kind"] == "blocked" else "confirm", timed_out=True)
                    except RuntimeError:
                        return  # A reply or button won the transaction.
                    row["status"] = result["status"]
                if row["status"] == "confirmed":
                    await gateway._resume_supervisor_if_needed(workflow_id)
                    return
                await gateway._pause_supervisor(workflow_id)
                if row["status"] == "finishing":
                    await db(gateway.store.complete_final_wait, workflow_id, gate_id)
                    await gateway._schedule_pending()
                    return
                try:
                    target = await db(gateway.store.prepare_wait_execution, workflow_id, gate_id)
                except Exception:
                    if row["status"] != "executing":
                        raise
                    # 准备事务失败时尚未派发，恢复人工等待，避免反复扫描同一失败。
                    LOGGER.exception("重新执行准备失败，gate=%s。", gate_id)
                    await db(gateway.store.fail_wait_execution, workflow_id, gate_id, before_start=True)
                    return
            if target is not None:
                await self.execute(workflow_id, gate_id, target)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("处理半自动等待失败，gate=%s。", gate_id)

    async def execute(self, workflow_id: str, gate_id: str, target: dict[str, Any]) -> None:
        gateway = self.gateway
        closing = target["gate"]["status"] == "closing"
        original = json.loads(target["gate"]["business_result_json"])
        job = None
        prepared_execution = False
        try:
            if not target["threadId"]:
                raise RuntimeError("原步骤会话不可恢复。")
            prompt = (
                "【平台已确认超时阻断】人工等待到期无人回复。停止所有业务执行，仅在原授权内执行阻断收尾："
                "先添加带稳定标记的停止评论并回查确认，再按平台规则读取开发人及转交负责人，保持 Jira 状态和处理角色不变。"
                "不得恢复任务，不得改变原阻断原因；无权限、失败或不确定均如实说明并返回 blocked。"
                if closing else
                "【用户点击重新执行】仅重新处理当前遇阻步骤。结合本会话人工讨论与下方最新总结，"
                "先核对此前已完成的提交、文件和 Jira 写入，不重复执行已成功或状态不明的写操作，"
                "接着完成当前步骤剩余工作。不能跳过当前步骤或操作其他步骤。"
            )
            if closing:
                prompt += "\n既定阻断原因：" + original["reason"] + "\n已锁定工单：" + (original["jiraComment"]["issueKey"] or "未锁定，不得另选工单")
            prompt += "\n当前总结：\n" + target["summary"]
            prompt += target.get("jiraInstruction", "")
            prompt += instruction(workflow_id, target["nodeId"], target["captureDocument"] and not closing,
                                  target["resultProtocolVersion"], defer_blocked=not closing)
            if len(prompt) > PROMPT_LIMIT:
                raise RuntimeError("重新执行提示超过容量限制。")
            agent = gateway.assistant_orchestrator.get_agent(target["agentId"])
            cwd = target["cwd"]
            if not agent.allow_cwd_override and cwd == agent.cwd:
                cwd = None

            async def event(message, received_at):
                await gateway.event_batcher.add(workflow_id, node_id=target["nodeId"], source="worker",
                    event_type="appserver." + str(message.get("method") or "unknown"),
                    payload={"receivedAt": received_at, "message": message})

            execution_key = "wait:" + gate_id
            await db(gateway.store.prepare_resource_execution, workflow_id, execution_key, target["agentId"])
            prepared_execution = True
            job = await gateway.assistant_orchestrator.dispatch(
                agent_id=target["agentId"], thread_id=target["threadId"], cwd=cwd, model=target["model"],
                prompt=prompt, write=target["write"], permission_profile=target["permissionProfile"],
                reasoning_effort=target.get("reasoningEffort"), timeout_sec=target["timeoutSec"],
                output_schema=schema(target["resultProtocolVersion"], jira=target.get("jiraEnabled", False)), event_callback=event)
            job.capture_document = target["captureDocument"] and not closing
            job.result_protocol_version = target["resultProtocolVersion"]
            job.jira_enabled = target.get("jiraEnabled", False)
            await db(gateway.store.set_wait_job, workflow_id, gate_id, job.job_id, job.turn_id)
            if not closing:
                await db(gateway.store.update_node_actual_prompt, workflow_id, target["nodeId"], job.prompt)
                await db(gateway.store.attach_node_job, workflow_id, target["nodeId"], job.snapshot())
            await db(gateway.store.observe_resource_execution, workflow_id, execution_key, job.snapshot())
            last_turn = None
            while not job.completed.is_set():
                if job.turn_id != last_turn:
                    await db(gateway.store.observe_resource_execution, workflow_id, execution_key, job.snapshot())
                    await db(gateway.store.set_wait_job, workflow_id, gate_id, job.job_id, job.turn_id)
                    last_turn = job.turn_id
                state = await db(gateway.store.get_workflow, workflow_id)
                if state["status"] != "running":
                    await gateway.assistant_orchestrator.cancel(job.job_id)
                    await job.completed.wait()
                    return
                try:
                    await asyncio.wait_for(job.completed.wait(), 0.25)
                except asyncio.TimeoutError:
                    pass
            await db(gateway.store.set_wait_job, workflow_id, gate_id, job.job_id, job.turn_id)
            await db(gateway.store.observe_resource_execution, workflow_id, execution_key, job.snapshot())
            await gateway.flush_events()
            if job.status != "completed" or job.business_result is None:
                raise RuntimeError("原执行者未返回已确认的步骤结果。")
            if closing:
                await db(gateway.store.finish_wait_result, workflow_id, gate_id, job.business_result)
                await db(gateway.store.finish_business_termination, workflow_id)
            else:
                snapshot = {**job.snapshot(), "businessResult": job.business_result}
                if job.document_results is not None:
                    snapshot["documents"] = job.document_results
                if job.document_result is not None:
                    snapshot["document"] = job.document_result
                await db(gateway.store.sync_node_job, workflow_id, target["nodeId"], snapshot)
                if await db(gateway.store.has_business_termination, workflow_id):
                    await db(gateway.store.finish_business_termination, workflow_id)
            await gateway._schedule_pending()
        except asyncio.CancelledError:
            if job and not job.completed.is_set():
                await gateway.assistant_orchestrator.cancel(job.job_id)
            raise
        except Exception:
            LOGGER.exception("等待期间原执行者操作失败，gate=%s。", gate_id)
            if closing:
                original["jiraComment"] = {"status": "unknown", "issueKey": original["jiraComment"]["issueKey"],
                    "reference": "", "detail": "阻断收尾未确认完成，未自动重发，请人工核查。"}
                original["summary"] = original["summary"][:19000] + "\n阻断收尾未确认完成，请人工核查。"
                await db(gateway.store.finish_wait_result, workflow_id, gate_id, original)
                await db(gateway.store.finish_business_termination, workflow_id)
                await gateway._schedule_pending()
            else:
                before_start = job is None or job.error_stage in {"queued", "configuration", "connect", "initialize", "reasoning/check", "permission/check", "thread/resume", "thread/start", "image/stage", "model/list"}
                uncertain = not before_start and job.error_kind not in {"turn_failed", "invalid_business_result"}
                await db(gateway.store.fail_wait_execution, workflow_id, gate_id, uncertain=uncertain, before_start=before_start)
                await gateway._schedule_pending()
        finally:
            if job:
                await db(gateway.store.observe_resource_execution, workflow_id, "wait:" + gate_id, job.snapshot())
            elif prepared_execution:
                await db(gateway.store.observe_resource_execution, workflow_id, "wait:" + gate_id,
                         {"status": "failed", "execution_stopped": True})
