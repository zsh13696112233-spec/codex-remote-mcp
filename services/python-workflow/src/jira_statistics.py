"""分发中心按工单最后结束结果统计，不修改历史数据。"""
import json
import uuid
from collections import Counter

from workflow_allocation import decode
from workflow_jira import execution_result
from workflow_store import utc_now


def statistics(store, group='', project='', *, items=False, category='', developer='', before=None, sop_id=''):
    if sop_id:
        uuid.UUID(sop_id)
    if group:
        uuid.UUID(group)
    if any(not isinstance(v, str) or len(v) > 160 for v in (project, category, developer)):
        raise ValueError('统计筛选参数无效。')
    if category not in ('', 'passed', 'other', 'completed_no_submission', 'rework_required', 'fully_submitted', 'limited_submitted', 'blocked', 'abnormal', 'cancelled', 'unknown', 'pending', 'submission_unknown', 'no_task'):
        raise ValueError('统计结果分类无效。')
    if before is not None and (type(before) is not int or before < 1):
        raise ValueError('分页参数无效。')
    with store._connect() as db:
        db.execute('BEGIN')
        rows = db.execute('''WITH issues AS (
          SELECT p.server_key,p.issue_key,MIN(p.id) AS id
          FROM jira_pool_items p JOIN jira_collections c USING(request_id)
          WHERE (?='' OR c.group_id=?) AND (?='' OR substr(p.issue_key,1,instr(p.issue_key,'-')-1)=?)
            AND (?='' OR c.sop_id=? OR EXISTS (SELECT 1 FROM workflow_jira_runs j
                JOIN workflow_sop_context s USING(workflow_id) WHERE j.server_key=p.server_key AND j.issue_key=p.issue_key AND s.sop_id=?))
          GROUP BY p.server_key,p.issue_key
        ), ended AS (
          SELECT j.server_key,j.issue_key,w.workflow_id,w.finished_at,
            ROW_NUMBER() OVER (PARTITION BY j.server_key,j.issue_key ORDER BY w.finished_at DESC,j.rowid DESC) AS position
          FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
          WHERE (?='' OR EXISTS (SELECT 1 FROM workflow_sop_context s WHERE s.workflow_id=w.workflow_id AND s.sop_id=?))
            AND w.status IN ('completed','failed','cancelled')
            AND (EXISTS(SELECT 1 FROM jira_execution_history h WHERE h.workflow_id=w.workflow_id)
              OR EXISTS(SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.job_id IS NOT NULL))
        ) SELECT i.*,e.workflow_id,e.finished_at FROM issues i LEFT JOIN ended e
          ON e.server_key=i.server_key AND e.issue_key=i.issue_key AND e.position=1 ORDER BY i.id DESC''',
          (group, group, project, project, sop_id, sop_id, sop_id, sop_id, sop_id)).fetchall()
        data = []
        for row in rows:
            sources = db.execute('''SELECT DISTINCT c.group_id FROM jira_pool_items p JOIN jira_collections c USING(request_id)
              WHERE p.server_key=? AND p.issue_key=? AND (?='' OR c.group_id=?)''',
              (row['server_key'], row['issue_key'], group, group)).fetchall()
            title = db.execute('SELECT title FROM jira_pool_items WHERE server_key=? AND issue_key=? ORDER BY id DESC LIMIT 1', (row['server_key'], row['issue_key'])).fetchone()[0]
            result = execution_result(db, row['workflow_id']) if row['workflow_id'] else None
            status, name = 'pending', '开发人未获取'
            flow, submission = None, 'unknown'
            if result:
                run = db.execute('SELECT status,spec_json,spec_zlib,jira_context_json FROM workflows WHERE workflow_id=?', (row['workflow_id'],)).fetchone()
                flow, submission = result['flowStatus'], result['submissionStatus']
                status = ({'full':'fully_submitted','limited':'limited_submitted','none':'completed_no_submission','unknown':'submission_unknown'}[submission]
                          if flow == 'passed' else flow or 'unknown')
                people = result.get('developers') or decode(run).get('jiraReservation', {}).get('people', {}).get('developers', [])
                if not people:
                    context = json.loads(run['jira_context_json'] or '{}')
                    if context.get('developerStatus') == 'resolved':
                        people = [context.get('developerName')]
                if len(people) == 1 and people[0]:
                    name = people[0]
            elif row['workflow_id'] is None:
                # 尚无结束结果时，按最近采集的开发人归属；已有结果仍使用该轮快照。
                candidate = db.execute('''SELECT p.people_json FROM jira_pool_items p
                  JOIN jira_collections c USING(request_id)
                  WHERE p.server_key=? AND p.issue_key=?
                  ORDER BY COALESCE(json_extract(p.people_json,'$.collectedAt'),c.finished_at) DESC,p.id DESC LIMIT 1''',
                  (row['server_key'], row['issue_key'])).fetchone()
                people = json.loads(candidate['people_json'] or '{}').get('developers', [])
                if len(people) == 1 and people[0]:
                    name = people[0]
            active = db.execute('''SELECT 1 FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
              WHERE j.server_key=? AND j.issue_key=? AND w.status NOT IN ('completed','failed','cancelled')
              AND (?='' OR EXISTS (SELECT 1 FROM workflow_sop_context s WHERE s.workflow_id=w.workflow_id AND s.sop_id=?)) LIMIT 1''', (row['server_key'],row['issue_key'],sop_id,sop_id)).fetchone()
            from jira_sop import context as sop_context
            data.append({**sop_context(db, row['workflow_id']), 'flowStatus':flow, 'submissionStatus':submission, 'id':row['id'],'issueKey':row['issue_key'],'title':title,'groupIds':[s[0] for s in sources],
                         'developer':name,'status':status,'workflowId':row['workflow_id'],'finishedAt':row['finished_at'],'reprocessing':bool(active)})
        generated = utc_now()
        from jira_sop import options
        metadata = {'sops': options(db, group), 'sopMetadataPending': db.execute('SELECT COUNT(*) FROM workflow_jira_runs j LEFT JOIN workflow_sop_context s USING(workflow_id) WHERE s.workflow_id IS NULL').fetchone()[0]}
        if items:
            selected = [r for r in data if (not developer or r['developer']==developer) and
                        (not category or (r['flowStatus']=='passed' if category=='passed' else
                         r['flowStatus'] not in ('passed','blocked') if category=='other' else r['status']==category)) and
                        (before is None or r['id']<before)]
            return {**metadata, 'items':selected[:50],'nextCursor':selected[49]['id'] if len(selected)>50 else None,'generatedAt':generated}
        groups = sorted({g for r in data for g in r['groupIds']})
        developers = sorted({r['developer'] for r in data})
        return {**metadata, 'summary':counts(data),'groups':[{'groupId':g,**counts([r for r in data if g in r['groupIds']])} for g in groups],
                'developers':[{'developer':d,**counts([r for r in data if r['developer']==d])} for d in developers], 'generatedAt':generated}


def counts(rows):
    values = Counter(r['status'] for r in rows)
    passed = sum(r['flowStatus'] == 'passed' for r in rows)
    blocked = sum(r['flowStatus'] == 'blocked' for r in rows)
    return {'total':len(rows), 'full':values['fully_submitted'], 'limited':values['limited_submitted'],
            'noSubmission':values['completed_no_submission'], 'submissionUnknown':values['submission_unknown'],
            'passed':passed, 'blocked':blocked, 'rateDenominator':passed + blocked,
            'other':len(rows)-passed-blocked, 'categories':dict(values)}
