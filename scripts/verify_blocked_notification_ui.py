"""阻断通知页面回归：只使用本地静态资源、虚构 API 与已有 Playwright。"""
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / 'services/role-task-config-center/src/main/resources/static'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    targets = [dict(id='group', targetType='GROUP', enabled=True, available=True, displayName='测试群', externalId='g'),
               dict(id='person', targetType='PERSON', enabled=True, available=True, displayName='测试开发人', externalId='p')]
    posts = []
    errors = []
    mappings = []
    fail_config = [True]

    def route_api(route):
        path = urlparse(route.request.url).path
        body = {}
        if path.endswith('/blocked-notifications/config'):
            if fail_config[0]:
                fail_config[0] = False
                return route.fulfill(status=503, json={'error': '配置暂不可用'})
            body = {'templateId': 'blocked.schema'}
        elif path.endswith('/blocked-notifications/test'):
            posts.append(route.request.post_data_json)
            body = {'state': 'pending'}
        elif '/blocked-notifications/mappings/' in path:
            mappings.append(route.request.post_data_json)
        elif path.endswith('/blocked-notifications/mappings'):
            body = []
        elif '/blocked-notifications/' in path:
            body = {'state': 'unknown', 'reason': '发送结果未确认，未自动重发。'}
        elif path == '/api/dingtalk/config':
            body = dict(enabled=True, clientId='mock-client', cardTemplateId='', eventPollIntervalMs=1000)
        elif path.endswith('/targets/directory'):
            body = dict(departments=[], people=targets[1:])
        elif path.endswith('/targets'):
            body = targets
        elif path in ('/api/roles', '/api/sops', '/api/task-definitions'):
            body = []
        elif path == '/api/groups':
            body = {'groups': []}
        route.fulfill(json=body)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            page.goto(f'http://127.0.0.1:{server.server_port}/?page=dingtalk')
            page.get_by_role('button', name='重新加载', exact=True).click()
            form = page.locator('[data-blocked-test]')
            form.locator('[name=groupId]').select_option('group')
            form.locator('[name=personId]').select_option('person')
            form.locator('[name=text]').fill('TEST-1 已阻断')
            form.get_by_role('button', name='发送测试卡片').click()
            form.get_by_role('button', name='查看投递结果').wait_for()
            form.get_by_role('button', name='发送测试卡片').click()
            page.wait_for_function('!document.querySelector("[data-blocked-test]").dataset.saving')
            assert len(posts) == 2 and posts[0]['requestId'] == posts[1]['requestId']
            page.reload()
            page.get_by_role('button', name='查看投递结果').click()
            page.get_by_text('发送结果未确认，未自动重发。', exact=True).wait_for()
            page.locator('[data-blocked-close]').click()
            page.screenshot(path='/tmp/blocked-notification-config.png', full_page=True)
            page.locator('[data-page=dingtalk-targets]').click()
            page.locator('[data-target-type=PERSON]').click()
            page.get_by_role('button', name='Jira 开发人映射').click()
            page.locator('[data-blocked-mapping] [name=accountType]').select_option('key')
            page.locator('[data-blocked-mapping] [name=accountId]').fill('dev-1')
            page.get_by_role('button', name='保存映射').click()
            page.wait_for_function('!document.querySelector("[data-blocked-mapping]")')
            assert mappings == [{'accountType': 'key', 'accountId': 'dev-1'}]
            page.evaluate('openTask({blockedNotificationGroupId:"group"})')
            assert page.locator('#taskForm [name=blockedNotificationGroupId]').input_value() == 'group'
            page.locator('#taskDialog [data-dialog-close]').click()
            assert not errors, errors
            browser.close()
            print('阻断通知页面：错误恢复、重复发送、刷新恢复、状态、映射和任务群回填通过。')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
