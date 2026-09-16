const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('src/main/resources/static/machines.js','utf8');
function setup(){
  const listeners={},button={},alert={};
  const form={elements:{ip:{value:'192.0.2.1'},port:{value:'4500'},groupId:{value:'g'},supervisor:{checked:false},executor:{checked:true},skillEnabled:{checked:true},skillRoot:{value:' /work/skills '}},querySelector:s=>s==='[type=submit]'?button:alert};
  const dialog={innerHTML:'',querySelector:s=>s==='form'?form:{},showModal(){},close(){this.closed=true}};
  const context=vm.createContext({document:{querySelector:()=>dialog,querySelectorAll:()=>[],addEventListener:(name,fn)=>listeners[name]=fn},state:{agents:[]},concreteGroup:()=> 'g',esc:v=>String(v??'').replaceAll('<','&lt;').replaceAll('"','&quot;'),time:v=>v,status:()=>'',groupMark:()=>'',render:async()=>{},toast:()=>{},api:async()=>({})});
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
  assert.deepEqual(sent.body.skillInstallation,{enabled:true,root:'/work/skills'});
  assert.equal(h.dialog.closed,true);
});
test('failed save keeps the form and allows retry',async()=>{
  const h=setup();h.context.api=async()=>{throw new Error('目录无效')};h.run('openMachine(machine)');
  await h.form.onsubmit({preventDefault(){},target:h.form});
  assert.equal(h.alert.textContent,'目录无效');assert.equal(h.button.disabled,false);assert.equal(h.dialog.closed,undefined);
});
test('directory detection uses saved machine ID, blocks duplicate clicks, and releases failure state',async()=>{
  const h=setup();let reject,calls=0;h.context.api=(url,options)=>{calls++;assert.equal(url,'/api/skills/machines/machine-a/check');assert.equal(options.body,'{}');return new Promise((_,r)=>reject=r)};
  const button={dataset:{machineAction:'skill-check',id:'machine-a'}};
  const event={target:{closest:()=>button}};
  const first=h.listeners.click(event);await h.listeners.click(event);assert.equal(calls,1);
  reject(new Error('离线'));await first;assert.equal(button.disabled,false);assert.equal(h.run('pendingMachineActions.size'),0);
});
test('machine card escapes persisted directory and diagnostic text',()=>{
  const h=setup();h.context.machine.skillInstallation={enabled:false,root:'<img>',checkMessage:'<script>'};
  const html=h.run('machineCard(machine,"executor")');assert.match(html,/未授权/);assert.match(html,/&lt;img>/);assert.doesNotMatch(html,/<script>/);
});
