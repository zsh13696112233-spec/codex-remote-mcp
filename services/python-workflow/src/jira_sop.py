"""采集范围与冻结 SOP 身份；不修改业务快照或授予派发资格。"""
import uuid


def identity(value):
    if not isinstance(value, dict):
        raise ValueError('SOP 归属必须是对象。')
    identifier, name = value.get('sopId'), value.get('sopName')
    if identifier is None and name is None:
        return None, None
    if not isinstance(identifier, str) or not isinstance(name, str) or not name.strip() or len(name) > 1000:
        raise ValueError('SOP 编号或名称无效。')
    return str(uuid.UUID(identifier)), name


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS workflow_sop_context (
            workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
            sop_id TEXT NOT NULL, sop_name TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_workflow_sop ON workflow_sop_context(sop_id,workflow_id);
        CREATE TABLE IF NOT EXISTS jira_polling_scopes (
            task_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, sop_id TEXT, sop_name TEXT,
            config_json TEXT NOT NULL);
    ''')
    db.execute('BEGIN IMMEDIATE')
    columns = {r[1] for r in db.execute('PRAGMA table_info(jira_collections)')}
    for column in ('sop_id', 'sop_name'):
        if column not in columns:
            db.execute(f'ALTER TABLE jira_collections ADD COLUMN {column} TEXT')
    db.execute('CREATE INDEX IF NOT EXISTS idx_jira_collection_sop ON jira_collections(sop_id,group_id)')


def register(db, workflow_id, value):
    identifier, name = identity(value)
    if identifier is None:
        return
    old = db.execute('SELECT sop_id,sop_name FROM workflow_sop_context WHERE workflow_id=?', (workflow_id,)).fetchone()
    # 名称是首次登记时的展示快照；同一 SOP 改名不构成身份变更。
    if old and old['sop_id'] != identifier:
        raise ValueError('运行冻结的 SOP 身份不能修改。')
    db.execute('INSERT OR IGNORE INTO workflow_sop_context SELECT workflow_id,?,? FROM workflows WHERE workflow_id=?',
               (identifier, name, workflow_id))


def context(db, workflow_id):
    row = db.execute('SELECT sop_id AS sopId,sop_name AS sopName FROM workflow_sop_context WHERE workflow_id=?', (workflow_id,)).fetchone()
    return dict(row) if row else {'sopId': None, 'sopName': None}


def options(db, group=''):
    rows = db.execute('''SELECT sop_id,sop_name,finished_at AS observed FROM jira_collections WHERE sop_id IS NOT NULL AND (?='' OR group_id=?)
        UNION ALL SELECT s.sop_id,s.sop_name,w.finished_at AS observed FROM workflow_sop_context s JOIN workflow_allocations a USING(workflow_id) JOIN workflows w USING(workflow_id)
        WHERE (?='' OR a.group_id=?) ORDER BY observed''', (group, group, group, group))
    values = {}
    for row in rows:
        values[row['sop_id']] = {'sopId': row['sop_id'], 'sopName': row['sop_name']}
    return sorted(values.values(), key=lambda value: (value['sopName'], value['sopId']))


def current_sources(db, server, issue, group=''):
    return [dict(r) for r in db.execute('''SELECT c.task_id AS taskId,c.name,c.group_id AS groupId,c.sop_id AS sopId,c.sop_name AS sopName,
        c.finished_at AS checkedAt,
        (SELECT state FROM jira_collections x WHERE x.task_id=c.task_id AND x.group_id=c.group_id AND x.config_json=c.config_json AND x.sop_id IS c.sop_id AND x.source='poll'
          ORDER BY x.created_at DESC,x.rowid DESC LIMIT 1) AS collectionState
        FROM jira_pool_items p JOIN jira_collections c USING(request_id)
        JOIN jira_polling_scopes s ON s.task_id=c.task_id AND s.group_id=c.group_id AND s.config_json=c.config_json AND s.sop_id IS c.sop_id
        WHERE p.server_key=? AND p.issue_key=? AND (?='' OR c.group_id=?) AND c.source='poll'
        AND c.request_id=(SELECT x.request_id FROM jira_collections x WHERE x.task_id=s.task_id AND x.group_id=s.group_id
            AND x.config_json=s.config_json AND x.sop_id IS s.sop_id AND x.source='poll' AND x.state='ready'
            ORDER BY x.finished_at DESC,x.rowid DESC LIMIT 1)
        ORDER BY c.name''', (server, issue, group, group))]
