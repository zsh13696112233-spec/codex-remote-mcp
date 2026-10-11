"""步骤思考强度的边界校验和执行服务能力映射。"""
from typing import Any

EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class ModelsUnavailable(RuntimeError):
    """执行服务能力不可读取；可安全展示的中文错误。"""


def normalize_effort(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("思考强度无效。")
    return value


async def read_models(client) -> list[dict[str, Any]]:
    models = []
    cursor = None
    seen = set()
    for _ in range(100):
        params = {"limit": 100, "includeHidden": True}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.request("model/list", params)
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise ValueError("执行服务模型列表格式无效。")
        for entry in response["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                raise ValueError("执行服务模型列表格式无效。")
            efforts = entry.get("supportedReasoningEfforts") or []
            if not isinstance(efforts, list):
                raise ValueError("执行服务思考强度列表格式无效。")
            models.append({"model": entry["model"], "supportedReasoningEfforts": [
                x["reasoningEffort"] for x in efforts if isinstance(x, dict)
                and isinstance(x.get("reasoningEffort"), str) and x["reasoningEffort"] in EFFORTS],
                "defaultReasoningEffort": entry.get("defaultReasoningEffort") if isinstance(entry.get("defaultReasoningEffort"), str) and entry["defaultReasoningEffort"] in EFFORTS else None})
        cursor = response.get("nextCursor")
        if cursor is None:
            return models
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("执行服务模型列表分页异常。")


def require_effort(models, model: str | None, effort: str) -> None:
    if not any(x["model"] == model and effort in x["supportedReasoningEfforts"] for x in models):
        raise ValueError("所选步骤模型不支持该思考强度，请刷新选项后重新选择。")
