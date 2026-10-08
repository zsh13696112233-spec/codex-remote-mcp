"""正式步骤业务结果协议；普通讨论与历史步骤不使用此协议。"""
import hashlib
import json
from typing import Any

from workflow_documents import DOCUMENT_SCHEMA

VERSION = 1
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "outcome", "reason", "jiraComment", "document"],
    "properties": {
        "summary": {"type": "string", "maxLength": 20000},
        "outcome": {"type": "string", "enum": ["success", "no_task", "blocked"]},
        "reason": {"type": "string", "maxLength": 2000},
        "document": DOCUMENT_SCHEMA,
        "jiraComment": {
            "type": "object", "additionalProperties": False,
            "required": ["status", "issueKey", "reference", "detail"],
            "properties": {
                "status": {"type": "string", "enum": ["succeeded", "failed", "unknown", "not_applicable"]},
                "issueKey": {"type": "string", "maxLength": 128},
                "reference": {"type": "string", "maxLength": 1000},
                "detail": {"type": "string", "maxLength": 2000},
            },
        },
    },
}


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        raise ValueError("步骤业务结果格式无效。")
    for key, limit in (("summary", 20000), ("reason", 2000)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError("步骤业务结果文本无效或超过容量限制。")
    if not value["summary"].strip() or value["outcome"] not in ("success", "no_task", "blocked"):
        raise ValueError("步骤业务结果无效。")
    if value["outcome"] != "success" and not value["reason"].strip():
        raise ValueError("提前结束必须提供原因。")
    document = value["document"]
    if document is not None:
        if not isinstance(document, dict) or set(document) != {"name", "path"}:
            raise ValueError("步骤文档字段无效。")
        for key, limit in (("name", 160), ("path", 4096)):
            if not isinstance(document[key], str) or not document[key].strip() or len(document[key]) > limit:
                raise ValueError("步骤文档字段无效。")
    jira = value["jiraComment"]
    if not isinstance(jira, dict) or set(jira) != {"status", "issueKey", "reference", "detail"}:
        raise ValueError("Jira 备注结果格式无效。")
    if jira["status"] not in ("succeeded", "failed", "unknown", "not_applicable"):
        raise ValueError("Jira 备注状态无效。")
    for key, limit in (("issueKey", 128), ("reference", 1000), ("detail", 2000)):
        if not isinstance(jira[key], str) or len(jira[key]) > limit:
            raise ValueError("Jira 备注结果文本无效。")
    if jira["status"] == "succeeded" and not jira["issueKey"].strip():
        raise ValueError("备注成功必须提供 Jira 编号。")
    if value["outcome"] == "blocked" and jira["status"] != "succeeded" and not jira["detail"].strip():
        raise ValueError("必须说明未确认 Jira 备注成功的原因。")
    return value


def parse(text: str) -> dict[str, Any]:
    return validate(json.loads(text))


def instruction(workflow_id: str, node_id: str, capture_document: bool | None) -> str:
    marker = hashlib.sha256(f"{workflow_id}:{node_id}".encode()).hexdigest()[:24]
    return (
        "\n【平台正式步骤结果协议】最终按指定 JSON 结构返回，不以普通文字请求停止。"
        "summary 为完整交接总结；outcome 为 success（可以继续）、no_task（无待处理任务）"
        "或 blocked（必要条件不满足，无法继续）。提前结束必须用 reason 说明原因。"
        "自身可纠正的命令错误先修正，保留职责明确规定的非阻断项，不夸大为阻断。"
        "确认阻断后停止业务写入，允许在原授权内向已锁定的唯一 Jira 添加收尾评论；"
        "职责明确要求时，还允许将该 Jira 的处理角色字段改为 Natural Person，其他业务写入停止。"
        "处理角色修改前后均须回查，结果不明不盲目重发；失败仍尝试评论并返回 blocked。"
        "summary 和 jiraComment.detail 披露角色修改结果，jiraComment.status 仅表示评论结果。"
        "评论说明阻断步骤、原因、已完成事项、未执行事项、恢复条件；"
        "不得泄露凭据、内部会话编号、执行机地址，不改变状态、负责人或创建新任务。"
        f"评论去重标记为 SOP-STOP-{marker}，先查已有评论，存在该标记则复用；"
        "提交结果不明先回查，无法确认时不要盲目重发。"
        "jiraComment.status 为 succeeded/failed/unknown/not_applicable，issueKey 为锁定编号，"
        "reference 为已确认评论编号或链接（没有则空字符串），detail 说明备注结果。"
        "无唯一 Jira、无工具或无权限时不要猜测或重新选单，说明原因；"
        "备注失败或未知仍返回 blocked，由平台结束，不再执行下一步。"
        "success 和 no_task 不需写停止评论，jiraComment 使用 not_applicable 并说明。"
        + ("document 沿用主文档要求。" if capture_document else "document 必须为 null，不采集主文档。")
    )


def message(termination: dict[str, Any]) -> str:
    title = "因阻断结束" if termination["outcome"] == "blocked" else "无待处理任务，已结束"
    jira = termination["jiraComment"]
    labels = {"succeeded": "已备注", "failed": "备注失败", "unknown": "备注结果未确认", "not_applicable": "未备注"}
    return f"{title}。\n原因：{termination['reason']}\nJira：{labels[jira['status']]}。{jira['detail']}"
