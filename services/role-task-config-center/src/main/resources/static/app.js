const state={page:"roles",roles:[],sops:[],tasks:[],agents:[],feishu:null,dingtalk:null,dingtalkTargets:[],dingtalkTargetType:"GROUP",gatewayOnline:false,agentsAvailable:false,agentRefreshInFlight:false,lastRuntimeRefreshAt:null,sop:{draft:null,baseline:"",selectedNodeId:null,tab:"workflow",drag:null}};
const $=s=>document.querySelector(s);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const uid=()=>`node-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;
const DEFAULT_EXPECTED_OUTPUT="完成本步骤，并返回清晰、完整且可验证的结果。";
const MODELS=["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna"];
const PERMISSION_LABELS={read_only:"只读",workspace_write:"工作区写入",auto_review:"自动审核"};

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

function showGatewayStatus(online){
  state.gatewayOnline=online;$("#gateway").className=`gateway ${online?"online":"offline"}`;$("#gateway").textContent=online?"● Python 网关正常":"● Python 网关不可用";
}
async function loadGatewayReady(){
  try{const value=await api("/api/gateway/ready");showGatewayStatus(value.ready===true)}catch{showGatewayStatus(false)}
}
async function loadAgents({preserveOnFailure=false}={}){
  try{
    const a=await api("/api/agents");state.agents=a.agents||[];state.agentsAvailable=true;
  }catch{
    state.agentsAvailable=false;if(!preserveOnFailure)state.agents=[];
  }
}
async function loadRuntime({preserveOnFailure=false}={}){
  await Promise.all([loadAgents({preserveOnFailure}),loadGatewayReady()]);
  state.lastRuntimeRefreshAt=new Date().toISOString();
}
async function loadBase(){
  await loadRuntime();
  [state.roles,state.sops,state.tasks,state.dingtalkTargets]=await Promise.all([api("/api/roles"),api("/api/sops"),api("/api/task-definitions"),api("/api/dingtalk/targets")]);
}
function roleCard(x){return `<article class="card"><div><h3>${esc(x.name)} ${status(x)}</h3><p>${esc(x.duty)}</p><span class="meta">版本 ${x.version} · 更新于 ${time(x.updatedAt)}</span></div><div class="actions"><button data-action="edit-role" data-id="${x.id}">编辑</button><button data-action="delete-role" data-id="${x.id}">删除</button></div></article>`}
function taskCard(x){const binding=x.dingtalkTarget?` · 钉钉：${targetTypeLabel(x.dingtalkTarget.targetType)} ${esc(x.dingtalkTarget.displayName)}${x.dingtalkActiveWorkflowId?"（运行中）":""}`:"";return `<article class="card"><div><h3>${esc(x.name)} ${status(x)}</h3><p>${esc(x.objective)}</p><span class="meta">SOP：${esc(x.sopName)}${binding} · 更新于 ${time(x.updatedAt)}</span></div><div class="actions"><button class="primary" data-action="run-task" data-id="${x.id}">运行</button><button data-action="runs" data-id="${x.id}" data-name="${esc(x.name)}">记录</button><button data-action="edit-task" data-id="${x.id}">编辑</button><button data-action="copy-task" data-id="${x.id}">复制</button><button data-action="delete-task" data-id="${x.id}">删除</button></div></article>`}

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
  const x=state.dingtalk||{enabled:false,clientId:"",secretConfigured:false,cardTemplateId:"",eventPollIntervalMs:1000,connectionStatus:"disabled"};
  const bindings=state.tasks.filter(t=>t.dingtalkTarget);
  $("#content").className="content";
  $("#content").innerHTML=`<section class="settings-panel">
    <div class="settings-heading"><div><h2>钉钉机器人配置</h2><p>通过官方 Stream SDK 接收群聊和卡片回调，无需开放公网地址。</p></div>${feishuStatus(x.connectionStatus)}</div>
    <form id="dingtalkForm">
      <label class="check"><input name="enabled" type="checkbox" ${x.enabled?"checked":""}> 启用钉钉机器人 Stream 长连接</label>
      <div class="grid"><label>Client ID<input name="clientId" maxlength="128" required value="${esc(x.clientId)}" placeholder="dingxxxxxxxxxxxxxxxx"></label>
      <label>Client Secret<input name="clientSecret" type="password" maxlength="512" placeholder="${x.secretConfigured?"已保存；留空表示不修改":"请输入 Client Secret"}"></label></div>
      <label>互动进度卡模板 ID（可选）<input name="cardTemplateId" maxlength="256" value="${esc(x.cardTemplateId)}" placeholder="留空时使用内置 Markdown 进度；填写已发布的 .schema 模板 ID"></label>
      <label>事件轮询间隔（毫秒）<input name="eventPollIntervalMs" type="number" min="250" max="60000" required value="${Number(x.eventPollIntervalMs)||1000}"></label>
      <p class="hint">Client Secret 只保存在服务端且不会回显。任务与通知对象在“任务定义”中绑定；不同绑定可以同时运行。模板 ID 留空时使用钉钉内置 Markdown 进度消息，填写后使用互动进度卡。</p>
      <div class="target-list">${bindings.length?bindings.map(t=>`<article class="target-card"><div class="target-main"><strong>${esc(t.name)}</strong><p>${targetTypeLabel(t.dingtalkTarget.targetType)}：${esc(t.dingtalkTarget.displayName)}</p></div><span class="badge ${t.dingtalkActiveWorkflowId?"":"off"}">${t.dingtalkActiveWorkflowId?"运行中":"已绑定"}</span></article>`).join(""):'<div class="empty">尚未绑定任务。请在“任务定义”中选择一个钉钉群或人员。</div>'}</div>
      <div id="dingtalkTestResult" class="test-result"></div>
      <footer><button type="button" data-dingtalk-test>测试连接</button><button class="primary" type="submit">保存配置</button></footer>
    </form>
  </section>`;
}
function dingtalkPayload(){
  const f=$("#dingtalkForm");
  return {enabled:f.enabled.checked,clientId:f.clientId.value.trim(),clientSecret:f.clientSecret.value.trim(),cardTemplateId:f.cardTemplateId.value.trim(),eventPollIntervalMs:Number(f.eventPollIntervalMs.value)};
}

function targetTypeLabel(value){return value==="PERSON"?"人员":"群聊"}
function renderDingTalkTargets(){
  const configured=!!state.dingtalk?.clientId,items=state.dingtalkTargets.filter(x=>x.targetType===state.dingtalkTargetType);
  $("#content").className="content target-content";
  $("#content").innerHTML=`<section class="target-panel">
    <div class="settings-heading"><div><h2>钉钉通知对象</h2><p>一个群或人员只能绑定一个任务定义；这里只维护本机器人所属应用的对象。</p></div>${configured?'<span class="connection-status online"><i></i>应用已配置</span>':'<span class="connection-status off"><i></i>请先配置机器人</span>'}</div>
    <div class="target-toolbar"><div class="target-tabs"><button class="${state.dingtalkTargetType==="GROUP"?"active":""}" data-target-type="GROUP">群聊</button><button class="${state.dingtalkTargetType==="PERSON"?"active":""}" data-target-type="PERSON">人员</button></div>${state.dingtalkTargetType==="PERSON"?`<button data-target-sync ${configured?"":"disabled"}>同步公司人员</button>`:""}</div>
    <p class="hint">${state.dingtalkTargetType==="GROUP"?'在群里首次 @ 机器人后，该群会自动出现在这里；管理员确认名称并启用后才可供任务定义选择。':'点击“同步公司人员”从钉钉通讯录拉取。首次同步的人员默认停用，确认后手动启用。'}</p>
    <div class="target-list">${!configured?'<div class="empty">请先在“钉钉机器人”页面保存 Client ID 和 Client Secret。</div>':items.length?items.map(targetCard).join(""):`<div class="empty">暂无${targetTypeLabel(state.dingtalkTargetType)}，${state.dingtalkTargetType==="GROUP"?'请先在目标群中 @ 机器人。':'请点击上方按钮同步。'}</div>`}</div>
  </section>`;
}
function targetCard(x){
  const unavailable=!x.available;
  const owner=state.tasks.find(t=>t.dingtalkTargetId===x.id);
  return `<article class="target-card" data-target-id="${x.id}"><div class="target-main"><div class="target-name"><span class="target-kind">${targetTypeLabel(x.targetType)}</span><input data-target-name maxlength="160" value="${esc(x.displayName)}"></div><p>${x.departmentDisplay?`部门：${esc(x.departmentDisplay)}`:`钉钉标识：${esc(x.externalId)}`}</p><small>最近同步：${time(x.lastSyncedAt)}${unavailable?' · 当前已不在通讯录中':''}${owner?` · 已绑定任务：${esc(owner.name)}`:""}</small></div><label class="check"><input data-target-enabled type="checkbox" ${x.enabled?"checked":""} ${unavailable?"disabled":""}> 启用</label><div class="actions"><button data-target-test>测试</button><button class="primary" data-target-save>保存</button><button data-target-delete>删除</button></div></article>`;
}

function supervisorAgents(){
  return state.agents.filter(a=>Array.isArray(a.capabilities)?a.capabilities.includes("supervisor"):a.agentId==="local");
}
function runtimeAgentCard(agent){
  const view=supervisorRuntimeView(agent.agentId);
  return `<article class="runtime-agent-card ${view.state}">
    <div class="runtime-agent-heading"><div><span class="runtime-dot ${view.state}"></span><strong>${esc(agent.agentId)}</strong></div><span class="runtime-state ${view.state}">${esc(view.label)}</span></div>
    <p>${esc(view.detail)}</p>
    <dl><div><dt>默认模型</dt><dd>${esc(agent.defaultModel||"跟随执行机设置")}</dd></div><div><dt>并发容量</dt><dd>${Number(agent.supervisorCapacity)||1}</dd></div><div><dt>最近探测</dt><dd>${time(agent.checkedAt)}</dd></div><div><dt>最近在线</dt><dd>${time(agent.lastOnlineAt)}</dd></div></dl>
  </article>`;
}
function renderRuntimeStatus(){
  const agents=supervisorAgents(),counts={online:0,busy:0,offline:0,unknown:0};
  agents.forEach(agent=>{const value=supervisorRuntimeView(agent.agentId).state;counts[value==="online"?"online":value==="busy"?"busy":value==="offline"?"offline":"unknown"]++});
  const gatewayState=state.gatewayOnline?"online":"offline";
  const emptyMessage=!state.gatewayOnline?"Python 网关不可用，恢复连接后会自动显示主监督状态。":!state.agentsAvailable?"主监督列表暂时无法读取，将在 10 秒后自动重试。":"尚未登记具备主监督能力的执行机。";
  $("#content").className="content runtime-content";
  $("#content").innerHTML=`<section class="runtime-dashboard">
    <div class="runtime-overview">
      <div class="runtime-overview-head"><div><h2>运行总览</h2><p>页面可独立查看，不需要新建或打开 SOP。在线状态每 10 秒自动刷新，连续两次探测失败才显示离线。</p></div><button data-runtime-refresh ${state.agentRefreshInFlight?"disabled":""}>立即刷新</button></div>
      <div class="runtime-gateway ${gatewayState}"><span class="runtime-dot ${gatewayState}"></span><div><strong>Python 网关</strong><small>${state.gatewayOnline?"连接正常":"当前不可用"}</small></div><b>${state.gatewayOnline?"正常":"不可用"}</b></div>
      <div class="runtime-summary"><div><span class="runtime-dot online"></span><strong>${counts.online}</strong><small>在线空闲</small></div><div><span class="runtime-dot busy"></span><strong>${counts.busy}</strong><small>在线忙碌</small></div><div><span class="runtime-dot offline"></span><strong>${counts.offline}</strong><small>离线</small></div><div><span class="runtime-dot unknown"></span><strong>${counts.unknown}</strong><small>未知或停用</small></div></div>
      <p class="runtime-refreshed">最近刷新：${time(state.lastRuntimeRefreshAt)}</p>
    </div>
    <div class="runtime-supervisors"><div class="runtime-section-head"><div><h2>主监督执行机</h2><p>共 ${agents.length} 台已登记主监督；本页只展示状态，不提供运行控制。</p></div></div>${agents.length?`<div class="runtime-agent-grid">${agents.map(runtimeAgentCard).join("")}</div>`:`<div class="runtime-empty">${emptyMessage}</div>`}</div>
  </section>`;
}

async function render({reload=true}={}){
  if(reload){if(state.page==="runtime")await loadRuntime();else await loadBase()}
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
  if(state.page==="dingtalk-targets"){
    $("#search").closest(".toolbar").classList.add("hidden");
    [state.dingtalk,state.dingtalkTargets]=await Promise.all([api("/api/dingtalk/config"),api("/api/dingtalk/targets")]);
    renderDingTalkTargets();return;
  }
  if(state.page==="runtime"){
    $("#search").closest(".toolbar").classList.add("hidden");
    renderRuntimeStatus();return;
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
function openTask(x={enabled:true}){const f=$("#taskForm");f.reset();f.id.value=x.id||"";f.name.value=x.name||"";f.objective.value=x.objective||"";f.additionalNotes.value=x.additionalNotes||"";f.sopId.innerHTML=state.sops.filter(s=>s.enabled||s.id===x.sopId).map(s=>`<option value="${s.id}" ${s.id===x.sopId?"selected":""}>${esc(s.name)}</option>`).join("");const used=new Set(state.tasks.filter(t=>t.id!==x.id&&t.dingtalkTargetId).map(t=>t.dingtalkTargetId));const targets=state.dingtalkTargets.filter(t=>(t.enabled&&t.available&&!used.has(t.id))||t.id===x.dingtalkTargetId);f.dingtalkTargetId.innerHTML=`<option value="">不绑定钉钉</option>${targets.map(t=>`<option value="${t.id}" ${t.id===x.dingtalkTargetId?"selected":""}>${targetTypeLabel(t.targetType)} · ${esc(t.displayName)}${!t.enabled||!t.available?"（已不可用）":""}</option>`).join("")}`;f.dingtalkTargetId.value=x.dingtalkTargetId||"";f.enabled.checked=x.enabled!==false;$("#taskDialog").showModal()}

function blankSop(){return{id:"",name:"",description:"",supervisorAgentId:"local",supervisorTimeoutSec:7200,maxRetryCount:10,advanceMode:"automatic",handoffMode:"cumulative_files",defaultStepModel:"gpt-5.6-sol",enabled:true,steps:[]}}
function normalizeStep(s){const permissionProfile=s.permissionProfile||(s.writeEnabled===true?"workspace_write":"read_only");return{...s,_clientId:s._clientId||uid(),displayName:s.displayName||"",instruction:s.instruction||"",expectedOutput:s.expectedOutput||DEFAULT_EXPECTED_OUTPUT,executorType:s.executorType||"local",agentId:s.agentId||"",workingDirectory:s.workingDirectory||"",permissionProfile,writeEnabled:permissionProfile!=="read_only",modelOverride:s.modelOverride||null,timeoutSec:s.timeoutSec||1800,skills:[...(s.skills||[])],mcps:[...(s.mcps||[])]}}
function setDraft(sop){
  const copy={...blankSop(),...sop,steps:(sop.steps||[]).map(normalizeStep)};
  state.sop.draft=copy;state.sop.selectedNodeId=copy.steps[0]?._clientId||null;state.sop.tab="workflow";state.sop.baseline=draftFingerprint(copy);
}
function sopPayload(d=state.sop.draft){return{name:d.name.trim(),description:(d.description||"").trim(),supervisorAgentId:(d.supervisorAgentId||"").trim(),supervisorTimeoutSec:Number(d.supervisorTimeoutSec),maxRetryCount:Number(d.maxRetryCount),advanceMode:d.advanceMode||"automatic",handoffMode:d.handoffMode||"cumulative_files",defaultStepModel:d.defaultStepModel,enabled:d.enabled!==false,steps:d.steps.map(s=>({id:s.id||undefined,displayName:(s.displayName||"").trim(),roleId:s.roleId,instruction:(s.instruction||"").trim(),expectedOutput:(s.expectedOutput||DEFAULT_EXPECTED_OUTPUT).trim(),executorType:s.executorType||"local",agentId:(s.agentId||"").trim(),workingDirectory:(s.workingDirectory||"").trim(),permissionProfile:s.permissionProfile||"read_only",writeEnabled:(s.permissionProfile||"read_only")!=="read_only",modelOverride:s.modelOverride||null,timeoutSec:Number(s.timeoutSec),skills:[...(s.skills||[])],mcps:[...(s.mcps||[])]}))}}
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
    <label>步骤结果交接<select data-sop-field="handoffMode"><option value="cumulative_files" ${d.handoffMode==="cumulative_files"?"selected":""}>文件交接（累计传递前序文件）</option><option value="legacy_text" ${d.handoffMode==="legacy_text"?"selected":""}>文字交接（追加上一步文字结果）</option></select></label>
    <label>主监督执行机 *<div class="agent-picker"><input data-sop-field="supervisorAgentId" maxlength="128" value="${esc(d.supervisorAgentId||"")}" placeholder="例如：local" autocomplete="off"><button type="button" class="agent-picker-toggle" data-agent-menu-toggle aria-label="查看全部主监督执行机" aria-expanded="false">▼</button><div class="agent-picker-menu" hidden>${agentChoiceButtons("supervisor","supervisor")}</div></div>${supervisorSelectionStatusHtml(d.supervisorAgentId)}</label>
    <label>主监督最长时间（秒）<input data-sop-field="supervisorTimeoutSec" type="number" min="10" max="7200" value="${d.supervisorTimeoutSec}"></label>
    <label>单次任务最多重跑次数<input data-sop-field="maxRetryCount" type="number" min="0" max="100" value="${d.maxRetryCount}"></label>
    <label class="check"><input data-sop-field="enabled" type="checkbox" ${d.enabled?"checked":""}> 启用该工作流</label>
    <p class="inspector-hint">文字交接适合纯文本串行任务；文件交接不会传递步骤文字，前序步骤应发布文件。执行机列表只提供填写建议，网关离线或列表中没有该 ID 时仍可保存；真正运行时由网关校验。失败策略固定为步骤失败后停止。</p>`;
}
function suggestedAgents(capability){
  return state.agents.filter(a=>a.enabled!==false&&(!Array.isArray(a.capabilities)||a.capabilities.includes(capability)||(capability==="supervisor"&&a.agentId==="local")));
}
function supervisorRuntimeView(agentId){
  if(!state.gatewayOnline)return{state:"unknown",label:"状态未知",detail:"Python 网关不可用"};
  if(!state.agentsAvailable)return{state:"unknown",label:"状态未知",detail:"主监督状态暂时无法读取"};
  const agent=state.agents.find(a=>a.agentId===agentId);
  if(!agent)return{state:"unregistered",label:"未登记",detail:"该 ID 不在当前执行机列表中"};
  if(agent.enabled===false)return{state:"disabled",label:"已停用",detail:"该主监督已在网关配置中停用"};
  const checked=agent.checkedAt?`最近检查 ${new Date(agent.checkedAt).toLocaleTimeString("zh-CN",{hour12:false})}`:"正在等待首次检查";
  if(agent.connectionStatus==="online"&&agent.availability==="busy")return{state:"busy",label:"在线忙碌",detail:checked};
  if(agent.connectionStatus==="online")return{state:"online",label:"在线空闲",detail:checked};
  if(agent.connectionStatus==="offline")return{state:"offline",label:"离线",detail:checked};
  return{state:"unknown",label:"状态未知",detail:checked};
}
function supervisorSelectionStatusHtml(agentId){
  const view=supervisorRuntimeView(agentId||"");
  return `<small class="agent-selection-status ${view.state}" data-supervisor-selection-status><i></i><span>${esc(view.label)}</span><b>${esc(view.detail)}</b></small>`;
}
function agentChoiceButtons(capability,scope){
  const agents=suggestedAgents(capability);
  return agents.length?agents.map(a=>{
    if(capability!=="supervisor")return `<button type="button" data-agent-choice="${scope}" data-agent-id="${esc(a.agentId)}"><span>${esc(a.agentId)}</span>${a.defaultModel?`<small>${esc(a.defaultModel)}</small>`:""}</button>`;
    const view=supervisorRuntimeView(a.agentId);
    return `<button type="button" data-agent-choice="${scope}" data-agent-id="${esc(a.agentId)}" title="${esc(view.detail)}"><span class="agent-choice-main"><i class="agent-status-dot ${view.state}"></i><span>${esc(a.agentId)}</span></span><small class="agent-runtime-status ${view.state}">${esc(view.label)}</small></button>`;
  }).join(""):'<span class="agent-picker-empty">暂无可用建议</span>';
}
function updateAgentRuntimeUi(){
  document.querySelectorAll('[data-agent-choice="supervisor"]').forEach(button=>{
    const view=supervisorRuntimeView(button.dataset.agentId),dot=button.querySelector(".agent-status-dot"),label=button.querySelector(".agent-runtime-status");
    if(dot)dot.className=`agent-status-dot ${view.state}`;
    if(label){label.className=`agent-runtime-status ${view.state}`;label.textContent=view.label}
    button.title=view.detail;
  });
  const selected=document.querySelector("[data-supervisor-selection-status]"),input=document.querySelector('[data-sop-field="supervisorAgentId"]');
  if(selected&&input){const view=supervisorRuntimeView(input.value.trim());selected.className=`agent-selection-status ${view.state}`;selected.innerHTML=`<i></i><span>${esc(view.label)}</span><b>${esc(view.detail)}</b>`}
}
async function refreshAgentRuntimeStatuses(){
  if(document.hidden||!["sops","runtime"].includes(state.page)||state.agentRefreshInFlight)return;
  state.agentRefreshInFlight=true;
  try{await loadRuntime({preserveOnFailure:true})}finally{state.agentRefreshInFlight=false}
  if(state.page==="runtime")renderRuntimeStatus();else updateAgentRuntimeUi();
}
function agentPermissionProfiles(agentId){
  const agent=state.agents.find(a=>a.agentId===agentId);if(!agent)return["read_only"];
  if(Array.isArray(agent.permissionProfiles)&&agent.permissionProfiles.length)return agent.permissionProfiles;
  return agent.allowWrite?["read_only","workspace_write"]:["read_only"];
}
function permissionOptions(step){
  const allowed=agentPermissionProfiles(step.agentId);
  return allowed.map(value=>`<option value="${value}" ${step.permissionProfile===value?"selected":""}>${PERMISSION_LABELS[value]||value}</option>`).join("");
}
function nodeInspectorHtml(s){
  const role=roleById(s.roleId)||{name:s.roleName||"未知角色",duty:s.roleDuty||"",enabled:false};
  return `<div class="inspector-heading"><strong>节点配置</strong><small>步骤由上到下严格串行执行</small></div>
    <div class="selected-role"><i>${esc(role.name.slice(0,1))}</i><div><strong>${esc(role.name)}</strong><small>${esc(role.duty||"暂无职责说明")}</small></div>${role.enabled===false?'<b>已停用</b>':""}</div>
    <label>显示名称 *<input data-node-field="displayName" value="${esc(s.displayName)}"></label>
    <label>本步骤执行说明 *<textarea data-node-field="instruction" placeholder="描述该节点需要完成的工作">${esc(s.instruction)}</textarea></label>
    <div class="inspector-grid"><label>执行位置<select data-node-field="executorType"><option value="local">本机</option><option value="remote" ${s.executorType==="remote"?"selected":""}>远程</option></select></label><label>执行机 *<div class="agent-picker"><input data-node-field="agentId" maxlength="128" value="${esc(s.agentId||"")}" placeholder="例如：local" autocomplete="off"><button type="button" class="agent-picker-toggle" data-agent-menu-toggle aria-label="查看全部步骤执行机" aria-expanded="false">▼</button><div class="agent-picker-menu" hidden>${agentChoiceButtons("executor","executor")}</div></div></label></div>
    <label>模型<select data-node-field="modelOverride"><option value="">继承工作流默认模型</option>${MODELS.map(m=>`<option value="${m}" ${s.modelOverride===m?"selected":""}>${m}</option>`).join("")}</select></label>
    <div class="inspector-grid"><label>超时（秒）<input data-node-field="timeoutSec" type="number" min="10" max="7200" value="${s.timeoutSec}"></label><label>工作目录<input data-node-field="workingDirectory" value="${esc(s.workingDirectory)}" placeholder="可选"></label></div>
    <label>Skill 标签<input data-node-field="skills" value="${esc(s.skills.join(", "))}" placeholder="多个标签用逗号分隔"></label>
    <label>MCP 标签<input data-node-field="mcps" value="${esc(s.mcps.join(", "))}" placeholder="多个标签用逗号分隔"></label>
    <label>权限档位<select data-node-field="permissionProfile">${permissionOptions(s)}</select></label>
    <p class="inspector-hint">只读：业务工作区只读且不请求审批；工作区写入：允许修改工作区但不请求审批；自动审核：允许修改工作区，越界操作交由 Auto-review 判断。文件交接只开放托管输出目录并关闭网络。执行机关闭写入时只能选择只读。Skill 与 MCP 仍仅作为配置标签。</p>`;
}

function addRoleNode(roleId,index){
  const role=roleById(roleId);if(!role||!role.enabled)return;
  const suggested=state.agents.find(a=>a.enabled!==false&&(!Array.isArray(a.capabilities)||a.capabilities.includes("executor")));const node=normalizeStep({roleId:role.id,roleName:role.name,roleDuty:role.duty,displayName:role.name,instruction:role.duty,agentId:suggested?.agentId||"local"});
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
  if(!(d.supervisorAgentId||"").trim())return"请输入主监督执行机 ID。";
  if(!d.steps.length)return"请至少拖入一个角色节点。";
  if(d.supervisorTimeoutSec<10||d.supervisorTimeoutSec>7200)return"主监督最长时间必须在 10 到 7200 秒之间。";
  if(d.maxRetryCount<0||d.maxRetryCount>100)return"单次任务最多重跑次数必须在 0 到 100 之间。";
  for(let i=0;i<d.steps.length;i++){
    const s=d.steps[i];if(!s.displayName.trim())return`请填写第 ${i+1} 个节点的显示名称。`;
    if(!s.instruction.trim())return`请填写第 ${i+1} 个节点的执行说明。`;
    if(!(s.agentId||"").trim())return`请输入第 ${i+1} 个节点的执行机 ID。`;
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
$("#taskForm").addEventListener("submit",async e=>{e.preventDefault();const f=e.currentTarget,b={name:f.name.value,objective:f.objective.value,sopId:f.sopId.value,additionalNotes:f.additionalNotes.value,enabled:f.enabled.checked,dingtalkTargetId:f.dingtalkTargetId.value||null};try{await api(f.id.value?`/api/task-definitions/${f.id.value}`:"/api/task-definitions",{method:f.id.value?"PUT":"POST",body:JSON.stringify(b)});$("#taskDialog").close();toast("任务定义已保存");render()}catch(x){toast(x.message)}});

$("#content").addEventListener("click",async e=>{
  let botTestButton=null;
  try{
    const runtimeRefresh=e.target.closest("[data-runtime-refresh]");
    if(runtimeRefresh){runtimeRefresh.disabled=true;await refreshAgentRuntimeStatuses();return}
    const pickerToggle=e.target.closest("[data-agent-menu-toggle]");
    if(pickerToggle){
      const menu=pickerToggle.closest(".agent-picker").querySelector(".agent-picker-menu"),opening=menu.hidden;
      document.querySelectorAll(".agent-picker-menu").forEach(item=>item.hidden=true);
      document.querySelectorAll("[data-agent-menu-toggle]").forEach(item=>item.setAttribute("aria-expanded","false"));
      menu.hidden=!opening;pickerToggle.setAttribute("aria-expanded",String(opening));return;
    }
    const agentChoice=e.target.closest("[data-agent-choice]");
    if(agentChoice){
      if(agentChoice.dataset.agentChoice==="supervisor"&&state.sop.draft){state.sop.draft.supervisorAgentId=agentChoice.dataset.agentId;renderSopWorkspace();return}
      const step=selectedStep();if(agentChoice.dataset.agentChoice==="executor"&&step){step.agentId=agentChoice.dataset.agentId;if(!agentPermissionProfiles(step.agentId).includes(step.permissionProfile)){step.permissionProfile="read_only";step.writeEnabled=false}renderSopWorkspace();return}
    }
    if(!e.target.closest(".agent-picker")){document.querySelectorAll(".agent-picker-menu").forEach(item=>item.hidden=true);document.querySelectorAll("[data-agent-menu-toggle]").forEach(item=>item.setAttribute("aria-expanded","false"))}
    const test=e.target.closest("[data-feishu-test]");
    if(test){botTestButton=test;test.disabled=true;const result=await api("/api/feishu/config/test",{method:"POST",body:JSON.stringify(feishuPayload())});const output=$("#feishuTestResult");output.className=`test-result ${result.success?"success":"error"}`;output.textContent=result.message;test.disabled=false;return}
    const dingtalkTest=e.target.closest("[data-dingtalk-test]");
    if(dingtalkTest){botTestButton=dingtalkTest;dingtalkTest.disabled=true;const result=await api("/api/dingtalk/config/test",{method:"POST",body:JSON.stringify(dingtalkPayload())});const output=$("#dingtalkTestResult");output.className=`test-result ${result.success?"success":"error"}`;output.textContent=result.message;dingtalkTest.disabled=false;return}
    const targetType=e.target.closest("[data-target-type]");if(targetType){state.dingtalkTargetType=targetType.dataset.targetType;renderDingTalkTargets();return}
    const sync=e.target.closest("[data-target-sync]");if(sync){sync.disabled=true;const result=await api("/api/dingtalk/targets/sync-people",{method:"POST"});state.dingtalkTargets=await api("/api/dingtalk/targets");renderDingTalkTargets();toast(`同步完成：新增 ${result.created}，更新 ${result.updated}，失效 ${result.unavailable}`);return}
    const targetAction=e.target.closest("[data-target-save],[data-target-test],[data-target-delete]");
    if(targetAction){const card=targetAction.closest("[data-target-id]"),id=card.dataset.targetId;if(targetAction.matches("[data-target-save]")){await api(`/api/dingtalk/targets/${id}`,{method:"PUT",body:JSON.stringify({displayName:card.querySelector("[data-target-name]").value.trim(),enabled:card.querySelector("[data-target-enabled]").checked})});toast("通知对象已保存")}else if(targetAction.matches("[data-target-test]")){await api(`/api/dingtalk/targets/${id}/test`,{method:"POST"});toast("测试消息已发送")}else{if(!confirm("确定删除这个通知对象？"))return;await api(`/api/dingtalk/targets/${id}`,{method:"DELETE"});toast("通知对象已删除")}state.dingtalkTargets=await api("/api/dingtalk/targets");renderDingTalkTargets();return}
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
  if(sopField&&state.sop.draft){updateField(e.target,state.sop.draft,sopField);const title=document.querySelector(".canvas-toolbar strong");if(sopField==="name"&&title)title.textContent=e.target.value||"未命名工作流";if(sopField==="supervisorAgentId")updateAgentRuntimeUi();syncDirtyUi();return}
  const nodeField=e.target.dataset.nodeField,step=selectedStep();
  if(nodeField&&step){updateField(e.target,step,nodeField);const card=document.querySelector(`[data-node-id="${step._clientId}"]`);if(card&&nodeField==="displayName")card.querySelector(".node-role strong").textContent=e.target.value||roleById(step.roleId)?.name||"未命名节点";if(card&&nodeField==="instruction")card.querySelector(".node-body>p").textContent=e.target.value||"尚未填写执行说明";syncDirtyUi()}
});
$("#content").addEventListener("change",e=>{
  const sf=e.target.dataset.sopField;if(sf&&state.sop.draft){updateField(e.target,state.sop.draft,sf);syncDirtyUi();return}
  const nf=e.target.dataset.nodeField,step=selectedStep();if(nf&&step){updateField(e.target,step,nf);if(nf==="permissionProfile")step.writeEnabled=step.permissionProfile!=="read_only";if(nf==="agentId"&&!agentPermissionProfiles(step.agentId).includes(step.permissionProfile)){step.permissionProfile="read_only";step.writeEnabled=false;renderSopWorkspace();return}syncDirtyUi()}
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
  const map={roles:["角色管理","定义协作角色及其职责边界。","＋ 新建角色"],sops:["SOP 工作流","拖动角色配置可复用的严格串行流程。","＋ 新建 SOP"],tasks:["任务定义","保存任务配置、钉钉绑定、运行并追溯不可变快照。","＋ 新建任务"],runtime:["运行状态","查看 Python 网关和全部主监督执行机的实时状态。",""],feishu:["飞书机器人","配置长连接、固定任务和运行状态。",""],dingtalk:["钉钉机器人","配置 Stream 长连接并查看任务绑定。",""],"dingtalk-targets":["钉钉通知对象","维护任务定义可选择的人员或群聊。",""]};
  [$("#title").textContent,$("#subtitle").textContent,$("#create").textContent]=map[state.page];
  $("#create").classList.toggle("hidden",["runtime","feishu","dingtalk","dingtalk-targets"].includes(state.page));
  try{await render()}catch(e){toast(e.message)}
});
$("#create").onclick=()=>state.page==="roles"?openRole():state.page==="sops"?startNewSop():state.page==="tasks"?openTask():null;
$("#refresh").onclick=async()=>{if(state.page==="sops"&&!confirmDiscard())return;try{if(state.page==="sops"){const id=state.sop.draft?.id;await loadBase();if(id&&state.sops.some(s=>s.id===id))setDraft(await api(`/api/sops/${id}`));else if(state.sops.length)setDraft(state.sops[0]);else state.sop.draft=null;renderSopWorkspace()}else await render()}catch(e){toast(e.message)}};
$("#search").oninput=()=>render({reload:false});
window.addEventListener("beforeunload",e=>{if(isSopDirty()){e.preventDefault();e.returnValue=""}});
setInterval(refreshAgentRuntimeStatuses,10000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshAgentRuntimeStatuses()});
render().catch(e=>toast(e.message));
