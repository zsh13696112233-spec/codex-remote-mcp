let machineGroups=[];
let selectedMachineGroupId=null;
const pendingMachineActions=new Set();
async function renderMachines(){
  selectedMachineGroupId=concreteGroup();
  document.querySelector('.toolbar').classList.add('hidden');
  const content=document.querySelector('#content');content.className='content machine-content';
  content.innerHTML='<div class="empty">正在加载机器…</div>';
  try{
    await loadRuntime();
    machineGroups=(await api('/api/agent-groups')).groups;
    if(!state.agentsAvailable)throw new Error('机器列表加载失败，请重试。');
    if(state.page!=='machines')return;
    renderMachineBoard();
  }catch(error){if(state.page==='machines')content.innerHTML=`<div class="empty">${esc(error.message)} <button data-machine-action="refresh">重试</button></div>`}
}
function renderMachineBoard(){
  const group=machineGroups.find(g=>g.id===concreteGroup());
  selectedMachineGroupId=concreteGroup();
  const members=state.agents.filter(groupMatches);
  document.querySelector('#content').innerHTML=`

    <div class="machine-board">
      <section class="machine-groups" aria-label="机器分组">
        <div class="machine-section-title"><h2>分组 <span>${machineGroups.length}</span></h2></div>
        <button class="machine-new-group" data-machine-action="group-add">＋ 新建分组</button>
        <div class="machine-group-list">${machineGroups.map(g=>`<button class="machine-group ${g.id===selectedMachineGroupId?'active':''}" data-machine-action="group-select" data-id="${esc(g.id)}" aria-pressed="${g.id===selectedMachineGroupId}"><span>${esc(g.name)}</span><small>${state.agents.filter(a=>a.groupId===g.id).length} 台机器</small></button>`).join('')||'<p class="machine-muted">暂无分组，请先新建分组。</p>'}</div>
        ${group?`<div class="machine-group-tools"><button data-machine-action="group-edit" data-id="${esc(group.id)}">重命名</button><button data-machine-action="group-delete" data-id="${esc(group.id)}" ${members.length?'disabled title="仅空分组可删除"':''}>删除分组</button></div>`:''}
      </section>
      <div class="machine-group-detail">
        <div class="machine-group-heading"><div><h2>${group?esc(group.name):groupState.selected==='unassigned'?'待归组':'全部机器'}</h2><p>${group?`${members.length} 台机器`:'先选择分组，再登记机器。'}</p></div><button data-machine-action="refresh">刷新列表</button></div>
        <div class="machine-role-columns">${machineRoleColumn(members,'supervisor',true)}${machineRoleColumn(members,'executor',true)}</div>
      </div>
    </div>`;
}
function machineRoleColumn(members,role,hasGroup){
  const supervisor=role==='supervisor',title=supervisor?'主监督机':'执行机',items=members.filter(a=>(a.capabilities||[]).includes(role));
  return `<section class="machine-role-column ${role}" aria-label="${title}"><div class="machine-column-heading"><div><h2>${title} <span>${items.length}</span></h2></div><button data-machine-action="add" data-role="${role}" ${hasGroup?'':'disabled'}>＋ 添加</button></div><div class="machine-card-list">${items.map(a=>machineCard(a,role)).join('')||`<div class="machine-role-empty"><strong>暂无${title}</strong><p>${hasGroup?`添加一台${title}，完善分组配置。`:'请先在左侧新建分组。'}</p></div>`}</div></section>`;
}
function machineCard(a,role){
  const tested=a.testStatus==='passed',failed=a.testStatus==='failed';
  const supervisor=(a.capabilities||[]).includes('supervisor');
  const online=supervisor?a.connectionStatus==='online':tested;
  const offline=supervisor?a.connectionStatus==='offline':failed;
  return `<article class="machine-card"><div class="machine-card-heading"><div><h3>${esc(a.name||a.ip)} ${groupMark(a)}<small> : ${esc(a.port)}</small></h3></div>${status(a)}</div><div class="machine-state-row"><span class="machine-state ${online?'online':offline?'offline':''}">● ${online?'在线':offline?'离线':'在线状态未知'}</span><span class="machine-state ${tested?'online':failed?'offline':''}">${tested?'检测通过':failed?'检测失败':'未检测'}</span>${(a.capabilities||[]).length>1?'<span class="machine-dual-role">兼任监督 / 执行</span>':''}</div><p class="machine-last-tested">最近检测 <span>${time(a.testedAt)}</span></p>${role==='executor'?`<p class="machine-last-tested machine-skill-settings">Skill 下发：${a.skillInstallation?.enabled?'已授权':'未授权'}<br>${esc(a.skillInstallation?.root||'尚未配置安装目录')}<br>${esc(a.skillInstallation?.checkMessage||'尚未检测目录')}${a.skillInstallation?.checkedAt?` · ${time(a.skillInstallation.checkedAt)}`:''}</p>`:''}<div class="machine-card-actions">${(role==='executor'?['test','edit','skill-check','toggle']:['test','edit','toggle']).map(action=>`<button data-machine-action="${action}" data-id="${esc(a.agentId)}" ${pendingMachineActions.has(a.agentId)?'disabled':''}>${action==='test'?'检测连接':action==='edit'?'编辑':action==='skill-check'?'检测 Skill 目录':a.enabled?'停用':'启用'}</button>`).join('')}</div></article>`;
}

function machineBody(a){return {ip:a.ip,port:a.port,groupId:a.groupId,capabilities:a.capabilities,enabled:a.enabled}}
function openMachineGroup(group){
  let dialog=document.querySelector('#machineGroupDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='machineGroupDialog';document.body.append(dialog)}
  dialog.innerHTML=`<form><h2>${group?'重命名分组':'新建分组'}</h2><label>分组名称<input name="groupName" required maxlength="100" value="${esc(group?.name||'')}"></label><p role="alert"></p><footer><button type="button">取消</button><button type="submit" class="primary">保存分组</button></footer></form>`;
  dialog.querySelector('[type=button]').onclick=()=>dialog.close();
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
  dialog.innerHTML=`<form><h2>${a?'编辑':'添加'}机器</h2><label>IP 地址<input name="ip" required maxlength="45" value="${esc(a?.ip||'')}"></label><label>执行服务端口<input name="port" type="number" min="1" max="65535" required value="${a?.port||4500}"></label><label>分组<select name="groupId">${machineGroups.map(g=>`<option value="${esc(g.id)}" ${g.id===(a?.groupId||selectedMachineGroupId)?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label><label class="check"><input type="checkbox" name="supervisor" ${(a?a.capabilities.includes('supervisor'):role==='supervisor')?'checked':''}>主监督</label><label class="check"><input type="checkbox" name="executor" ${(a?a.capabilities.includes('executor'):role==='executor')?'checked':''}>执行机</label><fieldset><legend>Skill 安装设置</legend><label class="check"><input type="checkbox" name="skillEnabled" ${a?.skillInstallation?.enabled?'checked':''}>允许向这台执行机下发 Skill</label><label>执行机上的 Skill 安装目录<input name="skillRoot" maxlength="1024" value="${esc(a?.skillInstallation?.root||'')}" placeholder="填写执行服务可识别的绝对目录"></label><p class="machine-muted">保存后持续有效，无需重启。目录须预先存在，安装仍受执行机写权限限制。保存后可在机器卡片检测目录。</p></fieldset><p role="alert" class="machine-error"></p><footer><button type="button" data-close-machine>取消</button><button type="submit" class="primary">保存</button></footer></form>`;
  dialog.querySelector('[data-close-machine]').onclick=()=>dialog.close();
  const mcpSettings=a?.mcpInstallation||{};
  const mcpFields=document.createElement('fieldset');
  mcpFields.innerHTML=`<legend>MCP 安装设置（Windows）</legend><label class="check"><input name="mcpEnabled" type="checkbox" ${mcpSettings.enabled?'checked':''}>允许安装 MCP</label><label>安装目录<input name="mcpRoot" value="${esc(mcpSettings.installRoot||mcpSettings.programRoot||'')}" maxlength="1024"></label><p class="machine-muted">平台在此目录内分别创建程序和临时子目录。Python、Node.js 由安装助手查找，无需填写路径；缺少时不自动安装。附带 Skill 使用上方授权目录。</p>`;
  dialog.querySelector('form footer').before(mcpFields);
  dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const f=event.target,button=f.querySelector('[type=submit]');button.disabled=true;
    const capabilities=['supervisor','executor'].filter(c=>f.elements[c].checked);
    try{
      if(!capabilities.length)throw new Error('请至少选择一种能力。');
      await api(a?`/api/agents/${encodeURIComponent(a.agentId)}`:'/api/agents',{method:a?'PUT':'POST',body:JSON.stringify({ip:f.elements.ip.value.trim(),port:Number(f.elements.port.value),groupId:f.elements.groupId.value,capabilities,enabled:a?.enabled??true,mcpInstallation:{enabled:f.elements.mcpEnabled.checked,platform:'windows',installRoot:f.elements.mcpRoot.value.trim()},skillInstallation:{enabled:f.elements.skillEnabled.checked,root:f.elements.skillRoot.value.trim()}})});
      selectedMachineGroupId=f.elements.groupId.value;dialog.close();await render();toast('已保存，请检测连接。');
    }catch(error){f.querySelector('.machine-error').textContent=error.message}finally{button.disabled=false}
  };dialog.showModal();
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-machine-action]');if(!button)return;
  const action=button.dataset.machineAction,id=button.dataset.id,a=state.agents.find(x=>x.agentId===id);
  if(pendingMachineActions.has(id))return;
  if(action==='group-select'){await selectGroup(id);return}
  button.disabled=true;
  if(action==='test'||action==='toggle'||action==='skill-check'){
    pendingMachineActions.add(id);
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
    }else if(action==='toggle'){
      await api(`/api/agents/${encodeURIComponent(id)}`,{method:'PUT',body:JSON.stringify({...machineBody(a),enabled:!a.enabled})});
    }
    await render();
  }catch(error){toast(error.message)}finally{pendingMachineActions.delete(id);document.querySelectorAll('.machine-card-actions [data-id]').forEach(b=>{if(b.dataset.id===id)b.disabled=false});button.disabled=false;if(action==='test')button.textContent='检测连接';if(action==='skill-check')button.textContent='检测 Skill 目录'}
});
