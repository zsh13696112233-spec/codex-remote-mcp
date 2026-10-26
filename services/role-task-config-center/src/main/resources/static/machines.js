let machineGroups=[];
let selectedMachineGroupId=null;
const pendingMachineActions=new Set();
const machineRemarkDrafts=new Map();
const machineView={query:'',filter:'all',group:null,selected:null,returnTo:null,version:0,lastSuccess:null,error:'',loaded:false,refreshing:false};
// Lucide monitor (ISC); original geometry, styled locally. See vendor/lucide/LICENSE.
const machineMonitorSvg="<svg aria-hidden=\"true\" class=\"machine-monitor\"\n  xmlns=\"http://www.w3.org/2000/svg\"\n  width=\"24\"\n  height=\"24\"\n  viewBox=\"0 0 24 24\"\n  fill=\"none\"\n  stroke=\"currentColor\"\n  stroke-width=\"2\"\n  stroke-linecap=\"round\"\n  stroke-linejoin=\"round\"\n>\n  <rect width=\"20\" height=\"14\" x=\"2\" y=\"3\" rx=\"2\" />\n  <line x1=\"8\" x2=\"16\" y1=\"21\" y2=\"21\" />\n  <line x1=\"12\" x2=\"12\" y1=\"17\" y2=\"21\" />\n</svg>";
function machineConnection(a){
  const supervisor=(a.capabilities||[]).includes('supervisor');
  const connection=supervisor?a.connectionStatus:a.testStatus==='passed'?'online':a.testStatus==='failed'?'offline':'unknown';
  const kind=['online','offline'].includes(connection)?connection:'unknown';
  return {kind,label:supervisor?({online:'在线',offline:'离线',unknown:'在线状态未知'}[kind]):({online:'最近检测在线',offline:'最近检测离线',unknown:'未检测'}[kind])};
}
function machineDisplayState(a){return a.enabled?machineConnection(a):{kind:'disabled',label:'已停用'}}
function machineMatches(a){
  const query=machineView.query.trim().toLowerCase();
  return (!query||`${a.remark||''} ${a.ip}:${a.port}`.toLowerCase().includes(query))&&(machineView.filter==='all'||machineDisplayState(a).kind===machineView.filter);
}
function machineSorted(items){return [...items].sort((a,b)=>a.ip.localeCompare(b.ip,undefined,{numeric:true})||a.port-b.port||a.agentId.localeCompare(b.agentId))}
function leaveMachines(){
  machineView.version++;closeMachineDetails();
  Object.assign(machineView,{query:'',filter:'all',group:null,loaded:false,lastSuccess:null,error:''});
  machineRemarkDrafts.clear();
}
async function renderMachines(){
  if(machineView.group!==groupState.selected){machineView.version++;closeMachineDetails();machineView.group=groupState.selected;machineView.loaded=false;machineView.lastSuccess=null;machineView.error=''}
  selectedMachineGroupId=concreteGroup();
  machineGroups=groupState.items;
  document.querySelector('.toolbar').classList.add('hidden');
  const content=document.querySelector('#content');content.className='content machine-content';
  if(!document.querySelector('#machineGrid'))renderMachineBoard();
  updateMachineBoard();
  await Promise.all([refreshMachineStatuses(true),loadGatewayReady()]);
}
async function refreshMachineStatuses(force=false){
  if(state.page!=='machines'||(!force&&(document.hidden||machineView.refreshing||pendingMachineActions.size||[...machineRemarkDrafts.values()].some(d=>d.saving))))return;
  const version=++machineView.version,group=groupState.selected;
  machineView.refreshing=true;
  const current=()=>version===machineView.version&&state.page==='machines'&&group===groupState.selected;
  try{
    const result=await api('/api/agents');
    if(!current())return;
    state.agents=result.agents||[];state.agentsAvailable=true;
    machineView.loaded=true;machineView.error='';machineView.lastSuccess=new Date().toISOString();
  }catch(error){
    if(!current())return;
    state.agentsAvailable=false;
    machineView.error=machineView.loaded?'状态更新失败，当前为上次结果。':'机器列表加载失败，请重试。';
  }finally{if(version===machineView.version)machineView.refreshing=false}
  if(current())updateMachineBoard();
}
function renderMachineBoard(){
  document.querySelector('#content').innerHTML=`<div class="machine-board"><div class="machine-group-detail">
    <div class="machine-group-heading"><div><h2>机器管理</h2><p id="machineTotal"></p></div><button data-machine-action="refresh">刷新列表</button></div>
    <div class="machine-filters"><input id="machineSearch" type="search" aria-label="搜索机器" placeholder="搜索备注 / IP / 端口" value="${esc(machineView.query)}"><select id="machineFilter" aria-label="筛选机器状态">${[['all','全部状态'],['online','在线'],['offline','离线'],['unknown','未知'],['disabled','已停用']].map(([value,label])=>`<option value="${value}" ${machineView.filter===value?'selected':''}>${label}</option>`).join('')}</select></div>
    <p id="machineRefreshStatus" role="status"></p><div id="machineGrid" class="machine-role-columns"></div>
    <p class="machine-muted">每 10 秒更新状态。普通执行机显示最近连接检测结果，需手动“检测连接”更新。</p>
  </div></div>`;
  updateMachineBoard();
}
function updateMachineBoard(){
  if(state.page!=='machines')return;
  const grid=document.querySelector('#machineGrid');if(!grid)return;
  const members=machineView.loaded?state.agents.filter(groupMatches):[];
  document.querySelector('#machineTotal').textContent=`${groupState.items.find(g=>g.id===groupState.selected)?.name||'全部机器'} · ${new Set(members.map(a=>a.agentId)).size} 台机器`;
  const note=document.querySelector('#machineRefreshStatus');
  note.textContent=machineView.error||'';
  if(machineView.lastSuccess)note.textContent+=` 最近成功更新 ${time(machineView.lastSuccess)}`;
  note.classList.toggle('machine-error',!!machineView.error);
  if(!machineView.loaded){grid.machineMarkup=null;grid.innerHTML=`<div class="empty">${machineView.error?'请点击“刷新列表”重试。':'正在加载机器…'}</div>`;return}
  const focused=document.activeElement?.closest('[data-machine-action="details"]');
  const focusId=focused?.dataset.id,focusRole=focused?.dataset.role;
  const html=machineRoleColumn(members,'supervisor',true)+machineRoleColumn(members,'executor',true);
  if(grid.machineMarkup!==html){grid.innerHTML=html;grid.machineMarkup=html;if(focusId)[...grid.querySelectorAll('[data-machine-action="details"]')].find(b=>b.dataset.id===focusId&&b.dataset.role===focusRole)?.focus({preventScroll:true})}
  updateMachineDetails();
}
function machineRoleColumn(members,role,hasGroup){
  const title=role==='supervisor'?'主监督机':'执行机',all=members.filter(a=>(a.capabilities||[]).includes(role)),items=machineSorted(all.filter(machineMatches));
  const filtered=machineView.query.trim()||machineView.filter!=='all';
  return `<section class="machine-role-column ${role}" aria-label="${title}"><div class="machine-column-heading"><h2>${title} <span>${filtered?`${items.length} / ${all.length}`:all.length} 台</span></h2><button data-machine-action="add" data-role="${role}">＋ 添加</button></div><div class="machine-card-list">${items.map(a=>machineCard(a,role)).join('')||`<div class="machine-role-empty"><strong>${all.length?'没有符合条件的机器':`暂无${title}`}</strong><p>${all.length?'请调整搜索或状态筛选。':hasGroup?'点击添加，登记机器。':'请先选择分组。'}</p></div>`}</div></section>`;
}
function machineCard(a,role){
  const view=machineDisplayState(a),name=a.remark||a.ip;
  return `<button type="button" class="machine-tile ${view.kind}" data-machine-action="details" data-id="${esc(a.agentId)}" data-role="${role}" aria-label="${esc(name)}，${esc(a.ip)}:${a.port}，${view.label}"><span class="machine-icon">${machineMonitorSvg}${view.kind==='unknown'?'<b class="machine-icon-mark">?</b>':view.kind==='disabled'?'<b class="machine-icon-mark">−</b>':''}</span><strong title="${esc(name)}">${esc(name)}</strong><small title="${esc(a.ip)}:${a.port}">${a.remark?`${esc(a.ip)}:${a.port}`:`端口 ${a.port}`}</small><span class="machine-tile-state">${view.label}</span>${(a.capabilities||[]).length>1?'<span class="machine-dual-role">兼任监督 / 执行</span>':''}</button>`;
}
function machineDetailReadOnly(a){
  const connection=machineConnection(a),executor=(a.capabilities||[]).includes('executor');
  return `<dl><dt>地址</dt><dd>${esc(a.ip)}:${a.port}</dd><dt>分组</dt><dd>${esc(groupState.items.find(g=>g.id===a.groupId)?.name||'分组不可用')}</dd><dt>角色</dt><dd>${a.capabilities.map(c=>c==='supervisor'?'主监督机':'执行机').join(' / ')}</dd><dt>启停</dt><dd>${a.enabled?'启用中':'已停用'}</dd><dt>连接状态</dt><dd>${connection.label}</dd><dt>检测结果</dt><dd>${a.testStatus==='passed'?'检测通过':a.testStatus==='failed'?'检测失败':'未检测'}</dd><dt>最近检测</dt><dd>${esc(time(a.testedAt))}</dd></dl>${executor?`<h3>Skill 安装设置</h3><p>下发：${a.skillInstallation?.enabled?'已授权':'未授权'}</p><p>${esc(a.skillInstallation?.root||'尚未配置安装目录')}</p><p>${esc(a.skillInstallation?.checkMessage||'尚未检测目录')}</p>${a.skillInstallation?.checkedAt?`<p>${esc(time(a.skillInstallation.checkedAt))}</p>`:''}`:''}`;
}
function machineDetailActions(a){
  const actions=['test','edit',...((a.capabilities||[]).includes('executor')?['skill-check']:[]),'toggle','delete'];
  return actions.map(action=>`<button data-machine-action="${action}" data-id="${esc(a.agentId)}" ${pendingMachineActions.has(a.agentId)?'disabled':''}>${action==='test'?'检测连接':action==='edit'?'编辑':action==='skill-check'?'检测 Skill 目录':action==='delete'?'删除':a.enabled?'停用':'启用'}</button>`).join('');
}
function openMachineDetails(a,trigger){
  machineView.selected=a.agentId;machineView.returnTo={id:a.agentId,role:trigger.dataset.role};
  let dialog=document.querySelector('#machineDetails');
  if(!dialog){
    dialog=document.createElement('dialog');dialog.id='machineDetails';dialog.setAttribute('aria-labelledby','machineDetailsTitle');document.body.append(dialog);
    dialog.addEventListener('close',()=>{machineView.selected=null;const target=machineView.returnTo;machineView.returnTo=null;if(target)[...document.querySelectorAll('[data-machine-action="details"]')].find(b=>b.dataset.id===target.id&&b.dataset.role===target.role)?.focus({preventScroll:true})});
  }
  dialog.innerHTML=`<header><h2 id="machineDetailsTitle">机器详情</h2><button type="button" class="dialog-close" data-machine-action="close-details" aria-label="关闭机器详情" title="关闭" autofocus>×</button></header><label>备注${machineRemarkHtml(a)}</label><div data-machine-detail-info></div><div class="machine-card-actions"></div>`;
  updateMachineDetails();dialog.showModal();
}
function closeMachineDetails(){machineView.selected=null;document.querySelector('#machineDetails')?.close()}
function updateMachineDetails(){
  if(!machineView.selected)return;
  const a=state.agents.find(a=>a.agentId===machineView.selected&&groupMatches(a));
  if(!a){closeMachineDetails();toast('机器已删除或移出当前分组。');return}
  const dialog=document.querySelector('#machineDetails');if(!dialog)return;
  const info=dialog.querySelector('[data-machine-detail-info]'),actions=dialog.querySelector('.machine-card-actions');
  const html=machineDetailReadOnly(a),buttons=machineDetailActions(a);
  if(info.innerHTML!==html)info.innerHTML=html;
  if(actions.innerHTML!==buttons)actions.innerHTML=buttons;
  const input=dialog.querySelector('[data-machine-remark]');
  if(!machineRemarkDrafts.has(a.agentId)&&document.activeElement!==input)input.value=a.remark||'';
}

function machineRemarkHtml(a){
  const draft=machineRemarkDrafts.get(a.agentId);
  return `<div class="machine-remark" data-machine-remark-row="${esc(a.agentId)}"><input data-machine-remark="${esc(a.agentId)}" aria-label="机器备注" maxlength="200" value="${esc(draft?.value??a.remark??'')}" placeholder="点击添加备注" ${draft?.saving?'disabled':''} aria-invalid="${!!draft?.error}"><small role="status" aria-live="polite">${esc(draft?.saving?'正在保存…':draft?.error||'')}</small></div>`;
}
function paintMachineRemark(id){
  const draft=machineRemarkDrafts.get(id),machine=state.agents.find(a=>a.agentId===id);
  document.querySelectorAll('[data-machine-remark-row]').forEach(row=>{
    if(row.dataset.machineRemarkRow!==id)return;
    const input=row.querySelector('input');
    input.value=draft?.value??machine?.remark??'';input.disabled=!!draft?.saving;
    input.setAttribute('aria-invalid',String(!!draft?.error));
    row.querySelector('small').textContent=draft?.saving?'正在保存…':draft?.error||'';
  });
}
async function saveMachineRemark(input){
  const id=input.dataset.machineRemark,machine=state.agents.find(a=>a.agentId===id);
  if(!machine||machineRemarkDrafts.get(id)?.saving)return;
  const value=input.value.trim();
  if(value===(machine.remark||'')){machineRemarkDrafts.delete(id);paintMachineRemark(id);return}
  const draft={value,error:'',saving:false};machineRemarkDrafts.set(id,draft);
  if([...value].length>100){draft.error='备注最多 100 个字符，尚未保存。';paintMachineRemark(id);return}
  draft.saving=true;machineView.version++;paintMachineRemark(id);
  try{
    const saved=await api(`/api/agents/${encodeURIComponent(id)}/remark`,{method:'PUT',body:JSON.stringify({remark:value})});
    const current=state.agents.find(a=>a.agentId===id);if(current)current.remark=saved.remark;
    machineRemarkDrafts.delete(id);machineView.version++;
  }catch(error){draft.saving=false;draft.error=`保存失败：${error.message}。点击备注后移开焦点可重试。`}
  paintMachineRemark(id);
  updateMachineBoard();
  machineView.refreshing=false;
}
document.addEventListener('input',event=>{
  const input=event.target;
  if(input.id==='machineSearch'){machineView.query=input.value;updateMachineBoard();return}
  if(!input.matches('[data-machine-remark]'))return;
  if(machineRemarkDrafts.get(input.dataset.machineRemark)?.saving)return;
  machineRemarkDrafts.set(input.dataset.machineRemark,{value:input.value,error:'',saving:false});
});
document.addEventListener('change',event=>{if(event.target.id==='machineFilter'){machineView.filter=event.target.value;updateMachineBoard()}});
document.addEventListener('focusout',event=>{
  if(event.target.matches('[data-machine-remark]'))return saveMachineRemark(event.target);
});
document.addEventListener('keydown',event=>{
  const input=event.target;if(!input.matches('[data-machine-remark]')||event.isComposing)return;
  if(event.key==='Enter'){event.preventDefault();input.blur()}
  if(event.key==='Escape'){event.preventDefault();machineRemarkDrafts.delete(input.dataset.machineRemark);paintMachineRemark(input.dataset.machineRemark);input.blur()}
});

function machineBody(a){return {ip:a.ip,port:a.port,groupId:a.groupId,capabilities:a.capabilities,enabled:a.enabled}}
function openMachineGroup(group){
  let dialog=document.querySelector('#machineGroupDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='machineGroupDialog';document.body.append(dialog)}
  dialog.innerHTML=`<form><button type="button" class="dialog-close" data-dialog-close aria-label="关闭弹窗" title="关闭">×</button><h2>${group?'重命名分组':'新建分组'}</h2><label>分组名称<input name="groupName" required maxlength="100" value="${esc(group?.name||'')}"></label><p role="alert"></p><footer><button type="button" data-dialog-close>取消</button><button type="submit" class="primary">保存分组</button></footer></form>`;
  dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]');button.disabled=true;
    try{await api(group?`/api/agent-groups/${encodeURIComponent(group.id)}`:'/api/agent-groups',{method:group?'PUT':'POST',body:JSON.stringify({name:form.elements.groupName.value.trim()})});dialog.close();await render()}
    catch(error){form.querySelector('[role=alert]').textContent=error.message}finally{button.disabled=false}
  };dialog.showModal();
}
function openMachine(a,role='executor'){
  if(!a&&!concreteGroup())return chooseGroup(()=>openMachine(a,role));
  let dialog=document.querySelector('#machineDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='machineDialog';document.body.append(dialog)}
  dialog.innerHTML=`<form><button type="button" class="dialog-close" data-dialog-close aria-label="关闭弹窗" title="关闭">×</button><h2>${a?'编辑':'添加'}机器</h2>${!a?'<label>备注<input name="remark" maxlength="200" placeholder="可选，最多 100 个字符"></label>':''}<label>IP 地址<input name="ip" required maxlength="45" value="${esc(a?.ip||'')}"></label><label>执行服务端口<input name="port" type="number" min="1" max="65535" required value="${a?.port||4500}"></label><label>分组<select name="groupId">${machineGroups.map(g=>`<option value="${esc(g.id)}" ${g.id===(a?.groupId||selectedMachineGroupId)?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label><label class="check"><input type="checkbox" name="supervisor" ${(a?a.capabilities.includes('supervisor'):role==='supervisor')?'checked':''}>主监督</label><label class="check"><input type="checkbox" name="executor" ${(a?a.capabilities.includes('executor'):role==='executor')?'checked':''}>执行机</label><fieldset><legend>Skill 安装设置</legend><label class="check"><input type="checkbox" name="skillEnabled" ${a?.skillInstallation?.enabled?'checked':''}>允许向这台执行机下发 Skill</label><label>执行机上的 Skill 安装目录<input name="skillRoot" maxlength="1024" value="${esc(a?.skillInstallation?.root||'')}" placeholder="填写执行服务可识别的绝对目录"></label><p class="machine-muted">保存后持续有效，无需重启。目录须预先存在，安装仍受执行机写权限限制。保存后可在机器详情检测目录。</p></fieldset><p role="alert" class="machine-error"></p><footer><button type="button" data-close-machine>取消</button><button type="submit" class="primary">保存</button></footer></form>`;
  dialog.querySelector('[data-close-machine]').onclick=()=>dialog.close();
  const mcpSettings=a?.mcpInstallation||{};
  const mcpFields=document.createElement('fieldset');
  mcpFields.innerHTML=`<legend>MCP 安装设置（Windows）</legend><label class="check"><input name="mcpEnabled" type="checkbox" ${mcpSettings.enabled?'checked':''}>允许安装 MCP</label><label>安装目录<input name="mcpRoot" value="${esc(mcpSettings.installRoot||mcpSettings.programRoot||'')}" maxlength="1024"></label><p class="machine-muted">平台在此目录内分别创建程序和临时子目录。Python、Node.js 由安装助手查找，无需填写路径；缺少时不自动安装。附带 Skill 使用上方授权目录。</p>`;
  dialog.querySelector('form footer').before(mcpFields);
  dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const f=event.target,button=f.querySelector('[type=submit]');button.disabled=true;
    const capabilities=['supervisor','executor'].filter(c=>f.elements[c].checked);
    try{
      if(!a&&[...f.elements.remark.value.trim()].length>100)throw new Error('机器备注最多 100 个字符。');
      if(!capabilities.length)throw new Error('请至少选择一种能力。');
      machineView.version++;
      await api(a?`/api/agents/${encodeURIComponent(a.agentId)}`:'/api/agents',{method:a?'PUT':'POST',body:JSON.stringify({...(!a?{remark:f.elements.remark.value.trim()}:{}),ip:f.elements.ip.value.trim(),port:Number(f.elements.port.value),groupId:f.elements.groupId.value,capabilities,enabled:a?.enabled??true,...(!a||f.elements.mcpEnabled.checked!==!!a.mcpInstallation?.enabled||f.elements.mcpRoot.value.trim()!==(a.mcpInstallation?.installRoot||a.mcpInstallation?.programRoot||'')?{mcpInstallation:{enabled:f.elements.mcpEnabled.checked,platform:'windows',installRoot:f.elements.mcpRoot.value.trim()}}:{}),skillInstallation:{enabled:f.elements.skillEnabled.checked,root:f.elements.skillRoot.value.trim()}})});
      selectedMachineGroupId=f.elements.groupId.value;dialog.close();await render();toast('已保存。连接信息有变化时请重新检测。');
    }catch(error){f.querySelector('.machine-error').textContent=error.message}finally{button.disabled=false;machineView.refreshing=false}
  };dialog.showModal();
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-machine-action]');if(!button)return;
  const action=button.dataset.machineAction,id=button.dataset.id,a=state.agents.find(x=>x.agentId===id);
  if(action==='details'){openMachineDetails(a,button);return}
  if(action==='close-details'){closeMachineDetails();return}
  if(pendingMachineActions.has(id))return;
  if(action==='delete'&&!confirm('确定删除这台机器？删除后将从机器管理和可选列表隐藏，历史记录保留。'))return;
  if(action==='group-select'){await selectGroup(id);return}
  button.disabled=true;
  if(action==='test'||action==='toggle'||action==='skill-check'||action==='delete'){
    pendingMachineActions.add(id);machineView.version++;
    document.querySelectorAll('.machine-card-actions [data-id]').forEach(b=>{if(b.dataset.id===id)b.disabled=true});
  }
  try{
    if(action==='add'||action==='edit'){openMachine(a,button.dataset.role);return}
    if(action==='refresh'){await render();return}
    if(action==='group-add'||action==='group-edit'){
      editGroup(id);return;
    }else if(action==='group-delete'){
      if(!confirm('删除这个空分组？'))return;
      await api(`/api/agent-groups/${encodeURIComponent(id)}`,{method:'DELETE'});
    }else if(action==='test'){
      button.textContent='正在检测…';
      const result=await api(`/api/agents/${encodeURIComponent(id)}/test`,{method:'POST',body:'{}'});toast(result.message);
    }else if(action==='skill-check'){
      button.textContent='正在检测…';
      const result=await api(`/api/skills/machines/${encodeURIComponent(id)}/check`,{method:'POST',body:'{}'});toast(result.message);
    }else if(action==='delete'){
      await api(`/api/agents/${encodeURIComponent(id)}`,{method:'DELETE'});
      machineRemarkDrafts.delete(id);if(machineView.selected===id)closeMachineDetails();toast('机器已删除');
    }else if(action==='toggle'){
      await api(`/api/agents/${encodeURIComponent(id)}`,{method:'PUT',body:JSON.stringify({...machineBody(a),enabled:!a.enabled})});
    }
    await render();
  }catch(error){toast(error.message)}finally{pendingMachineActions.delete(id);machineView.refreshing=false;document.querySelectorAll('.machine-card-actions [data-id]').forEach(b=>{if(b.dataset.id===id)b.disabled=false});button.disabled=false;if(action==='test')button.textContent='检测连接';if(action==='skill-check')button.textContent='检测 Skill 目录'}
});
