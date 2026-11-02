const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

// Run the actual classic scripts without a browser dependency. This checks state,
// payloads and rendered markup; it intentionally does not claim visual coverage.
function fixture(search='') {
  const nodes=new Map();
  function element(){return {innerHTML:'',value:'',textContent:'',dataset:{},close(){},focus(){},style:{},classList:{add(){},remove(){},toggle(){}},addEventListener(){},querySelector(){return element()},querySelectorAll(){return []},closest(){return element()}}}
  const navButtons=['roles','groups','skills'].map(page=>({...element(),dataset:{page}}));
  const document={querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector)},querySelectorAll(selector){return selector==='nav button'?navButtons:[]},addEventListener(){},createElement:element};
  const context=vm.createContext({document,window:{addEventListener(){}},location:{search,href:'http://localhost/'+search},history:{replaceState(){}},URL,URLSearchParams,HTMLElement:class {},console,queueMicrotask(){},setInterval(){},setTimeout(){},confirm(){return false},fetch(){throw Error('Unexpected network request')}});
  for(const name of ['groups.js','machines.js','run-catalog.js','task-schedules.js','skills.js','app.js']) {
    let script=fs.readFileSync(path.join(__dirname,'../../main/resources/static',name),'utf8');
    if(name==='app.js')script=script.slice(0,script.indexOf('const initialPage='))+script.slice(script.indexOf('const effortCatalogs ='));
    vm.runInContext(script,context,{filename:name});
  }
  const run=code=>vm.runInContext(code,context);
  run(`groupState.items=[{id:'a',name:'甲组'},{id:'b',name:'乙组'}];state.roles=[{id:'ra',name:'甲角色',duty:'职责',groupId:'a',enabled:true},{id:'rb',name:'乙角色',duty:'职责',groupId:'b',enabled:true},{id:'old',name:'旧角色',duty:'职责',groupId:null,enabled:true}];state.agents=[{agentId:'ma',groupId:'a',enabled:true,capabilities:['supervisor','executor']},{agentId:'mb',groupId:'b',enabled:true,capabilities:['supervisor','executor']}];`);
  return {run,nodes,context,navButtons};
}

function jiraFixture(jira=null){
  const h=fixture('?groupId=a');
  h.context.savedJira=jira;
  h.run("setDraft({...blankSop(),id:'saved',dispatchMode:'group',name:'Jira 流程',jira:savedJira,steps:[{roleId:'ra',displayName:'步骤',timeoutSec:60}]})");
  return h;
}

test('Jira status defaults to pending and is editable after reload',()=>{
  const {run}=jiraFixture();
  run("Object.assign(state.sop.draft,{jiraEnabled:true,jiraProject:' 产品研发 A ',jiraAccount:' bot '})");
  assert.equal(run('validateSop()'),'');
  assert.equal(run('sopPayload().jira.jql'),'project = "产品研发 A" AND status = 10205 AND assignee = currentUser()');
  assert.equal(run('sopPayload().jira.account'),'bot');
  const html=run('workflowInspectorHtml()');
  assert.match(html,/data-sop-field="jiraProject"/);
  assert.match(html,/data-sop-field="jiraStatus"/);
  assert.match(html,/只领取该 Jira 登录账号负责、符合所填状态的工单/);
  assert.doesNotMatch(html,/data-sop-field="jiraJql"/);
  run("setDraft({...sopPayload(),id:'saved'})");
  assert.equal(run('state.sop.draft.jiraProject'),'产品研发 A');
  assert.equal(run('state.sop.draft.jiraStatus'),"待办");
  assert.equal(run('isSopDirty()'),false);
  run("state.sop.draft.jiraStatus='Closed';state.sop.draft.jiraJql='project = OTHER AND status = Closed'");
  assert.equal(run('sopPayload().jira.jql'),'project = "产品研发 A" AND status = "Closed" AND assignee = currentUser()');
  assert.equal(run('isSopDirty()'),true);
  run("setDraft({...sopPayload(),id:'saved'})");
  assert.equal(run('state.sop.draft.jiraStatus'),'Closed');
});

test('Jira names containing quotes and backslashes stay within quoted values',()=>{
  const {run,context}=jiraFixture();
  context.project='研发 "A"\\B';
  context.issueStatus='Closed';
  run('Object.assign(state.sop.draft,{jiraEnabled:true,jiraProject:project,jiraStatus:issueStatus})');
  assert.equal(run('sopPayload().jira.jql'),'project = "研发 \\"A\\"\\\\B" AND status = "Closed" AND assignee = currentUser()');
  run("setDraft({...sopPayload(),id:'saved'})");
  assert.equal(run('state.sop.draft.jiraProject'),context.project);
  assert.equal(run('state.sop.draft.jiraStatus'),context.issueStatus);
  assert.match(run('workflowInspectorHtml()'),/&quot;A&quot;/);
});

test('legacy simple Jira queries retain the project and status',()=>{
  const query='project = PROJ AND status = "Open"';
  const {run}=jiraFixture({jql:query,account:''});
  assert.equal(run('state.sop.draft.jiraProject'),'PROJ');
  assert.equal(run('sopPayload().jira.jql'),'project = "PROJ" AND status = "Open" AND assignee = currentUser()');
  assert.equal(run('isSopDirty()'),false);
});

test('legacy pending query gains the assignee filter and fixed query reloads unchanged',()=>{
  for(const query of ['project = PROJ AND status = 10205','project = PROJ AND status = 10205 AND assignee = currentUser()']){
    const {run}=jiraFixture({jql:query,account:'bot'});
    assert.equal(run('state.sop.draft.jiraProject'),'PROJ');
    assert.equal(run('sopPayload().jira.jql'),'project = "PROJ" AND status = 10205 AND assignee = currentUser()');
    run("setDraft({...sopPayload(),id:'saved'})");
    assert.equal(run('isSopDirty()'),false);
    assert.doesNotMatch(run('workflowInspectorHtml()'),/旧查询无法自动转换/);
  }
});

test('complex legacy Jira queries require a project before replacement or can be disabled',()=>{
  const query='project = PROJ AND status in (Open, "In Progress") AND assignee = currentUser() ORDER BY created ASC';
  const {run}=jiraFixture({jql:query,account:'bot'});
  assert.match(run('workflowInspectorHtml()'),/请填写项目名称后保存/);
  run("state.sop.draft.name='新名称'");
  assert.match(run('validateSop()'),/请填写 Jira 项目名称/);
  run("state.sop.draft.jiraProject='NEW'");
  assert.equal(run('validateSop()'),'');
  assert.equal(run('sopPayload().jira.jql'),'project = "NEW" AND status = 10205 AND assignee = currentUser()');
  run('state.sop.draft.jiraEnabled=false');
  assert.equal(run('sopPayload().jira'),null);
});

test('Jira project is required and rejects controls or oversized generated queries',()=>{
  const {run,context}=jiraFixture();
  run('state.sop.draft.jiraEnabled=true');
  assert.match(run('validateSop()'),/请填写 Jira 项目名称/);
  context.badStatus='Open\nOR status = Closed';
  run('state.sop.draft.jiraProject=badStatus');
  assert.match(run('validateSop()'),/控制字符/);
  context.longName='"'.repeat(2000);
  run('state.sop.draft.jiraProject=longName');
  assert.match(run('validateSop()'),/过长/);
});

test('Jira status rejects empty and control input and safely quotes query text',()=>{
  const {run,context}=jiraFixture();
  run("Object.assign(state.sop.draft,{jiraEnabled:true,jiraProject:'TEST',jiraStatus:' '})");
  assert.match(run('validateSop()'),/请填写 Jira 状态/);
  context.status='Open\nOR status = Closed';
  run('state.sop.draft.jiraStatus=status');
  assert.match(run('validateSop()'),/控制字符/);
  context.status='进行 "A"\\B';
  run('state.sop.draft.jiraStatus=status');
  assert.equal(run('validateSop()'),'');
  run("setDraft({...sopPayload(),id:'saved'})");
  assert.equal(run('state.sop.draft.jiraStatus'),context.status);
});

test('all, unassigned and concrete filters preserve their distinct meanings',()=>{
  const {run}=fixture('?groupId=a');
  assert.equal(run('state.roles.filter(groupMatches).map(x=>x.id).join()'),'ra');
  run(`groupState.selected='unassigned'`);
  assert.equal(run('state.roles.filter(groupMatches).map(x=>x.id).join()'),'old');
  assert.equal(run('concreteGroup()'),'');
  run(`groupState.selected=''`);
  assert.equal(run('state.roles.filter(groupMatches).length'),3);
});

test('SOP palette, machines and payload stay within the selected group',()=>{
  const {run}=fixture('?groupId=a');
  run(`setDraft({...blankSop(),name:'流程',supervisorAgentId:'ma',steps:[{roleId:'ra',displayName:'步骤',instruction:'执行',agentId:'ma',timeoutSec:60}]})`);
  assert.equal(run('sopPayload().groupId'),'a');
  assert.match(run('rolePaletteHtml()'),/甲角色/);
  assert.doesNotMatch(run('rolePaletteHtml()'),/乙角色|旧角色/);
  assert.equal(run(`suggestedAgents('supervisor').map(x=>x.agentId).join()`),'ma');
  assert.equal(run('validateSop()'),'');
  run(`state.sop.draft.steps[0].roleId='rb'`);
  assert.match(run('validateSop()'),/角色必须属于当前分组/);
  run(`state.sop.draft.steps[0].roleId='ra';state.sop.draft.groupId='b'`);
  assert.match(run('validateSop()'),/角色必须属于当前分组/);
});

test('dirty SOP cancellation retains the draft and previous filter',async()=>{
  const {run,nodes}=fixture('?groupId=a');
  run(`state.page='sops';setDraft({...blankSop(),name:'原流程'});state.sop.draft.name='未保存';`);
  run('renderCatalogActions(1)');
  const actions=nodes.get('#groupActions').innerHTML;
  assert.equal(await run(`selectGroup('b')`),false);
  assert.equal(nodes.get('#groupActions').hidden,false);
  assert.equal(nodes.get('#groupActions').innerHTML,actions);
  assert.equal(run('groupState.selected'),'a');
  assert.equal(run('state.sop.draft.name'),'未保存');
});

test('group names are escaped and historical run labels use the frozen name',()=>{
  const {run}=fixture();
  run(`groupState.items[0].name='<script>alert(1)</script>'`);
  assert.doesNotMatch(run(`groupOptions('a')`),/<script>/);
  assert.match(run(`groupOptions('a')`),/&lt;script&gt;/);
  assert.match(run(`groupMark({groupId:'a',groupName:'运行时名称'})`),/运行时名称/);
  assert.doesNotMatch(run(`groupMark({groupId:'a',groupName:'运行时名称'})`),/alert/);
  assert.match(run(`taskCard({id:'old',name:'旧任务',objective:'目标',enabled:true})`),/disabled title="请先归组"/);
});

test('run list requests include the current group and retain other filters',async()=>{
  const {run,nodes,context}=fixture('?groupId=b');
  let requested='';
  context.fetch=async url=>{requested=url;return{ok:true,text:async()=>JSON.stringify({items:[],total:0,page:0,size:20,statusFresh:true})}};
  run(`state.page='runs';runCatalogFilters.name='日报';runCatalogFilters.type='schedule';`);
  await run('loadRunCatalog()');
  assert.match(requested,/groupId=b/);assert.match(requested,/type=schedule/);assert.match(requested,/name=/);
  assert.match(nodes.get('#runRows').innerHTML,/暂无符合条件/);
});

test('retired unassigned links return to all and no migration entry is rendered',async()=>{
  const {run,nodes}=fixture('?page=roles&groupId=unassigned');
  assert.equal(run('groupState.selected'),'');
  assert.equal(run('groupUrl.searchParams.has("groupId")'),false);
  assert.equal(run('groupUrl.searchParams.get("page")'),'roles');
  run('renderGroupFilter()');
  assert.doesNotMatch(nodes.get('#groupFilter').innerHTML,/待归组|unassigned/);
  await run('renderGroups()');
  assert.doesNotMatch(nodes.get('#content').innerHTML,/待归组|group-unassigned/);
});

test('switching to group management ignores an unfinished business page render',async()=>{
  const {run,nodes,context}=fixture();
  let release;
  context.basePending=new Promise(resolve=>{release=resolve});
  run('loadGroups=async()=>{};loadBase=async()=>{await basePending};state.page="roles"');
  const previous=run('render()');
  await Promise.resolve();await Promise.resolve();
  run('state.page="groups"');
  await run('render()');
  const expected=nodes.get('#content').innerHTML;
  release();
  await previous;
  assert.match(expected,/分组名称/);
  assert.equal(nodes.get('#content').innerHTML,expected);
});

test('role catalog preserves group filtering and empty creation guidance',async()=>{
  const {run,nodes}=fixture('?groupId=a');
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/甲角色/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/乙角色|旧角色/);
  assert.match(nodes.get('#groupActions').innerHTML,/共 1 个角色/);
  run('state.roles=[]');await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/这个分组还没有角色/);
  assert.match(nodes.get('#content').innerHTML,/data-role-create/);
});

test('role batch assignment requires selection and clears its count on deselection',()=>{
  const {run,nodes,context}=fixture();
  nodes.set('[data-role-count]',{dataset:{total:'3'}});
  nodes.set('#groupActions [data-assign-group]',{});
  context.document.querySelectorAll=()=>[{},{}];
  run('updateRoleSelection()');
  assert.equal(nodes.get('[data-role-count]').textContent,'已选择 2 个角色');
  assert.equal(nodes.get('#groupActions [data-assign-group]').disabled,false);
  context.document.querySelectorAll=()=>[];
  run('updateRoleSelection()');
  assert.equal(nodes.get('[data-role-count]').textContent,'共 3 个角色');
  assert.equal(nodes.get('#groupActions [data-assign-group]').disabled,true);
});

test('task panel uses the selected group in its empty state',async()=>{
  const {run,nodes}=fixture('?groupId=a');
  run("state.page='tasks';state.tasks=[]");
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/这个分组还没有任务/);
  assert.match(nodes.get('#groupActions').innerHTML,/共 0 个任务/);

});

test('empty SOP panel shows creation guidance but keeps an unsaved draft editable',()=>{
  const {run,nodes}=fixture('?groupId=a');
  run("state.page='sops';state.sops=[];renderSopWorkspace()");
  assert.match(nodes.get('#content').innerHTML,/这个分组还没有工作流/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/data-sop-save/);
  run("setDraft(blankSop());renderSopWorkspace()");
  assert.match(nodes.get('#content').innerHTML,/data-sop-save/);
  assert.match(nodes.get('#content').innerHTML,/data-flow-canvas/);
  assert.equal(nodes.get('#groupActions').hidden,true);
  assert.match(nodes.get('#sopPicker').innerHTML,/新建工作流（未保存）/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/sop-list-panel|sopSearch/);
});


test('entering Skill management always opens the library and clears old targets',async()=>{
  const {run,navButtons}=fixture();
  run("state.page='groups';skillView.tab='history';skillView.selected.add('old');render=async()=>{}");
  await navButtons.find(b=>b.dataset.page==='skills').onclick();
  assert.equal(run('state.page'),'skills');assert.equal(run('skillView.tab'),'library');assert.equal(run('skillView.selected.size'),0);
});

test('shared group navigation clears Skill selections while preserving current tab',async()=>{
  const {run}=fixture();
  run("state.page='skills';skillView.tab='machines';skillView.selected.add('old');skillView.checked.add('p');render=async()=>{}");
  await run("selectGroup('b')");
  assert.equal(run('groupState.selected'),'b');assert.equal(run('skillView.tab'),'machines');assert.equal(run('skillView.selected.size'),0);assert.equal(run('skillView.checked.size'),0);
});

test('group catalog and MCP picker expose package counts and scoped links',async()=>{
  const {run,nodes}=fixture();
  run("groupState.items[0].mcpCount=3;state.page='groups'");await run('renderGroups()');
  assert.match(nodes.get('#content').innerHTML,/<th>MCP<\/th>/);
  assert.match(nodes.get('#content').innerHTML,/data-group-jump="mcps" data-id="a">3<\/button>/);
  run("state.page='mcps';renderGroupFilter()");
  assert.match(nodes.get('#groupFilter').innerHTML,/<small>3<\/small>/);
  run('groupState.items=[]');await run('renderGroups()');
  assert.match(nodes.get('#content').innerHTML,/colspan="9"/);
});


test('SOP DingTalk details switch defaults off and survives save/reload and draft changes',()=>{
  const {run}=fixture('?groupId=a');
  run(`setDraft({...blankSop(),name:'通知流程'})`);
  assert.equal(run('sopPayload().dingtalkShowExecutionDetails'),false);
  assert.doesNotMatch(run('workflowInspectorHtml()'),/data-sop-field="dingtalkShowExecutionDetails"[^>]*checked/);
  run(`updateField({type:'checkbox',checked:true},state.sop.draft,'dingtalkShowExecutionDetails')`);
  assert.equal(run('isSopDirty()'),true);
  assert.equal(run('sopPayload().dingtalkShowExecutionDetails'),true);
  run(`setDraft({...sopPayload(),id:'saved'})`);
  assert.equal(run('isSopDirty()'),false);
  assert.match(run('workflowInspectorHtml()'),/data-sop-field="dingtalkShowExecutionDetails"[^>]*checked/);
  run(`state.sops=[{...state.sop.draft}];state.sop.draft.dingtalkShowExecutionDetails=false;discardSopChanges()`);
  assert.equal(run('sopPayload().dingtalkShowExecutionDetails'),true);
  run(`setDraft({...sopPayload(),id:'',name:'副本'})`);
  assert.equal(run('sopPayload().dingtalkShowExecutionDetails'),true);
});

test('top group picker restores current label, escapes names and exposes load errors',()=>{
  const {run,nodes}=fixture('?groupId=b');
  run('renderGroupFilter()');
  assert.match(nodes.get('#groupFilter').innerHTML,/<strong>乙组<\/strong>/);
  assert.doesNotMatch(nodes.get('#groupFilter').innerHTML,/<input|搜索分组/);
  assert.doesNotMatch(nodes.get('#groupFilter').innerHTML,/group-sidebar|data-open-groups/);
  run(`groupState.items[1].name='<img onerror="x">';renderGroupFilter()`);
  assert.doesNotMatch(nodes.get('#groupFilter').innerHTML,/<img/);
  run(`groupState.items=[];groupState.error='分组暂时无法加载';renderGroupFilter()`);
  assert.match(nodes.get('#groupFilter').innerHTML,/分组暂不可用/);
  assert.match(nodes.get('#groupFilter').innerHTML,/data-group-retry/);
  run(`state.page='groups';renderGroupFilter()`);
  assert.equal(nodes.get('#groupFilter').hidden,true);
});



test('bulk controls appear only when catalog items are selected',()=>{
  const {run,nodes,context}=fixture();
  run('renderCatalogActions(3)');
  assert.match(nodes.get('#groupActions').innerHTML,/data-batch-actions hidden/);
  assert.match(nodes.get('#groupActions').innerHTML,/data-clear-group-selection/);
  context.document.querySelectorAll=()=>[{}];
  run('updateRoleSelection()');
  assert.equal(nodes.get('#groupActions [data-batch-actions]').hidden,false);
  context.document.querySelectorAll=()=>[];
  run('updateRoleSelection()');
  assert.equal(nodes.get('#groupActions [data-batch-actions]').hidden,true);
});

test('choosing a group updates the URL without dropping the active page',async()=>{
  const {run,context}=fixture('?page=tasks&groupId=a&runPage=2');
  let saved;
  context.history.replaceState=(state,title,url)=>{saved=url};
  run(`state.page='tasks';render=async()=>{}`);
  await run(`selectGroup('b')`);
  assert.equal(saved.searchParams.get('groupId'),'b');
  assert.equal(saved.searchParams.get('page'),'tasks');
  assert.equal(saved.searchParams.has('runPage'),false);
});

test('SOP picker scopes and escapes options and keeps batch management in a dialog',()=>{
  const {run,nodes}=fixture('?groupId=a');
  run(`state.page='sops';state.sops=[{...blankSop(),id:'s1',name:'<流程>',groupId:'a'},{...blankSop(),id:'s2',name:'乙组流程',groupId:'b'}];setDraft(state.sops[0]);renderSopWorkspace()`);
  assert.match(nodes.get('#sopPicker').innerHTML,/value="s1" selected/);
  assert.match(nodes.get('#sopPicker').innerHTML,/&lt;流程&gt;/);
  assert.doesNotMatch(nodes.get('#sopPicker').innerHTML,/乙组流程|type="search"/);
  assert.match(nodes.get('#content').innerHTML,/<dialog id="sopManageDialog"/);
  assert.match(nodes.get('#content').innerHTML,/data-sop-delete="s1"/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/sop-list-panel|sopSearch/);
});

test('SOP picker cancellation and loading failure preserve the current draft',async()=>{
  const {run,context}=fixture('?groupId=a');
  run(`state.page='sops';setDraft({...blankSop(),id:'s1',name:'原流程'});state.sop.draft.name='未保存'`);
  assert.equal(await run(`selectSop('s2')`),false);
  assert.equal(run('state.sop.draft.name'),'未保存');
  context.confirm=()=>true;
  context.fetch=async()=>{throw Error('加载失败')};
  await assert.rejects(run(`selectSop('s2')`),/加载失败/);
  assert.equal(run('state.sop.draft.id'),'s1');
  assert.equal(run('state.sop.draft.name'),'未保存');
});

test('late SOP selection cannot replace a new group or an edited draft',async()=>{
  const {run,context}=fixture('?groupId=a');
  run(`state.page='sops';setDraft({...blankSop(),id:'s1',name:'原流程'})`);
  let release;
  context.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:true,text:async()=>JSON.stringify({id:'s2',name:'新流程',groupId:'a',steps:[]})})});
  const pending=run(`selectSop('s2')`);
  run(`groupState.selected='b'`);release();
  assert.equal(await pending,false);
  assert.equal(run('state.sop.draft.id'),'s1');
  run(`groupState.selected='a'`);
  const pendingEdit=run(`selectSop('s2')`);
  run(`state.sop.draft.name='加载期间修改'`);release();
  assert.equal(await pendingEdit,false);
  assert.equal(run('state.sop.draft.name'),'加载期间修改');
});

test('successful SOP selection replaces the editor and updates the picker',async()=>{
  const {run,nodes,context}=fixture('?groupId=a');
  run(`state.page='sops';state.sops=[{...blankSop(),id:'s1',name:'一号'},{...blankSop(),id:'s2',name:'二号'}];setDraft(state.sops[0])`);
  context.fetch=async()=>({ok:true,text:async()=>JSON.stringify({id:'s2',name:'二号',groupId:'a',enabled:true,steps:[]})});
  assert.equal(await run(`selectSop('s2')`),true);
  assert.equal(run('state.sop.draft.id'),'s2');
  assert.match(nodes.get('#sopPicker').innerHTML,/value="s2" selected/);
});

test('SOP batch controls use the management dialog selection',()=>{
  const {run,nodes,context}=fixture();
  run(`state.page='sops'`);
  nodes.set('[data-role-count]',{dataset:{total:'2'}});
  context.document.querySelectorAll=()=>[{}];
  run('updateRoleSelection()');
  assert.equal(nodes.get('#sopManageDialog [data-batch-actions]').hidden,false);
  assert.equal(nodes.get('#sopManageDialog [data-assign-group]').disabled,false);
  assert.equal(nodes.get('[data-role-count]').textContent,'已选择 1 个工作流');
});

test('rebuilding the SOP editor invalidates an older selection response',async()=>{
  const {run,context}=fixture('?groupId=a');
  run(`state.page='sops';setDraft({...blankSop(),id:'s1',name:'原流程'})`);
  let release;
  context.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:true,text:async()=>JSON.stringify({id:'s2',name:'旧响应',groupId:'a',steps:[]})})});
  const pending=run(`selectSop('s2')`);
  run('renderSopWorkspace()');release();
  assert.equal(await pending,false);
  assert.equal(run('state.sop.draft.id'),'s1');
});

test('SOP editor converts legacy machine choices into a group request',()=>{
  const {run,context}=fixture('?groupId=a');context.queueMicrotask=()=>{};
  run(`setDraft({id:'old',groupId:'a',name:'旧流程',dispatchMode:'fixed',supervisorAgentId:'ma',steps:[{roleId:'ra',displayName:'步骤',agentId:'ma',workingDirectory:'/old'}]});`);
  assert.equal(run('sopPayload().dispatchMode'),'group');
  assert.equal(run('sopPayload().supervisorAgentId'),undefined);
  assert.equal(run('sopPayload().steps[0].agentId'),undefined);
  assert.equal(run('sopPayload().steps[0].workingDirectory'),undefined);
  assert.match(run('workflowInspectorHtml()'),/点击保存后/);
  assert.doesNotMatch(run('workflowInspectorHtml()'),/data-agent-display/);
  assert.doesNotMatch(run('nodeInspectorHtml(state.sop.draft.steps[0])'),/data-agent-display|data-node-field="workingDirectory"/);
});
test('machine refresh does not mutate an unsaved group SOP',()=>{
  const {run,context}=fixture('?groupId=a');
  run(`setDraft({...blankSop(),name:'未保存草稿',steps:[{roleId:'ra'}]});`);
  const before=run('JSON.stringify(state.sop.draft)');
  context.document.querySelectorAll=()=>[];
  run(`state.agents[0].remark='新备注';updateAgentRuntimeUi()`);
  assert.equal(run('JSON.stringify(state.sop.draft)'),before);
});

test('deleted group selection returns to all groups after reload',async()=>{
  const {run,context}=fixture('?groupId=a&runPage=2');
  let replaced;
  context.history.replaceState=(_state,_title,url)=>{replaced=String(url)};
  context.api=async()=>({groups:[{id:'b',name:'乙组'}]});
  await run('loadGroups()');
  assert.equal(run('groupState.selected'),'');
  assert.equal(run('runCatalogFilters.page'),0);
  assert.equal(new URL(replaced).searchParams.has('groupId'),false);
  assert.equal(new URL(replaced).searchParams.has('runPage'),false);
  assert.doesNotMatch(run('groupOptions()'),/value="a"/);
});
