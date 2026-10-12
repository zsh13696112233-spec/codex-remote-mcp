"""半自动时长与监控等待交互回归；只访问虚构 API 和本机静态页面。"""
import copy
import threading
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
GROUP = "00000000-0000-0000-0000-000000000001"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO / "services"), **kwargs)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sop = dict(id="sop", name="等待确认测试", groupId=GROUP, supervisorAgentId="local", advanceMode="automatic",
        supervisorTimeoutSec=7200, defaultStepModel="gpt-5.6-sol", steps=[dict(id="first", roleId="role", displayName="分析需求", instruction="分析", agentId="local", timeoutSec=1800)])
    saves, errors, actions = [], [], []
    expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    snapshot = dict(workflowId="demo", name="最终确认测试", status="running", advanceMode="semi_automatic", progress=dict(completed=1, total=1),
        nodes=[dict(id="a", displayName="最终步骤", status="completed", response="最新总结", dependsOn=[], artifacts=[])],
        pendingAdvance=dict(gateId="gate", kind="finish", state="countdown", completedNodeId="a", nextNodeId=None, expiresAt=expires))

    def api(route):
        path = urlparse(route.request.url).path
        body = {}
        if path.endswith("/api/agents/local/models"):
            body = {"models": []}
        elif path.endswith("/api/agents"):
            body = {"agents": [dict(agentId="local", groupId=GROUP, enabled=True, capabilities=["supervisor", "executor"], permissionProfiles=["read_only"])]}
        elif path.endswith("/api/groups"):
            body = {"groups": [dict(id=GROUP, name="测试组", sopCount=1)]}
        elif path.endswith("/api/roles"):
            body = [dict(id="role", name="分析", duty="职责", enabled=True, groupId=GROUP)]
        elif path.endswith("/api/sops"):
            body = [sop]
        elif path.endswith("/api/sops/sop"):
            if route.request.method == "PUT":
                saves.append(route.request.post_data_json)
                sop.update(copy.deepcopy(saves[-1]))
            body = sop
        elif path.endswith(("/api/task-definitions", "/api/dingtalk/targets")):
            body = []
        elif path.endswith("/api/workflows/demo"):
            body = snapshot
        elif path.endswith("/events"):
            body = {"events": [], "nextCursor": 0}
        elif "/advance/" in path:
            actions.append(path)
            snapshot["pendingAdvance"]["state"] = "busy"
        route.fulfill(json=body)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", api)
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(base + f"/role-task-config-center/src/main/resources/static/?page=sops&groupId={GROUP}")
            page.locator("[data-sop-field=advanceMode]").select_option("semi_automatic")
            assert page.locator("[data-sop-field=confirmationMinutes]").input_value() == "2"
            assert page.locator("[data-sop-field=blockedMinutes]").input_value() == "30"
            page.locator("[data-sop-field=confirmationMinutes]").fill("5")
            page.locator("[data-sop-field=blockedMinutes]").fill("45")
            page.locator("[data-sop-save]").click()
            page.get_by_text("SOP 工作流已保存", exact=True).wait_for()
            assert saves[-1]["confirmationMinutes"] == 5 and saves[-1]["blockedMinutes"] == 45
            page.reload()
            page.locator("[data-sop-field=blockedMinutes]").wait_for()
            assert page.locator("[data-sop-field=blockedMinutes]").input_value() == "45"
            page.locator("[data-sop-field=confirmationMinutes]").scroll_into_view_if_needed()
            page.screenshot(path="/tmp/sop-wait-settings.png")
            console = base + "/workflow-console/src/main/resources/static/?workflowId=demo"
            page.goto(console)
            page.get_by_role("button", name="确认结束", exact=False).wait_for()
            assert "第 0 步" not in page.locator("body").inner_text()
            snapshot["discussionBusy"] = True
            page.reload()
            page.get_by_role("button", name="正在处理…", exact=True).wait_for()
            assert page.locator(".advance-confirm").is_disabled()
            snapshot["discussionBusy"] = False
            snapshot["pendingAdvance"]["state"] = "held"
            page.reload()
            page.get_by_role("button", name="确认结束", exact=True).click()
            page.wait_for_function("document.querySelector('.advance-confirm')?.disabled")
            assert len(actions) == 1 and actions[0].endswith("/confirm")
            snapshot["pendingAdvance"].update(kind="blocked", state="held")
            snapshot["nodes"][0].update(status="waiting", error="缺少确认信息")
            page.reload()
            page.get_by_text("当前步骤遇到阻碍，等待人工处理", exact=True).first.wait_for()
            assert page.locator(".advance-confirm").count() == 0
            assert "重新执行" in page.locator("body").inner_text()
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator(".advance-waiting").scroll_into_view_if_needed()
            page.screenshot(path="/tmp/sop-wait-monitor-mobile.png")
            assert not errors, errors
            browser.close()
            print("通过：半自动时长配置、保存刷新；最终确认、讨论禁用、重复提交保护、遇阻提示和窄屏展示。")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
