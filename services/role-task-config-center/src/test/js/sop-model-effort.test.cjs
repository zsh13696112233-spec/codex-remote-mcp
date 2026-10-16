const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../main/resources/static/app.js'),'utf8');
function fixture(){
  const control={innerHTML:''},calls=[];
  let response={models:[{model:'gpt-6.1-sol',supportedReasoningEfforts:['low','medium','high','xhigh','max','ultra']}]};
  const context=vm.createContext({state:{page:'sops',sop:{tab:'workflow',draft:{defaultStepModel:'gpt-6.1-sol',steps:[{agentId:'worker'}]}}},esc:x=>String(x??''),document:{querySelector:s=>s.includes('workflow')?control:null},api:async url=>{calls.push(url);if(response instanceof Error)throw response;return response}});
  vm.runInContext(source.slice(source.indexOf('const MODELS='),source.indexOf('const PERMISSION_LABELS=')),context);
  vm.runInContext(source.slice(source.indexOf('const effortCatalogs ='),source.indexOf('let instructionEdit =')),context);
  return {control,calls,run:code=>vm.runInContext(code,context),respond:value=>response=value};
}
test('only two current models, full labels, legacy selection remains explicit',()=>{
  const f=fixture(),html=f.run('modelOptionsHtml("gpt-6.1-sol")');
  assert.match(html,/selected>GPT-6.1 Sol/);
  assert.match(html,/>GPT-6 Astra/);
  assert.doesNotMatch(html,/5\.6|gpt-6-sol/);
  assert.match(f.run('modelOptionsHtml("gpt-5.6-sol")'),/selected disabled>gpt-5.6-sol（已停用，请重新选择）/);
});
test('actual capabilities appear with Chinese labels and preserve overrides',async()=>{
  const f=fixture();await f.run('refreshEffortControls()');
  assert.match(f.control.innerHTML,/value="low" >轻度/);
  assert.match(f.control.innerHTML,/value="medium" >标准/);
  assert.match(f.control.innerHTML,/value="ultra" >最高/);
  assert.doesNotMatch(f.control.innerHTML,/value="none"/);
  f.run('state.sop.draft.steps[0].reasoningEffortOverride="high"');
  assert.match(f.run('effortControlHtml(state.sop.draft.steps[0])'),/value="high" selected/);
});
test('missing model and empty capabilities have distinct explanations',async()=>{
  const f=fixture();f.respond({models:[{model:'gpt-5.6-sol',supportedReasoningEfforts:['high']}]});
  await f.run('refreshEffortControls()');
  assert.match(f.control.innerHTML,/执行机未提供所选模型/);
  assert.doesNotMatch(f.control.innerHTML,/value="high"/);
  f.respond({models:[{model:'gpt-6.1-sol',supportedReasoningEfforts:[]}]});
  await f.run('effortCatalogs.clear();refreshEffortControls()');
  assert.match(f.control.innerHTML,/所选模型未提供可选思考强度/);
});
test('failed request retains selection and refreshing recovers current capabilities',async()=>{
  const f=fixture();f.run('state.sop.draft.defaultStepReasoningEffort="high"');
  f.respond(Error('offline'));await f.run('refreshEffortControls()');
  assert.match(f.control.innerHTML,/加载失败/);
  assert.match(f.control.innerHTML,/高（待验证）/);
  f.respond({models:[{model:'gpt-6.1-sol',supportedReasoningEfforts:['high']}]});
  await f.run('effortCatalogs.clear();refreshEffortControls()');
  assert.equal(f.calls.length,2);
  assert.match(f.control.innerHTML,/value="high" selected>高/);
  assert.doesNotMatch(f.control.innerHTML,/待验证|加载失败/);
  assert.match(source,/if\(state.page==="sops"\)\{effortCatalogs.clear\(\);const id=/);
});
test('empty workflow and missing executor explain setup; model switches recompute options',async()=>{
  const f=fixture();await f.run('refreshEffortControls()');
  f.run('state.sop.draft.defaultStepModel="gpt-6-astra"');
  assert.match(f.run('effortControlHtml()'),/执行机未提供所选模型/);
  f.run('state.sop.draft.steps[0].agentId=""');
  assert.match(f.run('effortControlHtml()'),/请先选择步骤执行机/);
  f.run('state.sop.draft.steps=[]');
  assert.match(f.run('effortControlHtml()'),/添加步骤后可选择/);
});
