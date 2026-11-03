const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/main/resources/static/jira-runs.js','utf8');
function context(extra={}) {
  const ctx=vm.createContext({document:{addEventListener(){}},setInterval(){},esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),time:v=>v,groupName:()=>'<测试组>',...extra});
  vm.runInContext(source,ctx);
  return ctx;
}
test('Jira ledger separates workflow completion from submission and escapes results',()=>{
  const ctx=context();
  ctx.rows=[{workflowId:'run',groupId:'g',name:'<任务>',status:'completed',claimState:'ready',issueKey:'TEST-1',createdAt:'now',resourceState:'released',deliveries:[],monitorUrl:'/monitor'}];
  const html=vm.runInContext('jiraRowsHtml(rows)',ctx);
  assert.match(html,/流程已结束/);
  assert.match(html,/尚无交付回执/);
  assert.match(html,/&lt;任务&gt;/);
  assert.doesNotMatch(html,/已提交（附凭证）/);
  ctx.rows[0].deliveries=[{status:'submitted',reference:'<r123>',summary:'已提交',recordedAt:'now'},{status:'completed',reference:'',summary:'检查完毕',recordedAt:'later'}];
  const submitted=vm.runInContext('jiraRowsHtml(rows)',ctx);
  assert.match(submitted,/已记录提交凭证/);
  assert.doesNotMatch(submitted,/&lt;r123&gt;/);
  assert.match(vm.runInContext('jiraDetailHtml(rows[0])',ctx),/&lt;r123&gt;/);
});
test('Jira ledger shows no work and interrupted claims separately',()=>{
  const ctx=context();
  ctx.row={status:'completed',outcome:'no_task'};
  assert.equal(vm.runInContext('jiraRunStatus(row)',ctx),'无可领取工单');
  ctx.row={status:'failed',outcome:'blocked'};
  assert.equal(vm.runInContext('jiraRunStatus(row)',ctx),'因阻断结束');
  ctx.row={status:'running',waitingFor:'blocked'};
  assert.equal(vm.runInContext('jiraRunStatus(row)',ctx),'遇阻，等待人工处理');
  assert.match(vm.runInContext('jiraRowsHtml([])',ctx),/暂无 Jira 领取记录/);
});
test('failed refresh is explicit and preserves earlier results in background',async()=>{
  const elements=Object.fromEntries(['#jiraRefresh','#jiraNotice','#jiraRows','#jiraPrevious','#jiraNext'].map(id=>[id,{textContent:'',innerHTML:'old',disabled:false}]));
  const ctx=context({URLSearchParams,state:{page:'jira'},groupState:{selected:''},$:id=>elements[id],api:async()=>{throw Error('offline')}});
  vm.runInContext('jiraLoaded=true',ctx);
  await vm.runInContext('loadJiraRuns({background:true})',ctx);
  assert.match(elements['#jiraNotice'].textContent,/更新失败，当前为上次结果/);
  assert.equal(elements['#jiraRows'].innerHTML,'old');
  assert.equal(elements['#jiraRefresh'].disabled,false);
});

test('machine remarks replace IDs and distinguish missing remarks from pending allocation',()=>{
  const ctx=context();
  ctx.rows=[{supervisorAgentId:'private-supervisor-id',supervisorRemark:'<监督备注>',executorAgentId:'private-executor-id',executorRemark:'  '}];
  const html=vm.runInContext('jiraRowsHtml(rows)',ctx);
  assert.match(html,/监督机：&lt;监督备注&gt;/);
  assert.match(html,/执行机：未填写备注/);
  assert.doesNotMatch(html,/private-supervisor-id|private-executor-id|<监督备注>/);
  ctx.rows=[{}];
  assert.match(vm.runInContext('jiraRowsHtml(rows)',ctx),/监督机：等待分配<\/span><span>执行机：等待分配/);
});

// Small DOM fixture: records element identity and writes, rather than just matching markup.
function listFixture() {
  const elements=Object.fromEntries(['#jiraRefresh','#jiraNotice','#jiraRows','#jiraPrevious','#jiraNext'].map(id=>[id,{textContent:'',disabled:false,writes:[]}]));
  for(const id of ['#jiraRefresh','#jiraPrevious','#jiraNext']) {
    let disabled=false;const node=elements[id];
    Object.defineProperty(node,'disabled',{get:()=>disabled,set:value=>{disabled=value;node.writes.push(value)}});
  }
  let body=null,markup='';const container=elements['#jiraRows'];container.scrollLeft=140;container.replacements=0;
  function rowElement() {
    let html='';const row={dataset:{},children:[],remove(){body.children.splice(body.children.indexOf(row),1)}};
    Object.defineProperty(row,'innerHTML',{get:()=>html,set:value=>{html=value;row.children=[...value.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(match=>({innerHTML:match[1]}))}});
    return row;
  }
  Object.defineProperty(container,'innerHTML',{get:()=>markup,set:value=>{
    markup=value;container.replacements++;
    body=value.includes('<tbody>')?{children:[],insertBefore(row,before){const old=this.children.indexOf(row);if(old>=0)this.children.splice(old,1);const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,row)}}:null;
    if(body)for(const match of value.matchAll(/<tr data-jira-run="([^"]*)">([\s\S]*?)<\/tr>/g)){const row=rowElement();row.dataset.jiraRun=match[1];row.innerHTML=match[2];body.children.push(row)}
  }});
  container.querySelector=()=>body;
  const ctx=context({URLSearchParams,state:{page:'jira'},groupState:{selected:'g'},$:id=>elements[id]||null,document:{addEventListener(){},createElement:()=>rowElement()},api:async()=>({items:[],nextCursor:null})});
  const run=code=>vm.runInContext(code,ctx);
  const row={workflowId:'one',groupId:'g',name:'任务',issueKey:'TEST-1',status:'running',claimState:'ready',createdAt:'now',monitorUrl:'/monitor',deliveries:[]};
  return {ctx,run,elements,container,row,body:()=>body};
}
test('background refresh keeps buttons enabled and reuses unchanged rows and action cells',async()=>{
  const h=listFixture();h.ctx.api=async()=>({items:[h.row],nextCursor:5});await h.run('loadJiraRuns()');
  const row=h.body().children[0],actions=row.children[7],replacements=h.container.replacements;
  for(const node of Object.values(h.elements))node.writes.length=0;
  let resolve;h.ctx.api=()=>new Promise(r=>resolve=r);
  const refresh=h.run('loadJiraRuns({background:true})');
  assert.equal(h.elements['#jiraRefresh'].disabled,false);assert.equal(h.elements['#jiraNext'].disabled,false);
  assert.equal(h.elements['#jiraNotice'].textContent,'');
  resolve({items:[h.row],nextCursor:5});await refresh;
  assert.equal(h.body().children[0],row);assert.equal(row.children[7],actions);
  assert.equal(h.container.replacements,replacements);assert.equal(h.container.scrollLeft,140);
  assert.deepEqual(h.elements['#jiraRefresh'].writes,[false]);
  h.ctx.api=async()=>({items:[{...h.row,status:'completed'}],nextCursor:5});await h.run('loadJiraRuns({background:true})');
  assert.equal(h.body().children[0],row);assert.equal(row.children[7],actions);assert.match(row.children[4].innerHTML,/流程已结束/);
});
test('background refresh inserts and removes only affected records',async()=>{
  const h=listFixture();h.ctx.api=async()=>({items:[h.row,{...h.row,workflowId:'two'}],nextCursor:null});await h.run('loadJiraRuns()');
  const kept=h.body().children[0];
  h.ctx.api=async()=>({items:[{...h.row,workflowId:'new'},h.row],nextCursor:null});await h.run('loadJiraRuns({background:true})');
  assert.deepEqual(h.body().children.map(row=>row.dataset.jiraRun),['new','one']);assert.equal(h.body().children[1],kept);
});
test('failed navigation keeps current page and next cursor; stale background cannot undo navigation',async()=>{
  const h=listFixture();h.ctx.api=async()=>({items:[h.row],nextCursor:5});await h.run('loadJiraRuns()');
  h.ctx.api=async()=>{throw Error('offline')};await h.run("loadJiraRuns({cursor:5,direction:'next'})");
  assert.equal(h.run('jiraCursor'),null);assert.equal(h.run('jiraPages.length'),0);assert.equal(h.run('jiraNextCursor'),5);
  assert.equal(h.body().children[0].dataset.jiraRun,'one');assert.equal(h.elements['#jiraNext'].disabled,false);
  let resolve;h.ctx.api=()=>new Promise(r=>resolve=r);const old=h.run('loadJiraRuns({background:true})');
  h.ctx.api=async()=>({items:[{...h.row,workflowId:'next-page'}],nextCursor:null});await h.run("loadJiraRuns({cursor:5,direction:'next'})");
  resolve({items:[h.row],nextCursor:5});await old;
  assert.equal(h.run('jiraCursor'),5);assert.equal(h.run('jiraPages.length'),1);assert.equal(h.body().children[0].dataset.jiraRun,'next-page');
});
test('late group responses do not replace visible results and duplicate polls are skipped',async()=>{
  const h=listFixture();let resolve,calls=0;h.ctx.api=()=>{calls++;return new Promise(r=>resolve=r)};
  const pending=h.run('loadJiraRuns({background:true})');await h.run('loadJiraRuns({background:true})');assert.equal(calls,1);
  h.ctx.groupState.selected='other';resolve({items:[h.row],nextCursor:null});await pending;
  assert.equal(h.container.replacements,0);
});
test('open details receive new receipts without closing or resetting scroll',async()=>{
  const h=listFixture();const body={innerHTML:''},dialog={open:true,scrollTop:150,querySelector:()=>body};
  h.elements['#jiraDetails']=dialog;h.run("jiraDetailId='one'");
  h.ctx.api=async()=>({items:[{...h.row,reason:'<原因>',deliveries:[{status:'submitted',reference:'<hash>',summary:'完成',recordedAt:'now'}]}],nextCursor:null});
  await h.run('loadJiraRuns({background:true})');
  assert.equal(dialog.open,true);assert.equal(dialog.scrollTop,150);assert.match(body.innerHTML,/&lt;原因&gt;/);assert.match(body.innerHTML,/&lt;hash&gt;/);
});
test('terminal records retain pending-stop warning and full reasons only in details',()=>{
  const h=listFixture();h.ctx.row={...h.row,status:'completed',resourceState:'review',reason:'完整原因'};
  assert.match(h.run('jiraRowHtml(row)'),/等待确认执行已停止/);assert.doesNotMatch(h.run('jiraRowHtml(row)'),/完整原因/);
  assert.match(h.run('jiraDetailHtml(row)'),/完整原因/);
});
test('rework details distinguish explicit non-submission and escape reasons',()=>{
  const ctx=context();
  ctx.row={name:'任务',issueKey:'TEST-1',status:'running',claimState:'ready',createdAt:'now',reworkCount:2,reworks:[{startedAt:'today',reason:'<script>缺少权限</script>'}],deliveries:[{status:'not_submitted',summary:'提交前阻断',recordedAt:'now'}]};
  const html=vm.runInContext('jiraDetailHtml(row)',ctx);
  assert.match(html,/返工 2 次/);
  assert.match(html,/&lt;script&gt;缺少权限/);
  assert.match(html,/要求提交但未提交/);
  assert.doesNotMatch(html,/<script>/);
});

test('center views show one panel while preserving filter input and accessible selection',()=>{
  const elements={'#jiraPool':{},'#jiraCollections':{},'#jiraHistoryPanel':{},'#poolProject':{value:'TEST'}};
  const buttons=['pool','collections','history'].map(view=>({dataset:{jiraView:view},setAttribute(name,value){this[name]=value}}));
  const ctx=context({$:id=>elements[id],document:{addEventListener(){},querySelectorAll:()=>buttons}});
  for(const view of ['collections','history','pool']){
    vm.runInContext(`showJiraView('${view}')`,ctx);
    assert.equal(elements['#jiraPool'].hidden,view!=='pool');
    assert.equal(elements['#jiraCollections'].hidden,view!=='collections');
    assert.equal(elements['#jiraHistoryPanel'].hidden,view!=='history');
    assert.deepEqual(buttons.map(button=>button['aria-pressed']),buttons.map(button=>String(button.dataset.jiraView===view)));
    assert.equal(elements['#poolProject'].value,'TEST');
  }
});

function poolFixture() {
  const elements=Object.fromEntries(['#jiraPool','#poolNext','#poolProjects','#poolSchedules','#poolItems','#poolCollections','#poolNotice'].map(id=>[id,{innerHTML:'',textContent:'',querySelector:()=>null}]));
  const result={projects:[],schedules:[],items:[],collections:[],nextCursor:null};
  const ctx=context({URLSearchParams,state:{page:'jira'},groupState:{selected:'g'},$:id=>elements[id],api:async()=>result});
  return {ctx,elements,result,run:code=>vm.runInContext(code,ctx)};
}
test('pool renders merged sources with current run link and latest collection time',async()=>{
  const h=poolFixture();
  h.result.items=[{issue_key:'TEST-1',title:'<工单>',state:'bound',workflow_id:'run',run_status:'running',
    collected_at:'最近采集时间',started_at:'原执行时间',monitorUrl:'/monitor?workflowId=run',
    sources:[{name:'<来源一>',groupId:'one'},{name:'来源二',groupId:'two'}],reworkCount:1,
    reworks:[{startedAt:'上次时间',reason:'<返工原因>'}]}];
  await h.run('loadJiraPool()');
  const html=h.elements['#poolItems'].innerHTML;
  assert.equal((html.match(/<tr>/g)||[]).length,2);
  assert.match(html,/<th>最近采集<\/th>/);
  assert.match(html,/最近采集时间/);
  assert.match(html,/原执行时间/);
  assert.match(html,/&lt;来源一&gt;/);
  assert.match(html,/来源二/);
  assert.match(html,/href="\/monitor\?workflowId=run"/);
  assert.match(html,/正在执行/);
  assert.doesNotMatch(html,/&lt;返工原因&gt;/);
  assert.doesNotMatch(html,/已由其他来源处理|尚未开始|未创建运行|已等待/);
});
test('pool recollection does not reset waiting duration or claim unstarted work is handled',()=>{
  const now=Date.parse('2026-11-02T08:30:00Z');
  const ctx=context({Date:{now:()=>now,parse:Date.parse}});
  ctx.row={issue_key:'TEST-1',title:'工单',state:'pending',collected_at:'2026-11-02T08:29:00Z',
    queued_at:'2026-11-02T08:00:00Z',sources:[{name:'任务',groupId:'g'}]};
  const waiting=vm.runInContext('jiraPoolRowHtml(row)',ctx);
  assert.match(waiting,/已等待 30 分钟/);
  assert.match(waiting,/2026-11-02T08:29:00Z/);
  assert.match(waiting,/待分配/);
  ctx.row.state='consumed';
  const unselected=vm.runInContext('jiraPoolRowHtml(row)',ctx);
  assert.match(unselected,/本次未派发/);
  assert.match(unselected,/未创建运行/);
  assert.doesNotMatch(unselected,/已由其他来源处理|已等待/);
});
test('pool shows manual wait and stop verification from the current run',()=>{
  const ctx=context();
  ctx.row={issue_key:'TEST-1',title:'工单',state:'bound',workflow_id:'run',run_status:'running',
    waitingFor:'blocked',collected_at:'now',sources:[{name:'任务',groupId:'g'}]};
  assert.match(vm.runInContext('jiraPoolRowHtml(row)',ctx),/遇阻，等待人工处理/);
  ctx.row.run_status='failed';ctx.row.waitingFor=null;ctx.row.outcome='blocked';ctx.row.stop_pending=true;
  const stopped=vm.runInContext('jiraPoolRowHtml(row)',ctx);
  assert.match(stopped,/因阻断结束/);
  assert.match(stopped,/等待确认执行已停止/);
});
test('pool uses a single view action and lists every execution inside the dialog',()=>{
  const ctx=context();
  ctx.row={id:42,issue_key:'TEST-1',state:'bound',title:'工单',reworkCount:2,sources:[],executions:[
    {workflowId:'first',kind:'first',status:'failed',outcome:'blocked',reason:'隐藏原因'},
    {workflowId:'retry1',kind:'rework',reworkNumber:1,status:'failed',outcome:'blocked'},
    {workflowId:'retry2',kind:'rework',reworkNumber:2,status:'completed'},
    {workflowId:'old',kind:'legacy',status:'failed'}]};
  const html=vm.runInContext('jiraPoolRowHtml(row)',ctx);
  assert.equal((html.match(/data-pool-executions=/g)||[]).length,1);
  assert.match(html,/data-pool-executions="42"/);
  assert.match(html,/>查看<\/button>/);
  assert.doesNotMatch(html,/首次执行|第 1 次返工|隐藏原因|共 4 条/);
  const list=vm.runInContext('jiraExecutionListHtml(row)',ctx);
  assert.match(list,/共 4 条/);
  assert.equal((list.match(/<li>/g)||[]).length,4);
  assert.match(list,/首次执行/);
  assert.match(list,/第 1 次返工/);
  assert.match(list,/第 2 次返工/);
  assert.match(list,/历史执行/);
  ctx.row.reworkCount=0;
  assert.match(vm.runInContext('jiraPoolRowHtml(row)',ctx),/>查看<\/button>/);
  ctx.row.executions=[];
  assert.doesNotMatch(vm.runInContext('jiraPoolRowHtml(row)',ctx),/data-pool-executions/);
  assert.match(vm.runInContext('jiraPoolRowHtml(row)',ctx),/暂无记录/);
});
test('execution dialog separates trigger reason from current blocking and keeps selection during polling',async()=>{
  const h=poolFixture(),body={innerHTML:'',querySelectorAll:()=>[]},dialog={open:false,scrollTop:150,querySelector:()=>body,showModal(){this.open=true},close(){this.open=false}};
  h.elements['#jiraBlockedReason']=dialog;
  h.result.items=[{id:42,issue_key:'TEST-1',title:'工单',state:'bound',sources:[],executions:[
    {workflowId:'retry1',kind:'rework',reworkNumber:1,status:'failed',outcome:'blocked',startedAt:'start',finishedAt:'end',
      reworkReason:'上轮<原因>',reason:'本轮<阻断>',monitorUrl:'/monitor?workflowId=retry1'}]}];
  await h.run('loadJiraPool()');
  h.run("openJiraPoolExecution('42')");
  assert.equal(dialog.open,true);
  assert.match(body.innerHTML,/共 1 条/);
  assert.doesNotMatch(body.innerHTML,/上轮&lt;原因&gt;|触发本轮返工/);
  assert.match(body.innerHTML,/本轮阻断原因<\/dt><dd>本轮&lt;阻断&gt;/);
  assert.match(body.innerHTML,/href="\/monitor\?workflowId=retry1"/);
  h.result.items[0].executions[0].reason='更新后的原因';
  await h.run('loadJiraPool(null,{background:true})');
  assert.equal(dialog.open,true);
  assert.equal(dialog.scrollTop,150);
  assert.match(body.innerHTML,/更新后的原因/);
  h.result.items[0].executions.push({workflowId:'retry2',kind:'rework',reworkNumber:2,status:'completed'});
  await h.run('loadJiraPool(null,{background:true})');
  assert.match(body.innerHTML,/共 2 条/);
  assert.match(body.innerHTML,/第 2 次返工/);
  assert.equal(dialog.scrollTop,150);
  h.result.items[0].executions[0].reason='';
  await h.run('loadJiraPool(null,{background:true})');
  assert.match(body.innerHTML,/未记录具体原因，请查看运行过程/);
  await h.run('loadJiraPool()');
  assert.equal(dialog.open,false);
});
test('pool refresh preserves overview disclosure state and uses only submitted filters',async()=>{
  const h=poolFixture();
  h.elements['#poolProjects'].querySelector=()=>({open:true});
  h.elements['#poolSchedules'].querySelector=()=>({open:true});
  h.elements['#poolProject']={value:'UNSUBMITTED'};
  h.run("poolFilters={project:'TEST',task:'任务',state:'pending'};jiraView='collections'");
  let request;
  h.ctx.api=async url=>{request=url;return {...h.result,projects:[{project:'TEST',pending:2,running:1,lastCollectedAt:'now'}],schedules:[{name:'任务',nextAt:'later'}]}};
  await h.run('loadJiraPool()');
  const query=new URLSearchParams(request.split('?')[1]);
  assert.equal(query.get('project'),'TEST');
  assert.equal(query.get('task'),'任务');
  assert.match(h.elements['#poolProjects'].innerHTML,/<details class="jira-project-list" open>/);
  assert.match(h.elements['#poolSchedules'].innerHTML,/<details open>/);
  assert.equal(h.run('jiraView'),'collections');
  assert.equal(h.elements['#poolProject'].value,'UNSUBMITTED');
});
test('a submitted pool query supersedes a pending poll without losing its result',async()=>{
  const h=poolFixture();let resolve,calls=0;
  h.ctx.api=()=>{calls++;return new Promise(r=>resolve=r)};
  const pending=h.run('loadJiraPool(null,{background:true})');
  await h.run('loadJiraPool(null,{background:true})');
  assert.equal(calls,1);
  h.ctx.api=async()=>({...h.result,nextCursor:12});
  await h.run('loadJiraPool(null)');
  resolve({...h.result,nextCursor:99});await pending;
  assert.equal(h.run('poolNext'),12);
  assert.equal(h.elements['#poolNext'].disabled,false);
});
test('pool load errors replace loading messages and refresh errors retain loaded data',async()=>{
  const h=poolFixture();
  h.ctx.api=async()=>{throw Error('offline')};
  await h.run('loadJiraPool()');
  assert.match(h.elements['#poolNotice'].textContent,/暂时无法读取/);
  assert.match(h.elements['#poolItems'].innerHTML,/工单加载失败/);
  h.ctx.api=async()=>h.result;await h.run('loadJiraPool()');
  const html=h.elements['#poolItems'].innerHTML;
  h.ctx.api=async()=>{throw Error('offline')};await h.run('loadJiraPool()');
  assert.equal(h.elements['#poolItems'].innerHTML,html);
  assert.match(h.elements['#poolNotice'].textContent,/保留上次结果/);
});

test('execution records show only current blocking and machine summary',()=>{
  const ctx=context();
  ctx.row={issue_key:'TEST-1',executions:[{workflowId:'old',kind:'rework',reworkNumber:1,status:'completed',reason:'结束说明隐藏',reworkReason:'旧阻断隐藏',monitorUrl:'/process',machines:{supervisorAgentId:'s',supervisorRemark:'监督<甲>',executorAgentId:'e',executorRemark:'执行乙',executorDeleted:true}}]};
  const html=vm.runInContext('jiraExecutionListHtml(row)',ctx);
  assert.match(html,/监督&lt;甲&gt;/);
  assert.match(html,/执行乙（已删除）/);
  assert.match(html,/查看本轮过程/);
  assert.doesNotMatch(html,/本轮结果|交付|步骤总结|机器详情|旧阻断隐藏|结束说明隐藏|本轮阻断原因|<details/);
});
