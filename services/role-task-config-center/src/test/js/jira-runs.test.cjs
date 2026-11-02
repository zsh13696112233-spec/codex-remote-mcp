const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/main/resources/static/jira-runs.js','utf8');
function context(extra={}) {
  const ctx=vm.createContext({setInterval(){},esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),time:v=>v,groupName:()=>'<测试组>',...extra});
  vm.runInContext(source,ctx);
  return ctx;
}
test('Jira ledger separates workflow completion from submission and escapes results',()=>{
  const ctx=context();
  ctx.rows=[{workflowId:'run',groupId:'g',name:'<任务>',status:'completed',claimState:'ready',issueKey:'TEST-1',createdAt:'now',resourceState:'released',deliveries:[],monitorUrl:'/monitor'}];
  const html=vm.runInContext('jiraRowsHtml(rows)',ctx);
  assert.match(html,/流程完成/);
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
  const ctx=context({URLSearchParams,state:{page:'jira'},groupState:{selected:'g'},$:id=>elements[id]||null,document:{createElement:()=>rowElement()},api:async()=>({items:[],nextCursor:null})});
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
  assert.equal(h.body().children[0],row);assert.equal(row.children[7],actions);assert.match(row.children[4].innerHTML,/流程完成/);
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
