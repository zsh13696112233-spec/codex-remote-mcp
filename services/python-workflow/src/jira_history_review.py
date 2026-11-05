"""历史结果的人工核对补充；不修改冻结协议、步骤或原始回执。"""
import hashlib
import json
import zlib
from datetime import datetime, timezone


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_jira_history_reviews (
        workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
        status TEXT, commits_json TEXT NOT NULL, developers_json TEXT NOT NULL,
        developer_source TEXT NOT NULL, note TEXT NOT NULL, evidence_json TEXT NOT NULL,
        source_hash TEXT NOT NULL, reviewed_at TEXT NOT NULL
    )""")


def source(db, workflow_id, *, allow_manual_completion=False):
    row = db.execute("""SELECT w.workflow_id,w.status,w.termination_json,w.jira_context_json,
        w.finished_at,w.response,w.error,w.spec_json,w.spec_zlib,j.delivery_version
        FROM workflows w JOIN workflow_jira_runs j USING(workflow_id)
        WHERE workflow_id=?""", (workflow_id,)).fetchone()
    if row is None or (row['delivery_version'] != 1 and not allow_manual_completion) or row['status'] not in ('completed', 'failed', 'cancelled'):
        raise ValueError('只能核对已结束的 Jira 运行；新协议仅允许人工无提交或审核返工分类。')
    value = dict(row)
    value['spec'] = json.loads(zlib.decompress(value.pop('spec_zlib')) if row['spec_zlib'] else value['spec_json'])
    value.pop('spec_json')
    delivery_fields = 'node_id,attempt,status,reference,summary'
    if allow_manual_completion:
        delivery_fields += ',submission_type,commits_json'
    value['deliveries'] = [dict(r) for r in db.execute(
        f'SELECT {delivery_fields} FROM workflow_jira_deliveries WHERE workflow_id=? ORDER BY node_id,attempt', (workflow_id,))]
    value['steps'] = [dict(r) for r in db.execute(
        'SELECT node_id,status,response,error FROM workflow_nodes WHERE workflow_id=? ORDER BY position', (workflow_id,))]
    value['cancellations'] = [json.loads(zlib.decompress(r['payload_zlib']) if r['payload_zlib'] else r['payload_json']) for r in db.execute(
        "SELECT payload_json,payload_zlib FROM workflow_events WHERE workflow_id=? AND event_type='workflow.cancelled' ORDER BY sequence", (workflow_id,))]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate(value):
    termination = json.loads(value['termination_json'] or '{}')
    submitted = any(d['status'] in ('submitted', 'unknown') for d in value['deliveries'])
    status, note = 'unknown', '历史结果尚不能归入新分类，需核对原始记录。'
    if termination.get('outcome') == 'no_task' and not submitted:
        status, note = None, '无可领取任务，不计入提交或 Bug 处理量。'
    elif termination.get('outcome') == 'blocked' and not submitted:
        status, note = 'blocked', '原正式结果明确业务阻断，未发现提交或提交结果不明回执。'
    elif value['status'] == 'failed':
        status, note = 'abnormal', '原运行执行失败，未形成正常提交结束结论。'
    elif value['status'] == 'cancelled':
        manual = any(e.get('reason') in ('user_requested', 'chat_control') for e in value['cancellations'])
        status, note = (None, '人工取消，独立保留。') if manual else ('abnormal', '原运行中断，未发现人工取消记录。')
    people = value['spec'].get('jiraReservation', {}).get('people') or {}
    developers, developer_source = people.get('developers', []), 'reservation'
    if not developers:
        context = json.loads(value['jira_context_json'] or '{}')
        developers = [context['developerName']] if context.get('developerStatus') == 'resolved' and context.get('developerName') else []
        developer_source = 'formal_result' if developers else 'unavailable'
    return {'workflowId': value['workflow_id'], 'status': status, 'commits': [],
            'developers': developers, 'developerSource': developer_source,
            'note': note, 'evidence': ['termination', 'deliveries', 'steps'], 'sourceHash': fingerprint(value)}


def apply_reviews(db, reviews):
    """调用方开启事务；任一记录过期或冲突时整体回滚，不覆盖既有核对。"""
    initialize(db)
    inserted = 0
    for review in reviews:
        value = source(db, review['workflowId'], allow_manual_completion=review['status'] in ('completed_no_submission', 'rework_required'))
        if fingerprint(value) != review['sourceHash']:
            raise ValueError('历史证据已变化，请重新核对。')
        status = review['status']
        if status not in (None, 'unknown', 'blocked', 'abnormal', 'fully_submitted', 'limited_submitted', 'completed_no_submission', 'rework_required'):
            raise ValueError('历史分类无效。')
        if status in ('completed_no_submission', 'rework_required'):
            allowed = {'completed', 'not_applicable', status}
            if status == 'rework_required':
                allowed.add('not_submitted')
            if (value['status'] != 'completed'
                    or json.loads(value['termination_json'] or '{}').get('outcome')
                    or review['commits']
                    or not value['deliveries']
                    or any(d['status'] not in allowed
                           or d['submission_type'] or json.loads(d['commits_json'] or '[]')
                           for d in value['deliveries'])):
                raise ValueError('仅正常结束且无提交、无待核实或冲突回执的运行可人工标记本分类。')
            if not isinstance(review['note'], str) or not review['note'].strip():
                raise ValueError('人工结束分类必须填写核对说明。')
        if status in ('fully_submitted', 'limited_submitted'):
            from workflow_jira import validate_delivery
            validate_delivery({'status': 'submitted', 'reference': '历史核对', 'summary': review['note'],
                               'submissionType': 'full' if status == 'fully_submitted' else 'limited',
                               'commits': review['commits']}, 2)
            if value['status'] != 'completed' or json.loads(value['termination_json'] or '{}').get('outcome'):
                raise ValueError('非正常完成的历史运行不能补为提交成功。')
            if not any(d['status'] == 'submitted' for d in value['deliveries']):
                raise ValueError('历史提交分类必须有提交回执。')
        if status == 'blocked' and review['commits']:
            raise ValueError('阻断结果不能同时记录已提交。')
        old = db.execute('SELECT source_hash,status,commits_json FROM workflow_jira_history_reviews WHERE workflow_id=?', (review['workflowId'],)).fetchone()
        if old:
            if old['source_hash'] != review['sourceHash'] or old['status'] != status or json.loads(old['commits_json']) != review['commits']:
                raise ValueError('已有不同核对结果，拒绝覆盖。')
            continue
        db.execute('INSERT INTO workflow_jira_history_reviews (workflow_id,status,commits_json,developers_json,developer_source,note,evidence_json,source_hash,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (review['workflowId'], status, json.dumps(review['commits'], ensure_ascii=False),
                    json.dumps(review['developers'], ensure_ascii=False), review['developerSource'], review['note'],
                    json.dumps(review['evidence'], ensure_ascii=False), review['sourceHash'], datetime.now(timezone.utc).isoformat()))
        inserted += 1
    return inserted
