const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/main/resources/static/app.js','utf8');
function fixture(){
  const calls=[],waits=[],errors=[],shown=[];
  const context=vm.createContext({state:{},api:async path=>{calls.push(path);return [{id:path}]},loadGatewayReady:async()=>{},loadRuntime:async()=>{calls.push('runtime')},setSyncWait:visible=>waits.push(visible),toast:message=>errors.push(message),showTaskForm:value=>shown.push(value)});
  vm.runInContext(source.slice(source.indexOf('async function loadBase('),source.indexOf('function roleCard(')),context);
  vm.runInContext(source.slice(source.indexOf('let taskEditorLoading='),source.indexOf('function showTaskForm(')),context);
  return {context,calls,waits,errors,shown,run:code=>vm.runInContext(code,context)};
}
for(const [page,paths] of [
  ['roles',['/api/roles']],
  ['tasks',['/api/task-definitions']],
  ['sops',['/api/roles','/api/sops','runtime']],
  ['dingtalk',['/api/task-definitions','/api/dingtalk/targets']],
  ['dingtalk-targets',['/api/task-definitions']],
])test(`${page} only loads its own dependencies`,async()=>{
  const h=fixture();h.context.state.page=page;
  await h.run('loadBase()');assert.deepEqual(h.calls,paths);
});
test('role and task menus tolerate unavailable notification and machine services',async()=>{
  const h=fixture();h.context.api=async path=>{
    if(!['/api/roles','/api/task-definitions'].includes(path))throw Error('unavailable');
    return [{id:path}];
  };h.context.loadRuntime=async()=>{throw Error('offline')};
  let checks=0;h.context.loadGatewayReady=()=>{checks++;return new Promise(()=>{})};
  await h.run('loadBase("roles")');await h.run('loadBase("tasks")');
  assert.equal(h.context.state.roles[0].id,'/api/roles');
  assert.equal(h.context.state.tasks[0].id,'/api/task-definitions');
  assert.equal(checks,2);
});
test('task editor waits for fresh candidates and prevents repeat opens',async()=>{
  const h=fixture();let resolveTargets;
  h.context.api=async path=>{
    h.calls.push(path);
    return path.endsWith('/targets')?new Promise(resolve=>{resolveTargets=resolve}):[{id:'sop'}];
  };
  const pending=h.run('openTask({id:"task",sopId:"sop",dingtalkTargetId:"person"})');
  await h.run('openTask()');assert.equal(h.calls.length,2);assert.equal(h.shown.length,0);
  resolveTargets([{id:'person'}]);await pending;
  assert.equal(h.shown[0].id,'task');assert.equal(h.shown[0].dingtalkTargetId,'person');
  assert.equal(h.context.state.dingtalkTargets[0].id,'person');assert.deepEqual(h.waits,[true,false]);
  const again=h.run('openTask()');resolveTargets([{id:'updated-person'}]);await again;
  assert.equal(h.context.state.dingtalkTargets[0].id,'updated-person');assert.equal(h.calls.length,4);
});
test('task candidate failure releases loading and allows retry without stale options',async()=>{
  const h=fixture();h.context.state.dingtalkTargets=[{id:'stale'}];
  h.context.api=async()=>{throw Error('加载失败')};
  await h.run('openTask()');assert.deepEqual(h.errors,['加载失败']);assert.equal(h.shown.length,0);
  assert.deepEqual(h.waits,[true,false]);
  h.context.api=async()=>[];await h.run('openTask()');assert.equal(h.shown.length,1);
  assert.deepEqual(h.waits,[true,false,true,false]);
});
