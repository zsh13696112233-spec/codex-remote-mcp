"""仅补齐工单池展示字段，不授予派发资格或改写运行快照。"""
import json
import logging
from datetime import datetime, timedelta, timezone

from jira_accounts import usable
from jira_http import JiraClient, JiraHttpError
from workflow_jira import ISSUE_KEY
from workflow_store import utc_now

LOGGER = logging.getLogger(__name__)


def backfill(store):
    # 每轮最多处理同分组、同服务的 50 张工单；失败批次延后，避免阻塞后续工单。
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    eligible = '''a.deleted_at IS NULL AND a.enabled=1 AND a.test_status='passed'
        AND a.identity IS NOT NULL AND a.token IS NOT NULL
        AND a.username IS NOT NULL AND a.executor_alias=a.username'''
    pending = '''p.details_json IS NULL AND
        (p.details_checked_at IS NULL OR p.details_checked_at<?)'''
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        target = db.execute(f'''SELECT c.group_id,p.server_key FROM jira_pool_items p
            JOIN jira_collections c USING(request_id)
            WHERE {pending} AND EXISTS (SELECT 1 FROM jira_accounts a
                WHERE a.group_id=c.group_id AND a.server=p.server_key AND {eligible})
            ORDER BY COALESCE(p.details_checked_at,''),p.id LIMIT 1''', (cutoff,)).fetchone()
        if not target:
            return
        group, server = target
        accounts = [dict(a) for a in db.execute(f'''SELECT * FROM jira_accounts a
            WHERE a.group_id=? AND a.server=? AND {eligible} ORDER BY a.id''', (group, server))]
        keys = [r[0] for r in db.execute(f'''SELECT DISTINCT p.issue_key FROM jira_pool_items p
            JOIN jira_collections c USING(request_id) WHERE {pending} AND c.group_id=?
            AND p.server_key=? ORDER BY p.issue_key LIMIT 50''', (cutoff, group, server))]
        marks = ','.join('?' for _ in keys)
        now = utc_now()
        db.execute(f'''UPDATE jira_pool_items SET details_checked_at=? WHERE details_json IS NULL
            AND server_key=? AND issue_key IN ({marks}) AND request_id IN
            (SELECT request_id FROM jira_collections WHERE group_id=?)''', (now, server, *keys, group))
    if any(not ISSUE_KEY.fullmatch(key) for key in keys):
        raise ValueError('历史 Jira 工单编号格式无效。')
    found, failed = {}, False
    for account in accounts:
        if len(found) == len(keys):
            break
        try:
            client = JiraClient(server, account['token'])
            if client.identity() != account['identity']:
                raise ValueError('Jira 实际账号与检测身份不一致。')
            page_results = {}
            while True:
                remaining = set(keys) - found.keys() - page_results.keys()
                if not remaining:
                    break
                page = client.search('key in (' + ','.join('"' + key + '"' for key in sorted(remaining)) + ')')
                if not page:
                    break
                if not set(page) <= remaining:
                    raise JiraHttpError('Jira 补齐查询返回了非请求工单。')
                page_results.update({key: value[2] for key, value in page.items()})
            with store._connect() as db:
                current = usable(db, account['id'], group)
                if current['version'] != account['version']:
                    raise ValueError('补齐期间 Jira 账号发生变化。')
            found.update(page_results)
        except (ValueError, JiraHttpError) as error:
            failed = True
            LOGGER.warning('Jira 工单类型与优先级补齐失败：%s', error)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for account in accounts:
                current = usable(db, account['id'], group)
                if current['version'] != account['version']:
                    raise ValueError('补齐期间 Jira 账号发生变化。')
        except ValueError as error:
            LOGGER.warning('Jira 工单类型与优先级补齐未保存：%s', error)
            return
        for key, details in found.items():
            # 自动采集可能已写入更新的值，补齐不得覆盖。
            db.execute('''UPDATE jira_pool_items SET details_json=?,details_checked_at=?
                WHERE server_key=? AND issue_key=? AND details_json IS NULL''',
                       (json.dumps(details), now, server, key))
        if not failed:
            missing = json.dumps({'issueType': None, 'priority': None})
            db.execute(f'''UPDATE jira_pool_items SET details_json=? WHERE details_json IS NULL
                AND server_key=? AND issue_key IN ({marks}) AND request_id IN
                (SELECT request_id FROM jira_collections WHERE group_id=?)''', (missing, server, *keys, group))
