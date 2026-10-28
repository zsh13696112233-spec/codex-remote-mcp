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
  assert.match(submitted,/&lt;r123&gt;/);
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
  const elements=Object.fromEntries(['#jiraRefresh','#jiraNotice','#jiraRows','#jiraPrevious','#jiraNext'].map(id=>[id,{textContent:'',innerHTML:'old'}]));
  const ctx=context({URLSearchParams,state:{page:'jira'},groupState:{selected:''},$:id=>elements[id],api:async()=>{throw Error('offline')}});
  await vm.runInContext('loadJiraRuns({background:true})',ctx);
  assert.match(elements['#jiraNotice'].textContent,/暂时无法读取/);
  assert.equal(elements['#jiraRows'].innerHTML,'old');
  assert.equal(elements['#jiraRefresh'].disabled,false);
});
