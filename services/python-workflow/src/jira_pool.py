"""运行创建前的 Jira 采集、待办及预约。凭据不落中央数据库。"""
import asyncio
import json
import uuid

from workflow_jira import ISSUE_KEY, server_key, normalize_config


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS jira_execution_history (
      workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),server_key TEXT NOT NULL,
      issue_key TEXT NOT NULL,started_at TEXT NOT NULL,previous_workflow_id TEXT,reason TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS jira_collections (
      request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, group_id TEXT NOT NULL,
      name TEXT NOT NULL, source TEXT NOT NULL, config_json TEXT NOT NULL,
      state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
      finished_at TEXT, agent_id TEXT, server_key TEXT, account_id TEXT,
      account_alias TEXT, count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS jira_pool_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES jira_collections(request_id),
      server_key TEXT NOT NULL, issue_key TEXT NOT NULL, title TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending', reservation_id TEXT UNIQUE, workflow_id TEXT,
      UNIQUE(request_id,issue_key)
    );
    CREATE INDEX IF NOT EXISTS idx_jira_pool_issue ON jira_pool_items(server_key,issue_key,state);
    ''')


def request_collection(store, body):
    from workflow_store import utc_now
    request_id = str(uuid.UUID(body['requestId']))
    config = normalize_config(body.get('jira'))
    if not config or body.get('source') not in {'web', 'schedule', 'dingtalk'}:
        raise ValueError('采集来源或 Jira 配置无效。')
    for key in ('taskDefinitionId', 'groupId'):
        uuid.UUID(body[key])
    name = body.get('name', '')
    if not isinstance(name, str) or len(name) > 1000:
        raise ValueError('任务名称无效。')
    encoded = json.dumps(config, sort_keys=True)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM jira_collections WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if (old['task_id'], old['group_id'], old['config_json'], old['source']) != (body['taskDefinitionId'], body['groupId'], encoded, body['source']):
                raise ValueError('采集请求编号已用于其他配置。')
        else:
            db.execute('''INSERT INTO jira_collections(request_id,task_id,group_id,name,source,config_json,state,created_at)
                VALUES(?,?,?,?,?,?,'waiting',?)''', (request_id, body['taskDefinitionId'], body['groupId'], name, body['source'], encoded, utc_now()))
    return collection(store, request_id)


def collection(store, request_id):
    with store._connect() as db:
        row = db.execute('SELECT * FROM jira_collections WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            raise ValueError('找不到采集请求。')
        pending = db.execute("SELECT COUNT(*) FROM jira_pool_items WHERE request_id=? AND state='pending'", (request_id,)).fetchone()[0]
        return {'requestId': row['request_id'], 'state': row['state'], 'reason': row['reason'],
                'count': row['count'], 'pending': pending, 'createdAt': row['created_at'], 'finishedAt': row['finished_at']}


def issue_available(db, server, key):
    old = db.execute('''SELECT w.status,c.workflow_id FROM jira_issue_claims c JOIN workflows w USING(workflow_id)
        WHERE c.server_key=? AND c.issue_key=?''', (server, key)).fetchone()
    if old and (old['status'] not in {'completed', 'failed', 'cancelled'} or db.execute(
            'SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?', (old['workflow_id'],)).fetchone()):
        return False
    return not db.execute("SELECT 1 FROM jira_pool_items WHERE server_key=? AND issue_key=? AND state='reserved'", (server, key)).fetchone()


def reserve(store, request_id, reservation_id):
    uuid.UUID(reservation_id)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM jira_pool_items WHERE reservation_id=?', (reservation_id,)).fetchone()
        batch = db.execute('SELECT * FROM jira_collections WHERE request_id=?', (request_id,)).fetchone()
        if not batch or batch['state'] != 'ready':
            raise ValueError('采集尚未完成或来源已失效。')
        if old and old['state'] not in {'reserved','bound'}:
            raise ValueError('预约已失效。')
        if old and old['request_id'] != request_id:
            raise ValueError('预约编号已用于其他来源。')
        if not old:
            if batch['source'] != 'schedule' and db.execute('SELECT 1 FROM jira_pool_items WHERE request_id=? AND reservation_id IS NOT NULL', (request_id,)).fetchone():
                return None
            for item in db.execute("SELECT * FROM jira_pool_items WHERE request_id=? AND state='pending' ORDER BY id", (request_id,)).fetchall():
                if not issue_available(db, item['server_key'], item['issue_key']):
                    continue
                db.execute("UPDATE jira_pool_items SET state='reserved',reservation_id=? WHERE id=?", (reservation_id, item['id']))
                old = dict(item)
                break
        if not old:
            return None
        return {'reservationId': reservation_id, 'issueKey': old['issue_key'], 'title': old['title'],
                'server': old['server_key'], 'accountId': batch['account_id']}


def bind(db, spec):
    """与工作流创建使用同一事务；预约同时成为既有领取锁。"""
    binding = spec.get('jiraReservation')
    if not binding:
        return
    item = db.execute('''SELECT p.*,c.task_id,c.group_id,c.config_json,c.account_id FROM jira_pool_items p
        JOIN jira_collections c USING(request_id) WHERE reservation_id=?''', (binding['reservationId'],)).fetchone()
    if (not item or item['state'] != 'reserved' or item['task_id'] != spec.get('taskDefinitionId')
            or item['group_id'] != spec.get('groupId') or json.loads(item['config_json']) != spec['jira'] or item['issue_key'] != binding['issueKey']
            or item['server_key'] != binding['server'] or item['account_id'] != binding['accountId']
            or ('title' in binding and item['title'] != binding['title'])):
        raise ValueError('工单预约与提交快照不一致。')
    old = db.execute('SELECT workflow_id FROM jira_issue_claims WHERE server_key=? AND issue_key=?', (item['server_key'], item['issue_key'])).fetchone()
    if old:
        if old['workflow_id'] != spec['workflowId']:
            previous = db.execute('SELECT status FROM workflows WHERE workflow_id=?', (old['workflow_id'],)).fetchone()
            if previous['status'] not in {'completed', 'failed', 'cancelled'} or db.execute('SELECT 1 FROM workflow_machine_claims WHERE workflow_id=?', (old['workflow_id'],)).fetchone():
                raise ValueError('原工单仍被占用。')
        db.execute('DELETE FROM jira_issue_claims WHERE server_key=? AND issue_key=?', (item['server_key'], item['issue_key']))
    db.execute('INSERT INTO jira_issue_claims VALUES(?,?,?)', (item['server_key'], item['issue_key'], spec['workflowId']))
    db.execute("UPDATE jira_pool_items SET state='consumed' WHERE server_key=? AND issue_key=? AND state='pending'", (item['server_key'], item['issue_key']))
    db.execute("UPDATE jira_pool_items SET state='bound',workflow_id=? WHERE id=?", (spec['workflowId'], item['id']))
    db.execute("UPDATE jira_pool_items SET state='consumed' WHERE request_id=? AND state='pending' AND EXISTS (SELECT 1 FROM jira_collections c WHERE c.request_id=? AND c.source<>'schedule')", (item['request_id'], item['request_id']))


def cancel(store, request_id):
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE jira_collections SET state='cancelled',reason='来源配置已变更或停止派发。' WHERE request_id=?", (request_id,))
        # 配置中心仅在确认尚未创建运行后撤销；已绑定运行不受影响。
        db.execute("UPDATE jira_pool_items SET state='cancelled' WHERE request_id=? AND state IN ('pending','reserved')", (request_id,))


async def collect(gateway, request_id):
    from workflow_resources import finish_check
    from workflow_store import utc_now
    from jira_runtime import client_for, read_file, CONFIG_PATH, account_from_config, search_page, account_identity, excluding_query
    with gateway.store._connect() as db:
        row = db.execute("SELECT * FROM jira_collections WHERE request_id=? AND state='waiting'", (request_id,)).fetchone()
        if not row:
            return
        row = dict(row)
        # 同时触发的同一任务共用一次已完整完成的查询，各来源仍单独保存执行额度。
        shared = db.execute('''SELECT * FROM jira_collections WHERE task_id=? AND config_json=?
            AND state='ready' AND finished_at>=? ORDER BY finished_at DESC LIMIT 1''', (row['task_id'],row['config_json'],row['created_at'])).fetchone()
        if shared:
            db.execute("UPDATE jira_collections SET state='ready',finished_at=?,server_key=?,account_id=?,account_alias=?,count=? WHERE request_id=? AND state='waiting'",
                (shared['finished_at'],shared['server_key'],shared['account_id'],shared['account_alias'],shared['count'],request_id))
            for item in db.execute('SELECT * FROM jira_pool_items WHERE request_id=?', (shared['request_id'],)).fetchall():
                state = 'pending' if issue_available(db,item['server_key'],item['issue_key']) else 'consumed'
                db.execute('INSERT INTO jira_pool_items(request_id,server_key,issue_key,title,state) VALUES(?,?,?,?,?)', (request_id,item['server_key'],item['issue_key'],item['title'],state))
            return
    selected = await acquire_supervisor(gateway, 'jira:' + request_id)
    if not selected:
        return
    key, agent = selected
    stopped = True
    try:
        with gateway.store._connect() as db:
            if not db.execute("UPDATE jira_collections SET state='collecting',agent_id=? WHERE request_id=? AND state='waiting'", (key, request_id)).rowcount:
                return
        config = json.loads(row['config_json'])
        async with client_for(gateway.orchestrator, agent) as client:
            raw = await read_file(client, CONFIG_PATH)
            alias, account = account_from_config(raw, config['account'])
            stopped = False
            identity = await account_identity(client, raw, alias, agent.cwd)
            stopped = True
            found = {}
            while True:
                stopped = False
                page = await search_page(client, raw, alias, excluding_query(config['jql'], list(found)), agent.cwd)
                stopped = True
                if not page:
                    break
                if any(key in found for key in page) or len(found) + len(page) > 1000:
                    raise ValueError('采集超过 1000 项或分页未前进，请缩小 SOP 查询范围。')
                found.update(page)
        with gateway.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("UPDATE jira_collections SET state='ready',server_key=?,account_id=?,account_alias=?,count=?,finished_at=? WHERE request_id=? AND state='collecting'", (account['baseUrl'], identity, alias, len(found), utc_now(), request_id)).rowcount:
                return
            for issue, title in found.items():
                state = 'pending' if issue_available(db, account['baseUrl'], issue) else 'consumed'
                db.execute('INSERT INTO jira_pool_items(request_id,server_key,issue_key,title,state) VALUES(?,?,?,?,?)', (request_id, account['baseUrl'], issue, title, state))
    except Exception as error:
        from jira_runtime import JiraCommandError
        if isinstance(error, (JiraCommandError, ValueError)):
            stopped = True
        # 远程错误可能包含凭据或命令，页面仅接收固定分类。
        with gateway.store._connect() as db:
            db.execute("UPDATE jira_collections SET state='failed',reason=?,finished_at=? WHERE request_id=? AND state='collecting'", ('采集失败，请核对监督机、账号和 Jira 查询；执行状态不明时保留机器占用。', utc_now(), request_id))
        import logging
        logging.getLogger(__name__).warning('Jira 采集失败，异常类型=%s', type(error).__name__)
    finally:
        await asyncio.to_thread(finish_check, gateway.store, 'jira:' + request_id, stopped)


def board(store, group_id='', before=None, limit=50, project='', task='', state=''):
    from workflow_monitor import public_text
    if group_id:
        uuid.UUID(group_id)
    if type(limit) is not int or not 1 <= limit <= 100 or before is not None and (type(before) is not int or before < 1):
        raise ValueError('分页参数无效。')
    if any(not isinstance(v, str) or len(v) > 160 for v in (project, task, state)):
        raise ValueError('筛选参数无效。')
    # 采集记录仍各自承担派发资格；这里只投影每张工单的当前状态。
    # 游标使用首次入池 ID，重复采集、返工不会把已翻过的工单移到另一页。
    query = '''WITH ranked AS (
        SELECT p.*,c.name,c.group_id,c.task_id,c.created_at,c.finished_at,c.source,
            MIN(p.id) OVER issue AS issue_id,
            MAX(c.finished_at) OVER issue AS collected_at,
            MIN(CASE WHEN p.state IN ('pending','reserved') AND c.state='ready'
                THEN COALESCE(c.finished_at,c.created_at) END) OVER issue AS queued_at,
            FIRST_VALUE(p.title) OVER (PARTITION BY p.server_key,p.issue_key ORDER BY p.id DESC) AS latest_title,
            ROW_NUMBER() OVER (PARTITION BY p.server_key,p.issue_key ORDER BY
                CASE
                    WHEN w.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                        SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=p.workflow_id) THEN 0
                    WHEN p.state='reserved' AND c.state='ready' THEN 1
                    WHEN p.state='pending' AND c.state='ready' THEN 2
                    WHEN p.state<>'consumed' THEN 3 ELSE 4
                END,p.id DESC) AS position
        FROM jira_pool_items p JOIN jira_collections c USING(request_id)
        LEFT JOIN workflows w ON w.workflow_id=p.workflow_id
        WINDOW issue AS (PARTITION BY p.server_key,p.issue_key)
    ), current_items AS (
        SELECT r.*,CASE
                WHEN w.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                    SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=claim.workflow_id)
                    THEN claim.workflow_id
                WHEN r.state IN ('pending','reserved','invalid','cancelled') THEN NULL
                ELSE COALESCE(claim.workflow_id,r.workflow_id)
            END AS current_workflow_id
        FROM ranked r
        LEFT JOIN jira_issue_claims claim ON claim.server_key=r.server_key AND claim.issue_key=r.issue_key
        LEFT JOIN workflows w ON w.workflow_id=claim.workflow_id
        WHERE r.position=1
    ), board_items AS (
        SELECT r.issue_id AS id,r.request_id,r.server_key,r.issue_key,r.latest_title AS title,
            CASE WHEN w.workflow_id IS NOT NULL THEN 'bound' ELSE r.state END AS state,
            r.reservation_id,w.workflow_id,r.name,r.group_id,r.task_id,r.created_at,r.finished_at,
            r.collected_at,r.queued_at,r.source,w.status AS run_status,w.finished_at AS run_finished_at,
            w.termination_json,h.started_at,
            EXISTS (SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=w.workflow_id)
                AND w.status IN ('completed','failed','cancelled') AS stop_pending,
            (SELECT kind FROM workflow_advance_gates g WHERE g.workflow_id=w.workflow_id
                AND g.status IN ('pending','held') ORDER BY g.created_at DESC LIMIT 1) AS waitingFor
        FROM current_items r LEFT JOIN workflows w ON w.workflow_id=r.current_workflow_id
        LEFT JOIN jira_execution_history h ON h.workflow_id=w.workflow_id
    ), filtered AS (
        SELECT b.* FROM board_items b
        WHERE (:project='' OR substr(b.issue_key,1,instr(b.issue_key,'-')-1)=:project)
            AND (:state='' OR COALESCE(b.run_status,b.state)=:state)
            AND EXISTS (SELECT 1 FROM jira_pool_items p JOIN jira_collections c USING(request_id)
                WHERE p.server_key=b.server_key AND p.issue_key=b.issue_key
                    AND (:group_id='' OR c.group_id=:group_id)
                    AND (:task='' OR c.name LIKE :task_pattern))
    ) '''
    params = {'group_id': group_id, 'project': project, 'task': task, 'task_pattern': '%'+task+'%',
              'state': state, 'before': before, 'limit': limit + 1}
    with store._connect() as db:
        # 页面、汇总与来源读取同一快照，避免采集或绑定途中出现不一致。
        db.execute('BEGIN')
        rows = db.execute(query + '''SELECT * FROM filtered WHERE (:before IS NULL OR id<:before)
            ORDER BY id DESC LIMIT :limit''', params).fetchall()
        collections = db.execute('''SELECT request_id AS requestId,name,group_id AS groupId,source,state,reason,count,
            created_at AS createdAt,finished_at AS finishedAt FROM jira_collections
            WHERE (?='' OR group_id=?) ORDER BY created_at DESC LIMIT 50''', (group_id, group_id)).fetchall()
        projects = db.execute(query + '''SELECT substr(issue_key,1,instr(issue_key,'-')-1) AS project,
            MAX(collected_at) AS lastCollectedAt,
            SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN run_status='running' THEN 1 ELSE 0 END) AS running
            FROM filtered GROUP BY substr(issue_key,1,instr(issue_key,'-')-1) ORDER BY project''', params).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            termination = json.loads(item.pop('termination_json') or '{}')
            item['outcome'] = termination.get('outcome')
            item['reason'] = public_text(termination.get('reason'))
            item['stop_pending'] = bool(item['stop_pending'])
            item['sources'] = [dict(source) for source in db.execute('''SELECT taskId,groupId,name FROM (
                SELECT c.task_id AS taskId,c.group_id AS groupId,c.name,
                    ROW_NUMBER() OVER (PARTITION BY c.task_id,c.group_id ORDER BY p.id DESC) AS position
                FROM jira_pool_items p JOIN jira_collections c USING(request_id)
                WHERE p.server_key=? AND p.issue_key=? AND (?='' OR c.group_id=?)
            ) WHERE position=1 ORDER BY name,taskId,groupId''',
                (row['server_key'], row['issue_key'], group_id, group_id))]
            item['reworks'] = rework_history(db, row['server_key'], row['issue_key'])
            item['reworkCount'] = len(item['reworks'])
            item['executions'] = execution_history(db, row['server_key'], row['issue_key'])
            items.append(item)
        return {'projects': [dict(r) for r in projects], 'items': items, 'collections': [dict(r) for r in collections],
                'nextCursor': rows[limit - 1]['id'] if len(rows) > limit else None}


async def tick(gateway):
    with gateway.store._connect() as db:
        rows = db.execute("SELECT request_id FROM jira_collections WHERE state='waiting' ORDER BY created_at LIMIT 10").fetchall()
    for row in rows:
        await collect(gateway, row['request_id'])


def recover(store):
    with store._connect() as db:
        db.execute("UPDATE jira_collections SET state='failed',reason='采集过程因服务重启中断，请核实机器状态后重新触发。' WHERE state='collecting'")


def record_rework(db, workflow_id, timestamp):
    """每个新运行首次真正绑定业务执行时记账；重复事件不重复计数。"""
    current = db.execute('SELECT * FROM workflow_jira_runs WHERE workflow_id=?', (workflow_id,)).fetchone()
    if not current or not current['issue_key'] or db.execute('SELECT 1 FROM jira_execution_history WHERE workflow_id=?', (workflow_id,)).fetchone():
        return
    previous = db.execute('''SELECT j.workflow_id,w.termination_json FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
        WHERE j.server_key=? AND j.issue_key=? AND j.workflow_id<>? AND w.status IN ('completed','failed','cancelled')
        AND EXISTS (SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=j.workflow_id AND n.job_id IS NOT NULL)
        ORDER BY w.finished_at DESC,j.rowid DESC LIMIT 1''', (current['server_key'], current['issue_key'], workflow_id)).fetchone()
    reason, source = '', None
    if previous:
        termination = json.loads(previous['termination_json']) if previous['termination_json'] else {}
        deliveries = db.execute('''SELECT d.* FROM workflow_jira_deliveries d WHERE workflow_id=?
            AND attempt=(SELECT MAX(x.attempt) FROM workflow_jira_deliveries x WHERE x.workflow_id=d.workflow_id AND x.node_id=d.node_id)
            ORDER BY recorded_at DESC''', (previous['workflow_id'],)).fetchall()
        if termination.get('outcome') == 'blocked':
            reason = termination.get('reason') or '上一轮业务阻断，未提供具体原因。'
        elif any(d['status'] == 'blocked' for d in deliveries):
            reason = next(d['summary'] or '上一轮业务阻断，未提供具体原因。' for d in deliveries if d['status'] == 'blocked')
        elif any(d['status'] == 'not_submitted' for d in deliveries) and not any(d['status'] in {'submitted', 'unknown'} for d in deliveries):
            reason = next(d['summary'] or '上一轮未提交代码，未提供具体原因。' for d in deliveries if d['status'] == 'not_submitted')
        if reason:
            source = previous['workflow_id']
    db.execute('INSERT INTO jira_execution_history VALUES(?,?,?,?,?,?)', (workflow_id,current['server_key'],current['issue_key'],timestamp,source,reason))


def execution_history(db, server, issue):
    """逐轮读取已开始的业务执行；历史未记账的运行不补算返工编号。"""
    from workflow_monitor import public_text
    rows = db.execute('''SELECT w.workflow_id AS workflowId,w.status,w.created_at AS createdAt,
        w.finished_at AS finishedAt,w.termination_json,h.workflow_id AS tracked,
        h.started_at AS startedAt,h.previous_workflow_id AS previousWorkflowId,h.reason AS reworkReason,
        (SELECT MIN(n.started_at) FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id) AS nodeStartedAt,
        (SELECT kind FROM workflow_advance_gates g WHERE g.workflow_id=w.workflow_id
            AND g.status IN ('pending','held') ORDER BY g.created_at DESC LIMIT 1) AS waitingFor,
        EXISTS (SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=w.workflow_id)
            AND w.status IN ('completed','failed','cancelled') AS stopPending
        FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
        LEFT JOIN jira_execution_history h USING(workflow_id)
        WHERE j.server_key=? AND j.issue_key=? AND (h.workflow_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.job_id IS NOT NULL))
        ORDER BY COALESCE(h.started_at,(SELECT MIN(n.started_at) FROM workflow_nodes n
            WHERE n.workflow_id=w.workflow_id),w.created_at),w.created_at,w.workflow_id''', (server, issue)).fetchall()
    executions, rework_number = [], 0
    for row in rows:
        entry = dict(row)
        tracked = entry.pop('tracked') is not None
        termination = json.loads(entry.pop('termination_json') or '{}')
        entry['startedAt'] = entry['startedAt'] or entry.pop('nodeStartedAt')
        entry.pop('nodeStartedAt', None)
        entry['outcome'] = termination.get('outcome')
        entry['reason'] = public_text(termination.get('reason'))
        entry['reworkReason'] = public_text(entry['reworkReason'])
        entry['stopPending'] = bool(entry['stopPending'])
        from workflow_allocation import snapshot
        allocation = snapshot(db, entry['workflowId'])
        entry['machines'] = {key: value for key, value in (allocation or {}).items()
                             if key in {'supervisorAgentId', 'executorAgentId', 'supervisorRemark',
                                        'executorRemark', 'supervisorDeleted', 'executorDeleted'}}
        if entry['previousWorkflowId']:
            rework_number += 1
            entry['reworkNumber'] = rework_number
            entry['kind'] = 'rework'
        else:
            entry['reworkNumber'] = None
            entry['kind'] = 'first' if tracked and not executions else 'execution' if tracked else 'legacy'
        executions.append(entry)
    return executions


def rework_history(db, server, issue):
    from workflow_monitor import public_text
    return [{**dict(row), 'reason': public_text(row['reason'])} for row in db.execute('''SELECT workflow_id AS workflowId,started_at AS startedAt,
        previous_workflow_id AS previousWorkflowId,reason FROM jira_execution_history
        WHERE server_key=? AND issue_key=? AND previous_workflow_id IS NOT NULL ORDER BY started_at''', (server, issue))]


async def acquire_supervisor(gateway, operation_id):
    from workflow_gateway import SIDECAR_LEASE_TIMEOUT_SEC
    from workflow_jira import compatible_agent
    from workflow_resources import claim_check
    from workflow_store import utc_now
    rows = await asyncio.to_thread(gateway.registry.rows)
    for key, row in rows.items():
        agent = gateway.registry.config_from_row(key, row)
        if (row['test_status'] != 'passed' or not agent.enabled or not agent.allow_write
                or 'supervisor' not in agent.capabilities or not compatible_agent(agent)):
            continue
        state = (await asyncio.to_thread(gateway.store.sidecar_status, key, timeout_sec=SIDECAR_LEASE_TIMEOUT_SEC)
                 if agent.orchestration_mode == 'remote_sidecar' else gateway._supervisor_runtime.get(key, {}))
        if state.get('connectionStatus') != 'online':
            continue
        if await asyncio.to_thread(claim_check, gateway.store, key, operation_id, utc_now()):
            return key, agent
    return None


async def verify_reservation(gateway, binding):
    """排队后重新核验单张工单，成功后 Java 才创建运行。"""
    from workflow_resources import finish_check
    from jira_runtime import client_for, read_file, CONFIG_PATH, account_from_config, account_identity, search_page, including_query
    with gateway.store._connect() as db:
        row = db.execute('''SELECT p.*,c.config_json FROM jira_pool_items p JOIN jira_collections c USING(request_id)
            WHERE reservation_id=?''', (binding['reservationId'],)).fetchone()
        if row['state'] == 'bound':
            return True
    operation = 'jira-verify:' + binding['reservationId']
    selected = await acquire_supervisor(gateway, operation)
    if not selected:
        return False
    _, agent = selected
    stopped = True
    try:
        config = json.loads(row['config_json'])
        async with client_for(gateway.orchestrator, agent) as client:
            raw = await read_file(client, CONFIG_PATH)
            alias, account = account_from_config(raw, config['account'])
            stopped = False
            identity = await account_identity(client, raw, alias, agent.cwd)
            stopped = True
            if account['baseUrl'] != binding['server'] or identity != binding['accountId']:
                raise ValueError('监督机账号与采集身份不一致。')
            stopped = False
            page = await search_page(client, raw, alias, including_query(config['jql'], binding['issueKey']), agent.cwd)
            stopped = True
        if binding['issueKey'] not in page:
            with gateway.store._connect() as db:
                db.execute("UPDATE jira_pool_items SET state='invalid',reservation_id=NULL WHERE reservation_id=? AND state='reserved'", (binding['reservationId'],))
            return False
        return True
    except Exception as error:
        from jira_runtime import JiraCommandError
        if isinstance(error, (JiraCommandError, ValueError)):
            stopped = True
        import logging
        logging.getLogger(__name__).warning('Jira 派发核验失败，异常类型=%s', type(error).__name__)
        raise RuntimeError('工单核验失败，请核对监督机账号及连接；未创建运行。') from None
    finally:
        await asyncio.to_thread(finish_check, gateway.store, operation, stopped)


def execution_detail(store, workflow_id):
    from workflow_monitor import public_text
    from workflow_allocation import snapshot as allocation_snapshot
    from workflow_resources import snapshot as resource_snapshot
    with store._connect() as db:
        db.execute('BEGIN')
        row = db.execute("SELECT w.status,w.started_at,w.finished_at,w.response,w.termination_json FROM workflows w JOIN workflow_jira_runs j USING(workflow_id) WHERE w.workflow_id=?", (workflow_id,)).fetchone()
        if row is None:
            return None
        termination = json.loads(row['termination_json'] or '{}')
        steps = [{'nodeId': n['node_id'], 'name': public_text(n['role_name']) or '步骤 ' + str(i + 1),
                  'status': n['status'], 'summary': public_text(n['response'])}
                 for i, n in enumerate(db.execute('SELECT node_id,role_name,status,response FROM workflow_nodes WHERE workflow_id=? ORDER BY position', (workflow_id,)))]
        deliveries = [dict(d) for d in db.execute('SELECT node_id AS nodeId,attempt,status,reference,summary,recorded_at AS recordedAt FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY recorded_at,node_id,attempt', (workflow_id,))]
        for delivery in deliveries:
            for key in ('reference', 'summary'):
                delivery[key] = public_text(delivery[key])
        return {'workflowId': workflow_id, 'status': row['status'], 'summary': public_text(row['response']),
                'reason': public_text(termination.get('reason')), 'steps': steps, 'deliveries': deliveries,
                'allocation': allocation_snapshot(db, workflow_id), 'resources': resource_snapshot(db, workflow_id)}
