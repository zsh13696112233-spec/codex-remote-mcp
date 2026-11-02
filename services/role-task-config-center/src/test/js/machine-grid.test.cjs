const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function fixture(){
  const nodes=new Map();
  const element=()=>({innerHTML:'',textContent:'',value:'',classList:{toggle(){}},querySelectorAll:()=>[],close(){this.closed=true}});
  const c=vm.createContext({document:{hidden:false,activeElement:null,addEventListener(){},querySelector(s){if(!nodes.has(s))nodes.set(s,element());return nodes.get(s)}},state:{page:'machines',agents:[]},groupState:{selected:'a',items:[{id:'a',name:'甲组'}]},esc:v=>String(v??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),time:v=>v||'未检测',toast(){},api:async()=>({agents:[]})});
  vm.runInContext(fs.readFileSync('src/main/resources/static/machines.js','utf8'),c);
  const run=s=>vm.runInContext(s,c);
  run(`function groupMatches(a){return !groupState.selected||a.groupId===groupState.selected}`);
  const machine={agentId:'one',ip:'192.0.2.1',port:4500,remark:'构建机',enabled:true,groupId:'a',capabilities:['supervisor'],connectionStatus:'online',testStatus:'failed'};
  c.machine=machine;c.state.agents=[machine];
  return {c,run,nodes,machine};
}
test('single-role machine appears only in its role column',()=>{
  const h=fixture();h.run('machineView.loaded=true;updateMachineBoard()');
  const html=h.nodes.get('#machineGrid').innerHTML;
  assert.equal((html.match(/data-id="one"/g)||[]).length,1);
  assert.match(h.nodes.get('#machineTotal').textContent,/1 台机器/);assert.match(html,/machine-state online/);assert.match(html,/<table/);assert.doesNotMatch(html,/<svg/);assert.doesNotMatch(html,/检测 Skill 目录/);
});
test('status filtering, disabled precedence and ordering remain stable across state transitions',()=>{
  const h=fixture();h.run(`machineView.filter='online'`);assert.equal(h.run('machineMatches(machine)'),true);
  h.machine.enabled=false;assert.equal(h.run('machineMatches(machine)'),false);
  h.run(`machineView.filter='disabled'`);assert.equal(h.run('machineMatches(machine)'),true);
  assert.match(h.run('machineCard(machine,"executor")'),/machine-row disabled/);
  h.c.other={...h.machine,agentId:'two',ip:'192.0.2.2'};
  for(const status of ['online','offline']){h.machine.connectionStatus=status;assert.equal(h.run('machineSorted([other,machine]).map(a=>a.agentId).join()'),'one,two')}
});
test('poll reads saved state, deduplicates requests and preserves last success on failure',async()=>{
  const h=fixture();let resolve,calls=0;
  h.c.api=url=>{assert.equal(url,'/api/agents');calls++;return new Promise(r=>resolve=r)};
  const p=h.run('refreshMachineStatuses()');await h.run('refreshMachineStatuses()');assert.equal(calls,1);
  resolve({agents:[h.machine]});await p;const success=h.run('machineView.lastSuccess');assert.ok(success);
  h.c.api=async()=>{throw Error('offline')};await h.run('refreshMachineStatuses()');
  assert.equal(h.c.state.agents[0],h.machine);assert.equal(h.run('machineView.lastSuccess'),success);assert.match(h.nodes.get('#machineRefreshStatus').textContent,/当前为上次结果/);
});
test('first failure shows retry instead of machines cached by another page',async()=>{
  const h=fixture();h.c.api=async()=>{throw Error('offline')};await h.run('refreshMachineStatuses()');
  assert.match(h.nodes.get('#machineGrid').innerHTML,/刷新列表/);assert.doesNotMatch(h.nodes.get('#machineGrid').innerHTML,/data-id/);
});
test('late responses after navigation or mutations cannot overwrite state',async()=>{
  for(const change of [`groupState.selected='b'`,`machineView.version++`,`state.page='roles'`]){
    const h=fixture();let resolve;h.c.api=()=>new Promise(r=>resolve=r);
    const p=h.run('refreshMachineStatuses()');h.run(change);resolve({agents:[]});await p;assert.equal(h.c.state.agents.length,1);
  }
});
test('hidden pages, operations and saving remarks suppress polling',async()=>{
  for(const condition of [`document.hidden=true`,`pendingMachineActions.add('one')`,`machineRemarkDrafts.set('one',{saving:true})`,`state.page='roles'`]){
    const h=fixture();let calls=0;h.c.api=async()=>{calls++;return {agents:[]}};h.run(condition);await h.run('refreshMachineStatuses()');assert.equal(calls,0);
  }
});
test('refresh preserves selected details and remark draft when status filter hides tile',async()=>{
  const h=fixture(),input={value:'正在输入'},info={innerHTML:''},actions={innerHTML:''};
  h.nodes.set('#machineDetails',{querySelector:s=>s==='[data-machine-detail-info]'?info:s==='.machine-card-actions'?actions:input});
  h.run(`machineView.selected='one';machineView.filter='online';machineRemarkDrafts.set('one',{value:'正在输入',error:'尚未保存'})`);
  h.c.api=async()=>({agents:[{...h.machine,connectionStatus:'offline'}]});await h.run('refreshMachineStatuses()');
  assert.equal(input.value,'正在输入');assert.equal(h.run('machineView.selected'),'one');assert.equal(h.run('machineView.filter'),'online');assert.match(info.innerHTML,/离线/);assert.match(h.nodes.get('#machineGrid').innerHTML,/没有符合条件/);
});
test('removed machine closes details',()=>{
  const h=fixture();h.run(`machineView.selected='missing';updateMachineDetails()`);
  assert.equal(h.run('machineView.selected'),null);assert.equal(h.nodes.get('#machineDetails').closed,true);
});
test('global supervisors remain visible and their details work under an executor group filter',()=>{
  const h=fixture(),info={innerHTML:''},actions={innerHTML:''},input={value:''};
  h.nodes.set('#machineDetails',{querySelector:s=>s==='[data-machine-detail-info]'?info:s==='.machine-card-actions'?actions:input});
  h.c.state.agents=[{...h.machine,groupId:null,capabilities:['supervisor']}];
  h.run("machineView.loaded=true;machineView.selected='one';updateMachineBoard()");
  assert.match(h.nodes.get('#machineGrid').innerHTML,/data-id="one"/);
  assert.equal(h.run('machineView.selected'),'one');
  assert.doesNotMatch(info.innerHTML,/全局|<dt>分组<\/dt>/);
});

test('list refresh updates occupation, detection result and timestamp together',async()=>{
  const h=fixture();
  h.c.api=async()=>({agents:[{...h.machine,availability:'busy',testStatus:'passed',testedAt:'2026-10-30T01:00:00Z'}]});
  await h.run('refreshMachineStatuses()');
  let html=h.nodes.get('#machineGrid').innerHTML;
  assert.match(html,/忙碌/);assert.match(html,/检测通过/);assert.match(html,/2026-10-30T01:00:00Z/);
  h.c.api=async()=>({agents:[{...h.machine,connectionStatus:'offline',availability:'busy',occupation:{state:'review'},testStatus:'failed',testedAt:'2026-10-30T01:00:10Z'}]});
  await h.run('refreshMachineStatuses()');
  html=h.nodes.get('#machineGrid').innerHTML;
  assert.match(html,/执行状态待核实/);assert.match(html,/检测失败/);assert.match(html,/2026-10-30T01:00:10Z/);assert.doesNotMatch(html,/>空闲</);
});
test('occupation filtering is independent of connection and disabled status',()=>{
  const h=fixture();Object.assign(h.machine,{enabled:false,availability:'busy',connectionStatus:'offline'});
  h.run("machineView.filter='busy'");assert.equal(h.run('machineMatches(machine)'),true);
  h.machine.occupation={state:'review'};
  h.run("machineView.filter='review'");assert.equal(h.run('machineMatches(machine)'),true);
  delete h.machine.occupation;delete h.machine.availability;
  assert.match(h.run('machineCard(machine,"supervisor")'),/占用状态未知/);
});

test('status refresh preserves horizontal position in each machine list',()=>{
  const h=fixture();h.run('machineView.loaded=true;updateMachineBoard()');
  const grid=h.nodes.get('#machineGrid');
  let lists=[{dataset:{machineList:'supervisor'},scrollLeft:120},{dataset:{machineList:'executor'},scrollLeft:250}];
  grid.querySelectorAll=selector=>selector==='[data-machine-list]'?lists:[];
  Object.defineProperty(grid,'innerHTML',{get(){return ''},set(){lists=lists.map(list=>({dataset:list.dataset,scrollLeft:0}))}});
  h.machine.testedAt='2026-10-30T02:00:00Z';h.run('updateMachineBoard()');
  assert.deepEqual(lists.map(list=>list.scrollLeft),[120,250]);
});
