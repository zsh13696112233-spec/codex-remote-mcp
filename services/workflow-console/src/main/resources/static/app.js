const STATUS = {
  pending: "未开始", queued: "进行中", running: "进行中", cancelling: "进行中",
  completed: "已完成", skipped: "已跳过", failed: "未完成", cancelled: "未完成", interrupted: "未完成"
};
const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
const ACTIVE = new Set(["queued", "running", "cancelling"]);
const FAILED = new Set(["failed", "cancelled", "interrupted"]);
const state = {
  workflowId: new URLSearchParams(location.search).get("workflowId")?.trim() || "",
  cursor: 0, timer: null, durationTimer: null, messages: [], snapshot: null,
  refreshError: null, refreshRetrying: false, refreshing: false, sending: false
};
const $ = selector => document.querySelector(selector);
const text = value => value == null ? "" : String(value);
const zh = status => STATUS[status] || "状态未知";
const fmt = value => value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "—";

function elapsed(start, end) {
  if (!start) return "—";
  const seconds = Math.floor(Math.max(0, new Date(end || Date.now()) - new Date(start)) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours}小时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${rest}秒`;
  return `${rest}秒`;
}

function visualState(status) {
  if (ACTIVE.has(status)) return "running";
  if (status === "completed") return "completed";
  if (status === "skipped") return "skipped";
  if (FAILED.has(status)) return "failed";
  return "pending";
}

function isInitializing(snapshot) {
  if (!snapshot || !["queued", "running"].includes(snapshot.status)) return false;
  const nodes = snapshot.nodes || [];
  return nodes.length > 0 && nodes.every(node => node.status === "pending" && !node.startedAt);
}

function initializationMessage(snapshot) {
  const createdAt = new Date(snapshot?.createdAt || snapshot?.startedAt || 0).getTime();
  const waitingTooLong = Number.isFinite(createdAt) && createdAt > 0 && Date.now() - createdAt >= 30000;
  return waitingTooLong
    ? "准备时间较长，系统仍在尝试，请稍候…"
    : "任务已接收，正在准备运行环境和第 1 步…";
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const raw = await response.text();
  let body = {};
  try { body = raw ? JSON.parse(raw) : {}; } catch { body = {error: raw}; }
  if (!response.ok) {
    const error = new Error(body.error || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.style.display = "block";
  setTimeout(() => element.style.display = "none", 3500);
}

async function gateway() {
  try {
    await api("/api/gateway/ready");
    $("#gateway").className = "connection online";
    $("#gateway").textContent = "● 网关连接正常";
  } catch {
    $("#gateway").className = "connection offline";
    $("#gateway").textContent = "● 网关暂时无法连接";
  }
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const snapshot = await api(`/api/workflows/${encodeURIComponent(state.workflowId)}`);
    state.snapshot = snapshot;
    state.refreshError = null;
    state.refreshRetrying = false;
    render(snapshot);
    await events();
    if (TERMINAL.has(snapshot.status) && Number(snapshot.pendingChatCount || 0) === 0 && !state.sending) stop();
  } catch (error) {
    const firstFailure = !state.refreshError;
    state.refreshError = error.message;
    state.refreshRetrying = !error.status || error.status >= 500 || [408, 429].includes(error.status);
    renderRefreshError();
    if (state.refreshRetrying) {
      if (firstFailure) toast("连接暂时中断，正在自动重试");
    } else {
      stop();
      if (firstFailure) toast(error.message);
    }
  } finally {
    state.refreshing = false;
  }
}

async function events() {
  let more = true;
  while (more) {
    const batch = await api(`/api/workflows/${encodeURIComponent(state.workflowId)}/events?after=${state.cursor}&limit=200`);
    const list = batch.events || [];
    list.forEach(consume);
    more = list.length === 200;
    if (list.length) state.cursor = Math.max(...list.map(event => Number(event.sequence || 0)), state.cursor);
  }
  renderMessages();
}

function consume(event) {
  const payload = event.payload || {};
  if (event.type === "chat.user.accepted") {
    upsertMessage({id: payload.messageId, role: "user", time: event.createdAt,
      text: text(payload.text), status: "accepted", streaming: false});
    return;
  }
  if (event.type === "chat.user.forwarded") {
    const value = findMessage(payload.messageId);
    if (value) value.status = "processing";
    return;
  }
  if (event.type === "chat.assistant.delta") {
    let value = findMessage(payload.assistantMessageId);
    if (!value) {
      value = {id: payload.assistantMessageId, replyTo: payload.messageId, role: "assistant",
        time: event.createdAt, text: "", status: "processing", streaming: true};
      state.messages.push(value);
    }
    value.text += text(payload.delta);
    value.streaming = true;
    return;
  }
  if (event.type === "chat.assistant.completed") {
    let value = findMessage(payload.assistantMessageId);
    if (!value) {
      value = {id: payload.assistantMessageId, replyTo: payload.messageId,
        role: "assistant", time: event.createdAt, text: ""};
      state.messages.push(value);
    }
    value.text = text(payload.text);
    value.status = "completed";
    value.streaming = false;
    const user = findMessage(payload.messageId);
    if (user) user.status = "completed";
    return;
  }
  if (event.type === "chat.message.failed") {
    const value = findMessage(payload.messageId);
    if (value) {
      value.status = "failed";
      value.error = text(payload.error);
    }
    state.messages.filter(item => item.replyTo === payload.messageId && item.streaming)
      .forEach(item => { item.streaming = false; item.status = "failed"; item.error = text(payload.error); });
    return;
  }
  if (event.source !== "supervisor") return;
  const message = payload.message;
  const method = message?.method;
  const params = message?.params || {};
  if (method === "item/agentMessage/delta" && typeof params.delta === "string") {
    let draft = state.messages.at(-1);
    if (!draft || draft.role !== "progress" || !draft.streaming) {
      draft = {id: `progress-${event.sequence}`, role: "progress", time: event.createdAt, text: "", streaming: true};
      state.messages.push(draft);
    }
    draft.text += params.delta;
    return;
  }
  if (method === "item/completed" && params.item?.type === "agentMessage" && params.item.text) {
    const value = text(params.item.text);
    const draft = state.messages.at(-1);
    if (draft?.role === "progress" && draft.streaming) {
      draft.text = value;
      draft.streaming = false;
    } else if (state.messages.at(-1)?.text !== value) {
      state.messages.push({id: `progress-${event.sequence}`, role: "progress", time: event.createdAt, text: value, streaming: false});
    }
    return;
  }
  if (typeof event.payload?.message === "string") {
    state.messages.push({id: `progress-${event.sequence}`, role: "progress", time: event.createdAt, text: event.payload.message, streaming: false});
  }
}

function findMessage(id) { return state.messages.find(item => item.id === id); }
function upsertMessage(value) {
  const existing = findMessage(value.id);
  if (existing) Object.assign(existing, value);
  else state.messages.push(value);
}

function make(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value != null) element.textContent = value;
  return element;
}

function renderMessages() {
  const box = $("#messages");
  const stayAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  if (!state.messages.length) {
    const empty = make("div", "chat-empty");
    const message = state.refreshError
      ? state.refreshRetrying ? "连接暂时中断，正在自动重试" : "暂时无法读取任务信息"
      : isInitializing(state.snapshot)
        ? "系统正在准备任务，完成后将在这里显示进展"
        : "正在等待任务消息";
    empty.append(make("span", "", "···"), make("p", "", message));
    box.append(empty);
    return;
  }
  state.messages.forEach((message, index) => {
    const row = make("article", `chat-row ${message.role || "progress"}${message.streaming ? " streaming" : ""}${message.status === "failed" ? " failed" : ""}`);
    const isUser = message.role === "user";
    const avatar = make("div", "avatar", isUser ? "我" : "AI");
    const content = make("div", "chat-content");
    const meta = make("div", "chat-meta");
    const isFinal = message.role === "progress" && index === state.messages.length - 1 && TERMINAL.has(state.snapshot?.status);
    const sourceLabel = message.role === "progress" ? (isFinal ? "任务结果" : "任务进度") : "任务助手";
    meta.append(make("strong", "", isUser ? "我" : sourceLabel), make("time", "", fmt(message.time)));
    const bubble = make("div", "bubble");
    bubble.append(make("p", "", message.text));
    if (message.streaming) bubble.append(make("i", "typing-caret"));
    if (message.status === "failed") {
      bubble.append(make("span", "chat-error", message.error || "发送失败，请重试"));
      const retry = make("button", "retry-message", "重试");
      retry.type = "button";
      retry.onclick = () => sendChatMessage(message.id, message.text);
      bubble.append(retry);
    }
    content.append(meta, bubble);
    row.append(avatar, content);
    box.append(row);
  });
  if (stayAtBottom) box.scrollTop = box.scrollHeight;
}

function addDetail(grid, label, value) {
  const item = make("div", "detail-item");
  const content = make("strong", "", value || "—");
  item.append(make("span", "", label), content);
  grid.append(item);
  return content;
}

function resultTextWithoutImageLinks(value, hasArtifacts) {
  const valueText = text(value);
  if (!hasArtifacts) return valueText;
  return valueText
    .replace(/\[[^\]]*\]\([^)]+\.(?:png|jpe?g|gif|webp)\)/gi, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function appendStepArtifacts(result, node) {
  const artifacts = Array.isArray(node.artifacts) ? node.artifacts : [];
  if (!artifacts.length) return;
  const gallery = make("div", "step-artifacts");
  artifacts.forEach((artifact, index) => {
    const figure = make("figure", "step-artifact");
    const link = make("a", "step-image-link");
    const imageUrl = `/api/workflows/${encodeURIComponent(state.workflowId)}/artifacts/${encodeURIComponent(artifact.id)}`;
    link.href = imageUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "打开原图";
    const image = make("img", "step-image");
    image.src = imageUrl;
    image.alt = artifacts.length === 1 ? "本步骤生成的图片" : `本步骤生成的第 ${index + 1} 张图片`;
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", () => {
      figure.classList.add("failed");
      link.replaceChildren(make("span", "step-image-error", "图片暂时无法加载"));
    }, {once: true});
    link.append(image);
    figure.append(link, make("figcaption", "", artifacts.length === 1 ? "查看生成图片" : `查看生成图片 ${index + 1}`));
    gallery.append(figure);
  });
  result.append(gallery);
}

function renderSteps(nodes, initializing = false) {
  const list = $("#steps");
  list.replaceChildren();
  nodes.forEach((node, index) => {
    const stateName = visualState(node.status);
    const row = make("article", `step ${stateName}`);
    const rail = make("div", "step-rail");
    const marker = make("span", "step-marker");
    if (stateName === "running") marker.append(make("i", "spinner"));
    else if (stateName === "completed") marker.textContent = "✓";
    else if (stateName === "failed") marker.textContent = "!";
    else marker.textContent = String(index + 1);
    rail.append(marker);
    if (index < nodes.length - 1) rail.append(make("i", "rail-line"));

    const card = make("div", "step-card");
    const head = make("div", "step-head");
    const title = make("div", "step-title");
    title.append(make("span", "step-number", `第 ${index + 1} 步`), make("h3", "", node.displayName || "任务步骤"));
    head.append(title, make("span", `step-state ${stateName}`, zh(node.status)));

    const details = make("div", "step-details");
    addDetail(details, "执行机", node.agentId || "未指定");
    addDetail(details, "负责角色", node.roleName || "未指定角色");
    addDetail(details, "开始时间", fmt(node.startedAt));
    addDetail(details, "结束时间", fmt(node.finishedAt));
    const duration = addDetail(details, "执行耗时", elapsed(node.startedAt, node.finishedAt));
    if (node.startedAt) {
      duration.dataset.elapsedStart = node.startedAt;
      if (node.finishedAt) duration.dataset.elapsedEnd = node.finishedAt;
    }
    card.append(head, details);

    if (node.response || node.error) {
      const result = make("div", `step-result${node.error ? " error" : ""}`);
      const hasArtifacts = Array.isArray(node.artifacts) && node.artifacts.length > 0;
      const resultText = resultTextWithoutImageLinks(node.error || node.response, hasArtifacts);
      result.append(make("span", "", node.error ? "未完成原因" : "步骤结果"));
      if (resultText) result.append(make("p", "", resultText));
      if (!node.error) appendStepArtifacts(result, node);
      card.append(result);
    } else if (stateName === "running" || (initializing && index === 0)) {
      const waiting = make("div", "step-waiting");
      const message = initializing && index === 0
        ? initializationMessage(state.snapshot)
        : "正在执行，请稍候…";
      waiting.append(make("i", "pulse-dot"), make("span", "", message));
      card.append(waiting);
    }
    row.append(rail, card);
    list.append(row);
  });
}

function render(snapshot) {
  $("#name").textContent = snapshot.name || "未命名任务";
  $("#id").textContent = snapshot.workflowId || state.workflowId;
  const status = $("#status");
  status.textContent = isInitializing(snapshot) ? "准备中" : zh(snapshot.status);
  status.className = `status ${visualState(snapshot.status)}`;
  const nodes = snapshot.nodes || [];
  const initializing = isInitializing(snapshot);
  const active = nodes.find(node => ACTIVE.has(node.status));
  const failed = nodes.find(node => FAILED.has(node.status));
  $("#current").textContent = initializing
    ? initializationMessage(snapshot)
    : active
    ? `第 ${nodes.indexOf(active) + 1} 步：${active.displayName || "正在执行"}`
    : failed ? `第 ${nodes.indexOf(failed) + 1} 步未完成`
      : snapshot.status === "completed" ? "全部步骤已完成" : "尚未开始";
  $("#progress").textContent = `${snapshot.progress?.completed || 0} / ${snapshot.progress?.total || nodes.length}`;
  $("#retries").textContent = snapshot.retryPolicy
    ? `${snapshot.retryPolicy.remainingRetries} / ${snapshot.retryPolicy.maxRetries}`
    : "—";
  renderDuration();
  $("#updated").textContent = new Date().toLocaleString("zh-CN", {hour12: false});
  renderSteps(nodes, initializing);
  renderComposer();
}

function renderRefreshError() {
  const status = $("#status");
  status.textContent = state.refreshRetrying ? "重新连接中" : "无法加载";
  status.className = "status failed";
  $("#current").textContent = state.refreshRetrying
    ? "连接暂时中断，正在自动重试…"
    : state.refreshError;
  $("#updated").textContent = new Date().toLocaleString("zh-CN", {hour12: false});
  if (!state.snapshot) {
    $("#name").textContent = "正在加载任务";
    $("#id").textContent = state.workflowId;
  }
  renderMessages();
}

function renderDuration() {
  if (!state.snapshot) return;
  $("#duration").textContent = elapsed(state.snapshot.startedAt, state.snapshot.finishedAt);
  document.querySelectorAll("[data-elapsed-start]").forEach(element => {
    element.textContent = elapsed(element.dataset.elapsedStart, element.dataset.elapsedEnd);
  });
}

function renderComposer() {
  const input = $("#chatInput");
  const button = $("#sendMessage");
  input.disabled = state.sending;
  button.disabled = state.sending || !input.value.trim();
  button.textContent = state.sending ? "发送中…" : "发送";
  const retries = state.snapshot?.retryPolicy?.remainingRetries;
  $("#chatHint").textContent = TERMINAL.has(state.snapshot?.status)
    ? `任务已结束，仍可咨询或请求从某一步重跑${retries == null ? "" : `（剩余 ${retries} 次）`}`
    : "控制操作需要再次回复“确认执行”";
}

async function sendChatMessage(messageId, originalText) {
  if (state.sending) return;
  const input = $("#chatInput");
  const value = text(originalText ?? input.value).trim();
  if (!value) return;
  if (value.length > 4000) { toast("消息不能超过 4000 个字符"); return; }
  const id = messageId || crypto.randomUUID();
  upsertMessage({id, role: "user", time: new Date().toISOString(), text: value,
    status: "accepted", streaming: false, error: null});
  renderMessages();
  state.sending = true;
  if (!originalText) input.value = "";
  renderComposer();
  try {
    await api(`/api/workflows/${encodeURIComponent(state.workflowId)}/messages`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({messageId: id, text: value})
    });
    const message = findMessage(id);
    if (message) message.status = "accepted";
    if (!state.timer) state.timer = setInterval(refresh, 2000);
    if (!state.durationTimer) state.durationTimer = setInterval(renderDuration, 1000);
    await refresh();
  } catch (error) {
    const message = findMessage(id);
    if (message) { message.status = "failed"; message.error = error.message; }
    if (error.status === 409) await refresh();
    toast(error.message);
  } finally {
    state.sending = false;
    renderComposer();
    renderMessages();
  }
}

function start() {
  stop();
  refresh();
  state.timer = setInterval(refresh, 2000);
  state.durationTimer = setInterval(renderDuration, 1000);
}
function stop() {
  if (state.timer) clearInterval(state.timer);
  if (state.durationTimer) clearInterval(state.durationTimer);
  state.timer = null;
  state.durationTimer = null;
}

$("#lookupForm").onsubmit = event => {
  event.preventDefault();
  const id = $("#workflowInput").value.trim();
  if (id) location.href = `/?workflowId=${encodeURIComponent(id)}`;
};

$("#chatForm").onsubmit = event => {
  event.preventDefault();
  sendChatMessage();
};
$("#chatInput").addEventListener("input", renderComposer);
$("#chatInput").addEventListener("keydown", event => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    sendChatMessage();
  }
});

if (state.workflowId) {
  $("#lookup").hidden = true;
  $("#monitor").hidden = false;
  $("#id").textContent = state.workflowId;
  start();
} else {
  $("#lookup").hidden = false;
  $("#monitor").hidden = true;
}
gateway();
setInterval(gateway, 15000);
