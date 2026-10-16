const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../main/resources/static/app.js'),'utf8');
function fixture(){
  const role={id:'role',name:'角色',duty:'角色提示词',enabled:true};
  const state={sop:{draft:{steps:[]}}},handlers={};
  const textarea={value:'',focus(){},addEventListener:(event,fn)=>handlers[event]=fn},button={focus(){}};
  const label={textContent:'',className:''},reset={hidden:true,addEventListener:(event,fn)=>handlers.reset=fn};
  const dialog={querySelector:s=>s==='textarea'?textarea:s==='[data-instruction-source]'?label:s==='[data-instruction-reset]'?reset:{textContent:''},showModal(){},close(){},addEventListener:(event,fn)=>handlers[event]=fn};
  const form={addEventListener:(event,fn)=>handlers[event]=fn};
  const context=vm.createContext({state,roleById:()=>role,selectedStep:()=>state.sop.draft.steps[0],suggestedAgents:()=>[],normalizeStep:s=>({...s,_clientId:'step'}),renderSopWorkspace(){},syncDirtyUi(){},toast:message=>{throw Error(message)},document:{querySelector:s=>s==='#instructionDialog'?dialog:s==='#instructionForm'?form:s==='#instructionText'?textarea:s==='[data-instruction-reset]'?reset:button,addEventListener(){}}});
  vm.runInContext(source.slice(source.indexOf('function addRoleNode('),source.indexOf('function moveNode(')),context);
  vm.runInContext(source.slice(source.indexOf('let instructionEdit =')),context);
  return {role,state,textarea,label,reset,input:()=>handlers.input(),restore:()=>handlers.reset(),cancel:()=>handlers.close(),run:s=>vm.runInContext(s,context),save:()=>handlers.submit({preventDefault(){},currentTarget:{querySelector:()=>textarea}})};
}
test('drag inherits; editor shows role; unchanged save preserves inheritance and follows role updates',()=>{
 const f=fixture();f.run('addRoleNode("role",0)');
 assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.run('openInstructionEditor()');assert.equal(f.textarea.value,'角色提示词');f.save();
 assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.role.duty='新版角色提示词';f.run('openInstructionEditor()');assert.equal(f.textarea.value,'新版角色提示词');
});
test('custom content overrides; clearing or matching role restores inheritance',()=>{
 const f=fixture();f.run('addRoleNode("role",0);openInstructionEditor()');
 f.textarea.value='自定义步骤';f.save();f.role.duty='新版角色';
 f.run('openInstructionEditor()');assert.equal(f.textarea.value,'自定义步骤');
 f.textarea.value='  新版角色  ';f.save();assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.run('openInstructionEditor()');f.textarea.value='   ';f.save();
 assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.run('openInstructionEditor()');assert.equal(f.textarea.value,'新版角色');
});

test('source colors track edits and reset without changing the draft until confirmed',()=>{
 const f=fixture();f.run('addRoleNode("role",0);openInstructionEditor()');
 assert.equal(f.label.textContent,'继承角色');assert.equal(f.label.className,'instruction-source inherited');
 assert.equal(f.reset.hidden,true);
 f.textarea.value='独立要求';f.input();
 assert.equal(f.label.textContent,'自定义');assert.equal(f.label.className,'instruction-source custom');
 assert.equal(f.reset.hidden,false);assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.save();f.run('openInstructionEditor()');f.restore();
 assert.equal(f.textarea.value,'角色提示词');assert.equal(f.label.textContent,'继承角色');
 assert.equal(f.state.sop.draft.steps[0].instruction,'独立要求');
 f.cancel();f.run('openInstructionEditor()');assert.equal(f.textarea.value,'独立要求');
 f.restore();f.save();assert.equal(f.state.sop.draft.steps[0].instruction,'');
 f.run('openInstructionEditor()');f.textarea.value='  ';f.input();assert.equal(f.label.textContent,'继承角色');
});
