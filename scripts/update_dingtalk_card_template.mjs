import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const target = resolve(process.argv[2] ?? "docs/card_1787896598119.json");
const outer = JSON.parse(readFileSync(target, "utf8"));
const editor = JSON.parse(outer.editorData);
const root = editor.schema?.componentsTree?.[0];

if (root?.componentName !== "AICardContainer") {
  throw new Error("未找到钉钉 AI 卡片根容器。");
}

const stateColors = new Map([
  [1, ["#D97706", "#FBBF24"]],
  [3, ["#059669", "#34D399"]],
  [4, ["#2563EB", "#60A5FA"]],
  [5, ["#DC2626", "#F87171"]],
]);

for (const state of root.children ?? []) {
  const colors = stateColors.get(state.props?.status);
  const content = state.children?.[0];
  const heading = content?.children?.find((child) => child.componentName === "BaseText");
  if (!colors || !heading) {
    throw new Error(`卡片状态 ${state.props?.status ?? "未知"} 缺少标题组件。`);
  }
  heading.props.fontColorType = "Custom";
  heading.props.customLightColor.value = colors[0];
  heading.props.customDarkColor.value = colors[1];
  heading.props.bold = true;
  heading.props.customFontSize = 16;
  heading.props.customFontLineHeight = 24;
  heading.props.marginTop = 12;
  heading.props.marginBottom = 8;
}

editor.mockData.cardData = {
  title: "文档生成测试",
  markdown:
    "**任务：** 文档生成测试\n\n" +
    "**状态：** ⏳ 等待确认\n\n" +
    "**执行进度：** █████░░░░░ 50% · 1 / 2\n\n" +
    "**当前步骤：** 质量审查 · 质量审查员\n\n" +
    "**剩余返工：** 10 次\n\n" +
    "**步骤状态**\n" +
    "✅ 1. 生成初稿 · 策略负责人 · 已完成\n" +
    "▫️ 2. 质量审查 · 质量审查员 · 准备中\n\n" +
    "**最近产出 · 生成初稿 · 策略负责人**\n初稿已经生成，等待质量审查。\n\n" +
    "**等待确认**\n下一步：质量审查 · 质量审查员\n" +
    "请点击“暂停”或“立即进入下一步”；否则将在 30 秒后自动继续。\n\n" +
    "**最新助手回复**\n当前初稿已完成，进入质量审查前等待你的确认。",
  status: "⏳ 等待确认",
  workflowId: "00000000-0000-4000-8000-000000000101",
  gateId: "00000000000040008000000000000201",
  showConfirm: true,
  showHold: true,
  confirmText: "立即进入下一步",
  holdText: "暂停",
  confirmAction: "advance_confirm",
  holdAction: "advance_hold",
  flowStatus: "1",
};

for (const variable of editor.variableList ?? []) {
  if (variable.id === "title") variable.description = "动态任务名称";
  if (variable.id === "markdown") {
    variable.description = "任务进度、步骤产出与最新助手回复 Markdown 正文";
  }
  if (variable.id === "status") variable.description = "带状态图标的中文状态文案";
}

editor.editVersion = 2;
outer.editorData = JSON.stringify(editor);
writeFileSync(target, `${JSON.stringify(outer, null, 2)}\n`, "utf8");
