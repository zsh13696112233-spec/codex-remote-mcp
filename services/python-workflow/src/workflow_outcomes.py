"""正式步骤业务结果协议；普通讨论与历史步骤不使用此协议。"""
import copy
import hashlib
import json
import logging
from typing import Any

from workflow_documents import DOCUMENT_SCHEMA, SEMI_DOCUMENTS_SCHEMA

VERSION = 2
SCHEMA_V1 = {
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


SCHEMA = copy.deepcopy(SCHEMA_V1)
SCHEMA["required"].append("jiraDeveloper")
SCHEMA["properties"]["jiraDeveloper"] = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "accountType", "accountId", "displayName", "detail"],
    "properties": {
        "status": {"type": "string", "enum": ["resolved", "empty", "multiple", "unavailable", "not_applicable"],
                   "description": "阻断时禁止 not_applicable；未锁定唯一工单、无权限或读取失败使用 unavailable，并清空人员信息、说明原因。"},
        "accountType": {"type": "string", "enum": ["", "accountId", "key", "name"]},
        "accountId": {"type": "string", "maxLength": 256},
        "displayName": {"type": "string", "maxLength": 160},
        "detail": {"type": "string", "maxLength": 2000},
    },
}


SCHEMA_V3 = copy.deepcopy(SCHEMA)
SCHEMA_V3["required"].remove("document")
SCHEMA_V3["required"].append("documents")
del SCHEMA_V3["properties"]["document"]
SCHEMA_V3["properties"]["documents"] = {
    "type": "array", "items": DOCUMENT_SCHEMA["anyOf"][1],
}


# 版本 5 沿用版本 4 的结果字段；采集范围由运行冻结版本决定。
SCHEMA_V4 = copy.deepcopy(SCHEMA_V3)
SCHEMA_V4["properties"]["documents"] = SEMI_DOCUMENTS_SCHEMA


def schema(version: int) -> dict[str, Any]:
    if type(version) is not int or version not in (1, 2, 3, 4, 5):
        raise ValueError("步骤结果协议版本无效。")
    return {1: SCHEMA_V1, 2: SCHEMA, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V4}[version]


def validate(value: Any, version: int = VERSION) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(schema(version)["required"]):
        raise ValueError("步骤业务结果格式无效。")
    for key, limit in (("summary", 20000), ("reason", 2000)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError("步骤业务结果文本无效或超过容量限制。")
    if not value["summary"].strip() or value["outcome"] not in ("success", "no_task", "blocked"):
        raise ValueError("步骤业务结果无效。")
    if value["outcome"] != "success" and not value["reason"].strip():
        raise ValueError("提前结束必须提供原因。")
    documents = value["documents"] if version in (3, 4, 5) else ([] if value["document"] is None else [value["document"]])
    if not isinstance(documents, list):
        raise ValueError("步骤文档清单格式无效。")
    if version in (4, 5):
        from workflow_documents import validate_descriptors
        validate_descriptors(documents)
    for document in documents:
        if not isinstance(document, dict) or set(document) != ({"name", "path", "primary"} if version in (4, 5) else {"name", "path"}):
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
    if version in (2, 3, 4, 5):
        developer = value["jiraDeveloper"]
        if not isinstance(developer, dict) or set(developer) != set(SCHEMA["properties"]["jiraDeveloper"]["required"]):
            raise ValueError("Jira 开发人结果格式无效。")
        if developer["status"] not in ("resolved", "empty", "multiple", "unavailable", "not_applicable"):
            raise ValueError("Jira 开发人读取状态无效。")
        for key, limit in (("accountId", 256), ("displayName", 160), ("detail", 2000)):
            if not isinstance(developer[key], str) or len(developer[key]) > limit:
                raise ValueError("Jira 开发人字段无效或超过容量限制。")
        if developer["status"] == "resolved":
            if developer["accountType"] not in ("accountId", "key", "name") or not developer["accountId"].strip() or not jira["issueKey"].strip():
                raise ValueError("开发人必须来自唯一 Jira 并提供稳定账号标识。")
        elif developer["accountType"] != "" or developer["accountId"] != "":
            raise ValueError("无法确定开发人时不得填写账号。")
        if value["outcome"] == "blocked" and developer["status"] == "not_applicable":
            raise ValueError("阻断时必须报告开发人读取结果。")
    return value


def parse(text: str, version: int = VERSION) -> dict[str, Any]:
    value = json.loads(text)
    developer = value.get("jiraDeveloper") if isinstance(value, dict) else None
    normalized = (
        version in (2, 3, 4, 5) and isinstance(developer, dict)
        and value.get("outcome") == "blocked"
        and developer.get("status") == "not_applicable"
        and all(developer.get(key) == "" for key in ("accountType", "accountId", "displayName"))
        and isinstance(developer.get("detail"), str)
    )
    if normalized:
        developer["status"] = "unavailable"
    value = validate(value, version)
    if normalized:
        # 原始回答保留在执行事件中；日志不包含业务正文或账号。
        logging.getLogger(__name__).warning("阻断结果开发人状态已规范化：not_applicable -> unavailable。")
    return value


def instruction(workflow_id: str, node_id: str, capture_document: bool | None, version: int = VERSION, *, defer_blocked: bool = False) -> str:
    marker = hashlib.sha256(f"{workflow_id}:{node_id}".encode()).hexdigest()[:24]
    result = (
        "\n【平台正式步骤结果协议】最终按指定 JSON 结构返回，不以普通文字请求停止。"
        "summary 为完整交接总结，先写本步结论、需要用户处理或确认的事项及未决风险，再写执行细节和证据；"
        "不要把关键待办放在末尾，不重复平台按钮操作提示；"
        "outcome 为 success（可以继续）、no_task（无待处理任务）"
        "或 blocked（必要条件不满足，无法继续）。提前结束必须用 reason 说明原因。"
        "遇阻时 reason 先简明说明阻碍，再明确需要用户提供的信息、授权或决定；没有需要用户处理的事项时如实说明，不编造要求。"
        "自身可纠正的命令错误先修正，保留职责明确规定的非阻断项，不夸大为阻断。"
        "确认阻断后停止业务写入，允许在原授权内向已锁定的唯一 Jira 添加收尾评论；"
        "先写入阻断评论并回查确认成功，之后必须在原授权内尝试转交负责人；评论未确认成功不得转交。"
        "负责人转交是 blocked 分支明确允许的收尾操作，不代表审核通过；其他业务写入停止。"
        "本阻断收尾规则优先于职责中的旧收尾规则，Jira 状态和处理角色保持不变，不得改为 Natural Person。"
        "实际读取 Public.Jira.Fields.Developer 指定的字段：唯一开发人作为目标负责人；"
        "为空或多人时使用 Public.Jira.Manager；读取失败先重试读取一次，仍失败才使用 Manager。"
        "Manager 未配置、账号无法确认或无转交权限时不得猜测，记录待人工处理事项。"
        "转交前回查唯一工单的当前负责人、状态和处理角色，已为目标负责人则不重复写入；"
        "仅更新负责人，转交后回查负责人、状态和处理角色，确认实际结果。"
        "写入结果不明时先回查，仍不明不得盲目重复操作；发现并发变更时停止覆盖并记录待人工核查。"
        "职责明确要求的本次总结文档属于允许的收尾交付，可在原工作目录和写入权限内写入、回读并交付；"
        "不得据此修改业务代码、提交版本、移动业务方案或继续业务流转，不改变阻断结论。"
        "无法安全写入或回读时，在 summary 中保留完整总结并说明文档未生成或未确认的原因，"
        "不虚报交付，不重发结果不明的写入。"
        "summary、jiraComment.detail、原授权内的业务日志和职责要求的总结文档记录转交前负责人、"
        "目标负责人、转交后实际负责人及成功、失败、结果不明或未执行的原因；无法回查不得虚报。"
        "日志或总结文档无法写入或回读时在 summary 说明，不掩盖已确认的转交结果。"
        "无论转交成功与否最终仍返回 blocked，失败或结果不明说明待人工处理事项。"
        "jiraComment.status 仅表示评论结果，不因转交失败改写已确认成功的评论状态。"
        "评论说明阻断步骤、原因、已完成事项、未执行事项、恢复条件；"
        "不得泄露凭据、内部会话编号、执行机地址，不改变 Jira 状态和处理角色或创建新任务。"
        f"评论去重标记为 SOP-STOP-{marker}，先查已有评论，存在该标记则复用；"
        "提交结果不明先回查，无法确认时不要盲目重发。"
        "jiraComment.status 为 succeeded/failed/unknown/not_applicable，issueKey 为锁定编号，"
        "reference 为已确认评论编号或链接（没有则空字符串），detail 说明备注结果。"
        "无唯一 Jira、无工具或无权限时不要猜测或重新选单，说明原因；"
        "备注失败或未知仍返回 blocked，由平台结束，不再执行下一步。"
        "success 和 no_task 不需写停止评论，jiraComment 使用 not_applicable 并说明。"
        + ("凡本步骤处理了唯一 Jira，无论成功或阻断，都在 jiraComment.issueKey 返回该编号，"
           "并从该 Jira 实际读取 Public.Jira.Fields.Developer 指定的开发人字段，用于结束卡片展示；"
           "评论 not_applicable 不代表编号或开发人不适用，不为展示新增评论或修改 Jira。"
           "jiraDeveloper.status 为 resolved/empty/multiple/unavailable；未处理 Jira 的非阻断步骤可为 not_applicable。"
           "outcome 为 blocked 时禁止使用 not_applicable；未锁定唯一 Jira、无权限或读取失败均用 unavailable，"
           "accountType、accountId、displayName 留空，detail 说明原因。"
           "jiraComment 与 jiraDeveloper 状态独立：未锁定工单时评论可为 not_applicable，但开发人必须为 unavailable。"
           "开发人字段为空时使用 empty；开发人为空、多人、无法获取或不适用均不单独阻断业务。"
           "detail 尽量说明读取情况，但开发人说明为空不影响结果有效性；不得猜测或补填人员。"
           "唯一人员使用实际账号字段 accountId、key、name 中可用的稳定标识，并以 accountType 标明类型，"
           "accountId 填该字段原值，displayName 必须填写开发人对象实际返回的显示名，detail 说明读取结果。"
           "平台按显示名精确匹配钉钉人员；不得把用户名或拼音填为显示名，不得自行翻译或生成姓名。"
           "多人时 displayName 填实际读取的姓名列表（不超过160字符），不选择其中一人。"
           "jiraDeveloper 只报告真实开发人读取结果，不得将兜底转交对象 Manager 填为开发人。"
           "缺失、多人或读取失败时账号类型和账号填空字符串并说明原因，不猜测，不使用 assignee 或发起人。"
           "已经因业务必要条件判定 blocked 的，不因开发人读取失败改变该决定；不得选择钉钉群、人员或发送通知。" if version in (2, 3, 4, 5) else "")
        + ("" if version in (3, 4, 5) else "document 沿用主文档要求。" if capture_document else "document 必须为 null，不采集主文档。")
    )

    if version == 3:
        result += (
            "documents 返回本步骤实际交付的全部文档，每项为 name（展示名称）和 path（实际工作目录内文件路径）。"
            "无文档返回空数组。只交付 UTF-8 .md/.txt，每份最大512 KiB；不返回正文，"
            "不列入参考资料、日志、代码、敏感配置或凭据。不限制文档数量。"
            "成功、无任务或遇阻均报告已经交付的文档，不为填充清单额外生成文件。"
            "职责明确要求的总结文档属于正式交付，应按职责要求生成、回读并列入 documents，不属于填充清单；"
            "未要求生成总结文件的步骤不额外生成。无论是否交付文件，summary 都须保留完整文字总结。"
        )
    if version in (4, 5):
        from workflow_documents import SEMI_INSTRUCTION
        result += SEMI_INSTRUCTION if capture_document else "documents 必须为空数组，本步骤不采集文档。"
    if version == 5 and capture_document:
        result += "职责要求的独立总结属于交付文档，应生成、回读并列入 documents；没有交付文档返回空数组，不额外生成文件。"
    if defer_blocked:
        start = result.index("确认阻断后停止业务写入")
        end = result.index("jiraComment.status 为", start)
        result = result[:start] + (
            "本运行采用半自动人工等待。首次或再次发现业务阻碍时返回 blocked，由平台等待人工回复，"
            "本次不代表正式结束，禁止执行停止评论和负责人转交等阻断收尾，Jira 状态和处理角色保持不变。"
            "本规则优先于历史角色职责中的立即停止收尾要求。jiraComment 使用 not_applicable，说明等待人工处理。"
        ) + result[end:]
        result = result.replace("备注失败或未知仍返回 blocked，由平台结束，不再执行下一步。", "等待期间不允许进入后续步骤；正式超时后平台另行授权阻断收尾。")
    return result


def message(termination: dict[str, Any]) -> str:
    title = "因阻断结束" if termination["outcome"] == "blocked" else "无待处理任务，已结束"
    jira = termination["jiraComment"]
    labels = {"succeeded": "已备注", "failed": "备注失败", "unknown": "备注结果未确认", "not_applicable": "未备注"}
    return f"{title}。\n原因：{termination['reason']}\nJira：{labels[jira['status']]}。{jira['detail']}"
