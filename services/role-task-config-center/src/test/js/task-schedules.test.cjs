const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function fixture({machineError=false}={}){
  const fields=['name','sopId','taskDefinitionId','mode','dailyTime','intervalMinutes','enabled','groupId'];
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
  return {dialog,form,nodes,requests,run:code=>vm.runInContext(code,context)};
}

test('editing legacy schedule removes check controls and saves without machine lookup',async()=>{
  const {form,requests,run}=fixture({machineError:true});
  await run(`openSchedule({id:'r',name:'旧规则',groupId:'g',sopId:'s',taskDefinitionId:'t',mode:'interval',intervalMinutes:5,preCheck:{enabled:true,agentId:'busy'}})`);
  assert.equal(form.sopId.disabled,true);
  assert.equal(form.taskDefinitionId.disabled,true);
  await form.onsubmit({preventDefault(){}});
  const payload=JSON.parse(requests.find(x=>x.options).options.body);
  assert.equal(payload.intervalMinutes,5);
  assert.equal(payload.taskDefinitionId,'t');
  assert.equal('preCheck' in payload,false);
  assert.equal(requests.some(x=>x.url==='/api/agents'),false);
});

test('new daily schedule preserves time controls without check fields',async()=>{
  const {form,requests,run}=fixture();
  await run(`openSchedule({name:'每日任务',groupId:'g',sopId:'s',taskDefinitionId:'t',mode:'daily',dailyTime:'09:00'})`);
  assert.equal(form.dailyTime.required,true);
  assert.equal(form.intervalMinutes.disabled,true);
  await form.onsubmit({preventDefault(){}});
  const payload=JSON.parse(requests.find(x=>x.options).options.body);
  assert.equal(payload.dailyTime,'09:00');
  assert.equal(payload.intervalMinutes,null);
  assert.equal('preCheck' in payload,false);
});


test('one minute interval can be edited and submitted',async()=>{
  const {dialog,form,requests,run}=fixture();
  await run(`openSchedule({id:'r',name:'每分钟',groupId:'g',sopId:'s',taskDefinitionId:'t',mode:'interval',intervalMinutes:1})`);
  assert.match(dialog.innerHTML,/name="intervalMinutes"[^>]*min="1"/);
  await form.onsubmit({preventDefault(){}});
  const payload=JSON.parse(requests.find(x=>x.options).options.body);
  assert.equal(payload.intervalMinutes,1);
});
