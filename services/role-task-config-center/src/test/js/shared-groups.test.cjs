const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

// Run the actual classic scripts without a browser dependency. This checks state,
// payloads and rendered markup; it intentionally does not claim visual coverage.
function fixture(search='') {
  const nodes=new Map();
  function element(){return {innerHTML:'',value:'',textContent:'',dataset:{},close(){},style:{},classList:{add(){},remove(){},toggle(){}},addEventListener(){},querySelector(){return element()},querySelectorAll(){return []},closest(){return element()}}}
  const navButtons=['roles','groups','skills'].map(page=>({...element(),dataset:{page}}));
  const document={querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector)},querySelectorAll(selector){return selector==='nav button'?navButtons:[]},addEventListener(){},createElement:element};
  const context=vm.createContext({document,Event:class {},window:{dispatchEvent(){},addEventListener(){},SopEditor:{normalize:d=>d,mount(el,options){this.lastOptions=options;return()=>{this.unmountCount=(this.unmountCount||0)+1}}}},location:{search,href:'http://localhost/'+search},history:{replaceState(){}},URL,URLSearchParams,console,setInterval(){},setTimeout(){},confirm(){return false},fetch(){throw Error('Unexpected network request')}});
  for(const name of ['groups.js','machines.js','run-catalog.js','task-schedules.js','skills.js','app.js']) {
    let script=fs.readFileSync(path.join(__dirname,'../../main/resources/static',name),'utf8');
    if(name==='app.js')script=script.slice(0,script.indexOf('const initialPage='));
    vm.runInContext(script,context,{filename:name});
  }
  const run=code=>vm.runInContext(code,context);
  run(`groupState.items=[{id:'a',name:'甲组'},{id:'b',name:'乙组'}];state.roles=[{id:'ra',name:'甲角色',duty:'职责',groupId:'a',enabled:true},{id:'rb',name:'乙角色',duty:'职责',groupId:'b',enabled:true},{id:'old',name:'旧角色',duty:'职责',groupId:null,enabled:true}];state.agents=[{agentId:'ma',groupId:'a',enabled:true,capabilities:['supervisor','executor']},{agentId:'mb',groupId:'b',enabled:true,capabilities:['supervisor','executor']}];`);
  return {run,nodes,context,navButtons};
}

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
  run('renderSopWorkspace()');assert.equal(run("window.SopEditor.lastOptions.roles.filter(r=>r.enabled&&r.groupId===state.sop.draft.groupId).map(r=>r.name).join()"),'甲角色');

  assert.equal(run(`suggestedAgents('supervisor').map(x=>x.agentId).join()`),'ma');
  assert.equal(run('validateSop()'),'');
  run(`state.sop.draft.steps[0].roleId='rb'`);
  assert.match(run('validateSop()'),/角色必须属于当前分组/);
  run(`state.sop.draft.steps[0].roleId='ra';state.sop.draft.groupId='b'`);
  assert.match(run('validateSop()'),/当前分组的主监督/);
});

test('dirty SOP cancellation retains the draft and previous filter',async()=>{
  const {run}=fixture('?groupId=a');
  run(`state.page='sops';setDraft({...blankSop(),name:'原流程'});state.sop.draft.name='未保存';`);
  assert.equal(await run(`selectGroup('b')`),false);
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
  run('renderGroupSidebar()');
  assert.doesNotMatch(nodes.get('#groupSidebar').innerHTML,/待归组|unassigned/);
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

test('role search distinguishes no matches from an empty group and preserves filtering',async()=>{
  const {run,nodes}=fixture('?groupId=a');
  nodes.set('#search',{value:'不存在',closest(){return{classList:{remove(){}}}}});
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/没有找到匹配的角色/);
  assert.match(nodes.get('#content').innerHTML,/data-role-clear-search/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/data-role-create/);
  nodes.get('#search').value='';
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/甲角色/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/乙角色|旧角色/);
  assert.match(nodes.get('#groupActions').innerHTML,/共 1 个角色/);
  run('state.roles=[]');
  await run('render({reload:false})');
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

test('task panel uses task empty and search states with the selected group',async()=>{
  const {run,nodes}=fixture('?groupId=a');
  run("state.page='tasks';state.tasks=[]");
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/这个分组还没有任务/);
  assert.match(nodes.get('#groupActions').innerHTML,/共 0 个任务/);
  nodes.get('#search').value='未匹配';
  await run('render({reload:false})');
  assert.match(nodes.get('#content').innerHTML,/没有找到匹配的任务/);
  assert.match(nodes.get('#content').innerHTML,/data-role-clear-search/);
});

test('empty SOP panel shows creation guidance but keeps an unsaved draft editable',()=>{
  const {run,nodes}=fixture('?groupId=a');
  run("state.page='sops';state.sops=[];renderSopWorkspace()");
  assert.match(nodes.get('#content').innerHTML,/这个分组还没有工作流/);
  assert.doesNotMatch(nodes.get('#content').innerHTML,/data-sop-save/);
  run("setDraft(blankSop());renderSopWorkspace()");
  assert.match(nodes.get('#content').innerHTML,/sopEditorRoot/);assert.ok(run('window.SopEditor.lastOptions.onSave'));
  assert.match(nodes.get('#content').innerHTML,/sopEditorRoot/);
  assert.match(nodes.get('#groupActions').innerHTML,/共 0 个工作流/);
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

test('group catalog and MCP sidebar expose package counts and scoped links',async()=>{
  const {run,nodes}=fixture();
  run("groupState.items[0].mcpCount=3;state.page='groups'");await run('renderGroups()');
  assert.match(nodes.get('#content').innerHTML,/<th>MCP<\/th>/);
  assert.match(nodes.get('#content').innerHTML,/data-group-jump="mcps" data-id="a">3<\/button>/);
  run("state.page='mcps';renderGroupSidebar()");
  assert.match(nodes.get('#groupSidebar').innerHTML,/<small>3<\/small>/);
  run('groupState.items=[]');await run('renderGroups()');
  assert.match(nodes.get('#content').innerHTML,/colspan="9"/);
});
