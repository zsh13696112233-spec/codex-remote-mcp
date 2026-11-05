"""中央分组 Jira 账号。Token 仅保存在本表，不进入公开投影和运行快照。"""
import json
import sqlite3
import uuid

from jira_http import JiraClient, JiraHttpError
from workflow_jira import server_key


class AccountConflict(RuntimeError):
    pass


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS jira_accounts (
      id TEXT PRIMARY KEY, group_id TEXT NOT NULL, name TEXT NOT NULL,
      server TEXT NOT NULL, token TEXT, executor_alias TEXT NOT NULL,
      enabled INTEGER NOT NULL, test_status TEXT NOT NULL DEFAULT 'untested',
      identity TEXT, username TEXT, tested_at TEXT, test_message TEXT NOT NULL DEFAULT '',
      version INTEGER NOT NULL DEFAULT 1, deleted_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_jira_account_name
      ON jira_accounts(group_id,name) WHERE deleted_at IS NULL;
    ''')
    db.execute('BEGIN IMMEDIATE')
    if 'username' not in {row[1] for row in db.execute('PRAGMA table_info(jira_accounts)')}:
        db.execute('ALTER TABLE jira_accounts ADD COLUMN username TEXT')
    db.commit()


def public(row):
    return {key: row[column] for key, column in (
        ('id', 'id'), ('groupId', 'group_id'), ('name', 'name'), ('server', 'server'),
        ('testStatus', 'test_status'),
        ('identity', 'identity'), ('username', 'username'), ('testedAt', 'tested_at'), ('testMessage', 'test_message'),
        ('version', 'version'))} | {'enabled': bool(row['enabled']), 'tokenConfigured': bool(row['token']),
        'executorAlias': row['executor_alias'] if row['username'] == row['executor_alias'] else ''}


def get(db, identifier):
    row = db.execute('SELECT * FROM jira_accounts WHERE id=? AND deleted_at IS NULL', (identifier,)).fetchone()
    if row is None:
        raise ValueError('找不到 Jira 账号。')
    return row


def usable(db, identifier, group_id, *, existing=False):
    if not identifier:
        raise ValueError('旧 Jira 配置未绑定中央账号，请使用已绑定账号的最新配置。')
    row = get(db, identifier)
    if row['group_id'] != group_id:
        raise ValueError('Jira 账号与任务必须属于同一分组。')
    if not row['identity'] or not row['token'] or (not existing and (not row['enabled'] or row['test_status'] != 'passed')):
        raise ValueError('Jira 账号未启用或未通过检测。')
    if not existing and (not row['username'] or row['executor_alias'] != row['username']):
        raise ValueError('请重新检测 Jira 账号，自动映射执行机 Jira 账号。')
    return row


def listing(store, group_id=None):
    if group_id:
        uuid.UUID(group_id)
    with store._connect() as db:
        return {'accounts': [public(row) for row in db.execute(
            'SELECT * FROM jira_accounts WHERE deleted_at IS NULL AND (? IS NULL OR group_id=?) ORDER BY name,id',
            (group_id or None, group_id or None))]}


def version(row, value):
    if type(value) is not int or row['version'] != value:
        raise AccountConflict('账号已被其他操作修改，请刷新后重试。')


def text_field(body, key, limit):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('Jira 账号名称、地址或 Token 格式无效。')
    return value.strip()


def executor_account(profile):
    username = profile['username']
    if not username or len(username) > 128:
        raise JiraHttpError('未获取可用的登录账号名，无法自动映射执行机 Jira 账号。')
    return username


def save(store, identifier, body):
    from workflow_store import utc_now
    if not isinstance(body, dict) or set(body) - {'groupId', 'name', 'server', 'token', 'enabled', 'version'}:
        raise ValueError('Jira 账号请求格式无效。')
    group = text_field(body, 'groupId', 36)
    uuid.UUID(group)
    name = text_field(body, 'name', 100)
    server = server_key(text_field(body, 'server', 1000))
    enabled = body.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('账号启用状态无效。')
    token = body.get('token', '')
    if not isinstance(token, str) or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('Token 格式无效。')
    old = None
    if identifier:
        with store._connect() as db:
            old = dict(get(db, identifier))
        version(old, body.get('version'))
        if (group, server) != (old['group_id'], old['server']):
            raise ValueError('更换分组或 Jira 服务时请新建账号。')
        if token:
            profile = JiraClient(server, token).profile()
            identity = profile['identity']
            if old['identity'] and identity != old['identity']:
                raise ValueError('新 Token 属于不同的实际账号，旧 Token 已保留。')
            alias = executor_account(profile)
    elif not token:
        raise ValueError('新增账号必须填写 Token。')
    try:
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM agent_groups WHERE id=? AND deleted_at IS NULL', (group,)).fetchone():
                raise ValueError('找不到有效分组。')
            if old:
                version(get(db, identifier), body.get('version'))
                db.execute('UPDATE jira_accounts SET name=?,enabled=?,version=version+1 WHERE id=?',
                           (name, enabled, identifier))
                if token:
                    db.execute("UPDATE jira_accounts SET token=?,identity=?,username=?,executor_alias=?,test_status='passed',tested_at=?,test_message='' WHERE id=?",
                               (token, identity, profile['username'], alias, utc_now(), identifier))
            else:
                identifier = str(uuid.uuid4())
                db.execute('INSERT INTO jira_accounts(id,group_id,name,server,token,executor_alias,enabled) VALUES(?,?,?,?,?,?,?)',
                           (identifier, group, name, server, token, '', enabled))
            return public(get(db, identifier))
    except sqlite3.IntegrityError:
        raise AccountConflict('同一分组内的 Jira 账号名称不能重复。') from None


def detect(store, identifier, expected):
    from workflow_store import utc_now
    with store._connect() as db:
        row = dict(get(db, identifier))
    version(row, expected)
    identity, username, alias, status, message = row['identity'], row['username'], row['executor_alias'], 'passed', ''
    try:
        profile = JiraClient(row['server'], row['token']).profile()
        actual = profile['identity']
        if identity and actual != identity:
            raise JiraHttpError('检测到实际账号变化，请新建账号记录。')
        alias = executor_account(profile)
        identity, username = actual, profile['username']
    except JiraHttpError as error:
        status, message = 'failed', str(error)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        version(get(db, identifier), expected)
        db.execute('UPDATE jira_accounts SET identity=?,username=?,executor_alias=?,test_status=?,test_message=?,tested_at=?,version=version+1 WHERE id=?',
                   (identity, username, alias, status, message, utc_now(), identifier))
        return public(get(db, identifier))


def delete(store, identifier, expected):
    from workflow_store import utc_now
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        version(get(db, identifier), expected)
        if db.execute("SELECT 1 FROM jira_collections WHERE json_extract(config_json,'$.connectionId')=? AND state IN ('waiting','collecting','ready') AND (state<>'ready' OR EXISTS (SELECT 1 FROM jira_pool_items p WHERE p.request_id=jira_collections.request_id AND p.state IN ('pending','reserved'))) LIMIT 1", (identifier,)).fetchone():
            raise AccountConflict('账号仍有待派发来源，请先停止来源。')
        from workflow_allocation import decode
        for active in db.execute("SELECT spec_json,spec_zlib FROM workflows WHERE status NOT IN ('completed','failed','cancelled') OR EXISTS (SELECT 1 FROM workflow_machine_claims c WHERE c.workflow_id=workflows.workflow_id)"):
            if decode(active).get('jira', {}).get('connectionId') == identifier:
                raise AccountConflict('账号仍有未完成运行，请结束运行后再删除。')
        db.execute('UPDATE jira_accounts SET deleted_at=?,token=NULL,enabled=0,version=version+1 WHERE id=?', (utc_now(), identifier))
    return {'deleted': True}
