// 仅供本地 UI 回归；业务数据保存在内存，不连接网关或数据库。
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const moduleRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const roles = ["分析", "开发", "审查"].map((name, i) => ({
  id: `role-${i}`,
  name,
  duty: `完成${name}工作`,
  groupId: "g",
  enabled: true,
}));
const agents = [
  {
    agentId: "local",
    name: "测试执行机",
    groupId: "g",
    enabled: true,
    capabilities: ["supervisor", "executor"],
    permissionProfiles: ["read_only", "workspace_write"],
    connectionStatus: "online",
    availability: "idle",
    checkedAt: "2026-09-23T00:00:00Z",
    connectionTestPassed: true,
  },
];
let sops = [];
const json = (res, value, status = 200) => {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(value));
};
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const p = url.pathname;
    if (p === "/api/groups" || p === "/api/agent-groups")
      return json(res, {
        groups: [
          {
            id: "g",
            name: "测试组",
            roleCount: 3,
            sopCount: sops.length,
            taskCount: 0,
            machineCount: 1,
          },
        ],
        unassigned: {},
      });
    if (p === "/api/gateway/ready") return json(res, { ready: true });
    if (p === "/api/agents") return json(res, { agents });
    if (p === "/api/roles") return json(res, roles);
    if (p === "/api/task-definitions" || p === "/api/dingtalk/targets")
      return json(res, []);
    if (p === "/api/sops" && req.method === "GET") return json(res, sops);
    if (p.startsWith("/api/sops") && ["POST", "PUT"].includes(req.method)) {
      let text = "";
      for await (const chunk of req) {
        text += chunk;
        if (text.length > 1000000) throw Error("too large");
      }
      const d = JSON.parse(text);
      d.id = p.split("/")[3] || `sop-${sops.length + 1}`;
      d.updatedAt = new Date().toISOString();
      d.steps.forEach((s, i) => {
        s.id = `db-${i}-${Date.now()}`;
      });
      sops = sops.filter((s) => s.id !== d.id);
      sops.push(d);
      return json(res, d);
    }
    if (p.startsWith("/api/sops/"))
      return json(
        res,
        sops.find((s) => s.id === p.split("/")[3]) || {},
        sops.some((s) => s.id === p.split("/")[3]) ? 200 : 404,
      );
    if (p.startsWith("/api/"))
      return json(res, { error: "测试接口未实现" }, 404);
    const root = p.startsWith("/sop-editor/")
      ? path.join(moduleRoot, "target/generated-resources/static")
      : path.join(moduleRoot, "src/main/resources/static");
    const file = path.resolve(root, "." + (p === "/" ? "/index.html" : p));
    if (!file.startsWith(root + path.sep)) return json(res, {}, 403);
    const data = await fs.readFile(file);
    res.writeHead(200, {
      "Content-Type":
        {
          html: "text/html; charset=utf-8",
          js: "text/javascript; charset=utf-8",
          css: "text/css; charset=utf-8",
        }[file.split(".").pop()] || "application/octet-stream",
      "Cache-Control": "no-store",
    });
    res.end(data);
  } catch {
    json(res, { error: "测试资源不可用" }, 404);
  }
});
server.listen(18091, "127.0.0.1", () =>
  console.log("SOP UI fixture: http://127.0.0.1:18091/?page=sops&groupId=g"),
);
