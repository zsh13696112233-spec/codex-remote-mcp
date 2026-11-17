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

test('reminder target choices require available group but allow directory people',()=>{
  const h=fixture();h.context.targets=[{id:'g',targetType:'GROUP',enabled:true,available:true,displayName:'<群>'},{id:'off',targetType:'GROUP',enabled:false,available:true,displayName:'off'},{id:'p',targetType:'PERSON',enabled:false,available:true,displayName:'人员'},{id:'gone',targetType:'PERSON',available:false}];
  assert.match(h.run("jiraReminderOptions(targets,'GROUP','g')"),/&lt;群&gt;/);
  assert.doesNotMatch(h.run("jiraReminderOptions(targets,'GROUP','')"),/off/);
  assert.match(h.run("jiraReminderOptions(targets,'PERSON','p')"),/人员/);
  assert.match(h.run("jiraReminderOptions(targets,'PERSON','gone')"),/原对象不可用/);
});
test('reminder status shows business wording and explicit timezone',()=>{
  const h=fixture();
  assert.match(h.run("jiraReminderStatus({phase:'available'})"),/有任务，已重置/);
  assert.match(h.run("jiraReminderStatus({phase:'outside'})"),/不在生效时段/);
  assert.match(h.run("jiraReminderStatus({phase:'unknown'})"),/等待新的完整采集结果/);
  assert.match(h.run("jiraReminderStatus({phase:'waiting',nextAt:'2026-11-12T01:00:00Z'})"),/09:00:00.*北京时间/);
  assert.match(h.run("jiraReminderWindowRow({start:'22:00',end:'02:00'})"),/value="22:00".*value="02:00"/);
});
test('account card exposes the reminder configuration action',()=>{
  const h=fixture();assert.match(h.run("jiraAccountCard({id:'account',name:'test'})"),/data-jira-reminder="account"/);
});

function reminderFixture(){
  const h=fixture();
  const fields={enabled:{checked:true},groupId:{value:'group'},personId:{value:'person'}};
  const controls=Object.fromEntries(['[data-reminder-person-search]','[data-reminder-person-matches]','[data-reminder-close]','[data-add-window]','[data-windows]','[data-refresh-status]','[data-reminder-status]','[data-reminder-error]','[type=submit]'].map(k=>[k,{disabled:false,textContent:''}]));
  const row={querySelector:selector=>({value:selector==='[data-start]'?'09:00':'18:00'})};
  const form={elements:{namedItem:name=>fields[name]},querySelector:key=>controls[key],querySelectorAll:key=>key==='[name=day]:checked'?[{value:'1'}]:[row]};
  const dialog={open:false,innerHTML:'',setAttribute(){},showModal(){this.open=true},close(){this.open=false},querySelector:key=>key==='form'?form:controls[key]};
  h.context.document.querySelector=()=>dialog;
  h.context.toast=()=>{};
  const config={enabled:true,version:2,days:[1],windows:[{start:'09:00',end:'18:00'}],groupId:'group',personId:'person',status:{phase:'waiting'}};
  h.context.api=async path=>path==='/api/dingtalk/targets'?[]:config;
  return {...h,form,controls,dialog,config};
}
test('reminder form prevents duplicate save and retains input on failure',async()=>{
  const h=reminderFixture();await h.run("editJiraReminder({id:'account',name:'账号'})");
  let reject,requests=[];
  h.context.api=(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return new Promise((resolve,no)=>reject=no)};
  const pending=h.form.onsubmit({preventDefault(){}});
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(requests.length,1);
  assert.deepEqual(requests[0].body,{version:2,enabled:true,groupId:'group',personId:'person',days:[1],windows:[{start:'09:00',end:'18:00'}]});
  reject(Error('提醒配置已变化，请刷新后重试。'));await pending;
  assert.equal(h.dialog.open,true);assert.equal(h.controls['[type=submit]'].disabled,false);
  assert.match(h.controls['[data-reminder-error]'].textContent,/配置已变化/);
  assert.equal(h.form.elements.namedItem('personId').value,'person');
});
test('closing reminder while loading prevents a late form from reopening it',async()=>{
  const h=reminderFixture();let finish;
  h.context.api=path=>path==='/api/dingtalk/targets'?Promise.resolve([]):new Promise(resolve=>finish=resolve);
  const pending=h.run("editJiraReminder({id:'account',name:'账号'})");h.dialog.close();
  finish(h.config);await pending;
  assert.equal(h.dialog.open,false);assert.doesNotMatch(h.dialog.innerHTML,/<form>/);
});

test('reminder search matches partial names and departments locally with normalized keywords',()=>{
  const h=fixture();
  h.context.people=[
    {id:'zhang',targetType:'PERSON',available:true,displayName:'张小三',departmentDisplay:'游戏研发部'},
    {id:'alice',targetType:'PERSON',available:true,displayName:'Alice Chen',departmentDisplay:'Planning'},
    {id:'unavailable',targetType:'PERSON',available:false,displayName:'张小三'},
    {id:'group',targetType:'GROUP',available:true,enabled:true,displayName:'张小三'}
  ];
  const ids=q=>Array.from(h.run(`jiraReminderTargets(people,'PERSON',${JSON.stringify(q)}).map(t=>t.id)`));
  assert.deepEqual(ids('小三'),['zhang']);
  assert.deepEqual(ids('  张  研发  '),['zhang']);
  assert.deepEqual(ids('ｌＩＣＥ'),['alice']);
  assert.deepEqual(ids('planning chen'),['alice']);
  assert.deepEqual(ids('张 Planning'),[]);
  assert.deepEqual(ids('   '),['zhang','alice']);
});
test('search preserves selected IDs even when filtered out and distinguishes unavailable people',()=>{
  const h=fixture();h.context.people=[
    {id:'one',targetType:'PERSON',available:true,displayName:'张三',departmentDisplay:'研发'},
    {id:'two',targetType:'PERSON',available:true,displayName:'张三',departmentDisplay:'策划'},
    {id:'gone',targetType:'PERSON',available:false,displayName:'李四'}
  ];
  const html=h.run("jiraReminderOptions(people,'PERSON','one','策划')");
  assert.match(html,/<option value="one" selected>张三（研发）（已选择）/);
  assert.match(html,/<option value="two" >张三（策划）/);
  assert.equal((html.match(/value="one"/g)||[]).length,1);
  assert.match(h.run("jiraReminderOptions(people,'PERSON','one','无人匹配')"),/未找到匹配人员.*value="one" selected/);
  assert.match(h.run("jiraReminderOptions(people,'PERSON','gone','张')"),/原对象不可用/);
});
test('typing, clearing and Enter never request the backend or change the current recipient',async()=>{
  const h=reminderFixture();
  let requests=0;
  const people=[{id:'person',targetType:'PERSON',available:true,displayName:'张三',departmentDisplay:'研发'},
    {id:'second',targetType:'PERSON',available:true,displayName:'李四',departmentDisplay:'策划'}];
  h.context.api=async path=>{requests++;return path==='/api/dingtalk/targets'?people:h.config};
  await h.run("editJiraReminder({id:'account',name:'账号'})");
  assert.equal(requests,2);
  const search=h.controls['[data-reminder-person-search]'],select=h.form.elements.namedItem('personId'),hint=h.controls['[data-reminder-person-matches]'];
  search.value='策划';search.oninput();
  assert.match(select.innerHTML,/value="second"/);assert.equal(select.value,'person');
  assert.equal(hint.textContent,'匹配 1 位人员');
  search.value='不存在';search.oninput();
  assert.equal(hint.textContent,'未找到匹配人员');assert.equal(select.value,'person');
  assert.doesNotMatch(select.innerHTML,/value="second"/);
  search.value='';search.oninput();
  assert.equal(hint.textContent,'');assert.match(select.innerHTML,/value="second"/);
  let prevented=0;search.onkeydown({key:'Enter',isComposing:false,preventDefault(){prevented++}});
  search.onkeydown({key:'Enter',isComposing:true,preventDefault(){prevented++}});
  assert.equal(prevented,1);assert.equal(requests,2);
  select.value='second';search.value='张';search.oninput();
  assert.equal(select.value,'second');assert.match(select.innerHTML,/value="second" selected/);
  let submitted;
  h.context.api=async (path,options)=>{submitted=JSON.parse(options.body);return {...h.config,version:3}};
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(submitted.personId,'second');
  assert.equal(Object.hasOwn(submitted,'keyword'),false);
});
