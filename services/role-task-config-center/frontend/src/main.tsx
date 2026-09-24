import React, {
  createContext,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  FreeLayoutEditorProvider,
  EditorRenderer,
  WorkflowNodeRenderer,
  useNodeRender,
  EditorState,
  WorkflowDragService,
  type FreeLayoutPluginContext,
  type WorkflowJSON,
  type WorkflowNodeRegistry,
} from "@flowgram.ai/free-layout-editor";
import "@flowgram.ai/free-layout-editor/index.css";
import "./style.css";
import {
  clone,
  normalize,
  orderedKeys,
  canConnect,
  insertStep,
  insertAcceptance,
  removeStep,
  key,
  permissions,
  expectedOutput,
  DraftHistory,
  type Draft,
  type Step,
  type Role,
  type Agent,
  type Edge,
} from "./model";

export interface EditorOptions {
  draft: Draft;
  roles: Role[];
  agents: Agent[];
  groups: { id: string; name: string }[];
  models: string[];
  online: boolean;
  onChange(d: Draft): void;
  onSave(d: Draft): Promise<Draft>;
  onError(message: string): void;
  onBack(): void;
}
const Actions = createContext({ select: (_id: string) => {} });
function NodeCard() {
  const { node, form } = useNodeRender();
  const actions = useContext(Actions);
  return (
    <WorkflowNodeRenderer
      node={node}
      className={`fg-card fg-${node.flowNodeType}`}
    >
      <div onClick={() => actions.select(node.id)} data-sop-node={node.id}>
        {form?.render()}
      </div>
    </WorkflowNodeRenderer>
  );
}
const registries: WorkflowNodeRegistry[] = ["start", "step", "end", "acceptance"].map(
  (type) => ({
    type,
    meta: {
      deleteDisable: true,
      copyDisable: true,
      inputDisable: type === "start",
      outputDisable: type === "end",
      defaultPorts: [
        ...(type !== "start"
          ? [{ type: "input" as const, location: "left" as const }]
          : []),
        ...(type !== "end"
          ? [{ type: "output" as const, location: "right" as const }]
          : []),
      ],
    },
    formMeta: {
      render: ({ form }) => (
        <>
          <div className="fg-card-heading">
            <span className="fg-icon">
              {type === "start" ? "▶" : type === "end" ? "■" : "◇"}
            </span>
            <strong>
              {type === "acceptance" ? form.values.name : type === "step"
                ? form.values.displayName
                : type === "start"
                  ? "开始"
                  : "结束"}
            </strong>
          </div>
          <p>
            {type === "acceptance" ? "通过继续；不通过修复，超限暂停" : type === "step"
              ? form.values.instruction || "配置本步骤执行要求"
              : type === "start"
                ? "从任务定义接收目标与输入"
                : "完成所有步骤并交付结果"}
          </p>
          {type === "step" && (
            <small>
              {form.values.roleName || "角色步骤"} ·{" "}
              {form.values.modelOverride || "继承默认模型"}
            </small>
          )}
        </>
      ),
    },
  }),
);
function toFlow(d: Draft): WorkflowJSON {
  return {
    nodes: d.editorGraph.nodes.map((n) => ({
      id: n.id,
      type: n.type,
      meta: { position: { x: n.x, y: n.y } },
      data:
        n.type === "step" ? clone(d.steps.find((s) => s.nodeKey === n.id)) : n.type === "acceptance" ? clone(n.acceptance) : {},
    })),
    edges: d.editorGraph.edges.map((e) => ({
      sourceNodeID: e.source,
      targetNodeID: e.target,
    })),
  };
}
function fromFlow(d: Draft, json: WorkflowJSON): Draft {
  return {
    ...d,
    steps: json.nodes
      .filter((n) => n.type === "step")
      .map((n) => ({ ...n.data, nodeKey: n.id })),
    editorGraph: {
      version: d.editorGraph.version,
      nodes: json.nodes.map((n) => ({
        id: n.id,
        type: n.type as "start" | "step" | "end" | "acceptance",
        ...(n.type === "acceptance" ? {acceptance: n.data} : {}),
        x: n.meta?.position?.x || 0,
        y: n.meta?.position?.y || 0,
      })),
      edges: json.edges.map((e) => ({
        source: e.sourceNodeID,
        target: e.targetNodeID,
      })),
    },
  };
}
const labels: Record<string, string> = {
  read_only: "只读",
  workspace_write: "工作区写入",
  auto_review: "自动审核",
  full_access: "完全访问",
};
function Field({
  label,
  value,
  onChange,
  multiline = false,
  type = "text",
  options,
  disabled = false,
  maxLength,
}: {
  label: string;
  value: string | number;
  onChange(v: string): void;
  multiline?: boolean;
  type?: string;
  options?: { value: string; label: string }[];
  disabled?: boolean;
  maxLength?: number;
}) {
  return (
    <label className="fg-field">
      <span>{label}</span>
      {options ? (
        <select
          aria-label={label}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : multiline ? (
        <textarea
          aria-label={label}
          value={value}
          maxLength={maxLength}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          aria-label={label}
          type={type}
          value={value}
          maxLength={maxLength}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}
export function Editor({ options }: { options: EditorOptions }) {
  const [draft, setDraft] = useState(() => normalize(options.draft));
  const current = useRef(draft);
  const history = useRef(new DraftHistory(draft));
  const initial = useRef(toFlow(draft));
  const baseline = useRef(JSON.stringify(draft));
  const ctx = useRef<FreeLayoutPluginContext>();
  const dragSubscription = useRef<{ dispose(): void }>();
  const loading = useRef(false);
  const savingRef = useRef(false);
  const [ready, setReady] = useState(false);
  const [arranging, setArranging] = useState(false);
  const [selected, setSelected] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [mouseMode, setMouseMode] = useState(true);
  const selectNode = (id: string) => {
    setSelected(id);
    setInspectorOpen(true);
  };
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [runtime, setRuntime] = useState({
    agents: options.agents,
    online: options.online,
  });
  const [, refreshHistory] = useState(0);
  const commit = (next: Draft, load = false, record = true) => {
    if (savingRef.current) return;
    if (!load) {
      for (const n of next.editorGraph.nodes.filter(n => n.type === "acceptance")) {
        const before = current.current.editorGraph.edges.find(e => e.target === n.id)?.source;
        const after = next.editorGraph.edges.find(e => e.target === n.id)?.source;
        if (after && before !== after) options.onError("IF 判断对象已按连线更新，请确认验收条件适用。");
      }
    }
    if (record) history.current.record(next);
    current.current = next;
    setDraft(next);
    options.onChange(clone(next));
    if (load && ctx.current) {
      loading.current = true;
      try {
        ctx.current.document.clear();
        ctx.current.document.fromJSON(toFlow(next));
      } finally {
        loading.current = false;
      }
    }
    refreshHistory((x) => x + 1);
  };
  const actions = useRef({ commit, select: setSelected });
  actions.current = { commit, select: setSelected };
  const props = useMemo(
    () => ({
      initialData: initial.current,
      nodeRegistries: registries,
      materials: { renderDefaultNode: NodeCard },
      nodeEngine: { enable: true },
      history: { enable: false, disableShortcuts: true },
      twoWayConnection: false,
      canDeleteNode: () => false,
      canResetLine: () => false,
      canAddLine: (
        _ctx: FreeLayoutPluginContext,
        from: { node: { id: string } },
        to: { node: { id: string } },
      ) => canConnect(current.current.editorGraph, from.node.id, to.node.id),
      onContentChange: (c: FreeLayoutPluginContext) => {
        if (!loading.current && !savingRef.current)
          actions.current.commit(
            fromFlow(current.current, c.document.toJSON()),
            false,
            !c.get(WorkflowDragService).isDragging,
          );
      },
      onAllLayersRendered: (c: FreeLayoutPluginContext) => {
        ctx.current = c;
        c.playground.editorState.changeState(
          EditorState.STATE_MOUSE_FRIENDLY_SELECT.id,
        );
        dragSubscription.current?.dispose();
        dragSubscription.current = c
          .get(WorkflowDragService)
          .onNodesDrag((event) => {
            if (event.type === "onDragEnd" && !savingRef.current)
              actions.current.commit(
                fromFlow(current.current, c.document.toJSON()),
              );
          });
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            if (ctx.current === c)
              c.tools.fitView(false).then(() => setReady(true));
          }),
        );
      },
      onDispose: () => {
        ctx.current = undefined;
      },
    }),
    [],
  );
  React.useEffect(() => {
    const listener = (e: Event) => {
      setRuntime((e as CustomEvent).detail);
    };
    window.addEventListener("sop-runtime", listener);
    const opened = () =>
      requestAnimationFrame(() =>
        requestAnimationFrame(() => ctx.current?.tools.fitView(false)),
      );
    window.addEventListener("sop-editor-open", opened);
    return () => {
      window.removeEventListener("sop-runtime", listener);
      window.removeEventListener("sop-editor-open", opened);
      dragSubscription.current?.dispose();
      ctx.current = undefined;
    };
  }, []);
  const roles = options.roles.filter((r) => r.groupId === draft.groupId);
  const agents = runtime.agents.filter((a) => a.groupId === draft.groupId);
  const gate = draft.editorGraph.nodes.find(n => n.id === selected && n.type === "acceptance");
  const updateGate = (field: string, value: string | number) => {
    const d = clone(current.current), n = d.editorGraph.nodes.find(n => n.id === selected)!;
    n.acceptance = {...n.acceptance!, [field]: value};
    commit(d, true);
  };
  const addGate = (point = {x: 350, y: 250}, edge?: Edge) => {
    const d = insertAcceptance(current.current, point, edge);
    commit(d, true); selectNode(d.editorGraph.nodes[d.editorGraph.nodes.length - 1].id);
  };
  const step = draft.steps.find((s) => s.nodeKey === selected);
  const selectedType = draft.editorGraph.nodes.find(
    (n) => n.id === selected,
  )?.type;
  const updateDraft = (field: keyof Draft, value: unknown) =>
    commit({ ...current.current, [field]: value });
  const updateStep = (field: keyof Step, value: unknown) => {
    if (!step) return;
    let next = { ...step, [field]: value };
    if (field === "roleId") {
      const role = roles.find((r) => r.id === value);
      next.roleName = role?.name;
    }
    if (
      field === "agentId" &&
      !permissions(agents.find((a) => a.agentId === value)).includes(
        next.permissionProfile,
      )
    )
      next.permissionProfile = "read_only";
    next.writeEnabled = next.permissionProfile !== "read_only";
    const d = {
      ...current.current,
      steps: current.current.steps.map((s) =>
        s.nodeKey === step.nodeKey ? next : s,
      ),
    };
    // 使用 FlowGram 表单更新节点数据，不重建画布和视口。
    loading.current = true;
    try {
      ctx.current?.document.getNode(step.nodeKey)?.form?.updateFormValues(next);
    } finally {
      loading.current = false;
    }
    commit(d);
  };
  const addRole = (role: Role, point = { x: 160, y: 240 }, edge?: Edge) => {
    if (
      savingRef.current ||
      !role.enabled ||
      role.groupId !== current.current.groupId
    )
      return;
    const s: Step = {
      nodeKey: key(),
      displayName: role.name,
      roleId: role.id,
      roleName: role.name,
      instruction: role.duty,
      expectedOutput,
      executorType: "local",
      agentId:
        agents.find((a) => a.enabled && a.capabilities.includes("executor"))
          ?.agentId || "",
      workingDirectory: "",
      permissionProfile: "read_only",
      writeEnabled: false,
      modelOverride: null,
      timeoutSec: 1800,
      skills: [],
      mcps: [],
    };
    commit(insertStep(current.current, s, point, edge), true);
    selectNode(s.nodeKey);
  };
  const undo = (redo = false) => {
    if (savingRef.current) return;
    const d = redo ? history.current.redo() : history.current.undo();
    if (d) commit(d, true, false);
  };
  const save = async () => {
    if (savingRef.current || arranging) return;
    try {
      orderedKeys(current.current.editorGraph, current.current.steps);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    if (!runtime.online) {
      setError("网关不可用，请恢复连接后再保存。");
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      const saved = normalize(await options.onSave(clone(current.current)));
      savingRef.current = false;
      history.current = new DraftHistory(saved);
      baseline.current = JSON.stringify(saved);
      commit(saved, true, false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };
  const controlKeys = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).matches("input,textarea,select")) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.stopPropagation();
      undo(e.shiftKey);
    }
  };
  const modelOptions = (inherit = false) => [
    ...(inherit ? [{ value: "", label: "继承工作流默认模型" }] : []),
    ...options.models.map((m) => ({ value: m, label: m })),
  ];
  const agentOptions = (capability: string, value: string) => {
    const list = agents.filter(
      (a) =>
        a.capabilities.includes(capability) &&
        (a.enabled || a.agentId === value),
    );
    const status = (a: Agent) =>
      !a.enabled
        ? "已停用"
        : a.connectionStatus === "offline"
          ? "离线"
          : a.connectionStatus === "online"
            ? a.availability === "busy"
              ? "在线忙碌"
              : "在线"
            : "状态未知";
    return [
      { value: "", label: "请选择执行机" },
      ...(!list.some((a) => a.agentId === value) && value
        ? [{ value, label: `${value}（不可用）` }]
        : []),
      ...list.map((a) => ({
        value: a.agentId,
        label: `${a.name || a.agentId} · ${status(a)}`,
      })),
    ];
  };
  return (
    <Actions.Provider value={{ select: selectNode }}>
      <div
        className="fg-editor"
        data-ready={ready && !arranging}
        data-palette={paletteOpen}
        data-inspector={inspectorOpen}
        onKeyDownCapture={controlKeys}
      >
        <div className="fg-toolbar">
          <button disabled={saving || arranging} onClick={options.onBack}>
            返回列表
          </button>
          <div>
            <strong>{draft.name || "未命名工作流"}</strong>
            <small>
              {saving
                ? "正在保存…"
                : JSON.stringify(draft) === baseline.current
                  ? "所有修改已保存"
                  : "有未保存修改"}
            </small>
          </div>
          <div className="fg-actions">
            <button
              disabled={saving || !history.current.past.length}
              onClick={() => undo()}
            >
              撤销
            </button>
            <button
              disabled={saving || !history.current.future.length}
              onClick={() => undo(true)}
            >
              重做
            </button>
            <button
              aria-expanded={paletteOpen}
              onClick={() => setPaletteOpen((v) => !v)}
            >
              角色库
            </button>
            <button
              disabled={saving}
              onClick={() => {
                setSelected("");
                setInspectorOpen(true);
              }}
            >
              流程设置
            </button>
            <button
              className="primary"
              disabled={saving || arranging}
              onClick={save}
            >
              {saving ? "正在保存…" : "保存工作流"}
            </button>
          </div>
        </div>
        {!runtime.online && (
          <div className="fg-notice">
            网关不可用，草稿仍可编辑；恢复连接后可保存。
          </div>
        )}
        {error && (
          <div className="fg-error" role="alert">
            {error}
          </div>
        )}
        <div
          className="fg-workspace"
          ref={(el) => {
            if (el) el.inert = saving || arranging;
          }}
        >
          <aside className="fg-palette">
            <h3>角色库</h3>
            <p>拖到空白处添加，拖到连线上插入。</p>
            {roles
              .filter((r) => r.enabled)
              .map((r) => (
                <button
                  key={r.id}
                  draggable
                  onDragStart={(e) => {
                    e.dataTransfer.setData("application/x-sop-role", r.id);
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  onClick={() => addRole(r)}
                  title={r.duty}
                >
                  <span>◇</span>
                  {r.name}
                  <b>＋</b>
                </button>
              ))}
            {!roles.some((r) => r.enabled) && <p>本组暂无启用的角色。</p>}
            <button draggable onDragStart={e => e.dataTransfer.setData("application/x-sop-role", "__acceptance__")} onClick={() => addGate()}>IF 判断 ＋</button>
            <div className="fg-palette-note">
              开始 → 角色步骤 → 结束
              <br />
              本版按连线顺序串行执行。
            </div>
          </aside>
          <section
            className="fg-canvas"
            onDragOver={(e) => {
              if (e.dataTransfer.types.includes("application/x-sop-role")) {
                e.preventDefault();
                e.dataTransfer.dropEffect = "copy";
              }
            }}
            onDrop={(e) => {
              const role = roles.find(
                (r) =>
                  r.id === e.dataTransfer.getData("application/x-sop-role"),
              );
              const isGate = e.dataTransfer.getData("application/x-sop-role") === "__acceptance__";
              if ((!role && !isGate) || !ctx.current) return;
              e.preventDefault();
              const point =
                ctx.current.playground.config.getPosFromMouseEvent(e);
              const line =
                ctx.current.document.linesManager.getCloseInLineFromMousePos(
                  point,
                  18,
                );
              if (isGate) { addGate(point, line?.from && line?.to ? {source: line.from.id, target: line.to.id} : undefined); return; }
              addRole(
                role!,
                point,
                line?.from && line?.to
                  ? { source: line.from.id, target: line.to.id }
                  : undefined,
              );
            }}
          >
            <FreeLayoutEditorProvider {...props} readonly={saving}>
              <EditorRenderer className="fg-renderer" />
            </FreeLayoutEditorProvider>
            <div className="fg-canvas-tools">
              <button
                aria-pressed={mouseMode}
                onClick={() => {
                  const next = !mouseMode;
                  setMouseMode(next);
                  ctx.current?.playground.editorState.changeState(
                    next
                      ? EditorState.STATE_MOUSE_FRIENDLY_SELECT.id
                      : EditorState.STATE_SELECT.id,
                  );
                }}
              >
                {mouseMode ? "小手模式" : "选择模式"}
              </button>
              <button onClick={() => ctx.current?.playground.config.zoomout()}>
                −
              </button>
              <button onClick={() => ctx.current?.playground.config.zoomin()}>
                ＋
              </button>
              <button onClick={() => ctx.current?.tools.fitView()}>
                适应画布
              </button>
              <button
                disabled={arranging}
                onClick={async () => {
                  if (!ctx.current) return;
                  setArranging(true);
                  try {
                    loading.current = true;
                    await ctx.current.tools.autoLayout({
                      enableAnimation: false,
                      layoutConfig: { rankdir: "LR" },
                    });
                    loading.current = false;
                    commit(
                      fromFlow(current.current, ctx.current.document.toJSON()),
                    );
                    await ctx.current.tools.fitView(false);
                  } catch (e) {
                    setError(String(e));
                  } finally {
                    loading.current = false;
                    setArranging(false);
                  }
                }}
              >
                自动整理
              </button>
            </div>
          </section>
          <aside className="fg-inspector">
            <button
              className="fg-close-inspector"
              aria-label="收起属性面板"
              onClick={() => setInspectorOpen(false)}
            >
              ×
            </button>
            <h3>
              {step
                ? "步骤设置"
                : selectedType === "acceptance" ? "IF 判断设置" : selectedType === "start"
                  ? "开始"
                  : selectedType === "end"
                    ? "结束"
                    : "流程设置"}
            </h3>
            {selectedType === "start" || selectedType === "end" ? (
              <>
                <p>
                  {selectedType === "start"
                    ? "任务目标与输入由任务定义提供。"
                    : "所有步骤完成后结束工作流，交付现有步骤结果。"}
                </p>
                <button onClick={() => setSelected("")}>编辑流程设置</button>
              </>
            ) : gate ? (
              <>
                <Field label="判断名称" value={gate.acceptance!.name} onChange={v => updateGate("name", v)} />
                <Field label="验收条件" multiline maxLength={10000} value={gate.acceptance!.criteria} onChange={v => updateGate("criteria", v)} />
                <Field label="最大自动修复次数" type="number" value={String(gate.acceptance!.maxRepairs)} onChange={v => updateGate("maxRepairs", Number(v))} />
                <p>当前判断对象：{draft.steps.find(s => s.nodeKey === draft.editorGraph.edges.find(e => e.target === gate.id)?.source)?.displayName || "尚未连接角色步骤"}</p>
                <p>使用紧邻前一步的原会话。通过继续，不通过修复；超限或需要决策时暂停。</p>
                <button onClick={() => {commit(removeStep(current.current, gate.id), true); setSelected("");}}>删除判断</button>
              </>
            ) : step ? (
              <>
                {roles.find((r) => r.id === step.roleId)?.enabled === false && (
                  <div className="fg-notice">当前角色已停用。</div>
                )}
                <Field
                  label="角色"
                  value={step.roleId}
                  onChange={(v) => updateStep("roleId", v)}
                  options={[
                    ...(!roles.some((r) => r.id === step.roleId)
                      ? [
                          {
                            value: step.roleId,
                            label: step.roleName || "角色已不可用",
                          },
                        ]
                      : []),
                    ...roles
                      .filter((r) => r.enabled || r.id === step.roleId)
                      .map((r) => ({
                        value: r.id,
                        label: r.name + (r.enabled ? "" : "（已停用）"),
                      })),
                  ]}
                />
                <Field
                  label="显示名称 *"
                  value={step.displayName}
                  maxLength={160}
                  onChange={(v) => updateStep("displayName", v)}
                />
                <Field
                  label="本步骤执行要求 *"
                  value={step.instruction}
                  multiline
                  onChange={(v) => updateStep("instruction", v)}
                />
                <Field
                  label="预期输出"
                  value={step.expectedOutput}
                  multiline
                  onChange={(v) => updateStep("expectedOutput", v)}
                />
                <Field
                  label="执行机 *"
                  value={step.agentId}
                  options={agentOptions("executor", step.agentId)}
                  onChange={(v) => updateStep("agentId", v)}
                />
                <Field
                  label="模型"
                  value={step.modelOverride || ""}
                  options={modelOptions(true)}
                  onChange={(v) => updateStep("modelOverride", v || null)}
                />
                <Field
                  label="工作目录"
                  value={step.workingDirectory}
                  maxLength={1000}
                  onChange={(v) => updateStep("workingDirectory", v)}
                />
                <Field
                  label="超时（秒）"
                  value={step.timeoutSec}
                  type="number"
                  onChange={(v) => updateStep("timeoutSec", Number(v))}
                />
                <Field
                  label="权限档位"
                  value={step.permissionProfile}
                  options={[
                    ...new Set([
                      step.permissionProfile,
                      ...permissions(
                        agents.find((a) => a.agentId === step.agentId),
                      ),
                    ]),
                  ].map((p) => ({ value: p, label: labels[p] || p }))}
                  onChange={(v) => updateStep("permissionProfile", v)}
                />
                <details>
                  <summary>高级设置</summary>
                  <TagField
                    label="Skill 标签"
                    values={step.skills}
                    onChange={(v) => updateStep("skills", v)}
                  />
                  <TagField
                    label="MCP 标签"
                    values={step.mcps}
                    onChange={(v) => updateStep("mcps", v)}
                  />
                </details>
                <div className="fg-step-actions">
                  <button
                    onClick={() => {
                      const n = { ...clone(step), nodeKey: key() };
                      delete n.id;
                      const pos = draft.editorGraph.nodes.find(
                        (x) => x.id === step.nodeKey,
                      )!;
                      commit(
                        insertStep(current.current, n, {
                          x: pos.x + 30,
                          y: pos.y + 180,
                        }),
                        true,
                      );
                      setSelected(n.nodeKey);
                    }}
                  >
                    复制步骤
                  </button>
                  <button
                    className="fg-danger"
                    onClick={() => {
                      const target = current.current.editorGraph.edges.find(e => e.source === step.nodeKey)?.target;
                      if (current.current.editorGraph.nodes.find(n => n.id === target)?.type === "acceptance" && !window.confirm("删除此步骤也会删除其后的 IF 判断，是否继续？")) return;
                      commit(removeStep(current.current, step.nodeKey), true);
                      setSelected("");
                    }}
                  >
                    删除步骤
                  </button>
                </div>
              </>
            ) : (
              <>
                <Field
                  label="工作流名称 *"
                  value={draft.name}
                  maxLength={160}
                  onChange={(v) => updateDraft("name", v)}
                />
                <Field
                  label="说明"
                  value={draft.description || ""}
                  maxLength={2000}
                  multiline
                  onChange={(v) => updateDraft("description", v)}
                />
                <Field
                  label="所属分组 *"
                  value={draft.groupId || ""}
                  options={[
                    { value: "", label: "请选择分组" },
                    ...options.groups.map((g) => ({
                      value: g.id,
                      label: g.name,
                    })),
                  ]}
                  onChange={(v) => updateDraft("groupId", v)}
                />
                <Field
                  label="主监督 *"
                  value={draft.supervisorAgentId}
                  options={agentOptions("supervisor", draft.supervisorAgentId)}
                  onChange={(v) => updateDraft("supervisorAgentId", v)}
                />
                <Field
                  label="默认模型"
                  value={draft.defaultStepModel}
                  options={modelOptions()}
                  onChange={(v) => updateDraft("defaultStepModel", v)}
                />
                <Field
                  label="主监督最长时间（秒）"
                  value={draft.supervisorTimeoutSec}
                  type="number"
                  onChange={(v) =>
                    updateDraft("supervisorTimeoutSec", Number(v))
                  }
                />
                <Field
                  label="最多重跑次数"
                  value={draft.maxRetryCount}
                  type="number"
                  onChange={(v) => updateDraft("maxRetryCount", Number(v))}
                />
                <Field
                  label="步骤流转"
                  value={draft.advanceMode}
                  options={[
                    { value: "automatic", label: "全自动" },
                    { value: "semi_automatic", label: "半自动（等待确认）" },
                  ]}
                  onChange={(v) => updateDraft("advanceMode", v)}
                />
                <Field
                  label="结果交接"
                  value={draft.handoffMode}
                  options={[
                    { value: "legacy_text", label: "文字交接" },
                    { value: "cumulative_files", label: "累计文件交接" },
                  ]}
                  onChange={(v) => updateDraft("handoffMode", v)}
                />
                <label className="fg-check">
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(e) => updateDraft("enabled", e.target.checked)}
                  />
                  启用工作流
                </label>
              </>
            )}
            {selected && selectedType !== "end" && (
              <>
                <h4>连接到</h4>
                {draft.editorGraph.edges
                  .filter((e) => e.source === selected)
                  .map((e) => (
                    <div className="fg-edge-row" key={e.target}>
                      <span>
                        {draft.steps.find((s) => s.nodeKey === e.target)
                          ?.displayName || "结束"}
                      </span>
                      <button
                        onClick={() =>
                          commit(
                            {
                              ...current.current,
                              editorGraph: {
                                ...current.current.editorGraph,
                                edges: current.current.editorGraph.edges.filter(
                                  (x) => x !== e,
                                ),
                              },
                            },
                            true,
                          )
                        }
                      >
                        断开
                      </button>
                    </div>
                  ))}
                <p>从右侧端口拖到下一步骤左侧端口连接。</p>
              </>
            )}
          </aside>
        </div>
      </div>
    </Actions.Provider>
  );
}
function TagField({
  label,
  values,
  onChange,
}: {
  label: string;
  values: string[];
  onChange(v: string[]): void;
}) {
  const [text, setText] = useState(values.join(", "));
  React.useEffect(() => {
    if (
      text
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean)
        .join() !== values.join()
    )
      setText(values.join(", "));
  }, [values]);
  return (
    <Field
      label={label}
      value={text}
      onChange={(v) => {
        setText(v);
        onChange(
          v
            .split(/[,，]/)
            .map((s) => s.trim())
            .filter(Boolean),
        );
      }}
    />
  );
}
declare global {
  interface Window {
    SopEditor: {
      normalize: typeof normalize;
      orderedKeys: typeof orderedKeys;
      mount(el: HTMLElement, options: EditorOptions): () => void;
    };
  }
}
window.SopEditor = {
  normalize,
  orderedKeys,
  mount(el, options) {
    const root = createRoot(el);
    root.render(<Editor options={options} />);
    return () => root.unmount();
  },
};
