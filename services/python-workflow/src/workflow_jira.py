"""Jira 领取台账。运行上下文不携带 Token。"""

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
    if not isinstance(value, dict) or set(value) not in ({"jql", "account"}, {"jql", "connectionId"}):
        raise ValueError("Jira 配置必须包含查询条件和中央账号引用。")
    if "connectionId" in value:
        import uuid
        try:
            uuid.UUID(value["connectionId"])
        except (ValueError, TypeError, AttributeError):
            raise ValueError("请选择中央 Jira 账号。") from None
    for key, limit in (("jql", 4000), ("account", 128)):
        if key not in value:
            continue
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
        WHERE a.executor_id=? AND j.issue_key IS NOT NULL AND j.credentials_cleared=0 AND j.account_source='transferred' LIMIT 1""", (agent_id,)).fetchone() is not None


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
    delivery_columns = {row[1] for row in db.execute("PRAGMA table_info(workflow_jira_deliveries)")}
    for column in ("submission_type", "commits_json"):
        if column not in delivery_columns:
            db.execute(f"ALTER TABLE workflow_jira_deliveries ADD COLUMN {column} TEXT")
    columns = {row[1] for row in db.execute("PRAGMA table_info(workflow_jira_runs)")}
    if "delivery_version" not in columns:
        db.execute("ALTER TABLE workflow_jira_runs ADD COLUMN delivery_version INTEGER NOT NULL DEFAULT 1")
    if "account_source" not in columns:
        db.execute("ALTER TABLE workflow_jira_runs ADD COLUMN account_source TEXT NOT NULL DEFAULT 'transferred'")
    if "cleanup_attempted_at" not in columns:
        db.execute("ALTER TABLE workflow_jira_runs ADD COLUMN cleanup_attempted_at TEXT")
    from jira_history_review import initialize as initialize_history_reviews
    initialize_history_reviews(db)


def node_instruction(db, workflow_id):
    row = db.execute("SELECT * FROM workflow_jira_runs WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        return ""
    if row["state"] != "ready":
        raise RuntimeError("监督机尚未领取工单并完成入口准备，不能启动步骤。")
    version = delivery_version(db, workflow_id)
    extra = ("\n交付回执额外返回 submissionType 和 commits。仅本轮实际提交并回查确认后，"
             "submissionType 为 full（必要验证通过）或 limited（符合职责的验证受限提交条件），"
             "commits 为 [{repository:仓库业务名称, revision:实际提交编号}]，支持多个仓库及编号。"
             "受限原因写入 summary。未提交时 submissionType 为 null、commits 为 []。"
             "仓库仅用客户端、服务端等业务名称，不填地址或凭据；不得把更新版本、历史提交或交付链接当成本轮提交编号。"
             "阻断不得提交；分类不改变职责的提交准入标准。\n" if version >= 2 else "")
    if version >= 3:
        extra += ("交付状态增加 completed_no_submission（明确无需新增提交）和 rework_required（审核不通过，已交返工）。"
                  "只有核实当前无待提交业务改动、无需新增提交且已完成职责要求的必要交接，才能使用 completed_no_submission；"
                  "summary 必须说明依据及仍需人工补验的范围，不代表修复验收通过。"
                  "仅修复步骤结束、代码留待后续审核或提交，应继续使用 completed，不得提前声明无需提交。"
                  "审核明确不通过且返工交接已完成时使用 rework_required，并在 summary 写明未通过原因；"
                  "不得将审核失败当作验证受限通过，也不得为获得分类而改变 Jira 流转或提交准入标准。"
                  "这两类回执的步骤 outcome 为 success，表示本步骤交接完成，不改变依赖执行及人工确认规则；"
                  "提交失败、要求提交却未提交、交接失败或结果不明须如实按原规则报告，不能使用这两类。\n")
    extra += "流程完成与提交分类独立。普通 completed 回执不会阻止流程计为通过；无需为获得通过分类额外声明无需提交。实际要求的操作失败仍必须如实报告。\n"
    return extra + ("\n【本轮 Jira 任务，优先于角色中自行领取任务的要求】\n"
            f"本轮唯一工单：{row['issue_key']}。所有步骤只处理此工单，不重新搜索或领取其他工单。\n"
            f"通过 PowerShell 调用本轮专属入口 {json.dumps(row['launcher_path'], ensure_ascii=False)}，"
            f"参数与 jira_cli 相同，例如 issue get {row['issue_key']}。"
            "入口负责选择本轮固定账号，读取、评论和修改都直接调用此入口；"
            "不要自行添加 --user、用户名、密码或令牌，不绕过入口直接调用 jira_cli。"
            "不得读取、打印或更改账号配置，不使用其他已保存账号。"
            "若入口仍要求选择账号，报告平台入口异常，不自行换账号。"
            "先实际读取工单，再按职责进行具体修改；领取记录不代表 Jira 已被修改。"
            "历史完成的工单只要仍符合查询条件就可能再次领取。"
            "SOP 步骤之间由平台按依赖关系推进，不要求转交 Jira 负责人，不因缺少下一步骤的接收账号而阻断。"
            "仅在当前步骤职责明确要求时转交 Jira 负责人，并回查实际负责人是否符合职责指定的接收人；"
            "接收人可以与本轮领取账号相同。转交未完成或结果无法确认时如实说明，按职责处理，不擅自选择接收人或虚报交接成功。"
            "不得把工作流完成当成代码提交成功。最终 jiraDelivery.status 使用 submitted（已有提交凭证）、"
            "completed（本步完成但未提交）、not_submitted（本步要求提交代码但明确未提交）、blocked（受阻）、unknown（提交结果不明）或 not_applicable。"
            "reference 填实际确认的提交号或交付链接；submitted 必须实际回查并有凭证，不能只凭已发出提交命令。summary 简述交付结果。\n")


DELIVERY_FIELDS = {
    "submissionType": {"type": ["string", "null"], "enum": ["full", "limited", None]},
    "commits": {"type": "array", "maxItems": 100, "items": {
        "type": "object", "additionalProperties": False, "required": ["repository", "revision"],
        "properties": {"repository": {"type": "string", "minLength": 1, "maxLength": 160},
                       "revision": {"type": "string", "minLength": 1, "maxLength": 160}}}},
}


DELIVERY_STATUSES = ("submitted", "completed", "not_submitted", "blocked", "unknown", "not_applicable")
COMPLETION_STATUSES = ("completed_no_submission", "rework_required")


def validate_delivery(value, version=1):
    if not isinstance(value, dict) or set(value) != ({"status", "reference", "summary"} | (set(DELIVERY_FIELDS) if version >= 2 else set())):
        raise ValueError("Jira 交付结果格式无效。")
    if not isinstance(value["status"], str) or value["status"] not in DELIVERY_STATUSES + (COMPLETION_STATUSES if version >= 3 else ()):
        raise ValueError("Jira 交付状态无效。")
    for key, limit in (("reference", 1000), ("summary", 2000)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise ValueError("Jira 交付结果文本无效。")
    if value["status"] in COMPLETION_STATUSES and not value["summary"].strip():
        raise ValueError("无需提交或审核返工必须说明具体依据。")
    if value["status"] == "submitted" and not value["reference"].strip():
        raise ValueError("已提交必须提供实际提交凭证。")
    if version >= 2:
        kind, commits = value["submissionType"], value["commits"]
        if not isinstance(commits, list) or len(commits) > 100:
            raise ValueError("提交清单格式无效。")
        if value["status"] == "submitted":
            if kind not in ("full", "limited") or not commits:
                raise ValueError("已提交必须提供验证类型和实际提交编号。")
        elif kind is not None or commits:
            raise ValueError("未提交回执不能填写提交类型或编号。")
        for commit in commits:
            if not isinstance(commit, dict) or set(commit) != {"repository", "revision"}:
                raise ValueError("提交编号格式无效。")
            for text in commit.values():
                if not isinstance(text, str) or not text.strip() or len(text) > 160 or any(ord(c) < 32 for c in text):
                    raise ValueError("仓库名称或提交编号无效。")
    return value


def delivery_version(db, workflow_id):
    return db.execute("SELECT delivery_version FROM workflow_jira_runs WHERE workflow_id=?", (workflow_id,)).fetchone()[0]


def _manually_cancelled(db, workflow_id):
    import zlib
    events = db.execute("SELECT payload_json,payload_zlib FROM workflow_events WHERE workflow_id=? AND event_type='workflow.cancelled'", (workflow_id,))
    return any(json.loads(zlib.decompress(e['payload_zlib']) if e['payload_zlib'] else e['payload_json']).get('reason') in ('user_requested', 'chat_control') for e in events)


def execution_result(db, workflow_id):
    """流程完成与提交分类独立；保留旧交付分类供历史接口兼容。"""
    result = _delivery_result(db, workflow_id)
    if result is None:
        return None
    row = db.execute("SELECT status,termination_json FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    termination = json.loads(row["termination_json"] or "{}")
    flow = None
    if row["status"] in ("completed", "failed", "cancelled"):
        if termination.get("outcome") == "no_task":
            flow = "no_task"
        elif row["status"] == "cancelled":
            flow = "cancelled" if _manually_cancelled(db, workflow_id) else "blocked"
        elif row["status"] == "completed" and not termination.get("outcome"):
            flow = "passed"
        else:
            flow = "blocked"
    submission = {"fully_submitted": "full", "limited_submitted": "limited",
                  "completed_no_submission": "none"}.get(result["status"], "unknown")
    if result['status'] == 'rework_required' and result.get('source') == 'manual_review' and not result['commits']:
        submission = 'none'
    if result['commits'] and result['recorded'] and submission == 'unknown':
        submitted = db.execute("SELECT submission_type FROM workflow_jira_deliveries WHERE workflow_id=? AND status='submitted'", (workflow_id,)).fetchall()
        unresolved = db.execute("SELECT 1 FROM workflow_jira_deliveries d WHERE workflow_id=? AND status='unknown' AND attempt=(SELECT MAX(x.attempt) FROM workflow_jira_deliveries x WHERE x.workflow_id=d.workflow_id AND x.node_id=d.node_id) LIMIT 1", (workflow_id,)).fetchone()
        if submitted and not unresolved and all(r[0] in ('full', 'limited') for r in submitted):
            submission = 'limited' if any(r[0] == 'limited' for r in submitted) else 'full'
    if not result["commits"] and result["recorded"]:
        receipts = db.execute("SELECT node_id,attempt,status FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY attempt", (workflow_id,)).fetchall()
        latest = {r["node_id"]: r["status"] for r in receipts}
        nodes = {r[0] for r in db.execute("SELECT node_id FROM workflow_nodes WHERE workflow_id=?", (workflow_id,))}
        if nodes and nodes == set(latest) and set(latest.values()) <= {"completed", "completed_no_submission", "rework_required", "not_applicable", "not_submitted", "blocked"}:
            submission = "none"
    return {**result, "flowStatus": flow, "submissionStatus": submission}


def _delivery_result(db, workflow_id):
    from workflow_monitor import public_text
    row = db.execute("SELECT w.status,w.termination_json,j.delivery_version FROM workflows w JOIN workflow_jira_runs j USING(workflow_id) WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        return None
    if row['delivery_version'] == 1 or row['status'] == 'completed':
        review = db.execute('SELECT * FROM workflow_jira_history_reviews WHERE workflow_id=?', (workflow_id,)).fetchone()
        if review is not None and (row['delivery_version'] == 1 or review['status'] in COMPLETION_STATUSES):
            return {'status': review['status'],
                    'commits': [{k: public_text(v) for k, v in c.items()} for c in json.loads(review['commits_json'])],
                    'recorded': True, 'source': 'manual_review' if review['status'] in COMPLETION_STATUSES else 'historical_review',
                    'note': public_text(review['note']), 'reviewedAt': review['reviewed_at'],
                    'developers': [public_text(v) for v in json.loads(review['developers_json'])],
                    'developerSource': review['developer_source']}
    deliveries = db.execute("SELECT node_id,attempt,status,submission_type,commits_json,summary FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY recorded_at,node_id,attempt", (workflow_id,)).fetchall()
    commits, seen = [], set()
    for d in deliveries:
        for commit in json.loads(d['commits_json'] or '[]'):
            key = (commit['repository'], commit['revision'])
            if key not in seen:
                seen.add(key)
                commits.append({k: public_text(v) for k,v in commit.items()})
    latest = {}
    for d in deliveries:
        if d['node_id'] not in latest or d['attempt'] >= latest[d['node_id']]['attempt']:
            latest[d['node_id']] = d
    unresolved = any(d['status'] == 'unknown' for d in latest.values())
    status = None
    termination = json.loads(row['termination_json'] or '{}')
    if row['delivery_version'] >= 2 and row['status'] in ('completed', 'failed') and termination.get('outcome') != 'no_task':
        submitted = [d for d in deliveries if d['status'] == 'submitted']
        if termination.get('outcome') == 'blocked' and not submitted and not unresolved:
            status = 'blocked'
        elif row['status'] == 'completed' and submitted and commits and not unresolved:
            status = 'limited_submitted' if any(d['submission_type'] == 'limited' for d in submitted) else 'fully_submitted'
        else:
            status = 'abnormal'
    note = None
    if row['delivery_version'] >= 3 and row['status'] == 'completed' and not termination.get('outcome'):
        latest_statuses = {d['status'] for d in latest.values()}
        if 'unknown' in latest_statuses:
            status = 'abnormal'
        elif 'rework_required' in latest_statuses:
            status = 'rework_required'
            note = '；'.join(d['summary'] for d in latest.values() if d['status'] == 'rework_required')
        elif latest_statuses & {'not_submitted', 'blocked'}:
            status = 'abnormal'
        elif not submitted and not commits:
            nodes = db.execute('SELECT node_id,depends_on_json FROM workflow_nodes WHERE workflow_id=?', (workflow_id,)).fetchall()
            node_ids = {n['node_id'] for n in nodes}
            dependencies = {dep for n in nodes for dep in json.loads(n['depends_on_json'])}
            leaves = node_ids - dependencies
            if (leaves and node_ids == set(latest)
                    and latest_statuses <= {'completed', 'not_applicable', 'completed_no_submission'}
                    and all(latest[n]['status'] == 'completed_no_submission' for n in leaves)):
                status = 'completed_no_submission'
                note = '；'.join(latest[n]['summary'] for n in sorted(leaves))
    if row['delivery_version'] >= 2 and row['status'] == 'cancelled':
        if not _manually_cancelled(db, workflow_id):
            status = 'abnormal'
    return {'status': status, 'commits': commits, 'recorded': row['delivery_version'] >= 2,
            **({'note': public_text(note)} if note else {})}


class JiraStore:
    def jira_cleanup_attempted(self, workflow_id):
        from workflow_store import utc_now
        with self._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET cleanup_attempted_at=? WHERE workflow_id=?", (utc_now(), workflow_id))

    def jira_cleanup_candidates(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("""SELECT j.workflow_id,a.executor_id,j.launcher_path FROM workflow_jira_runs j
                JOIN workflows w USING(workflow_id) JOIN workflow_allocations a USING(workflow_id)
                WHERE j.credentials_cleared=0 AND j.account_source='transferred' AND j.issue_key IS NOT NULL AND w.status IN ('completed','failed','cancelled')
                AND NOT EXISTS (SELECT 1 FROM workflow_machine_claims c WHERE c.workflow_id=j.workflow_id)
                ORDER BY COALESCE(j.cleanup_attempted_at,''),w.finished_at,j.workflow_id LIMIT 20""")]

    def jira_credentials_cleared(self, workflow_id):
        with self._connect() as db:
            db.execute("UPDATE workflow_jira_runs SET credentials_cleared=1 WHERE workflow_id=?", (workflow_id,))

    def jira_context(self, workflow_id):
        spec = self.get_spec(workflow_id)
        if not spec.get("jira"):
            raise ValueError("本运行未启用监督机统一领取 Jira。")
        binding = spec.get("jiraReservation")
        with self._connect() as db:
            row = db.execute("SELECT * FROM workflow_jira_runs WHERE workflow_id=?", (workflow_id,)).fetchone()
            allocation = db.execute("SELECT * FROM workflow_allocations WHERE workflow_id=?", (workflow_id,)).fetchone()
            if not allocation or not allocation["executor_id"]:
                raise RuntimeError("尚未分配监督机和执行机。")
            return {"reservation": binding, "config": spec["jira"], "supervisorId": allocation["supervisor_id"],
                    "executorId": allocation["executor_id"], "cwd": allocation["cwd"],
                    "state": row["state"], "issueKey": row["issue_key"],
                    "server": row["server_key"], "account": row["account_alias"],
                    "fingerprint": row["credential_fingerprint"], "launcherPath": row["launcher_path"],
                    "accountSource": row["account_source"]}

    def jira_update(self, workflow_id, operation, payload, *, sidecar_supervisor_id=None, lease_token=None, central_verified=False):
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
                if spec.get("jira", {}).get("connectionId") and row["state"] == "waiting_claim" and not central_verified:
                    raise ValueError("工单必须由中央网关完成启动前核验。")
                server = server_key(payload.get("server"))
                account, fingerprint = payload.get("account"), payload.get("fingerprint")
                if (not isinstance(account, str) or not 1 <= len(account) <= 128
                        or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                    raise ValueError("Jira 账号标识无效。")
                candidates = payload.get("candidates")
                if not isinstance(candidates, list) or len(candidates) > 1000 or any(not isinstance(k, str) or not ISSUE_KEY.fullmatch(k) for k in candidates):
                    raise ValueError("Jira 候选编号格式无效或超过 1000 项。")
                binding = spec.get("jiraReservation")
                if binding and (server != binding["server"] or candidates != [binding["issueKey"]]):
                    raise ValueError("本轮只能领取已预约的工单。")
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
                    if db.execute("SELECT 1 FROM jira_pool_items WHERE server_key=? AND issue_key=? AND state='reserved'", (server, key)).fetchone():
                        continue
                    previous = db.execute("SELECT w.status,c.workflow_id FROM jira_issue_claims c JOIN workflows w USING(workflow_id) WHERE c.server_key=? AND c.issue_key=?", (server, key)).fetchone()
                    if previous and previous["workflow_id"] != workflow_id:
                        if previous["status"] not in {"completed", "failed", "cancelled"} or db.execute("SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?", (previous["workflow_id"],)).fetchone():
                            continue
                        db.execute("DELETE FROM jira_issue_claims WHERE server_key=? AND issue_key=?", (server, key))
                    if not previous or previous["workflow_id"] != workflow_id:
                        db.execute("INSERT INTO jira_issue_claims VALUES(?,?,?)", (server, key, workflow_id))
                    selected = key
                    break
                if selected is None and payload["candidates"]:
                    # 当前查询页全部已领取；监督机应排除该页继续查询，而不是提前报告无任务。
                    return {"issueKey": None, "state": "contended"}
                if selected:
                    db.execute("UPDATE jira_pool_items SET state='consumed' WHERE server_key=? AND issue_key=? AND state='pending'", (server, selected))
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
                    source = payload.get("accountSource", "transferred")
                    if not isinstance(source, str) or source not in {"local", "transferred"}:
                        raise ValueError("Jira 账号来源无效。")
                    if row["launcher_path"] and row["account_source"] != source:
                        raise RuntimeError("本轮 Jira 账号来源已固定，不能修改。")
                    db.execute("UPDATE workflow_jira_runs SET launcher_path=?,account_source=? WHERE workflow_id=?", (path, source, workflow_id))
                    return {"issueKey": row["issue_key"], "state": row["state"]}
                db.execute("UPDATE workflow_jira_runs SET state='ready',launcher_path=? WHERE workflow_id=?", (path, workflow_id))
                result = {"issueKey": row["issue_key"], "state": "ready"}
            else:
                raise ValueError("不支持的 Jira 操作。")
            db.execute("UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?", (workflow_id,))
            self._add_event_with_connection(db, workflow_id, None, "supervisor", "jira." + result["state"], result, now)
            return result

    def jira_runs(self, group_id, *, before=None, limit=50, sop_id=""):
        if sop_id:
            import uuid
            uuid.UUID(sop_id)
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
                AND (?='' OR EXISTS (SELECT 1 FROM workflow_sop_context s WHERE s.workflow_id=j.workflow_id AND s.sop_id=?))
                ORDER BY j.rowid DESC LIMIT ?""", (group_id, group_id, before, before, sop_id, sop_id, limit + 1)).fetchall()
            result = []
            for row in rows[:limit]:
                deliveries = [dict(r) for r in db.execute("SELECT node_id AS nodeId,attempt,status,reference,summary,recorded_at AS recordedAt FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY recorded_at,node_id,attempt", (row["workflow_id"],))]
                waiting = db.execute("SELECT kind FROM workflow_advance_gates WHERE workflow_id=? AND status IN ('pending','held') ORDER BY created_at DESC LIMIT 1", (row["workflow_id"],)).fetchone()
                termination = json.loads(row["termination_json"]) if row["termination_json"] else None
                from jira_pool import rework_history, execution_history
                reworks = rework_history(db, row["server_key"], row["issue_key"])
                executions = execution_history(db, row["server_key"], row["issue_key"])
                from jira_sop import context
                result.append({**context(db, row["workflow_id"]), "executionCount": len(executions), "retryCount": max(0, len(executions) - 1), "executions": executions, "reworkCount": len(reworks), "reworks": reworks, "workflowId": row["workflow_id"], "taskDefinitionId": row["task_definition_id"],
                    "name": row["name"], "groupId": row["group_id"], "issueKey": row["issue_key"], "claimState": row["state"],
                    "status": row["status"], "outcome": termination["outcome"] if termination else None,
                    "waitingFor": waiting["kind"] if waiting else None,
                    "reason": termination["reason"] if termination else None,
                    "supervisorAgentId": row["supervisor_id"], "executorAgentId": row["executor_id"],
                    "supervisorRemark": row["supervisor_remark"] or "", "executorRemark": row["executor_remark"] or "",
                    "workingDirectory": row["cwd"], "createdAt": row["created_at"],
                    "executionResult": execution_result(db, row["workflow_id"]),
                    "claimedAt": row["claimed_at"], "finishedAt": row["finished_at"], "deliveries": deliveries,
                    "resourceState": "occupied" if db.execute("SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?", (row["workflow_id"],)).fetchone() else "released"})
            return {"items": result, "nextCursor": rows[limit - 1]["cursor"] if len(rows) > limit else None}
