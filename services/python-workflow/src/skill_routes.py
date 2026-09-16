"""Skill 管理 HTTP 边界，浏览器经配置中心代理访问。"""

import asyncio
import json
import logging

from starlette.responses import JSONResponse
from starlette.routing import Route

from skill_packages import ZIP_LIMIT, SkillError

LOGGER = logging.getLogger(__name__)


async def read_limited(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise SkillError("上传或请求内容超过容量限制。", 413)
        data.extend(chunk)
    return bytes(data)


async def skill_api(request):
    manager = request.app.state.skills
    try:
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        group_id = request.query_params.get("groupId") or None
        if request.method == "GET":
            if path == "/skills":
                values = await asyncio.to_thread(manager.store.packages, group_id)
                return JSONResponse({"skills": [{k: v for k, v in p.items() if k != "manifest"} for p in values],
                                     "enabled": manager.root is not None})
            if path == "/skills/machines":
                return JSONResponse({"agents": await asyncio.to_thread(manager.machines, group_id),
                                     "groups": await asyncio.to_thread(manager.gateway.registry.groups)})
            if path == "/skills/inventory":
                return JSONResponse({"items": await asyncio.to_thread(manager.store.inventory, group_id)})
            if "package_id" in request.path_params:
                return JSONResponse(await asyncio.to_thread(manager.store.package, request.path_params["package_id"]))
            if "batch_id" in request.path_params:
                return JSONResponse(await asyncio.to_thread(manager.store.batch, request.path_params["batch_id"]))
            return JSONResponse({"deployments": await asyncio.to_thread(manager.store.batches, group_id)})
        if path == "/skills":
            if request.headers.get("content-type", "").split(";")[0] not in {"application/zip", "application/octet-stream"}:
                raise SkillError("请上传 ZIP 文件。", 415)
            return JSONResponse(await asyncio.to_thread(manager.upload, await read_limited(request, ZIP_LIMIT), group_id), status_code=201)
        body = json.loads(await read_limited(request, 32 * 1024))
        if path == "/skills/groups/assign":
            return JSONResponse(await asyncio.to_thread(manager.store.assign, body))
        if "agent_id" in request.path_params:
            if body != {}:
                raise SkillError("请先在机器管理保存目录，再进行检测。")
            return JSONResponse(await manager.check_directory(request.path_params["agent_id"]))
        if path == "/skill-deployments":
            return JSONResponse(await asyncio.to_thread(manager.create, body), status_code=202)
        manager.enabled()
        if not isinstance(body, dict) or set(body) != {"requestId"}:
            raise SkillError("请提供操作请求编号。")
        await asyncio.to_thread(manager.store.action, request.path_params["task_id"], body["requestId"], request.path_params["action"])
        return JSONResponse({"accepted": True}, status_code=202)
    except SkillError as error:
        return JSONResponse({"error": str(error)}, status_code=error.status)
    except (json.JSONDecodeError, UnicodeError):
        return JSONResponse({"error": "请求必须是合法 JSON。"}, status_code=400)
    except Exception as error:
        LOGGER.error("Skill 管理接口失败，类型=%s", type(error).__name__)
        return JSONResponse({"error": "Skill 服务处理失败，请检查中央服务日志。"}, status_code=500)


def skill_routes():
    return [Route("/skills", skill_api, methods=["GET", "POST"]),
            Route("/skills/groups/assign", skill_api, methods=["POST"]),
            Route("/skills/machines", skill_api, methods=["GET"]),
            Route("/skills/inventory", skill_api, methods=["GET"]),
            Route("/skills/machines/{agent_id}/check", skill_api, methods=["POST"]),
            Route("/skills/{package_id}", skill_api, methods=["GET"]),
            Route("/skill-deployments", skill_api, methods=["GET", "POST"]),
            Route("/skill-deployments/{batch_id}", skill_api, methods=["GET"]),
            Route("/skill-deployment-tasks/{task_id}/{action}", skill_action, methods=["POST"])]


async def skill_action(request):
    if request.path_params["action"] not in {"retry", "check"}:
        return JSONResponse({"error": "不支持的安装操作。"}, status_code=404)
    return await skill_api(request)
