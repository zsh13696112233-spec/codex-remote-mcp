"""账号提醒使用的只读采集汇总，不把派发余量当作查询结果。"""
import hashlib
import json
import uuid
from datetime import datetime, timezone


def collection_status(store, account_id, *, now=None):
    account_id = str(uuid.UUID(account_id))
    now = now or datetime.now(timezone.utc)
    with store._connect() as db:
        db.execute('BEGIN')
        account = db.execute('SELECT enabled,test_status,version,deleted_at FROM jira_accounts WHERE id=?',
                             (account_id,)).fetchone()
        if account is None:
            raise ValueError('找不到 Jira 账号。')
        scopes = [dict(row) for row in db.execute(
            'SELECT * FROM jira_polling_scopes ORDER BY task_id')
            if json.loads(row['config_json']).get('connectionId') == account_id]
        fingerprint = hashlib.sha256(json.dumps([account['version'], scopes], sort_keys=True).encode()).hexdigest()
        result = dict(state='unknown', scopeVersion=fingerprint, collectedAt=None,
                      lastNonemptyAt=None, scopeCount=len(scopes))
        if account['deleted_at']:
            result['state'] = 'deleted'
            return result
        if not account['enabled'] or account['test_status'] != 'passed':
            result['state'] = 'disabled'
            return result
        if not scopes:
            result['state'] = 'unconfigured'
            return result
        completed = []
        nonempty = []
        uncertain = False
        has_work = False
        for scope in scopes:
            parameters = (scope['task_id'], scope['group_id'], scope['config_json'], scope['sop_id'])
            base = """FROM jira_collections WHERE source='poll' AND task_id=? AND group_id=?
                      AND config_json=? AND sop_id IS ?"""
            rows = db.execute("SELECT state,count,finished_at " + base + """
                AND state IN ('ready','failed') ORDER BY created_at DESC,request_id DESC LIMIT 1""",
                parameters).fetchall()
            last_nonempty = db.execute("SELECT MAX(finished_at) " + base +
                " AND state='ready' AND count>0", parameters).fetchone()[0]
            if last_nonempty:
                nonempty.append(last_nonempty)
            if not rows or rows[0]['state'] != 'ready' or not rows[0]['finished_at']:
                uncertain = True
                continue
            latest = rows[0]
            timestamp = datetime.fromisoformat(latest['finished_at'].replace('Z', '+00:00'))
            if not 0 <= (now - timestamp).total_seconds() <= 120:
                uncertain = True
                continue
            completed.append(latest['finished_at'])
            has_work |= latest['count'] > 0
        result['lastNonemptyAt'] = max(nonempty) if nonempty else None
        result['collectedAt'] = min(completed) if len(completed) == len(scopes) else None
        result['state'] = 'available' if has_work else 'unknown' if uncertain else 'empty'
        return result
