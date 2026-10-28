let jiraRequest = 0;
let jiraBusy = false;
let jiraCursor = null;
const jiraPages = [];

function jiraRunStatus(row) {
  if (row.outcome === 'no_task') return '无可领取工单';
  if (row.outcome === 'blocked') return '因阻断结束';
  if (row.status === 'running' && row.waitingFor) return {blocked:'遇阻，等待人工处理',finish:'等待确认结束',advance:'等待确认下一步',legacy:'等待进入下一步'}[row.waitingFor] || '等待确认';
  return {queued:'等待机器',running:'正在执行',cancelling:'正在停止',completed:'流程完成',failed:'执行中断或失败',cancelled:'已取消'}[row.status] || '状态未知';
}

function jiraDeliveryHtml(deliveries) {
  if (!deliveries?.length) return '<span>尚无交付回执</span>';
  const labels = {submitted:'已提交（附凭证）',completed:'本步完成，未提交',blocked:'业务阻断',unknown:'交付结果待核实',not_applicable:'本步无交付操作'};
  const submitted=deliveries.some(d=>d.status==='submitted');
  const summary=submitted?'已记录提交凭证':(labels[deliveries.at(-1).status]||'结果待核实');
  return `<details><summary>${esc(summary)} · ${deliveries.length} 条回执</summary>${deliveries.map(d=>`<p><b>${esc(labels[d.status]||'结果待核实')}</b> · ${esc(time(d.recordedAt))}<br>${esc(d.summary||'')}${d.reference?`<br>凭证：${esc(d.reference)}`:''}</p>`).join('')}</details>`;
}

function jiraRowsHtml(rows) {
  if (!rows.length) return '<div class="empty">暂无 Jira 领取记录。请先在 SOP 中启用监督机统一领取，再手动运行或由定时任务触发。</div>';
  return `<table class="run-table"><thead><tr><th>分组 / 任务</th><th>Jira</th><th>领取与分配</th><th>运行结果</th><th>交付回执</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr>
    <td>${esc(groupName(row.groupId))}<br>${esc(row.name||'历史任务')}</td>
    <td>${esc(row.issueKey||'尚未领取')}</td>
    <td>${esc({waiting_claim:'等待监督机领取',claimed:'已领取，正在下发账号',ready:'已下发至执行机',no_task:'本轮无可领取工单'}[row.claimState]||'领取状态未知')}<br>监督机：${esc(row.supervisorAgentId||'等待分配')}<br>执行机：${esc(row.executorAgentId||'等待分配')}<br>${row.claimedAt?esc(time(row.claimedAt)):''}</td>
    <td>${esc(jiraRunStatus(row))}${row.resourceState==='occupied'&&['completed','failed','cancelled'].includes(row.status)?'<br>等待确认执行已停止':''}${row.reason?`<br>${esc(row.reason)}`:''}<br><small>${esc(time(row.createdAt))} 开始${row.finishedAt?`<br>${esc(time(row.finishedAt))} 结束`:''}</small></td>
    <td>${jiraDeliveryHtml(row.deliveries)}</td>
    <td><a href="${esc(row.monitorUrl)}" target="_blank" rel="noopener">查看过程</a><br><button type="button" data-allocation="${esc(row.workflowId)}">机器分配</button></td>
  </tr>`).join('')}</tbody></table>`;
}

async function renderJiraRuns() {
  jiraCursor = null;
  jiraPages.length = 0;
  $('#content').className = 'content run-catalog';
  $('.toolbar').classList.add('hidden');
  $('#content').innerHTML = '<section class="run-catalog-panel"><div class="actions"><button id="jiraRefresh">刷新</button><span id="jiraNotice" role="status">正在加载…</span></div><p class="meta">按分组查看领取、机器分配和最终结果。同一工单的历次尝试分别保留；流程完成不代表已提交。</p><div id="jiraRows"></div><footer class="actions"><button id="jiraPrevious" disabled>上一页</button><button id="jiraNext" disabled>下一页</button></footer></section>';
  $('#jiraRefresh').onclick=()=>loadJiraRuns();
  await loadJiraRuns();
}

async function loadJiraRuns({background=false}={}) {
  const request=++jiraRequest, group=groupState.selected, cursor=jiraCursor;
  const current=()=>request===jiraRequest&&state.page==='jira'&&group===groupState.selected&&cursor===jiraCursor;
  jiraBusy=true;
  $('#jiraRefresh').disabled=true;
  $('#jiraPrevious').disabled=true;
  $('#jiraNext').disabled=true;
  $('#jiraNotice').textContent='正在读取最新领取与结果…';
  try {
    const params=new URLSearchParams({groupId:group});
    if(cursor!==null)params.set('before',String(cursor));
    const result=await api('/api/jira-runs?'+params);
    if(!current())return;
    $('#jiraRows').innerHTML=jiraRowsHtml(result.items);
    $('#jiraNotice').textContent='每 10 秒自动刷新当前页。';
    $('#jiraPrevious').disabled=jiraPages.length===0;
    $('#jiraNext').disabled=result.nextCursor===null;
    $('#jiraPrevious').onclick=()=>{jiraCursor=jiraPages.pop();loadJiraRuns()};
    $('#jiraNext').onclick=()=>{jiraPages.push(jiraCursor);jiraCursor=result.nextCursor;loadJiraRuns()};
  } catch(error) {
    if(current()) {
      $('#jiraNotice').textContent='Jira 台账暂时无法读取，请刷新重试。';
      if(!background)$('#jiraRows').innerHTML='<div class="empty">领取记录加载失败</div>';
    }
  } finally { if(request===jiraRequest)jiraBusy=false;if(current())$('#jiraRefresh').disabled=false; }
}
setInterval(()=>{if(typeof state!=='undefined'&&state.page==='jira'&&!document.hidden&&!jiraBusy)loadJiraRuns({background:true})},10000);
