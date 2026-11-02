let allocationRequest = 0;
function allocationHtml(value) {
  if (!value.allocationFresh) return '<p role="alert">调度状态暂时无法读取，请点击刷新重试。</p>';
  const allocation=value.allocation;
  if (!allocation) return '<p>此运行沿用原快照中的固定机器设置。</p>';
  const resources=value.resources||{};
  const resourceLabel={occupied:'任务占用中',review:'执行状态待核实，暂不接收新任务',released:'未占用机器'}[resources.state]||'状态未知';
  return `<dl><dt>监督机</dt><dd>${esc(allocation.supervisorAgentId ? (allocation.supervisorRemark?.trim()||'未填写备注') : '等待分配')}${allocation.supervisorDeleted?'（已删除）':''}${allocation.supervisorAgentId?`<br><small>编号：${esc(allocation.supervisorAgentId)}</small>`:''}</dd><dt>执行机</dt><dd>${esc(allocation.executorAgentId ? (allocation.executorRemark?.trim()||'未填写备注') : '等待分配')}${allocation.executorDeleted?'（已删除）':''}${allocation.executorAgentId?`<br><small>编号：${esc(allocation.executorAgentId)}</small>`:''}</dd><dt>工作目录</dt><dd>${esc(allocation.workingDirectory||'分配执行机后确定')}</dd><dt>分配时间</dt><dd>${esc(allocation.assignedAt?time(allocation.assignedAt):'尚未分配')}</dd><dt>占用状态</dt><dd>${resourceLabel}</dd></dl>${allocation.waitingReason?`<p>${esc(allocation.waitingReason)}</p>`:''}<p class="meta">全部步骤使用同一台执行机和目录。流程结束后，还须确认远端已停止才会释放机器。</p>`;
}
async function showAllocation(workflowId) {
  let dialog=document.querySelector('#allocationDialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='allocationDialog';document.body.append(dialog)}
  const request=++allocationRequest;
  dialog.innerHTML='<button type="button" class="dialog-close" data-dialog-close aria-label="关闭">×</button><h2>机器分配</h2><div data-allocation-body aria-live="polite">正在读取调度状态…</div><footer><button type="button" data-allocation-refresh>刷新</button><button type="button" data-dialog-close>关闭</button></footer>';
  const refresh=dialog.querySelector('[data-allocation-refresh]');refresh.disabled=true;
  refresh.onclick=()=>showAllocation(workflowId);
  if(!dialog.open)dialog.showModal();
  try {
    const value=await api(`/api/task-runs/${encodeURIComponent(workflowId)}`);
    if(request!==allocationRequest||!dialog.open)return;
    dialog.querySelector('[data-allocation-body]').innerHTML=allocationHtml(value);
  } catch(error) {
    if(request===allocationRequest&&dialog.open)dialog.querySelector('[data-allocation-body]').textContent='无法读取调度状态，请刷新重试。';
  } finally { if(request===allocationRequest)refresh.disabled=false }
}
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-allocation]');
  if(button)showAllocation(button.dataset.allocation);
});
