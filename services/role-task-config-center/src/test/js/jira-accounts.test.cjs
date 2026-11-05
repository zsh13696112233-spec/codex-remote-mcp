const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function fixture(){
  const content={innerHTML:'',className:''};
  const context=vm.createContext({state:{page:'jira-accounts',jiraAccounts:[]},groupState:{selected:'a'},URLSearchParams,document:{addEventListener(){}},$:()=>content,esc:x=>String(x??'').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),groupName:id=>id,api:async()=>({accounts:[]})});
  vm.runInContext(fs.readFileSync('src/main/resources/static/jira-accounts.js','utf8'),context);
  return {context,content,run:code=>vm.runInContext(code,context)};
}
test('SOP choices isolate group, enabled status and successful detection',()=>{
  const h=fixture();
  h.context.state.jiraAccounts=[{id:'ok',name:'<名称>',identity:'INTERNAL-ID',username:'登录名',executorAlias:'登录名',groupId:'a',enabled:true,testStatus:'passed'},{id:'other',name:'other',groupId:'b',enabled:true,testStatus:'passed'},{id:'disabled',groupId:'a',enabled:false,testStatus:'passed'},{id:'failed',groupId:'a',enabled:true,testStatus:'failed'}];
  const html=h.run("jiraAccountOptions('a','ok')");
  assert.match(html,/&lt;名称&gt;（登录名）/);
  assert.doesNotMatch(html,/other|disabled|failed/);
  assert.match(h.run("jiraAccountOptions('a','missing')"),/原账号不可用/);
});
test('account listing presents empty and gateway failure states',async()=>{
  const h=fixture();await h.run('renderJiraAccounts()');assert.match(h.content.innerHTML,/尚无 Jira 账号/);
  h.context.api=async()=>{throw Error('网关不可用')};await h.run('renderJiraAccounts()');assert.match(h.content.innerHTML,/网关不可用/);
});
test('late response cannot replace another group or page',async()=>{
  const h=fixture();let finish;h.context.api=()=>new Promise(resolve=>finish=resolve);
  const pending=h.run('renderJiraAccounts()');h.context.groupState.selected='b';h.content.innerHTML='new group';finish({accounts:[]});await pending;
  assert.equal(h.content.innerHTML,'new group');
});
test('public account fields are escaped and token is never rendered',async()=>{
  const h=fixture();h.context.api=async()=>({accounts:[{id:'one',name:'<script>',server:'https://jira.test',groupId:'a',identity:'<identity>',executorAlias:'bot',enabled:true,testStatus:'passed',tokenConfigured:true}]});
  await h.run('renderJiraAccounts()');assert.match(h.content.innerHTML,/&lt;script&gt;/);assert.doesNotMatch(h.content.innerHTML,/<script>|tokenConfigured/);
});

test('cards and SOP choices show login names instead of stable identifiers or aliases',()=>{
  const h=fixture();h.context.account={id:'ok',name:'自定义名称',identity:'JIRAUSER123',username:'login-user',executorAlias:'login-user',groupId:'a',enabled:true,testStatus:'passed'};
  h.context.state.jiraAccounts=[h.context.account];
  const card=h.run('jiraAccountCard(account)');
  assert.match(card,/<dt>执行机 Jira 账号<\/dt><dd>login-user<\/dd>/);
  assert.doesNotMatch(card,/JIRAUSER123/);
  assert.match(h.run("jiraAccountOptions('a','ok')"),/自定义名称（login-user）/);
  delete h.context.account.username;h.context.account.executorAlias='';
  assert.match(h.run('jiraAccountCard(account)'),/检测后自动映射/);
  assert.match(h.run("jiraAccountOptions('a','ok')"),/原账号不可用/);
  assert.doesNotMatch(h.run("jiraAccountOptions('a','ok')"),/JIRAUSER123|local-alias/);
});
