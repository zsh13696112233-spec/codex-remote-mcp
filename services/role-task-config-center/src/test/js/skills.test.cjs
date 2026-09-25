const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../../main/resources/static/skills.js'),'utf8');
function setup(){
  const nodes=new Map();const listeners={};const storage=new Map();
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',showModal(){this.open=true},close(){this.open=false},classList:{add(){}},closest(){return this}});return nodes.get(selector)};
  const context=vm.createContext({crypto:webcrypto,Uint8Array,console,state:{page:'skills'},pageRenderVersion:1,
    document:{hidden:false,addEventListener:(event,handler)=>listeners[event]=handler,querySelector:node,querySelectorAll:()=>[]},
    groupState:{selected:''},concreteGroup:()=> 'g',groupName:id=>id,groupOptions:()=>'',chooseGroup:fn=>fn('g'),loadGroups:async()=>{},renderGroupFilter(){},navigator:{},setInterval(){},$:node,esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    time:v=>v,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    loadGatewayReady:async()=>{},api:async()=>({deployments:[]})});
  vm.runInContext(source,context);return {context,nodes,node,listeners,storage,run:code=>vm.runInContext(code,context)};
}
test('HTTP UUID and uncertain submit reuse request ID',()=>{
  const h=setup();const a=h.run('skillPending("pending",{packageId:"one",agentIds:["a"]})');
  assert.match(a,/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(h.run('skillPending("pending",{packageId:"one",agentIds:["a"]})'),a);
  assert.notEqual(h.run('skillPending("pending",{packageId:"two",agentIds:["a"]})'),a);
});
test('renders package data as text and distinguishes script environment',()=>{
  const h=setup();h.run('skillView.packageId="a";skillView.packages=[{id:"a",name:"demo",description:"<img onerror=bad>",fileCount:2,size:20,hasScripts:true}];renderSkillDetails()');
  h.run('renderSkillPackages()');
  assert.match(h.node('#skillPackages').innerHTML,/&lt;img/);
  assert.doesNotMatch(h.node('#skillPackages').innerHTML,/<img/);
  assert.match(h.node('#skillDetails').innerHTML,/运行环境未验证/);
});
test('late page response cannot replace another page',async()=>{
  const h=setup();
  // One shared response resolves all pending API reads.
  let resolve;const pending=new Promise(r=>resolve=r);h.context.api=()=>pending;
  const render=h.run('renderSkills()');h.context.state.page='roles';h.node('#content').innerHTML='roles';
  resolve({skills:[],agents:[],groups:[],deployments:[]});await render;
  assert.equal(h.node('#content').innerHTML,'roles');
});
test('polling history preserves package and machine selection controls',async()=>{
  const h=setup();h.node('#skillMachines').innerHTML='selected machine';
  h.run('skillView.selected.add("a");skillView.packageId="p"');
  await h.run('pollSkillHistory()');
  assert.equal(h.node('#skillMachines').innerHTML,'selected machine');
  assert.equal(h.run('skillView.selected.has("a")'),true);
  assert.equal(h.run('skillView.packageId'),'p');
});
test('ineligible targets excluded and pending request survives failure',async()=>{
  const h=setup();let sent;
  h.run('skillView.deployGroup="g";skillView.packageId="p";skillView.machines=[{agentId:"a",groupId:"g",eligible:true},{agentId:"b",groupId:"g",eligible:false}];skillView.selected=new Set(["b","a"])');
  h.context.api=async(url,options)=>{if(options){sent=JSON.parse(options.body);throw new Error('network failed')}return {deployments:[]}};
  const button={dataset:{skill:'deploy'}};
  await h.listeners.click({target:{closest:()=>button}});
  assert.deepEqual(sent.agentIds,['a']);
  assert.equal(JSON.parse(h.storage.get('skillPending')).requestId,sent.requestId);
  assert.equal(h.run('skillView.busy'),false);
});
test('clipboard unavailable provides manual reference',async()=>{
  const h=setup();const button={dataset:{skill:'copy',name:'sales-report'}};
  await h.listeners.click({target:{closest:()=>button}});
  assert.match(h.node('#skillMessage').textContent,/\$sales-report/);
});
test('selected older package is restored without silently selecting another',async()=>{
  const h=setup();h.run('skillView.packageId="old"');
  h.context.api=async url=>{
    if(url==='/api/skills')return {skills:[],enabled:true};
    if(url==='/api/skills/machines')return {agents:[],groups:[]};
    if(url==='/api/skills/old')return {id:'old',name:'older-skill',description:'Older',fileCount:1,size:10};
    return {deployments:[]};
  };
  await h.run('renderSkills()');
  assert.equal(h.run('skillView.packageId'),'old');
  assert.match(h.node('#skillPackages').innerHTML,/older-skill/);
  assert.doesNotMatch(h.node('#content').innerHTML,/id="skillPackage"|一个 ZIP 包含一个 Skill/);
});

test('package cards select the package used for deployment',async()=>{
  const h=setup();h.run('skillView.packages=[{id:"one",name:"first"},{id:"two",name:"second"}]');
  await h.listeners.click({target:{closest:()=>({dataset:{skill:'package',id:'two'}})}});
  assert.equal(h.run('skillView.packageId'),'two');
  assert.match(h.node('#skillDetailDialog').innerHTML,/second/);
});
test('machine inventory switches machine and keeps package selection on polling',async()=>{
  const h=setup();h.run('skillView.tab="machines";skillView.packageId="p";skillView.machines=[{agentId:"a",name:"A",capabilities:["executor"]},{agentId:"b",name:"B",capabilities:["executor"]}];skillView.inventory=[{agentId:"a",name:"skill-a",state:"completed"},{agentId:"b",name:"skill-b",state:"failed",error:"<error>"}];renderSkillInventory()');
  assert.match(h.node('#skillInventory').innerHTML,/skill-a/);
  await h.listeners.click({target:{closest:()=>({dataset:{skill:'machine',id:'b'}})}});
  assert.match(h.node('#skillInventory').innerHTML,/skill-b/);assert.doesNotMatch(h.node('#skillInventory').innerHTML,/skill-a/);
  assert.match(h.node('#skillInventory').innerHTML,/&lt;error&gt;/);
  h.context.api=async url=>url.endsWith('inventory')?{items:[{agentId:'b',name:'skill-b',state:'completed'}]}:{deployments:[]};
  await h.run('pollSkillHistory()');assert.equal(h.run('skillView.machineId'),'b');assert.equal(h.run('skillView.packageId'),'p');
  assert.match(h.node('#skillInventory').innerHTML,/复制引用/);
});
test('changing group clears hidden deployment targets and renders machine empty state',()=>{
  const h=setup();h.run('skillView.selected.add("outside")');
  h.run('resetSkillSelection();renderSkillInventory()');
  assert.equal(h.run('skillView.selected.size'),0);assert.match(h.node('#skillMachineNav').innerHTML,/暂无执行机/);
});

test('upload opens a modal and file selection updates the filename as text',async()=>{
  const h=setup(),dialog=h.node('#skillUploadDialog');
  dialog.querySelector=h.node;dialog.showModal=()=>{dialog.open=true};dialog.close=()=>{dialog.open=false};
  await h.listeners.click({target:{closest:()=>({dataset:{skill:'open-upload'}})}});
  assert.equal(dialog.open,true);assert.match(dialog.innerHTML,/选择 ZIP 文件/);
  h.node('#skillZip').onchange({target:{files:[{name:'<demo>.zip'}]}});
  assert.equal(h.node('#skillFileName').textContent,'<demo>.zip');
  h.node('[data-skill-upload-close]').onclick();assert.equal(dialog.open,false);
});

test('failed modal upload preserves file and prevents duplicate upload while pending',async()=>{
  const h=setup();const file={name:'demo.zip',size:100};h.node('#skillZip').files=[file];
  let reject,calls=0;h.context.api=(url,options)=>{if(options){calls++;return new Promise((_,r)=>reject=r)}return Promise.resolve({deployments:[],items:[]})};
  const event={target:{closest:()=>({dataset:{skill:'upload'}})}};
  const pending=h.listeners.click(event);await h.listeners.click(event);assert.equal(calls,1);
  reject(new Error('ZIP 损坏'));await pending;
  assert.equal(h.node('#skillUploadError').textContent,'ZIP 损坏');assert.equal(h.node('#skillZip').files[0],file);assert.equal(h.run('skillView.busy'),false);
});

test('successful modal upload closes dialog and selects the uploaded package',async()=>{
  const h=setup();h.node('#skillZip').files=[{name:'demo.zip',size:100}];let closed=false;h.node('#skillUploadDialog').close=()=>{closed=true};
  h.context.api=async(url,options)=>{
    if(options)return {id:'new'};
    if(url==='/api/skills')return {skills:[{id:'new',name:'demo',fileCount:1,size:100}],enabled:true};
    if(url==='/api/skills/machines')return {agents:[],groups:[]};
    return {deployments:[],items:[]};
  };
  await h.listeners.click({target:{closest:()=>({dataset:{skill:'upload'}})}});
  assert.equal(closed,true);assert.equal(h.run('skillView.packageId'),'new');assert.match(h.node('#skillMessage').textContent,/上传校验通过/);
});

test('all view shows memberships and name or description search',()=>{
  const h=setup();h.run(`skillView.packages=[{id:'a',name:'alpha',description:'日报',groups:[{id:'g',name:'甲组'},{id:'h',name:'乙组'}]},{id:'b',name:'beta',description:'其他'}];renderSkillPackages()`);
  assert.match(h.node('#skillPackages').innerHTML,/甲组 · 乙组/);
  assert.match(h.node('#skillPackages').innerHTML,/未归组/);
  h.listeners.input({target:{id:'skillSearch',value:'日报'}});
  assert.match(h.node('#skillPackages').innerHTML,/alpha/);
  assert.doesNotMatch(h.node('#skillPackages').innerHTML,/beta/);
});

test('deployment group scopes targets and restores only matching uncertain request',()=>{
  const h=setup();h.run(`skillView.packages=[{id:'p',name:'demo',groups:[{id:'g',name:'甲'},{id:'h',name:'乙'}]}];skillView.machines=[{agentId:'a',name:'A',groupId:'g',eligible:true,capabilities:['executor']},{agentId:'b',name:'B',groupId:'h',eligible:true,capabilities:['executor']}];skillView.groupId='g'`);
  h.storage.set('skillPending',JSON.stringify({requestId:'old',payload:{packageId:'p',groupId:'h',agentIds:['b']}}));
  h.run(`openSkillDeploy('p')`);
  assert.equal(h.run('skillView.deployGroup'),'g');assert.equal(h.run('skillView.selected.size'),0);
  assert.match(h.node('#skillMachines').innerHTML,/data-skill-machine="a"/);
  assert.doesNotMatch(h.node('#skillMachines').innerHTML,/data-skill-machine="b"/);
});

test('batch assignment appends memberships and uses selected IDs',async()=>{
  const h=setup();h.run(`skillView.checked=new Set(['a','b'])`);h.node('#skillAssignGroup').value='g';let sent;
  h.context.api=async(url,options)=>{if(options){sent={url,body:JSON.parse(options.body)};return {accepted:true}}return {skills:[],agents:[],groups:[],deployments:[],items:[]}};
  await h.listeners.click({target:{closest:()=>({dataset:{skill:'assign'}})}});
  assert.deepEqual(sent,{url:'/api/skills/groups/assign',body:{groupId:'g',packageIds:['a','b']}});
  assert.equal(h.run('skillView.checked.size'),0);
});

test('late group response cannot overwrite new group page',async()=>{
  const h=setup();h.context.groupState.selected='g';let resolve;
  h.context.api=()=>new Promise(r=>{if(!resolve)resolve=[];resolve.push(r)});
  const pending=h.run('renderSkills()');h.context.pageRenderVersion++;h.context.groupState.selected='h';h.node('#content').innerHTML='乙组';
  for(const r of resolve)r({skills:[],agents:[],groups:[],deployments:[],items:[]});
  await pending;assert.equal(h.node('#content').innerHTML,'乙组');
});

test('upload binds selected group and does not overwrite navigation after response',async()=>{
  const h=setup();h.run(`skillView.uploadGroup='g'`);h.node('#skillZip').files=[{name:'demo.zip',size:10}];let resolve,url;
  h.context.api=(u)=>{url=u;return new Promise(r=>resolve=r)};
  const pending=h.listeners.click({target:{closest:()=>({dataset:{skill:'upload'}})}});
  h.context.pageRenderVersion++;h.context.groupState.selected='h';h.node('#content').innerHTML='乙组';resolve({id:'p'});await pending;
  assert.equal(url,'/api/skills?groupId=g');assert.equal(h.node('#content').innerHTML,'乙组');
});

test('refresh restores an older pending package within its group without selecting hidden targets',async()=>{
  const h=setup();h.context.groupState.selected='g';
  h.storage.set('skillPending',JSON.stringify({requestId:'old-request',payload:{packageId:'old',groupId:'g',agentIds:['a']}}));
  h.context.api=async url=>{
    if(url==='/api/skills/old')return {id:'old',name:'历史包',groups:[{id:'g',name:'甲组'}]};
    return {skills:[],agents:[],groups:[],items:[],deployments:[]};
  };
  await h.run('renderSkills()');
  assert.equal(h.run('skillView.packageId'),'old');assert.equal(h.run('skillView.selected.size'),0);
  assert.match(h.node('#skillPackages').innerHTML,/历史包/);assert.match(h.node('#skillMessage').textContent,/尚未确认/);
  h.run(`openSkillDeploy('old')`);assert.equal(h.run(`skillView.selected.has('a')`),true);
});

test('late polling error is discarded after group changes',async()=>{
  const h=setup();h.run(`skillView.tab='machines'`);let reject;
  h.context.api=()=>new Promise((_,r)=>reject=r);
  const pending=h.run('pollSkillHistory()');h.context.pageRenderVersion++;h.node('#skillMessage').textContent='新组';
  reject(new Error('旧错误'));await pending;assert.equal(h.node('#skillMessage').textContent,'新组');
});
