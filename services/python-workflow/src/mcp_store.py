"""MCP 包和安装任务的中央持久状态。"""

import json
import uuid

from skill_packages import SkillError
from skill_store import identifier, now

ACTIVE = ("queued", "transferring", "installing", "registering", "verifying", "review")


def ensure_available(db, agents):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_tasks'").fetchone():
        return
    for agent in set(agents):
        if db.execute("SELECT 1 FROM mcp_tasks WHERE agent_id=? AND occupied=1 LIMIT 1", (agent,)).fetchone():
            raise ValueError("执行机正在安装 MCP 或等待安装核对，请稍后重试。")


def workflow_agents(db, workflow_id):
    return [row[0] for row in db.execute("""SELECT supervisor_agent_id FROM workflows WHERE workflow_id=?
        UNION SELECT agent_id FROM workflow_nodes WHERE workflow_id=?""", (workflow_id, workflow_id))]


class McpStore:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mcp_packages(
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_package_groups(
                    package_id TEXT NOT NULL REFERENCES mcp_packages(id),
                    group_id TEXT NOT NULL REFERENCES agent_groups(id), PRIMARY KEY(package_id,group_id));
                CREATE TABLE IF NOT EXISTS mcp_batches(
                    id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES mcp_packages(id),
                    group_id TEXT NOT NULL, group_name TEXT NOT NULL, request TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_tasks(
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES mcp_batches(id),
                    agent_id TEXT NOT NULL, state TEXT NOT NULL, occupied INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'install', snapshot TEXT NOT NULL,
                    thread_id TEXT, turn_id TEXT, result TEXT, registration TEXT,
                    message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS mcp_machine_occupied ON mcp_tasks(agent_id) WHERE occupied=1;
                CREATE TABLE IF NOT EXISTS mcp_actions(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES mcp_tasks(id), action TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(mcp_tasks)")}
            if "diagnostics" not in columns:
                db.execute("ALTER TABLE mcp_tasks ADD COLUMN diagnostics TEXT")
            if "execution_started" not in columns:
                # 旧记录无法证明没有启动过，按不确定执行处理。
                db.execute("ALTER TABLE mcp_tasks ADD COLUMN execution_started INTEGER NOT NULL DEFAULT 1")
            for field in ("program_verified", "skill_verified", "execution_stopped"):
                if field not in columns:
                    db.execute(f"ALTER TABLE mcp_tasks ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")

    def group(self, db, group_id):
        key = identifier(group_id)
        row = db.execute("SELECT name FROM agent_groups WHERE id=?", (key,)).fetchone()
        if not row:
            raise SkillError("请选择有效分组。", 409)
        return row[0]

    def add(self, metadata, group):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.group(db, group)
            db.execute("INSERT OR IGNORE INTO mcp_packages VALUES(?,?,?)", (metadata["id"], json.dumps(metadata), now()))
            db.execute("INSERT OR IGNORE INTO mcp_package_groups VALUES(?,?)", (metadata["id"], group))
        return self.package(metadata["id"])

    def package(self, key):
        with self.store._connect() as db:
            row = db.execute("SELECT metadata FROM mcp_packages WHERE id=?", (key,)).fetchone()
            if not row:
                raise SkillError("找不到 MCP 包。", 404)
            return json.loads(row[0])

    def packages(self, group=None):
        with self.store._connect() as db:
            if group:
                self.group(db, group)
            rows = db.execute("""SELECT p.* FROM mcp_packages p WHERE ? IS NULL OR EXISTS
                (SELECT 1 FROM mcp_package_groups g WHERE g.package_id=p.id AND g.group_id=?)
                ORDER BY created_at DESC LIMIT 200""", (group, group)).fetchall()
            return [{**{k: v for k, v in json.loads(row["metadata"]).items() if k != "manifest"},
                     "groups": [r[0] for r in db.execute("SELECT group_id FROM mcp_package_groups WHERE package_id=?", (row["id"],))]}
                    for row in rows]

    def assign(self, body):
        if not isinstance(body, dict) or set(body) != {"groupId", "packageIds"}:
            raise SkillError("请选择分组和 MCP 包。")
        keys = body["packageIds"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 200 or any(not isinstance(k, str) for k in keys):
            raise SkillError("请选择 1–200 个 MCP 包。")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.group(db, body["groupId"])
            for key in keys:
                if not db.execute("SELECT 1 FROM mcp_packages WHERE id=?", (key,)).fetchone():
                    raise SkillError("找不到 MCP 包。", 404)
                db.execute("INSERT OR IGNORE INTO mcp_package_groups VALUES(?,?)", (key, body["groupId"]))
        return {"accepted": True}

    def create(self, body, snapshots):
        key = identifier(body["requestId"])
        request = json.dumps(body, sort_keys=True)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request FROM mcp_batches WHERE id=?", (key,)).fetchone()
            if old:
                if old[0] != request:
                    raise SkillError("请求编号已用于其他下发。", 409)
                return key
            group_name = self.group(db, body["groupId"])
            if not db.execute("SELECT 1 FROM mcp_package_groups WHERE package_id=? AND group_id=?",
                              (body["packageId"], body["groupId"])).fetchone():
                raise SkillError("此 MCP 包不属于所选分组。", 409)
            for agent, snapshot in snapshots.items():
                row = db.execute("SELECT group_id,config,test_status FROM registered_agents WHERE id=?", (agent,)).fetchone()
                if (not row or row["group_id"] != body["groupId"] or row["config"] != snapshot["config"]
                        or row["test_status"] != "passed"):
                    raise SkillError("执行机设置已变化，请刷新后重试。", 409)
                settings = db.execute("SELECT settings FROM agent_mcp_settings WHERE agent_id=?", (agent,)).fetchone()
                if not settings or json.loads(settings[0]) != snapshot["settings"]:
                    raise SkillError("执行机安装设置已变化。", 409)
                skill = db.execute("SELECT enabled,root FROM agent_skill_settings WHERE agent_id=?", (agent,)).fetchone()
                if not skill or (bool(skill[0]), skill[1]) != (snapshot["skill"]["enabled"], snapshot["skill"]["root"]):
                    raise SkillError("执行机 Skill 安装授权已变化。", 409)
            db.execute("INSERT INTO mcp_batches VALUES(?,?,?,?,?,?)",
                       (key, body["packageId"], body["groupId"], group_name, request, now()))
            for agent, snapshot in snapshots.items():
                stamp = now()
                db.execute("""INSERT INTO mcp_tasks(id,batch_id,agent_id,state,snapshot,created_at,updated_at,execution_started)
                    VALUES(?,?,?,'queued',?,?,?,0)""", (str(uuid.uuid4()), key, agent, json.dumps(snapshot), stamp, stamp))
        return key

    def tasks(self, batch=None, group=None):
        with self.store._connect() as db:
            rows = db.execute("""SELECT t.*,b.package_id,b.group_id FROM mcp_tasks t JOIN mcp_batches b ON b.id=t.batch_id
                JOIN registered_agents a ON a.id=t.agent_id
                WHERE (? IS NULL OR t.batch_id=?) AND (? IS NULL OR a.group_id=?) ORDER BY t.created_at DESC""",
                (batch, batch, group, group)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def public(task):
        public = {key: task[key] for key in ("id", "batch_id", "agent_id", "state", "message", "created_at", "updated_at", "package_id")}
        result = json.loads(task["result"]) if task.get("result") else {}
        public["installation"] = {"programPath": result.get("command"), "skillPath": result.get("skillPath"),
                                  "verified": task["state"] == "completed",
                                  "programVerified": bool(task.get("program_verified")),
                                  "skillVerified": bool(task.get("skill_verified"))}
        public["diagnostics"] = json.loads(task["diagnostics"]) if task.get("diagnostics") else None
        return public

    def batches(self, group=None):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("SELECT id,package_id,group_id,group_name,created_at FROM mcp_batches WHERE ? IS NULL OR group_id=? ORDER BY created_at DESC LIMIT 100", (group, group))]

    def batch(self, key):
        with self.store._connect() as db:
            row = db.execute("SELECT id,package_id,group_id,group_name,created_at FROM mcp_batches WHERE id=?", (key,)).fetchone()
            if not row:
                raise SkillError("找不到下发批次。", 404)
        return {**dict(row), "tasks": [self.public(task) for task in self.tasks(batch=key)]}

    def update(self, key, **fields):
        allowed = {"state", "occupied", "mode", "thread_id", "turn_id", "result", "registration", "message",
                   "execution_started", "execution_stopped", "program_verified", "skill_verified", "diagnostics"}
        if not fields or set(fields) - allowed:
            raise ValueError("非法安装状态字段")
        with self.store._connect() as db:
            db.execute("UPDATE mcp_tasks SET " + ",".join(k + "=?" for k in fields) + ",updated_at=? WHERE id=?",
                       (*fields.values(), now(), key))

    def recover(self):
        with self.store._connect() as db:
            for row in db.execute('SELECT id,diagnostics FROM mcp_tasks WHERE occupied=1 AND diagnostics IS NOT NULL').fetchall():
                value = json.loads(row['diagnostics'])
                if value.get('pending'):
                    value.update(pending=False, reason='中央服务重启，检测已中断，请重新检测。')
                    db.execute('UPDATE mcp_tasks SET diagnostics=? WHERE id=?', (json.dumps(value), row['id']))
            db.execute("""UPDATE mcp_tasks SET state='failed',occupied=0,message='安装准备中断，尚未启动安装，可重试。'
                WHERE occupied=1 AND execution_started=0""")
            db.execute("""UPDATE mcp_tasks SET state='review',message='安装中断，请检测远程执行状态。'
                WHERE occupied=1 AND state<>'review'""")

    def claim(self):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = db.execute("SELECT * FROM mcp_tasks WHERE state='queued' ORDER BY created_at,id").fetchall()
            for row in candidates:
                agent = row["agent_id"]
                if db.execute("SELECT 1 FROM mcp_tasks WHERE agent_id=? AND occupied=1 AND id<>?", (agent, row["id"])).fetchone():
                    continue
                if db.execute("SELECT 1 FROM skill_tasks WHERE agent_id=? AND state='running'", (agent,)).fetchone():
                    continue
                if db.execute("""SELECT 1 FROM workflows w WHERE w.status IN ('queued','running','cancelling') AND
                    (w.supervisor_agent_id=? OR EXISTS(SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.agent_id=?))""", (agent, agent)).fetchone():
                    continue
                db.execute("UPDATE mcp_tasks SET state='transferring',occupied=1,updated_at=? WHERE id=?", (now(), row["id"]))
                return dict(row)
        return None

    def action(self, key, request_id, action):
        request_id = identifier(request_id)
        if action not in {"retry", "check"}:
            raise SkillError("不支持的安装操作。", 404)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT task_id,action FROM mcp_actions WHERE id=?", (request_id,)).fetchone()
            if old:
                if tuple(old) != (key, action):
                    raise SkillError("操作编号冲突。", 409)
                return
            row = db.execute("SELECT * FROM mcp_tasks WHERE id=?", (key,)).fetchone()
            if not row:
                raise SkillError("找不到安装任务。", 404)
            if row["state"] in ACTIVE and row["state"] != "review":
                raise SkillError("安装任务仍在进行。", 409)
            if row["state"] == "review" and action != "check":
                raise SkillError("请先检测，确认远程安装已停止。", 409)
            db.execute("INSERT INTO mcp_actions VALUES(?,?,?)", (request_id, key, action))
            installed = row["result"] and json.loads(row["result"]).get("status") == "installed"
            if action == "retry" and row["state"] == "failed" and not installed:
                db.execute("UPDATE mcp_tasks SET result=NULL,thread_id=NULL,turn_id=NULL,execution_started=0,execution_stopped=0 WHERE id=?", (key,))
            db.execute("UPDATE mcp_tasks SET state='queued',mode=?,updated_at=? WHERE id=?",
                       (action, now(), key))
