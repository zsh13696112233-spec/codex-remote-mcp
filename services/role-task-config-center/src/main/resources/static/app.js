const state={page:"roles",roles:[],sops:[],tasks:[],agents:[],feishu:null,dingtalk:null,gatewayOnline:false,sop:{draft:null,baseline:"",selectedNodeId:null,tab:"workflow",drag:null}};
const $=s=>document.querySelector(s);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const uid=()=>`node-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;
const DEFAULT_EXPECTED_OUTPUT="完成本步骤，并返回清晰、完整且可验证的结果。";
const MODELS=["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna"];

async function api(path,options={}){
  const r=await fetch(path,{headers:{"Content-Type":"application/json"},...options});
  const text=await r.text();let body={};
  try{body=text?JSON.parse(text):{}}catch{body={error:text}}
  if(!r.ok)throw new Error(body.error||`请求失败（${r.status}）`);
  return body;
}
function toast(s){const e=$("#toast");e.textContent=s;e.style.display="block";setTimeout(()=>e.style.display="none",3200)}
function time(v){return v?new Date(v).toLocaleString("zh-CN",{hour12:false}):"—"}
function status(x){return `<span class="badge ${x.enabled?"":"off"}">${x.enabled?"启用中":"已停用"}</span>`}

async function loadBase(){
  try{
    const a=await api("/api/agents");state.agents=a.agents||[];state.gatewayOnline=true;
    $("#gateway").className="gateway online";$("#gateway").textContent="● Python 网关正常";
  }catch{
    state.agents=[];state.gatewayOnline=false;
    $("#gateway").className="gateway offline";$("#gateway").textContent="● Python 网关不可用";
  }
  [state.roles,state.sops,state.tasks]=await Promise.all([api("/api/roles"),api("/api/sops"),api("/api/task-definitions")]);
}
function roleCard(x){return `<article class="card"><div><h3>${esc(x.name)} ${status(x)}</h3><p>${esc(x.duty)}</p><span class="meta">版本 ${x.version} · 更新于 ${time(x.updatedAt)}</span></div><div class="actions"><button data-action="edit-role" data-id="${x.id}">编辑</button><button data-action="delete-role" data-id="${x.id}">删除</button></div></article>`}
function taskCard(x){return `<article class="card"><div><h3>${esc(x.name)} ${status(x)}</h3><p>${esc(x.objective)}</p><span class="meta">SOP：${esc(x.sopName)} · 更新于 ${time(x.updatedAt)}</span></div><div class="actions"><button class="primary" data-action="run-task" data-id="${x.id}">运行</button><button data-action="runs" data-id="${x.id}" data-name="${esc(x.name)}">记录</button><button data-action="edit-task" data-id="${x.id}">编辑</button><button data-action="copy-task" data-id="${x.id}">复制</button><button data-action="delete-task" data-id="${x.id}">删除</button></div></article>`}

function feishuStatus(value){
  const map={connected:["已连接","online"],failed:["连接失败","offline"],disconnected:["未连接","offline"],disabled:["未启用","off"]};
  const item=map[value]||["未知","off"];
  return `<span class="connection-status ${item[1]}"><i></i>${item[0]}</span>`;
}
function renderFeishuConfig(){
  const x=state.feishu||{enabled:false,appId:"",secretConfigured:false,taskDefinitionId:"",eventPollIntervalMs:1000,connectionStatus:"disabled"};
  const tasks=state.tasks.filter(t=>t.enabled&&!t.deleted);
  $("#content").className="content";
  $("#content").innerHTML=`<section class="settings-panel">
    <div class="settings-heading"><div><h2>飞书机器人配置</h2><p>通过官方 SDK 长连接接收群聊消息，无需开放公网回调地址。</p></div>${feishuStatus(x.connectionStatus)}</div>
    <form id="feishuForm">
      <label class="check"><input name="enabled" type="checkbox" ${x.enabled?"checked":""}> 启用飞书机器人长连接</label>
      <div class="grid"><label>App ID<input name="appId" maxlength="128" required value="${esc(x.appId)}" placeholder="cli_xxxxxxxxxxxxxxxx"></label>
      <label>App Secret<input name="appSecret" type="password" maxlength="512" placeholder="${x.secretConfigured?"已保存；留空表示不修改":"请输入 App Secret"}"></label></div>
      <label>固定任务定义<select name="taskDefinitionId" required><option value="">请选择任务</option>${tasks.map(t=>`<option value="${t.id}" ${t.id===x.taskDefinitionId?"selected":""}>${esc(t.name)}</option>`).join("")}</select></label>
      <label>事件轮询间隔（毫秒）<input name="eventPollIntervalMs" type="number" min="250" max="60000" required value="${Number(x.eventPollIntervalMs)||1000}"></label>
      <p class="hint">App Secret 会保存在配置中心 MySQL 中，但不会通过接口或页面回显。任务运行期间不能停用机器人或切换应用、密钥和固定任务。</p>
      <div id="feishuTestResult" class="test-result"></div>
      <footer><button type="button" data-feishu-test>测试连接</button><button class="primary" type="submit">保存配置</button></footer>
    </form>
  </section>`;
}
function feishuPayload(){
  const f=$("#feishuForm");
  return {enabled:f.enabled.checked,appId:f.appId.value.trim(),appSecret:f.appSecret.value.trim(),taskDefinitionId:f.taskDefinitionId.value,eventPollIntervalMs:Number(f.eventPollIntervalMs.value)};
}

function renderDingTalkConfig(){
  const x=state.dingtalk||{enabled:false,clientId:"",secretConfigured:false,taskDefinitionId:"",cardTemplateId:"",eventPollIntervalMs:1000,connectionStatus:"disabled"};
  const tasks=state.tasks.filter(t=>t.enabled&&!t.deleted);
  $("#content").className="content";
  $("#content").innerHTML=`<section class="settings-panel">
    <div class="settings-heading"><div><h2>钉钉机器人配置</h2><p>通过官方 Stream SDK 接收群聊和卡片回调，无需开放公网地址。</p></div>${feishuStatus(x.connectionStatus)}</div>
    <form id="dingtalkForm">
      <label class="check"><input name="enabled" type="checkbox" ${x.enabled?"checked":""}> 启用钉钉机器人 Stream 长连接</label>
      <div class="grid"><label>Client ID<input name="clientId" maxlength="128" required value="${esc(x.clientId)}" placeholder="dingxxxxxxxxxxxxxxxx"></label>
      <label>Client Secret<input name="clientSecret" type="password" maxlength="512" placeholder="${x.secretConfigured?"已保存；留空表示不修改":"请输入 Client Secret"}"></label></div>
      <label>固定任务定义<select name="taskDefinitionId" required><option value="">请选择任务</option>${tasks.map(t=>`<option value="${t.id}" ${t.id===x.taskDefinitionId?"selected":""}>${esc(t.name)}</option>`).join("")}</select></label>
      <label>互动进度卡模板 ID（可选）<input name="cardTemplateId" maxlength="256" value="${esc(x.cardTemplateId)}" placeholder="留空时使用内置 Markdown 进度；填写已发布的 .schema 模板 ID"></label>
      <label>事件轮询间隔（毫秒）<input name="eventPollIntervalMs" type="number" min="250" max="60000" required value="${Number(x.eventPollIntervalMs)||1000}"></label>
      <p class="hint">Client Secret 只保存在服务端且不会回显。模板 ID 留空时使用钉钉内置 Markdown 进度消息，可回复“暂停”“继续”或“立即进入下一步”；填写模板后使用可更新、带按钮的互动进度卡。飞书和钉钉只能启用一个，任务运行期间不能切换模式或修改关键配置。后续咨询需回复或引用启动消息、进度消息或进度卡。</p>
      <div id="dingtalkTestResult" class="test-result"></div>
      <footer><button type="button" data-dingtalk-test>测试连接</button><button class="primary" type="submit">保存配置</button></footer>
    </form>
  </section>`;
}
function dingtalkPayload(){
  const f=$("#dingtalkForm");
  return {enabled:f.enabled.checked,clientId:f.clientId.value.trim(),clientSecret:f.clientSecret.value.trim(),taskDefinitionId:f.taskDefinitionId.value,cardTemplateId:f.cardTemplateId.value.trim(),eventPollIntervalMs:Number(f.eventPollIntervalMs.value)};
}

async function render({reload=true}={}){
  if(reload)await loadBase();
  if(state.page==="feishu"){
    $("#search").closest(".toolbar").classList.add("hidden");
    state.feishu=await api("/api/feishu/config");
    renderFeishuConfig();return;
  }
  if(state.page==="dingtalk"){
    $("#search").closest(".toolbar").classList.add("hidden");
    state.dingtalk=await api("/api/dingtalk/config");
    renderDingTalkConfig();return;
  }
  if(state.page==="sops"){
    $("#search").closest(".toolbar").classList.add("hidden");
    if(!state.sop.draft&&state.sops.length)setDraft(state.sops[0]);
    renderSopWorkspace();return;
  }
  $("#search").closest(".toolbar").classList.remove("hidden");
  const q=$("#search").value.trim().toLowerCase();
  const data=state[state.page].filter(x=>(x.name||"").toLowerCase().includes(q));
  $("#content").className="content";
  $("#content").innerHTML=data.length?data.map(state.page==="roles"?roleCard:taskCard).join(""):`<div class="empty">暂无数据，点击右上角开始创建。</div>`;
}
function openRole(x={enabled:true}){const f=$("#roleForm");f.reset();f.id.value=x.id||"";f.version.value=x.version??0;f.name.value=x.name||"";f.duty.value=x.duty||"";f.enabled.checked=x.enabled!==false;$("#roleDialog").showModal()}
function openTask(x={enabled:true}){const f=$("#taskForm");f.reset();f.id.value=x.id||"";f.name.value=x.name||"";f.objective.value=x.objective||"";f.additionalNotes.value=x.additionalNotes||"";f.sopId.innerHTML=state.sops.filter(s=>s.enabled||s.id===x.sopId).map(s=>`<option value="${s.id}" ${s.id===x.sopId?"selected":""}>${esc(s.name)}</option>`).join("");f.enabled.checked=x.enabled!==false;$("#taskDialog").showModal()}

function blankSop(){return{id:"",name:"",description:"",supervisorAgentId:"local",supervisorTimeoutSec:7200,maxRetryCount:10,advanceMode:"automatic",defaultStepModel:"gpt-5.6-sol",enabled:true,steps:[]}}
function normalizeStep(s){return{...s,_clientId:s._clientId||uid(),displayName:s.displayName||"",instruction:s.instruction||"",expectedOutput:s.expectedOutput||DEFAULT_EXPECTED_OUTPUT,executorType:s.executorType||"local",agentId:s.agentId||"",workingDirectory:s.workingDirectory||"",writeEnabled:s.writeEnabled===true,modelOverride:s.modelOverride||null,timeoutSec:s.timeoutSec||1800,skills:[...(s.skills||[])],mcps:[...(s.mcps||[])]}}
function setDraft(sop){
  const copy={...blankSop(),...sop,steps:(sop.steps||[]).map(normalizeStep)};
  state.sop.draft=copy;state.sop.selectedNodeId=copy.steps[0]?._clientId||null;state.sop.tab="workflow";state.sop.baseline=draftFingerprint(copy);
}
function sopPayload(d=state.sop.draft){return{name:d.name.trim(),description:(d.description||"").trim(),supervisorAgentId:"local",supervisorTimeoutSec:Number(d.supervisorTimeoutSec),maxRetryCount:Number(d.maxRetryCount),advanceMode:d.advanceMode||"automatic",defaultStepModel:d.defaultStepModel,enabled:d.enabled!==false,steps:d.steps.map(s=>({id:s.id||undefined,displayName:(s.displayName||"").trim(),roleId:s.roleId,instruction:(s.instruction||"").trim(),expectedOutput:(s.expectedOutput||DEFAULT_EXPECTED_OUTPUT).trim(),executorType:s.executorType||"local",agentId:s.agentId||"",workingDirectory:(s.workingDirectory||"").trim(),writeEnabled:s.writeEnabled===true,modelOverride:s.modelOverride||null,timeoutSec:Number(s.timeoutSec),skills:[...(s.skills||[])],mcps:[...(s.mcps||[])]}))}}
function draftFingerprint(d=state.sop.draft){return d?JSON.stringify(sopPayload(d)):""}
function isSopDirty(){return !!state.sop.draft&&draftFingerprint()!==state.sop.baseline}
function confirmDiscard(){return !isSopDirty()||confirm("当前工作流有未保存的修改，确定放弃吗？")}
function discardSopChanges(){
  const saved=state.sops.find(s=>s.id===state.sop.draft?.id);
  if(saved)setDraft(saved);else{state.sop.draft=null;state.sop.baseline="";state.sop.selectedNodeId=null;state.sop.tab="workflow"}
}
function selectedStep(){return state.sop.draft?.steps.find(s=>s._clientId===state.sop.selectedNodeId)||null}
function roleById(id){return state.roles.find(r=>r.id===id)}
function syncDirtyUi(){
  const dirty=isSopDirty(),label=document.querySelector(".canvas-toolbar small"),reset=document.querySelector("[data-sop-reset]");
  if(label)label.textContent=dirty?"有未保存修改":"所有修改已保存";
  if(reset)reset.disabled=!dirty;
}

function renderSopWorkspace(){
  $("#content").className="content sop-content";
  $("#content").innerHTML=`<div class="sop-workspace">
    <aside class="sop-list-panel">
      <div class="panel-title"><div><strong>工作流列表</strong><small>${state.sops.length} 条工作流</small></div><button class="icon-primary" data-sop-new title="新建 SOP">＋</button></div>
      <input id="sopSearch" class="sop-search" type="search" placeholder="搜索工作流">
      <div class="sop-list">${sopListHtml()}</div>
    </aside>
    <section class="sop-canvas-panel">
      <div class="canvas-toolbar"><div><strong>${esc(state.sop.draft?.name||"未命名工作流")}</strong><small>${isSopDirty()?"有未保存修改":"所有修改已保存"}</small></div><div class="canvas-actions"><button data-sop-reset ${!isSopDirty()?"disabled":""}>撤销修改</button><button class="primary" data-sop-save>保存工作流</button></div></div>
      <div class="role-palette"><div class="palette-label"><strong>角色库</strong><small>拖入下方画布添加步骤</small></div><div class="role-palette-list">${rolePaletteHtml()}</div></div>
      <div class="flow-canvas" data-flow-canvas>${flowHtml()}</div>
    </section>
    <aside class="sop-inspector-panel">${inspectorHtml()}</aside>
  </div>`;
}
function sopListHtml(){
  if(!state.sops.length)return `<div class="sop-list-empty"><b>还没有工作流</b><span>点击上方“＋”开始创建</span></div>`;
  return state.sops.map(s=>`<article class="sop-list-item ${state.sop.draft?.id===s.id?"active":""}" data-sop-select="${s.id}" data-name="${esc((s.name||"").toLowerCase())}"><div><strong>${esc(s.name)}</strong>${status(s)}</div><small>${s.steps.length} 个步骤 · ${time(s.updatedAt)}</small><button data-sop-delete="${s.id}" title="删除工作流">×</button></article>`).join("");
}
function rolePaletteHtml(){
  const enabled=state.roles.filter(r=>r.enabled);
  if(!enabled.length)return `<span class="palette-empty">没有可用角色，请先在角色管理中启用角色</span>`;
  return enabled.map(r=>`<div class="role-chip" draggable="true" data-drag-role="${r.id}" title="${esc(r.duty)}"><i>${esc(r.name.slice(0,1))}</i><span>${esc(r.name)}</span><b>＋</b></div>`).join("");
}
function dropZone(index){return `<div class="flow-drop-zone" data-drop-index="${index}"><span>放到这里</span></div>`}
function flowHtml(){
  const d=state.sop.draft;
  if(!d)return `<div class="canvas-empty"><b>选择或新建一个工作流</b><span>工作流节点会显示在这里</span></div>`;
  if(!d.steps.length)return `<div class="canvas-empty canvas-drop-empty" data-drop-index="0"><div class="drop-icon">↳</div><b>拖动角色到这里</b><span>角色将按照从上到下的顺序严格串行执行</span></div>`;
  return `<div class="flow-list">${d.steps.map((s,i)=>`${dropZone(i)}${nodeHtml(s,i)}`).join("")}${dropZone(d.steps.length)}</div>`;
}
function nodeHtml(s,index){
  const role=roleById(s.roleId)||{name:s.roleName||"未知角色",duty:s.roleDuty||"角色已不存在",enabled:false};
  return `<article class="flow-node ${state.sop.selectedNodeId===s._clientId?"selected":""}" draggable="true" data-node-id="${s._clientId}">
    <div class="node-order"><span>${index+1}</span><i></i></div>
    <div class="node-body"><div class="node-top"><div class="node-role"><i>${esc(role.name.slice(0,1))}</i><div><strong>${esc(s.displayName||role.name)}</strong><small>${esc(role.name)}${role.enabled===false?" · 已停用":""}</small></div></div><div class="node-buttons"><button data-node-shift="-1" data-node-shift-id="${s._clientId}" title="上移节点" ${index===0?"disabled":""}>↑</button><button data-node-shift="1" data-node-shift-id="${s._clientId}" title="下移节点" ${index===state.sop.draft.steps.length-1?"disabled":""}>↓</button><button data-node-remove="${s._clientId}" title="移除节点">×</button><span class="node-drag" title="按住拖动排序">⠿</span></div></div>
    <p>${esc(s.instruction||"尚未填写执行说明")}</p><div class="node-meta"><span>${esc(s.modelOverride||`继承 ${state.sop.draft.defaultStepModel}`)}</span><span>${esc(s.agentId||"未选择执行机")}</span><span>${s.timeoutSec||1800} 秒</span></div></div>
  </article>`;
}
function inspectorHtml(){
  const hasNode=!!selectedStep();
  return `<div class="inspector-tabs"><button class="${state.sop.tab==="workflow"?"active":""}" data-inspector-tab="workflow">流程设置</button><button class="${state.sop.tab==="node"?"active":""}" data-inspector-tab="node" ${hasNode?"":"disabled"}>节点设置</button></div><div class="inspector-body">${state.sop.tab==="node"&&hasNode?nodeInspectorHtml(selectedStep()):workflowInspectorHtml()}</div>`;
}
function workflowInspectorHtml(){
  const d=state.sop.draft;if(!d)return `<div class="inspector-empty">请先选择工作流</div>`;
  return `<div class="inspector-heading"><strong>工作流配置</strong><small>设置工作流的基础运行参数</small></div>
    <label>工作流名称 *<input data-sop-field="name" maxlength="100" value="${esc(d.name)}" placeholder="例如：需求开发与质量验收"></label>
    <label>工作流说明<textarea data-sop-field="description" maxlength="2000" placeholder="说明这个流程的适用场景">${esc(d.description||"")}</textarea></label>
    <label>步骤默认模型<select data-sop-field="defaultStepModel">${MODELS.map(m=>`<option ${d.defaultStepModel===m?"selected":""}>${m}</option>`).join("")}</select></label>
    <label>步骤流转方式<select data-sop-field="advanceMode"><option value="automatic" ${d.advanceMode==="automatic"?"selected":""}>全自动（完成后立即继续）</option><option value="semi_automatic" ${d.advanceMode==="semi_automatic"?"selected":""}>半自动（等待确认，30 秒后自动继续）</option></select></label>
    <label>主监督最长时间（秒）<input data-sop-field="supervisorTimeoutSec" type="number" min="10" max="7200" value="${d.supervisorTimeoutSec}"></label>
    <label>单次任务最多重跑次数<input data-sop-field="maxRetryCount" type="number" min="0" max="100" value="${d.maxRetryCount}"></label>
    <label class="check"><input data-sop-field="enabled" type="checkbox" ${d.enabled?"checked":""}> 启用该工作流</label>
    <p class="inspector-hint">主执行机固定使用本地，失败策略固定为步骤失败后停止。</p>`;
}
function agentOptions(selected){
  const known=state.agents.some(a=>a.agentId===selected);
  const keep=selected&&!known?`<option value="${esc(selected)}" selected>${esc(selected)} · 当前配置${state.gatewayOnline?"（未在线）":"（网关离线）"}</option>`:"";
  const empty=!selected&&!state.agents.length?`<option value="" selected>暂无可用执行机</option>`:"";
  return empty+keep+state.agents.map(a=>`<option value="${esc(a.agentId)}" ${a.agentId===selected?"selected":""}>${esc(a.agentId)}${a.defaultModel?` · ${esc(a.defaultModel)}`:""}</option>`).join("");
}
function nodeInspectorHtml(s){
  const role=roleById(s.roleId)||{name:s.roleName||"未知角色",duty:s.roleDuty||"",enabled:false};
  return `<div class="inspector-heading"><strong>节点配置</strong><small>步骤由上到下严格串行执行</small></div>
    <div class="selected-role"><i>${esc(role.name.slice(0,1))}</i><div><strong>${esc(role.name)}</strong><small>${esc(role.duty||"暂无职责说明")}</small></div>${role.enabled===false?'<b>已停用</b>':""}</div>
    <label>显示名称 *<input data-node-field="displayName" value="${esc(s.displayName)}"></label>
    <label>本步骤执行说明 *<textarea data-node-field="instruction" placeholder="描述该节点需要完成的工作">${esc(s.instruction)}</textarea></label>
    <div class="inspector-grid"><label>执行位置<select data-node-field="executorType"><option value="local">本机</option><option value="remote" ${s.executorType==="remote"?"selected":""}>远程</option></select></label><label>执行机 *<select data-node-field="agentId">${agentOptions(s.agentId)}</select></label></div>
    <label>模型<select data-node-field="modelOverride"><option value="">继承工作流默认模型</option>${MODELS.map(m=>`<option value="${m}" ${s.modelOverride===m?"selected":""}>${m}</option>`).join("")}</select></label>
    <div class="inspector-grid"><label>超时（秒）<input data-node-field="timeoutSec" type="number" min="10" max="7200" value="${s.timeoutSec}"></label><label>工作目录<input data-node-field="workingDirectory" value="${esc(s.workingDirectory)}" placeholder="可选"></label></div>
    <label>Skill 标签<input data-node-field="skills" value="${esc(s.skills.join(", "))}" placeholder="多个标签用逗号分隔"></label>
    <label>MCP 标签<input data-node-field="mcps" value="${esc(s.mcps.join(", "))}" placeholder="多个标签用逗号分隔"></label>
    <label class="check"><input data-node-field="writeEnabled" type="checkbox" ${s.writeEnabled?"checked":""}> 允许写入工作目录</label>
    <p class="inspector-hint">Skill 与 MCP 当前仅作为配置标签，不改变真实执行权限。</p>`;
}

function addRoleNode(roleId,index){
  const role=roleById(roleId);if(!role||!role.enabled)return;
  const node=normalizeStep({roleId:role.id,roleName:role.name,roleDuty:role.duty,displayName:role.name,instruction:role.duty,agentId:state.agents[0]?.agentId||""});
  state.sop.draft.steps.splice(index,0,node);state.sop.selectedNodeId=node._clientId;state.sop.tab="node";renderSopWorkspace();
}
function moveNode(nodeId,index){
  const steps=state.sop.draft.steps,from=steps.findIndex(s=>s._clientId===nodeId);if(from<0)return;
  const [node]=steps.splice(from,1);if(from<index)index--;steps.splice(Math.max(0,Math.min(index,steps.length)),0,node);state.sop.selectedNodeId=nodeId;renderSopWorkspace();
}
function shiftNode(nodeId,delta){
  const steps=state.sop.draft.steps,from=steps.findIndex(s=>s._clientId===nodeId),to=from+delta;
  if(from<0||to<0||to>=steps.length)return;
  [steps[from],steps[to]]=[steps[to],steps[from]];state.sop.selectedNodeId=nodeId;state.sop.tab="node";renderSopWorkspace();
}
function updateField(target,obj,field){
  if(target.type==="checkbox")obj[field]=target.checked;
  else if(target.type==="number")obj[field]=Number(target.value);
  else if(field==="skills"||field==="mcps")obj[field]=target.value.split(",").map(v=>v.trim()).filter(Boolean);
  else if(field==="modelOverride")obj[field]=target.value||null;
  else obj[field]=target.value;
}
function validateSop(){
  const d=state.sop.draft;if(!d)return"请先选择或新建工作流。";if(!d.name.trim())return"请输入工作流名称。";
  if(!d.steps.length)return"请至少拖入一个角色节点。";
  if(d.supervisorTimeoutSec<10||d.supervisorTimeoutSec>7200)return"主监督最长时间必须在 10 到 7200 秒之间。";
  if(d.maxRetryCount<0||d.maxRetryCount>100)return"单次任务最多重跑次数必须在 0 到 100 之间。";
  for(let i=0;i<d.steps.length;i++){
    const s=d.steps[i];if(!s.displayName.trim())return`请填写第 ${i+1} 个节点的显示名称。`;
    if(!s.instruction.trim())return`请填写第 ${i+1} 个节点的执行说明。`;
    if(!s.agentId)return state.gatewayOnline?`请选择第 ${i+1} 个节点的执行机。`:`Python 网关不可用，无法为第 ${i+1} 个新节点选择执行机。`;
    if(s.timeoutSec<10||s.timeoutSec>7200)return`第 ${i+1} 个节点的超时必须在 10 到 7200 秒之间。`;
  }
  return"";
}
async function saveSop(){
  const message=validateSop();if(message){toast(message);return}
  const d=state.sop.draft,saved=await api(d.id?`/api/sops/${d.id}`:"/api/sops",{method:d.id?"PUT":"POST",body:JSON.stringify(sopPayload())});
  await loadBase();setDraft(saved);renderSopWorkspace();toast("SOP 工作流已保存");
}
async function selectSop(id){
  if(state.sop.draft?.id===id)return;if(!confirmDiscard())return;
  setDraft(await api(`/api/sops/${id}`));renderSopWorkspace();
}
function startNewSop(){if(!confirmDiscard())return;setDraft(blankSop());renderSopWorkspace();setTimeout(()=>document.querySelector('[data-sop-field="name"]')?.focus(),0)}

$("#roleForm").addEventListener("submit",async e=>{e.preventDefault();const f=e.currentTarget,b={name:f.name.value,duty:f.duty.value,enabled:f.enabled.checked};try{if(f.id.value){b.version=Number(f.version.value);await api(`/api/roles/${f.id.value}`,{method:"PUT",body:JSON.stringify(b)})}else await api("/api/roles",{method:"POST",body:JSON.stringify(b)});$("#roleDialog").close();toast("角色已保存");render()}catch(x){toast(x.message)}});
$("#taskForm").addEventListener("submit",async e=>{e.preventDefault();const f=e.currentTarget,b={name:f.name.value,objective:f.objective.value,sopId:f.sopId.value,additionalNotes:f.additionalNotes.value,enabled:f.enabled.checked};try{await api(f.id.value?`/api/task-definitions/${f.id.value}`:"/api/task-definitions",{method:f.id.value?"PUT":"POST",body:JSON.stringify(b)});$("#taskDialog").close();toast("任务定义已保存");render()}catch(x){toast(x.message)}});

$("#content").addEventListener("click",async e=>{
  let botTestButton=null;
  try{
    const test=e.target.closest("[data-feishu-test]");
    if(test){botTestButton=test;test.disabled=true;const result=await api("/api/feishu/config/test",{method:"POST",body:JSON.stringify(feishuPayload())});const output=$("#feishuTestResult");output.className=`test-result ${result.success?"success":"error"}`;output.textContent=result.message;test.disabled=false;return}
    const dingtalkTest=e.target.closest("[data-dingtalk-test]");
    if(dingtalkTest){botTestButton=dingtalkTest;dingtalkTest.disabled=true;const result=await api("/api/dingtalk/config/test",{method:"POST",body:JSON.stringify(dingtalkPayload())});const output=$("#dingtalkTestResult");output.className=`test-result ${result.success?"success":"error"}`;output.textContent=result.message;dingtalkTest.disabled=false;return}
    const action=e.target.closest("button[data-action]");
    if(action){
      const id=action.dataset.id,a=action.dataset.action;
      if(a==="edit-role")openRole(state.roles.find(x=>x.id===id));
      if(a==="edit-task")openTask(await api(`/api/task-definitions/${id}`));
      if(a.startsWith("delete-")&&confirm("确定删除？")){const path=a==="delete-role"?`roles/${id}`:`task-definitions/${id}`;await api(`/api/${path}`,{method:"DELETE"});toast("已删除");await render()}
      if(a==="copy-task"){await api(`/api/task-definitions/${id}/copy`,{method:"POST"});toast("已创建副本");await render()}
      if(a==="run-task"){action.disabled=true;const r=await api(`/api/task-definitions/${id}/runs`,{method:"POST"});toast("任务已提交");window.open(r.monitorUrl,"_blank","noopener");action.disabled=false}
      if(a==="runs")showRuns(id,action.dataset.name);return;
    }
    const del=e.target.closest("[data-sop-delete]");
    if(del){e.stopPropagation();const id=del.dataset.sopDelete;if(!confirm("确定删除这个 SOP 工作流？"))return;await api(`/api/sops/${id}`,{method:"DELETE"});if(state.sop.draft?.id===id){state.sop.draft=null;state.sop.baseline=""}await loadBase();if(!state.sop.draft&&state.sops.length)setDraft(state.sops[0]);renderSopWorkspace();toast("SOP 已删除");return}
    const list=e.target.closest("[data-sop-select]");if(list){await selectSop(list.dataset.sopSelect);return}
    if(e.target.closest("[data-sop-new]")){startNewSop();return}
    if(e.target.closest("[data-sop-save]")){await saveSop();return}
    if(e.target.closest("[data-sop-reset]")){if(!confirm("确定撤销当前所有未保存修改？"))return;const id=state.sop.draft.id;if(id)setDraft(await api(`/api/sops/${id}`));else setDraft(blankSop());renderSopWorkspace();return}
    const tab=e.target.closest("[data-inspector-tab]");if(tab&&!tab.disabled){state.sop.tab=tab.dataset.inspectorTab;renderSopWorkspace();return}
    const shift=e.target.closest("[data-node-shift]");
    if(shift){shiftNode(shift.dataset.nodeShiftId,Number(shift.dataset.nodeShift));return}
    const remove=e.target.closest("[data-node-remove]");
    if(remove){const id=remove.dataset.nodeRemove;state.sop.draft.steps=state.sop.draft.steps.filter(s=>s._clientId!==id);if(state.sop.selectedNodeId===id){state.sop.selectedNodeId=state.sop.draft.steps[0]?._clientId||null;state.sop.tab=state.sop.selectedNodeId?"node":"workflow"}renderSopWorkspace();return}
    const node=e.target.closest("[data-node-id]");if(node){state.sop.selectedNodeId=node.dataset.nodeId;state.sop.tab="node";renderSopWorkspace()}
  }catch(x){if(botTestButton)botTestButton.disabled=false;toast(x.message)}
});
$("#content").addEventListener("submit",async e=>{
  if(!["feishuForm","dingtalkForm"].includes(e.target.id))return;e.preventDefault();
  const submit=e.target.querySelector('button[type="submit"]');submit.disabled=true;
  try{
    if(e.target.id==="feishuForm"){state.feishu=await api("/api/feishu/config",{method:"PUT",body:JSON.stringify(feishuPayload())});toast(state.feishu.message||"飞书配置已保存");renderFeishuConfig()}
    else{state.dingtalk=await api("/api/dingtalk/config",{method:"PUT",body:JSON.stringify(dingtalkPayload())});toast(state.dingtalk.message||"钉钉配置已保存");renderDingTalkConfig()}
  }catch(x){toast(x.message);submit.disabled=false}
});
$("#content").addEventListener("input",e=>{
  if(e.target.id==="sopSearch"){const q=e.target.value.trim().toLowerCase();document.querySelectorAll(".sop-list-item").forEach(item=>item.hidden=!item.dataset.name.includes(q));return}
  const sopField=e.target.dataset.sopField;
  if(sopField&&state.sop.draft){updateField(e.target,state.sop.draft,sopField);const title=document.querySelector(".canvas-toolbar strong");if(sopField==="name"&&title)title.textContent=e.target.value||"未命名工作流";syncDirtyUi();return}
  const nodeField=e.target.dataset.nodeField,step=selectedStep();
  if(nodeField&&step){updateField(e.target,step,nodeField);const card=document.querySelector(`[data-node-id="${step._clientId}"]`);if(card&&nodeField==="displayName")card.querySelector(".node-role strong").textContent=e.target.value||roleById(step.roleId)?.name||"未命名节点";if(card&&nodeField==="instruction")card.querySelector(".node-body>p").textContent=e.target.value||"尚未填写执行说明";syncDirtyUi()}
});
$("#content").addEventListener("change",e=>{
  const sf=e.target.dataset.sopField;if(sf&&state.sop.draft){updateField(e.target,state.sop.draft,sf);syncDirtyUi();return}
  const nf=e.target.dataset.nodeField,step=selectedStep();if(nf&&step){updateField(e.target,step,nf);syncDirtyUi()}
});
$("#content").addEventListener("dragstart",e=>{
  const role=e.target.closest("[data-drag-role]"),node=e.target.closest("[data-node-id]");
  if(role)state.sop.drag={type:"role",id:role.dataset.dragRole};else if(node){state.sop.drag={type:"node",id:node.dataset.nodeId};node.classList.add("dragging")}else return;
  e.dataTransfer.effectAllowed="move";e.dataTransfer.setData("text/plain",state.sop.drag.id);
});
function dropTargetAt(element,clientY){
  const zone=element?.closest?.("[data-drop-index]");
  if(zone)return{element:zone,index:Number(zone.dataset.dropIndex),side:"zone"};
  const node=element?.closest?.("[data-node-id]");if(!node)return null;
  const index=state.sop.draft.steps.findIndex(s=>s._clientId===node.dataset.nodeId);
  if(index<0)return null;
  const rect=node.getBoundingClientRect(),after=clientY>=rect.top+rect.height/2;
  return{element:node,index:index+(after?1:0),side:after?"after":"before"};
}
function dropTarget(e){return dropTargetAt(e.target,e.clientY)}
function showDropTarget(target){
  document.querySelectorAll(".drag-over,.drop-before,.drop-after").forEach(x=>x.classList.remove("drag-over","drop-before","drop-after"));
  if(target)target.element.classList.add(target.side==="zone"?"drag-over":target.side==="after"?"drop-after":"drop-before");
}
$("#content").addEventListener("dragover",e=>{
  const target=dropTarget(e);if(!target||!state.sop.drag)return;e.preventDefault();
  showDropTarget(target);
  e.dataTransfer.dropEffect="move";
});
$("#content").addEventListener("drop",e=>{
  const target=dropTarget(e);if(!target||!state.sop.drag)return;e.preventDefault();
  const drag=state.sop.drag;state.sop.drag=null;
  drag.type==="role"?addRoleNode(drag.id,target.index):moveNode(drag.id,target.index);
});
$("#content").addEventListener("dragend",()=>{state.sop.drag=null;document.querySelectorAll(".dragging,.drag-over,.drop-before,.drop-after").forEach(x=>x.classList.remove("dragging","drag-over","drop-before","drop-after"))});

let pointerDrag=null;
$("#content").addEventListener("pointerdown",e=>{
  if(e.button!==0)return;
  const handle=e.target.closest(".node-drag"),role=e.target.closest("[data-drag-role]");
  if(!handle&&!role)return;
  e.preventDefault();
  const node=handle?.closest("[data-node-id]");
  pointerDrag={type:role?"role":"node",id:role?role.dataset.dragRole:node.dataset.nodeId,pointerId:e.pointerId,source:handle||role};
  pointerDrag.source.setPointerCapture?.(e.pointerId);node?.classList.add("dragging");
});
$("#content").addEventListener("pointermove",e=>{
  if(!pointerDrag||pointerDrag.pointerId!==e.pointerId)return;
  const element=document.elementFromPoint(e.clientX,e.clientY);
  showDropTarget(dropTargetAt(element,e.clientY));
});
$("#content").addEventListener("pointerup",e=>{
  if(!pointerDrag||pointerDrag.pointerId!==e.pointerId)return;
  const drag=pointerDrag,target=dropTargetAt(document.elementFromPoint(e.clientX,e.clientY),e.clientY);pointerDrag=null;
  document.querySelectorAll(".dragging,.drag-over,.drop-before,.drop-after").forEach(x=>x.classList.remove("dragging","drag-over","drop-before","drop-after"));
  if(!target)return;
  drag.type==="role"?addRoleNode(drag.id,target.index):moveNode(drag.id,target.index);
});
$("#content").addEventListener("pointercancel",()=>{pointerDrag=null;document.querySelectorAll(".dragging,.drag-over,.drop-before,.drop-after").forEach(x=>x.classList.remove("dragging","drag-over","drop-before","drop-after"))});

async function showRuns(id,name){const list=await api(`/api/task-definitions/${id}/runs`);$("#runsTitle").textContent=`${name} · 运行记录`;$("#runList").innerHTML=list.length?list.map(r=>`<div class="run"><b>${esc(r.workflowId)}</b> <span class="badge">${esc(r.status)}</span><p class="meta">${time(r.submittedAt)}${r.sourceWorkflowId?` · 重试自 ${esc(r.sourceWorkflowId)}`:""}</p><div class="actions"><button data-monitor="${esc(r.monitorUrl)}">查看监控</button><button data-retry="${r.workflowId}">按原快照重试</button>${["queued","running","submitting"].includes(r.status)?`<button data-cancel="${r.workflowId}">取消</button>`:""}</div></div>`).join(""):`<div class="empty">暂无运行记录</div>`;$("#runsDialog").showModal()}
$("#runList").onclick=async e=>{const b=e.target.closest("button");if(!b)return;try{if(b.dataset.monitor)window.open(b.dataset.monitor,"_blank","noopener");if(b.dataset.retry){const r=await api(`/api/task-runs/${b.dataset.retry}/retry`,{method:"POST"});window.open(r.monitorUrl,"_blank","noopener");toast("已按原快照重试")}if(b.dataset.cancel){await api(`/api/task-runs/${b.dataset.cancel}/cancel`,{method:"POST"});toast("已请求取消")}}catch(x){toast(x.message)}};

document.querySelectorAll("nav button").forEach(b=>b.onclick=async()=>{
  if(b.dataset.page===state.page)return;
  if(state.page==="sops"){const dirty=isSopDirty();if(!confirmDiscard())return;if(dirty)discardSopChanges()}
  document.querySelector("nav .active").classList.remove("active");b.classList.add("active");state.page=b.dataset.page;
  const map={roles:["角色管理","定义协作角色及其职责边界。","＋ 新建角色"],sops:["SOP 工作流","拖动角色配置可复用的严格串行流程。","＋ 新建 SOP"],tasks:["任务定义","保存任务配置、运行并追溯不可变快照。","＋ 新建任务"],feishu:["飞书机器人","配置长连接、固定任务和运行状态。",""],dingtalk:["钉钉机器人","配置 Stream 长连接、互动卡和固定任务。",""]};
  [$("#title").textContent,$("#subtitle").textContent,$("#create").textContent]=map[state.page];
  $("#create").classList.toggle("hidden",["feishu","dingtalk"].includes(state.page));
  try{await render()}catch(e){toast(e.message)}
});
$("#create").onclick=()=>state.page==="roles"?openRole():state.page==="sops"?startNewSop():state.page==="tasks"?openTask():null;
$("#refresh").onclick=async()=>{if(state.page==="sops"&&!confirmDiscard())return;try{if(state.page==="sops"){const id=state.sop.draft?.id;await loadBase();if(id&&state.sops.some(s=>s.id===id))setDraft(await api(`/api/sops/${id}`));else if(state.sops.length)setDraft(state.sops[0]);else state.sop.draft=null;renderSopWorkspace()}else await render()}catch(e){toast(e.message)}};
$("#search").oninput=()=>render({reload:false});
window.addEventListener("beforeunload",e=>{if(isSopDirty()){e.preventDefault();e.returnValue=""}});
render().catch(e=>toast(e.message));
