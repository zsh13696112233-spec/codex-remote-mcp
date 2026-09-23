import { describe, it, expect } from "vitest";
import {
  normalize,
  orderedKeys,
  canConnect,
  removeStep,
  insertStep,
  DraftHistory,
  clone,
  type Step,
} from "./model";
const step = (nodeKey: string): Step => ({
  nodeKey,
  displayName: nodeKey,
  roleId: "same-role",
  instruction: "执行",
  expectedOutput: "输出",
  executorType: "local",
  agentId: "machine",
  workingDirectory: "",
  permissionProfile: "read_only",
  writeEnabled: false,
  modelOverride: null,
  timeoutSec: 60,
  skills: [],
  mcps: [],
});
export const fixture = () =>
  normalize({
    name: "流程",
    description: "",
    groupId: "g",
    supervisorAgentId: "machine",
    supervisorTimeoutSec: 7200,
    maxRetryCount: 10,
    advanceMode: "automatic",
    handoffMode: "legacy_text",
    defaultStepModel: "gpt-5.6-sol",
    enabled: true,
    steps: [step("a"), step("b"), step("c")],
  });
describe("串行图契约", () => {
  it("旧数据缺少 nodeKey 时使用步骤 ID，转换后保存再打开仍保持标识和布局", () => {
    const legacy = {
      ...fixture(),
      editorGraph: undefined,
      steps: [
        { ...step(""), id: "old-a" },
        { ...step(""), id: "old-b" },
      ],
    };
    const d = normalize(legacy);
    expect(orderedKeys(d.editorGraph, d.steps)).toEqual(["old-a", "old-b"]);
    d.editorGraph.nodes[1].x = 512;
    expect(normalize(JSON.parse(JSON.stringify(d)))).toEqual(d);
  });
  it("重复角色拥有不同节点标识，旧 SOP 转为串行图", () => {
    const d = fixture();
    expect(orderedKeys(d.editorGraph, d.steps)).toEqual(["a", "b", "c"]);
    expect(normalize(d)).toEqual(d);
  });
  it("顺序来自连线，不依赖数组或坐标", () => {
    const d = fixture();
    d.steps.reverse();
    d.editorGraph.nodes.reverse();
    d.editorGraph.nodes[2].x = -1000;
    expect(orderedKeys(d.editorGraph, d.steps)).toEqual(["a", "b", "c"]);
  });
  it.each([
    "断链",
    "孤立",
    "自环",
    "分叉",
    "汇合",
    "重复节点",
    "节点不匹配",
    "非法坐标",
    "版本",
  ])("拒绝%s", (kind) => {
    const d = fixture();
    if (kind === "断链") d.editorGraph.edges.pop();
    if (kind === "孤立")
      d.editorGraph.edges = [
        { source: "start", target: "end" },
        { source: "a", target: "b" },
        { source: "b", target: "c" },
        { source: "c", target: "a" },
      ];
    if (kind === "自环") d.editorGraph.edges[1].target = "a";
    if (kind === "分叉") d.editorGraph.edges[1].source = "start";
    if (kind === "汇合") d.editorGraph.edges[1].target = "end";
    if (kind === "重复节点") d.editorGraph.nodes[2].id = "a";
    if (kind === "节点不匹配") d.steps[0].nodeKey = "missing";
    if (kind === "非法坐标") d.editorGraph.nodes[0].x = Infinity;
    if (kind === "版本") d.editorGraph.version = 2;
    expect(() => orderedKeys(d.editorGraph, d.steps)).toThrow();
  });
  it("插入连线和删除中间步骤保持链路", () => {
    let d = fixture();
    d = insertStep(
      d,
      step("d"),
      { x: 100, y: 200 },
      { source: "a", target: "b" },
    );
    expect(orderedKeys(d.editorGraph, d.steps)).toEqual(["a", "d", "b", "c"]);
    d = removeStep(d, "b");
    expect(orderedKeys(d.editorGraph, d.steps)).toEqual(["a", "d", "c"]);
    expect(removeStep(d, "start")).toEqual(d);
  });
  it("空白处新增节点必须连接后才能保存", () => {
    const d = insertStep(fixture(), step("d"), { x: 10, y: 20 });
    expect(() => orderedKeys(d.editorGraph, d.steps)).toThrow();
  });
  it("端口阻止回环、分叉及重复连线，允许重连", () => {
    const g = fixture().editorGraph;
    expect(canConnect(g, "a", "b")).toBe(false);
    expect(canConnect(g, "end", "start")).toBe(false);
    g.edges = g.edges.filter((e) => e.source !== "b");
    expect(canConnect(g, "b", "a")).toBe(false);
    expect(canConnect(g, "b", "c")).toBe(true);
  });
  it("属性与布局一起撤销重做，历史不会污染当前草稿", () => {
    const d = fixture(),
      history = new DraftHistory(d);
    const changed = clone(d);
    changed.name = "新名称";
    changed.steps[0].instruction = "新要求";
    changed.editorGraph.nodes[1].x = 500;
    history.record(changed);
    changed.name = "外部修改";
    expect(history.undo()).toEqual(d);
    expect(history.redo()?.name).toBe("新名称");
    history.undo();
    history.record({ ...d, name: "另一修改" });
    expect(history.redo()).toBeUndefined();
  });
});
