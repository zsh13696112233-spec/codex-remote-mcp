/* MCP 安装只访问配置中心；提交编号保留到确定响应。 */
const mcpView={tab:'library',machineId:'',expanded:new Set(),packages:[],machines:[],items:[],batches:[],busy:false,polling:false,version:0};
const mcpLabels={queued:'等待安装',transferring:'正在下发',installing:'正在安装',registering:'正在注册',verifying:'正在验证',completed:'安装成功',needs_configuration:'待配置',unsupported:'不支持的安装包',failed:'安装失败',review:'待核对'};
function mcpScope(path){const group=concreteGroup();return path+(group?'?groupId='+encodeURIComponent(group):'')}
function mcpMachineReasons(machine){
  const reasons=[];
  if(!machine.enabled)reasons.push('机器未启用');
  if(machine.testStatus!=='passed')reasons.push('连接尚未检测通过');
  if(!machine.mcpInstallation?.enabled)reasons.push('未授权 MCP 安装');
  return reasons;
}
function mcpMachineOption(machine){
  const reasons=mcpMachineReasons(machine);
  return `<label class="skill-machine"><input type="checkbox" data-mcp-machine="${esc(machine.agentId)}" ${reasons.length?'disabled':''}><span><strong>${esc(machine.name)}:${esc(machine.port)}</strong><small>${esc(reasons.join('；')||'可选择安装')}</small></span></label>`;
}
async function renderMcps(){
  if(state.page!=='mcps')return;
  const version=++mcpView.version;
  $(".toolbar").classList.add('hidden');
  $('#content').className='content skill-content mcp-content';
  $('#content').innerHTML='<section class="settings-panel">正在加载 MCP…</section>';
  try{
    const [packages,machines,inventory,batches]=await Promise.all([api(mcpScope('/api/mcp-packages')),api(mcpScope('/api/mcp-packages/machines')),api(mcpScope('/api/mcp-packages/inventory')),api(mcpScope('/api/mcp-deployments'))]);
    if(state.page!=='mcps'||version!==mcpView.version)return;
    Object.assign(mcpView,{packages:packages.packages,machines:machines.agents,groups:machines.groups||[],items:inventory.items,batches:batches.deployments,enabled:packages.enabled});
    drawMcps();
  }catch(error){if(state.page==='mcps'&&version===mcpView.version)$('#content').innerHTML=`<section class="settings-panel">${esc(error.message)}<button data-mcp="reload">重新加载</button></section>`}
}
function drawMcps(){
  let content='';
  if(mcpView.tab==='library')content=`<div class="mcp-toolbar"><span>共 ${mcpView.packages.length} 个安装包</span><button class="primary" data-mcp="upload" ${mcpView.enabled?'':'disabled'}>＋ 上传 ZIP</button></div>${mcpView.enabled?'':'<p class="skill-warning">请先配置中央 MCP 包存储目录。</p>'}<div class="mcp-card-grid">${mcpView.packages.map(p=>`<article class="mcp-card"><div class="mcp-card-heading"><h3>${esc(p.name)}</h3></div><p>${p.fileCount} 个文件</p><div class="mcp-package-groups">${(p.groups||[]).map(id=>'<span class="group-tag">'+esc((mcpView.groups||[]).find(g=>g.id===id)?.name||'所属分组')+'</span>').join('')}</div><footer><button data-mcp="assign" data-id="${esc(p.id)}">加入分组</button><button data-mcp="deploy" data-id="${esc(p.id)}">下发</button></footer></article>`).join('')||'<p class="skill-empty">暂无安装包，上传 ZIP 后可向执行机下发。</p>'}</div>`;
  if(mcpView.tab==='machines')content='<div class="mcp-inventory-layout"><nav id="mcpMachineNav" aria-label="选择执行机"></nav><div id="mcpInventory"></div></div>';
  if(mcpView.tab==='history')content=mcpView.batches.map(b=>`<article class="skill-installed"><strong>${esc(b.group_name)}</strong><span>${esc(b.created_at)}</span><button data-mcp="batch" data-id="${esc(b.id)}">查看结果</button></article>`).join('')||'<p>暂无下发记录。</p>';
  $('#content').innerHTML=`<section class="settings-panel"><div class="skill-tabs">${[['library','MCP / CLI 包库'],['machines','执行机'],['history','下发记录']].map(([id,label])=>`<button data-mcp="tab" data-id="${id}" class="${mcpView.tab===id?'active':''}">${label}</button>`).join('')}<button data-mcp="reload">刷新</button></div><p id="mcpPollStatus" role="status"></p>${content}</section>`;
  drawMcpInventory();
}
function mcpTaskRows(items){return items.map(t=>`<article class="skill-installed"><div><strong>${esc(mcpPackageName(t.package_id))}</strong><small>${esc(mcpView.machines.find(m=>m.agentId===t.agent_id)?.name||t.agent_id)}</small><span class="badge">${mcpLabels[t.state]||'状态未知'}</span>${mcpKindBadge(t.installation)}<p>${esc(t.message)}</p>${mcpDiagnosticRows(t.diagnostics)}${t.installation?.programPath?`<p>程序：${esc(t.installation.programPath)} · ${t.installation.programVerified?'已验证':'待验证'}</p>`:''}${t.installation?.skillPath?`<p>Skill：${esc(t.installation.skillPath)} · ${t.installation.skillVerified?'已识别':'待检查'}</p>`:''}</div>${['failed','needs_configuration','unsupported'].includes(t.state)?`<button data-mcp="retry" data-id="${esc(t.id)}">重试</button>`:''}${!['queued','transferring','installing','registering','verifying'].includes(t.state)?`<button data-mcp="check" data-id="${esc(t.id)}">重新检测</button>`:''}</article>`).join('')||'<p>暂无平台安装记录。</p>'}
function mcpPackageName(id){return mcpView.packages.find(p=>p.id===id)?.name||'安装包（'+String(id||'未知').slice(0,12)+'）'}
function mcpCurrentItems(agentId){
  const latest=new Map();
  mcpView.items.filter(t=>t.agent_id===agentId).forEach(t=>{
    const previous=latest.get(t.package_id);
    if(!previous||String(t.created_at)>String(previous.created_at))latest.set(t.package_id,t);
  });
  return [...latest.values()];
}
function drawMcpInventory(){
  const node=$('#mcpInventory'),nav=$('#mcpMachineNav');if(!node)return;
  const machines=mcpView.machines.filter(m=>(m.capabilities||[]).includes('executor'));
  if(!machines.some(m=>m.agentId===mcpView.machineId))mcpView.machineId=machines[0]?.agentId||'';
  const navigation=machines.map(m=>`<button class="skill-machine-tab ${m.agentId===mcpView.machineId?'active':''}" data-mcp="machine" data-id="${esc(m.agentId)}" aria-pressed="${m.agentId===mcpView.machineId}"><strong>${esc(m.name)}:${esc(m.port)}</strong><small>${mcpCurrentItems(m.agentId).length} 个安装包</small></button>`).join('')||'<p class="skill-empty">此分组暂无执行机。</p>';
  if(nav&&nav.innerHTML!==navigation)nav.innerHTML=navigation;
  const machine=machines.find(m=>m.agentId===mcpView.machineId),items=mcpCurrentItems(mcpView.machineId);
  const html=machine?`<div class="skill-inventory-heading"><h2>${esc(machine.name)}</h2><small>平台安装记录 · ${items.length} 项 · ${machine.mcpInstallation?.enabled?'已授权安装':'未授权安装'}</small></div>${items.map(t=>`<article class="mcp-install-row"><div class="mcp-install-summary"><div><div class="skill-installed-title"><strong>${esc(mcpPackageName(t.package_id))}</strong><span class="badge">${mcpLabels[t.state]||'状态未知'}</span>${mcpKindBadge(t.installation)}</div><small>最近检测：${esc(t.diagnostics?.checkedAt||'尚未检测')}</small></div><div class="actions">${['failed','needs_configuration','unsupported'].includes(t.state)?`<button data-mcp="retry" data-id="${esc(t.id)}">重试</button>`:''}${!['queued','transferring','installing','registering','verifying'].includes(t.state)?`<button data-mcp="check" data-id="${esc(t.id)}">重新检测</button>`:''}</div></div><details data-mcp-details="${esc(t.id)}" ${mcpView.expanded.has(t.id)?'open':''}><summary>检测详情</summary><p>${esc(t.message)}</p>${mcpDiagnosticRows(t.diagnostics)}${t.installation?.programPath?`<p>程序：${esc(t.installation.programPath)} · ${t.installation.programVerified?'已验证':'待验证'}</p>`:''}${t.installation?.skillPath?`<p>Skill：${esc(t.installation.skillPath)} · ${t.installation.skillVerified?'已识别':'待检查'}</p>`:''}</details></article>`).join('')||'<p class="skill-empty">这台执行机暂无平台安装记录，请到 MCP / CLI 包库选择并下发。</p>'}`:'<p class="skill-empty">登记执行机后可查看安装情况。</p>';
  // Compare generated markup, since browser serialization changes boolean attributes.
  if(node.mcpMarkup!==html){node.innerHTML=html;node.mcpMarkup=html;}
}
document.addEventListener('toggle',event=>{
  const id=event.target.dataset?.mcpDetails;if(!id)return;
  if(event.target.open)mcpView.expanded.add(id);else mcpView.expanded.delete(id);
},true);
function mcpKindBadge(installation){
  if(!installation?.programPath)return '';
  const label=installation.kind==='cli'?'CLI + Skill':installation.skillPath?'MCP + Skill':'MCP';
  return `<span class="badge">${label}</span>`;
}
function mcpDiagnosticRows(value){
  if(!value)return '<p>尚无详细检测记录，请点击“重新检测”。</p>';
  if(value.kind==='cli')return `<div class="mcp-diagnostics"><p>最近检测：${esc(value.checkedAt)}${value.pending?' · 检测中':''}</p><p>检测阶段：${esc(value.stage)}</p><p>${esc(value.reason)}</p></div>`;
  const found=value.found===true?'已找到':value.found===false?'未找到':'尚未确认';
  const count=Number.isInteger(value.toolCount)&&value.toolCount>=0?value.toolCount:'未知';
  return `<div class="mcp-diagnostics"><p>最近检测：${esc(value.checkedAt)}${value.pending?' · 检测中':''}</p><p>检测阶段：${esc(value.stage)}；服务：${found}；连接：${esc(value.connection)}；工具数量：${count}</p><p>${esc(value.reason)}</p></div>`;
}
function mcpDialog(html){
  let d=$('#mcpDialog');if(!d){d=document.createElement('dialog');d.id='mcpDialog';document.body.append(d)}
  d.innerHTML=`<div class="skill-upload-dialog">${html}<p role="alert"></p><footer><button data-mcp="close">关闭</button></footer></div>`;
  d.dataset.batchId='';d.oncancel=e=>{if(mcpView.busy)e.preventDefault()};d.showModal();return d;
}
async function mcpSubmit(path,payload,key){
  const requestId=skillPending(key,payload);
  const result=await api(path,{method:'POST',body:JSON.stringify({...payload,requestId})});sessionStorage.removeItem(key);return result;
}
document.addEventListener('click',async event=>{
  const b=event.target.closest('[data-mcp]');if(!b||mcpView.busy)return;
  const action=b.dataset.mcp,id=b.dataset.id;
  const version=mcpView.version;
  if(action==='close')return $('#mcpDialog').close();
  if(action==='tab'){mcpView.tab=id;return drawMcps()}
  if(action==='reload')return renderMcps();
  if(action==='machine'){mcpView.machineId=id;drawMcpInventory();return;}
  const group=concreteGroup();
  if(['upload','deploy','assign'].includes(action)&&!group)return chooseGroup(()=>document.querySelector(`[data-mcp="${action}"]${id?`[data-id="${id}"]`:''}`)?.click());
  if(action==='upload'){mcpDialog('<h2>上传 MCP / CLI ZIP</h2><input id="mcpZip" type="file" accept=".zip"><button data-mcp="save-upload">上传</button>');return}
  if(action==='deploy'){
    const machines=mcpView.machines.filter(m=>m.groupId===group&&m.capabilities.includes('executor'));
    mcpDialog(`<h2>选择执行机</h2><p>无法选择时，请到“机器管理”编辑对应机器，开启“允许安装 MCP”、填写安装目录并保存，再检测连接。返回后刷新 MCP 页面。</p><div class="skill-machines">${machines.map(mcpMachineOption).join('')||'<p>没有可用执行机。</p>'}</div><button data-mcp="save-deploy" data-id="${esc(id)}" ${machines.some(m=>!mcpMachineReasons(m).length)?'':'disabled'}>开始安装</button>`);return;
  }
  mcpView.busy=true;b.disabled=true;
  try{
    if(action==='save-upload'){
      const file=$('#mcpZip').files[0];if(!file)throw new Error('请选择 ZIP。');
      const response=await fetch(mcpScope('/api/mcp-packages'),{method:'POST',headers:{'Content-Type':'application/zip'},body:file});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'上传失败');$('#mcpDialog').close();
    }else if(action==='save-deploy'){
      const agentIds=[...document.querySelectorAll('[data-mcp-machine]:checked')].map(n=>n.dataset.mcpMachine);if(!agentIds.length)throw new Error('请选择执行机。');
      await mcpSubmit('/api/mcp-deployments',{groupId:group,packageId:id,agentIds},'mcpPending');$('#mcpDialog').close();mcpView.tab='machines';
    }else if(action==='assign')await api('/api/mcp-packages/groups/assign',{method:'POST',body:JSON.stringify({groupId:group,packageIds:[id]})});
    else if(action==='retry'||action==='check')await mcpSubmit(`/api/mcp-deployment-tasks/${encodeURIComponent(id)}/${action}`,{},'mcpAction:'+id+':'+action);
    else if(action==='batch'){const value=await api('/api/mcp-deployments/'+encodeURIComponent(id));if(state.page!=='mcps'||version!==mcpView.version)return;const d=mcpDialog('<h2>下发结果</h2><div id="mcpBatchResults">'+mcpTaskRows(value.tasks)+'</div>');d.dataset.batchId=id;return}
    await renderMcps();
  }catch(error){const d=$('#mcpDialog');if(d?.open)d.querySelector('[role=alert]').textContent=error.message;else toast(error.message)}
  finally{mcpView.busy=false;b.disabled=false}
});
setInterval(async()=>{
  const dialog=$('#mcpDialog'),batch=dialog?.open?dialog.dataset.batchId:'';
  if(state.page!=='mcps'||mcpView.busy||mcpView.polling||document.hidden||(dialog?.open&&!batch))return;
  const version=mcpView.version,scope=batch?'/api/mcp-deployments/'+encodeURIComponent(batch):mcpScope('/api/mcp-packages/inventory');
  mcpView.polling=true;
  try{const value=await api(scope);if(state.page==='mcps'&&version===mcpView.version){const status=$('#mcpPollStatus');if(status)status.textContent='';if(batch){if(dialog.open&&dialog.dataset.batchId===batch)$('#mcpBatchResults').innerHTML=mcpTaskRows(value.tasks)}else{mcpView.items=value.items;drawMcpInventory()}}}catch(error){if(state.page==='mcps'&&version===mcpView.version){const status=$('#mcpPollStatus');if(status)status.textContent='进度刷新失败，请稍后刷新。'}}finally{mcpView.polling=false}
},3000);
