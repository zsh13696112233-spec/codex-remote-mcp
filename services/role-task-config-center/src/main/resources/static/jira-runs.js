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
  return {queued:'等待机器',running:'正在执行',cancelling:'正在停止',completed:'流程已结束',failed:'执行中断或失败',cancelled:'已取消'}[row.status] || '状态未知';
}

const jiraDeliveryLabels = {not_submitted:'要求提交但未提交',submitted:'已提交（附凭证）',completed:'本步完成，未提交',blocked:'业务阻断',unknown:'交付结果待核实',not_applicable:'本步无交付操作'};
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
    <td><strong>${esc(row.issueKey||'尚未领取')}</strong><small>重试 ${Number(row.retryCount||0)} 次</small></td>
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
  return `<p class="meta">${esc(row.name||'历史任务')} · ${esc(row.issueKey||'尚未领取')}</p><dl><dt>下发状态</dt><dd>${esc(jiraClaimStatus(row))}</dd><dt>运行状态</dt><dd>${esc(jiraRunStatus(row))}${jiraStopPending(row)?' · 等待确认执行已停止':''}</dd><dt>领取时间</dt><dd>${row.claimedAt?esc(time(row.claimedAt)):'尚未领取'}</dd><dt>开始时间</dt><dd>${esc(time(row.createdAt))}</dd><dt>结束时间</dt><dd>${row.finishedAt?esc(time(row.finishedAt)):'尚未结束'}</dd><dt>原因说明</dt><dd>${esc(row.reason||'暂无')}</dd></dl><h3>重试 ${Number(row.retryCount||0)} 次</h3><p class="meta">按同一工单实际执行轮数减 1 统计；启动失败且未执行步骤不计，同一运行内重跑步骤不重复计数。</p>${(row.executions||[]).slice(1).map(r=>`<article class="jira-receipt"><strong>第 ${r.retryNumber} 次重试 · ${esc(time(r.startedAt))}</strong><p>${esc(r.reason)}</p></article>`).join('')}<h3>交付回执</h3>${row.deliveries?.length?row.deliveries.map(d=>`<article class="jira-receipt"><strong>${esc(jiraDeliveryLabels[d.status]||'结果待核实')}</strong><small>${esc(time(d.recordedAt))}</small><p>${esc(d.summary||'')}</p>${d.reference?`<p>凭证：${esc(d.reference)}</p>`:''}</article>`).join(''):'<p class="meta">尚无交付回执</p>'}`;
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
let jiraView = 'pool';
let poolFilters = {project:'',task:'',state:''};
function jiraCenterHtml() {
  return `<section class="jira-overview" aria-label="领取概览">
    <div class="jira-center-heading"><div><h2>领取概览</h2><p>按当前分组查看工单领取与执行进度</p></div><div class="jira-refresh-actions"><span>每 10 秒自动更新</span><button id="jiraRefresh" type="button">刷新</button></div></div>
    <div id="poolProjects" class="jira-metrics"><div><span>项目</span><strong>—</strong></div><div><span>待分配工单</span><strong>—</strong></div><div><span>执行中</span><strong>—</strong></div></div>
    <div id="poolSchedules" class="jira-schedules"></div>
  </section>
  <section class="jira-workspace">
    <div class="jira-view-tabs" aria-label="领取记录分类">
      <button type="button" data-jira-view="pool" aria-pressed="true" aria-controls="jiraPool">工单池</button>
      <button type="button" data-jira-view="collections" aria-pressed="false" aria-controls="jiraCollections">采集记录</button>
      <button type="button" data-jira-view="history" aria-pressed="false" aria-controls="jiraHistoryPanel">运行历史</button>
    </div>
    <section id="jiraPool" class="jira-view-panel" aria-label="工单池">
      <form id="poolFilters" class="run-filters jira-pool-filters">
        <label>项目编号<input id="poolProject" type="search" maxlength="160" placeholder="输入项目编号"></label>
        <label>任务名称<input id="poolTask" type="search" maxlength="160" placeholder="输入任务名称"></label>
        <label>工单状态<select id="poolState"><option value="">全部状态</option><option value="pending">待分配</option><option value="reserved">启动中</option><option value="running">执行中</option><option value="completed">已完成</option><option value="failed">失败</option><option value="invalid">已不符合条件</option></select></label>
        <div class="actions"><button id="poolFilter" class="primary" type="submit">查询</button><button id="poolReset" type="button">重置</button></div>
      </form>
      <p class="jira-section-note">同一工单合并展示一行，显示当前状态与最近采集时间；历次执行可在运行历史查看。</p>
      <div id="poolItems" class="jira-table-scroll"><div class="empty">正在加载工单…</div></div>
      <div class="jira-pagination"><span>按首次入池倒序 · 首次实际执行不计重试，之后每轮实际执行加 1</span><div class="actions"><button id="poolFirst" type="button">最新工单</button><button id="poolNext" type="button" disabled>更早工单</button></div></div>
    </section>
    <section id="jiraCollections" class="jira-view-panel" aria-label="采集记录" hidden>
      <div class="jira-section-heading"><h3>采集记录</h3><span>最近 50 次</span></div>
      <p class="jira-section-note">查看每次查询的时间、结果和发现的工单数量。</p>
      <div id="poolCollections" class="jira-table-scroll"><div class="empty">正在加载采集记录…</div></div>
    </section>
    <section id="jiraHistoryPanel" class="jira-view-panel" aria-label="运行历史" hidden>
      <div class="jira-section-heading"><h3>运行历史</h3><span>领取 · 机器分配 · 交付结果</span></div>
      <p class="jira-section-note">流程完成不代表已提交，提交凭证和返工理由可在详情中查看。</p>
      <p id="jiraNotice" role="status">正在加载…</p><div id="jiraRows" class="jira-table-scroll"></div>
      <footer class="jira-pagination"><span>按运行时间倒序排列</span><div class="actions"><button id="jiraPrevious" disabled>上一页</button><button id="jiraNext" disabled>下一页</button></div></footer>
    </section>
    <dialog id="jiraBlockedReason" aria-labelledby="jiraBlockedReasonTitle">
      <button type="button" class="dialog-close" data-dialog-close aria-label="关闭执行记录">×</button>
      <h2 id="jiraBlockedReasonTitle">执行记录</h2><div data-execution-body></div>
      <footer><button type="button" data-dialog-close>关闭</button></footer>
    </dialog>
    <p id="poolNotice" class="jira-pool-notice" role="status" aria-live="polite"></p>
  </section>`;
}
function showJiraView(view) {
  jiraView=view;
  const panels={pool:'jiraPool',collections:'jiraCollections',history:'jiraHistoryPanel'};
  for(const [name,id] of Object.entries(panels))$('#'+id).hidden=name!==view;
  document.querySelectorAll('[data-jira-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.jiraView===view)));
  if(view!=='history')$('#jiraDetails')?.close();
  if(view!=='pool')$('#jiraBlockedReason')?.close();
}
async function renderJiraRuns() {
  $('#jiraDetails')?.close();jiraDetailId=null;
  jiraCursor=null;jiraNextCursor=null;jiraPages.length=0;jiraItems=[];jiraLoaded=false;
  poolBefore=null;poolNext=null;poolLoaded=false;poolFilters={project:'',task:'',state:''};
  $('#content').className='content run-catalog jira-catalog';
  $('.toolbar').classList.add('hidden');
  $('#content').innerHTML=jiraCenterHtml();
  document.querySelectorAll('[data-jira-view]').forEach(button=>button.onclick=()=>showJiraView(button.dataset.jiraView));
  showJiraView(jiraView);
  $('#jiraRefresh').onclick=()=>Promise.all([loadJiraRuns(),loadJiraPool()]);
  $('#poolFilters').onsubmit=event=>{event.preventDefault();poolFilters={project:$('#poolProject').value.trim(),task:$('#poolTask').value.trim(),state:$('#poolState').value};loadJiraPool(null)};
  $('#poolReset').onclick=()=>{$('#poolFilters').reset();poolFilters={project:'',task:'',state:''};loadJiraPool(null)};
  $('#poolFirst').onclick=()=>loadJiraPool(null);
  $('#poolNext').onclick=()=>loadJiraPool(poolNext);
  $('#poolItems').onclick=event=>{const button=event.target.closest('[data-pool-executions]');if(button)openJiraPoolExecution(button.dataset.poolExecutions)};
  $('#jiraPrevious').onclick=()=>{if(jiraPages.length)loadJiraRuns({cursor:jiraPages.at(-1),direction:'previous'})};
  $('#jiraNext').onclick=()=>{if(jiraNextCursor!==null)loadJiraRuns({cursor:jiraNextCursor,direction:'next'})};
  $('#jiraRows').onclick=event=>{const button=event.target.closest('[data-jira-details]');if(button)openJiraDetails(button.dataset.jiraDetails)};
  await Promise.all([loadJiraRuns(),loadJiraPool()]);
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
    if(background&&$('#jiraPool'))loadJiraPool(poolBefore,{background:true});
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

let poolBefore=null;
let poolNext=null;
let poolLoading=false;
let poolLoaded=false;
let poolRequest=0;
let poolItems=[];
let poolExecutionItemId=null;
function jiraExecutionLabel(execution) {
  return execution.retryNumber===0?'首次执行':`第 ${execution.retryNumber} 次重试`;
}
function jiraExecutionDetailHtml(row,execution) {
  return `<div class="jira-execution-heading"><h3>${esc(jiraExecutionLabel(execution))}</h3><span class="jira-status ${jiraStatusKind(execution)}">${esc(jiraRunStatus(execution))}</span></div>
    ${execution.stopPending?'<p class="jira-execution-warning">等待确认执行已停止</p>':''}
    <div class="jira-execution-times"><div><span>开始时间</span><strong>${execution.startedAt?esc(time(execution.startedAt)):'未记录'}</strong></div><div><span>结束时间</span><strong>${execution.finishedAt?esc(time(execution.finishedAt)):'尚未结束'}</strong></div></div><dl class="jira-execution-reasons">
    ${execution.outcome==='blocked'?`<div class="jira-reason-block blocked"><dt>本轮阻断原因</dt><dd>${esc(execution.reason||'未记录具体原因，请查看运行过程。')}</dd></div>`:''}
    </dl>${execution.monitorUrl?`<a class="jira-execution-process" href="${esc(execution.monitorUrl)}" target="_blank" rel="noopener">查看本轮过程 <span aria-hidden="true">↗</span></a>`:''}`;
}
function jiraExecutionListHtml(row) {
  const executions=[...(row.executions||[])].reverse();
  return `<div class="jira-execution-summary"><span class="jira-execution-issue">${esc(row.issue_key)}</span><span>共 ${executions.length} 条</span></div>${executions.length?`<ol class="jira-execution-records">${executions.map(execution=>`<li>${jiraExecutionDetailHtml(row,execution)}<p>监督机：${esc(execution.machines?.supervisorAgentId?(execution.machines.supervisorRemark||'未填写备注'):'未记录分配')}${execution.machines?.supervisorDeleted?'（已删除）':''} · 执行机：${esc(execution.machines?.executorAgentId?(execution.machines.executorRemark||'未填写备注'):'未记录分配')}${execution.machines?.executorDeleted?'（已删除）':''}</p></li>`).join('')}</ol>`:'<p class="jira-execution-empty">暂无记录</p>'}`;
}
function updateJiraPoolExecution() {
  const dialog=$('#jiraBlockedReason');
  if(!dialog?.open)return;
  const row=poolItems.find(item=>String(item.id)===poolExecutionItemId);
  if(!row)return;
  const body=dialog.querySelector('[data-execution-body]'),html=jiraExecutionListHtml(row);
  if(body.jiraMarkup!==html){
    const top=dialog.scrollTop;
    body.innerHTML=html;body.jiraMarkup=html;
    dialog.scrollTop=top;
  }
}
function openJiraPoolExecution(id) {
  if(!poolItems.some(row=>String(row.id)===id&&row.executions?.length))return;
  poolExecutionItemId=id;$('#jiraBlockedReason').querySelector('[data-execution-body]').jiraMarkup=null;$('#jiraBlockedReason').querySelector('[data-execution-body]').innerHTML='';$('#jiraBlockedReason').showModal();updateJiraPoolExecution();
}
function jiraPoolRowHtml(row) {
  const run={status:row.run_status,outcome:row.outcome,waitingFor:row.waitingFor};
  const labels={invalid:'已不符合条件',pending:'待分配',reserved:'启动中',bound:'已创建运行',consumed:'本次未派发',cancelled:'来源已失效'};
  const waiting=!row.run_status&&['pending','reserved'].includes(row.state);
  const collectedAt=row.collected_at||row.created_at;
  const sources=row.sources||[{name:row.name,groupId:row.group_id}];
  return `<tr><td>${esc(row.issue_key.split('-')[0])} / ${row.monitorUrl?`<a href="${esc(row.monitorUrl)}" target="_blank" rel="noopener">${esc(row.issue_key)}</a>`:esc(row.issue_key)}<small>${esc(row.title)}</small></td>
    <td>开发人：${esc(row.people?.developers?.join('、')||'未获取')}<br>负责人：${esc(row.people?.assignee||'未获取')}${row.people?.collectedAt?`<small>${esc(time(row.people.collectedAt))}</small>`:''}</td>
    <td>${sources.map(s=>`<div>${esc(s.name)}<small>${esc(groupName(s.groupId))}</small></div>`).join('')}</td>
    <td>${esc(time(collectedAt))}${waiting?`<small>已等待 ${Math.max(0,Math.floor((Date.now()-Date.parse(row.queued_at||collectedAt))/60000))} 分钟</small>`:''}</td>
    <td><span class="jira-status ${row.run_status?jiraStatusKind(run):waiting?'waiting':'muted'}">${esc(row.run_status?jiraRunStatus(run):labels[row.state]||'等待处理')}</span>${row.stop_pending?'<small class="jira-warning">等待确认执行已停止</small>':''}</td>
    <td>${row.started_at?esc(time(row.started_at)):row.workflow_id?'尚未开始':'未创建运行'}</td>
    <td>${Number(row.retryCount||0)} 次</td>
    <td class="jira-executions-cell">${row.executions?.length?`<button class="jira-executions-view" type="button" data-pool-executions="${esc(row.id)}" aria-label="查看 ${esc(row.issue_key)} 的执行记录">查看</button>`:'<span class="jira-execution-empty">暂无记录</span>'}</td></tr>`;
}
async function loadJiraPool(before=poolBefore,{background=false}={}) {
  if(state.page!=='jira'||(background&&poolLoading))return;
  const panel=$('#jiraPool');
  if(!panel)return;
  const request=++poolRequest,group=groupState.selected;
  const current=()=>request===poolRequest&&state.page==='jira'&&group===groupState.selected&&panel===$('#jiraPool');
  poolLoading=true;
  try {
    const result=await api('/api/jira-pool?'+new URLSearchParams({groupId:group,...poolFilters,...(before?{before}:{})}));
    if(!current())return;
    if(!background)$('#jiraBlockedReason')?.close();
    poolItems=result.items;
    poolBefore=before;poolNext=result.nextCursor;$('#poolNext').disabled=poolNext===null;
    const projects=result.projects||[];
    const projectsOpen=$('#poolProjects').querySelector('details')?.open;
    const schedulesOpen=$('#poolSchedules').querySelector('details')?.open;
    $('#poolProjects').innerHTML=`<div><span>项目</span><strong>${projects.length}</strong></div><div><span>待分配工单</span><strong>${projects.reduce((sum,p)=>sum+Number(p.pending||0),0)}</strong></div><div><span>执行中</span><strong>${projects.reduce((sum,p)=>sum+Number(p.running||0),0)}</strong></div>${projects.length?`<details class="jira-project-list"${projectsOpen?' open':''}><summary>项目明细</summary>${projects.map(p=>`<p><strong>${esc(p.project)}</strong><span>待分配 ${Number(p.pending)} · 执行中 ${Number(p.running)}</span><small>最近采集 ${esc(time(p.lastCollectedAt))}</small></p>`).join('')}</details>`:''}`;
    $('#poolSchedules').innerHTML=(result.schedules||[]).length?`<details${schedulesOpen?' open':''}><summary>定时采集 <span>${result.schedules.length} 条规则</span></summary>${result.schedules.map(s=>`<p><strong>${esc(s.name)}</strong><span>下次触发 ${s.nextAt?esc(time(s.nextAt)):'未安排'}</span></p>`).join('')}</details>`:'<span>当前分组暂无启用的定时采集规则</span>';
    $('#poolItems').innerHTML=result.items.length?`<table class="jira-table jira-pool-table"><thead><tr><th>项目 / 工单</th><th>开发人 / 负责人</th><th>来源任务</th><th>最近采集</th><th>当前状态</th><th>执行开始</th><th>重试次数</th><th>执行记录</th></tr></thead><tbody>${result.items.map(jiraPoolRowHtml).join('')}</tbody></table>`:'<div class="empty jira-empty"><strong>暂无符合条件的工单</strong><span>可调整筛选条件，或等待任务触发后自动采集。</span></div>';
    const batchLabels={waiting:'等待采集机器',collecting:'正在查询',ready:'采集完成',failed:'采集失败',cancelled:'已停止派发'};
    $('#poolCollections').innerHTML=result.collections.length?`<table class="jira-table jira-collection-table"><thead><tr><th>任务</th><th>触发时间</th><th>完成时间</th><th>结果</th><th>工单数</th></tr></thead><tbody>${result.collections.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(time(r.createdAt))}</td><td>${r.finishedAt?esc(time(r.finishedAt)):'尚未完成'}</td><td><span class="jira-status ${r.state==='ready'?'success':r.state==='failed'?'failed':'waiting'}">${esc(batchLabels[r.state]||'状态未知')}</span><small>${esc(r.reason)}</small></td><td>${Number(r.count)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty jira-empty"><strong>暂无采集记录</strong><span>首次手动启动或定时触发后，采集结果会显示在这里。</span></div>';
    updateJiraPoolExecution();
    poolLoaded=true;$('#poolNotice').textContent='';
  }catch(error){
    if(current()){
      $('#poolNotice').textContent=poolLoaded?'更新失败，保留上次结果，请稍后重试。':'领取信息暂时无法读取，请稍后刷新重试。';
      if(!poolLoaded){
        $('#poolItems').innerHTML='<div class="empty">工单加载失败</div>';
        $('#poolCollections').innerHTML='<div class="empty">采集记录加载失败</div>';
      }
    }
  }finally{if(request===poolRequest)poolLoading=false}
}

const jiraStartWatching=new Set();
async function watchJiraStart(taskId,requestId) {
  if(jiraStartWatching.has(requestId))return;
  jiraStartWatching.add(requestId);
  try {
    const result=await api('/api/jira-requests/'+encodeURIComponent(requestId));
    if(['started','empty','failed','cancelled'].includes(result.state)) {
      sessionStorage.removeItem('jira-start:'+taskId);
      toast(result.state==='started'?'Jira 任务已启动，请在领取中心查看':result.reason||'本次未启动任务');
    }else setTimeout(()=>watchJiraStart(taskId,requestId),5000);
  }catch(error){setTimeout(()=>watchJiraStart(taskId,requestId),10000)}
  finally{jiraStartWatching.delete(requestId)}
}
for(let i=0;typeof sessionStorage!=='undefined'&&i<sessionStorage.length;i++){
  const key=sessionStorage.key(i);
  if(key.startsWith('jira-start:'))watchJiraStart(key.slice(11),sessionStorage.getItem(key));
}
