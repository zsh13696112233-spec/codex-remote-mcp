const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../../main/resources/static');

function fixture() {
  const handlers={},calls=[],storage=new Map(),panel={isConnected:true,innerHTML:''};
  const context=vm.createContext({
    document:{querySelector:()=>panel,addEventListener:(name,fn)=>handlers[name]=fn},
    state:{dingtalkTargets:[],tasks:[]},esc:String,time:()=>'',targetTypeLabel:()=> '人员',
    blockedGroupOptions:()=>'',toast(){},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    api:async(url,options)=>{calls.push({url,options});return {templateId:'test.schema'};},
  });
  vm.runInContext(fs.readFileSync(path.join(root,'blocked-notifications.js'),'utf8'),context);
  return {context,handlers,calls,storage,panel};
}

test('person cards retain controls without a Jira mapping entry',()=>{
  const {context}=fixture();
  const app=fs.readFileSync(path.join(root,'app.js'),'utf8');
  const start=app.indexOf('function targetCard(');
  const end=app.indexOf('\nfunction ',start+1);
  vm.runInContext(app.slice(start,end),context);
  const html=vm.runInContext('targetCard({id:"p",targetType:"PERSON",displayName:"开发人",available:true,enabled:true})',context);
  assert.match(html,/data-target-test/);
  assert.match(html,/data-target-enabled/);
  assert.doesNotMatch(html,/data-jira-mapping|Jira 开发人映射/);
});

test('configuration explains name matching and still offers group notification tests',async()=>{
  const {context,panel}=fixture();
  context.state.dingtalkTargets=[{id:'disabled-person',targetType:'PERSON',displayName:'开发人',enabled:false,available:true},{id:'gone-person',targetType:'PERSON',displayName:'离职人员',enabled:true,available:false}];
  await vm.runInContext('loadBlockedConfig()',context);
  assert.match(panel.innerHTML,/全自动成功完成或业务阻断/);
  assert.match(panel.innerHTML,/显示名精确匹配/);
  assert.match(panel.innerHTML,/不受人员启用开关影响/);
  assert.match(panel.innerHTML,/value="disabled-person"/);
  assert.doesNotMatch(panel.innerHTML,/gone-person/);
  assert.match(panel.innerHTML,/data-blocked-test/);
  assert.doesNotMatch(panel.innerHTML,/维护.*映射/);
});

test('test card submission retains its request ID and recovers after an error',async()=>{
  const {context,handlers,calls,storage}=fixture();
  storage.set('blocked-test-id','existing-request');
  const button={disabled:false},output={innerHTML:'',textContent:''};
  const form={dataset:{},groupId:{value:'g'},personId:{value:'p'},text:{value:'测试'},
    matches:s=>s.includes('[data-blocked-test]'),
    querySelector:s=>s==='[type="submit"]'?button:output};
  const event={target:form,preventDefault(){}};
  await handlers.submit(event);await handlers.submit(event);
  assert.equal(calls.length,2);
  assert.equal(JSON.parse(calls[0].options.body).requestId,'existing-request');
  assert.equal(calls[0].options.body,calls[1].options.body);
  context.api=async()=>{throw Error('暂不可用');};
  await handlers.submit(event);
  assert.equal(output.textContent,'暂不可用');
  assert.equal(button.disabled,false);
  assert.equal(form.dataset.saving,undefined);
  assert.equal(storage.get('blocked-test-id'),'existing-request');
});
