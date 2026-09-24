"""Serial acceptance gates. All decisions are committed in the central runtime database."""

import hashlib
import mimetypes
import json
import uuid
import sqlite3
from typing import Any
from datetime import datetime, timezone


RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["passed", "failed", "needs_input"]},
        "reason": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "reason", "issues"],
}


def normalize_acceptance(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("验收配置必须是对象。")
    name, criteria = value.get("name"), value.get("criteria")
    limit = value.get("maxRepairs", 2)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise ValueError("判断名称必须为 1 到 200 个字符。")
    if not isinstance(criteria, str) or not 1 <= len(criteria.strip()) <= 10000:
        raise ValueError("验收条件必须为 1 到 10000 个字符。")
    if type(limit) is not int or not 0 <= limit <= 10:
        raise ValueError("自动修复次数必须为 0 到 10 的整数。")
    return {"name": name.strip(), "criteria": criteria.strip(), "maxRepairs": limit}


def parse_result(response: str) -> dict[str, Any]:
    if not isinstance(response, str) or len(response) > 20000:
        raise ValueError("验收结论无效或超过容量限制。")
    value = json.loads(response)
    if (not isinstance(value, dict) or set(value) != {"decision", "reason", "issues"}
            or value["decision"] not in {"passed", "failed", "needs_input"}
            or not isinstance(value["reason"], str) or not value["reason"].strip()
            or len(value["reason"]) > 4000 or not isinstance(value["issues"], list)
            or len(value["issues"]) > 50
            or any(not isinstance(x, str) or len(x) > 2000 for x in value["issues"])):
        raise ValueError("验收结论格式无效。")
    return value


class AcceptanceStore:
    def hold_acceptance_after_disconnect(self, c, workflow_id):
        rows = c.execute("SELECT * FROM workflow_acceptance WHERE workflow_id=? AND state NOT IN ('initial','passed')",
                         (workflow_id,)).fetchall()
        if not rows:
            return False
        for row in rows:
            if row["state"] == "held":
                continue
            reason = "执行连接已中断，请核对原会话后补充要求并确认修复；不会自动重复执行。"
            c.execute("UPDATE workflow_acceptance SET state='held',reason=? WHERE workflow_id=? AND node_id=?",
                      (reason, workflow_id, row["node_id"]))
            c.execute("UPDATE workflow_nodes SET status='acceptance_held' WHERE workflow_id=? AND node_id=?",
                      (workflow_id, row["node_id"]))
            self._acceptance_event(c, workflow_id, row["node_id"], "held",
                                   {"reason": reason, "pauseId": uuid.uuid4().hex, "repairs": row["repairs"]})
        c.execute("UPDATE workflows SET status='running',supervisor_status='interrupted',finished_at=NULL WHERE workflow_id=?",
                  (workflow_id,))
        c.execute("DELETE FROM supervisor_leases WHERE workflow_id=?", (workflow_id,))
        return True

    def initialize_acceptance(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS workflow_acceptance (
            workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, config_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'initial', repairs INTEGER NOT NULL DEFAULT 0,
            manual INTEGER NOT NULL DEFAULT 0, token TEXT, job_id TEXT, thread_id TEXT,
            turn_id TEXT, reason TEXT, instruction TEXT, history_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(workflow_id,node_id),
            FOREIGN KEY(workflow_id,node_id) REFERENCES workflow_nodes(workflow_id,node_id))""")

    def acceptance_snapshot(self, workflow_id: str, node_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM workflow_acceptance WHERE workflow_id=? AND node_id=?",
                            (workflow_id, node_id)).fetchone()
        if row is None:
            return None
        return {"config": json.loads(row["config_json"]), "state": row["state"],
                "repairs": row["repairs"], "manual": bool(row["manual"]),
                "reason": row["reason"], "history": [{k: h.get(k) for k in ("stage", "result", "reason", "at")} for h in json.loads(row["history_json"])]}

    def _acceptance_event(self, c, workflow_id, node_id, state, payload):
        now = datetime.now(timezone.utc).isoformat()
        c.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
        self._add_event_with_connection(c, workflow_id, node_id, "supervisor",
                                        "acceptance." + state, {"nodeId": node_id, **payload}, now)

    def _acceptance_intercept(self, c, workflow_id, node_id, snapshot):
        row = c.execute("SELECT * FROM workflow_acceptance WHERE workflow_id=? AND node_id=?",
                        (workflow_id, node_id)).fetchone()
        if row is None:
            return snapshot
        if row["state"] != "initial":
            return None  # A stale business job must never overwrite an acceptance stage.
        current = c.execute("SELECT status,job_id FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (workflow_id,node_id)).fetchone()
        if current is None or current["status"] == "pending" or (snapshot.get("job_id") and current["job_id"] and snapshot["job_id"] != current["job_id"]):
            return None
        if snapshot.get("status") != "completed":
            return snapshot
        state = "check_pending" if snapshot.get("thread_id") else "held"
        reason = None if state != "held" else "原执行会话不可用，请人工处理。"
        c.execute("UPDATE workflow_acceptance SET state=?,thread_id=?,reason=? WHERE workflow_id=? AND node_id=?",
                  (state, snapshot.get("thread_id"), reason, workflow_id, node_id))
        self._acceptance_event(c, workflow_id, node_id, state, {"reason": reason})
        return {**snapshot, "status": "acceptance_held" if state == "held" else "running", "finished_at": None}

    def acceptance_operation(self, workflow_id, node_id, operation, payload,
                             *, sidecar_supervisor_id=None, lease_token=None):
        if operation not in {"claim", "attach", "finish", "hold"} or not isinstance(payload, dict):
            raise ValueError("无效的验收操作。")
        if operation in {"attach", "finish"}:
            token = payload.get("token")
            if not isinstance(token, str) or not 1 <= len(token) <= 128:
                raise ValueError("验收轮次标识无效。")
            for key in ("jobId", "turnId"):
                value = payload.get(key)
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 512):
                    raise ValueError("验收执行标识无效。")
            if operation == "attach" and not payload.get("jobId"):
                raise ValueError("缺少验收执行标识。")
            if operation == "finish" and payload.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("验收执行结果状态无效。")
        if operation == "claim":
            from agent_registry import AgentRegistry
            spec = self.get_spec(workflow_id)
            target = next((n for n in spec["nodes"] if n["id"] == node_id), None)
            if target is None:
                raise ValueError("找不到验收步骤。")
            AgentRegistry(self).validate(spec["supervisorAgentId"], [target["agentId"]], require_test=True)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if sidecar_supervisor_id is not None:
                self._validate_sidecar_access_with_connection(c, sidecar_supervisor_id, workflow_id,
                                                              lease_token=lease_token, require_lease=True)
            row = c.execute("SELECT * FROM workflow_acceptance WHERE workflow_id=? AND node_id=?",
                            (workflow_id, node_id)).fetchone()
            node = c.execute("SELECT * FROM workflow_nodes WHERE workflow_id=? AND node_id=?",
                             (workflow_id, node_id)).fetchone()
            workflow = c.execute("SELECT status,handoff_mode FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None or node is None:
                raise ValueError("该步骤没有验收关卡。")
            if workflow["status"] not in {"running", "queued"}:
                raise ValueError("任务已结束，不能推进验收。")
            config = json.loads(row["config_json"])
            if operation == "hold":
                if row["state"] not in {"check_pending", "repair_pending"}:
                    return None
                reason = "执行机暂不可用或权限已变化，请处理后再确认修复。"
                c.execute("UPDATE workflow_acceptance SET state='held',reason=? WHERE workflow_id=? AND node_id=?", (reason,workflow_id,node_id))
                c.execute("UPDATE workflow_nodes SET status='acceptance_held' WHERE workflow_id=? AND node_id=?", (workflow_id,node_id))
                self._acceptance_event(c,workflow_id,node_id,"held", {"reason":reason,"pauseId":uuid.uuid4().hex,"repairs":row["repairs"]})
                return None
            if operation == "claim":
                if row["state"] not in {"check_pending", "repair_pending"}:
                    return None
                from mcp_store import ensure_available
                ensure_available(c, [spec["supervisorAgentId"], node["agent_id"]])
                stage = "checking" if row["state"] == "check_pending" else "repairing"
                runtime = c.execute("SELECT cwd,model FROM workflow_node_runtime WHERE workflow_id=? AND node_id=? ORDER BY attempt_number DESC LIMIT 1", (workflow_id,node_id)).fetchone()
                token = uuid.uuid4().hex
                c.execute("UPDATE workflow_acceptance SET state=?,token=?,job_id=NULL,turn_id=NULL "
                          "WHERE workflow_id=? AND node_id=?", (stage, token, workflow_id, node_id))
                self._acceptance_event(c, workflow_id, node_id, stage, {"repairs": row["repairs"]})
                prompt = ("请基于本会话需求、补充要求、预期输出及实际产物进行只读验收，不得修复或修改产物。"
                          "本关卡只能修复本步骤负责的产物；如果问题属于更早步骤、需要跨步骤返工，返回 needs_input。"
                          "原因和问题清单使用普通中文，不输出内部地址、会话编号或凭据。返回规定的结构化结论；缺少证据或需要业务决策时返回 needs_input。\n验收条件：\n" + config["criteria"]
                          if stage == "checking" else
                          "本轮已获授权，请修复上一版产物的问题，只交付一个修复版本，然后停止，等待独立复检。"
                          "仅修复本步骤负责的产物；属于更早步骤的问题不得代为修改，应说明需要人工处理。"
                          "不得放宽原需求或验收条件。\n验收条件：\n" + config["criteria"]
                          + "\n上次问题：\n" + (row["reason"] or "")
                          + "\n补充要求：\n" + (row["instruction"] or "无"))
                return {"token": token, "stage": stage, "threadId": row["thread_id"], "prompt": prompt,
                        "handoffMode": workflow["handoff_mode"], "stepNumber": node["position"] + 1,
                        "agentId": node["agent_id"], "cwd": runtime["cwd"] if runtime and runtime["cwd"] else node["cwd"], "model": runtime["model"] if runtime and runtime["model"] else node["model"],
                        "permissionProfile": node["permission_profile"], "write": bool(node["write_enabled"]),
                        "timeoutSec": node["timeout_sec"]}
            if payload.get("token") != row["token"] or row["state"] not in {"checking", "repairing"}:
                return None
            if operation == "attach":
                c.execute("UPDATE workflow_acceptance SET job_id=?,turn_id=? WHERE workflow_id=? AND node_id=?",
                          (payload.get("jobId"), payload.get("turnId"), workflow_id, node_id))
                return None
            if operation != "finish":
                raise ValueError("不支持的验收操作。")
            state, reason = "held", "验收执行异常，需核对原会话后人工处理。"
            history = json.loads(row["history_json"])
            result = None
            if payload.get("status") == "completed":
                if row["state"] == "repairing":
                    state, reason = "check_pending", None
                    response = payload.get("response")
                    if not isinstance(response, str) or len(response) > 20000:
                        raise ValueError("修复结果无效或超过容量限制。")
                    if "artifacts" in payload:
                        self._replace_acceptance_artifacts(c, workflow_id, node_id, node, payload["artifacts"])
                    c.execute("UPDATE workflow_nodes SET response=?,job_id=COALESCE(?,job_id),turn_id=COALESCE(?,turn_id) WHERE workflow_id=? AND node_id=?",
                              (response, row["job_id"], payload.get("turnId"), workflow_id, node_id))
                else:
                    try:
                        result = parse_result(payload.get("response"))
                        reason = result["reason"] + "\n" + "\n".join(result["issues"])
                        if result["decision"] == "passed":
                            state = "passed"
                        elif result["decision"] == "failed" and not row["manual"] and row["repairs"] < config["maxRepairs"]:
                            state = "repair_pending"
                            c.execute("UPDATE workflow_acceptance SET repairs=repairs+1 WHERE workflow_id=? AND node_id=?",
                                      (workflow_id, node_id))
                    except (TypeError, ValueError):
                        reason = "验收结论格式无效，请人工处理。"
            history.append({"businessResponse": node["response"] if row["state"] == "repairing" else None,
                            "jobId": row["job_id"], "turnId": payload.get("turnId") or row["turn_id"],
                            "attemptNumber": node["attempt_count"], "stage": row["state"], "token": row["token"], "result": result,
                            "reason": reason, "at": datetime.now(timezone.utc).isoformat()})
            c.execute("UPDATE workflow_acceptance SET state=?,reason=?,history_json=?,turn_id=? WHERE workflow_id=? AND node_id=?",
                      (state, reason, json.dumps(history, ensure_ascii=False), payload.get("turnId"), workflow_id, node_id))
            self._acceptance_event(c, workflow_id, node_id, state,
                                   {"reason": reason, "pauseId": row["token"], "repairs": row["repairs"]})
            if state == "held":
                c.execute("UPDATE workflow_nodes SET status='acceptance_held' WHERE workflow_id=? AND node_id=?",
                          (workflow_id, node_id))
            if state == "passed":
                c.execute("UPDATE workflow_nodes SET status='completed',finished_at=? WHERE workflow_id=? AND node_id=?",
                          (datetime.now(timezone.utc).isoformat(), workflow_id, node_id))
                self._create_advance_gate_with_connection(c, workflow_id, node_id)
        return self.acceptance_snapshot(workflow_id, node_id)

    def resume_acceptance(self, workflow_id: str, node_id: str, action_id: str) -> None:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            action = c.execute("SELECT * FROM workflow_control_actions WHERE action_id=? AND workflow_id=?",
                               (action_id, workflow_id)).fetchone()
            if action is None or action["action_type"] != "repair_acceptance" or action["node_id"] != node_id:
                raise ValueError("验收修复需要独立确认。")
            if action["retry_ordinal"] is not None:
                return
            if action["status"] != "executing" or not action["revision_instruction"]:
                raise ValueError("请先补充修复要求并确认执行。")
            row = c.execute("SELECT * FROM workflow_acceptance WHERE workflow_id=? AND node_id=?",
                            (workflow_id, node_id)).fetchone()
            if row is None or row["state"] != "held":
                raise ValueError("当前步骤没有等待人工处理的验收。")
            workflow = c.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow["status"] not in {"running", "queued"}:
                raise ValueError("任务已结束。")
            ordinal = workflow["used_retry_count"] + 1
            if ordinal > workflow["max_retry_count"]:
                raise ValueError("本任务的重跑次数已经用完。")
            c.execute("UPDATE workflow_acceptance SET state='repair_pending',manual=1,instruction=?,repairs=repairs+1 "
                      "WHERE workflow_id=? AND node_id=?", (action["revision_instruction"], workflow_id, node_id))
            c.execute("UPDATE workflow_nodes SET status='running' WHERE workflow_id=? AND node_id=?", (workflow_id, node_id))
            c.execute("UPDATE workflows SET used_retry_count=?,status=CASE WHEN EXISTS "
                      "(SELECT 1 FROM supervisor_leases WHERE workflow_id=?) THEN 'running' ELSE 'queued' END "
                      "WHERE workflow_id=?", (ordinal, workflow_id, workflow_id))
            c.execute("UPDATE workflow_control_actions SET retry_ordinal=? WHERE action_id=?", (ordinal, action_id))
            self._acceptance_event(c, workflow_id, node_id, "repair_requested", {"retryOrdinal": ordinal})

    def _replace_acceptance_artifacts(self, c, workflow_id, node_id, node, files):
        if not isinstance(files, list) or len(files) > 1:
            raise ValueError("修复最多交付一个文件。")
        for value in files:
            if not isinstance(value.get("content"), bytes) or len(value["content"]) > 20000000:
                raise ValueError("修复文件无效或过大。")
            self._safe_artifact_filename(value["filename"])
        now = datetime.now(timezone.utc).isoformat()
        attempt = node["attempt_count"]
        c.execute("INSERT OR REPLACE INTO workflow_node_attempts "
                  "(workflow_id,node_id,attempt_number,status,job_id,thread_id,turn_id,response,error,actual_prompt,started_at,finished_at,archived_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (workflow_id,node_id,attempt,node["status"],node["job_id"],node["thread_id"],node["turn_id"],
                   node["response"],node["error"],node["actual_prompt"],node["started_at"],node["finished_at"],now))
        c.execute("INSERT OR REPLACE INTO workflow_attempt_artifacts "
                  "SELECT artifact_id,workflow_id,node_id,?,source_item_id,media_type,filename,content,byte_size,sha256,created_at,? "
                  "FROM workflow_artifacts WHERE workflow_id=? AND node_id=?", (attempt,now,workflow_id,node_id))
        c.execute("DELETE FROM workflow_artifacts WHERE workflow_id=? AND node_id=?", (workflow_id,node_id))
        c.execute("UPDATE workflow_nodes SET attempt_count=attempt_count+1 WHERE workflow_id=? AND node_id=?", (workflow_id,node_id))
        for value in files:
            count = c.execute("SELECT (SELECT COUNT(*) FROM workflow_artifacts WHERE workflow_id=?) + "
                              "(SELECT COUNT(*) FROM workflow_attempt_artifacts WHERE workflow_id=?) + "
                              "(SELECT COUNT(*) FROM workflow_input_images WHERE workflow_id=?)", (workflow_id,workflow_id,workflow_id)).fetchone()[0]
            if count >= 50:
                raise ValueError("单个工作流最多保存 50 个文件。")
            content, filename = value["content"], self._safe_artifact_filename(value["filename"])
            c.execute("INSERT INTO workflow_artifacts(artifact_id,workflow_id,node_id,source_item_id,media_type,filename,content,byte_size,sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex,workflow_id,node_id,uuid.uuid4().hex,mimetypes.guess_type(filename)[0] or "application/octet-stream",filename,content,len(content),hashlib.sha256(content).hexdigest(),now))
