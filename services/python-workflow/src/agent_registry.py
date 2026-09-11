"""中央机器登记；只保存配置和凭据引用，不保存令牌内容。"""

import ipaddress
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from codex_orchestrator_mcp import AgentConfig
    from workflow_store import WorkflowStore


class AgentRegistry:
    def __init__(self, store: "WorkflowStore") -> None:
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_groups (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS registered_agents (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES agent_groups(id),
                    ip TEXT NOT NULL, port INTEGER NOT NULL, config TEXT NOT NULL,
                    test_status TEXT NOT NULL DEFAULT 'untested', tested_at TEXT,
                    UNIQUE(ip, port));
                CREATE TABLE IF NOT EXISTS agent_registry_imports (
                    name TEXT PRIMARY KEY);
            """)

    def groups(self) -> list[dict[str, str]]:
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("SELECT id, name FROM agent_groups ORDER BY name")]

    def save_group(self, body: dict[str, Any], group_id: str | None = None) -> dict[str, str]:
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("分组名称必须为 1–100 个字符。")
        with self.store._connect() as db:
            try:
                if group_id:
                    if not db.execute("UPDATE agent_groups SET name=? WHERE id=?", (name.strip(), group_id)).rowcount:
                        raise ValueError("找不到分组。")
                else:
                    group_id = str(uuid.uuid4())
                    db.execute("INSERT INTO agent_groups VALUES (?, ?)", (group_id, name.strip()))
            except sqlite3.IntegrityError as error:
                raise ValueError("分组名称已存在。") from error
        return {"id": group_id, "name": name.strip()}

    def delete_group(self, group_id: str) -> None:
        with self.store._connect() as db:
            if db.execute("SELECT 1 FROM registered_agents WHERE group_id=?", (group_id,)).fetchone():
                raise ValueError("分组内仍有机器，不能删除。")
            if not db.execute("DELETE FROM agent_groups WHERE id=?", (group_id,)).rowcount:
                raise ValueError("找不到分组。")

    def rows(self) -> dict[str, dict[str, Any]]:
        with self.store._connect() as db:
            return {row["id"]: dict(row) for row in db.execute("SELECT * FROM registered_agents ORDER BY id")}

    def configs(self) -> dict[str, "AgentConfig"]:
        from codex_orchestrator_mcp import AgentConfig
        return {key: AgentConfig.from_dict(key, json.loads(row["config"])) for key, row in self.rows().items()}

    def public(self) -> list[dict[str, Any]]:
        return [{"agentId": key, "name": row["ip"], "ip": row["ip"], "port": row["port"],
                 "groupId": row["group_id"], "testStatus": row["test_status"], "testedAt": row["tested_at"],
                 "enabled": json.loads(row["config"]).get("enabled", True),
                 "capabilities": json.loads(row["config"]).get("capabilities", [])}
                for key, row in self.rows().items()]

    def save_agent(self, body: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
        from codex_orchestrator_mcp import AgentConfig
        previous = self.rows().get(agent_id) if agent_id else None
        if not isinstance(body.get("ip"), str) or "%" in body["ip"]:
            raise ValueError("请输入有效的 IP 地址，不支持域名。")
        try:
            ip = str(ipaddress.ip_address(body.get("ip", "")))
        except ValueError as error:
            if previous and not json.loads(previous["config"]).get("registry_managed") and body["ip"] == previous["ip"]:
                ip = body["ip"]
            else:
                raise ValueError("请输入有效的 IP 地址，不支持域名。") from error
        port = body.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("端口必须为 1–65535 的整数。")
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or any(not isinstance(c, str) or c not in ("supervisor", "executor") for c in capabilities):
            raise ValueError("请选择主监督或执行机能力。")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("启用状态必须是布尔值。")
        if not isinstance(body.get("groupId"), str):
            raise ValueError("请选择有效分组。")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM agent_groups WHERE id=?", (body.get("groupId"),)).fetchone():
                raise ValueError("请选择有效分组。")
            old = db.execute("SELECT * FROM registered_agents WHERE id=?", (agent_id,)).fetchone() if agent_id else None
            if agent_id and old is None:
                raise ValueError("找不到机器。")
            agent_id = agent_id or "machine-" + uuid.uuid4().hex
            if db.execute("SELECT 1 FROM registered_agents WHERE ip=? AND port=? AND id<>?", (ip, port, agent_id)).fetchone():
                raise ValueError("该 IP 和端口已登记。")
            config = json.loads(old["config"]) if old else defaults()
            parsed = urlparse(config.get("url", ""))
            protocol = parsed.scheme or os.getenv("CODEX_MACHINE_PROTOCOL", "ws")
            url = config["url"] if old and old["ip"] == ip and old["port"] == port else f"{protocol}://{'[' + ip + ']' if ':' in ip else ip}:{port}{parsed.path}"
            config.update(url=url,
                          capabilities=list(dict.fromkeys(capabilities)), enabled=enabled)
            if "supervisor" in capabilities:
                config["capacity"] = 1
                was_supervisor = old is not None and "supervisor" in json.loads(old["config"]).get("capabilities", [])
                if config.get("registry_managed") or not was_supervisor:
                    config["orchestration_mode"] = "remote_sidecar"
                config.setdefault("orchestration_mode", "local_db" if was_supervisor else "remote_sidecar")
                if config["orchestration_mode"] == "remote_sidecar":
                    endpoint_changed = old is None or old["ip"] != ip or old["port"] != port
                    if config.get("registry_managed") and (endpoint_changed or not was_supervisor):
                        config["sidecar_token_file"] = sidecar_token_path(ip, port)
                    elif not config.get("sidecar_token_env") and not config.get("sidecar_token_file"):
                        config["sidecar_token_file"] = sidecar_token_path(ip, port)
            else:
                for key in ("capacity", "sidecar_token_env", "sidecar_token_file"):
                    config.pop(key, None)
                config["orchestration_mode"] = "local_db"
            AgentConfig.from_dict(agent_id, config)
            if config.get("orchestration_mode") == "remote_sidecar":
                reference = (config.get("sidecar_token_env"), config.get("sidecar_token_file"))
                for row in db.execute("SELECT config FROM registered_agents WHERE id<>?", (agent_id,)):
                    other = json.loads(row["config"])
                    if other.get("orchestration_mode") == "remote_sidecar" and reference == (other.get("sidecar_token_env"), other.get("sidecar_token_file")):
                        raise ValueError("主监督凭据引用重复，请在部署路径模板中使用 IP 和端口区分机器。")
            changed = old is None or (old["ip"], old["port"], old["group_id"], json.loads(old["config"]).get("capabilities")) != (ip, port, body["groupId"], config["capabilities"])
            try:
                db.execute("""INSERT INTO registered_agents VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, ip=excluded.ip,
                    port=excluded.port, config=excluded.config, test_status=excluded.test_status,
                    tested_at=excluded.tested_at""", (agent_id, body["groupId"], ip, port, json.dumps(config),
                    "untested" if changed else old["test_status"], None if changed else old["tested_at"]))
            except sqlite3.IntegrityError as error:
                raise ValueError("该 IP 和端口已登记。") from error
        return next(row for row in self.public() if row["agentId"] == agent_id)

    def record_test(self, agent_id: str, passed: bool) -> None:
        with self.store._connect() as db:
            if not db.execute("UPDATE registered_agents SET test_status=?, tested_at=? WHERE id=?",
                              ("passed" if passed else "failed", datetime.now(timezone.utc).isoformat(), agent_id)).rowcount:
                raise ValueError("找不到机器。")

    def validate(self, supervisor_id: str, executor_ids: list[str], *, require_test: bool = False) -> None:
        from codex_orchestrator_mcp import AgentConfig
        rows = self.rows()
        configs = {key: AgentConfig.from_dict(key, json.loads(row["config"])) for key, row in rows.items()}
        for key, capability in [(supervisor_id, "supervisor")] + [(key, "executor") for key in executor_ids]:
            if key not in rows:
                raise ValueError("请选择已登记的机器。")
            if capability not in configs[key].capabilities:
                raise ValueError("机器能力与所选职责不匹配。")
            if rows[key]["group_id"] != rows[supervisor_id]["group_id"]:
                raise ValueError("主监督只能使用同一分组的执行机。")
            if require_test and (not configs[key].enabled or rows[key]["test_status"] != "passed"):
                raise ValueError("机器必须启用并通过连接检测后才能运行。")

    def import_file(self, path: Path) -> dict[str, int]:
        from codex_orchestrator_mcp import AgentConfig
        raw = json.loads(path.read_text(encoding="utf-8"))
        agents = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(agents, dict) or not agents:
            raise ValueError("旧配置必须包含非空的 agents 对象。")
        values = []
        for key, value in agents.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, dict):
                raise ValueError("旧配置机器格式无效。")
            agent = AgentConfig.from_dict(key, value)
            parsed = urlparse(agent.url)
            try:
                host = str(ipaddress.ip_address(parsed.hostname))
            except ValueError:
                host = parsed.hostname
            # 导入保留旧地址（含历史域名），网页新登记仍只接受 IP。
            config = asdict(agent)
            config.pop("agent_id")
            config["capabilities"] = list(agent.capabilities)
            if "supervisor" not in agent.capabilities:
                config.pop("capacity")
            values.append((key, host, parsed.port or (443 if parsed.scheme == "wss" else 80), json.dumps(config)))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM agent_registry_imports WHERE name='legacy'").fetchone():
                raise ValueError("旧配置已经导入，不能重复导入。")
            existing = db.execute("SELECT id FROM agent_groups WHERE name='默认分组'").fetchone()
            group_id = existing["id"] if existing else "default"
            db.execute("INSERT OR IGNORE INTO agent_groups VALUES (?, '默认分组')", (group_id,))
            try:
                for key, ip, port, config in values:
                    db.execute("INSERT INTO registered_agents(id,group_id,ip,port,config) VALUES (?, ?, ?, ?, ?)", (key, group_id, ip, port, config))
            except sqlite3.IntegrityError as error:
                raise ValueError("旧配置与已登记机器冲突，未导入任何机器。") from error
            db.execute("INSERT INTO agent_registry_imports VALUES ('legacy')")
        return {"imported": len(values)}


def sidecar_token_path(ip: str, port: int) -> str:
    template = os.getenv("CODEX_MACHINE_SIDECAR_TOKEN_TEMPLATE", "")
    if not template:
        raise ValueError("请先配置统一的主监督凭据路径模板。")
    return template.replace("{ip}", ip.replace(":", "_")).replace("{port}", str(port))


def defaults() -> dict[str, Any]:
    cwd = os.getenv("CODEX_MACHINE_CWD", "")
    if not cwd:
        raise ValueError("请先配置统一工作目录 CODEX_MACHINE_CWD。")
    result = {"registry_managed": True, "cwd": cwd, "model": os.getenv("CODEX_MACHINE_MODEL", "gpt-5.6-sol"),
              "allow_write": os.getenv("CODEX_MACHINE_ALLOW_WRITE", "false").lower() == "true",
              "allow_cwd_override": False, "allow_full_access": False}
    for key in ("token_env", "token_file"):
        value = os.getenv("CODEX_MACHINE_" + key.upper())
        if value:
            result[key] = value
    return result
