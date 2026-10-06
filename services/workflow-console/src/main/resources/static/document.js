"use strict";
(() => {
  const query = new URLSearchParams(location.search);
  const workflowId = query.get("workflowId"), documentId = query.get("documentId");
  const title = document.getElementById("title"), metadata = document.getElementById("metadata");
  const status = document.getElementById("status"), content = document.getElementById("content");
  const refresh = document.getElementById("refresh");
  let busy = false, revision = null;
  if (workflowId) document.getElementById("back").href = `/?workflowId=${encodeURIComponent(workflowId)}`;
  const renderer = new marked.Renderer();
  renderer.html = () => "";
  renderer.image = () => "";
  marked.use({renderer});
  async function load() {
    if (busy) return;
    if (!workflowId || !documentId || workflowId.length > 128 || documentId.length > 128) {
      title.textContent = "无法打开文档"; status.textContent = "缺少有效的文档链接。"; refresh.disabled = true; return;
    }
    busy = true; refresh.disabled = true;
    try {
      const response = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}/documents/${encodeURIComponent(documentId)}`, {cache: "no-store", signal: AbortSignal.timeout(15000)});
      if (!response.ok) {
        if (response.status === 404) {content.replaceChildren(); revision = null;}
        throw new Error(response.status === 404 ? "找不到文档或所属任务。" : "暂时无法读取文档，请稍后刷新。");
      }
      const value = await response.json();
      title.textContent = value.name || "交付文档";
      metadata.textContent = `第 ${value.stepNumber} 步 · ${value.stepName || "交付文档"} · 更新时间：${new Date(value.updatedAt).toLocaleString("zh-CN")} · 版本 ${value.revision}`;
      status.textContent = value.removed ? "文档已移除。" : value.error ? `${value.error}${value.available ? " 以下为上次成功保存的正文。" : " 暂无可用正文。"}` : "已显示最新保存版本";
      status.className = value.error ? "warning" : "";
      if (revision !== value.revision) {
        content.replaceChildren();
        if (!value.removed && value.available) {
          if (value.format === "markdown") {
            content.innerHTML = DOMPurify.sanitize(marked.parse(value.content || ""), {
              ALLOWED_TAGS: ["p","br","hr","h1","h2","h3","h4","h5","h6","ul","ol","li","strong","em","del","blockquote","pre","code","table","thead","tbody","tr","th","td","a"],
              ALLOWED_ATTR: ["href","title"], ALLOW_DATA_ATTR: false
            });
            for (const link of content.querySelectorAll("a")) {
              const href = link.getAttribute("href") || "";
              if (!/^https?:\/\//i.test(href)) link.removeAttribute("href");
              else {link.target = "_blank"; link.rel = "noopener noreferrer";}
            }
          } else {
            const pre = document.createElement("pre"); pre.className = "plain"; pre.textContent = value.content || ""; content.append(pre);
          }
        }
        revision = value.revision;
      }
    } catch (error) {
      status.textContent = error.name === "TimeoutError" ? "读取超时，请稍后刷新。" : error.message || "读取失败，请稍后刷新。";
      if (revision !== null) status.textContent += " 当前保留上次读取的内容。";
      status.className = "warning";
    } finally {busy = false; refresh.disabled = false;}
  }
  refresh.addEventListener("click", load);
  document.addEventListener("visibilitychange", () => {if (!document.hidden) load();});
  setInterval(() => {if (!document.hidden) load();}, 10000);
  load();
})();
