/* Skill 管理：只访问配置中心，安装结果与脚本可运行性分别展示。 */
const skillView={packages:[],machines:[],groups:[],batches:[],inventory:[],machineId:"",packageId:"",groupId:"",selected:new Set(),checked:new Set(),tab:'library',query:'',deployGroup:'',uploadGroup:'',enabled:false,busy:false,polling:false};
function skillScope(path,group=skillView.groupId){return path+(group?`?groupId=${encodeURIComponent(group)}`:'')}
function resetSkillSelection(){
  skillView.selected.clear();skillView.checked.clear();skillView.packageId='';skillView.machineId='';skillView.query='';
  for(const id of ['skillUploadDialog','skillDeployDialog','skillAssignDialog','skillDetailDialog'])document.querySelector(`#${id}`)?.close();
}
function skillDialog(id,html){
  let dialog=document.querySelector(`#${id}`);
  if(!dialog){dialog=document.createElement('dialog');dialog.id=id;document.body.append(dialog)}
  dialog.innerHTML=`<div class="skill-upload-dialog">${html}<p class="skill-dialog-error" role="status"></p><footer><button data-skill="close">关闭</button></footer></div>`;
  dialog.oncancel=e=>{if(skillView.busy)e.preventDefault()};dialog.showModal();return dialog;
}
function skillRequestId(){
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function skillPending(key,payload){
  let old;try{old=JSON.parse(sessionStorage.getItem(key)||"null")}catch{}
  if(old&&JSON.stringify(old.payload)===JSON.stringify(payload))return old.requestId;
  const requestId=skillRequestId();sessionStorage.setItem(key,JSON.stringify({payload,requestId}));return requestId;
}
function skillMessage(text){const node=document.querySelector("#skillMessage");if(node)node.textContent=text}
function savedSkillDeployment(){
  try{const value=JSON.parse(sessionStorage.getItem('skillPending')||'null');const p=value?.payload;
    return typeof p?.groupId==='string'&&typeof p.packageId==='string'&&Array.isArray(p.agentIds)&&p.agentIds.every(id=>typeof id==='string')?value:null;
  }catch{return null}
}
function openSkillUpload(){
  if(!concreteGroup())return chooseGroup(()=>openSkillUpload());
  skillView.uploadGroup=concreteGroup();
  let dialog=document.querySelector('#skillUploadDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='skillUploadDialog';document.body.append(dialog)}
  dialog.innerHTML=`<div class="skill-upload-dialog"><h2>上传 Skill</h2><p>上传到：${esc(groupName(skillView.uploadGroup))}</p><label class="skill-file-drop" for="skillZip"><span class="skill-upload-symbol">↑</span><strong id="skillFileName">点击选择 ZIP 文件</strong><small>最大 20 MiB</small><input id="skillZip" type="file" accept=".zip,application/zip" aria-label="选择 Skill ZIP"></label><p id="skillUploadError" role="status" aria-live="polite"></p><footer><button data-skill-upload-close>取消</button><button data-skill="upload" class="primary">上传</button></footer></div>`;
  dialog.querySelector('[data-skill-upload-close]').onclick=()=>{if(!skillView.busy)dialog.close()};
  dialog.oncancel=event=>{if(skillView.busy)event.preventDefault()};
  dialog.querySelector('#skillZip').onchange=event=>{
    dialog.querySelector('#skillFileName').textContent=event.target.files[0]?.name||'点击选择 ZIP 文件';
    dialog.querySelector('#skillUploadError').textContent='';
  };
  dialog.showModal();
}
async function renderSkills(){
  if(state.page!=="skills")return;
  const version=pageRenderVersion;
  if(skillView.groupId!==groupState.selected)resetSkillSelection();
  skillView.groupId=groupState.selected;
  $(".toolbar").classList.add("hidden");
  $("#content").className="content skill-content";
  $("#content").innerHTML='<section class="settings-panel" role="status">正在加载 Skill…</section>';
  try{
    const [packages,machines,batches,inventory]=await Promise.all([api(skillScope("/api/skills")),api(skillScope("/api/skills/machines")),api(skillScope("/api/skill-deployments")),api(skillScope("/api/skills/inventory")),loadGatewayReady()]);
    if(state.page!=="skills"||version!==pageRenderVersion)return;
    skillView.packages=packages.skills;skillView.machines=machines.agents;skillView.groups=machines.groups;skillView.batches=batches.deployments;skillView.inventory=inventory.items||[];skillView.enabled=packages.enabled;
    const pending=savedSkillDeployment();
    if(!skillView.packageId&&pending&&(!skillView.groupId||pending.payload.groupId===skillView.groupId))skillView.packageId=pending.payload.packageId;
    if(skillView.packageId&&!skillView.packages.some(p=>p.id===skillView.packageId)){
      const chosen=await api(`/api/skills/${encodeURIComponent(skillView.packageId)}`);
      if(state.page!=="skills"||version!==pageRenderVersion)return;
      if(!skillView.groupId||(chosen.groups||[]).some(g=>g.id===skillView.groupId))skillView.packages.unshift(chosen);else skillView.packageId='';
    }
    if(!skillView.packageId)skillView.packageId=skillView.packages[0]?.id||"";
    renderSkillWorkspace();
    if(pending&&skillView.packageId===pending.payload.packageId)skillMessage('上次下发结果尚未确认，可打开该 Skill 的下发窗口复用请求。');

  }catch(error){if(state.page==="skills"&&version===pageRenderVersion)$("#content").innerHTML=`<section class="settings-panel" role="alert">${esc(error.message)}<button data-skill="reload">重新加载</button></section>`}
}
function renderSkillWorkspace(){
  const tab=skillView.tab;
  $('#content').innerHTML=`<section class="settings-panel"><div class="skill-tabs" role="tablist" aria-label="Skill 管理视图">${[['library','Skill 库'],['machines','执行机'],['history','下发记录']].map(([id,name])=>`<button role="tab" aria-selected="${id===tab}" data-skill="tab" data-id="${id}" class="${id===tab?'active':''}">${name}</button>`).join('')}</div>
  ${!skillView.enabled?'<p class="skill-warning">尚未启用下发，请维护人员配置中央包存储目录。</p>':''}
  ${tab==='library'?`<div class="skill-library-toolbar"><input id="skillSearch" type="search" placeholder="搜索 Skill 名称或说明" aria-label="搜索 Skill" value="${esc(skillView.query)}"><span id="skillCount"></span><button data-skill="open-assign">加入分组</button><button data-skill="open-upload" class="primary" ${skillView.enabled?'':'disabled'}>＋ 上传</button></div><div id="skillPackages" class="skill-package-list"></div>`:tab==='machines'?'<div class="skill-inventory-layout"><nav id="skillMachineNav" aria-label="查看执行机 Skill"></nav><div id="skillInventory"></div></div>':'<p class="skill-empty">最近 100 个批次 · 按下发时分组记录</p><div id="skillHistory"></div>'}</section><p id="skillMessage" role="status" aria-live="polite"></p>`;
  if(tab==='library')renderSkillPackages();else if(tab==='machines')renderSkillInventory();else renderSkillHistory();
}
function renderSkillDetails(){
  const p=skillView.packages.find(p=>p.id===skillView.packageId);
  $("#skillDetails").innerHTML=p?`<p>${p.fileCount} 个文件 · ${(p.size/1024).toFixed(1)} KiB</p><p>${p.hasScripts?"包含脚本，运行环境未验证":"提示词与资源包"}</p>`:"<p>暂无 Skill 包。</p>";
}
function renderSkillPackages(){
  const query=skillView.query.trim().toLowerCase(),rows=skillView.packages.filter(p=>`${p.name} ${p.description}`.toLowerCase().includes(query));
  $('#skillCount').textContent=`${rows.length} 个 Skill · 已选 ${skillView.checked.size}`;
  $('[data-skill="open-assign"]').disabled=!skillView.checked.size;
  $("#skillPackages").innerHTML=rows.length?rows.map(p=>`<article class="skill-package ${p.id===skillView.packageId?'active':''}"><div class="skill-card-title"><input type="checkbox" data-skill-package="${esc(p.id)}" aria-label="选择 ${esc(p.name)}" ${skillView.checked.has(p.id)?'checked':''}><strong>${esc(p.name)}</strong></div><span>${esc(p.description)}</span><small>${p.hasScripts?'含脚本':'提示词与资源'} · ${esc(p.id.slice(0,12))}</small><small>${(p.groups||[]).length?p.groups.map(g=>esc(g.name)).join(' · '):'未归组'}</small><div class="actions"><button data-skill="package" data-id="${esc(p.id)}">详情</button><button data-skill="open-deploy" data-id="${esc(p.id)}" ${skillView.enabled?'':'disabled'}>下发</button></div></article>`).join(''):`<p class="skill-empty">${query?'没有匹配的 Skill，请调整搜索。':'暂无 Skill，可上传 ZIP 或从全部分组中加入已有 Skill。'}</p>`;
}
function openSkillAssign(){
  if(!skillView.checked.size)return skillMessage('请先勾选 Skill。');
  skillDialog('skillAssignDialog',`<h2>加入分组</h2><p>为 ${skillView.checked.size} 个 Skill 添加归属，保留已有分组。</p><label>目标分组<select id="skillAssignGroup">${groupOptions(concreteGroup())}</select></label><button data-skill="assign" class="primary">加入所选分组</button>`);
}
function openSkillDeploy(id){
  const p=skillView.packages.find(p=>p.id===id);if(!p)return;
  if(!(p.groups||[]).length){skillView.checked=new Set([id]);openSkillAssign();return}
  skillView.packageId=id;skillView.deployGroup=skillView.groupId||p.groups[0].id;skillView.selected.clear();
  const pending=savedSkillDeployment();
  if(pending?.payload.packageId===id&&p.groups.some(g=>g.id===pending.payload.groupId)&&(!skillView.groupId||skillView.groupId===pending.payload.groupId)){
    skillView.deployGroup=pending.payload.groupId;skillView.selected=new Set(pending.payload.agentIds);
  }
  skillDialog('skillDeployDialog',`<h2>下发 ${esc(p.name)}</h2><label>所属分组<select id="skillDeployGroup" ${skillView.groupId?'disabled':''}>${p.groups.filter(g=>!skillView.groupId||g.id===skillView.groupId).map(g=>`<option value="${esc(g.id)}" ${g.id===skillView.deployGroup?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label><div id="skillMachines" class="skill-machines"></div><p>仅下发到选中的本组执行机。重复提交相同选择会复用未确认的请求。</p><button data-skill="deploy" class="primary">下发到选中执行机</button>`);
  renderSkillMachines();
}
function renderSkillInventory(){
  const machines=skillView.machines.filter(m=>(m.capabilities||[]).includes('executor')&&(!skillView.groupId||m.groupId===skillView.groupId));
  if(!machines.some(m=>m.agentId===skillView.machineId))skillView.machineId=machines[0]?.agentId||'';
  $("#skillMachineNav").innerHTML=machines.map(m=>{
    const count=skillView.inventory.filter(i=>i.agentId===m.agentId&&i.state==='completed').length;
    return `<button class="skill-machine-tab ${m.agentId===skillView.machineId?'active':''}" data-skill="machine" data-id="${esc(m.agentId)}" aria-pressed="${m.agentId===skillView.machineId}"><strong>${esc(m.name)}:${m.port}</strong><small>${count} 个已安装</small></button>`;
  }).join('')||'<p class="skill-empty">此分组暂无执行机。</p>';
  const machine=machines.find(m=>m.agentId===skillView.machineId);
  const items=skillView.inventory.filter(i=>i.agentId===skillView.machineId);
  const labels={queued:'等待安装',running:'正在处理',completed:'已安装并识别',failed:'安装或检测未完成'};
  $("#skillInventory").innerHTML=machine?`<div class="skill-inventory-heading"><small>平台安装记录 · ${items.length} 项</small></div>${items.length?items.map(i=>`<article class="skill-installed"><div class="skill-installed-info"><div class="skill-installed-title"><strong>${esc(i.name)}</strong><span class="badge">${labels[i.state]||'状态未知'}</span></div><small>${i.completedFiles}/${i.fileCount} 个文件已核对 · 最近更新 ${time(i.updatedAt)}</small>${!(i.groups||[]).some(g=>g.id===machine.groupId)?'<small>未收录到当前机器分组</small>':''}${i.hasScripts?'<small>脚本运行环境未验证</small>':''}${i.error?`<p class="skill-warning">${esc(i.error)}</p>`:''}</div><div class="actions">${i.state==='failed'&&i.canRetry?`<button data-skill="retry" data-task="${esc(i.taskId)}">重试</button>`:''}<button data-skill="check" data-task="${esc(i.taskId)}" ${['queued','running'].includes(i.state)?'disabled':''}>重新检测</button>${i.state==='completed'?`<button data-skill="copy" data-name="${esc(i.name)}">复制引用</button>`:''}</div></article>`).join(''):'<p class="skill-empty">这台执行机暂无平台下发的 Skill。请到 Skill 库页签选择并下发。</p>'}`:'<p class="skill-empty">登记执行机后可查看安装情况。</p>';
}
function renderSkillMachines(){
  const machines=skillView.machines.filter(m=>m.groupId===skillView.deployGroup&&(m.capabilities||[]).includes('executor'));
  $("#skillMachines").innerHTML=machines.length?machines.map(m=>`<label class="skill-machine"><input type="checkbox" data-skill-machine="${esc(m.agentId)}" ${m.eligible?"":"disabled"} ${skillView.selected.has(m.agentId)&&m.eligible?"checked":""}><span><strong>${esc(m.name)}:${m.port}</strong><small>${esc(m.eligible?"可下发":m.error)}</small>${m.eligible?`<small>安装根目录：${esc(m.root)}</small>`:""}</span></label>`).join(""):"<p>此分组暂无机器。</p>";
}
function renderSkillHistory(){
  const labels={queued:"等待安装",running:"正在处理",completed:"已安装并识别",failed:"未完成"};
  $("#skillHistory").innerHTML=skillView.batches.length?skillView.batches.map(b=>`<article class="skill-batch"><h3>${esc(b.name)} <small>${esc(b.groupName||"历史未归组")} · ${time(b.createdAt)}</small></h3><p>${b.hasScripts?"包含脚本，运行环境未验证":"提示词与资源包"}</p>${b.tasks.map(t=>{
    const m=skillView.machines.find(m=>m.agentId===t.agent_id);const active=["queued","running"].includes(t.state);
    return `<div class="skill-task"><div><strong>${esc(m?`${m.name}:${m.port}`:"执行机不在当前分组")}</strong><span class="badge">${esc(labels[t.state]||"状态未知")}</span><small>${t.completedFiles}/${b.fileCount} 个文件已核对 · 尝试 ${t.attempts} 次</small>${t.error?`<p class="skill-warning">${esc(t.error)}</p>`:""}</div><div class="actions">${t.state==="failed"&&t.canRetry?`<button data-skill="retry" data-task="${esc(t.id)}" ${skillView.busy?"disabled":""}>重试</button>`:""}<button data-skill="check" data-task="${esc(t.id)}" ${active||skillView.busy?"disabled":""}>重新检测</button>${t.state==="completed"?`<button data-skill="copy" data-name="${esc(b.name)}">复制引用</button>`:""}</div></div>`;
  }).join("")}</article>`).join(""):"<p>暂无下发记录。上传 Skill 后选择执行机下发。</p>";
}
async function pollSkillHistory(){
  if(state.page!=="skills"||document.hidden||skillView.busy||skillView.polling||skillView.tab==='library')return;
  if(skillView.groupId!==groupState.selected||!document.querySelector(skillView.tab==='history'?'#skillHistory':'#skillInventory'))return;
  skillView.polling=true;const version=pageRenderVersion;
  const tab=skillView.tab;
  try{const value=await api(skillScope(tab==='history'?'/api/skill-deployments':'/api/skills/inventory'));if(state.page==="skills"&&version===pageRenderVersion&&tab===skillView.tab){
    if(tab==='history'&&JSON.stringify(skillView.batches)!==JSON.stringify(value.deployments)){skillView.batches=value.deployments;renderSkillHistory()}
    if(tab==='machines'&&JSON.stringify(skillView.inventory)!==JSON.stringify(value.items||[])){skillView.inventory=value.items||[];renderSkillInventory()}
  }}
  catch(error){if(state.page==="skills"&&version===pageRenderVersion&&tab===skillView.tab)skillMessage(`进度刷新失败：${error.message}`)}finally{skillView.polling=false}
}
document.addEventListener("change",event=>{
  if(event.target.id==="skillDeployGroup"){skillView.deployGroup=event.target.value;skillView.selected.clear();renderSkillMachines()}
  const key=event.target.dataset.skillPackage;if(key){if(event.target.checked)skillView.checked.add(key);else skillView.checked.delete(key);renderSkillPackages()}
  const id=event.target.dataset.skillMachine;if(id){if(event.target.checked)skillView.selected.add(id);else skillView.selected.delete(id)}
});
document.addEventListener('input',event=>{if(event.target.id==='skillSearch'){skillView.query=event.target.value;renderSkillPackages()}});
document.addEventListener("click",async event=>{
  const button=event.target.closest("[data-skill]");if(!button||skillView.busy)return;
  const action=button.dataset.skill;if(action==="open-upload"){openSkillUpload();return}if(action==="reload"){await renderSkills();return}
  if(action==='close'){button.closest('dialog').close();return}
  if(action==='tab'){skillView.tab=button.dataset.id;renderSkillWorkspace();await pollSkillHistory();return}
  if(action==='open-assign'){openSkillAssign();return}
  if(action==='open-deploy'){openSkillDeploy(button.dataset.id);return}
  if(action==="package"){skillView.packageId=button.dataset.id;const p=skillView.packages.find(p=>p.id===skillView.packageId);skillDialog('skillDetailDialog',`<h2>${esc(p.name)}</h2><p>${esc(p.description)}</p><div id="skillDetails"></div>`);renderSkillDetails();return}
  if(action==="machine"){skillView.machineId=button.dataset.id;renderSkillInventory();return}
  if(action==="copy"){
    const text=`$${button.dataset.name}`;
    try{await navigator.clipboard.writeText(text);skillMessage(`已复制 ${text}，可粘贴到 SOP 执行要求。`)}
    catch{skillMessage(`请手动复制到 SOP 执行要求：${text}`)}return;
  }
  const version=pageRenderVersion,current=()=>state.page==='skills'&&version===pageRenderVersion;
  skillView.busy=true;const controls=[...document.querySelectorAll('#content button,#content input,#content select,dialog[id^="skill"] button,dialog[id^="skill"] input,dialog[id^="skill"] select')];controls.forEach(c=>{c.dataset.skillDisabled=String(c.disabled);c.disabled=true});
  try{
    if(action==="upload"){
      const file=$("#skillZip").files[0];if(!file||!file.name.toLowerCase().endsWith(".zip"))throw new Error("请选择 ZIP 文件。");
      if(file.size>20*1024*1024)throw new Error("ZIP 不能超过 20 MiB。");
      skillMessage("正在上传并校验…");$("#skillUploadError").textContent="正在上传并校验…";
      const p=await api(skillScope("/api/skills",skillView.uploadGroup),{method:"POST",headers:{"Content-Type":"application/zip"},body:file});
      if(current()){skillView.packageId=p.id;$('#skillUploadDialog').close();await renderSkills();if(current()){skillMessage('上传校验通过，已加入当前组。');await loadGroups();if(current())renderGroupFilter()}}
    }else if(action==='assign'){
      const groupId=$('#skillAssignGroup').value;if(!groupId)throw new Error('请选择目标分组。');
      await api('/api/skills/groups/assign',{method:'POST',body:JSON.stringify({groupId,packageIds:[...skillView.checked]})});
      if(current()){$('#skillAssignDialog').close();skillView.checked.clear();await renderSkills();if(current()){skillMessage('已加入所选分组，原有归属保持不变。');await loadGroups();if(current())renderGroupFilter()}}
    }else if(action==="deploy"){
      const agentIds=[...skillView.selected].filter(id=>skillView.machines.some(m=>m.agentId===id&&m.eligible&&m.groupId===skillView.deployGroup)).sort();
      if(!skillView.packageId||!agentIds.length)throw new Error("请选择 Skill 和至少一台允许安装的执行机。");
      const payload={groupId:skillView.deployGroup,packageId:skillView.packageId,agentIds};const requestId=skillPending("skillPending",payload);
      await api("/api/skill-deployments",{method:"POST",body:JSON.stringify({...payload,requestId})});
      sessionStorage.removeItem("skillPending");if(current()){$('#skillDeployDialog').close();skillView.tab='history';renderSkillWorkspace();skillMessage('下发请求已接收。')}
    }else{
      const key=`skillAction:${button.dataset.task}:${action}`;const requestId=skillPending(key,{});
      await api(`/api/skill-deployment-tasks/${button.dataset.task}/${action}`,{method:"POST",body:JSON.stringify({requestId})});
      sessionStorage.removeItem(key);if(current())skillMessage("操作已接收，正在核对远程文件。");
    }
  }catch(error){if(current()){skillMessage(error.message);if(action==="upload")$("#skillUploadError").textContent=error.message;const errorNode=document.querySelector('dialog[open] .skill-dialog-error');if(errorNode)errorNode.textContent=error.message}}finally{
    skillView.busy=false;controls.forEach(c=>{c.disabled=c.dataset.skillDisabled==="true";delete c.dataset.skillDisabled});await pollSkillHistory();
  }
});
setInterval(pollSkillHistory,3000);
