"""中央机器登记；只保存配置和凭据引用，不保存令牌内容。"""

import ipaddress
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from workflow_service_config import setting
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
                CREATE TABLE IF NOT EXISTS agent_skill_settings (
                    agent_id TEXT PRIMARY KEY REFERENCES registered_agents(id),
                    enabled INTEGER NOT NULL DEFAULT 0, root TEXT NOT NULL DEFAULT '',
                    checked_at TEXT, check_message TEXT);
            """)
            # 旧部署授权仅导入一次；此后网页保存的数据库记录是唯一事实来源。
            legacy = setting("skill_deployment", {}).get("agents", {})
            for row in db.execute("SELECT id FROM registered_agents").fetchall():
                rule = legacy.get(row["id"], {})
                db.execute("INSERT OR IGNORE INTO agent_skill_settings(agent_id,enabled,root) VALUES(?,?,?)",
                           (row["id"], int(rule.get("enabled") is True), rule.get("root", "")))

    def skill_settings(self, agent_id: str) -> dict[str, Any]:
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM agent_skill_settings WHERE agent_id=?", (agent_id,)).fetchone()
        return {"enabled": bool(row["enabled"]), "root": row["root"],
                "checkedAt": row["checked_at"], "checkMessage": row["check_message"]} if row else {
                    "enabled": False, "root": "", "checkedAt": None, "checkMessage": None}

    def record_skill_check(self, agent_id, expected_config, expected_root, message):
        with self.store._connect() as db:
            db.execute("""UPDATE agent_skill_settings SET checked_at=?, check_message=?
                WHERE agent_id=? AND root=? AND EXISTS
                (SELECT 1 FROM registered_agents WHERE id=? AND config=?)""",
                (datetime.now(timezone.utc).isoformat(), message, agent_id, expected_root, agent_id, expected_config))

    def groups(self) -> list[dict[str, str]]:
        with self.store._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT id, name FROM agent_groups ORDER BY name")]
            available = db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_group_packages'").fetchone()
            for row in rows:
                row["skillCount"] = db.execute("SELECT COUNT(*) FROM skill_group_packages WHERE group_id=?", (row["id"],)).fetchone()[0] if available else 0
            return rows

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
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_group_packages'").fetchone():
                for table in ("skill_group_packages", "skill_batches"):
                    if db.execute(f"SELECT 1 FROM {table} WHERE group_id=? LIMIT 1", (group_id,)).fetchone():
                        raise ValueError("分组仍有 Skill 或下发历史引用，不能删除。")
            if db.execute("SELECT 1 FROM registered_agents WHERE group_id=?", (group_id,)).fetchone():
                raise ValueError("分组内仍有机器，不能删除。")
            if not db.execute("DELETE FROM agent_groups WHERE id=?", (group_id,)).rowcount:
                raise ValueError("找不到分组。")

    def rows(self) -> dict[str, dict[str, Any]]:
        with self.store._connect() as db:
            return {row["id"]: dict(row) for row in db.execute("SELECT * FROM registered_agents ORDER BY id")}

    def configs(self) -> dict[str, "AgentConfig"]:
        return {key: self.config_from_row(key, row) for key, row in self.rows().items()}

    @staticmethod
    def config_from_row(agent_id: str, row: dict[str, Any]) -> "AgentConfig":
        from codex_orchestrator_mcp import AgentConfig
        # 登记目录是默认值，节点可以指定目录；同样适用于此前登记的机器。
        config = json.loads(row["config"])
        config["allow_cwd_override"] = True
        return AgentConfig.from_dict(agent_id, config)

    def public(self) -> list[dict[str, Any]]:
        return [{"agentId": key, "name": row["ip"], "ip": row["ip"], "port": row["port"],
                 "groupId": row["group_id"], "testStatus": row["test_status"], "testedAt": row["tested_at"],
                 "enabled": json.loads(row["config"]).get("enabled", True),
                 "skillInstallation": self.skill_settings(key),
                 "capabilities": json.loads(row["config"]).get("capabilities", [])}
                for key, row in self.rows().items()]

    def save_agent(self, body: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
        from codex_orchestrator_mcp import AgentConfig
        if not isinstance(body.get("ip"), str) or "%" in body["ip"]:
            raise ValueError("请输入有效的 IP 地址，不支持域名。")
        try:
            ip = str(ipaddress.ip_address(body.get("ip", "")))
        except ValueError as error:
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
            if old is not None and old["group_id"] != body["groupId"]:
                active = db.execute("""SELECT 1 FROM workflows w
                    WHERE w.status IN ('queued','running','cancelling') AND
                    (w.supervisor_agent_id=? OR EXISTS
                     (SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.agent_id=?))
                    LIMIT 1""", (agent_id, agent_id)).fetchone()
                if active:
                    raise ValueError("机器仍有关联任务运行，请结束运行后再改组。")
            agent_id = agent_id or "machine-" + uuid.uuid4().hex
            if db.execute("SELECT 1 FROM registered_agents WHERE ip=? AND port=? AND id<>?", (ip, port, agent_id)).fetchone():
                raise ValueError("该 IP 和端口已登记。")
            config = json.loads(old["config"]) if old else defaults()
            previous = db.execute("SELECT * FROM agent_skill_settings WHERE agent_id=?", (agent_id,)).fetchone()
            skill_enabled = bool(previous["enabled"]) if previous else False
            skill_root = previous["root"] if previous else ""
            if "skillInstallation" in body:
                from skill_packages import SkillError, remote_root
                rule = body["skillInstallation"]
                if (not isinstance(rule, dict) or not {"enabled", "root"} <= set(rule)
                        or set(rule) - {"enabled", "root", "checkedAt", "checkMessage"}
                        or not isinstance(rule["enabled"], bool) or not isinstance(rule["root"], str)):
                    raise ValueError("Skill 安装设置必须包含启用开关和目录。")
                skill_enabled, skill_root = rule["enabled"], rule["root"].strip()
                try:
                    if skill_root or skill_enabled:
                        skill_root = str(remote_root(skill_root))
                except SkillError as error:
                    raise ValueError(str(error)) from error
                if skill_enabled and "executor" not in capabilities:
                    raise ValueError("只有执行机可以授权 Skill 安装。")
            if "executor" not in capabilities:
                skill_enabled = False
            settings_changed = previous is None or (skill_enabled, skill_root) != (bool(previous["enabled"]), previous["root"])
            if old and (settings_changed or old["ip"] != ip or old["port"] != port or old["group_id"] != body["groupId"]):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_tasks'").fetchone():
                    if db.execute("SELECT 1 FROM skill_tasks WHERE agent_id=? AND state IN ('queued','running') LIMIT 1", (agent_id,)).fetchone():
                        raise ValueError("这台机器仍有待处理的 Skill 下发，请结束后再修改分组、安装设置或连接地址。")
            previous_credentials = (config.get("token_env"), config.get("token_file"))
            # 保存时同步部署凭据引用；切换来源时移除旧引用，不能同时保留两种来源。
            for credential_key in ("token_env", "token_file"):
                config.pop(credential_key, None)
                value = setting("machine_defaults." + credential_key)
                if value:
                    config[credential_key] = value
            credentials_changed = previous_credentials != (config.get("token_env"), config.get("token_file"))
            # 未提供目录默认值时保留历史登记。
            artifact_root = setting("machine_defaults.artifact_root")
            if artifact_root is not None:
                config["artifact_root"] = artifact_root
            parsed = urlparse(config.get("url", ""))
            protocol = parsed.scheme or setting("machine_defaults.protocol", "ws")
            url = config["url"] if old and old["ip"] == ip and old["port"] == port else f"{protocol}://{'[' + ip + ']' if ':' in ip else ip}:{port}{parsed.path}"
            config.update(url=url,
                          capabilities=list(dict.fromkeys(capabilities)), enabled=enabled,
                          allow_cwd_override=True,
                          allow_write=setting("machine_defaults.allow_write", False),
                          allow_full_access=setting("machine_defaults.allow_full_access", False))
            if "supervisor" in capabilities:
                config["capacity"] = 1
                was_supervisor = old is not None and "supervisor" in json.loads(old["config"]).get("capabilities", [])
                if not was_supervisor:
                    config["orchestration_mode"] = setting("machine_defaults.orchestration_mode", "remote_sidecar")
                if config["orchestration_mode"] == "remote_sidecar":
                    endpoint_changed = old is None or old["ip"] != ip or old["port"] != port
                    if endpoint_changed or not was_supervisor:
                        config["sidecar_token_file"] = sidecar_token_path(ip, port)
            else:
                for key in ("capacity", "sidecar_token_env", "sidecar_token_file"):
                    config.pop(key, None)
                config.pop("orchestration_mode", None)
            AgentConfig.from_dict(agent_id, config)
            if config.get("orchestration_mode") == "remote_sidecar":
                reference = (config.get("sidecar_token_env"), config.get("sidecar_token_file"))
                for row in db.execute("SELECT config FROM registered_agents WHERE id<>?", (agent_id,)):
                    other = json.loads(row["config"])
                    if other.get("orchestration_mode") == "remote_sidecar" and reference == (other.get("sidecar_token_env"), other.get("sidecar_token_file")):
                        raise ValueError("主监督凭据引用重复，请在部署路径模板中使用 IP 和端口区分机器。")
            changed = old is None or credentials_changed or (old["ip"], old["port"], old["group_id"], json.loads(old["config"]).get("capabilities")) != (ip, port, body["groupId"], config["capabilities"])
            try:
                db.execute("""INSERT INTO registered_agents VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, ip=excluded.ip,
                    port=excluded.port, config=excluded.config, test_status=excluded.test_status,
                    tested_at=excluded.tested_at""", (agent_id, body["groupId"], ip, port, json.dumps(config),
                    "untested" if changed else old["test_status"], None if changed else old["tested_at"]))
            except sqlite3.IntegrityError as error:
                raise ValueError("该 IP 和端口已登记。") from error
            db.execute("""INSERT INTO agent_skill_settings(agent_id,enabled,root) VALUES(?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET enabled=excluded.enabled,root=excluded.root""",
                (agent_id, int(skill_enabled), skill_root))
            if settings_changed or changed:
                db.execute("UPDATE agent_skill_settings SET checked_at=NULL,check_message=NULL WHERE agent_id=?", (agent_id,))
        return next(row for row in self.public() if row["agentId"] == agent_id)

    def record_test(self, agent_id: str, passed: bool) -> None:
        with self.store._connect() as db:
            if not db.execute("UPDATE registered_agents SET test_status=?, tested_at=? WHERE id=?",
                              ("passed" if passed else "failed", datetime.now(timezone.utc).isoformat(), agent_id)).rowcount:
                raise ValueError("找不到机器。")

    def validate(self, supervisor_id: str, executor_ids: list[str], *, require_test: bool = False, group_id: str | None = None) -> None:
        if group_id is not None:
            try:
                uuid.UUID(group_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("请选择有效分组。") from None
        rows = self.rows()
        configs = {key: self.config_from_row(key, row) for key, row in rows.items()}
        for key, capability in [(supervisor_id, "supervisor")] + [(key, "executor") for key in executor_ids]:
            if key not in rows:
                raise ValueError("请选择已登记的机器。")
            if group_id is not None and rows[key]["group_id"] != group_id:
                raise ValueError("所选机器必须属于工作流的分组。")
            if capability not in configs[key].capabilities:
                raise ValueError("机器能力与所选职责不匹配。")
            if rows[key]["group_id"] != rows[supervisor_id]["group_id"]:
                raise ValueError("主监督只能使用同一分组的执行机。")
            if require_test and (not configs[key].enabled or rows[key]["test_status"] != "passed"):
                raise ValueError("机器必须启用并通过连接检测后才能运行。")


def sidecar_token_path(ip: str, port: int) -> str:
    template = setting("machine_defaults.sidecar_token_template", "")
    if not template:
        raise ValueError("请先配置统一的主监督凭据路径模板。")
    return template.replace("{ip}", ip.replace(":", "_")).replace("{port}", str(port))


def defaults() -> dict[str, Any]:
    cwd = setting("machine_defaults.cwd", "")
    if not cwd:
        raise ValueError("请先在服务配置中设置 machine_defaults.cwd。")
    result = {"cwd": cwd, "model": setting("machine_defaults.model", "gpt-5.6-sol"),
              "allow_write": setting("machine_defaults.allow_write", False),
              "allow_cwd_override": True,
              "allow_full_access": setting("machine_defaults.allow_full_access", False)}
    for key in ("token_env", "token_file", "artifact_root"):
        value = setting("machine_defaults." + key)
        if value:
            result[key] = value
    return result
