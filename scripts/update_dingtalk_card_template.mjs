import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const target = resolve(process.argv[2] ?? "docs/card_1787896598119.json");
const outer = JSON.parse(readFileSync(target, "utf8"));
const editor = JSON.parse(outer.editorData);
const root = editor.schema?.componentsTree?.[0];

if (root?.componentName !== "AICardContainer") {
  throw new Error("未找到钉钉 AI 卡片根容器。");
}

const clone = (value) => JSON.parse(JSON.stringify(value));

function markdownBlock(source, stateId, suffix, title, variable) {
  const block = clone(source);
  block.id = `${stateId}_${suffix}`;
  block.title = title;
  block.props.content = {
    type: "variableValue",
    variable,
    variableType: "global",
    varType: "markdown",
  };
  return block;
}

for (const state of root.children ?? []) {
  const content = state.children?.[0];
  const heading = content?.children?.find(
    (child) => child.componentName === "BaseText" && child.title === "任务状态标题",
  );
  const markdown = content?.children?.find((child) => child.componentName === "MarkdownBlock");
  const buttons = content?.children?.find((child) => child.componentName === "ButtonGroup");
  if (!heading || !markdown) {
    throw new Error(`卡片状态 ${state.props?.status ?? "未知"} 缺少基础组件。`);
  }
  heading.props.text.content = "${title}";
  heading.props.fontColorType = "Default";
  heading.props.bold = true;
  heading.props.customFontSize = 16;
  heading.props.customFontLineHeight = 24;
  heading.props.marginTop = 12;
  heading.props.marginBottom = 8;

  content.children = [
    heading,
    markdownBlock(markdown, state.id, "body", "任务详情", "cardBody"),
  ];
  if (state.props.status === 1 && buttons) content.children.push(buttons);
}

editor.mockData.cardData = {
  title: "文档生成测试",
  markdown:
    "**任务：** 文档生成测试\n\n" +
    "**状态：** 🟡 等待确认\n\n" +
    "**执行进度：** █████░░░░░ 50% · 1 / 2\n\n" +
    "**当前步骤：** 质量审查 · 质量审查员\n\n" +
    "**剩余返工：** 10 次\n\n" +
    "**步骤状态**\n" +
    "✅ 1. 生成初稿 · 策略负责人 · 已完成\n" +
    "• 2. 质量审查 · 质量审查员 · 准备中\n\n" +
    "**最近产出 · 生成初稿 · 策略负责人**\n初稿已经生成，等待质量审查。\n\n" +
    "**等待确认**\n下一步：质量审查 · 质量审查员\n" +
    "请点击“暂停”或“立即进入下一步”；否则将在 30 秒后自动继续。\n\n" +
    "**最新助手回复**\n当前初稿已完成，进入质量审查前等待你的确认。",
  status: "🟡 等待确认",
  progressText: "50% · 1 / 2 步",
  currentStep: "### 🎯 当前步骤\n\n**质量审查**\n\n质量审查员 · 准备中",
  stepTimeline:
    "### 📋 步骤状态\n\n✅ **生成初稿** · 已完成\n• **质量审查** · 准备中",
  latestOutput: "> **📄 最近产出 · 生成初稿 · 策略负责人**\n>\n> 初稿已经生成，等待质量审查。",
  latestReply: "> **💬 最新助手回复**\n>\n> 当前初稿已完成，进入质量审查前等待你的确认。",
  result: "",
  notice:
    "### 🟡 等待确认\n\n下一步：**质量审查 · 质量审查员**\n\n可选择暂停或立即继续；未操作时将在 30 秒后自动进入下一步。",
  cardBody:
    "**🟡 等待确认 · 1 / 2 步**\n\n" +
    "**📋 步骤状态**\n\n" +
    "✅ 生成初稿 · 策略负责人 · 已完成\n" +
    "• 质量审查 · 质量审查员 · 准备中\n\n" +
    "**下一步：** 质量审查 · 质量审查员\n\n" +
    "30 秒后自动继续，也可以直接选择操作。",
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

const structuredVariables = [
  ["progressText", "string", "醒目的进度、步骤数与剩余返工次数"],
  ["currentStep", "markdown", "当前步骤、角色、状态和耗时"],
  ["stepTimeline", "markdown", "紧凑步骤轨迹"],
  ["latestOutput", "markdown", "最近完成步骤的产出摘要"],
  ["latestReply", "markdown", "最近一次任务助手回复摘要"],
  ["result", "markdown", "任务最终结果或失败说明"],
  ["notice", "markdown", "等待确认和最近状态提示"],
  ["cardBody", "markdown", "状态、步骤状态、最终结果、产出或等待操作"],
];
for (const [id, type, description] of structuredVariables) {
  const existing = editor.variableList.find((variable) => variable.id === id);
  const definition = {
    id,
    type,
    name: id,
    description,
    private: false,
    editorVarType: "variables",
  };
  if (existing) Object.assign(existing, definition);
  else editor.variableList.push(definition);
}

const expectedMarkdownVariables = ["cardBody"];
for (const state of root.children ?? []) {
  const children = state.children?.[0]?.children ?? [];
  const renderedVariables = children
    .filter((child) => child.componentName === "MarkdownBlock")
    .map((child) => child.props?.content?.variable);
  if (JSON.stringify(renderedVariables) !== JSON.stringify(expectedMarkdownVariables)) {
    throw new Error(`卡片状态 ${state.props?.status} 的结构化内容组件不完整。`);
  }
  const buttonCount = children.filter((child) => child.componentName === "ButtonGroup").length;
  const expectedButtonCount = state.props?.status === 1 ? 1 : 0;
  if (buttonCount !== expectedButtonCount) {
    throw new Error(`卡片状态 ${state.props?.status} 的操作按钮数量不正确。`);
  }
  const baseTextCount = children.filter((child) => child.componentName === "BaseText").length;
  if (baseTextCount !== 1) {
    throw new Error(`卡片状态 ${state.props?.status} 只能保留一个标题组件。`);
  }
}

editor.editVersion = 5;
outer.editorData = JSON.stringify(editor);
writeFileSync(target, `${JSON.stringify(outer, null, 2)}\n`, "utf8");
