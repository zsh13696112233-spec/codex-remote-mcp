let machineGroups=[];
async function renderMachines(){
  document.querySelector('.toolbar').classList.add('hidden');
  const content=document.querySelector('#content');content.className='content';
  content.innerHTML='<div class="empty">正在加载机器…</div>';
  try{
    await loadRuntime();
    machineGroups=(await api('/api/agent-groups')).groups;
    content.innerHTML=`<div class="actions"><button data-machine-action="group-add">＋ 新建分组</button><button data-machine-action="add" ${machineGroups.length?'':'disabled'}>＋ 添加机器</button><button data-machine-action="refresh">刷新</button></div>${machineGroups.length?machineGroups.map(g=>`<section class="card" style="display:block"><div class="actions"><h3>${esc(g.name)}</h3><button data-machine-action="group-edit" data-id="${esc(g.id)}">重命名</button><button data-machine-action="group-delete" data-id="${esc(g.id)}">删除分组</button></div>${state.agents.filter(a=>a.groupId===g.id).map(a=>`<article class="card"><div><h3>${esc(a.name)} : ${a.port} ${status(a)}</h3><p>${(a.capabilities||[]).map(c=>c==='supervisor'?'主监督':'执行机').join(' / ')} · ${a.testStatus==='passed'?'检测通过':a.testStatus==='failed'?'检测失败':'未检测'}${a.connectionStatus?` · ${a.connectionStatus==='online'?'在线':a.connectionStatus==='offline'?'离线':'在线状态未知'}`:''}</p><small>编号：${esc(a.agentId)} · 最近检测：${time(a.testedAt)}</small></div><div class="actions"><button data-machine-action="test" data-id="${esc(a.agentId)}">检测连接</button><button data-machine-action="edit" data-id="${esc(a.agentId)}">编辑</button><button data-machine-action="toggle" data-id="${esc(a.agentId)}">${a.enabled?'停用':'启用'}</button></div></article>`).join('')||'<p>暂无机器，请添加机器。</p>'}</section>`).join(''):'<div class="empty">请先建立分组，再添加机器。</div>'}`;
  }catch(error){content.innerHTML=`<div class="empty">${esc(error.message)} <button data-machine-action="refresh">重试</button></div>`}
}

function machineBody(a){return {ip:a.ip,port:a.port,groupId:a.groupId,capabilities:a.capabilities,enabled:a.enabled}}
function openMachineGroup(group){
  let dialog=document.querySelector('#machineGroupDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='machineGroupDialog';document.body.append(dialog)}
  dialog.innerHTML=`<form><h2>${group?'重命名分组':'新建分组'}</h2><label>分组名称<input name="groupName" required maxlength="100" value="${esc(group?.name||'')}"></label><p role="alert"></p><footer><button type="button">取消</button><button type="submit" class="primary">保存分组</button></footer></form>`;
  dialog.querySelector('[type=button]').onclick=()=>dialog.close();
  dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]');button.disabled=true;
    try{await api(group?`/api/agent-groups/${encodeURIComponent(group.id)}`:'/api/agent-groups',{method:group?'PUT':'POST',body:JSON.stringify({name:form.elements.groupName.value.trim()})});dialog.close();await renderMachines()}
    catch(error){form.querySelector('[role=alert]').textContent=error.message}finally{button.disabled=false}
  };dialog.showModal();
}
function openMachine(a){
  let dialog=document.querySelector('#machineDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='machineDialog';document.body.append(dialog)}
  dialog.innerHTML=`<form><h2>${a?'编辑':'添加'}机器</h2><label>IP 地址<input name="ip" required maxlength="45" value="${esc(a?.ip||'')}"></label><label>执行服务端口<input name="port" type="number" min="1" max="65535" required value="${a?.port||4500}"></label><label>分组<select name="groupId">${machineGroups.map(g=>`<option value="${esc(g.id)}" ${g.id===a?.groupId?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label><label class="check"><input type="checkbox" name="supervisor" ${a?.capabilities.includes('supervisor')?'checked':''}>主监督</label><label class="check"><input type="checkbox" name="executor" ${!a||a.capabilities.includes('executor')?'checked':''}>执行机</label><p role="alert" class="machine-error"></p><footer><button type="button" data-close-machine>取消</button><button type="submit" class="primary">保存</button></footer></form>`;
  dialog.querySelector('[data-close-machine]').onclick=()=>dialog.close();
  dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const f=event.target,button=f.querySelector('[type=submit]');button.disabled=true;
    const capabilities=['supervisor','executor'].filter(c=>f.elements[c].checked);
    try{
      if(!capabilities.length)throw new Error('请至少选择一种能力。');
      await api(a?`/api/agents/${encodeURIComponent(a.agentId)}`:'/api/agents',{method:a?'PUT':'POST',body:JSON.stringify({ip:f.elements.ip.value.trim(),port:Number(f.elements.port.value),groupId:f.elements.groupId.value,capabilities,enabled:a?.enabled??true})});
      dialog.close();await renderMachines();toast('已保存，请检测连接。');
    }catch(error){f.querySelector('.machine-error').textContent=error.message}finally{button.disabled=false}
  };dialog.showModal();
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-machine-action]');if(!button)return;
  const action=button.dataset.machineAction,id=button.dataset.id,a=state.agents.find(x=>x.agentId===id);
  button.disabled=true;
  try{
    if(action==='add'||action==='edit'){openMachine(a);return}
    if(action==='refresh'){await renderMachines();return}
    if(action==='group-add'||action==='group-edit'){
      openMachineGroup(machineGroups.find(g=>g.id===id));return;
    }else if(action==='group-delete'){
      if(!confirm('删除这个空分组？'))return;
      await api(`/api/agent-groups/${encodeURIComponent(id)}`,{method:'DELETE'});
    }else if(action==='test'){
      button.textContent='正在检测…';
      const result=await api(`/api/agents/${encodeURIComponent(id)}/test`,{method:'POST',body:'{}'});toast(result.message);
    }else if(action==='toggle'){
      await api(`/api/agents/${encodeURIComponent(id)}`,{method:'PUT',body:JSON.stringify({...machineBody(a),enabled:!a.enabled})});
    }
    await renderMachines();
  }catch(error){toast(error.message)}finally{button.disabled=false;if(action==='test')button.textContent='检测连接'}
});
