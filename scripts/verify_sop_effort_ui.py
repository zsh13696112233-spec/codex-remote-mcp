"""SOP 强度和大窗口编辑的浏览器回归；只使用虚构 API。"""
import copy
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / 'services/role-task-config-center/src/main/resources/static'
GROUP = '00000000-0000-0000-0000-000000000001'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sop = dict(id='sop', name='测试流程', groupId=GROUP, supervisorAgentId='local', defaultStepModel='gpt-5.6-sol', steps=[
        dict(id='first', roleId='role', displayName='分析需求', instruction='原执行要求', agentId='local')])
    errors, saves = [], []
    fail = [True]

    def route_api(route):
        path = urlparse(route.request.url).path
        body = {}
        if path == '/api/agents/local/models':
            if fail[0]:
                return route.fulfill(status=503, json={'error': '暂不可用'})
            body = {'models': [{'model': 'gpt-5.6-sol', 'supportedReasoningEfforts': ['medium', 'high']}]}
        elif path == '/api/agents':
            body = {'agents': [dict(agentId='local', groupId=GROUP, enabled=True, capabilities=['supervisor', 'executor'], permissionProfiles=['read_only'])]}
        elif path == '/api/groups':
            body = {'groups': [dict(id=GROUP, name='测试组', sopCount=1)]}
        elif path == '/api/roles':
            body = [dict(id='role', name='分析', duty='职责', enabled=True, groupId=GROUP)]
        elif path == '/api/sops':
            body = [sop]
        elif path == '/api/sops/sop':
            if route.request.method == 'PUT':
                saves.append(route.request.post_data_json)
                sop.update(copy.deepcopy(saves[-1]))
            body = sop
        elif path in ('/api/task-definitions', '/api/dingtalk/targets'):
            body = []
        elif path == '/api/gateway/ready':
            body = {'ready': True}
        route.fulfill(json=body)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            url = f'http://127.0.0.1:{server.server_port}/?page=sops&groupId={GROUP}'
            page.goto(url)
            page.locator('[data-effort-control]').get_by_text('思考强度加载失败，请刷新页面重试。').wait_for()
            fail[0] = False
            assert page.locator('[data-effort-refresh]').count() == 0
            page.reload()
            default = page.locator('[data-sop-field=defaultStepReasoningEffort]')
            default.locator('option[value=high]').wait_for(state='attached')
            assert page.get_by_text('强度越高通常耗时越长；保存时会检查每个步骤是否支持。', exact=True).count() == 0
            default.select_option('high')
            page.locator('.flow-node').click()
            override = page.locator('[data-node-field=reasoningEffortOverride]')
            assert override.input_value() == ''
            override.select_option('medium')
            assert page.locator('.instruction-field textarea, .instruction-preview').count() == 0
            assert page.locator('[data-instruction-edit]').count() == 1
            page.get_by_role('button', name='编辑', exact=True).click()
            dialog = page.locator('#instructionDialog')
            dialog.locator('textarea').fill('取消的内容')
            dialog.get_by_role('button', name='取消', exact=True).click()
            assert page.evaluate('selectedStep().instruction') == '原执行要求'
            page.locator('[data-instruction-edit]').click()
            dialog.locator('textarea').fill('Escape 放弃')
            page.keyboard.press('Escape')
            page.locator('[data-instruction-edit]').click()
            assert dialog.locator('textarea').input_value() == '原执行要求'
            text = '步骤执行要求\n' + '读取输入并验证结果。\n' * 300
            dialog.locator('textarea').fill(text)
            bounds = dialog.bounding_box()
            assert bounds['width'] >= 1000 and abs(bounds['x'] + bounds['width']/2 - 720) < 2
            page.screenshot(path='/tmp/sop-instruction-desktop.png')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert dialog.bounding_box()['width'] <= 390
            page.screenshot(path='/tmp/sop-instruction-mobile.png')
            dialog.get_by_role('button', name='确认修改').click()
            assert page.evaluate('selectedStep().instruction').strip() == text.strip()
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.locator('[data-sop-save]').click()
            page.get_by_text('SOP 工作流已保存', exact=True).wait_for()
            assert saves[-1]['defaultStepReasoningEffort'] == 'high'
            assert saves[-1]['steps'][0]['reasoningEffortOverride'] == 'medium'
            assert saves[-1]['steps'][0]['instruction'] == text.strip()
            page.reload()
            page.locator('.flow-node').click()
            assert page.locator('[data-node-field=reasoningEffortOverride]').input_value() == 'medium'
            assert page.evaluate('selectedStep().instruction').strip() == text.strip()
            assert not errors, errors
            browser.close()
            print('SOP 强度：错误重试、继承覆盖、保存刷新；大窗口：确认取消、Escape、长文本、桌面居中和窄屏通过。')
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
