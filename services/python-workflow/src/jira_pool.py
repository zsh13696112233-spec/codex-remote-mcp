"""运行创建前的 Jira 采集、待办及预约。凭据仅由中央账号管理读取。"""
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
    CREATE INDEX IF NOT EXISTS idx_jira_collection_poll ON jira_collections(source,task_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_jira_pool_issue ON jira_pool_items(server_key,issue_key,state);
    ''')

    db.execute('BEGIN IMMEDIATE')
    if 'people_json' not in {r[1] for r in db.execute('PRAGMA table_info(jira_pool_items)')}:
        db.execute('ALTER TABLE jira_pool_items ADD COLUMN people_json TEXT')
    if 'candidate_queued_at' not in {r[1] for r in db.execute('PRAGMA table_info(jira_pool_items)')}:
        db.execute('ALTER TABLE jira_pool_items ADD COLUMN candidate_queued_at TEXT')
    columns = {r[1] for r in db.execute('PRAGMA table_info(jira_pool_items)')}
    for column in ('details_json', 'details_checked_at'):
        if column not in columns:
            db.execute(f'ALTER TABLE jira_pool_items ADD COLUMN {column} TEXT')
    db.commit()
    from jira_sop import initialize as initialize_sop
    initialize_sop(db)
    db.commit()

def validate_people(value):
    from datetime import datetime
    if not isinstance(value, dict) or set(value) != {'developers', 'developerStatus', 'assignee', 'assigneeStatus', 'collectedAt'}:
        raise ValueError('人员展示信息格式无效。')
    names = value['developers']
    if (not isinstance(names, list) or len(names) > 100
            or any(not isinstance(n, str) or not n.strip() or len(n) > 160 for n in names)
            or not isinstance(value['assignee'], str) or len(value['assignee']) > 160
            or value['developerStatus'] not in ('resolved', 'empty', 'unavailable')
            or value['assigneeStatus'] not in ('resolved', 'unavailable')
            or (value['developerStatus'] == 'resolved') != bool(names)
            or (value['assigneeStatus'] == 'resolved') != bool(value['assignee'])):
        raise ValueError('人员展示信息格式无效。')
    try:
        timestamp = datetime.fromisoformat(value['collectedAt'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('人员采集时间无效。') from None


def sync_polling(store, body):
    """配置中心每 30 秒同步启用任务；只创建采集，不授予运行资格。"""
    from datetime import datetime, timezone
    from workflow_store import utc_now
    if not isinstance(body, dict) or set(body) != {'tasks'} or not isinstance(body['tasks'], list):
        raise ValueError('采集任务列表无效。')
    from jira_sop import identity
    tasks = {}
    for item in body['tasks']:
        task = str(uuid.UUID(item['taskDefinitionId']))
        group = str(uuid.UUID(item['groupId']))
        config = normalize_config(item['jira'])
        if not config or not config.get('connectionId') or task in tasks:
            raise ValueError('采集任务配置无效或重复。')
        name = item['name']
        if not isinstance(name, str) or len(name) > 1000:
            raise ValueError('任务名称无效。')
        tasks[task] = (group, json.dumps(config, sort_keys=True), name, *identity(item))
    now = utc_now()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM jira_polling_scopes')
        db.executemany('INSERT INTO jira_polling_scopes(task_id,group_id,config_json,sop_id,sop_name) VALUES(?,?,?,?,?)',
                       [(task, v[0], v[1], v[3], v[4]) for task, v in tasks.items()])
        for row in db.execute("SELECT * FROM jira_collections WHERE source='poll' AND (state IN ('waiting','collecting') OR (state='ready' AND EXISTS (SELECT 1 FROM jira_pool_items p WHERE p.request_id=jira_collections.request_id AND p.state='pending')))").fetchall():
            target = tasks.get(row['task_id'])
            if not target or (target[0], target[1], target[3]) != (row['group_id'], row['config_json'], row['sop_id']):
                db.execute("UPDATE jira_collections SET state='cancelled',reason='任务已停用或查询配置已变化。' WHERE request_id=?", (row['request_id'],))
                db.execute("UPDATE jira_pool_items SET state='cancelled' WHERE request_id=? AND state='pending'", (row['request_id'],))
        for task, (group, config, name, sop_id, sop_name) in tasks.items():
            last = db.execute("SELECT * FROM jira_collections WHERE task_id=? AND source='poll' AND state<>'cancelled' ORDER BY created_at DESC LIMIT 1", (task,)).fetchone()
            if last and (last['state'] in ('waiting', 'collecting') or
                    (datetime.now(timezone.utc) - datetime.fromisoformat(last['created_at'])).total_seconds() < 30):
                continue
            db.execute("INSERT INTO jira_collections(request_id,task_id,group_id,name,source,config_json,state,created_at,sop_id,sop_name) VALUES(?,?,?,?,'poll',?,'waiting',?,?,?)",
                       (str(uuid.uuid4()), task, group, name, config, now, sop_id, sop_name))
    return {'synchronized': len(tasks)}


def materialize_dispatch(db, row):
    """启动请求仅复制触发后完成的采集快照，绝不自行查询 Jira。"""
    from jira_accounts import usable
    account = usable(db, json.loads(row['config_json']).get('connectionId'), row['group_id'])
    latest = db.execute("SELECT * FROM jira_collections WHERE source='poll' AND task_id=? AND group_id=? AND config_json=? AND sop_id IS ? AND state='ready' AND finished_at>=? AND account_id=? AND account_alias=? ORDER BY finished_at DESC LIMIT 1",
                        (row['task_id'], row['group_id'], row['config_json'], row['sop_id'], row['created_at'], account['identity'], account['executor_alias'])).fetchone()
    if not latest:
        return
    db.execute("UPDATE jira_collections SET state='ready',server_key=?,account_id=?,account_alias=?,count=?,finished_at=? WHERE request_id=?",
               (latest['server_key'], latest['account_id'], latest['account_alias'], latest['count'], latest['finished_at'], row['request_id']))
    for item in db.execute("SELECT * FROM jira_pool_items WHERE request_id=? AND state IN ('pending','consumed')", (latest['request_id'],)).fetchall():
        state = 'pending' if issue_available(db, item['server_key'], item['issue_key']) else 'consumed'
        db.execute('INSERT INTO jira_pool_items(request_id,server_key,issue_key,title,state,people_json,details_json) VALUES(?,?,?,?,?,?,?)',
                   (row['request_id'], item['server_key'], item['issue_key'], item['title'], state, item['people_json'], item['details_json']))


def publish_poll(db, row, server, found):
    from workflow_store import utc_now
    now = utc_now()
    # 完整成功后才撤销候选；预约、运行及历史不变。
    for item in db.execute("SELECT p.id,p.issue_key FROM jira_pool_items p JOIN jira_collections c USING(request_id) WHERE c.task_id=? AND c.config_json=? AND p.state='pending' AND c.request_id<>?",
                           (row['task_id'], row['config_json'], row['request_id'])).fetchall():
        if item['issue_key'] not in found:
            db.execute("UPDATE jira_pool_items SET state='invalid' WHERE id=?", (item['id'],))
    # 自动采集只保留每个任务/工单的当前候选，复用 ID 保持分页和等待时间稳定。
    # 每次采集的结果/数量仍保留；派发和运行使用独立的不可变快照。
    for issue, (title, people_json, details_json) in found.items():
        old = db.execute("SELECT p.*,c.finished_at FROM jira_pool_items p JOIN jira_collections c USING(request_id) WHERE c.source='poll' AND c.task_id=? AND p.server_key=? AND p.issue_key=? AND c.sop_id IS ? ORDER BY p.id DESC LIMIT 1",
                         (row['task_id'], server, issue, row['sop_id'])).fetchone()
        state = 'pending' if issue_available(db, server, issue) else 'consumed'
        queued_at = (old['candidate_queued_at'] or old['finished_at']) if old and old['state'] == 'pending' else now
        if old:
            db.execute('UPDATE jira_pool_items SET request_id=?,title=?,state=?,people_json=?,candidate_queued_at=? WHERE id=?',
                       (row['request_id'], title, state, people_json, queued_at, old['id']))
        else:
            db.execute('INSERT INTO jira_pool_items(request_id,server_key,issue_key,title,state,people_json,candidate_queued_at) VALUES(?,?,?,?,?,?,?)',
                       (row['request_id'], server, issue, title, state, people_json, queued_at))
        db.execute('UPDATE jira_pool_items SET details_json=?,details_checked_at=? WHERE server_key=? AND issue_key=?',
                   (details_json, now, server, issue))


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
    from jira_sop import identity
    sop_id, sop_name = identity(body)
    encoded = json.dumps(config, sort_keys=True)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM jira_collections WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if (old['task_id'], old['group_id'], old['config_json'], old['source'], old['sop_id']) != (body['taskDefinitionId'], body['groupId'], encoded, body['source'], sop_id):
                raise ValueError('采集请求编号已用于其他配置。')
        else:
            from jira_accounts import usable
            usable(db, config.get("connectionId"), body["groupId"])
            db.execute('''INSERT INTO jira_collections(request_id,task_id,group_id,name,source,config_json,state,created_at,sop_id,sop_name)
                VALUES(?,?,?,?,?,?,'waiting',?,?,?)''', (request_id, body['taskDefinitionId'], body['groupId'], name, body['source'], encoded, utc_now(), sop_id, sop_name))
        current = db.execute('SELECT * FROM jira_collections WHERE request_id=?', (request_id,)).fetchone()
        if current['state'] == 'waiting':
            materialize_dispatch(db, current)
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
        if not batch or batch['state'] != 'ready' or batch['source'] == 'poll':
            raise ValueError('采集尚未完成或来源已失效。')
        from jira_accounts import usable
        if not old or old['state'] != 'bound':
            account = usable(db, json.loads(batch['config_json']).get('connectionId'), batch['group_id'])
            if batch['account_alias'] != account['executor_alias']:
                raise ValueError('执行机 Jira 账号映射已变化，请重新触发采集。')
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
                'server': old['server_key'], 'accountId': batch['account_id'],
                'connectionId': json.loads(batch['config_json']).get('connectionId'), 'accountAlias': batch['account_alias'],
                **({'people': json.loads(old['people_json'])} if old['people_json'] else {})}


def bind(db, spec):
    """与工作流创建使用同一事务；预约同时成为既有领取锁。"""
    binding = spec.get('jiraReservation')
    if not spec['jira'].get('connectionId'):
        return  # 历史存储契约；网关拒绝新增旧格式运行。
    from jira_accounts import usable
    account = usable(db, spec['jira'].get('connectionId'), spec.get('groupId'))
    if spec.get('jiraRetryOf'):
        from workflow_allocation import decode
        source = db.execute('SELECT w.spec_json,w.spec_zlib FROM workflows w JOIN workflow_task_bindings b USING(workflow_id) WHERE w.workflow_id=? AND b.task_definition_id=?', (spec['jiraRetryOf'], spec.get('taskDefinitionId'))).fetchone()
        original = decode(source) if source else {}
        if not binding or original.get('jiraReservation') != binding or original.get('jira') != spec['jira'] or original.get('sopId') != spec.get('sopId'):
            raise ValueError('重试必须沿用原运行的账号和固定工单快照。')
        return
    if not binding:
        raise ValueError('新 Jira 运行必须先预约工单。')
    item = db.execute('''SELECT p.*,c.task_id,c.group_id,c.config_json,c.account_id,c.account_alias,c.sop_id FROM jira_pool_items p
        JOIN jira_collections c USING(request_id) WHERE reservation_id=?''', (binding['reservationId'],)).fetchone()
    if (not item or item['state'] != 'reserved' or item['task_id'] != spec.get('taskDefinitionId')
            or item['sop_id'] != spec.get('sopId')
            or item['group_id'] != spec.get('groupId') or json.loads(item['config_json']) != spec['jira'] or item['issue_key'] != binding['issueKey']
            or item['server_key'] != binding['server'] or item['account_id'] != binding['accountId']
            or binding.get('connectionId') != account['id'] or binding.get('accountAlias') != item['account_alias']
            or item['account_alias'] != account['executor_alias']
            or ('title' in binding and item['title'] != binding['title'])
            or ('people' in binding and json.loads(item['people_json'] or 'null') != binding['people'])):
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
    await asyncio.to_thread(collect_sync, gateway.store, request_id)


def collect_sync(store, request_id):
    from workflow_store import utc_now
    from jira_accounts import usable
    from jira_http import JiraClient, JiraHttpError
    from jira_runtime import excluding_query
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM jira_collections WHERE request_id=? AND state='waiting'", (request_id,)).fetchone()
        if not row:
            return
        if row['source'] != 'poll':
            materialize_dispatch(db, row)
            return
        db.execute("UPDATE jira_collections SET state='collecting' WHERE request_id=?", (request_id,))
    try:
        config = json.loads(row['config_json'])
        with store._connect() as db:
            account = dict(usable(db, config.get('connectionId'), row['group_id']))
        client = JiraClient(account['server'], account['token'])
        if client.identity() != account['identity']:
            raise ValueError('Jira 实际账号与检测身份不一致。')
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = usable(db, account['id'], row['group_id'])
            if current['version'] != account['version']:
                raise ValueError('采集期间账号发生变化，请重新触发。')
            shared = db.execute("SELECT * FROM jira_collections WHERE source='poll' AND config_json=? AND state='ready' AND finished_at>=? AND account_id=? AND account_alias=? ORDER BY finished_at DESC LIMIT 1",
                                (row['config_json'],row['created_at'],account['identity'],account['executor_alias'])).fetchone()
            if shared:
                if db.execute("UPDATE jira_collections SET state='ready',server_key=?,account_id=?,account_alias=?,count=?,finished_at=? WHERE request_id=? AND state='collecting'",
                              (account['server'],account['identity'],account['executor_alias'],shared['count'],shared['finished_at'],request_id)).rowcount:
                    shared_items = db.execute('SELECT * FROM jira_pool_items WHERE request_id=?', (shared['request_id'],)).fetchall()
                    publish_poll(db, row, account['server'], {item['issue_key']: (item['title'], item['people_json'], item['details_json']) for item in shared_items})
                return
        try:
            field = client.developer_field()
        except JiraHttpError:
            field = None  # 人员展示信息不可用不改变工单派发资格。
        found = {}
        while True:
            page = client.search(excluding_query(config['jql'], list(found)), field)
            if not page:
                break
            if any(key in found for key in page) or len(found) + len(page) > 1000:
                raise ValueError('采集超过 1000 项或分页未前进，请缩小 SOP 查询范围。')
            found.update(page)
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = usable(db, account['id'], row['group_id'])
            if current['version'] != account['version']:
                raise ValueError('采集期间账号发生变化，请重新触发。')
            if not db.execute("UPDATE jira_collections SET state='ready',server_key=?,account_id=?,account_alias=?,count=?,finished_at=? WHERE request_id=? AND state='collecting'",
                              (account['server'],account['identity'],account['executor_alias'],len(found),utc_now(),request_id)).rowcount:
                return
            publish_poll(db, row, account['server'], {issue: (title, json.dumps(people), json.dumps(details)) for issue, (title, people, details) in found.items()})
    except (ValueError, JiraHttpError) as error:
        with store._connect() as db:
            db.execute("UPDATE jira_collections SET state='failed',reason=?,finished_at=? WHERE request_id=? AND state='collecting'", (str(error),utc_now(),request_id))


def board(store, group_id='', before=None, limit=50, project='', task='', state='', sop_id=''):
    if sop_id:
        uuid.UUID(sop_id)
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
            (SELECT MIN(all_items.id) FROM jira_pool_items all_items WHERE all_items.server_key=p.server_key AND all_items.issue_key=p.issue_key) AS issue_id,
            MAX(c.finished_at) OVER issue AS collected_at,
            MIN(CASE WHEN p.state IN ('pending','reserved') AND c.state='ready'
                THEN COALESCE(p.candidate_queued_at,c.finished_at,c.created_at) END) OVER issue AS queued_at,
            FIRST_VALUE(p.title) OVER (PARTITION BY p.server_key,p.issue_key ORDER BY c.finished_at DESC,p.id DESC) AS latest_title,
            FIRST_VALUE(p.details_json) OVER (PARTITION BY p.server_key,p.issue_key
                ORDER BY (json_extract(p.details_json,'$.issueType') IS NOT NULL
                    OR json_extract(p.details_json,'$.priority') IS NOT NULL) DESC,
                    (p.details_json IS NOT NULL) DESC,p.details_checked_at DESC,p.id DESC) AS latest_details,
            FIRST_VALUE(p.people_json) OVER (PARTITION BY p.server_key,p.issue_key
                ORDER BY COALESCE(json_extract(p.people_json,'$.collectedAt'),c.finished_at) DESC,p.id DESC) AS latest_people,
            ROW_NUMBER() OVER (PARTITION BY p.server_key,p.issue_key ORDER BY
                CASE
                    WHEN w.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                        SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=p.workflow_id) THEN 0
                    WHEN p.state='reserved' AND c.state='ready' THEN 1
                    WHEN p.state='pending' AND c.state='ready' THEN 2
                    WHEN p.state<>'consumed' THEN 3 ELSE 4
                END,c.finished_at DESC,p.id DESC) AS position
        FROM jira_pool_items p JOIN jira_collections c USING(request_id)
        LEFT JOIN workflows w ON w.workflow_id=p.workflow_id
        WHERE (:sop_id='' OR c.sop_id=:sop_id OR (c.sop_id IS NULL AND EXISTS (
            SELECT 1 FROM workflow_jira_runs j JOIN workflow_sop_context s USING(workflow_id)
            JOIN workflow_task_bindings t USING(workflow_id) JOIN workflow_allocations a USING(workflow_id)
            WHERE j.server_key=p.server_key AND j.issue_key=p.issue_key AND s.sop_id=:sop_id
                AND t.task_definition_id=c.task_id AND a.group_id=c.group_id)))
        WINDOW issue AS (PARTITION BY p.server_key,p.issue_key)
    ), current_items AS (
        SELECT r.*,CASE
                WHEN :sop_id<>'' THEN (
                    SELECT j.workflow_id FROM workflow_jira_runs j JOIN workflows sw USING(workflow_id)
                    JOIN workflow_sop_context ss USING(workflow_id)
                    WHERE j.server_key=r.server_key AND j.issue_key=r.issue_key AND ss.sop_id=:sop_id
                        AND (r.state NOT IN ('pending','reserved','invalid','cancelled')
                            OR sw.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                                SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=sw.workflow_id))
                    ORDER BY (sw.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                        SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=sw.workflow_id)) DESC,
                        sw.created_at DESC,j.rowid DESC LIMIT 1)
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
        SELECT r.issue_id AS id,r.request_id,r.server_key,r.issue_key,r.latest_title AS title,r.latest_people AS people_json,r.latest_details AS details_json,
            CASE WHEN w.workflow_id IS NOT NULL THEN 'bound' WHEN :sop_id<>'' AND r.state='bound' THEN 'consumed' ELSE r.state END AS state,
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
                    AND (:task='' OR c.name LIKE :task_pattern) AND (:sop_id='' OR c.sop_id=:sop_id OR EXISTS (
                        SELECT 1 FROM workflow_jira_runs j JOIN workflow_sop_context s USING(workflow_id)
                        JOIN workflow_task_bindings t USING(workflow_id) JOIN workflow_allocations a USING(workflow_id)
                        WHERE t.task_definition_id=c.task_id AND a.group_id=c.group_id AND j.server_key=b.server_key AND j.issue_key=b.issue_key AND s.sop_id=:sop_id)))
    ) '''
    params = {'group_id': group_id, 'project': project, 'task': task, 'task_pattern': '%'+task+'%',
              'sop_id': sop_id, 'state': state, 'before': before, 'limit': limit + 1}
    with store._connect() as db:
        # 页面、汇总与来源读取同一快照，避免采集或绑定途中出现不一致。
        db.execute('BEGIN')
        rows = db.execute(query + '''SELECT * FROM filtered WHERE (:before IS NULL OR id<:before)
            ORDER BY id DESC LIMIT :limit''', params).fetchall()
        collections = db.execute('''SELECT request_id AS requestId,name,group_id AS groupId,sop_id AS sopId,sop_name AS sopName,source,state,reason,count,
            created_at AS createdAt,finished_at AS finishedAt FROM jira_collections
            WHERE (?='' OR group_id=?) AND (?='' OR sop_id=?) ORDER BY created_at DESC LIMIT 50''', (group_id, group_id, sop_id, sop_id)).fetchall()
        projects = db.execute(query + '''SELECT substr(issue_key,1,instr(issue_key,'-')-1) AS project,
            MAX(collected_at) AS lastCollectedAt,
            SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN run_status='running' THEN 1 ELSE 0 END) AS running
            FROM filtered GROUP BY substr(issue_key,1,instr(issue_key,'-')-1) ORDER BY project''', params).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            details = item.pop('details_json')
            if sop_id:
                # Jira 属性来自最新采集，与执行结果的 SOP 范围独立。
                latest = db.execute("""SELECT details_json FROM jira_pool_items
                    WHERE server_key=? AND issue_key=? AND details_json IS NOT NULL
                    ORDER BY details_checked_at DESC,id DESC LIMIT 1""", (row['server_key'], row['issue_key'])).fetchone()
                if latest:
                    details = latest[0]
            item.update(json.loads(details or '{}'))
            item.setdefault('issueType', None)
            item.setdefault('priority', None)
            people_value = item.pop('people_json')
            if people_value:
                item['people'] = json.loads(people_value)
            termination = json.loads(item.pop('termination_json') or '{}')
            item['outcome'] = termination.get('outcome')
            item['reason'] = public_text(termination.get('reason'))
            item['stop_pending'] = bool(item['stop_pending'])
            item['sources'] = [dict(source) for source in db.execute('''SELECT taskId,groupId,name,sopId,sopName FROM (
                SELECT c.task_id AS taskId,c.group_id AS groupId,c.name,c.sop_id AS sopId,c.sop_name AS sopName,
                    ROW_NUMBER() OVER (PARTITION BY c.task_id,c.group_id,c.sop_id ORDER BY c.finished_at DESC,p.id DESC) AS position
                FROM jira_pool_items p JOIN jira_collections c USING(request_id)
                WHERE p.server_key=? AND p.issue_key=? AND (?='' OR c.group_id=?)
            ) WHERE position=1 ORDER BY name,taskId,groupId''',
                (row['server_key'], row['issue_key'], group_id, group_id))]
            from jira_sop import current_sources, context
            item['currentSources'] = current_sources(db, row['server_key'], row['issue_key'], group_id)
            if sop_id:
                item['sources'] = [s for s in item['sources'] if s['sopId'] == sop_id]
                item['currentSources'] = [s for s in item['currentSources'] if s['sopId'] == sop_id]
            item['currentSop'] = context(db, item['workflow_id'])
            item['occupiedByOtherSop'] = None
            if sop_id:
                occupied = db.execute("""SELECT w.workflow_id FROM jira_issue_claims c
                    JOIN workflows w USING(workflow_id) LEFT JOIN workflow_sop_context s USING(workflow_id)
                    WHERE c.server_key=? AND c.issue_key=? AND (s.sop_id IS NULL OR s.sop_id<>?)
                    AND (w.status NOT IN ('completed','failed','cancelled') OR EXISTS (
                        SELECT 1 FROM workflow_machine_claims m WHERE m.workflow_id=w.workflow_id))""",
                    (row['server_key'], row['issue_key'], sop_id)).fetchone()
                if occupied:
                    item['occupiedByOtherSop'] = context(db, occupied[0])
            item['reworks'] = rework_history(db, row['server_key'], row['issue_key'])
            item['reworkCount'] = len(item['reworks'])
            item['executions'] = execution_history(db, row['server_key'], row['issue_key'])
            if sop_id:
                item['executions'] = [e for e in item['executions'] if e['sopId'] == sop_id]
                item['reworks'] = [e for e in item['reworks'] if context(db, e['workflowId'])['sopId'] == sop_id]
                item['reworkCount'] = len(item['reworks'])
            item['executionCount'] = len(item['executions'])
            item['retryCount'] = max(0, len(item['executions']) - 1)
            from workflow_jira import execution_result
            item['executionResult'] = execution_result(db, item['workflow_id'])
            items.append(item)
        from jira_sop import options
        return {'sops': options(db, group_id), 'projects': [dict(r) for r in projects], 'items': items, 'collections': [dict(r) for r in collections],
                'nextCursor': rows[limit - 1]['id'] if len(rows) > limit else None}


async def tick(gateway):
    with gateway.store._connect() as db:
        rows = db.execute("SELECT request_id FROM jira_collections WHERE state='waiting' AND source='poll' ORDER BY created_at LIMIT 10").fetchall()
    for row in rows:
        await collect(gateway, row['request_id'])
    from jira_issue_details import backfill
    await asyncio.to_thread(backfill, gateway.store)


def recover(store):
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE jira_collections SET state='cancelled',reason='旧来源未绑定中央账号，请使用最新配置重新触发。' WHERE state IN ('waiting','collecting','ready') AND json_extract(config_json,'$.connectionId') IS NULL")
        db.execute("UPDATE jira_pool_items SET state='cancelled' WHERE state IN ('pending','reserved') AND request_id IN (SELECT request_id FROM jira_collections WHERE state='cancelled')")
        db.execute("UPDATE jira_collections SET state='failed',reason='采集过程因服务重启中断，请重新触发。' WHERE state='collecting'")


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
    sop_counts = {}
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
        from jira_sop import context
        entry.update(context(db, entry['workflowId']))
        entry['executionNumber'] = len(executions) + 1
        sop = entry['sopId']
        entry['sopRetryNumber'] = sop_counts.get(sop, 0) if sop else None
        if sop:
            sop_counts[sop] = entry['sopRetryNumber'] + 1
        entry['retryNumber'] = len(executions)
        from workflow_jira import execution_result
        entry['executionResult'] = execution_result(db, entry['workflowId'])
        executions.append(entry)
    return executions


def rework_history(db, server, issue):
    from workflow_monitor import public_text
    return [{**dict(row), 'reason': public_text(row['reason'])} for row in db.execute('''SELECT workflow_id AS workflowId,started_at AS startedAt,
        previous_workflow_id AS previousWorkflowId,reason FROM jira_execution_history
        WHERE server_key=? AND issue_key=? AND previous_workflow_id IS NOT NULL ORDER BY started_at''', (server, issue))]


def verify_issue(store, config, group, binding, *, existing=False):
    from jira_accounts import usable
    from jira_http import JiraClient
    from jira_runtime import including_query
    with store._connect() as db:
        account = dict(usable(db, config.get('connectionId'), group, existing=existing))
    if account['id'] != binding.get('connectionId') or account['server'] != binding['server'] or account['identity'] != binding['accountId']:
        raise ValueError('Jira 账号与冻结身份不一致。')
    client = JiraClient(account['server'], account['token'])
    if client.identity() != binding['accountId']:
        raise ValueError('Jira 实际账号与冻结身份不一致。')
    if binding['issueKey'] not in client.search(including_query(config['jql'], binding['issueKey'])):
        raise ValueError('固定工单已不符合查询条件，本轮停止，不更换工单。')


async def verify_reservation(gateway, binding):
    with gateway.store._connect() as db:
        row = db.execute('SELECT p.state,c.config_json,c.group_id,p.request_id FROM jira_pool_items p JOIN jira_collections c USING(request_id) WHERE reservation_id=?', (binding['reservationId'],)).fetchone()
    if row['state'] == 'bound':
        return True
    try:
        await asyncio.to_thread(verify_issue, gateway.store, json.loads(row['config_json']), row['group_id'], binding)
    except ValueError:
        cancel(gateway.store, row['request_id'])
        raise
    return True


def prepare_run(store, spec):
    import hashlib
    if not spec.get('jira'):
        return
    context = store.jira_context(spec['workflowId'])
    if context['state'] != 'waiting_claim':
        return
    binding = context['reservation']
    if not binding:
        raise ValueError('历史 Jira 快照缺少中央预约，请使用最新配置运行。')
    verify_issue(store, spec['jira'], spec['groupId'], binding, existing=True)
    result = store.jira_update(spec['workflowId'], 'claim', {'server': binding['server'], 'account': binding['accountAlias'],
        'fingerprint': hashlib.sha256(json.dumps([binding['connectionId'],binding['accountId'],binding['server'],binding['accountAlias']]).encode()).hexdigest(),
        'candidates': [binding['issueKey']]}, central_verified=True)
    if result['state'] != 'claimed':
        raise ValueError('固定工单已被其他运行占用，本轮停止。')


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
        from workflow_jira import execution_result
        from jira_sop import context
        return {**context(db, workflow_id), 'executionResult': execution_result(db, workflow_id), 'workflowId': workflow_id, 'status': row['status'], 'summary': public_text(row['response']),
                'reason': public_text(termination.get('reason')), 'steps': steps, 'deliveries': deliveries,
                'allocation': allocation_snapshot(db, workflow_id), 'resources': resource_snapshot(db, workflow_id)}
