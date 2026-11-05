"""领取中心按工单最后结束结果统计，不修改历史数据。"""
import json
import uuid
from collections import Counter

from workflow_allocation import decode
from workflow_jira import execution_result
from workflow_store import utc_now


def statistics(store, group='', project='', *, items=False, category='', developer='', before=None):
    if group:
        uuid.UUID(group)
    if any(not isinstance(v, str) or len(v) > 160 for v in (project, category, developer)):
        raise ValueError('统计筛选参数无效。')
    if category not in ('', 'passed', 'other', 'completed_no_submission', 'rework_required', 'fully_submitted', 'limited_submitted', 'blocked', 'abnormal', 'cancelled', 'unknown', 'pending'):
        raise ValueError('统计结果分类无效。')
    if before is not None and (type(before) is not int or before < 1):
        raise ValueError('分页参数无效。')
    with store._connect() as db:
        db.execute('BEGIN')
        rows = db.execute('''WITH issues AS (
          SELECT p.server_key,p.issue_key,MIN(p.id) AS id
          FROM jira_pool_items p JOIN jira_collections c USING(request_id)
          WHERE (?='' OR c.group_id=?) AND (?='' OR substr(p.issue_key,1,instr(p.issue_key,'-')-1)=?)
          GROUP BY p.server_key,p.issue_key
        ), ended AS (
          SELECT j.server_key,j.issue_key,w.workflow_id,w.finished_at,
            ROW_NUMBER() OVER (PARTITION BY j.server_key,j.issue_key ORDER BY w.finished_at DESC,j.rowid DESC) AS position
          FROM workflow_jira_runs j JOIN workflows w USING(workflow_id)
          WHERE w.status IN ('completed','failed','cancelled')
            AND COALESCE(json_extract(w.termination_json,'$.outcome'),'')<>'no_task'
            AND (EXISTS(SELECT 1 FROM jira_execution_history h WHERE h.workflow_id=w.workflow_id)
              OR EXISTS(SELECT 1 FROM workflow_nodes n WHERE n.workflow_id=w.workflow_id AND n.job_id IS NOT NULL))
        ) SELECT i.*,e.workflow_id,e.finished_at FROM issues i LEFT JOIN ended e
          ON e.server_key=i.server_key AND e.issue_key=i.issue_key AND e.position=1 ORDER BY i.id DESC''',
          (group, group, project, project)).fetchall()
        data = []
        for row in rows:
            sources = db.execute('''SELECT DISTINCT c.group_id FROM jira_pool_items p JOIN jira_collections c USING(request_id)
              WHERE p.server_key=? AND p.issue_key=? AND (?='' OR c.group_id=?)''',
              (row['server_key'], row['issue_key'], group, group)).fetchall()
            title = db.execute('SELECT title FROM jira_pool_items WHERE server_key=? AND issue_key=? ORDER BY id DESC LIMIT 1', (row['server_key'], row['issue_key'])).fetchone()[0]
            result = execution_result(db, row['workflow_id']) if row['workflow_id'] else None
            status, name = 'pending', '开发人未获取'
            if result:
                run = db.execute('SELECT status,spec_json,spec_zlib,jira_context_json FROM workflows WHERE workflow_id=?', (row['workflow_id'],)).fetchone()
                status = result.get('status') or ('cancelled' if run['status']=='cancelled' else 'unknown')
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
              WHERE j.server_key=? AND j.issue_key=? AND w.status NOT IN ('completed','failed','cancelled') LIMIT 1''', (row['server_key'],row['issue_key'])).fetchone()
            data.append({'id':row['id'],'issueKey':row['issue_key'],'title':title,'groupIds':[s[0] for s in sources],
                         'developer':name,'status':status,'workflowId':row['workflow_id'],'finishedAt':row['finished_at'],'reprocessing':bool(active)})
        generated = utc_now()
        if items:
            selected = [r for r in data if (not developer or r['developer']==developer) and
                        (not category or (r['status'] in ('fully_submitted','limited_submitted','completed_no_submission') if category=='passed' else
                         r['status'] not in ('fully_submitted','limited_submitted','completed_no_submission','blocked') if category=='other' else r['status']==category)) and
                        (before is None or r['id']<before)]
            return {'items':selected[:50],'nextCursor':selected[49]['id'] if len(selected)>50 else None,'generatedAt':generated}
        groups = sorted({g for r in data for g in r['groupIds']})
        developers = sorted({r['developer'] for r in data})
        return {'summary':counts(data),'groups':[{'groupId':g,**counts([r for r in data if g in r['groupIds']])} for g in groups],
                'developers':[{'developer':d,**counts([r for r in data if r['developer']==d])} for d in developers], 'generatedAt':generated}


def counts(rows):
    values = Counter(r['status'] for r in rows)
    full, limited = values['fully_submitted'], values['limited_submitted']
    no_submission = values['completed_no_submission']
    passed = full + limited + no_submission
    return {'total':len(rows),'full':full,'limited':limited,'noSubmission':no_submission,'passed':passed,'blocked':values['blocked'],
            'other':len(rows)-passed-values['blocked'],'categories':dict(values)}
