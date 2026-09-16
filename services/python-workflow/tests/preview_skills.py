"""仅用于本地 UI 验收：临时中央库与内存执行机，不访问真实部署配置。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from agent_registry import AgentRegistry
from workflow_store import WorkflowStore
from skill_deployment import SkillDeployment
from skill_routes import skill_routes
from tests.test_skills import MemoryRemote, bundle, SKILL


def main():
    with tempfile.TemporaryDirectory(prefix="skills-preview-") as directory:
        root = Path(directory)
        store = WorkflowStore(root / "state.db")
        with patch("workflow_service_config._load", return_value={"machine_defaults":{"cwd":"/work","allow_write":True}}):
            registry = AgentRegistry(store)
            group = registry.save_group({"name":"验收测试组"})["id"]
            registry.save_group({"name":"共享资料组"})
            machine = registry.save_agent({"ip":"192.0.2.10","port":4500,"capabilities":["executor"],"groupId":group,
                "skillInstallation":{"enabled":True,"root":"/work/skills"}})["agentId"]
            registry.record_test(machine, True)
        manager = SkillDeployment(SimpleNamespace(store=store, registry=registry),
            {"package_root":str(root/"packages"),"agents":{machine:{"enabled":True,"root":"/work/skills"}}}, MemoryRemote())
        manager.upload(bundle({"SKILL.md":SKILL,"scripts/main.py":b'print("hello")'}), group)
        legacy = manager.upload(bundle({"SKILL.md": SKILL.replace(b"demo", b"legacy-demo")}), group)
        with store._connect() as db:
            db.execute("DELETE FROM skill_group_packages WHERE package_id=?", (legacy["id"],))
        api = Starlette(routes=skill_routes()+[Route("/groups", lambda request: JSONResponse({"groups":registry.groups()})),
            Route("/gateway/ready", lambda request: JSONResponse({"ready":True}))])
        api.state.skills = manager
        # 被挂载的 Starlette 保留完整 URL，路由处理器根据 root_path 获取业务路径。
        @asynccontextmanager
        async def lifespan(app):
            await manager.start()
            try: yield
            finally: await manager.stop()
        static = Path(__file__).resolve().parents[2]/"role-task-config-center"/"src"/"main"/"resources"/"static"
        app = Starlette(routes=[Mount("/api", app=api), Mount("/", app=StaticFiles(directory=static, html=True))], lifespan=lifespan)
        uvicorn.run(app, host="127.0.0.1", port=18191, log_level="warning")


if __name__ == "__main__": main()
