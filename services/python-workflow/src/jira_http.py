"""中央 Jira API v2 只读访问；不重定向、不回显服务响应或认证内容。"""
import json
import http.client
import re
import urllib.error
import urllib.request

from workflow_jira import ISSUE_KEY


class JiraHttpError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JiraClient:
    def __init__(self, server, token):
        self.server = server
        self._token = token

    def request(self, path, body=None):
        request = urllib.request.Request(self.server + '/rest/api/2/' + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise JiraHttpError('Jira 响应过大，请缩小查询范围。')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            raise JiraHttpError('Jira 请求未成功，请检查账号权限、服务地址和查询条件。') from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise JiraHttpError('无法连接 Jira，请检查网络和服务地址。') from None
        except (ValueError, UnicodeError):
            raise JiraHttpError('Jira 响应格式无效。') from None

    def identity(self):
        return self.profile()['identity']

    def profile(self):
        user = self.request('myself')
        identity = (user.get('accountId') or user.get('key') or user.get('name')) if isinstance(user, dict) else None
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256 or any(ord(c) < 32 for c in identity):
            raise JiraHttpError('无法确认 Jira 账号身份。')
        username = user.get('name')
        if (not isinstance(username, str) or not username.strip() or len(username) > 256
                or any(ord(c) < 32 for c in username)):
            username = None
        return {'identity': identity, 'username': username.strip() if username else None}

    def developer_field(self):
        fields = self.request('field')
        if not isinstance(fields, list):
            raise JiraHttpError('Jira 字段列表格式无效。')
        matches = [f.get('id') for f in fields if isinstance(f, dict) and f.get('name') == '开发人']
        return matches[0] if len(matches) == 1 and isinstance(matches[0], str) and re.fullmatch(r'customfield_[0-9]+', matches[0]) else None

    def search(self, jql, field=None):
        value = self.request('search', {'jql': jql, 'startAt': 0, 'maxResults': 100,
                                      'fields': ['summary', 'assignee', 'issuetype', 'priority'] + ([field] if field else [])})
        if (not isinstance(value, dict) or not isinstance(value.get('issues'), list)
                or value.get('startAt') != 0 or type(value.get('total')) is not int
                or value['total'] < len(value['issues']) or (not value['issues'] and value['total'])):
            raise JiraHttpError('Jira 搜索分页响应无效。')
        result = {}
        for issue in value['issues']:
            if (not isinstance(issue, dict) or not isinstance(issue.get('key'), str)
                    or not ISSUE_KEY.fullmatch(issue['key']) or issue['key'] in result
                    or not isinstance(issue.get('fields'), dict)):
                raise JiraHttpError('Jira 工单响应格式无效。')
            fields = issue['fields']
            title = fields.get('summary')
            if not isinstance(title, str):
                raise JiraHttpError('Jira 工单标题格式无效。')
            result[issue['key']] = (title[:1000], people(fields, field), issue_details(fields))
        return result


def people(fields, field):
    from workflow_store import utc_now
    names, status = [], 'unavailable'
    if field and field in fields:
        value = fields[field]
        users = [] if value is None else value if isinstance(value, list) else [value]
        if len(users) <= 100 and all(isinstance(u, dict) and isinstance(u.get('displayName'), str)
                                   and 0 < len(u['displayName'].strip()) <= 160 for u in users):
            names = list(dict.fromkeys(u['displayName'].strip() for u in users))
            status = 'resolved' if names else 'empty'
    assignee = fields.get('assignee')
    name = assignee.get('displayName') if isinstance(assignee, dict) else None
    name = name.strip() if isinstance(name, str) and len(name) <= 160 else ''
    return {'developers': names, 'developerStatus': status, 'assignee': name,
            'assigneeStatus': 'resolved' if name else 'unavailable', 'collectedAt': utc_now()}


def issue_details(fields):
    result = {}
    for key, field in (('issueType', 'issuetype'), ('priority', 'priority')):
        value = fields.get(field)
        name = value.get('name') if isinstance(value, dict) else None
        result[key] = name.strip() if isinstance(name, str) and 0 < len(name.strip()) <= 160 else None
    return result
