"""Jira 领取内部接口与配置中心台账查询接口。"""

import json

from starlette.responses import JSONResponse
from starlette.routing import Route


async def internal_jira(request):
    from workflow_gateway import _validate_internal_write, _database_call, _internal_error_response
    try:
        gateway, supervisor, workflow_id, lease = await _validate_internal_write(request)
        if request.method == "GET":
            result = await _database_call(gateway.store.jira_context, workflow_id)
        else:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("请求体必须是对象。")
            result = await _database_call(gateway.store.jira_update, workflow_id,
                value.get("operation"), value.get("payload"), sidecar_supervisor_id=supervisor, lease_token=lease)
        return JSONResponse(result)
    except (ValueError, RuntimeError, PermissionError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


async def jira_runs(request):
    from workflow_gateway import _database_call, _internal_error_response
    try:
        before = request.query_params.get("before")
        return JSONResponse(await _database_call(request.app.state.gateway.store.jira_runs,
            request.query_params.get("groupId"), before=int(before) if before is not None else None,
            limit=int(request.query_params.get("limit", "50"))))
    except (ValueError, RuntimeError) as error:
        return _internal_error_response(error)


async def jira_capacity(request):
    """容量为提交建议；实际派发仍在中央写事务中原子占用机器。"""
    from workflow_gateway import _database_call, _internal_error_response, SIDECAR_LEASE_TIMEOUT_SEC
    from workflow_jira import compatible_agent
    try:
        gateway = request.app.state.gateway
        spec = gateway.store.normalize_spec(await request.json())
        if not spec.get("jira"):
            raise ValueError("批量容量查询仅支持 Jira 分组 SOP。")
        claims = await _database_call(gateway.store.machine_claims)
        candidates = (await gateway._eligible_group_executors([spec]))[spec["workflowId"]]
        rows = await _database_call(gateway.registry.rows)
        supervisors = []
        for key, row in rows.items():
            agent = gateway.registry.config_from_row(key, row)
            if (key in claims or row["test_status"] != "passed" or not agent.enabled
                    or not compatible_agent(agent) or "supervisor" not in agent.capabilities):
                continue
            if agent.orchestration_mode == "remote_sidecar":
                state = await _database_call(gateway.store.sidecar_status, key, timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC)
            else:
                state = gateway._supervisor_runtime.get(key, {})
            if state.get("connectionStatus") == "online":
                supervisors.append(key)
        count = 0
        # 优先使用纯监督机，为双角色机器保留执行位置。
        available = set(candidates)
        used = set()
        for supervisor in sorted(supervisors, key=lambda key: (key in candidates, key)):
            if supervisor in used:
                continue
            executors = available - {supervisor}
            if not executors:
                continue
            executor = min(executors, key=lambda key: (key in supervisors, key))
            used.update((supervisor, executor))
            available.difference_update((supervisor, executor))
            count += 1
        pending = await _database_call(gateway.store.pending_allocation_specs)
        # 同一任务尚有排队项时合并下一次触发。其他组不可运行的队列不能阻塞本组空闲机器。
        waiting = any(item.get("taskDefinitionId") == spec.get("taskDefinitionId") for item in pending)
        return JSONResponse({"capacity": 0 if waiting else min(100, count)})
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        return _internal_error_response(error)


def jira_routes():
    return [Route("/internal/v1/workflows/{workflow_id}/jira", internal_jira, methods=["GET", "POST"]),
            Route("/jira-runs", jira_runs, methods=["GET"]),
            Route("/jira-capacity", jira_capacity, methods=["POST"])]
