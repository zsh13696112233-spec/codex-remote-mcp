"""Jira 领取台账。凭据仅经过运行时，不进入中央运行库。"""

import hashlib
import json
import re
from pathlib import PureWindowsPath
from urllib.parse import urlsplit, urlunsplit


ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,110}-[1-9][0-9]{0,15}")


def compatible_agent(agent):
    return agent.allow_full_access and bool(agent.cwd) and PureWindowsPath(agent.cwd).is_absolute()


def normalize_config(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"jql", "account"}:
        raise ValueError("Jira 配置必须包含查询条件和账号别名。")
    for key, limit in (("jql", 4000), ("account", 128)):
        if not isinstance(value[key], str) or len(value[key]) > limit or any(ord(c) < 32 for c in value[key]):
            raise ValueError("Jira 查询条件或账号别名无效。")
    if not value["jql"].strip():
        raise ValueError("请填写 Jira 查询条件。")
    return {key: text.strip() for key, text in value.items()}


def server_key(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("Jira 地址无效。")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ValueError("Jira 地址必须是没有凭据或查询参数的 HTTP 地址。")
    port = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and port != {"http": 80, "https": 443}[parsed.scheme]:
        host += ":" + str(port)
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def run_directory(workflow_id):
    return hashlib.sha256(workflow_id.encode()).hexdigest()


def pending_credentials(db, agent_id):
    return db.execute("""SELECT 1 FROM workflow_jira_runs j JOIN workflow_allocations a USING(workflow_id)
        WHERE a.executor_id=? AND j.issue_key IS NOT NULL AND j.credentials_cleared=0 LIMIT 1""", (agent_id,)).fetchone() is not None


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_jira_runs (
            workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
            state TEXT NOT NULL DEFAULT 'waiting_claim', server_key TEXT, account_alias TEXT,
            issue_key TEXT, credential_fingerprint TEXT, launcher_path TEXT, claimed_at TEXT,
            credentials_cleared INTEGER NOT NULL DEFAULT 0, cleanup_attempted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS jira_issue_claims (
            server_key TEXT NOT NULL, issue_key TEXT NOT NULL,
            workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(workflow_id),
            PRIMARY KEY(server_key, issue_key)
        );
        CREATE INDEX IF NOT EXISTS idx_jira_history ON workflow_jira_runs(server_key,issue_key);
        CREATE TABLE IF NOT EXISTS workflow_jira_deliveries (
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id), node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL, status TEXT NOT NULL, reference TEXT NOT NULL,
            summary TEXT NOT NULL, recorded_at TEXT NOT NULL,
            PRIMARY KEY(workflow_id,node_id,attempt)
        );
    """)
    # 已存在的 Jira 表不会因 CREATE TABLE IF NOT EXISTS 自动增加新列。
    # 网关与本机 MCP 可能同时初始化，在写事务内检查并升级。
    db.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in db.execute("PRAGMA table_info(workflow_jira_runs)")}
    if "cleanup_attempted_at" not in columns:
        db.execute("ALTER TABLE workflow_jira_runs ADD COLUMN cleanup_attempted_at TEXT")


def node_instruction(db, workflow_id):
    row = db.execute("SELECT * FROM workflow_jira_runs WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        return ""
    if row["state"] != "ready":
        raise RuntimeError("监督机尚未领取工单并完成账号下发，不能启动步骤。")
    return ("\n【本轮 Jira 任务，优先于角色中自行领取任务的要求】\n"
            f"本轮唯一工单：{row['issue_key']}。所有步骤只处理此工单，不重新搜索或领取其他工单。\n"
            f"通过 PowerShell 调用本轮专属入口 {json.dumps(row['launcher_path'], ensure_ascii=False)}，"
            f"参数与 jira_cli 相同，例如 issue get {row['issue_key']}。"
            "入口负责传入本轮固定账号及认证参数，读取、评论和修改都直接调用此入口；"
            "不要自行添加 --user、用户名、密码或令牌，不绕过入口直接调用 jira_cli。"
            "不得读取、打印或更改账号配置，不使用其他已保存账号。"
            "若入口仍要求选择账号，报告平台入口异常，不自行换账号。"
            "先实际读取工单，再按职责进行具体修改；领取记录不代表 Jira 已被修改。"
            "历史完成的工单只要仍符合查询条件就可能再次领取。按职责完成负责人转交后，必须重新读取工单，"
            "确认实际负责人等于指定接收人且不再是本轮领取账号，才能报告交接成功；"
            "接收人不明确、转交失败或回查无法确认时，按既有结果协议报告遇阻和待人工动作，不得报告交接成功或擅自选择接收人。"
            "不得把工作流完成当成代码提交成功。最终 jiraDelivery.status 使用 submitted（已有提交凭证）、"
            "completed（本步完成但未提交）、blocked（受阻）、unknown（提交结果不明）或 not_applicable。"
            "reference 填实际确认的提交号或交付链接；submitted 必须实际回查并有凭证，不能只凭已发出提交命令。summary 简述交付结果。\n")


def validate_delivery(value):
    if not isinstance(value, dict) or set(value) != {"status", "reference", "summary"}:
        raise ValueError("Jira 交付结果格式无效。")
    if not isinstance(value["status"], str) or value["status"] not in {"submitted", "completed", "blocked", "unknown", "not_applicable"}:
        raise ValueError("Jira 交付状态无效。")
    for key, limit in (("reference", 1000), ("summary", 2000)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError("Jira 交付结果文本无效。")
    if value["status"] == "submitted" and not value["reference"].strip():
        raise ValueError("已提交必须提供实际提交凭证。")
    return value


class JiraStore:
    def jira_cleanup_attempted(self, workflow_id):
        from workflow_store import utc_now
        with self._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET cleanup_attempted_at=? WHERE workflow_id=?", (utc_now(), workflow_id))

    def jira_cleanup_candidates(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("""SELECT j.workflow_id,a.executor_id,j.launcher_path FROM workflow_jira_runs j
                JOIN workflows w USING(workflow_id) JOIN workflow_allocations a USING(workflow_id)
                WHERE j.credentials_cleared=0 AND j.issue_key IS NOT NULL AND w.status IN ('completed','failed','cancelled')
                AND NOT EXISTS (SELECT 1 FROM workflow_machine_claims c WHERE c.workflow_id=j.workflow_id)
                ORDER BY COALESCE(j.cleanup_attempted_at,''),w.finished_at,j.workflow_id LIMIT 20""")]

    def jira_credentials_cleared(self, workflow_id):
        with self._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET credentials_cleared=1 WHERE workflow_id=?", (workflow_id,))

    def jira_context(self, workflow_id):
        spec = self.get_spec(workflow_id)
        if not spec.get("jira"):
            raise ValueError("本运行未启用监督机统一领取 Jira。")
        with self._connect() as db:
            row = db.execute("SELECT * FROM workflow_jira_runs WHERE workflow_id=?", (workflow_id,)).fetchone()
            allocation = db.execute("SELECT * FROM workflow_allocations WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not allocation or not allocation["executor_id"]:
                raise RuntimeError("尚未分配监督机和执行机。")
            return {"config": spec["jira"], "supervisorId": allocation["supervisor_id"],
                    "executorId": allocation["executor_id"], "cwd": allocation["cwd"],
                    "state": row["state"], "issueKey": row["issue_key"],
                    "server": row["server_key"], "account": row["account_alias"],
                    "fingerprint": row["credential_fingerprint"], "launcherPath": row["launcher_path"]}

    def jira_update(self, workflow_id, operation, payload, *, sidecar_supervisor_id=None, lease_token=None):
        from workflow_store import utc_now
        if not isinstance(payload, dict) or not isinstance(operation, str):
            raise ValueError("Jira 请求必须是对象。")
        spec = self.get_spec(workflow_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(db, sidecar_supervisor_id, workflow_id,
                    lease_token=lease_token, require_lease=True)
            row = db.execute("SELECT j.*,w.status FROM workflow_jira_runs j JOIN workflows w USING(workflow_id) WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                raise ValueError("本运行未启用 Jira 领取。")
            if row["status"] not in {"queued", "running"}:
                raise RuntimeError("运行已停止，不能继续领取或下发 Jira。")
            if not db.execute("SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?", (workflow_id,)).fetchone():
                raise RuntimeError("运行尚未取得机器，不能领取 Jira。")
            now = utc_now()
            if operation == "claim":
                server = server_key(payload.get("server"))
                account, fingerprint = payload.get("account"), payload.get("fingerprint")
                if (not isinstance(account, str) or not 1 <= len(account) <= 128
                        or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                    raise ValueError("Jira 账号标识无效。")
                candidates = payload.get("candidates")
                if not isinstance(candidates, list) or len(candidates) > 1000 or any(not isinstance(k, str) or not ISSUE_KEY.fullmatch(k) for k in candidates):
                    raise ValueError("Jira 候选编号格式无效或超过 1000 项。")
                if spec.get("jiraRetryOf"):
                    previous = db.execute("SELECT j.* FROM workflow_jira_runs j JOIN workflow_task_bindings b USING(workflow_id) WHERE j.workflow_id=? AND b.task_definition_id=?",
                        (spec["jiraRetryOf"], spec.get("taskDefinitionId"))).fetchone()
                    if not previous or not previous["issue_key"]:
                        raise ValueError("来源运行没有已领取工单，不能按工单重试；请使用最新配置新建运行。")
                    if previous["server_key"] != server:
                        raise ValueError("监督机 Jira 服务地址与来源工单不一致。")
                    candidates = [key for key in candidates if key == previous["issue_key"]]
                if row["issue_key"]:
                    if (row["server_key"], row["account_alias"], row["credential_fingerprint"]) != (server, account, fingerprint):
                        raise RuntimeError("本轮 Jira 账号已变化，请结束本轮后重新运行。")
                    return {"issueKey": row["issue_key"], "state": row["state"]}
                if row["state"] == "no_task":
                    return {"issueKey": None, "state": "no_task"}
                selected = None
                for key in dict.fromkeys(candidates):
                    previous = db.execute("SELECT w.status,c.workflow_id FROM jira_issue_claims c JOIN workflows w USING(workflow_id) WHERE c.server_key=? AND c.issue_key=?", (server, key)).fetchone()
                    if previous:
                        if previous["status"] not in {"completed", "failed", "cancelled"} or db.execute("SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?", (previous["workflow_id"],)).fetchone():
                            continue
                        db.execute("DELETE FROM jira_issue_claims WHERE server_key=? AND issue_key=?", (server, key))
                    db.execute("INSERT INTO jira_issue_claims VALUES(?,?,?)", (server, key, workflow_id))
                    selected = key
                    break
                if selected is None and payload["candidates"]:
                    # 当前查询页全部已领取；监督机应排除该页继续查询，而不是提前报告无任务。
                    return {"issueKey": None, "state": "contended"}
                if selected:
                    db.execute("UPDATE workflow_jira_runs SET state='claimed',server_key=?,account_alias=?,credential_fingerprint=?,issue_key=?,claimed_at=? WHERE workflow_id=?", (server, account, fingerprint, selected, now, workflow_id))
                    db.execute("UPDATE workflows SET jira_context_json=? WHERE workflow_id=?", (json.dumps({"issueKey": selected, "developerStatus": "not_applicable", "developerName": ""}), workflow_id))
                else:
                    db.execute("UPDATE workflow_jira_runs SET state='no_task',server_key=?,account_alias=? WHERE workflow_id=?", (server, account, workflow_id))
                    termination = {"outcome": "no_task", "reason": "本次查询没有可领取的 Jira 工单。", "nodeId": None,
                        "jiraComment": {"status": "not_applicable", "issueKey": "", "reference": "", "detail": "未领取工单。"}}
                    db.execute("UPDATE workflows SET termination_json=? WHERE workflow_id=?", (json.dumps(termination, ensure_ascii=False), workflow_id))
                    db.execute("UPDATE workflow_nodes SET status='skipped',finished_at=?,error='无可领取工单，本步骤未执行。' WHERE workflow_id=? AND status='pending'", (now, workflow_id))
                result = {"issueKey": selected, "state": "claimed" if selected else "no_task"}
            elif operation in {"stage", "ready"}:
                path = payload.get("launcherPath")
                if (not isinstance(path, str) or not PureWindowsPath(path).is_absolute()
                        or PureWindowsPath(path).name != "jira.ps1"
                        or PureWindowsPath(path).parent.name != run_directory(workflow_id)
                        or PureWindowsPath(path).parent.parent.name != "CodexFlowJira"
                        or ".." in PureWindowsPath(path).parts
                        or any(ord(c) < 32 for c in path) or len(path) > 1000):
                    raise ValueError("Jira 运行入口路径无效。")
                if row["state"] not in {"claimed", "ready"}:
                    raise RuntimeError("请先领取工单。")
                if row["launcher_path"] and row["launcher_path"] != path:
                    raise RuntimeError("本轮 Jira 入口已固定，不能修改。")
                if operation == "stage":
                    db.execute("UPDATE workflow_jira_runs SET launcher_path=? WHERE workflow_id=?", (path, workflow_id))
                    return {"issueKey": row["issue_key"], "state": row["state"]}
                db.execute("UPDATE workflow_jira_runs SET state='ready',launcher_path=? WHERE workflow_id=?", (path, workflow_id))
                result = {"issueKey": row["issue_key"], "state": "ready"}
            else:
                raise ValueError("不支持的 Jira 操作。")
            db.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
            self._add_event_with_connection(db, workflow_id, None, "supervisor", "jira." + result["state"], result, now)
            return result

    def jira_runs(self, group_id, *, before=None, limit=50):
        group_id = group_id or None
        if group_id is not None and (not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", group_id)):
            raise ValueError("请选择有效分组。")
        if type(limit) is not int or not 1 <= limit <= 100 or (before is not None and (type(before) is not int or before < 1)):
            raise ValueError("分页参数无效。")
        with self._connect() as db:
            rows = db.execute("""SELECT j.*,j.rowid AS cursor,w.status,w.name,b.task_definition_id,w.created_at,w.finished_at,w.termination_json,
                a.supervisor_id,a.executor_id,a.cwd,a.group_id,
                supervisor.remark AS supervisor_remark,executor.remark AS executor_remark FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
                JOIN workflow_allocations a USING(workflow_id) LEFT JOIN workflow_task_bindings b USING(workflow_id)
                LEFT JOIN registered_agents supervisor ON supervisor.id=a.supervisor_id
                LEFT JOIN registered_agents executor ON executor.id=a.executor_id WHERE (? IS NULL OR a.group_id=?) AND (? IS NULL OR j.rowid<?)
                ORDER BY j.rowid DESC LIMIT ?""", (group_id, group_id, before, before, limit + 1)).fetchall()
            result = []
            for row in rows[:limit]:
                deliveries = [dict(r) for r in db.execute("SELECT node_id AS nodeId,attempt,status,reference,summary,recorded_at AS recordedAt FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY recorded_at,node_id,attempt", (row["workflow_id"],))]
                waiting = db.execute("SELECT kind FROM workflow_advance_gates WHERE workflow_id=? AND status IN ('pending','held') ORDER BY created_at DESC LIMIT 1", (row["workflow_id"],)).fetchone()
                termination = json.loads(row["termination_json"]) if row["termination_json"] else None
                result.append({"workflowId": row["workflow_id"], "taskDefinitionId": row["task_definition_id"],
                    "name": row["name"], "groupId": row["group_id"], "issueKey": row["issue_key"], "claimState": row["state"],
                    "status": row["status"], "outcome": termination["outcome"] if termination else None,
                    "waitingFor": waiting["kind"] if waiting else None,
                    "reason": termination["reason"] if termination else None,
                    "supervisorAgentId": row["supervisor_id"], "executorAgentId": row["executor_id"],
                    "supervisorRemark": row["supervisor_remark"] or "", "executorRemark": row["executor_remark"] or "",
                    "workingDirectory": row["cwd"], "createdAt": row["created_at"],
                    "claimedAt": row["claimed_at"], "finishedAt": row["finished_at"], "deliveries": deliveries,
                    "resourceState": "occupied" if db.execute("SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?", (row["workflow_id"],)).fetchone() else "released"})
            return {"items": result, "nextCursor": rows[limit - 1]["cursor"] if len(rows) > limit else None}
