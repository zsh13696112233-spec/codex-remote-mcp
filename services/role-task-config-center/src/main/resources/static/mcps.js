/* MCP 安装只访问配置中心；提交编号保留到确定响应。 */
const mcpView={tab:'library',packages:[],machines:[],items:[],batches:[],busy:false,polling:false,version:0};
const mcpLabels={queued:'等待安装',transferring:'正在下发',installing:'正在安装',registering:'正在注册',verifying:'正在验证',completed:'安装成功',needs_configuration:'待配置',unsupported:'不支持 MCP',failed:'安装失败',review:'待核对'};
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
  $('#search')?.closest('.toolbar').classList.add('hidden');
  $('#content').className='content skill-content';
  $('#content').innerHTML='<section class="settings-panel">正在加载 MCP…</section>';
  try{
    const [packages,machines,inventory,batches]=await Promise.all([api(mcpScope('/api/mcp-packages')),api(mcpScope('/api/mcp-packages/machines')),api(mcpScope('/api/mcp-packages/inventory')),api(mcpScope('/api/mcp-deployments'))]);
    if(state.page!=='mcps'||version!==mcpView.version)return;
    Object.assign(mcpView,{packages:packages.packages,machines:machines.agents,items:inventory.items,batches:batches.deployments,enabled:packages.enabled});
    drawMcps();
  }catch(error){if(state.page==='mcps'&&version===mcpView.version)$('#content').innerHTML=`<section class="settings-panel">${esc(error.message)}<button data-mcp="reload">重新加载</button></section>`}
}
function drawMcps(){
  let content='';
  if(mcpView.tab==='library')content=`<button data-mcp="upload" ${mcpView.enabled?'':'disabled'}>＋ 上传 ZIP</button>${mcpView.enabled?'':'<p>请先配置中央 MCP 包存储目录。</p>'}<div class="skill-package-list">${mcpView.packages.map(p=>`<article class="skill-installed"><div><strong>${esc(p.name)}</strong><small> ${p.fileCount} 个文件</small></div><button data-mcp="deploy" data-id="${esc(p.id)}">下发</button><button data-mcp="assign" data-id="${esc(p.id)}">加入当前分组</button></article>`).join('')||'<p>暂无 MCP 包。</p>'}</div>`;
  if(mcpView.tab==='machines')content='<div id="mcpInventory"></div>';
  if(mcpView.tab==='history')content=mcpView.batches.map(b=>`<article class="skill-installed"><strong>${esc(b.group_name)}</strong><span>${esc(b.created_at)}</span><button data-mcp="batch" data-id="${esc(b.id)}">查看结果</button></article>`).join('')||'<p>暂无下发记录。</p>';
  $('#content').innerHTML=`<section class="settings-panel"><div class="skill-tabs">${[['library','MCP 包库'],['machines','执行机'],['history','下发记录']].map(([id,label])=>`<button data-mcp="tab" data-id="${id}" class="${mcpView.tab===id?'active':''}">${label}</button>`).join('')}<button data-mcp="reload">刷新</button></div><p id="mcpPollStatus" role="status"></p>${content}</section>`;
  drawMcpInventory();
}
function mcpTaskRows(items){return items.map(t=>`<article class="skill-installed"><div><strong>${esc(mcpView.machines.find(m=>m.agentId===t.agent_id)?.name||t.agent_id)}</strong><span class="badge">${mcpLabels[t.state]||'状态未知'}</span><p>${esc(t.message)}</p>${mcpDiagnosticRows(t.diagnostics)}${t.installation?.programPath?`<p>程序：${esc(t.installation.programPath)} · ${t.installation.programVerified?'已验证':'待验证'}</p>`:''}${t.installation?.skillPath?`<p>Skill：${esc(t.installation.skillPath)} · ${t.installation.skillVerified?'已识别':'待检查'}</p>`:''}</div>${['failed','needs_configuration','unsupported'].includes(t.state)?`<button data-mcp="retry" data-id="${esc(t.id)}">重试</button>`:''}${!['queued','transferring','installing','registering','verifying'].includes(t.state)?`<button data-mcp="check" data-id="${esc(t.id)}">重新检测</button>`:''}</article>`).join('')||'<p>暂无平台安装记录。</p>'}
function drawMcpInventory(){const node=$('#mcpInventory');if(node)node.innerHTML=mcpTaskRows(mcpView.items)}
function mcpDiagnosticRows(value){
  if(!value)return '<p>尚无详细检测记录，请点击“重新检测”。</p>';
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
  const group=concreteGroup();
  if(['upload','deploy','assign'].includes(action)&&!group)return chooseGroup(()=>document.querySelector(`[data-mcp="${action}"]${id?`[data-id="${id}"]`:''}`)?.click());
  if(action==='upload'){mcpDialog('<h2>上传 MCP ZIP</h2><input id="mcpZip" type="file" accept=".zip"><button data-mcp="save-upload">上传</button>');return}
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
