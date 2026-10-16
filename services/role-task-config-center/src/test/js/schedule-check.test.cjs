const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function fixture({machineError=false}={}){
  const fields=['name','sopId','taskDefinitionId','mode','dailyTime','intervalMinutes','enabled','checkEnabled','checkScriptPath','checkArguments','checkTimeout','checkAgentId','groupId'];
  const element=()=>({value:'',innerHTML:'',checked:false,disabled:false,required:false,textContent:'',classList:{toggle(){},remove(){}},insertAdjacentHTML(where,text){this.innerHTML+=text}});
  const controls=Object.fromEntries(fields.map(name=>[name,element()]));
  const nodes=new Map();
  const form={...controls,elements:controls,querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector)}};
  const dialog={innerHTML:'',querySelector(){return form},showModal(){},close(){}};
  const content=element(),requests=[];
  const context=vm.createContext({document:{addEventListener(){}},$:selector=>selector==='#scheduleDialog'?dialog:content,
    esc:x=>String(x).replaceAll('<','&lt;'),state:{page:'schedules'},groupState:{selected:'g'},status:()=>'',groupMark:()=>'',
    bindGroupForm(f,x,change){f.elements.groupId.value=x.groupId||'g';change(f.elements.groupId.value)},
    api:async(url,options)=>{requests.push({url,options});if(options)return {};
      if(url==='/api/sops')return [{id:'s',name:'SOP',groupId:'g',enabled:true}];
      if(url==='/api/task-definitions')return [{id:'t',sopId:'s',name:'任务',groupId:'g',enabled:true}];
      if(url==='/api/agents'){if(machineError)throw Error('offline');return {agents:[{agentId:'a',groupId:'g',capabilities:['executor'],enabled:true,remark:'<机器>'},{agentId:'other',groupId:'other',capabilities:['executor'],enabled:true}]}}
      return [];
    },toast(){},render:async()=>{},URL,console});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../main/resources/static/task-schedules.js'),'utf8'),context);
  return {form,nodes,requests,run:code=>vm.runInContext(code,context)};
}

test('precheck edit restores configuration, scopes machines and saves separate path and arguments',async()=>{
  const {form,requests,run}=fixture();
  await run(`openSchedule({id:'r',name:'检查',groupId:'g',sopId:'s',taskDefinitionId:'t',mode:'interval',intervalMinutes:5,preCheck:{enabled:true,agentId:'a',scriptPath:'E:/Agent Bug/check.bat',arguments:'-user "Plan Agent"',timeoutSeconds:60}})`);
  await Promise.resolve();
  assert.equal(form.checkEnabled.checked,true);
  assert.equal(form.checkAgentId.value,'a');
  assert.match(form.checkAgentId.innerHTML,/&lt;机器>/);
  assert.doesNotMatch(form.checkAgentId.innerHTML,/value="other"/);
  await form.onsubmit({preventDefault(){}});
  const payload=JSON.parse(requests.find(x=>x.options)?.options.body);
  assert.equal(payload.preCheck.scriptPath,'E:/Agent Bug/check.bat');
  assert.equal(payload.preCheck.arguments,'-user "Plan Agent"');
  assert.equal(payload.preCheck.timeoutSeconds,60);
});

test('disabled checks do not load machines or block ordinary saves',async()=>{
  const {form,requests,run}=fixture({machineError:true});
  await run(`openSchedule({name:'旧任务',groupId:'g',sopId:'s',taskDefinitionId:'t',mode:'interval',intervalMinutes:5})`);
  assert.equal(form.checkAgentId.disabled,true);
  assert.equal(requests.some(x=>x.url==='/api/agents'),false);
  await form.onsubmit({preventDefault(){}});
  assert.equal(JSON.parse(requests.find(x=>x.options).options.body).preCheck.enabled,false);
});

test('machine loading errors remain actionable and prevent enabled-check save',async()=>{
  const {form,nodes,requests,run}=fixture({machineError:true});
  await run(`openSchedule({groupId:'g',preCheck:{enabled:true,agentId:'a'}})`);
  await Promise.resolve();
  await form.onsubmit({preventDefault(){}});
  assert.match(nodes.get('[data-check-machines-error]').textContent,/加载失败/);
  assert.match(nodes.get('[data-schedule-error]').textContent,/加载/);
  assert.equal(requests.some(x=>x.options),false);
});

test('latest persisted result is rendered with reason and Beijing time',()=>{
  const {run}=fixture();
  const text=run(`scheduleCheckSummary({preCheck:{enabled:true},lastCheckAt:'2026-10-16T00:00:00Z',lastCheckStatus:'skipped',lastCheckReason:'条件不满足'})`);
  assert.match(text,/08:00:00/);assert.match(text,/条件不满足/);
});
