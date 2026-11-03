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
    return [Route('/jira-collections', jira_collection, methods=['POST']),
            Route('/jira-collections/{request_id}', jira_collection, methods=['GET', 'DELETE']),
            Route('/jira-reservations', jira_reservation, methods=['POST']),
            Route('/jira-pool', jira_pool_board, methods=['GET']),
            Route("/internal/v1/workflows/{workflow_id}/jira", internal_jira, methods=["GET", "POST"]),
            Route("/jira-runs", jira_runs, methods=["GET"]),
            Route("/jira-runs/{workflow_id}", jira_run_detail, methods=["GET"]),
            Route("/jira-capacity", jira_capacity, methods=["POST"])]


async def jira_collection(request):
    from workflow_gateway import _database_call, _internal_error_response
    import jira_pool
    try:
        store = request.app.state.gateway.store
        request_id = request.path_params.get('request_id')
        if request.method == 'DELETE':
            await _database_call(jira_pool.cancel, store, request_id)
            return JSONResponse({'cancelled': True})
        if request.method == 'GET':
            return JSONResponse(await _database_call(jira_pool.collection, store, request_id))
        return JSONResponse(await _database_call(jira_pool.request_collection, store, await request.json()), status_code=202)
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        return _internal_error_response(ValueError('采集请求无效。') if isinstance(error, (KeyError, TypeError)) else error)


async def jira_reservation(request):
    from workflow_gateway import _database_call, _internal_error_response
    import jira_pool
    try:
        body = await request.json()
        value = await _database_call(jira_pool.reserve, request.app.state.gateway.store,
            body['requestId'], body['reservationId'])
        if value and not await jira_pool.verify_reservation(request.app.state.gateway, value):
            return JSONResponse({'reservation': None, 'state': 'waiting_verification'})
        return JSONResponse({'reservation': value})
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        return _internal_error_response(ValueError('预约请求无效。') if isinstance(error, (KeyError, TypeError)) else error)


async def jira_pool_board(request):
    from workflow_gateway import _database_call, _internal_error_response
    from jira_pool import board
    try:
        before = request.query_params.get('before')
        return JSONResponse(await _database_call(board, request.app.state.gateway.store,
            request.query_params.get('groupId', ''), int(before) if before else None, 50,
            request.query_params.get('project', ''), request.query_params.get('task', ''), request.query_params.get('state', '')))
    except (ValueError, RuntimeError) as error:
        return _internal_error_response(error)


async def jira_run_detail(request):
    import uuid
    from workflow_gateway import _database_call, _internal_error_response
    from jira_pool import execution_detail
    try:
        try:
            workflow_id = str(uuid.UUID(request.path_params['workflow_id']))
        except ValueError:
            raise ValueError('运行编号无效。') from None
        result = await _database_call(execution_detail, request.app.state.gateway.store, workflow_id)
        if result is None:
            return JSONResponse({'error': '未找到 Jira 运行记录。'}, status_code=404)
        return JSONResponse(result)
    except (ValueError, RuntimeError) as error:
        return _internal_error_response(error)
