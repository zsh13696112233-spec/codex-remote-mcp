"""监控执行详情的公开投影；原始审计事件不作修改。"""
import hashlib
import re
from typing import Any


def public_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # 自由文字中的常见凭据、地址及内部标识不进入监控详情。
    value = re.sub(r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[已隐藏凭据]", value)
    value = re.sub(r"(?i)\bBearer\s+\S+", "[已隐藏凭据]", value)
    value = re.sub(r'''(?im)(["']?(?:[\w-]*(?:token|password|passwd|secret|api[_-]?key)|authorization|thread[_-]?id|turn[_-]?id|session[_-]?id|agent[_-]?id)["']?\s*[:=]\s*).*$''', r"\1[已隐藏]", value)
    value = re.sub(r"\bsk-[\w-]+", "[已隐藏凭据]", value)
    value = re.sub(r"(?i)(?:https?|wss?|ssh)://\S+", "[已隐藏地址]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[已隐藏地址]", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "[已隐藏编号]", value)
    value = re.sub(r"(?i)data:image/[^\s]+", "[图片内容已省略]", value)
    return value[:8000] + ("\n【内容过长，已省略】" if len(value) > 8000 else "")


def monitor_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """只投影公开摘要及工具状态，不返回参数、命令、异常或原始推理。"""
    payload = event["payload"]
    if event["type"] == "chat.assistant.progress":
        return {**event, "payload": {"messageId": payload.get("messageId"),
                                    "text": public_text(payload.get("text"))}}
    if not event["type"].startswith("appserver."):
        return event
    # 历史及远端事件只验证了外层 payload，详情字段属于跨服务输入。
    message = payload.get("message")
    params = message.get("params") if isinstance(message, dict) else None
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if not isinstance(kind, str):
        return None
    completed = event["type"] == "appserver.item/completed"
    title, body = "", ""
    if kind == "reasoning" and completed:
        title = "思考摘要"
        summary = item.get("summary")
        if not isinstance(summary, list):
            return None
        body = "\n\n".join(public_text(p if isinstance(p, str) else p.get("text"))
                           for p in summary if isinstance(p, (str, dict)))
    elif kind == "agentMessage" and completed:
        if event["source"] in {"supervisor", "assistant"} or item.get("phase") != "commentary":
            return None
        title, body = "执行说明", public_text(item.get("text"))
    else:
        title = {"commandExecution": "执行命令", "fileChange": "修改文件",
                 "webSearch": "搜索资料", "imageView": "查看图片",
                 "imageGeneration": "生成图片", "mcpToolCall": "业务工具",
                 "dynamicToolCall": "业务工具", "collabToolCall": "协作任务",
                 "collabAgentToolCall": "协作任务"}.get(kind, "")
        if event["source"] == "supervisor":
            return None  # 编排轮询、派发等内部操作不进入详情。
        if title:
            failed = (item.get("status") in ("failed", "declined", "cancelled")
                      or bool(item.get("error")) or item.get("success") is False
                      or item.get("exitCode") not in (None, 0)
                      or isinstance(item.get("result"), dict) and item["result"].get("isError"))
            body = "未成功" if failed else "已完成" if completed else "执行中"
            title += " · " + body
            if completed and not failed:
                output = item.get("aggregatedOutput")
                result = item.get("result")
                parts = item.get("contentItems") or (result.get("content") if isinstance(result, dict) else None)
                if isinstance(parts, list):
                    output = "\n".join(part.get("text", "") for part in parts
                                       if isinstance(part, dict) and part.get("type") in ("text", "inputText")
                                       and isinstance(part.get("text"), str))
                if isinstance(output, str) and output.strip():
                    body += "\n" + public_text(output)
    if not title or not body:
        return None
    identity = "|".join(str(v) for v in (event["source"], event.get("nodeId"),
                                         params.get("turnId"),
                                         item.get("id") or event["sequence"]))
    return {**event, "type": "monitor.execution.detail", "payload": {
        "key": hashlib.sha256(identity.encode()).hexdigest(), "title": title,
        "text": public_text(body), "completed": completed,
        "truncated": bool(payload.get("truncated"))}}
