"""为运行时测试构造已登记机器；不调用业务导入接口，也不读取部署文件。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from agent_registry import AgentRegistry
from codex_orchestrator_mcp import AgentConfig, Orchestrator
from workflow_store import WorkflowStore
from workflow_gateway import WorkflowGateway, create_app


def seed_agents(store, agents):
    registry = AgentRegistry(store)
    with store._connect() as db:
        db.execute("INSERT OR IGNORE INTO agent_groups VALUES ('fixture', '测试分组')")
        for index, (key, value) in enumerate(agents.items()):
            value = dict(value)
            if value.get('capacity') == 0:
                value.pop('capacity')
            if 'capabilities' in value:
                value['capabilities'] = list(value['capabilities'])
            if value.get('orchestration_mode') == 'remote_sidecar' and not (value.get('sidecar_token_file') or value.get('sidecar_token_env')):
                value['sidecar_token_env'] = 'FIXTURE_SIDECAR_TOKEN'
            value.setdefault('cwd', '/work')
            value.setdefault('url', f'ws://127.0.0.1:{4500 + index}')
            config = AgentConfig.from_dict(key, value)
            value['capabilities'] = list(config.capabilities)
            parsed = urlparse(config.url)
            # 测试替身可能共用一个监听端口；登记行使用独立测试地址。
            db.execute("""INSERT INTO registered_agents VALUES (?, 'fixture', ?, ?, ?, 'passed', NULL)
                ON CONFLICT(id) DO UPDATE SET config=excluded.config""",
                (key, key, parsed.port or 4500, json.dumps(value)))
    return registry


class FixtureWorkflowStore(WorkflowStore):
    def __init__(self, path, *args, **kwargs):
        super().__init__(path, *args, **kwargs)
        seed_agents(self, {key: {'allow_write': True, 'allow_full_access': True, 'capabilities': ['supervisor', 'executor']} for key in ('local', 'supervisor-a')})


def fixture_orchestrator(config_path=None, **kwargs):
    instance = Orchestrator(**kwargs)
    if config_path is not None:
        # Only generated test payloads are read here; production accepts no list file.
        agents = json.loads(Path(config_path).read_text(encoding='utf-8'))['agents']
        instance.agent_provider = lambda: {key: AgentConfig.from_dict(key, value) for key, value in agents.items()}
    return instance


def fixture_gateway(store, orchestrator, assistant_orchestrator=None):
    if type(orchestrator) is object:
        orchestrator = SimpleNamespace()
    if not isinstance(store, WorkflowStore):
        with patch('agent_registry.AgentRegistry') as registry:
            registry.return_value.public.return_value = []
            return WorkflowGateway(store, orchestrator, assistant_orchestrator)
    if type(orchestrator) is object:
        orchestrator = SimpleNamespace()
    if isinstance(orchestrator, Orchestrator):
        from dataclasses import asdict
        seed_agents(store, {key: asdict(value) for key, value in orchestrator.load_agents().items()})
    elif hasattr(orchestrator, 'list_agents'):
        agents = {}
        for item in orchestrator.list_agents():
            if hasattr(orchestrator, 'get_agent'):
                from dataclasses import asdict
                agents[item['agent_id']] = asdict(orchestrator.get_agent(item['agent_id']))
                continue
            agents[item['agent_id']] = {
                'cwd': item.get('cwd', '/work'), 'enabled': item.get('enabled', True),
                'capabilities': item.get('capabilities', ['supervisor', 'executor']),
                'allow_write': item.get('allow_write', False),
                'orchestration_mode': item.get('orchestration_mode', 'local_db'),
            }
        seed_agents(store, agents)
    return WorkflowGateway(store, orchestrator, assistant_orchestrator)


def fixture_app(*, config_path, db_path, **kwargs):
    agents = json.loads(Path(config_path).read_text(encoding='utf-8'))['agents']
    seed_agents(WorkflowStore(Path(db_path)), agents)
    return create_app(db_path=db_path, **kwargs)
