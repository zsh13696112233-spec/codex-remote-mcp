const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/main/resources/static/app.js','utf8');
function fixture(){
  const nodes=new Map();
  const $=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{
      value:'',listeners:{},validity:'',open:false,
      addEventListener(type,handler){this.listeners[type]=handler},
      setCustomValidity(message){this.validity=message},
      reportValidity(){this.reported=true},focus(){},
      showModal(){this.open=true},close(){this.open=false},
    });
    return nodes.get(selector);
  };
  const form=$('#roleForm');
  Object.assign(form,{duty:{value:'原职责'},dataset:{},elements:{groupId:{value:'group'}},name:{value:'角色'},enabled:{checked:true},id:{value:'role'},version:{value:'2'},querySelector:()=>$('submit')});
  const requests=[];
  const context=vm.createContext({$,toast(){},render(){},api:async(path,options)=>requests.push(JSON.parse(options.body))});
  vm.runInContext(source.slice(source.indexOf('function openRoleDutyEditor()'),source.indexOf('function taskRequirements')),context);
  vm.runInContext(source.split('\n').find(line=>line.startsWith('$("#roleForm").addEventListener("submit"')),context);
  return {$,form,requests,context,open:()=>$('[data-role-duty-edit]').listeners.click(),confirm:()=>$('#roleDutyForm').listeners.submit({preventDefault(){}}),save:()=>form.listeners.submit({preventDefault(){},currentTarget:form})};
}
test('unconfirmed edits are discarded on reopening; confirmation only updates the role draft',()=>{
  const h=fixture();h.open();assert.equal(h.$('#roleDutyText').value,'原职责');
  h.$('#roleDutyText').value='未确认';h.$('#roleDutyDialog').close();h.open();
  assert.equal(h.$('#roleDutyText').value,'原职责');
  const longText='职责\n'.repeat(10000);h.$('#roleDutyText').value=longText;h.confirm();
  assert.equal(h.form.duty.value,longText);assert.equal(h.requests.length,0);assert.equal(h.$('#roleDutyDialog').open,false);
});
test('blank responsibilities cannot be confirmed or saved',async()=>{
  const h=fixture();h.open();h.$('#roleDutyText').value='  \n';h.confirm();
  assert.equal(h.form.duty.value,'原职责');assert.equal(h.$('#roleDutyDialog').open,true);assert.equal(h.$('#roleDutyText').validity,'请填写角色职责');
  h.form.duty.value='';await h.save();assert.equal(h.requests.length,0);assert.equal(h.$('#roleDutyText').reported,true);
});
test('save failure retains confirmed text and releases the submit lock for retry',async()=>{
  const h=fixture();h.open();h.$('#roleDutyText').value='新的职责';h.confirm();
  h.context.api=async()=>{throw Error('保存失败')};await h.save();
  assert.equal(h.form.duty.value,'新的职责');assert.equal(h.form.dataset.saving,undefined);assert.equal(h.$('submit').disabled,false);
  h.context.api=async(path,options)=>h.requests.push(JSON.parse(options.body));await h.save();
  assert.equal(h.requests[0].duty,'新的职责');assert.equal(h.requests[0].version,2);
});
