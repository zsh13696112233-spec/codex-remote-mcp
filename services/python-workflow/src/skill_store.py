"""中央 SQLite Skill 包、安装归属及后台任务。"""

import json
import uuid
from datetime import datetime, timezone

from skill_packages import SkillError


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str):
        raise SkillError("请求编号必须为 UUID。")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise SkillError("请求编号必须为 UUID。") from error


class SkillStore:
    def __init__(self, workflow_store):
        self.store = workflow_store
        with self.store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_packages (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_batches (
                    id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES skill_packages(id),
                    request_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_installations (
                    agent_id TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
                    package_id TEXT NOT NULL REFERENCES skill_packages(id), owner TEXT NOT NULL UNIQUE,
                    target TEXT NOT NULL, identity TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(agent_id, name));
                CREATE TABLE IF NOT EXISTS skill_tasks (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES skill_batches(id),
                    agent_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'install', progress TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, agent_id));
                CREATE UNIQUE INDEX IF NOT EXISTS skill_agent_running
                    ON skill_tasks(agent_id) WHERE state='running';
                CREATE INDEX IF NOT EXISTS skill_task_queue
                    ON skill_tasks(state, created_at, id);
                CREATE TABLE IF NOT EXISTS skill_actions (
                    request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES skill_tasks(id),
                    action TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_group_packages (
                    group_id TEXT NOT NULL REFERENCES agent_groups(id),
                    package_id TEXT NOT NULL REFERENCES skill_packages(id),
                    PRIMARY KEY(group_id, package_id));
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(skill_batches)")}
            for column in ("group_id", "group_name"):
                if column not in columns:
                    db.execute(f"ALTER TABLE skill_batches ADD COLUMN {column} TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS skill_batch_group ON skill_batches(group_id,created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS skill_package_groups ON skill_group_packages(package_id,group_id)")

    def require_group(self, db, group_id):
        try:
            key = identifier(group_id)
        except SkillError as error:
            raise SkillError("请选择有效分组。") from error
        if key != group_id:
            raise SkillError("请使用列表中的分组编号。")
        row = db.execute("SELECT name FROM agent_groups WHERE id=?", (key,)).fetchone()
        if not row:
            raise SkillError("所选分组不存在，请刷新列表。", 409)
        return row[0]

    def validate_group(self, group_id):
        with self.store._connect() as db:
            return self.require_group(db, group_id)

    def assign(self, body):
        if not isinstance(body, dict) or set(body) != {"groupId", "packageIds"}:
            raise SkillError("请选择分组和 Skill 包。")
        keys = body["packageIds"]
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 200
                or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys)):
            raise SkillError("请选择 1–200 个不重复的 Skill 包。")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_group(db, body["groupId"])
            for key in keys:
                if not db.execute("SELECT 1 FROM skill_packages WHERE id=?", (key,)).fetchone():
                    raise SkillError("找不到 Skill 包。", 404)
                db.execute("INSERT OR IGNORE INTO skill_group_packages VALUES(?,?)", (body["groupId"], key))
        return {"accepted": True}

    def package_groups(self, db, key):
        return [dict(r) for r in db.execute("SELECT g.id,g.name FROM agent_groups g JOIN skill_group_packages s ON s.group_id=g.id WHERE s.package_id=? ORDER BY g.name,g.id", (key,))]

    def require_membership(self, db, group_id, package_id, agents):
        name = self.require_group(db, group_id)
        if not db.execute("SELECT 1 FROM skill_group_packages WHERE group_id=? AND package_id=?", (group_id, package_id)).fetchone():
            raise SkillError("请先将 Skill 加入所选分组。", 409)
        for agent in agents:
            if not db.execute("SELECT 1 FROM registered_agents WHERE id=? AND group_id=?", (agent, group_id)).fetchone():
                raise SkillError("执行机必须属于所选分组。", 409)
        return name

    def add_package(self, metadata, group_id=None):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if group_id is not None:
                self.require_group(db, group_id)
            db.execute("INSERT OR IGNORE INTO skill_packages VALUES(?,?,?)",
                       (metadata["id"], json.dumps(metadata), now()))
            if group_id is not None:
                db.execute("INSERT OR IGNORE INTO skill_group_packages VALUES(?,?)", (group_id, metadata["id"]))
        return self.package(metadata["id"])

    def package(self, key):
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM skill_packages WHERE id=?", (key,)).fetchone()
            if not row:
                raise SkillError("找不到 Skill 包。", 404)
            return {**json.loads(row["metadata"]), "createdAt": row["created_at"], "groups": self.package_groups(db, key)}

    def packages(self, group_id=None):
        with self.store._connect() as db:
            if group_id:
                self.require_group(db, group_id)
            return [{**json.loads(r["metadata"]), "createdAt": r["created_at"], "groups": self.package_groups(db, r["id"])}
                    for r in db.execute("SELECT * FROM skill_packages p WHERE (? IS NULL OR EXISTS (SELECT 1 FROM skill_group_packages g WHERE g.package_id=p.id AND g.group_id=?)) ORDER BY created_at DESC LIMIT 200", (group_id or None, group_id))]

    def create(self, request_id, package, targets, group_id):
        key = identifier(request_id)
        request = json.dumps({"groupId": group_id, "packageId": package["id"], "agentIds": sorted(t["agentId"] for t in targets)}, sort_keys=True)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_json FROM skill_batches WHERE id=?", (key,)).fetchone()
            if old:
                if old[0] != request:
                    raise SkillError("请求编号已用于其他下发内容。", 409)
                return key
            group_name = self.require_membership(db, group_id, package["id"], [t["agentId"] for t in targets])
            db.execute("INSERT INTO skill_batches(id,package_id,request_json,created_at,group_id,group_name) VALUES(?,?,?,?,?,?)", (key, package["id"], request, now(), group_id, group_name))
            for target in targets:
                agent = target["agentId"]
                old = db.execute("SELECT * FROM skill_installations WHERE agent_id=? AND name=?",
                                 (agent, package["name"])).fetchone()
                error = target.get("error")
                if old and (old["package_id"] != package["id"] or old["target"] != target.get("target")
                            or old["identity"] != target.get("identity")):
                    error = "该机器已有同名安装记录或目标配置已变化，首版不支持覆盖。"
                if not old and not error:
                    db.execute("INSERT INTO skill_installations VALUES(?,?,?,?,?,?,0)",
                               (agent, package["name"], package["id"], str(uuid.uuid4()), target["target"], target["identity"]))
                db.execute("INSERT INTO skill_tasks(id,batch_id,agent_id,name,state,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), key, agent, package["name"], "failed" if error else "queued", error, now(), now()))
        return key

    def batches(self, group_id=None):
        with self.store._connect() as db:
            if group_id:
                self.require_group(db, group_id)
            ids = [r[0] for r in db.execute("SELECT id FROM skill_batches WHERE (? IS NULL OR group_id=?) ORDER BY created_at DESC LIMIT 100", (group_id or None, group_id))]
        return [self.batch(key) for key in ids]

    def inventory(self, group_id=None):
        """按执行机及名称汇总全量记录；冲突的新包不替换原安装的展示。"""
        with self.store._connect() as db:
            if group_id:
                self.require_group(db, group_id)
            rows = db.execute("""WITH ranked AS (
                SELECT t.*, b.package_id, b.group_id, p.metadata, i.target,
                    ROW_NUMBER() OVER (PARTITION BY t.agent_id, t.name COLLATE NOCASE
                        ORDER BY CASE WHEN b.package_id=i.package_id THEN 0 ELSE 1 END,
                        CASE WHEN t.state IN ('queued','running') THEN 0 ELSE 1 END,
                        t.updated_at DESC, t.rowid DESC) AS rank
                FROM skill_tasks t JOIN skill_batches b ON b.id=t.batch_id
                JOIN skill_packages p ON p.id=b.package_id
                LEFT JOIN skill_installations i ON i.agent_id=t.agent_id AND i.name=t.name
                ) SELECT * FROM ranked WHERE rank=1 AND (? IS NULL OR EXISTS
                    (SELECT 1 FROM registered_agents a WHERE a.id=ranked.agent_id AND a.group_id=?))
                    ORDER BY agent_id,name COLLATE NOCASE""", (group_id or None, group_id)).fetchall()
        result = []
        for row in rows:
            package = json.loads(row["metadata"])
            result.append({"agentId": row["agent_id"], "name": row["name"],
                "packageId": row["package_id"], "taskId": row["id"], "state": row["state"],
                "mode": row["mode"], "updatedAt": row["updated_at"], "error": row["error"],
                "completedFiles": len(json.loads(row["progress"])), "fileCount": package["fileCount"],
                "hasScripts": package["hasScripts"], "target": row["target"],
                "groups": self.package(row["package_id"])["groups"],
                "canRetry": self.can_retry(row)})
        return result

    def can_retry(self, task):
        if task["state"] != "failed":
            return False
        try:
            with self.store._connect() as db:
                self.validate_task_group(db, task, require_group=True)
            return True
        except SkillError:
            return False

    def batch(self, key):
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM skill_batches WHERE id=?", (key,)).fetchone()
            if not row:
                raise SkillError("找不到下发记录。", 404)
            tasks = [dict(r) for r in db.execute("SELECT * FROM skill_tasks WHERE batch_id=? ORDER BY created_at,id", (key,))]
        for task in tasks:
            task["completedFiles"] = len(json.loads(task.pop("progress")))
            task["canRetry"] = self.can_retry(task)
        package = self.package(row["package_id"])
        return {"id": key, "packageId": package["id"], "name": package["name"],
                "groupId": row["group_id"], "groupName": row["group_name"],
                "hasScripts": package["hasScripts"], "fileCount": package["fileCount"],
                "createdAt": row["created_at"], "tasks": tasks}

    def action(self, task_id, request_id, action):
        key = identifier(request_id)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM skill_actions WHERE request_id=?", (key,)).fetchone()
            if old:
                if old["task_id"] != task_id or old["action"] != action:
                    raise SkillError("请求编号已用于其他操作。", 409)
                return
            task = db.execute("SELECT * FROM skill_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise SkillError("找不到安装任务。", 404)
            if action == "retry":
                self.validate_task_group(db, task, require_group=True)
            if task["state"] in {"queued", "running"} or action == "retry" and task["state"] != "failed":
                raise SkillError("当前状态不允许此操作。", 409)
            if not db.execute("SELECT 1 FROM skill_installations WHERE agent_id=? AND name=?", (task["agent_id"], task["name"])).fetchone():
                raise SkillError("请修正机器配置后重新创建下发批次。", 409)
            db.execute("INSERT INTO skill_actions VALUES(?,?,?)", (key, task_id, action))
            db.execute("UPDATE skill_tasks SET state='queued',mode=?,attempts=0,error=NULL,updated_at=? WHERE id=?",
                       ("check" if action == "check" else "install", now(), task_id))

    def validate_task_group(self, db, task, require_group=False):
        batch = db.execute("SELECT * FROM skill_batches WHERE id=?", (task["batch_id"],)).fetchone()
        if not batch["group_id"]:
            if require_group:
                raise SkillError("旧下发记录请先归组，再重新下发。", 409)
            return
        self.require_membership(db, batch["group_id"], batch["package_id"], [task["agent_id"]])

    def validate_execution(self, task):
        if task["mode"] != "check":
            with self.store._connect() as db:
                self.validate_task_group(db, task)

    def recover(self):
        # 仅持有中央进程锁者可调用，确保旧进程已经退出。
        with self.store._connect() as db:
            db.execute("UPDATE skill_tasks SET state='queued',updated_at=? WHERE state='running'", (now(),))

    def claim(self):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT t.* FROM skill_tasks t WHERE state='queued'
                AND NOT EXISTS(SELECT 1 FROM skill_tasks r WHERE r.agent_id=t.agent_id AND r.state='running')
                ORDER BY created_at,id""").fetchall()
            from mcp_store import ensure_available
            for row in rows:
                try:
                    ensure_available(db, [row["agent_id"]])
                except ValueError:
                    continue
                db.execute("UPDATE skill_tasks SET state='running',updated_at=? WHERE id=?", (now(), row["id"]))
                return dict(row)
            return None

    def installation(self, task):
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM skill_installations WHERE agent_id=? AND name=?", (task["agent_id"], task["name"])).fetchone()
            if not row:
                raise SkillError("安装归属记录缺失，请重新下发。", 409)
            package_id = db.execute("SELECT package_id FROM skill_batches WHERE id=?", (task["batch_id"],)).fetchone()[0]
            if row["package_id"] != package_id:
                raise SkillError("同名不同内容不能覆盖。", 409)
            return dict(row)

    def owned(self, owner):
        with self.store._connect() as db:
            db.execute("UPDATE skill_installations SET claimed=1 WHERE owner=?", (owner,))

    def progress(self, task, paths):
        with self.store._connect() as db:
            db.execute("UPDATE skill_tasks SET progress=?,updated_at=? WHERE id=?", (json.dumps(paths), now(), task["id"]))

    def attempt(self, task):
        with self.store._connect() as db:
            db.execute("UPDATE skill_tasks SET attempts=attempts+1,updated_at=? WHERE id=?", (now(), task["id"]))

    def finish(self, task, error=None):
        with self.store._connect() as db:
            db.execute("UPDATE skill_tasks SET state=?,error=?,updated_at=? WHERE id=?",
                       ("failed" if error else "completed", error, now(), task["id"]))
