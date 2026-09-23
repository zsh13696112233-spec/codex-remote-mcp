export interface Step {
  nodeKey: string;
  id?: string;
  displayName: string;
  roleId: string;
  roleName?: string;
  instruction: string;
  expectedOutput: string;
  executorType: string;
  agentId: string;
  workingDirectory: string;
  permissionProfile: string;
  writeEnabled: boolean;
  modelOverride: string | null;
  timeoutSec: number;
  skills: string[];
  mcps: string[];
}
export interface GraphNode {
  id: string;
  type: "start" | "step" | "end";
  x: number;
  y: number;
}
export interface Edge {
  source: string;
  target: string;
}
export interface Graph {
  version: number;
  nodes: GraphNode[];
  edges: Edge[];
}
export interface Draft {
  id?: string;
  name: string;
  description: string;
  groupId: string;
  supervisorAgentId: string;
  supervisorTimeoutSec: number;
  maxRetryCount: number;
  advanceMode: string;
  handoffMode: string;
  defaultStepModel: string;
  enabled: boolean;
  steps: Step[];
  editorGraph: Graph;
}
export interface Role {
  id: string;
  name: string;
  duty: string;
  groupId: string;
  enabled: boolean;
}
export interface Agent {
  agentId: string;
  name?: string;
  groupId: string;
  enabled: boolean;
  capabilities: string[];
  permissionProfiles?: string[];
  allowWrite?: boolean;
  connectionStatus?: string;
  availability?: string;
}
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export function key(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return (
    "node-" + Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("")
  );
}
export const expectedOutput = "完成本步骤，并返回清晰、完整且可验证的结果。";
export function normalize(raw: Partial<Draft>): Draft {
  const d = clone(raw) as Draft;
  d.steps = (d.steps || []).map((s) => ({
    ...s,
    nodeKey: s.nodeKey || s.id || key(),
    expectedOutput: s.expectedOutput || expectedOutput,
    skills: s.skills || [],
    mcps: s.mcps || [],
    workingDirectory: s.workingDirectory || "",
    permissionProfile: s.permissionProfile || "read_only",
  }));
  if (!d.editorGraph) {
    const ids = ["start", ...d.steps.map((s) => s.nodeKey), "end"];
    d.editorGraph = {
      version: 1,
      nodes: ids.map((id, i) => ({
        id,
        type: i === 0 ? "start" : i === ids.length - 1 ? "end" : "step",
        x: i * 320,
        y: 100,
      })),
      edges: ids.slice(1).map((id, i) => ({ source: ids[i], target: id })),
    };
  }
  return d;
}
export function orderedKeys(g: Graph, steps: Step[]): string[] {
  const fail = () => {
    throw Error("请将开始、全部角色步骤和结束连接成一条完整串行链。");
  };
  const nodes = new Map(g.nodes.map((n) => [n.id, n]));
  const start = g.nodes.filter((n) => n.type === "start"),
    end = g.nodes.filter((n) => n.type === "end");
  if (
    g.version !== 1 ||
    !steps.length ||
    nodes.size !== g.nodes.length ||
    start.length !== 1 ||
    end.length !== 1 ||
    g.nodes.length !== steps.length + 2 ||
    g.edges.length !== g.nodes.length - 1
  )
    fail();
  if (
    g.nodes.some(
      (n) =>
        !/^[A-Za-z0-9_-]{1,128}$/.test(n.id) ||
        !["start", "step", "end"].includes(n.type) ||
        !Number.isFinite(n.x) ||
        !Number.isFinite(n.y) ||
        Math.abs(n.x) > 1000000 ||
        Math.abs(n.y) > 1000000,
    )
  )
    fail();
  if (
    new Set(steps.map((s) => s.nodeKey)).size !== steps.length ||
    steps.some((s) => nodes.get(s.nodeKey)?.type !== "step")
  )
    fail();
  const next = new Map<string, string>(),
    incoming = new Set<string>();
  for (const e of g.edges) {
    if (
      !nodes.has(e.source) ||
      !nodes.has(e.target) ||
      e.source === e.target ||
      next.has(e.source) ||
      incoming.has(e.target) ||
      e.source === end[0].id ||
      e.target === start[0].id
    )
      fail();
    next.set(e.source, e.target);
    incoming.add(e.target);
  }
  const seen = new Set<string>(),
    order: string[] = [];
  let id: string | undefined = start[0].id;
  while (id && !seen.has(id)) {
    seen.add(id);
    if (nodes.get(id)?.type === "step") order.push(id);
    if (id === end[0].id) break;
    id = next.get(id);
  }
  if (id !== end[0].id || seen.size !== nodes.size) fail();
  return order;
}
export function canConnect(g: Graph, source: string, target: string): boolean {
  if (
    !g.nodes.some((n) => n.id === source) ||
    !g.nodes.some((n) => n.id === target)
  )
    return false;
  if (
    source === target ||
    g.nodes.find((n) => n.id === source)?.type === "end" ||
    g.nodes.find((n) => n.id === target)?.type === "start"
  )
    return false;
  if (g.edges.some((e) => e.source === source || e.target === target))
    return false;
  const next = new Map(g.edges.map((e) => [e.source, e.target]));
  const seen = new Set<string>();
  let id: string | undefined = target;
  while (id && !seen.has(id)) {
    if (id === source) return false;
    seen.add(id);
    id = next.get(id);
  }
  return true;
}
export function removeStep(d: Draft, id: string): Draft {
  const out = clone(d);
  if (out.editorGraph.nodes.find((n) => n.id === id)?.type !== "step")
    return out;
  const incoming = out.editorGraph.edges.find((e) => e.target === id),
    outgoing = out.editorGraph.edges.find((e) => e.source === id);
  out.steps = out.steps.filter((s) => s.nodeKey !== id);
  out.editorGraph.nodes = out.editorGraph.nodes.filter((n) => n.id !== id);
  out.editorGraph.edges = out.editorGraph.edges.filter(
    (e) => e.source !== id && e.target !== id,
  );
  if (incoming && outgoing)
    out.editorGraph.edges.push({
      source: incoming.source,
      target: outgoing.target,
    });
  return out;
}
export function insertStep(
  d: Draft,
  step: Step,
  point: { x: number; y: number },
  edge?: Edge,
): Draft {
  const out = clone(d);
  out.steps.push(step);
  out.editorGraph.nodes.push({ id: step.nodeKey, type: "step", ...point });
  if (edge) {
    out.editorGraph.edges = out.editorGraph.edges.filter(
      (e) => e.source !== edge.source || e.target !== edge.target,
    );
    out.editorGraph.edges.push(
      { source: edge.source, target: step.nodeKey },
      { source: step.nodeKey, target: edge.target },
    );
  }
  return out;
}
export function permissions(agent?: Agent): string[] {
  return agent?.permissionProfiles?.length
    ? agent.permissionProfiles
    : agent?.allowWrite
      ? ["read_only", "workspace_write"]
      : ["read_only"];
}

/** 单一快照历史同时覆盖画布和属性；视口不在历史中。 */
export class DraftHistory {
  past: Draft[] = [];
  future: Draft[] = [];
  constructor(public current: Draft) {}
  record(next: Draft) {
    if (JSON.stringify(next) === JSON.stringify(this.current)) return false;
    this.past.push(clone(this.current));
    if (this.past.length > 100) this.past.shift();
    this.future = [];
    this.current = clone(next);
    return true;
  }
  undo() {
    const prev = this.past.pop();
    if (!prev) return;
    this.future.push(this.current);
    this.current = prev;
    return clone(prev);
  }
  redo() {
    const next = this.future.pop();
    if (!next) return;
    this.past.push(this.current);
    this.current = next;
    return clone(next);
  }
}
