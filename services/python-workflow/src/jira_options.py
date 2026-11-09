"""按账号、项目和工单类型读取 Jira 的真实选项；缓存不包含凭据。"""
import json
import time
import uuid
from urllib.parse import quote

from jira_http import JiraClient, JiraHttpError


def named(values, label):
    if not isinstance(values, list):
        raise JiraHttpError(f'Jira {label}响应格式无效。')
    result = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not isinstance(value.get('name'), str):
            raise JiraHttpError(f'Jira {label}响应格式无效。')
        result[value['id']] = {'id': value['id'], 'name': value['name']}
    return list(result.values())


def projects(client):
    raw = client.request('project')
    values = named(raw, '项目')
    if len(values) != len(raw):
        raise JiraHttpError('Jira 项目列表包含重复编号。')
    for value, original in zip(values, raw):
        if not isinstance(original.get('key'), str):
            raise JiraHttpError('Jira 项目编号缺失。')
        value['key'] = original['key']
    return {'projects': values}


def role_options(client, project, issue_type):
    start, matches = 0, []
    while True:
        page = client.request(f'issue/createmeta/{quote(project, safe="")}/issuetypes/{quote(issue_type, safe="")}?startAt={start}&maxResults=100')
        if (not isinstance(page, dict) or not isinstance(page.get('values'), list)
                or page.get('startAt') != start or type(page.get('total')) is not int
                or page['total'] < start + len(page['values'])):
            raise JiraHttpError('Jira 处理角色选项响应格式无效。')
        values = page['values']
        matches.extend(v for v in values if isinstance(v, dict) and v.get('name') == '处理角色')
        start += len(values)
        if start >= page['total']:
            break
        if not values or start > 2000:
            raise JiraHttpError('Jira 处理角色选项分页不完整。')
    if not matches:
        return []
    if len(matches) != 1 or not isinstance(matches[0].get('allowedValues'), list):
        raise JiraHttpError('当前类型未提供处理角色候选值，请检查 Jira 字段配置与账号权限。')
    field = matches[0]
    if any(not isinstance(v, dict) for v in field['allowedValues']):
        raise JiraHttpError('Jira 处理角色候选值格式无效。')
    return named([{'id': v.get('id'), 'name': v.get('value')} for v in field['allowedValues']], '处理角色')


def project_options(client, project, issue_type):
    data = client.request('project/' + quote(project, safe=''))
    if not isinstance(data, dict) or not isinstance(data.get('key'), str):
        raise JiraHttpError('Jira 项目响应格式无效。')
    project_value = named([data], '项目')[0] | {'key': data['key']}
    types = named(data.get('issueTypes'), '工单类型')
    selected = types if not issue_type else [t for t in types if issue_type == t['id'] or issue_type.casefold() == t['name'].casefold()]
    if not selected:
        return {'project': project_value, 'issueTypes': types, 'statuses': [], 'roles': [],
                'rolesError': '原工单类型不属于此项目，请重新选择。'}
    status_data = client.request('project/' + quote(data['key'], safe='') + '/statuses')
    if not isinstance(status_data, list):
        raise JiraHttpError('Jira 状态响应格式无效。')
    ids = {t['id'] for t in selected}
    statuses = named([v for t in status_data if isinstance(t, dict) and t.get('id') in ids for v in t.get('statuses', [])], '状态')
    roles, errors = {}, []
    for t in selected:
        try:
            for role in role_options(client, data['key'], t['id']):
                roles[role['id']] = role
        except JiraHttpError as error:
            errors.append(str(error))
    # 类型不限时必须展示完整候选集合，不能把部分成功的角色伪装为完整列表。
    return {'project': project_value,
            'issueTypes': types, 'statuses': statuses, 'roles': [] if errors else list(roles.values()),
            'rolesError': errors[0] if errors else '' if roles else '所选类型未提供可用的处理角色选项。'}


def options(store, identifier, project='', issue_type='', refresh=False):
    from jira_accounts import get, usable, version
    uuid.UUID(identifier)
    if any(not isinstance(v, str) or len(v) > 1000 or any(ord(c) < 32 for c in v) for v in (project, issue_type)):
        raise ValueError('项目或工单类型无效。')
    if issue_type and not project:
        raise ValueError('请先选择项目。')
    with store._connect() as db:
        row = dict(get(db, identifier))
        usable(db, identifier, row['group_id'])
        cached = db.execute('SELECT payload_json,updated_at FROM jira_account_options WHERE account_id=? AND account_version=? AND project_key=? AND issue_type=?',
                            (identifier, row['version'], project, issue_type)).fetchone()
        if not refresh and cached and time.time() - cached['updated_at'] < 600:
            return json.loads(cached['payload_json'])
    client = JiraClient(row['server'], row['token'])
    result = project_options(client, project, issue_type) if project else projects(client)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        version(get(db, identifier), row['version'])
        if not result.get('rolesError'):
            db.execute('DELETE FROM jira_account_options WHERE account_id=? AND account_version<>?', (identifier, row['version']))
            db.execute('INSERT OR REPLACE INTO jira_account_options VALUES(?,?,?,?,?,?)',
                       (identifier, row['version'], project, issue_type, json.dumps(result, ensure_ascii=False), time.time()))
    return result
