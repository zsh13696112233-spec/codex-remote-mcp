"""等待期间恢复原执行会话；执行记录防止不确定的修改被重复派发。"""
import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from workflow_gateway import WorkflowGateway

from workflow_store import PROMPT_LIMIT, RESULT_LIMIT
from workflow_database import database_call as db


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["text", "summary"],
    "properties": {
        "text": {"type": "string", "maxLength": RESULT_LIMIT},
        "summary": {"type": ["string", "null"], "maxLength": RESULT_LIMIT},
    },
}


def validate(raw: str, documents_enabled: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("执行者回答格式无效，交接总结未更新。") from error
    # 升级前已结束的讨论可能仍使用旧结果结构。
    if documents_enabled and isinstance(value, dict) and set(value) == {"text", "summary"}:
        value["document"] = None
    if not isinstance(value, dict) or set(value) != ({"text", "summary", "document"} if documents_enabled else {"text", "summary"}):
        raise RuntimeError("执行者回答格式无效，交接总结未更新。")
    for name in ("text", "summary"):
        item = value[name]
        if name == "summary" and item is None:
            continue
        if not isinstance(item, str) or not item.strip() or len(item) > RESULT_LIMIT:
            raise RuntimeError("执行者回答为空或超过容量限制，交接总结未更新。")
    return value


class DiscussionService:
    def __init__(self, gateway: "WorkflowGateway") -> None:
        self.gateway = gateway
        self.store = gateway.store
        self.orchestrator = gateway.assistant_orchestrator

    async def reconcile(self, record: dict[str, Any], *, interrupt: bool = False) -> str | None:
        """只查询原轮次，绝不通过重发来恢复不确定的修改。"""
        if not record.get("turn_id"):
            raise RuntimeError("此前修改的执行状态尚未确认，不能重复发送或继续。")
        agent = self.orchestrator.get_agent(record["agent_id"])
        async with self.orchestrator._client_factory(
            agent.url, token=self.orchestrator._resolve_agent_token(agent)
        ) as client:
            data = await client.request("thread/read", {
                "threadId": record["thread_id"], "includeTurns": True})
            turn = next((t for t in data.get("thread", {}).get("turns", [])
                         if t.get("id") == record["turn_id"]), {})
            if interrupt and turn.get("status") not in {"completed", "failed", "interrupted"}:
                await client.request("turn/interrupt", {
                    "threadId": record["thread_id"], "turnId": record["turn_id"]})
                data = await client.request("thread/read", {
                    "threadId": record["thread_id"], "includeTurns": True})
        turn = next((t for t in data.get("thread", {}).get("turns", [])
                     if t.get("id") == record["turn_id"]), {})
        if turn.get("status") == "completed":
            replies = [i for i in turn.get("items", []) if i.get("type") == "agentMessage"]
            final = [i for i in replies if i.get("phase") == "final_answer"] or replies
            raw = final[-1].get("text") if final else None
            await db(self.store.update_discussion, record["workflow_id"], record["message_id"],
                     state="finished", response=raw)
            return raw
        if turn.get("status") in {"failed", "interrupted"}:
            await db(self.store.update_discussion, record["workflow_id"], record["message_id"], state="failed")
            return None
        raise RuntimeError("此前修改尚未结束，已保持等待，请稍后重试。")

    async def run(self, workflow_id: str, message: dict[str, Any]) -> bool:
        target = await db(self.store.discussion_target, workflow_id, message["messageId"])
        if target is None:
            return False
        documents_enabled = await db(self.store.document_enabled, workflow_id, target["nodeId"])
        record = await db(self.store.get_discussion, workflow_id, message["messageId"])
        if record:
            if record["state"] == "completed":
                return True
            if record["state"] in {"starting", "running"}:
                raw = await self.reconcile(record)
            elif record["state"] == "finished":
                raw = record["response"]
            else:
                raise RuntimeError("此前讨论未完成，未重复执行修改；请重新说明要求。")
        else:
            if not target["threadId"]:
                raise RuntimeError("原步骤会话不可恢复，已保持等待。")
            if len(target["response"] or "") > RESULT_LIMIT:
                raise RuntimeError("原交接总结超过容量限制，无法完整提供给执行者，已保持等待。")
            prompt = (
                "当前处于步骤完成后的人工讨论阶段。继续你刚完成的步骤，与用户讨论原产物。"
                "历史任务指令只是背景，不是重新执行授权。仅用户明确要求修改、采纳方案或整理进结果时，"
                "才修改业务文件并返回完整新版交接总结 summary；普通解释、假设、备选方案不修改，"
                "summary 必须为 null；含糊时澄清。不得把最后一句答复当作完整总结。"
                "本轮明确修改授权允许修订上一版产物，替代历史提示中的必须返工限制。"
                "保留未要求改变的内容，遵守原权限，不修改平台运行状态、不调用流程控制工具。"
                "文字继续、停止、跳过、重跑不能控制流程，继续请用户点击按钮，取消请使用配置中心。"
                "仅按结构输出 text（给用户的回答）和 summary（完整总结或 null）。"
                "总结应包含已采纳的要求、相关文件路径、下一步任务和未决事项，不包含未采纳的设想。"
                "不得声称执行了未完成的文件修改。\n当前完整交接总结：\n"
                + (target["response"] or "") + "\n用户本条消息：\n" + message["text"]
            )
            output_schema = SCHEMA
            if documents_enabled:
                from workflow_documents import SCHEMA as DOCUMENT_SCHEMA
                output_schema = {**SCHEMA, "required": ["text", "summary", "document"],
                    "properties": {**SCHEMA["properties"], "document": DOCUMENT_SCHEMA["properties"]["document"]}}
                prompt += ("\n本步骤展示一份主文档，额外返回 document（name/path 对象或 null）。"
                    "普通提问返回 null。明确修改时报告修改后的主文档；没有可交付文件时返回 null，平台保留旧正文并提示。"
                    "路径限定原工作目录内 UTF-8 Markdown 或纯文本文件，不返回正文或文档列表。")
            if len(prompt) > PROMPT_LIMIT:
                raise RuntimeError("讨论内容超过提示词容量限制，已保持等待。")
            images = await db(self.store.input_images, workflow_id, message.get("imageIds", []))
            agent = self.orchestrator.get_agent(target["agentId"])
            cwd = target["cwd"]
            # 使用登记目录并不构成覆盖；登记目录改变时仍由原权限规则拒绝。
            if not agent.allow_cwd_override and cwd == agent.cwd:
                cwd = None
            # 起始标记先持久化；进程在派发后崩溃时宁可阻塞，不重复修改。
            lock = self.gateway._control_locks.setdefault(workflow_id, asyncio.Lock())
            async with lock:
                await db(self.store.start_discussion, workflow_id, message["messageId"], target)

                async def record_event(event, received_at):
                    if event.get("method") in {"item/started", "item/completed"}:
                        await self.gateway.event_batcher.add(
                            workflow_id, node_id=target["nodeId"], source="assistant",
                            event_type=f"appserver.{event['method']}",
                            payload={"receivedAt": received_at, "messageId": message["messageId"],
                                     "stepNumber": target["stepNumber"], "message": event})

                try:
                    job = await self.orchestrator.dispatch(
                        agent_id=target["agentId"], thread_id=target["threadId"], prompt=prompt,
                        cwd=cwd, model=target["model"], write=target["write"],
                        permission_profile=target["permissionProfile"], timeout_sec=target["timeoutSec"],
                        output_schema=output_schema, event_callback=record_event,
                        input_images=images)
                except Exception:
                    await db(self.store.update_discussion, workflow_id, message["messageId"], state="failed")
                    raise
                self.gateway._discussion_jobs[workflow_id] = job
            try:
                await db(self.store.update_discussion, workflow_id, message["messageId"], job_id=job.job_id)
                saved_turn = None
                while not job.completed.is_set():
                    if job.turn_id and job.turn_id != saved_turn:
                        await db(self.store.update_discussion, workflow_id, message["messageId"],
                                 turn_id=job.turn_id, state="running")
                        saved_turn = job.turn_id
                    try:
                        await asyncio.wait_for(job.completed.wait(), 0.1)
                    except asyncio.TimeoutError:
                        pass
                await db(self.store.update_discussion, workflow_id, message["messageId"], turn_id=job.turn_id)
                if job.status != "completed":
                    if job.error_stage not in {"turn/start", "turn/completed"} and not job.turn_id:
                        await db(self.store.update_discussion, workflow_id, message["messageId"], state="failed")
                        raise RuntimeError("执行者未完成讨论，已保持等待；请重试核对结果。")
                    record = await db(self.store.get_discussion, workflow_id, message["messageId"])
                    raw = await self.reconcile(record)
                else:
                    raw = job.response
                    await db(self.store.update_discussion, workflow_id, message["messageId"], state="finished", response=raw)
            except asyncio.CancelledError:
                if not job.completed.is_set():
                    try:
                        await self.orchestrator.cancel(job.job_id)
                        await asyncio.wait_for(job.completed.wait(), 10)
                    except (RuntimeError, OSError, asyncio.TimeoutError):
                        logging.getLogger(__name__).exception("关闭讨论时未确认执行者已停止。")
                # 保留起始或运行记录，后续只能核对原轮次，不能再次派发。
                raise
            finally:
                self.gateway._discussion_jobs.pop(workflow_id, None)
        if raw is None:
            raise RuntimeError("执行者未完成讨论，交接总结未更新。")
        try:
            decision = validate(raw, documents_enabled)
        except RuntimeError:
            await db(self.store.update_discussion, workflow_id, message["messageId"], state="failed")
            raise
        if documents_enabled and decision["summary"] is not None:
            from workflow_documents import collect, failure
            async def capture():
                if decision.get("document") is None:
                    return failure()
                agent = self.orchestrator.get_agent(target["agentId"])
                async with self.orchestrator._client_factory(agent.url, token=self.orchestrator._resolve_agent_token(agent)) as client:
                    return await collect(client, target["cwd"], decision.get("document"), [agent.token_file])
            try:
                decision["documentResult"] = await asyncio.wait_for(capture(), 30)
            except Exception:
                logging.getLogger(__name__).warning("讨论文档同步失败，保留旧正文。")
                decision["documentResult"] = failure()
        await self.gateway.event_batcher.flush()
        lock = self.gateway._control_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            await db(self.store.complete_discussion, workflow_id, message["messageId"], decision)
        return True
