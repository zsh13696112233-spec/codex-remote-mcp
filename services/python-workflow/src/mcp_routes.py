"""配置中心专用 MCP 下发接口。"""

import asyncio
import json
import logging

from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp_packages import ZIP_LIMIT
from skill_packages import SkillError
from skill_routes import read_limited

LOGGER = logging.getLogger(__name__)


async def mcp_api(request):
    manager = request.app.state.mcp_deployment
    try:
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        group = request.query_params.get("groupId") or None
        if request.method == "GET":
            if path == "/mcp-packages":
                return JSONResponse({"packages": await asyncio.to_thread(manager.store.packages, group), "enabled": manager.root is not None})
            if path == "/mcp-packages/machines":
                agents = await asyncio.to_thread(manager.gateway.registry.public)
                return JSONResponse({"agents": [a for a in agents if not group or a["groupId"] == group],
                                     "groups": await asyncio.to_thread(manager.gateway.registry.groups)})
            if path == "/mcp-packages/inventory":
                tasks = await asyncio.to_thread(manager.store.tasks, None, group)
                return JSONResponse({"items": [manager.store.public(task) for task in tasks]})
            if "package_id" in request.path_params:
                return JSONResponse(await asyncio.to_thread(manager.store.package, request.path_params["package_id"]))
            if "batch_id" in request.path_params:
                return JSONResponse(await asyncio.to_thread(manager.store.batch, request.path_params["batch_id"]))
            return JSONResponse({"deployments": await asyncio.to_thread(manager.store.batches, group)})
        manager.enabled()
        if path == "/mcp-packages":
            if request.headers.get("content-type", "").split(";")[0] not in {"application/zip", "application/octet-stream"}:
                raise SkillError("请上传 ZIP 文件。", 415)
            return JSONResponse(await asyncio.to_thread(manager.upload, await read_limited(request, ZIP_LIMIT), group), status_code=201)
        body = json.loads(await read_limited(request, 32768))
        if path == "/mcp-packages/groups/assign":
            return JSONResponse(await asyncio.to_thread(manager.store.assign, body))
        if path == "/mcp-deployments":
            return JSONResponse(await asyncio.to_thread(manager.create, body), status_code=202)
        if not isinstance(body, dict) or set(body) != {"requestId"}:
            raise SkillError("请提供操作请求编号。")
        await asyncio.to_thread(manager.store.action, request.path_params["task_id"], body["requestId"], request.path_params["action"])
        return JSONResponse({"accepted": True}, status_code=202)
    except (SkillError, ValueError) as error:
        message = str(error) if isinstance(error, SkillError) else "请求参数不正确。"
        return JSONResponse({"error": message}, status_code=getattr(error, "status", 400))
    except Exception as error:
        LOGGER.error("MCP 管理接口失败，类型=%s", type(error).__name__)
        return JSONResponse({"error": "MCP 服务处理失败。"}, status_code=500)


def mcp_routes():
    return [Route("/mcp-packages", mcp_api, methods=["GET", "POST"]),
            Route("/mcp-packages/groups/assign", mcp_api, methods=["POST"]),
            Route("/mcp-packages/machines", mcp_api, methods=["GET"]),
            Route("/mcp-packages/inventory", mcp_api, methods=["GET"]),
            Route("/mcp-packages/{package_id}", mcp_api, methods=["GET"]),
            Route("/mcp-deployments", mcp_api, methods=["GET", "POST"]),
            Route("/mcp-deployments/{batch_id}", mcp_api, methods=["GET"]),
            Route("/mcp-deployment-tasks/{task_id}/{action}", mcp_api, methods=["POST"])]
