"""独立文档页浏览器回归；使用已安装的 Playwright，不安装依赖或浏览器。"""
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / 'services/workflow-console/src/main/resources/static'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    value = {'id': 'doc', 'name': '实施方案.md', 'format': 'markdown', 'revision': 1,
             'updatedAt': '2026-10-06T08:00:00Z', 'stepName': '方案规划', 'stepNumber': 1,
             'removed': False, 'available': True, 'error': None,
             'content': '# 实施方案\n\n**重要内容**\n\n| 工作 | 说明 |\n| --- | --- |\n| 采集 | 文档 |\n\n```python\nprint("safe")\n```\n\n<img src="https://example.test/forbidden" onerror="alert(1)">\n\n[危险](javascript:alert(1))\n\n![图片](https://example.test/image.png)'}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.route('**/api/workflows/*/documents/*', lambda route: route.fulfill(json=value))
            external = []
            page.on('request', lambda request: external.append(request.url) if 'example.test' in request.url else None)
            page.goto(f'http://127.0.0.1:{server.server_port}/document.html?workflowId=flow&documentId=doc')
            page.locator('#content h1').wait_for()
            assert page.locator('#content table').count() == 1
            assert page.locator('#content pre code').inner_text() == 'print("safe")\n'
            assert page.locator('#content img, #content script, #content [onerror]').count() == 0
            assert page.locator('#content a[href]').count() == 0
            assert not external
            assert '第 1 步' in page.locator('#metadata').inner_text()
            assert page.locator('body').evaluate('(e) => e.scrollWidth <= innerWidth')
            value.update(revision=2, error='文档未同步', content='# 上次保存的正文')
            page.get_by_role('button', name='刷新文档').click()
            page.locator("#content").filter(has_text="上次保存的正文").wait_for()
            assert '上次成功保存' in page.locator('#status').inner_text()
            value.update(revision=3, removed=True, content=None, available=False)
            page.get_by_role('button', name='刷新文档').click()
            page.locator("#status").filter(has_text="已移除").wait_for()
            assert not page.locator('#content').inner_text()
            value.update(revision=4, removed=False, available=True, error=None, format='text', content='<script>普通文本</script>')
            page.get_by_role('button', name='刷新文档').click()
            page.locator("#content").filter(has_text="普通文本").wait_for()
            assert page.locator('#content script').count() == 0
            page.reload()
            page.locator('#content pre').wait_for()
            assert page.locator('#content').inner_text() == '<script>普通文本</script>'
            browser.close()
        print('文档页渲染、安全、失败保留、移除及刷新恢复验证通过。')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
