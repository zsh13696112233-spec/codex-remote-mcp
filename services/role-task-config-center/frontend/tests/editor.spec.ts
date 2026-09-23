import { test, expect, type Page } from "@playwright/test";

const existing = () => ({
  id: "existing",
  groupId: "g",
  name: "现有流程",
  description: "",
  supervisorAgentId: "local",
  supervisorTimeoutSec: 7200,
  maxRetryCount: 10,
  advanceMode: "automatic",
  handoffMode: "legacy_text",
  defaultStepModel: "gpt-5.6-sol",
  enabled: true,
  steps: ["分析", "开发", "审查"].map((name, i) => ({
    id: `db-${i}`,
    nodeKey: `n${i}`,
    displayName: name,
    roleId: `role-${i}`,
    roleName: name,
    instruction: `完成${name}工作`,
    expectedOutput: "交付结果",
    executorType: "local",
    agentId: "local",
    workingDirectory: "",
    modelOverride: null,
    permissionProfile: "read_only",
    writeEnabled: false,
    timeoutSec: 1800,
    skills: [],
    mcps: [],
  })),
});
async function openExisting(page: Page) {
  const d = existing();
  await page.route("**/api/sops", (r) =>
    r.request().method() === "GET"
      ? r.fulfill({ json: [d, { ...d, id: "second", name: "另一个流程" }] })
      : r.continue(),
  );
  await page.route("**/api/sops/second", (r) =>
    r.fulfill({ json: { ...d, id: "second", name: "另一个流程" } }),
  );
  await page.goto("/?page=sops&groupId=g");
  await expect(page.locator(".fg-editor")).toHaveAttribute(
    "data-ready",
    "true",
  );
}
async function connect(page: Page, source: string, target: string) {
  const from = page.locator(`[data-port-entity-id="port_output_${source}_"]`);
  const to = page.locator(`[data-port-entity-id="port_input_${target}_"]`);
  await from.dragTo(to);
}

test("真实画布：新建、拖入三个角色、保存刷新、属性撤销和重复挂载", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => {
    errors.push(e.message);
    console.log("PAGE ERROR", e.message);
  });
  await page.goto("/?page=sops&groupId=g");
  await page.locator("#create").click();
  await expect(page.getByText("从任务定义接收目标与输入")).toBeVisible();
  await expect(page.locator(".fg-editor")).toHaveAttribute(
    "data-ready",
    "true",
  );
  await page.getByLabel("工作流名称 *", { exact: true }).fill("画布验收流程");
  // 从角色库拖到已有连线中点；每次插入后重新整理。
  for (const role of ["分析", "开发", "审查"]) {
    const palette = page
      .locator(".fg-palette button")
      .filter({ hasText: role });
    await palette.scrollIntoViewIfNeeded();
    const source = await palette.boundingBox();
    const cards = await page.locator(".fg-step").all();
    const boxes = await Promise.all(cards.map((c) => c.boundingBox()));
    const start = boxes.length
      ? boxes.sort((a, b) => b!.x - a!.x)[0]
      : await page.locator(".fg-start").boundingBox();
    const end = await page.locator(".fg-end").boundingBox();
    expect(source && start && end).toBeTruthy();
    await page.mouse.move(
      source!.x + source!.width / 2,
      source!.y + source!.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      (start!.x + start!.width + end!.x) / 2,
      (start!.y + start!.height / 2 + end!.y + end!.height / 2) / 2,
      { steps: 15 },
    );
    await page.mouse.up();
    await expect(
      page.locator(".fg-card strong").filter({ hasText: role }),
    ).toHaveCount(1);
    await page.getByRole("button", { name: "自动整理", exact: true }).click();
    await expect(page.locator(".fg-editor")).toHaveAttribute(
      "data-ready",
      "true",
    );
    await expect(page.locator(".fg-step")).toHaveCount(
      ["分析", "开发", "审查"].indexOf(role) + 1,
    );
  }
  const firstSave = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/api/sops"),
  );
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  const originalGraph = (await firstSave).postDataJSON().editorGraph;
  await expect(page.getByText("所有修改已保存", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".fg-step")).toHaveCount(3);
  await page.locator(".fg-step").filter({ hasText: "审查" }).click();
  await page
    .getByLabel("本步骤执行要求 *", { exact: true })
    .fill("检查所有交付文件");
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(
    page.getByLabel("本步骤执行要求 *", { exact: true }),
  ).toHaveValue("完成审查工作");
  await page.getByRole("button", { name: "重做", exact: true }).click();
  await expect(
    page.getByLabel("本步骤执行要求 *", { exact: true }),
  ).toHaveValue("检查所有交付文件");
  await page.getByLabel("工作目录", { exact: true }).fill("C:/workspace");
  await page.getByLabel("预期输出", { exact: true }).fill("检查报告");
  await page.getByLabel("超时（秒）", { exact: true }).fill("600");
  await page
    .getByLabel("权限档位", { exact: true })
    .selectOption("workspace_write");
  await page.getByText("高级设置", { exact: true }).click();
  await page.getByLabel("Skill 标签", { exact: true }).fill("review, verify");
  await page.getByLabel("MCP 标签", { exact: true }).fill("tools");
  const nextSave = page.waitForRequest(
    (r) => r.method() === "PUT" && r.url().includes("/api/sops/"),
  );
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  const updated = (await nextSave).postDataJSON();
  expect(updated.editorGraph).toEqual(originalGraph);
  expect(
    updated.steps.find((s: any) => s.displayName === "审查"),
  ).toMatchObject({
    workingDirectory: "C:/workspace",
    expectedOutput: "检查报告",
    timeoutSec: 600,
    permissionProfile: "workspace_write",
    writeEnabled: true,
    skills: ["review", "verify"],
    mcps: ["tools"],
  });
  await expect(page.getByText("所有修改已保存", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "角色管理", exact: true }).click();
  await page.getByRole("button", { name: "SOP 工作流", exact: true }).click();
  await expect(page.locator(".fg-renderer")).toHaveCount(1);
  await expect(page.locator(".fg-step")).toHaveCount(3);
  await expect(page.locator(".fg-editor")).toHaveAttribute(
    "data-ready",
    "true",
  );
  await page.screenshot({
    path: "../target/sop-browser-results/desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 700, height: 900 });
  await expect(
    page.getByRole("button", { name: "保存工作流", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../target/sop-browser-results/narrow.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("重新连线决定顺序，删除衔接前后节点，布局和标识保留", async ({ page }) => {
  await openExisting(page);
  for (const id of ["start", "n0", "n1", "n2"]) {
    await page.locator(`[data-sop-node="${id}"]`).click();
    await page.getByRole("button", { name: "断开", exact: true }).click();
  }
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("串行链");
  await connect(page, "start", "n2");
  await connect(page, "n2", "n0");
  await connect(page, "n0", "n1");
  await connect(page, "n1", "end");
  let saved: any;
  await page.route("**/api/sops/existing", async (r) => {
    saved = r.request().postDataJSON();
    await r.fulfill({ json: { ...saved, id: "existing" } });
  });
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await expect(page.getByText("所有修改已保存", { exact: true })).toBeVisible();
  expect(saved.editorGraph.edges).toEqual(
    expect.arrayContaining([
      { source: "start", target: "n2" },
      { source: "n2", target: "n0" },
      { source: "n0", target: "n1" },
      { source: "n1", target: "end" },
    ]),
  );
  expect(saved.steps.map((s: any) => s.nodeKey).sort()).toEqual([
    "n0",
    "n1",
    "n2",
  ]);
  await page.locator('[data-sop-node="n0"]').click();
  await page.getByRole("button", { name: "删除步骤", exact: true }).click();
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await expect(page.getByText("所有修改已保存", { exact: true })).toBeVisible();
  expect(saved.editorGraph.edges).toContainEqual({
    source: "n2",
    target: "n1",
  });
  expect(saved.editorGraph.nodes).toHaveLength(4);
});

test("保存防重、失败保留草稿、运行状态刷新不覆盖输入、未保存提醒", async ({
  page,
}) => {
  await openExisting(page);
  await page.getByLabel("工作流名称 *", { exact: true }).fill("需要保留的草稿");
  await page.evaluate(() =>
    window.dispatchEvent(
      new CustomEvent("sop-runtime", { detail: { agents: [], online: false } }),
    ),
  );
  await expect(
    page.getByText("网关不可用，草稿仍可编辑；恢复连接后可保存。"),
  ).toBeVisible();
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "需要保留的草稿",
  );
  await page.evaluate(() =>
    window.dispatchEvent(
      new CustomEvent("sop-runtime", { detail: { agents: [], online: true } }),
    ),
  );
  let finish: () => void = () => {};
  const pending = new Promise<void>((resolve) => (finish = resolve));
  let count = 0;
  await page.route("**/api/sops/existing", async (r) => {
    count++;
    await pending;
    await r.fulfill({ status: 503, json: { message: "保存测试失败" } });
  });
  await page.getByRole("button", { name: "保存工作流", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "正在保存…", exact: true }),
  ).toBeDisabled();
  // 即使重复发送点击事件，保存引用锁也不允许第二次请求。
  await page
    .getByRole("button", { name: "正在保存…", exact: true })
    .dispatchEvent("click");
  await expect.poll(() => count).toBe(1);
  finish();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "需要保留的草稿",
  );
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.locator('[data-sop-select="second"]').click();
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "需要保留的草稿",
  );
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "角色管理", exact: true }).click();
  await expect(page.locator(".fg-renderer")).toHaveCount(1);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.reload({waitUntil: "commit", timeout: 3000}).catch(() => {});
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "需要保留的草稿",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator('[data-sop-select="second"]').click();
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "另一个流程",
  );
  await expect(page.locator(".fg-renderer")).toHaveCount(1);
});

test("选择请求晚到时不会覆盖新建草稿", async ({ page }) => {
  await openExisting(page);
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/sops/second", async (route) => {
    await gate;
    await route.fulfill({
      json: { ...existing(), id: "second", name: "迟到的流程" },
    });
  });
  const requested = page.waitForRequest("**/api/sops/second");
  await page.locator('[data-sop-select="second"]').click();
  await requested;
  await page.locator("#create").click();
  await page.getByLabel("工作流名称 *", { exact: true }).fill("新的草稿");
  const response = page.waitForResponse("**/api/sops/second");
  release();
  await response;
  await expect(page.getByLabel("工作流名称 *", { exact: true })).toHaveValue(
    "新的草稿",
  );
});

test("停用角色保留在已有步骤，角色库不提供停用角色", async ({ page }) => {
  await page.route("**/api/roles", (r) =>
    r.fulfill({
      json: ["分析", "开发", "审查"].map((name, i) => ({
        id: `role-${i}`,
        name,
        duty: `完成${name}工作`,
        groupId: "g",
        enabled: i !== 0,
      })),
    }),
  );
  await openExisting(page);
  await page.locator('[data-sop-node="n0"]').click();
  await expect(page.getByText("当前角色已停用。")).toBeVisible();
  await expect(page.locator(".fg-palette button")).toHaveCount(2);
});
