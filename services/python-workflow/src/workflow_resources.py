"""工作流整项占用机器；租约失效不代表远程执行已经停止。"""

import sqlite3
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from workflow_store import WorkflowStore


def initialize(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_machine_claims (
            agent_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
            acquired_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('occupied','review'))
        );
        CREATE INDEX IF NOT EXISTS workflow_machine_claims_owner
            ON workflow_machine_claims(workflow_id);
        CREATE TABLE IF NOT EXISTS workflow_execution_guards (
            workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
            execution_key TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            job_id TEXT, thread_id TEXT, turn_id TEXT,
            stopped INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(workflow_id, execution_key)
        );
        CREATE TABLE IF NOT EXISTS machine_check_claims (
            agent_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('occupied','review'))
        );
    """)


def protect_existing_executions(db: sqlite3.Connection) -> None:
    """首次升级保留仍活动的旧运行；不能凭新增表为空认定机器空闲。"""
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("""SELECT * FROM workflows w WHERE status IN ('running','cancelling')
        AND NOT EXISTS(SELECT 1 FROM workflow_machine_claims c WHERE c.workflow_id=w.workflow_id)
        ORDER BY created_at,workflow_id""").fetchall()
    for row in rows:
        nodes = db.execute("SELECT * FROM workflow_nodes WHERE workflow_id=?", (row["workflow_id"],)).fetchall()
        agents = {row["supervisor_agent_id"], *(node["agent_id"] for node in nodes)}
        for agent in agents:
            if claimed(db, agent, row["workflow_id"]):
                raise RuntimeError("升级前存在共享执行机的活动运行，请先结束原运行再升级。")
            db.execute("INSERT INTO workflow_machine_claims VALUES(?,?,?,'occupied')",
                       (agent, row["workflow_id"], row["started_at"] or row["created_at"]))
        executions = [("node:" + n["node_id"], n["agent_id"], n["job_id"], n["thread_id"], n["turn_id"])
                      for n in nodes if n["status"] in {"queued", "running", "cancelling"}]
        if row["supervisor_status"] in {"queued", "running", "cancelling"}:
            executions.append(("supervisor", row["supervisor_agent_id"], row["supervisor_job_id"],
                               row["supervisor_thread_id"], row["supervisor_turn_id"]))
        for execution in executions:
            db.execute("INSERT OR IGNORE INTO workflow_execution_guards VALUES(?,?,?,?,?,?,0)",
                       (row["workflow_id"], *execution))


def claimed(db: sqlite3.Connection, agent_id: str, workflow_id: str | None = None) -> bool:
    return db.execute("SELECT 1 FROM machine_check_claims WHERE agent_id=?", (agent_id,)).fetchone() is not None or db.execute(
        "SELECT 1 FROM workflow_machine_claims WHERE agent_id=? AND workflow_id IS NOT ?",
        (agent_id, workflow_id),
    ).fetchone() is not None


def claim_check(store: "WorkflowStore", agent_id: str, operation_id: str, timestamp: str) -> bool:
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if claimed(db, agent_id) or installation_busy(db, agent_id):
            return False
        db.execute("INSERT INTO machine_check_claims VALUES(?,?,?,'occupied')", (agent_id, operation_id, timestamp))
        return True


def finish_check(store: "WorkflowStore", operation_id: str, stopped: bool) -> None:
    with store._connect() as db:
        if stopped:
            db.execute("DELETE FROM machine_check_claims WHERE operation_id=?", (operation_id,))
        else:
            db.execute("UPDATE machine_check_claims SET state='review' WHERE operation_id=?", (operation_id,))


def installation_busy(db: sqlite3.Connection, agent_id: str) -> bool:
    for table, predicate in (("mcp_tasks", "occupied=1"), ("skill_tasks", "state='running'")):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            if db.execute(f"SELECT 1 FROM {table} WHERE agent_id=? AND {predicate}", (agent_id,)).fetchone():
                return True
    return False


def acquire(db: sqlite3.Connection, workflow_id: str, agent_ids: Iterable[str], timestamp: str) -> bool:
    """调用方必须持有写事务；先检查全体再写入，不能部分占用。"""
    agents = set(agent_ids)
    for owner in db.execute("SELECT DISTINCT workflow_id FROM workflow_machine_claims WHERE agent_id IN (" + ",".join("?" for _ in agents) + ")", tuple(agents)).fetchall():
        release_if_stopped(db, owner[0])
    if any(claimed(db, agent, workflow_id) or installation_busy(db, agent) for agent in agents):
        return False
    # 升级前已经启动而尚无占用记录的运行仍须保护。
    for agent in agents:
        if db.execute("""SELECT 1 FROM workflows w
            WHERE w.workflow_id<>? AND w.status IN ('running','cancelling') AND
            (w.supervisor_agent_id=? OR EXISTS
             (SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.agent_id=?))""",
                      (workflow_id, agent, agent)).fetchone():
            return False
    for agent in sorted(agents):
        db.execute("INSERT OR IGNORE INTO workflow_machine_claims VALUES(?,?,?,'occupied')",
                   (agent, workflow_id, timestamp))
    return True


def prepare_execution(db: sqlite3.Connection, workflow_id: str, execution_key: str, agent_id: str) -> None:
    """在可能产生外部执行之前持久化；响应丢失时不能凭没有编号释放机器。"""
    old = db.execute("SELECT stopped FROM workflow_execution_guards WHERE workflow_id=? AND execution_key=?",
                     (workflow_id, execution_key)).fetchone()
    if old and not old["stopped"]:
        raise RuntimeError("尚未确认此前执行已停止，不能再次启动。")
    db.execute("""INSERT INTO workflow_execution_guards VALUES(?,?,?,NULL,NULL,NULL,0)
        ON CONFLICT(workflow_id,execution_key) DO UPDATE SET
        agent_id=excluded.agent_id,job_id=NULL,thread_id=NULL,turn_id=NULL,stopped=0""",
               (workflow_id, execution_key, agent_id))


def observe_execution(db: sqlite3.Connection, workflow_id: str, execution_key: str, snapshot: dict[str, Any]) -> None:
    old = db.execute("SELECT * FROM workflow_execution_guards WHERE workflow_id=? AND execution_key=?",
                     (workflow_id, execution_key)).fetchone()
    if not old:
        return
    job = snapshot.get("job_id")
    if old["job_id"] and job != old["job_id"]:
        return  # 旧轮次或缺少身份的迟到结果不能释放当前执行。
    terminal = snapshot.get("status") in {"completed", "failed", "cancelled", "interrupted"}
    confirmed = terminal and (snapshot.get("execution_stopped") is True
                              or snapshot.get("status") == "completed")
    db.execute("""UPDATE workflow_execution_guards SET job_id=COALESCE(?,job_id),
        thread_id=COALESCE(?,thread_id),turn_id=COALESCE(?,turn_id),stopped=MAX(stopped,?)
        WHERE workflow_id=? AND execution_key=?""",
               (job, snapshot.get("thread_id"), snapshot.get("turn_id"), int(confirmed), workflow_id, execution_key))


def release_if_stopped(db: sqlite3.Connection, workflow_id: str) -> bool:
    workflow = db.execute("SELECT status FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    if not workflow or workflow["status"] not in {"completed", "failed", "cancelled"}:
        return False
    if db.execute("SELECT 1 FROM workflow_execution_guards WHERE workflow_id=? AND stopped=0",
                  (workflow_id,)).fetchone():
        changed = db.execute("UPDATE workflow_machine_claims SET state='review' WHERE workflow_id=? AND state<>'review'", (workflow_id,)).rowcount
        if changed:
            db.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
        return False
    changed = db.execute("DELETE FROM workflow_machine_claims WHERE workflow_id=?", (workflow_id,)).rowcount
    if changed:
        db.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
    return True


def snapshot(db: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
    rows = db.execute("SELECT agent_id,state,acquired_at FROM workflow_machine_claims WHERE workflow_id=? ORDER BY agent_id",
                      (workflow_id,)).fetchall()
    return {"state": "review" if any(r["state"] == "review" for r in rows) else "occupied" if rows else "released",
            "machines": [{"agentId": r["agent_id"], "acquiredAt": r["acquired_at"]} for r in rows]}
