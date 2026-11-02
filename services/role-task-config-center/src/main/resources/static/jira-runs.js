let jiraRequest = 0;
let jiraBusy = false;
let jiraCursor = null;
const jiraPages = [];
let jiraNextCursor = null;
let jiraLoaded = false;
let jiraItems = [];
let jiraDetailId = null;

function jiraRunStatus(row) {
  if (row.outcome === 'no_task') return '无可领取工单';
  if (row.outcome === 'blocked') return '因阻断结束';
  if (row.status === 'running' && row.waitingFor) return {blocked:'遇阻，等待人工处理',finish:'等待确认结束',advance:'等待确认下一步',legacy:'等待进入下一步'}[row.waitingFor] || '等待确认';
  return {queued:'等待机器',running:'正在执行',cancelling:'正在停止',completed:'流程完成',failed:'执行中断或失败',cancelled:'已取消'}[row.status] || '状态未知';
}

const jiraDeliveryLabels = {submitted:'已提交（附凭证）',completed:'本步完成，未提交',blocked:'业务阻断',unknown:'交付结果待核实',not_applicable:'本步无交付操作'};
function jiraDeliverySummary(deliveries) {
  if (!deliveries?.length) return '尚无交付回执';
  return deliveries.some(d=>d.status==='submitted')?'已记录提交凭证':jiraDeliveryLabels[deliveries.at(-1).status]||'结果待核实';
}
function jiraClaimStatus(row) {
  return {waiting_claim:'等待领取',claimed:'已领取，正在下发',ready:'已下发',no_task:'无可领取工单'}[row.claimState]||'领取状态未知';
}
function jiraStatusKind(row) {
  if(row.outcome==='blocked'||row.status==='failed')return 'failed';
  if(row.waitingFor||row.status==='queued'||row.status==='cancelling')return 'waiting';
  if(row.outcome==='no_task'||row.status==='cancelled')return 'muted';
  return row.status==='completed'?'success':row.status==='running'?'running':'muted';
}
function jiraStopPending(row) {
  return ['occupied','review'].includes(row.resourceState)&&['completed','failed','cancelled'].includes(row.status);
}
function jiraRowHtml(row) {
  return `<td><strong>${esc(row.name||'历史任务')}</strong><small>${esc(groupName(row.groupId))}</small></td>
    <td><strong>${esc(row.issueKey||'尚未领取')}</strong></td>
    <td><span class="jira-status ${row.claimState==='ready'?'success':'muted'}">${esc(jiraClaimStatus(row))}</span></td>
    <td><span>监督机：${esc(row.supervisorAgentId?(row.supervisorRemark?.trim()||'未填写备注'):'等待分配')}</span><span>执行机：${esc(row.executorAgentId?(row.executorRemark?.trim()||'未填写备注'):'等待分配')}</span></td>
    <td><span class="jira-status ${jiraStatusKind(row)}">${esc(jiraRunStatus(row))}</span>${jiraStopPending(row)?'<small class="jira-warning">等待确认执行已停止</small>':''}${row.reason?'<small>原因见详情</small>':''}</td>
    <td><span>${esc(jiraDeliverySummary(row.deliveries))}</span>${row.deliveries?.length?`<small>${row.deliveries.length} 条回执 · 详情可查看</small>`:''}</td>
    <td class="jira-times"><span>开始 ${esc(time(row.createdAt))}</span>${row.finishedAt?`<span>结束 ${esc(time(row.finishedAt))}</span>`:''}</td>
    <td><div class="jira-row-actions"><a href="${esc(row.monitorUrl)}" target="_blank" rel="noopener">查看过程</a><button type="button" data-allocation="${esc(row.workflowId)}">机器分配</button><button type="button" data-jira-details="${esc(row.workflowId)}">详情</button></div></td>`;
}
function jiraRowsHtml(rows) {
  if(!rows.length)return '<div class="empty">暂无 Jira 领取记录。请先在 SOP 中启用监督机统一领取，再手动运行或由定时任务触发。</div>';
  return `<table class="jira-table"><thead><tr><th>任务 / 分组</th><th>Jira 工单</th><th>下发状态</th><th>监督机 / 执行机</th><th>运行状态</th><th>交付结果</th><th>时间</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr data-jira-run="${esc(row.workflowId)}">${jiraRowHtml(row)}</tr>`).join('')}</tbody></table>`;
}
function updateJiraRows(rows) {
  const container=$('#jiraRows'),body=container.querySelector('tbody');
  if(!body||!rows.length){
    const html=jiraRowsHtml(rows);
    if(container.jiraMarkup!==html){container.innerHTML=html;container.jiraMarkup=html}
    return;
  }
  container.jiraMarkup=null;
  const existing=new Map([...body.children].map(tr=>[tr.dataset.jiraRun,tr]));
  rows.forEach((row,index)=>{
    let tr=existing.get(row.workflowId);
    const fresh=document.createElement('tr');fresh.dataset.jiraRun=row.workflowId;fresh.innerHTML=jiraRowHtml(row);
    if(tr){
      [...fresh.children].forEach((cell,i)=>{if(tr.children[i].innerHTML!==cell.innerHTML)tr.children[i].innerHTML=cell.innerHTML});
      existing.delete(row.workflowId);
    }else tr=fresh;
    if(body.children[index]!==tr)body.insertBefore(tr,body.children[index]||null);
  });
  existing.forEach(tr=>tr.remove());
}
function jiraDetailHtml(row) {
  return `<p class="meta">${esc(row.name||'历史任务')} · ${esc(row.issueKey||'尚未领取')}</p><dl><dt>下发状态</dt><dd>${esc(jiraClaimStatus(row))}</dd><dt>运行状态</dt><dd>${esc(jiraRunStatus(row))}${jiraStopPending(row)?' · 等待确认执行已停止':''}</dd><dt>领取时间</dt><dd>${row.claimedAt?esc(time(row.claimedAt)):'尚未领取'}</dd><dt>开始时间</dt><dd>${esc(time(row.createdAt))}</dd><dt>结束时间</dt><dd>${row.finishedAt?esc(time(row.finishedAt)):'尚未结束'}</dd><dt>原因说明</dt><dd>${esc(row.reason||'暂无')}</dd></dl><h3>交付回执</h3>${row.deliveries?.length?row.deliveries.map(d=>`<article class="jira-receipt"><strong>${esc(jiraDeliveryLabels[d.status]||'结果待核实')}</strong><small>${esc(time(d.recordedAt))}</small><p>${esc(d.summary||'')}</p>${d.reference?`<p>凭证：${esc(d.reference)}</p>`:''}</article>`).join(''):'<p class="meta">尚无交付回执</p>'}`;
}
function updateJiraDetails() {
  const dialog=$('#jiraDetails'),row=jiraItems.find(item=>item.workflowId===jiraDetailId);
  if(!dialog?.open||!row)return;
  const body=dialog.querySelector('[data-jira-detail-body]'),html=jiraDetailHtml(row);
  if(body.jiraMarkup!==html){const top=dialog.scrollTop;body.innerHTML=html;body.jiraMarkup=html;dialog.scrollTop=top}
}
function openJiraDetails(id) {
  if(!jiraItems.some(row=>row.workflowId===id))return;
  let dialog=$('#jiraDetails');
  if(!dialog){
    dialog=document.createElement('dialog');dialog.id='jiraDetails';dialog.setAttribute('aria-labelledby','jiraDetailsTitle');
    dialog.innerHTML='<button type="button" class="dialog-close" data-dialog-close aria-label="关闭详情">×</button><h2 id="jiraDetailsTitle">任务下发详情</h2><div data-jira-detail-body></div><footer><button type="button" data-dialog-close>关闭</button></footer>';
    dialog.addEventListener('close',()=>{jiraDetailId=null});document.body.append(dialog);
  }
  jiraDetailId=id;dialog.showModal();updateJiraDetails();
}
async function renderJiraRuns() {
  $('#jiraDetails')?.close();jiraDetailId=null;
  jiraCursor=null;jiraNextCursor=null;jiraPages.length=0;jiraItems=[];jiraLoaded=false;
  $('#content').className='content run-catalog jira-catalog';
  $('.toolbar').classList.add('hidden');
  $('#content').innerHTML='<section class="run-catalog-panel"><div class="jira-toolbar"><p class="meta">按分组查看领取、机器分配和交付结果。流程完成不代表已提交。</p><button id="jiraRefresh">刷新</button></div><p id="jiraNotice" role="status">正在加载…</p><div id="jiraRows" class="jira-table-scroll"></div><footer class="jira-pagination"><span>每 10 秒自动更新</span><div class="actions"><button id="jiraPrevious" disabled>上一页</button><button id="jiraNext" disabled>下一页</button></div></footer></section>';
  $('#jiraRefresh').onclick=()=>loadJiraRuns();
  $('#jiraPrevious').onclick=()=>{if(jiraPages.length)loadJiraRuns({cursor:jiraPages.at(-1),direction:'previous'})};
  $('#jiraNext').onclick=()=>{if(jiraNextCursor!==null)loadJiraRuns({cursor:jiraNextCursor,direction:'next'})};
  $('#jiraRows').onclick=event=>{const button=event.target.closest('[data-jira-details]');if(button)openJiraDetails(button.dataset.jiraDetails)};
  await loadJiraRuns();
}
async function loadJiraRuns({background=false,cursor=jiraCursor,direction=null}={}) {
  if(background&&jiraBusy)return;
  const request=++jiraRequest,group=groupState.selected;
  const current=()=>request===jiraRequest&&state.page==='jira'&&group===groupState.selected;
  jiraBusy=true;
  if(!background){
    $('#jiraRefresh').disabled=true;$('#jiraPrevious').disabled=true;$('#jiraNext').disabled=true;
    $('#jiraNotice').textContent='正在读取最新任务下发情况…';
  }
  try {
    const params=new URLSearchParams({groupId:group});
    if(cursor!==null)params.set('before',String(cursor));
    const result=await api('/api/jira-runs?'+params);
    if(!current())return;
    if(direction){
      if(direction==='next')jiraPages.push(jiraCursor);else jiraPages.pop();
      $('#jiraDetails')?.close();jiraDetailId=null;
    }
    jiraCursor=cursor;jiraNextCursor=result.nextCursor;jiraItems=result.items;
    updateJiraRows(result.items);updateJiraDetails();jiraLoaded=true;
    $('#jiraNotice').textContent='';
  }catch(error){
    if(current()){
      $('#jiraNotice').textContent=jiraLoaded?'更新失败，当前为上次结果。请稍后刷新重试。':'Jira 台账暂时无法读取，请刷新重试。';
      if(!jiraLoaded)$('#jiraRows').innerHTML='<div class="empty">领取记录加载失败</div>';
    }
  }finally{
    if(request===jiraRequest)jiraBusy=false;
    if(current()){
      $('#jiraRefresh').disabled=false;
      $('#jiraPrevious').disabled=jiraPages.length===0;
      $('#jiraNext').disabled=jiraNextCursor===null;
    }
  }
}
setInterval(()=>{if(typeof state!=='undefined'&&state.page==='jira'&&!document.hidden&&!jiraBusy)loadJiraRuns({background:true})},10000);
