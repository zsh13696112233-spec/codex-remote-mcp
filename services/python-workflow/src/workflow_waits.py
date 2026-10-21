"""半自动运行的三类等待；状态领取始终在中央数据库事务内完成。"""
import json
import uuid
import sqlite3
from typing import Any
from datetime import datetime, timedelta, timezone

UTC = timezone.utc



def normalize_policy(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("等待策略字段无效。")
    if value["version"] == 2:
        if set(value) != {"version"}:
            raise ValueError("人工确认策略不支持等待时长。")
        return dict(value)
    if value["version"] != 1:
        raise ValueError("等待策略版本无效。")
    if set(value) != {"version", "confirmationMinutes", "blockedMinutes"}:
        raise ValueError("等待策略字段无效。")
    for key in ("confirmationMinutes", "blockedMinutes"):
        if type(value[key]) is not int or not 1 <= value[key] <= 720:
            raise ValueError("等待时长必须是 1 到 720 分钟的整数。")
    return dict(value)


class WaitStore:
    @staticmethod
    def _wait_policy(connection: sqlite3.Connection, workflow_id: str) -> dict[str, int] | None:
        row = connection.execute("SELECT wait_policy_json FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _create_policy_wait(self, connection: sqlite3.Connection, workflow_id: str, node_id: str, outcome: dict[str, Any] | None = None) -> bool:
        policy = self._wait_policy(connection, workflow_id)
        if not policy:
            return False
        next_node = connection.execute(
            "SELECT node_id FROM workflow_nodes WHERE workflow_id=? AND status='pending' "
            "AND depends_on_json=? ORDER BY position LIMIT 1",
            (workflow_id, json.dumps([node_id], ensure_ascii=False)),
        ).fetchone()
        kind = "blocked" if outcome else ("advance" if next_node else "finish")
        now = datetime.now(UTC)
        manual = policy["version"] == 2
        # 历史表的截止时间不可空；held 状态不参与到期处理。
        expires_at = (now if manual else now + timedelta(
            minutes=policy["blockedMinutes" if outcome else "confirmationMinutes"])).isoformat()
        gate_id = uuid.uuid4().hex
        self._supersede_pending_advances(connection, workflow_id, "new_gate", now.isoformat())
        connection.execute(
            "INSERT INTO workflow_advance_gates(gate_id,workflow_id,completed_node_id,next_node_id,"
            "status,expires_at,held_at,created_at,updated_at,kind,business_result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (gate_id, workflow_id, node_id, next_node[0] if next_node else "",
             "held" if manual else "pending", expires_at, now.isoformat() if manual else None,
             now.isoformat(), now.isoformat(), kind,
             json.dumps(outcome, ensure_ascii=False) if outcome else None),
        )
        self._add_event_with_connection(connection, workflow_id, node_id, "gateway", "step.advance.waiting",
            {"gateId": gate_id, "completedNodeId": node_id, "nextNodeId": next_node[0] if next_node else None,
             "kind": kind, "state": "held" if manual else "countdown", "expiresAt": expires_at}, now.isoformat())
        return True

    def policy_wait(self, workflow_id: str, gate_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=? AND kind!='legacy'",
                                     (workflow_id, gate_id)).fetchone()
            return dict(row) if row else None

    def check_wait_stop(self, workflow_id: str, gate_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not row or row[0] != "cancelled":
                self._check_policy_wait(connection, workflow_id, gate_id)

    def _check_policy_wait(self, connection: sqlite3.Connection, workflow_id: str, gate_id: str, *, allow_expired: bool = False) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=? AND kind!='legacy'",
                                 (workflow_id, gate_id)).fetchone()
        workflow = connection.execute("SELECT status,termination_json FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        if not row or not workflow or workflow["status"] != "running" or workflow["termination_json"]:
            raise RuntimeError("这次等待已失效，请刷新后查看任务状态。")
        if row["status"] not in {"pending", "held"}:
            raise RuntimeError("这次等待已处理，请勿重复操作。")
        if not allow_expired and row["status"] == "pending" and row["expires_at"] <= datetime.now(UTC).isoformat():
            raise RuntimeError("等待时间已到，系统正在处理。")
        return row

    def claim_policy_wait(self, workflow_id: str, gate_id: str, action: str, *, timed_out: bool = False) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=?", (workflow_id, gate_id)).fetchone()
            expected = {"confirm": "finishing" if old and old["kind"] == "finish" else "confirmed", "retry": "executing", "timeout": "closing"}
            if not timed_out and old and old["kind"] != "legacy" and (old["status"] == expected.get(action) or (action == "confirm" and old["kind"] == "finish" and old["status"] == "confirmed")):
                return {"gateId": gate_id, "status": old["status"], "duplicate": True}
            row = self._check_policy_wait(connection, workflow_id, gate_id, allow_expired=timed_out)
            self._require_discussion_idle(connection, workflow_id)
            if timed_out and (row["status"] != "pending" or row["expires_at"] > now):
                raise RuntimeError("等待尚未到期或已经保持。")
            if row["kind"] == "blocked":
                if action not in {"retry", "timeout"}:
                    raise RuntimeError("本步骤尚未完成，请通过卡片重新执行。")
                status = "closing" if timed_out else "executing"
                if action == "retry":
                    node = connection.execute("SELECT * FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (workflow_id, row["completed_node_id"])).fetchone()
                    if not node or node["status"] != "waiting" or not node["thread_id"]:
                        raise RuntimeError("原步骤会话不可恢复，已保持等待。")
            else:
                if action != "confirm":
                    raise RuntimeError("当前等待不支持此操作。")
                status = "finishing" if row["kind"] == "finish" else "confirmed"
            connection.execute("UPDATE workflow_advance_gates SET status=?,confirmed_at=?,updated_at=? WHERE gate_id=?", (status, now, now, gate_id))
            self._add_event_with_connection(connection, workflow_id, row["completed_node_id"], "gateway",
                "step.advance.timed_out" if timed_out else "step.advance.confirmed",
                {"gateId": gate_id, "kind": row["kind"], "action": action, "nextNodeId": row["next_node_id"] or None}, now)
            return {"gateId": gate_id, "status": status, "resumedFromHold": row["status"] == "held"}

    def hold_policy_wait(self, workflow_id: str, gate_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._check_policy_wait(connection, workflow_id, gate_id)
            if row["status"] != "held":
                connection.execute("UPDATE workflow_advance_gates SET status='held',held_at=?,updated_at=? WHERE gate_id=?", (now, now, gate_id))
                self._add_event_with_connection(connection, workflow_id, row["completed_node_id"], "gateway", "step.advance.held", {"gateId": gate_id}, now)
            return {"gateId": gate_id, "status": "held", "heldAt": row["held_at"] or now}

    def policy_wait_workflows(self, ready: bool = False) -> list[str]:
        with self._connect() as connection:
            if ready:
                sql = ("SELECT w.workflow_id FROM workflows w WHERE w.status='running' AND w.wait_policy_json IS NOT NULL "
                       "AND w.termination_json IS NULL AND NOT EXISTS(SELECT 1 FROM workflow_advance_gates g WHERE g.workflow_id=w.workflow_id "
                       "AND g.status IN ('pending','held','executing','closing','finishing')) "
                       "AND NOT EXISTS(SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.status IN ('failed','cancelled','interrupted')) "
                       "AND EXISTS(SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.status='pending')")
            else:
                sql = ("SELECT DISTINCT w.workflow_id FROM workflows w JOIN workflow_advance_gates g USING(workflow_id) "
                       "WHERE w.status='running' AND g.kind!='legacy' AND g.status IN ('pending','held')")
            return [r[0] for r in connection.execute(sql)]

    def set_wait_job(self, workflow_id: str, gate_id: str, job_id: str, turn_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE workflow_advance_gates SET execution_job_id=?,execution_turn_id=? WHERE workflow_id=? AND gate_id=? AND status IN ('executing','closing')", (job_id, turn_id, workflow_id, gate_id))

    def fail_wait_execution(self, workflow_id: str, gate_id: str, *, uncertain: bool = False, before_start: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            gate = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=? AND status='executing'", (workflow_id, gate_id)).fetchone()
            if not gate:
                return
            error = "重新执行结果尚未确认，请核查执行状态；未自动重发。" if uncertain else "重新执行失败，请查看任务详情。"
            if before_start:
                error = "原步骤执行未能启动，已保持等待；请核查服务、原会话、执行机及权限后重试。"
                connection.execute("UPDATE workflow_nodes SET status='waiting',error=? WHERE workflow_id=? AND node_id=?", (error, workflow_id, gate["completed_node_id"]))
                self._create_policy_wait(connection, workflow_id, gate["completed_node_id"], json.loads(gate["business_result_json"]))
                connection.execute("UPDATE workflow_advance_gates SET status='held',held_at=?,updated_at=? WHERE workflow_id=? AND kind='blocked' AND status='pending'", (now, now, workflow_id))
            elif uncertain:
                connection.execute("UPDATE workflow_nodes SET error=? WHERE workflow_id=? AND node_id=?", (error, workflow_id, gate["completed_node_id"]))
            else:
                connection.execute("UPDATE workflow_nodes SET status='failed',error=?,finished_at=? WHERE workflow_id=? AND node_id=?", (error, now, workflow_id, gate["completed_node_id"]))
                connection.execute("UPDATE workflow_advance_gates SET status='superseded',updated_at=? WHERE gate_id=?", (now, gate_id))
            self._add_event_with_connection(connection, workflow_id, gate["completed_node_id"], "gateway", "node.retry_unconfirmed" if uncertain else "node.failed", {"error": error}, now)
        if not uncertain and not before_start:
            self.finish_workflow(workflow_id, supervisor_status="failed", response=None, error=error)

    def due_policy_waits(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT g.* FROM workflow_advance_gates g JOIN workflows w USING(workflow_id) "
                "WHERE w.status='running' AND g.kind!='legacy' AND g.execution_started=0 AND "
                "((g.status='pending' AND g.expires_at<=?) OR g.status IN ('executing','closing','finishing')) "
                "ORDER BY g.expires_at LIMIT 100", (datetime.now(UTC).isoformat(),))]

    def prepare_wait_execution(self, workflow_id: str, gate_id: str) -> dict[str, Any] | None:
        """领取后只准备一次；不确定的外部执行绝不自动重新派发。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            gate = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=?", (workflow_id, gate_id)).fetchone()
            if not gate or gate["status"] not in {"executing", "closing"} or gate["execution_started"]:
                return None
            workflow = connection.execute("SELECT status FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not workflow or workflow[0] != "running":
                return None
            node = connection.execute("SELECT * FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (workflow_id, gate["completed_node_id"])).fetchone()
            target = self._node_dispatch_spec(node, already_dispatched=True)
            runtime = connection.execute("SELECT cwd,model FROM workflow_node_runtime WHERE workflow_id=? AND node_id=? AND attempt_number=?", (workflow_id, node["node_id"], node["attempt_count"])).fetchone()
            if runtime:
                target.update(cwd=runtime["cwd"] or target["cwd"], model=runtime["model"] or target["model"])
            target.update(summary=node["response"] or "", gate=dict(gate))
            connection.execute("UPDATE workflow_advance_gates SET execution_started=1 WHERE gate_id=?", (gate_id,))
            if gate["status"] == "executing":
                columns = "workflow_id,node_id,status,dispatch_token,job_id,thread_id,turn_id,response,error,actual_prompt,started_at,finished_at"
                connection.execute(f"INSERT INTO workflow_node_attempts({columns},attempt_number,archived_at) SELECT {columns},attempt_count,? FROM workflow_nodes WHERE workflow_id=? AND node_id=?",
                                   (datetime.now(UTC).isoformat(), workflow_id, node["node_id"]))
                connection.execute("UPDATE workflow_nodes SET status='queued',attempt_count=attempt_count+1,job_id=NULL,turn_id=NULL,started_at=?,finished_at=NULL,error=NULL WHERE workflow_id=? AND node_id=?", (datetime.now(UTC).isoformat(), workflow_id, node["node_id"]))
                connection.execute("INSERT INTO workflow_node_runtime(workflow_id,node_id,attempt_number,cwd,model) SELECT workflow_id,node_id,attempt_count,?,? FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (target["cwd"], target["model"], workflow_id, node["node_id"]))
            return target

    def finish_wait_result(self, workflow_id: str, gate_id: str, result: dict[str, Any]) -> None:
        """超时收尾只更新预先冻结的阻断决定，不能改成成功或恢复执行。"""
        from workflow_outcomes import validate, message
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            gate = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=?", (workflow_id, gate_id)).fetchone()
            workflow = connection.execute("SELECT status,result_protocol_version FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not gate or gate["status"] != "closing" or workflow["status"] != "running":
                return
            original = json.loads(gate["business_result_json"])
            result = validate(result, workflow["result_protocol_version"])
            if result["outcome"] != "blocked" or result["jiraComment"]["issueKey"] != original["jiraComment"]["issueKey"]:
                raise ValueError("收尾结果不能改变阻断决定或工单身份。")
            original.update(summary=result["summary"], jiraComment=result["jiraComment"])
            if "jiraDeveloper" in result:
                original["jiraDeveloper"] = result["jiraDeveloper"]
            termination = {key: original[key] for key in ("outcome", "reason", "jiraComment", "jiraDeveloper") if key in original}
            termination["nodeId"] = gate["completed_node_id"]
            connection.execute("UPDATE workflows SET termination_json=?,response=? WHERE workflow_id=?", (json.dumps(termination, ensure_ascii=False), message(termination), workflow_id))
            connection.execute("UPDATE workflow_nodes SET status='failed',response=?,error=?,finished_at=? WHERE workflow_id=? AND node_id=?", (original["summary"], original["reason"], now, workflow_id, gate["completed_node_id"]))
            connection.execute("UPDATE workflow_nodes SET status='skipped',error='流程提前结束，本步骤未执行。',finished_at=? WHERE workflow_id=? AND status='pending'", (now, workflow_id))
            self._supersede_pending_advances(connection, workflow_id, "business_termination", now)

    def complete_final_wait(self, workflow_id: str, gate_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            gate = connection.execute("SELECT * FROM workflow_advance_gates WHERE workflow_id=? AND gate_id=? AND status='finishing'", (workflow_id, gate_id)).fetchone()
            workflow = connection.execute("SELECT status FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not gate or not workflow or workflow[0] != "running":
                return
            self._require_discussion_idle(connection, workflow_id)
            if connection.execute("SELECT 1 FROM workflow_nodes WHERE workflow_id=? AND status NOT IN ('completed','skipped')", (workflow_id,)).fetchone():
                raise RuntimeError("步骤尚未全部完成，不能结束任务。")
            summary = connection.execute("SELECT response FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (workflow_id, gate["completed_node_id"])).fetchone()[0]
            connection.execute("UPDATE workflow_advance_gates SET status='confirmed',updated_at=? WHERE gate_id=?", (now, gate_id))
            connection.execute("UPDATE workflows SET status='completed',supervisor_status='completed',response=?,finished_at=? WHERE workflow_id=?", (summary, now, workflow_id))
            self._release_supervisor_lease_with_connection(connection, workflow_id, "workflow_completed", now)
            self._add_event_with_connection(connection, workflow_id, None, "gateway", "workflow.completed", {"status": "completed", "response": summary}, now)
