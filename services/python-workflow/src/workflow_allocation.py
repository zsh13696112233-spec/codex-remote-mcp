"""分组运行的实际机器和目录；提交规格保持不可变。"""

import json
import zlib
import sqlite3
from typing import Any

from workflow_resources import acquire
from workflow_jira import compatible_agent


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_allocations (
        workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
        group_id TEXT NOT NULL, supervisor_id TEXT, executor_id TEXT, cwd TEXT,
        assigned_at TEXT, waiting_reason TEXT NOT NULL DEFAULT '等待空闲监督机和执行机。'
    )""")


def decode(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(zlib.decompress(row["spec_zlib"]).decode()) if row["spec_zlib"] is not None else json.loads(row["spec_json"])


def select_executor(db: sqlite3.Connection, workflow_id: str, supervisor_id: str, timestamp: str, eligible: set[str] | None = None) -> bool:
    """在领取监督租约的同一个事务中完成整项资源分配。"""
    from agent_registry import AgentRegistry
    allocation = db.execute("SELECT * FROM workflow_allocations WHERE workflow_id=?", (workflow_id,)).fetchone()
    if allocation is None:
        return True
    if allocation["executor_id"]:
        return allocation["supervisor_id"] == supervisor_id
    supervisor = db.execute("SELECT * FROM registered_agents WHERE id=? AND deleted_at IS NULL", (supervisor_id,)).fetchone()
    if supervisor is None or supervisor["test_status"] != "passed":
        return False
    supervisor_config = AgentRegistry.config_from_row(supervisor_id, supervisor)
    if not supervisor_config.enabled or "supervisor" not in supervisor_config.capabilities:
        return False
    row = db.execute("SELECT spec_json,spec_zlib FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    spec = decode(row)
    if spec.get("jira") and not compatible_agent(supervisor_config):
        return False
    candidates = db.execute("""SELECT * FROM registered_agents WHERE group_id=?
        AND deleted_at IS NULL AND test_status='passed' AND id<>? ORDER BY id""",
                            (allocation["group_id"], supervisor_id)).fetchall()
    compatible = False
    for candidate in candidates:
        agent = AgentRegistry.config_from_row(candidate["id"], candidate)
        if not agent.enabled or "executor" not in agent.capabilities:
            continue
        if spec.get("jira") and not compatible_agent(agent):
            continue
        profiles = agent.public_dict()["permission_profiles"]
        if any(node["permissionProfile"] not in profiles for node in spec["nodes"]):
            continue
        compatible = True
        if eligible is not None and candidate["id"] not in eligible:
            continue
        if not acquire(db, workflow_id, [supervisor_id, candidate["id"]], timestamp):
            continue
        db.execute("""UPDATE workflow_allocations SET supervisor_id=?,executor_id=?,cwd=?,assigned_at=?,waiting_reason=''
            WHERE workflow_id=?""", (supervisor_id, candidate["id"], agent.cwd, timestamp, workflow_id))
        db.execute("UPDATE workflows SET supervisor_agent_id=? WHERE workflow_id=?", (supervisor_id, workflow_id))
        db.execute("UPDATE workflow_nodes SET agent_id=?,cwd=?,executor_type='remote' WHERE workflow_id=?",
                   (candidate["id"], agent.cwd, workflow_id))
        return True
    reason = "等待本组满足全部步骤要求的空闲执行机。" if compatible else "组内没有已启用、检测通过且满足步骤权限的执行机。"
    changed = db.execute("UPDATE workflow_allocations SET waiting_reason=? WHERE workflow_id=? AND waiting_reason<>?", (reason, workflow_id, reason)).rowcount
    if changed:
        db.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
    return False


def resolve(db: sqlite3.Connection, spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("dispatchMode") != "group":
        return spec
    row = db.execute("SELECT * FROM workflow_allocations WHERE workflow_id=?", (spec["workflowId"],)).fetchone()
    if row and row["executor_id"]:
        spec["supervisorAgentId"] = row["supervisor_id"]
        for node in spec["nodes"]:
            node.update(agentId=row["executor_id"], cwd=row["cwd"], executorType="remote")
    return spec


def snapshot(db: sqlite3.Connection, workflow_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM workflow_allocations WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        return None
    agents = {item["id"]: item for item in db.execute(
        "SELECT id,remark,deleted_at FROM registered_agents WHERE id IN (?,?)",
        (row["supervisor_id"], row["executor_id"]))}
    deleted = {key for key, agent in agents.items() if agent["deleted_at"] is not None}
    return {"supervisorRemark": agents[row["supervisor_id"]]["remark"] if row["supervisor_id"] in agents else "",
            "executorRemark": agents[row["executor_id"]]["remark"] if row["executor_id"] in agents else "",
            "supervisorDeleted": row["supervisor_id"] in deleted,
            "executorDeleted": row["executor_id"] in deleted,
            "groupId": row["group_id"], "supervisorAgentId": row["supervisor_id"],
            "executorAgentId": row["executor_id"], "workingDirectory": row["cwd"],
            "assignedAt": row["assigned_at"], "waitingReason": row["waiting_reason"]}
