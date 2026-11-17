let allocationRequest = 0;
function allocationHtml(value) {
  if (!value.allocationFresh) return '<div class="allocation-message is-error" role="alert">调度状态暂时无法读取，请点击刷新重试。</div>';
  const allocation=value.allocation;
  if (!allocation) return '<div class="allocation-message">此运行沿用原快照中的固定机器设置。</div>';
  const resources=value.resources||{};
  const resourceState=['occupied','review','released'].includes(resources.state)?resources.state:'unknown';
  const resourceLabel={occupied:'任务占用中',review:'执行状态待核实，暂不接收新任务',released:'未占用机器',unknown:'状态未知'}[resourceState];
  const machine=(label,id,remark,deleted)=>`<section class="allocation-machine"><h3>${label}</h3><strong>${esc(id?(remark?.trim()||'未填写备注'):'等待分配')}${deleted?'（已删除）':''}</strong>${id?`<small>编号：${esc(id)}</small>`:'<small>分配后显示机器信息</small>'}</section>`;
  return `<div class="allocation-resource"><span>占用状态</span><span class="allocation-state ${resourceState}"><i aria-hidden="true"></i>${resourceLabel}</span></div>
    <div class="allocation-machines">${machine('监督机',allocation.supervisorAgentId,allocation.supervisorRemark,allocation.supervisorDeleted)}${machine('执行机',allocation.executorAgentId,allocation.executorRemark,allocation.executorDeleted)}</div>
    <dl class="allocation-facts"><div><dt>工作目录</dt><dd class="allocation-directory">${esc(allocation.workingDirectory||'分配执行机后确定')}</dd></div><div><dt>分配时间</dt><dd>${esc(allocation.assignedAt?time(allocation.assignedAt):'尚未分配')}</dd></div></dl>
    ${allocation.waitingReason?`<p class="allocation-waiting">${esc(allocation.waitingReason)}</p>`:''}
    <p class="allocation-note">全部步骤使用同一台执行机和目录。流程结束后，还须确认远端已停止才会释放机器。</p>`;
}
async function showAllocation(workflowId) {
  let dialog=document.querySelector('#allocationDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='allocationDialog';document.body.append(dialog)}
  dialog.setAttribute('aria-labelledby','allocationTitle');
  const request=++allocationRequest;
  dialog.innerHTML='<div class="allocation-panel"><header class="allocation-header"><h2 id="allocationTitle">机器分配</h2><button type="button" class="dialog-close" data-dialog-close aria-label="关闭机器分配" title="关闭">×</button></header><div class="allocation-body" data-allocation-body aria-live="polite"><div class="allocation-message" role="status">正在读取调度状态…</div></div><footer><button type="button" data-allocation-refresh>刷新状态</button><button type="button" class="primary" data-dialog-close>关闭</button></footer></div>';
  const refresh=dialog.querySelector('[data-allocation-refresh]');refresh.disabled=true;
  refresh.onclick=()=>showAllocation(workflowId);
  if(!dialog.open)dialog.showModal();
  try {
    const value=await api(`/api/task-runs/${encodeURIComponent(workflowId)}`);
    if(request!==allocationRequest||!dialog.open)return;
    dialog.querySelector('[data-allocation-body]').innerHTML=allocationHtml(value);
  } catch(error) {
    if(request===allocationRequest&&dialog.open)dialog.querySelector('[data-allocation-body]').innerHTML='<div class="allocation-message is-error" role="alert">无法读取调度状态，请刷新重试。</div>';
  } finally { if(request===allocationRequest)refresh.disabled=false }
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-allocation]');
  if(button)showAllocation(button.dataset.allocation);
});
