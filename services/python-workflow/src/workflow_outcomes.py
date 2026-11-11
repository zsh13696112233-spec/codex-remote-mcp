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


def schema(version: int, *, jira: bool = False, delivery_version: int = 1) -> dict[str, Any]:
    if type(version) is not int or version not in (1, 2, 3, 4, 5):
        raise ValueError("步骤结果协议版本无效。")
    result = {1: SCHEMA_V1, 2: SCHEMA, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V4}[version]
    if jira:
        result = copy.deepcopy(result)
        result["required"].append("jiraDelivery")
        result["properties"]["jiraDelivery"] = {
            "type": "object", "additionalProperties": False,
            "required": ["status", "reference", "summary"],
            "properties": {
                "status": {"type": "string", "enum": ["submitted", "completed", "not_submitted", "blocked", "unknown", "not_applicable"]},
                "reference": {"type": "string", "maxLength": 1000},
                "summary": {"type": "string", "maxLength": 2000},
            },
        }
    if jira and delivery_version >= 2:
        from workflow_jira import DELIVERY_FIELDS
        result["properties"]["jiraDelivery"]["properties"].update(copy.deepcopy(DELIVERY_FIELDS))
        result["properties"]["jiraDelivery"]["required"] += list(DELIVERY_FIELDS)
    if jira and delivery_version >= 3:
        from workflow_jira import COMPLETION_STATUSES
        result["properties"]["jiraDelivery"]["properties"]["status"]["enum"] += list(COMPLETION_STATUSES)
    return result


def validate(value: Any, version: int = VERSION, *, jira: bool = False, delivery_version: int = 1) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(schema(version, jira=jira, delivery_version=delivery_version)["required"]):
        raise ValueError("步骤业务结果格式无效。")
    if jira:
        from workflow_jira import COMPLETION_STATUSES, validate_delivery
        validate_delivery(value["jiraDelivery"], delivery_version)
        if value["jiraDelivery"]["status"] in COMPLETION_STATUSES and value["outcome"] != "success":
            raise ValueError("无需提交或审核返工的交接完成回执必须对应步骤正常结束。")
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


def parse(text: str, version: int = VERSION, *, jira: bool = False, delivery_version: int = 1) -> dict[str, Any]:
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
    value = validate(value, version, jira=jira, delivery_version=delivery_version)
    if normalized:
        # 原始回答保留在执行事件中；日志不包含业务正文或账号。
        logging.getLogger(__name__).warning("阻断结果开发人状态已规范化：not_applicable -> unavailable。")
    return value


def instruction(workflow_id: str, node_id: str, capture_document: bool | None, version: int = VERSION, *, defer_blocked: bool = False, advance_mode: str | None = None) -> str:
    marker = hashlib.sha256(f"{workflow_id}:{node_id}".encode()).hexdigest()[:24]
    result = (
        "\n【平台正式步骤结果协议】最终按指定 JSON 结构返回，不以普通文字请求停止。"
        "summary 为完整交接总结；summary、职责要求的总结文档沿用 SOP 原有的业务结论和说明格式，"
        "不套用统一的分段标题，不因此新增评论或业务操作。"
        "结论名称与正文结构由当前 SOP 决定；"
        "首句写简短业务结论，涉及提交时再说明本轮实际提交结果，不夹带工具、路径或排查过程。"
        "涉及 SVN 等版本提交时，已提交须给出实际版本号；未提交、无需提交、提交结果待核实分别如实说明，"
        "不把历史版本当作本轮提交，不从流程阻断推断未提交；不涉及提交的任务不硬加 SVN 状态。"
        "业务结论按 SOP 和证据判断，保留通过、返工、验证受限等实际结论；尚未形成审核结论时说明未完成及原因，"
        "不把流程阻断等同于审核不通过或返工，审核通过后提交或流转失败仍保留审核结论。"
        "需要人工处理的事项紧接结论说明由谁处理、具体动作及所需确认结果或材料；"
        "未确认处理人时写待指定，不猜测人员或职责，不把拟转交写成已接单，也不编造待办。"
        "再按 SOP 原有格式记录执行结果、必要交付、未决事项及证据，不为非代码任务强加审核或提交章节；"
        "不要把关键待办放在末尾，不重复平台按钮操作提示。"
        "outcome 为 success（本步骤职责及必要交接完成，可以继续）、no_task（无待处理任务）"
        "或 blocked（必要条件不满足，无法完成步骤并继续）。业务验收不合格但已按 SOP 完成退回和交接时仍是 success；业务结论如实保留。流程通过只表示全部必要步骤及确认完成，不表示业务合格或已经提交。提前结束必须用 reason 说明原因。"
        "遇阻时 reason 先简明说明阻碍，再明确需要用户提供的信息、授权或决定；没有需要用户处理的事项时如实说明，不编造要求。"
        "必要阻断、非阻断的验证限制及其他任务的问题分开写；不影响当前步骤的问题不混列为恢复前提。"
        "用户可见正文使用普通中文和业务步骤名称，不使用内部角色名、英文状态码或原始错误堆砌结论。"
        "自身可纠正的命令错误先修正，保留职责明确规定的非阻断项，不夸大为阻断。"
        "Jira 评论、状态、处理角色和负责人变更均按当前步骤职责执行，平台不指定业务动作、接收人或执行顺序。"
        "职责未要求的业务操作不自行增加；写入前核对当前工单及实际状态，写入后回查，"
        "结果不明先查询，不盲目重复操作；发现并发变更时停止覆盖并如实报告。"
        "不得泄露凭据、内部会话编号、执行机地址。"
        "职责明确要求的本次总结文档可在原工作目录和写入权限内写入、回读并交付；"
        "无法安全写入或回读时在 summary 说明，不虚报交付，不重发结果不明的写入，不改变阻断结论。"
        "jiraComment.status 为 succeeded/failed/unknown/not_applicable，issueKey 为锁定编号，"
        "reference 为已确认评论编号或链接（没有则空字符串），detail 说明备注结果。"
        "无唯一 Jira、无工具或无权限时不要猜测或重新选单，说明原因；"
        "jiraComment 如实报告停止评论结果，不适用时使用 not_applicable 并说明；该字段不要求新增评论。"
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
           "jiraDeveloper 只报告真实开发人读取结果，不得将其他转交对象填为开发人。"
           "缺失、多人或读取失败时账号类型和账号填空字符串并说明原因，不猜测，不使用 assignee 或发起人。"
           "已经因业务必要条件判定 blocked 的，不因开发人读取失败改变该决定；不得选择钉钉群、人员或发送通知。" if version in (2, 3, 4, 5) else "")
        + ("" if version in (3, 4, 5) else "document 沿用主文档要求。" if capture_document else "document 必须为 null，不采集主文档。")
    )

    if advance_mode == "automatic":
        result = (
            "\n【全自动执行与结束条件】默认持续完成当前步骤。工具报错、测试条件未准备、验证未完成、"
            "证据缺失或工作量较大，均不能单独作为阻断理由；不能认定完成不等于不能继续执行。"
            "遇到问题先在原授权内检查原因，实际修正调用、查询必要信息、准备测试条件、修复并重测，"
            "或采用满足同一验证目标的可行替代方式，不以建议重试或请求重新执行代替实际操作。"
            "问题解决后立即继续当前步骤剩余工作；局部受阻时先完成不受影响的独立必要工作。"
            "不机械重试确定性错误，不无限重试，不绕过权限；写入结果不明先查询实际状态，"
            "已成功不重复执行，仍无法确认不盲目重发，可继续只读核验。"
            "只有实际证据证明必要工作在当前授权和可用手段下无法继续，且独立必要工作已完成，"
            "才可认定真实阻断；明确权限拒绝等无需无意义重试。保留 SOP 的硬性停止条件、"
            "权限边界、非阻断事项和合法交接标准，不降低证据标准，不把审核未通过、验证受限"
            "或未提交代码自动等同于流程阻断。"
            "阻断 reason 说明受阻的必要工作、实际错误、已执行的处理及结果、可行替代方式为何不可用"
            "和最小外部恢复条件；不存在可行替代方式时说明检查依据，不编造尝试。"
            "最终输出前检查是否仍有可执行的必要工作：有则继续调用工具。最终 JSON 仅用于步骤结束，"
            "不必在每次遇到问题时选择结果；禁止以‘验证未完成，需补齐后重新执行’为由结束。"
            "以上处理均发生在正式阻断之前，不授权恢复已结束的工作流。\n"
        ) + result
        result = result.replace("或 blocked（必要条件不满足，无法完成步骤并继续）", "或 blocked（已按上述条件确认真实阻断，无法继续）")

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
        result += (
            "本运行采用半自动人工等待。首次或再次发现业务阻碍时返回 blocked，由平台等待人工回复，"
            "本次不代表正式结束，禁止执行停止评论和负责人转交等阻断收尾；"
            "等待不授权执行职责中的正式终止收尾。jiraComment 使用 not_applicable，说明等待人工处理。"
            "说明等待人工处理、本轮尚未结束，不暗示回复消息或补齐条件即会自动恢复。"
            "等待期间不允许进入后续步骤，由用户操作当前有效等待按钮。"
        )
    else:
        result += (
            "本次返回 blocked 为正式阻断结束，本轮因阻断停止，补齐条件不会自动恢复本轮；不再执行下一步。"
            "正式阻断收尾的业务操作仅按本步骤职责执行，不改变阻断结论。"
            f"若职责要求停止评论，可使用本步骤稳定去重标记 SOP-STOP-{marker}；"
            "先查已有评论，存在该标记则复用；提交结果不明先回查，无法确认时不要盲目重发。"
            "提供标记不构成新增评论的要求或授权。"
        )
    return result


def message(termination: dict[str, Any]) -> str:
    title = "因阻断结束" if termination["outcome"] == "blocked" else "无待处理任务，已结束"
    jira = termination["jiraComment"]
    labels = {"succeeded": "已备注", "failed": "备注失败", "unknown": "备注结果未确认", "not_applicable": "未备注"}
    return f"{title}。\n原因：{termination['reason']}\nJira：{labels[jira['status']]}。{jira['detail']}"
