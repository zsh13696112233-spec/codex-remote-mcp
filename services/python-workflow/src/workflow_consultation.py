"""任务内的记录查询和独立咨询；不写步骤状态，不恢复原业务会话。"""
import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from codex_orchestrator_mcp import AppServerRpcError, Job, is_absolute_remote_path
from workflow_store import ACTIVE_NODE_STATUSES, utc_now


TOOLS = {"list_step_attempts", "read_step_records", "consult_step"}
NOTICE = "【内容过长，已省略】"


async def storage_call(function, *args, **kwargs):
    # 数据库线程即使调用者取消也必须完成，之后才能释放消息队列。
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def safe_text(value: Any, limit: int = 20_000) -> str:
    """过滤常见凭据、内部连接信息和传输内容；不输出原始异常。"""
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[\w.\-+/=]+", r"\1[已隐藏]", text)
    text = re.sub(r'''(?i)((?:token|password|passwd|secret|api[_-]?key|authorization)\s*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;\n]+)''', r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:thread|turn|session)[_-]?id\s*[\"']?\s*[:=]\s*)[^\s,;\n]+", r"\1[已隐藏]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已隐藏]", text)
    text = re.sub(r"(?:https?|wss?)://[^\s<>\"')]+", "[连接地址已隐藏]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[内部地址已隐藏]", text)
    text = re.sub(r"(?i)data:image/[^\s]+", "[图片内容已省略]", text)
    return text if len(text) <= limit else text[:max(0, limit - len(NOTICE))] + NOTICE


def bounded_result(value: dict[str, Any], limit: int = 20_000) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return value
    # 仍返回有效 JSON，不能在序列化结果中间切断。
    result = {"summary": safe_text(encoded, max(0, (limit - 200) // 2)), "truncated": True}
    return result


def visible_item(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "agentMessage":
        return safe_text(item.get("text"))
    if kind == "userMessage":
        return safe_text("\n".join(str(part.get("text", "")) for part in item.get("content", [])
                                  if isinstance(part, dict) and part.get("type") in {"text", "inputText"}))
    if kind == "commandExecution":
        return safe_text(json.dumps({k: item[k] for k in ("command", "aggregatedOutput", "exitCode") if k in item}, ensure_ascii=False))
    if kind == "fileChange":
        return safe_text(json.dumps(item.get("changes", []), ensure_ascii=False))
    if kind in {"mcpToolCall", "dynamicToolCall"}:
        # 二进制/图片和工具参数可能含凭据；只取工具名及纯文字结果。
        result = item.get("result")
        parts = item.get("contentItems") or (result.get("content", []) if isinstance(result, dict) else [])
        if not isinstance(parts, list):
            parts = []
        return safe_text(str(item.get("tool", "")) + "\n" + "\n".join(
            str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") in {"text", "inputText"}))
    return ""  # 推理、图片、会话元数据不进入咨询记录。


class ConsultationStore:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_consultation_sessions (
                    workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    actor TEXT NOT NULL, thread_id TEXT, context_mode TEXT NOT NULL DEFAULT 'records',
                    blocked INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                    PRIMARY KEY(workflow_id,node_id,attempt,actor));
                CREATE TABLE IF NOT EXISTS workflow_assistant_calls (
                    workflow_id TEXT NOT NULL, message_id TEXT NOT NULL, call_index INTEGER NOT NULL,
                    request_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
                    thread_id TEXT, turn_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(workflow_id,message_id,call_index));
                CREATE TABLE IF NOT EXISTS workflow_record_queries (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, keyword TEXT, records_json TEXT NOT NULL,
                    source TEXT NOT NULL, complete INTEGER NOT NULL, created_at TEXT NOT NULL);
            """)

    def attempt(self, workflow_id, node_id, number=None):
        with self.store._connect() as db:
            current = db.execute("SELECT * FROM workflow_nodes WHERE workflow_id=? AND node_id=?", (workflow_id, node_id)).fetchone()
            if current is None:
                raise ValueError("当前任务没有这个步骤。")
            current = dict(current)
            latest = int(current["attempt_count"] or 0)
            number = latest if number is None else number
            if type(number) is not int or number < 0 or number > latest:
                raise ValueError("执行版本无效。")
            if number == latest:
                result = current.copy()
            else:
                row = db.execute("SELECT * FROM workflow_node_attempts WHERE workflow_id=? AND node_id=? AND attempt_number=?", (workflow_id, node_id, number)).fetchone()
                if row is None:
                    raise ValueError("找不到这次执行记录。")
                result = {**current, **dict(row)}
            result["attempt"] = number
            result["current_status"] = current["status"]
            result["current_attempt"] = latest
            runtime = db.execute("SELECT cwd,model FROM workflow_node_runtime WHERE workflow_id=? AND node_id=? AND attempt_number=?", (workflow_id, node_id, number)).fetchone()
            if runtime:
                result.update({k: runtime[k] for k in ("cwd", "model") if runtime[k]})
            return result

    def attempts(self, workflow_id, node_id):
        current = self.attempt(workflow_id, node_id)
        with self.store._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT attempt_number AS attempt,status,response,started_at,finished_at FROM workflow_node_attempts WHERE workflow_id=? AND node_id=? ORDER BY attempt_number", (workflow_id, node_id))]
        rows.append(current)
        return {"attempts": [{"attempt": r["attempt"], "version": r["attempt"] + 1,
                               "status": r["status"], "summary": safe_text(r.get("response"), 500),
                               "startedAt": r.get("started_at"), "finishedAt": r.get("finished_at")} for r in rows]}

    def claim(self, workflow_id, message_id, index, request):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM workflow_assistant_calls WHERE workflow_id=? AND message_id=? AND call_index=?", (workflow_id, message_id, index)).fetchone()
            if old:
                return dict(old), False
            now = utc_now()
            encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
            same = None
            if request["name"] == "consult_step":
                same = db.execute("SELECT * FROM workflow_assistant_calls WHERE workflow_id=? AND message_id=? AND request_json=? ORDER BY call_index LIMIT 1", (workflow_id, message_id, encoded)).fetchone()
            if same:
                db.execute("INSERT INTO workflow_assistant_calls VALUES(?,?,?,?,?,?,?,?,?,?)", (workflow_id, message_id, index, encoded, same["status"], same["result_json"], same["thread_id"], same["turn_id"], now, now))
                return dict(same), False
            db.execute("INSERT INTO workflow_assistant_calls(workflow_id,message_id,call_index,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (workflow_id, message_id, index, encoded, "pending", now, now))
            return {"request_json": json.dumps(request, ensure_ascii=False), "status": "pending"}, True

    def update_call(self, key, **values):
        allowed = {"status", "result_json", "thread_id", "turn_id"}
        if not values or not set(values) <= allowed:
            raise ValueError("咨询记录字段无效。")
        with self.store._connect() as db:
            db.execute("UPDATE workflow_assistant_calls SET " + ",".join(k + "=?" for k in values) + ",updated_at=? WHERE workflow_id=? AND message_id=? AND call_index=?", (*values.values(), utc_now(), *key))

    def session(self, key):
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM workflow_consultation_sessions WHERE workflow_id=? AND node_id=? AND attempt=? AND actor=?", key).fetchone()
            return dict(row) if row else None

    def save_session(self, key, thread_id, mode, blocked=0):
        with self.store._connect() as db:
            db.execute("INSERT INTO workflow_consultation_sessions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(workflow_id,node_id,attempt,actor) DO UPDATE SET thread_id=excluded.thread_id,context_mode=excluded.context_mode,blocked=excluded.blocked,updated_at=excluded.updated_at", (*key, thread_id, mode, blocked, utc_now()))

    def replay(self, workflow_id, message_id):
        with self.store._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM workflow_assistant_calls WHERE workflow_id=? AND message_id=? ORDER BY call_index", (workflow_id, message_id))]


class ConsultationService:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = ConsultationStore(gateway.store)

    @asynccontextmanager
    async def connection(self, attempt):
        registry = self.gateway.registry
        rows = await storage_call(registry.rows)
        row = rows.get(attempt["agent_id"])
        if not row or row["test_status"] != "passed":
            raise ValueError("执行服务尚未通过连接检测，当前只能查看已保存记录。")
        agent = registry.config_from_row(attempt["agent_id"], row)
        if not agent.enabled or "executor" not in agent.capabilities:
            raise ValueError("执行服务不可用于咨询，当前只能查看已保存记录。")
        orchestrator = self.gateway.assistant_orchestrator
        client = orchestrator._client_factory(agent.url, token=orchestrator._resolve_agent_token(agent))
        async with client:
            yield client, agent

    async def records(self, workflow_id, attempt, keyword=None, cursor=None, *, remote=True):
        if cursor is not None:
            try:
                query_id, position = cursor.split(":")
                if not position.isdigit():
                    raise ValueError()
                def load_query():
                    with self.gateway.store._connect() as db:
                        row = db.execute("SELECT * FROM workflow_record_queries WHERE id=? AND workflow_id=? AND node_id=? AND attempt=? AND keyword IS ?", (query_id, workflow_id, attempt["node_id"], attempt["attempt"], keyword)).fetchone()
                        return dict(row) if row else None
                query = await storage_call(load_query)
                if not query:
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError("记录分页位置与当前步骤或版本不匹配。") from None
            return self.record_page(attempt, query_id, json.loads(query["records_json"]), query["source"], bool(query["complete"]), query["created_at"], int(position))
        records = []
        source = "中央记录"
        complete = False
        thread = None
        if remote and attempt.get("thread_id") and attempt.get("turn_id"):
            try:
                async with self.connection(attempt) as (client, _):
                    response = await client.request("thread/read", {"threadId": attempt["thread_id"], "includeTurns": True})
                    thread = response.get("thread", {})
                    for turn in thread.get("turns", []):
                        if turn.get("id") == attempt["turn_id"]:
                            records = [{"kind": i.get("type"), "text": visible_item(i)} for i in turn.get("items", []) if isinstance(i, dict)]
                            records = [r for r in records if r["text"]]
                            complete = turn.get("status") in {"completed", "failed", "interrupted"}
                            source = "步骤会话记录"
                            break
            except (ConnectionError, OSError, RuntimeError, ValueError, TimeoutError, TypeError, KeyError, AttributeError):
                pass  # 返回显式不完整标记；不把连接异常作为业务证据。
        if not records:
            source, complete = "中央记录", False
            def read_local():
                with self.gateway.store._connect() as db:
                    return [self.gateway.store._event_snapshot(r) for r in db.execute("SELECT * FROM workflow_events WHERE workflow_id=? AND node_id=? ORDER BY sequence", (workflow_id, attempt["node_id"]))]
            for event in await storage_call(read_local):
                payload = event["payload"]
                params = payload.get("message", {}).get("params", {})
                # 仅轮次匹配或显式标记的版本可信，不能按当前步骤状态猜历史归属。
                if not ((attempt.get("turn_id") and params.get("turnId") == attempt["turn_id"])
                        or payload.get("attemptNumber") == attempt["attempt"]):
                    continue
                if event["type"] == "appserver.item/completed":
                    item = params.get("item") or {}
                    text = visible_item(item)
                    if text:
                        records.append({"kind": item.get("type"), "text": text, "time": event["createdAt"]})
            records.insert(0, {"kind": "instruction", "text": safe_text(attempt.get("actual_prompt") or attempt.get("original_prompt") or attempt.get("prompt"))})
            if attempt.get("response"):
                records.append({"kind": "result", "text": safe_text(attempt["response"])})
        if keyword:
            records = [r for r in records if keyword.casefold() in r["text"].casefold()]
        # 一次查询固定快照；执行继续或远端恢复不会改变后续页的来源和顺序。
        retained, size = [], 0
        for record in records:
            record_size = len(json.dumps(record, ensure_ascii=False))
            if size + record_size > 200_000:
                complete = False
                break
            retained.append(record)
            size += record_size
            if NOTICE in record.get("text", ""):
                complete = False
        query_id, observed = uuid.uuid4().hex, utc_now()
        def save_query():
            with self.gateway.store._connect() as db:
                db.execute("INSERT INTO workflow_record_queries VALUES(?,?,?,?,?,?,?,?,?)", (query_id, workflow_id, attempt["node_id"], attempt["attempt"], keyword, json.dumps(retained, ensure_ascii=False), source, int(complete), observed))
        await storage_call(save_query)
        return self.record_page(attempt, query_id, retained, source, complete, observed, 0)

    @staticmethod
    def record_page(attempt, identity, records, source, complete, observed, offset):
        page = []
        used = 0
        for record in records[offset:]:
            if len(record["text"]) > 8000:
                complete = False
            record = {**record, "text": safe_text(record["text"], 8000)}
            size = len(json.dumps(record, ensure_ascii=False))
            if page and used + size > 16_000:
                break
            page.append(record)
            used += size
            if len(page) == 20:
                break
        next_offset = offset + len(page)
        return {"nodeId": attempt["node_id"], "attempt": attempt["attempt"], "source": source,
                "observedAt": observed, "complete": complete and next_offset >= len(records), "records": page,
                "nextCursor": f"{identity}:{next_offset}" if next_offset < len(records) else None,
                "notice": "记录只代表当时执行情况，不证明当前代码正确。" + ("还有后续页。" if next_offset < len(records) else "") + ("记录可能缺失、被截断或仍在产生；未找到不代表不存在。" if not complete else "")}

    @staticmethod
    def validate(request):
        if not isinstance(request, dict) or set(request) - {"name", "nodeId", "attempt", "keyword", "cursor", "question"}:
            raise ValueError("咨询工具参数无效。")
        if request.get("name") not in TOOLS or not isinstance(request.get("nodeId"), str) or not 1 <= len(request["nodeId"]) <= 128:
            raise ValueError("请选择当前任务中的有效步骤。")
        number = request.get("attempt")
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError("执行版本无效。")
        for field, limit in (("keyword", 200), ("cursor", 128), ("question", 4000)):
            value = request.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > limit):
                raise ValueError("查询关键词、分页位置或问题长度无效。")
        if request["name"] == "consult_step" and not request.get("question"):
            raise ValueError("请提供需要核查的具体问题。")
        return request

    async def execute(self, workflow_id, message, index, request):
        request = self.validate(request)
        attempt = await storage_call(self.store.attempt, workflow_id, request["nodeId"], request.get("attempt"))
        request = {**request, "attempt": attempt["attempt"]}
        key = (workflow_id, message["messageId"], index)
        call, fresh = await storage_call(self.store.claim, *key, request)
        if not fresh:
            if call.get("result_json"):
                return json.loads(call["result_json"])
            return await self.recover_call(key, call, message)
        text = "正在核查步骤实现" if request["name"] == "consult_step" else "正在查看步骤记录"
        await storage_call(self.gateway.store.add_event, workflow_id, node_id=request["nodeId"], source="chat", event_type="chat.assistant.progress", payload={"messageId": message["messageId"], "nodeId": request["nodeId"], "attempt": attempt["attempt"], "text": f"{text}：{safe_text(attempt['display_name'] or attempt['node_id'], 100)} · 第{attempt['attempt'] + 1}次执行"})
        try:
            if request["name"] == "list_step_attempts":
                result = await storage_call(self.store.attempts, workflow_id, request["nodeId"])
            elif request["name"] == "read_step_records":
                result = await self.records(workflow_id, attempt, request.get("keyword"), request.get("cursor"))
            elif attempt["current_status"] in ACTIVE_NODE_STATUSES or not attempt.get("turn_id"):
                result = await self.records(workflow_id, attempt)
                result["notice"] = "该步骤正在执行或尚无可咨询的执行轮次，本次只查询记录；需要核查请在执行结束后重新提问。"
            else:
                result = await self.consult(key, message, attempt, request["question"])
        except (ConnectionError, OSError, RuntimeError, ValueError, TimeoutError, TypeError, KeyError, AttributeError) as error:
            # 失败仍保留已知咨询轮次，以便下次接收消息时核对，不重复派发。
            def read_call():
                with self.gateway.store._connect() as db:
                    return dict(db.execute("SELECT * FROM workflow_assistant_calls WHERE workflow_id=? AND message_id=? AND call_index=?", key).fetchone())
            saved = await storage_call(read_call)
            if saved.get("thread_id"):
                return await self.recover_call(key, saved, message)
            result = {"error": "本次查询或现场核查未完成，不能据此判断代码情况。",
                      "failure": "invalid_response" if isinstance(error, (TypeError, KeyError, AttributeError)) else "unavailable"}
            if request["name"] == "consult_step":
                result["savedRecords"] = await self.records(workflow_id, attempt, remote=False)
        result = bounded_result(result)
        await storage_call(self.store.update_call, key, status="completed", result_json=json.dumps(result, ensure_ascii=False))
        return result

    async def recover_call(self, key, call, message):
        request = json.loads(call["request_json"])
        result = {"error": "上次咨询未能确认完成，本次没有重新发送；请重新提问。"}
        attempt = await storage_call(self.store.attempt, key[0], request["nodeId"], request["attempt"])
        session_key = (key[0], request["nodeId"], request["attempt"], message.get("actorId") or "web")
        if call.get("thread_id") and call.get("turn_id"):
            try:
                async with self.connection(attempt) as (client, _):
                    data = await client.request("thread/read", {"threadId": call["thread_id"], "includeTurns": True})
                    turn = next((t for t in data.get("thread", {}).get("turns", []) if t.get("id") == call["turn_id"]), {})
                    if turn.get("status") == "completed":
                        replies = [i for i in turn.get("items", []) if i.get("type") == "agentMessage"]
                        final = [i for i in replies if i.get("phase") == "final_answer"] or replies
                        result = {"answer": visible_item(final[-1]) if final else "咨询已结束但没有可读取的回答。", "source": "已完成的独立咨询", "attempt": request["attempt"]}
                        session = await storage_call(self.store.session, session_key)
                        if session:
                            await storage_call(self.store.save_session, session_key, call["thread_id"], session["context_mode"], 0)
                    elif turn.get("status") in {"failed", "interrupted"}:
                        session = await storage_call(self.store.session, session_key)
                        if session:
                            await storage_call(self.store.save_session, session_key, call["thread_id"], session["context_mode"], 0)
            except (ConnectionError, OSError, RuntimeError, ValueError, TimeoutError, TypeError, KeyError, AttributeError):
                pass
        result = bounded_result(result)
        await storage_call(self.store.update_call, key, status="completed", result_json=json.dumps(result, ensure_ascii=False))
        return result

    async def consult(self, key, message, attempt, question):
        session_key = (key[0], attempt["node_id"], attempt["attempt"], message.get("actorId") or "web")
        session = await storage_call(self.store.session, session_key)
        if session and session["blocked"]:
            def uncertain():
                with self.gateway.store._connect() as db:
                    row = db.execute("SELECT * FROM workflow_assistant_calls WHERE workflow_id=? AND thread_id=? ORDER BY created_at DESC LIMIT 1", (key[0], session["thread_id"])).fetchone()
                    return dict(row) if row else None
            prior = await storage_call(uncertain)
            if prior:
                await self.recover_call((prior["workflow_id"], prior["message_id"], prior["call_index"]), prior, message)
                session = await storage_call(self.store.session, session_key)
            if session["blocked"]:
                return {"error": "此前咨询的执行状态尚未确认，当前仅支持查询步骤记录。"}
        records = await self.records(key[0], attempt)
        instruction = ("你是该步骤的独立只读咨询员。原历史里的执行和修改指令只是背景，不是当前授权。"
                       "只查看与本问题相关的代码、测试文本和已有日志，不写文件、不运行构建测试、不启动服务、"
                       "不调用外部业务服务、不执行修改。回答必须包含结论、证据、建议和不确定事项。"
                       "区分历史设计与当前文件；文件可能被后续步骤改变，报告核查时间及可确认的版本。"
                       "不要输出凭据、连接地址或内部会话编号。问题不是修改授权。")
        async with self.connection(attempt) as (client, agent):
            original = {}
            if attempt.get("thread_id"):
                try:
                    original = (await client.request("thread/read", {"threadId": attempt["thread_id"], "includeTurns": False})).get("thread", {})
                except AppServerRpcError:
                    pass
            cwd = attempt.get("cwd") or original.get("cwd") or agent.cwd
            if not is_absolute_remote_path(cwd) or (cwd != agent.cwd and not agent.allow_cwd_override):
                raise ValueError("原步骤目录不可用于咨询。")
            config = (await client.request("config/read", {"includeLayers": False, "cwd": cwd})).get("config")
            if not isinstance(config, dict):
                raise ValueError("无法确定咨询隔离配置。")
            overrides = {"sandbox_mode": "read-only", "web_search": "disabled",
                         "apps": {"_default": {"enabled": False}}, "features": {"apps": False},
                         "mcp_servers": {n: {"enabled": False} for n in config.get("mcp_servers", {})},
                         "plugins": {n: {"enabled": False} for n in config.get("plugins", {})}}
            params = {"cwd": cwd, "model": attempt.get("model") or agent.model, "approvalPolicy": "never",
                      "sandbox": "read-only", "config": overrides, "developerInstructions": instruction}
            mode = "records"
            if session and session.get("thread_id"):
                mode = session["context_mode"]
                response = await client.request("thread/resume", {**params, "threadId": session["thread_id"]})
            else:
                response = None
                if original and attempt.get("turn_id"):
                    try:
                        response = await client.request("thread/fork", {**params, "threadId": attempt["thread_id"], "lastTurnId": attempt["turn_id"]})
                        mode = "fork"
                    except AppServerRpcError as error:
                        if error.code not in {-32601, -32602}:
                            raise
                if response is None:
                    response = await client.request("thread/start", params)
            thread_id = response.get("thread", {}).get("id")
            policy = response.get("sandbox") or response.get("sandboxPolicy")
            if (not thread_id or thread_id == attempt.get("thread_id") or response.get("approvalPolicy") != "never"
                    or not isinstance(policy, dict) or policy.get("type") != "readOnly"
                    or policy.get("networkAccess") is not False
                    or response.get("cwd") != cwd):
                raise ValueError("执行服务未确认独立只读权限，已拒绝咨询。")
            inventory = await client.request("mcpServerStatus/list", {"threadId": thread_id, "limit": 100})
            if not isinstance(inventory.get("data"), list) or inventory.get("nextCursor") or any(
                    entry.get("tools") or entry.get("runtimeStatus") in {"starting", "ready"}
                    for entry in inventory["data"]):
                raise ValueError("咨询外部工具未被隔离，已拒绝咨询。")
            await storage_call(self.store.save_session, session_key, thread_id, mode, 1)
            await storage_call(self.store.update_call, key, status="starting", thread_id=thread_id)
            # 在派发前再次读取状态；咨询从不更改业务状态。
            latest = await storage_call(self.store.attempt, key[0], attempt["node_id"], attempt["attempt"])
            if latest["current_status"] in ACTIVE_NODE_STATUSES:
                await storage_call(self.store.save_session, session_key, thread_id, mode, 0)
                return {"error": "该步骤已开始新执行，本次未启动现场咨询，请在结束后重新提问。"}
            prompt = instruction + "\n当前问题：" + question + "\n核查时间：" + utc_now()
            prompt += "\n用户本条原始提问：" + message["text"]
            if message.get("imageIds"):
                prompt += "\n本条问题包含参考图片，本咨询没有收到原图；不能声称核查了图片。"
            if mode == "records":
                prompt += "\n仅有以下选取记录，历史上下文不完整：" + json.dumps(records, ensure_ascii=False)
            job = Job(job_id=uuid.uuid4().hex, agent_id=agent.agent_id, prompt=prompt, requested_thread_id=None,
                      cwd=cwd, write=False, permission_profile="read_only", model=params["model"], timeout_sec=540,
                      thread_id=thread_id)
            try:
                turn = await client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}})
                job.turn_id = turn["turn"]["id"]
                await storage_call(self.store.update_call, key, status="running", turn_id=job.turn_id)
                await self.gateway.assistant_orchestrator._consume_turn(job, client, time.monotonic() + 540)
                if job.status != "completed":
                    raise RuntimeError("咨询未完成。")
                if not job.response or not job.response.strip():
                    raise RuntimeError("咨询没有提供可读取的回答。")
                await storage_call(self.store.save_session, session_key, thread_id, mode, 0)
                return {"answer": safe_text(job.response), "source": "独立只读咨询", "attempt": attempt["attempt"],
                        "contextMode": mode, "observedAt": utc_now(),
                        "notice": "核查当前文件不等于还原历史代码。" + ("本次仅继承选取记录。" if mode == "records" else "")}
            finally:
                if job.status != "completed" and job.turn_id:
                    try:
                        await asyncio.wait_for(client.request("turn/interrupt", {"threadId": thread_id, "turnId": job.turn_id}), 5)
                    except (ConnectionError, OSError, RuntimeError, ValueError, TimeoutError):
                        pass
