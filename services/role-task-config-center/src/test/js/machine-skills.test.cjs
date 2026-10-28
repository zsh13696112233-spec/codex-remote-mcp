const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('src/main/resources/static/machines.js','utf8');
function setup(){
  const listeners={},button={},alert={};
  const form={elements:{remark:{value:' 测试机 '},ip:{value:'192.0.2.1'},port:{value:'4500'},workingDirectory:{value:' C:\\work '},groupId:{value:'g'},machineRole:{value:'executor'},skillEnabled:{checked:true},skillRoot:{value:' /work/skills '},mcpEnabled:{checked:true},mcpRoot:{value:'C:\\deploy\\program'}},querySelectorAll:()=>[],querySelector:s=>s==='[type=submit]'?button:alert};
  const dialog={innerHTML:'',querySelector:s=>s==='form'?form:{before(){}},showModal(){},close(){this.closed=true}};
  const context=vm.createContext({document:{createElement:()=>({}),querySelector:()=>dialog,querySelectorAll:()=>[],addEventListener:(name,fn)=>listeners[name]=fn},state:{agents:[]},groupState:{items:[]},concreteGroup:()=> 'g',esc:v=>String(v??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),time:v=>v,status:()=>'',groupMark:()=>'',render:async()=>{},toast:()=>{},api:async()=>({})});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  const machine={agentId:'machine-a',ip:'192.0.2.1',port:4500,groupId:'g',enabled:true,capabilities:['executor'],skillInstallation:{enabled:true,root:'/work/skills'}};
  context.machine=machine;context.state.agents=[machine];
  return {context,run,dialog,form,button,alert,listeners};
}
test('machine form restores saved settings and submits them with existing machine ID',async()=>{
  const h=setup();let sent;h.context.api=async(url,options)=>{sent={url,body:JSON.parse(options.body)}};
  h.run('openMachine(machine)');
  assert.match(h.dialog.innerHTML,/name="skillEnabled" checked/);
  assert.match(h.dialog.innerHTML,/value="\/work\/skills"/);
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.equal(sent.url,'/api/agents/machine-a');
  assert.equal(sent.body.remark,undefined);
  assert.equal(sent.body.workingDirectory,'C:\\work');
  assert.deepEqual(sent.body.skillInstallation,{enabled:true,root:'/work/skills'});
  assert.deepEqual(sent.body.mcpInstallation,{enabled:true,platform:'windows',installRoot:'C:\\deploy\\program'});
  assert.equal(h.dialog.closed,true);
});
test('failed save keeps the form and allows retry',async()=>{
  const h=setup();h.context.api=async()=>{throw new Error('目录无效')};h.run('openMachine(machine)');
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.equal(h.alert.textContent,'目录无效');assert.equal(h.button.disabled,false);assert.equal(h.dialog.closed,undefined);
});
test('group dialog stays independent of machine installation settings',()=>{
  const h=setup();h.run('openMachineGroup(null)');
  assert.match(h.dialog.innerHTML,/新建分组/);
  assert.doesNotMatch(h.dialog.innerHTML,/mcpEnabled/);
});
test('directory detection uses saved machine ID, blocks duplicate clicks, and releases failure state',async()=>{
  const h=setup();let reject,calls=0;h.context.api=(url,options)=>{calls++;assert.equal(url,'/api/skills/machines/machine-a/check');assert.equal(options.body,'{}');return new Promise((_,r)=>reject=r)};
  const button={dataset:{machineAction:'skill-check',id:'machine-a'}};
  const event={target:{closest:()=>button}};
  const first=h.listeners.click(event);await h.listeners.click(event);assert.equal(calls,1);
  reject(new Error('离线'));await first;assert.equal(button.disabled,false);assert.equal(h.run('pendingMachineActions.size'),0);
});
test('machine detail escapes persisted directory and diagnostic text',()=>{
  const h=setup();h.context.machine.skillInstallation={enabled:false,root:'<img>',checkMessage:'<script>'};
  const html=h.run('machineDetailReadOnly(machine)');assert.match(html,/未授权/);assert.match(html,/&lt;img>/);assert.doesNotMatch(html,/<script>/);
});

test('executor online status follows the saved connection test across page loads',()=>{
  for(const [testStatus,label] of [['passed','最近检测在线'],['failed','最近检测离线'],['untested','未检测']]){
    const h=setup();Object.assign(h.context.machine,{testStatus,testedAt:'2026-09-22T08:00:00Z'});
    const html=h.run('machineCard(machine,"executor")');
    assert.ok(html.includes(`>${label}</span>`));
    assert.match(h.run('machineDetailReadOnly(machine)'),/2026-09-22T08:00:00Z/);
  }
});

test('supervisor and dual-role cards retain runtime status independently of connection tests',()=>{
  for(const capabilities of [['supervisor'],['supervisor','executor']]){
    for(const role of capabilities){
      for(const [connectionStatus,testStatus,label] of [['offline','passed','离线'],['online','failed','在线'],['unknown','passed','在线状态未知']]){
        const h=setup();Object.assign(h.context.machine,{capabilities,connectionStatus,testStatus});
        h.context.role=role;
        assert.ok(h.run('machineCard(machine,role)').includes(`>${label}</span>`));
      }
    }
  }
});

test('existing machine dialog leaves remarks to the inline card editor',async()=>{
  const h=setup();h.context.machine.remark='<img>"';h.run('openMachine(machine)');
  assert.doesNotMatch(h.dialog.innerHTML,/name="remark"/);
  assert.match(h.run('machineCard(machine,"executor")'),/&lt;img>&quot;/);
  let sent;h.context.api=async(url,options)=>{sent=JSON.parse(options.body)};
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.equal(sent.remark,undefined);
});
test('new machines can still include a remark at creation',async()=>{
  const h=setup();h.run('openMachine(null)');let sent;
  h.context.api=async(url,options)=>{sent=JSON.parse(options.body)};
  await h.form.onsubmit({preventDefault(){},target:h.form});assert.equal(sent.remark,'测试机');
});

test('remark edits preserve legacy MCP settings when installation controls are unchanged',async()=>{
  const h=setup();h.context.machine.mcpInstallation={enabled:true,programRoot:'C:\\legacy',temporaryRoot:'D:\\temp',runtimes:['C:\\python.exe']};
  h.form.elements.mcpRoot.value='C:\\legacy';h.run('openMachine(machine)');let sent;
  h.context.api=async(url,options)=>{sent=JSON.parse(options.body)};
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.equal(sent.remark,undefined);assert.equal(sent.mcpInstallation,undefined);
});

function inlineFixture(){
  const h=setup();h.context.machine.remark='旧备注';
  const rows=[0,1].map(()=>{
    const input={dataset:{machineRemark:'machine-a'},value:'旧备注',matches:s=>s==='[data-machine-remark]',setAttribute(k,v){this[k]=v},blur(){return h.listeners.focusout({target:this})}};
    const status={};return {dataset:{machineRemarkRow:'machine-a'},input,status,querySelector:s=>s==='input'?input:status};
  });
  h.context.document.querySelectorAll=()=>rows;
  return {...h,rows,input:rows[0].input};
}
test('blur saves only remark once and synchronizes both role cards',async()=>{
  const h=inlineFixture();let resolve,calls=0;
  h.context.api=(url,options)=>{calls++;assert.equal(url,'/api/agents/machine-a/remark');assert.deepEqual(JSON.parse(options.body),{remark:'新备注'});return new Promise(r=>resolve=r)};
  h.input.value=' 新备注 ';const pending=h.listeners.focusout({target:h.input});
  await h.listeners.focusout({target:h.input});assert.equal(calls,1);assert.equal(h.rows[1].input.disabled,true);
  resolve({remark:'新备注'});await pending;
  assert.equal(h.context.machine.remark,'新备注');
  for(const row of h.rows){assert.equal(row.input.value,'新备注');assert.equal(row.input.disabled,false);assert.equal(row.status.textContent,'')}
});
test('unchanged and invalid remarks do not submit; empty text clears',async()=>{
  const h=inlineFixture();let calls=0;h.context.api=async(url,options)=>{calls++;return JSON.parse(options.body)};
  await h.listeners.focusout({target:h.input});assert.equal(calls,0);
  h.input.value='字'.repeat(101);await h.listeners.focusout({target:h.input});assert.equal(calls,0);assert.match(h.rows[0].status.textContent,/尚未保存/);
  h.input.value=' ';await h.listeners.focusout({target:h.input});assert.equal(calls,1);assert.equal(h.context.machine.remark,'');
});
test('save failure retains draft across redraw and retries on next blur',async()=>{
  const h=inlineFixture();h.context.api=async()=>{throw Error('离线')};h.input.value='待保存';
  await h.listeners.focusout({target:h.input});assert.equal(h.context.machine.remark,'旧备注');assert.equal(h.input.value,'待保存');assert.match(h.rows[0].status.textContent,/保存失败/);
  assert.match(h.run('machineRemarkHtml(machine)'),/value="待保存"/);
  h.context.api=async()=>({remark:'待保存'});await h.listeners.focusout({target:h.input});assert.equal(h.context.machine.remark,'待保存');
});
test('Escape cancels a draft without sending and Enter blurs to save',async()=>{
  const h=inlineFixture();let calls=0;h.context.api=async()=>{calls++;return {remark:'确认'}};
  h.input.value='取消';h.listeners.input({target:h.input});
  h.listeners.keydown({target:h.input,key:'Escape',preventDefault(){}});assert.equal(calls,0);assert.equal(h.input.value,'旧备注');
  h.input.value='确认';h.listeners.keydown({target:h.input,key:'Enter',preventDefault(){}});assert.equal(calls,1);
});

test('business delete confirms, deduplicates requests and refreshes on success',async()=>{
  const h=setup();h.context.confirm=()=>true;let resolve,calls=0,refresh=0;
  h.context.api=(url,options)=>{calls++;assert.equal(url,'/api/agents/machine-a');assert.equal(options.method,'DELETE');return new Promise(r=>resolve=r)};
  h.context.render=async()=>{refresh++};
  const button={dataset:{machineAction:'delete',id:'machine-a'}},event={target:{closest:()=>button}};
  assert.match(h.run('machineDetailActions(machine)'),/data-machine-action="delete"/);
  const pending=h.listeners.click(event);await h.listeners.click(event);assert.equal(calls,1);
  resolve({deleted:true});await pending;assert.equal(refresh,1);assert.equal(button.disabled,false);
});
test('cancelled or rejected machine deletion leaves the card available',async()=>{
  const h=setup();let calls=0,refresh=0,message='';h.context.confirm=()=>false;
  h.context.api=async()=>{calls++;throw Error('机器被 SOP 引用')};h.context.render=async()=>refresh++;h.context.toast=text=>message=text;
  const button={dataset:{machineAction:'delete',id:'machine-a'}},event={target:{closest:()=>button}};
  await h.listeners.click(event);assert.equal(calls,0);
  h.context.confirm=()=>true;await h.listeners.click(event);assert.equal(calls,1);assert.equal(refresh,0);assert.equal(button.disabled,false);assert.match(message,/引用/);
});

test('supervisor role hides group and clears execution installation authorizations',async()=>{
  const h=setup();h.form.elements.machineRole.value='supervisor';h.run('openMachine(machine)');
  assert.equal(h.form.elements.groupId.disabled,true);assert.equal(h.alert.hidden,true);
  let sent;h.context.api=async(url,options)=>{sent=JSON.parse(options.body)};
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.deepEqual(sent.capabilities,['supervisor']);assert.equal(sent.groupId,null);
  assert.equal(sent.skillInstallation.enabled,false);assert.equal(sent.mcpInstallation.enabled,false);
  h.form.elements.machineRole.value='executor';h.form.elements.machineRole.onchange();
  assert.equal(h.form.elements.groupId.disabled,false);assert.equal(h.alert.hidden,false);
});
