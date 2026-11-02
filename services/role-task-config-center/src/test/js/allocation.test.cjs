const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function render(value){
  const context=vm.createContext({document:{addEventListener(){}},esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),time:value=>value});
  vm.runInContext(fs.readFileSync('src/main/resources/static/allocation.js','utf8'),context);
  context.value=value;
  return vm.runInContext('allocationHtml(value)',context);
}
test('assignment distinguishes unknown, queued and review states and escapes machine data',()=>{
  assert.match(render({allocationFresh:false}),/暂时无法读取/);
  assert.match(render({allocationFresh:true,allocation:null}),/原快照/);
  const queued=render({allocationFresh:true,allocation:{waitingReason:'等待资源'},resources:{state:'released'}});
  assert.match(queued,/等待分配/);assert.match(queued,/等待资源/);
  const held=render({allocationFresh:true,allocation:{supervisorAgentId:'s1',executorAgentId:'<worker>',executorRemark:'<执行>',workingDirectory:'C:/<work>'},resources:{state:'review'}});
  assert.match(held,/执行状态待核实/);assert.match(held,/&lt;执行&gt;/);assert.match(held,/未填写备注/);assert.match(held,/&lt;worker&gt;/);assert.doesNotMatch(held,/<worker>/);
});

test('deleted allocation shows remarks and retains original IDs in details',()=>{
  const html=render({allocationFresh:true,allocation:{supervisorAgentId:'old-supervisor',supervisorRemark:'监督备注',supervisorDeleted:true,executorAgentId:'old-executor',executorDeleted:true},resources:{state:'released'}});
  assert.match(html,/编号：old-supervisor/);assert.match(html,/编号：old-executor/);
  assert.match(html,/监督备注（已删除）/);assert.match(html,/未填写备注（已删除）/);
});
